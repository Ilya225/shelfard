"""
Consumer projections and consumer impact — pure functions, no registry I/O.

A consumer projection is the set of column paths a consumer reads (``id``,
``address.zip``, ``tags[]``, ``tags[].sku``) plus the shape it expects for
them. For a schema change this module answers, per change and per consumer:

  * does the change touch any consumer?   → ``consumer_impact``
    (``no_consumers`` / ``consumer_constrained``)
  * how severe is it *for that consumer*? → the consumer's own diff
    (expected shape vs. the new schema projected onto the same paths), so
    e.g. int → bigint is SAFE for a consumer that reads the column, and a
    removed column it doesn't read doesn't concern it at all.

``annotate`` also decides whether a baseline update can be suggested: always
when every change is SAFE (``kind: "safe"``), and — opt-in at apply time —
when every WARNING/BREAKING change touches no consumer (``kind: "unaffected"``).
Projections themselves are never modified here: they belong to the consumers.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, Optional

from .models import (
    ChangeSeverity, ChangeType, ColumnChange, ColumnSchema, ColumnType, ConsumerImpact,
    ConsumerProjection, TableSchema,
)

_PART_RE = re.compile(r"^(.*?)((?:\[\])*)$")

_RANK = {ChangeSeverity.SAFE: 0, ChangeSeverity.WARNING: 1, ChangeSeverity.BREAKING: 2}


def _sev(value) -> ChangeSeverity:
    return ChangeSeverity(value)


def worst(severities: Iterable) -> Optional[ChangeSeverity]:
    ranked = [_sev(s) for s in severities]
    return max(ranked, key=_RANK.__getitem__) if ranked else None


# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────

def parse_path(path: str) -> list[str]:
    """``tags[].sku`` → ``["tags", "[]", "sku"]``; ``address.zip`` → ``["address", "zip"]``."""
    tokens: list[str] = []
    for part in path.split("."):
        m = _PART_RE.match(part)
        name, brackets = m.group(1), m.group(2)
        if name:
            tokens.append(name)
        tokens.extend(["[]"] * (len(brackets) // 2))
    return tokens


def _is_table_level(path: str) -> bool:
    """``<partition_keys>``, ``<clustering_keys>``, ``<multiple>`` (reorders): no column."""
    return any(t.startswith("<") for t in parse_path(path))


def touches(change_path: str, consumer_paths: Iterable[str]) -> bool:
    """True if a change at *change_path* concerns any of *consumer_paths*: the
    same path, a field nested under a read column (``address.zip`` under
    ``address``) or a parent of a read field (``address`` removed while
    ``address.zip`` is read). Table-level changes touch no column."""
    if _is_table_level(change_path):
        return False
    c = parse_path(change_path)
    for p in consumer_paths:
        t = parse_path(p)
        n = min(len(c), len(t))
        if c[:n] == t[:n]:
            return True
    return False


def _tree(paths: Iterable[str]) -> dict:
    """Path list → nested dict; a node value of None means "the whole column"."""
    root: dict = {}
    for path in paths:
        node = root
        tokens = parse_path(path)
        for i, tok in enumerate(tokens):
            last = i == len(tokens) - 1
            if node.get(tok, {}) is None:       # an ancestor is already read whole
                break
            if last:
                node[tok] = None
            else:
                node = node.setdefault(tok, {})
    return root


def _prune(col: ColumnSchema, sub: Optional[dict]) -> ColumnSchema:
    if sub is None:
        return col
    if col.col_type == ColumnType.STRUCT and col.fields is not None:
        by_name = {f.name: f for f in col.fields}
        fields = [_prune(by_name[name], child) for name, child in sub.items()
                  if name != "[]" and name in by_name]
        return replace(col, fields=fields)
    if col.col_type == ColumnType.ARRAY and col.items is not None and "[]" in sub:
        return replace(col, items=_prune(col.items, sub["[]"]))
    return col                                   # can't descend: the column as a whole


def project(schema: TableSchema, paths: Iterable[str]) -> TableSchema:
    """*schema* reduced to *paths*, in path order. Missing paths are left out (so
    they show up as removals in a diff); partition / clustering keys are dropped."""
    tree = _tree(paths)
    by_name = {c.name: c for c in schema.columns}
    columns = [_prune(by_name[name], sub) for name, sub in tree.items() if name in by_name]
    return TableSchema(table_name=schema.table_name, columns=columns,
                       source=schema.source, captured_at=schema.captured_at)


def missing_paths(schema: TableSchema, paths: Iterable[str]) -> list[str]:
    """Paths that don't exist in *schema*."""
    missing = []
    for path in paths:
        tokens = parse_path(path)
        cols = schema.columns
        col: Optional[ColumnSchema] = None
        ok = bool(tokens)
        for tok in tokens:
            if tok == "[]":
                if col is None or col.col_type != ColumnType.ARRAY or col.items is None:
                    ok = False
                    break
                col = col.items
                cols = col.fields or []
                continue
            col = next((c for c in cols if c.name == tok), None)
            if col is None:
                ok = False
                break
            cols = col.fields or []
        if not ok:
            missing.append(path)
    return missing


# ─────────────────────────────────────────────
# Per-consumer diff and annotation
# ─────────────────────────────────────────────

def consumer_diff(projection: ConsumerProjection, new_schema: TableSchema) -> dict:
    """The consumer's own diff: expected shape vs. *new_schema* on the same paths."""
    from .schema_comparison import compare_schemas

    result = compare_schemas(projection.expected_schema(), project(new_schema, projection.columns))
    if not result.success:
        raise ValueError(result.error)
    return result.data["diff"]


def _impact_for(change_path: str, cdiff: dict) -> Optional[ChangeSeverity]:
    """Worst severity of the consumer's own changes that concern *change_path*."""
    return worst(c["severity"] for c in cdiff["changes"]
                 if not _is_table_level(c["column_name"])
                 and touches(c["column_name"], [change_path]))


def annotate(diff: dict, projections: list[ConsumerProjection],
             old_schema: TableSchema, new_schema: TableSchema) -> dict:
    """Return *diff* (a serialized SchemaDiff) with consumer impact filled in:
    per-change ``consumer_impact`` / ``consumers``, ``consumer_summary`` and a
    recomputed ``suggested_update``."""
    from .schema_comparison import build_suggested_update, explain_change

    diff = dict(diff)
    cdiffs = {p.consumer_name: consumer_diff(p, new_schema) for p in projections}

    affected: dict[str, dict] = {}
    changes = []
    for change in diff["changes"]:
        change = dict(change)
        entries = []
        if not _is_table_level(change["column_name"]):
            for p in projections:
                severity = _impact_for(change["column_name"], cdiffs[p.consumer_name])
                if severity is None:
                    continue
                entries.append({"consumer": p.consumer_name, "severity": severity})
                slot = affected.setdefault(p.consumer_name, {"severities": [], "changes": []})
                slot["severities"].append(severity)
                slot["changes"].append(change["column_name"])
        change["consumers"] = entries
        change["consumer_impact"] = (ConsumerImpact.CONSUMER_CONSTRAINED if entries
                                     else ConsumerImpact.NO_CONSUMERS)
        changes.append(change)
    diff["changes"] = changes

    by_severity = {s.value: 0 for s in ChangeSeverity}
    consumers = []
    for name, slot in affected.items():
        severity = worst(slot["severities"])
        by_severity[severity.value] += 1
        consumers.append({"consumer": name, "severity": severity, "changes": slot["changes"]})
    outdated = [
        {"consumer": p.consumer_name, "severity": _sev(cdiffs[p.consumer_name]["overall_severity"]),
         "changes": [c["column_name"] for c in cdiffs[p.consumer_name]["changes"]]}
        for p in projections
        if p.consumer_name not in affected and cdiffs[p.consumer_name]["changes"]
    ]
    diff["consumer_summary"] = {
        "total": len(projections),
        "affected": len(affected),
        "by_severity": by_severity,
        "consumers": consumers,
        "outdated": outdated,
    }

    diff["suggested_update"] = None
    if changes:
        non_safe = [c for c in changes if _sev(c["severity"]) != ChangeSeverity.SAFE]
        if not non_safe:
            kind = "safe"
        elif all(c["consumer_impact"] == ConsumerImpact.NO_CONSUMERS for c in non_safe):
            kind = "unaffected"
        else:
            kind = None
        if kind:
            objs = [ColumnChange(**{**c, "change_type": ChangeType(c["change_type"]),
                                    "severity": _sev(c["severity"])}) for c in changes]
            update = build_suggested_update(old_schema, new_schema, objs)
            update["kind"] = kind
            update["explanation"] = [
                explain_change(c) + ("" if _sev(c.severity) == ChangeSeverity.SAFE
                                     else f" — {c.severity.value}, no consumer reads it")
                for c in objs
            ]
            diff["suggested_update"] = update
    return diff


def outdated_after(projections: list[ConsumerProjection], schema: TableSchema) -> list[dict]:
    """Consumers whose expected shape differs from *schema* (with their own severity)."""
    out = []
    for p in projections:
        d = consumer_diff(p, schema)
        if d["changes"]:
            out.append({"consumer": p.consumer_name, "severity": _sev(d["overall_severity"]),
                        "changes": [c["column_name"] for c in d["changes"]]})
    return out
