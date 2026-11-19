"""
Layer 2: Schema Comparison Engine

This is the core of Day 1. Pure deterministic logic —
no LLM involved here, only in interpreting the results.

The output SchemaDiff is designed to be:
1. Complete — the agent shouldn't need to re-examine the schemas
2. Self-explaining — each change includes its own reasoning string
3. Actionable — severity is pre-classified so the agent can route appropriately
"""

from dataclasses import replace

from .models import (
    ColumnConstraints, ColumnSchema, TableSchema, SchemaDiff, ColumnChange,
    ChangeType, ChangeSeverity, ToolResult, ColumnType
)
from .type_normalizer import is_safe_widening
from .parsers.json_reader import get_schema_from_json


# ─────────────────────────────────────────────
# Sampled sources
# Schemas inferred from example payloads (REST responses, JSON files) say
# what one sample looked like, not what the source guarantees:
#   * a null value is inferred as UNKNOWN — the type simply wasn't observed;
#   * a non-null value is inferred as NOT NULL — absence of nulls proves nothing;
#   * a new field can't break a reader, and JSON key order is meaningless.
# ─────────────────────────────────────────────

SAMPLED_SOURCES = frozenset({"rest_api", "json_file"})


def _is_sampled(old_schema: TableSchema, new_schema: TableSchema) -> bool:
    return old_schema.source in SAMPLED_SOURCES or new_schema.source in SAMPLED_SOURCES


# ─────────────────────────────────────────────
# Severity classification rules
# These are deterministic rules that avoid wasting LLM tokens
# on clear-cut cases. The LLM reasons about ambiguous ones.
# ─────────────────────────────────────────────

def _classify_added_column(col: ColumnSchema, sampled: bool = False) -> ChangeSeverity:
    """
    Adding a column is SAFE if nullable or has a default.
    BREAKING if NOT NULL with no default — existing rows can't satisfy it.
    For sampled (read-only payload) sources an extra field is always SAFE.
    """
    if sampled:
        return ChangeSeverity.SAFE
    if not col.nullable and col.default_value is None:
        return ChangeSeverity.BREAKING
    return ChangeSeverity.SAFE


def _classify_removed_column(col: ColumnSchema) -> ChangeSeverity:
    """Removing any column is always BREAKING — downstream SELECT * or named access fails."""
    return ChangeSeverity.BREAKING


def _length(col: ColumnSchema) -> int | None:
    """VARCHAR length; None (or a legacy 0) means unbounded."""
    return col.max_length or None


def _varchar(n: int | None) -> str:
    return f"VARCHAR({n})" if n else "unbounded VARCHAR"


def _classify_varchar_change(old_col, new_col):
    old_len, new_len = _length(old_col), _length(new_col)
    if new_len is None or (old_len is not None and new_len > old_len):
        return (
            ChangeType.TYPE_WIDENED,
            ChangeSeverity.SAFE,
            f"{_varchar(old_len)} → {_varchar(new_len)} — safe widening.",
        )
    return (
        ChangeType.TYPE_CHANGED,
        ChangeSeverity.BREAKING,
        f"{_varchar(old_len)} → {_varchar(new_len)} — narrowing; existing values longer "
        f"than {new_len} characters may be rejected or truncated.",
    )


def _decimal(p, s) -> str:
    return f"DECIMAL({p},{s or 0})" if p is not None else "unbounded DECIMAL"


def _classify_decimal_change(old_col, new_col):
    op, os_ = old_col.precision, old_col.scale or 0
    np_, ns = new_col.precision, new_col.scale or 0
    label = f"{_decimal(op, os_)} → {_decimal(np_, ns)}"
    problems = []
    if np_ is not None:
        if op is None:
            problems.append("precision is now bounded")
        else:
            if ns < os_:
                problems.append(f"scale decreased from {os_} to {ns} — fractional digits will be rounded")
            if np_ - ns < op - os_:
                problems.append(
                    f"integer digits decreased from {op - os_} to {np_ - ns} — large values may overflow"
                )
    if problems:
        return (
            ChangeType.TYPE_CHANGED,
            ChangeSeverity.WARNING,
            f"{label}: {'; '.join(problems)}.",
        )
    return (ChangeType.TYPE_WIDENED, ChangeSeverity.SAFE, f"{label} — safe widening.")


def _classify_type_change(
    old_col: ColumnSchema,
    new_col: ColumnSchema
) -> tuple[ChangeType, ChangeSeverity, str]:
    """
    Returns (change_type, severity, reasoning).
    """
    old_t = old_col.col_type
    new_t = new_col.col_type

    # Same logical type — check length/precision changes
    if old_t == new_t:
        if old_t == ColumnType.VARCHAR:
            return _classify_varchar_change(old_col, new_col)
        if old_t == ColumnType.DECIMAL:
            return _classify_decimal_change(old_col, new_col)
        return (ChangeType.TYPE_CHANGED, ChangeSeverity.SAFE,
                f"{old_t.value} attributes changed (length/precision/scale).")

    # Different types — check widening rules
    if is_safe_widening(old_t, new_t):
        return (
            ChangeType.TYPE_WIDENED,
            ChangeSeverity.SAFE,
            f"{old_t.value} → {new_t.value} is a safe numeric widening."
        )

    # Unknown or dangerous type change
    return (
        ChangeType.TYPE_CHANGED,
        ChangeSeverity.BREAKING,
        f"{old_t.value} → {new_t.value} is a potentially breaking type change. "
        f"Downstream consumers expecting {old_t.value} will fail or produce incorrect results."
    )


def _classify_nullability_change(
    old_col: ColumnSchema,
    new_col: ColumnSchema
) -> tuple[ChangeSeverity, str]:
    if not old_col.nullable and new_col.nullable:
        return (
            ChangeSeverity.SAFE,
            "Column changed from NOT NULL to NULL — safe, relaxes constraints."
        )
    else:  # nullable → not nullable
        return (
            ChangeSeverity.BREAKING,
            "Column changed from NULL to NOT NULL — "
            "any existing NULL values will violate the constraint."
        )


def _physical_type_change(old_col: ColumnSchema, new_col: ColumnSchema,
                          qualified: str) -> list[ColumnChange]:
    """Same normalized type, different vendor type (e.g. timestamptz → timestamp).
    Compared only when both sides recorded a physical type and the type is known.
    STRUCT / ARRAY are skipped: their fields and items are compared themselves."""
    if (old_col.physical_type is None or new_col.physical_type is None
            or old_col.physical_type == new_col.physical_type
            or old_col.col_type != new_col.col_type
            or old_col.col_type in (ColumnType.UNKNOWN, ColumnType.STRUCT, ColumnType.ARRAY)):
        return []
    return [ColumnChange(
        change_type=ChangeType.PHYSICAL_TYPE_CHANGED,
        column_name=qualified,
        old_value={"physical_type": old_col.physical_type},
        new_value={"physical_type": new_col.physical_type},
        severity=ChangeSeverity.WARNING,
        reasoning=f"Physical type of '{qualified}' changed from {old_col.physical_type} to "
                  f"{new_col.physical_type} (still {new_col.col_type.value}). Semantics such as time "
                  f"zone handling, padding or storage size may differ — verify consumers.",
    )]


_CONSTRAINT_LABELS = {
    "primary_key": "primary key", "primary_key_position": "primary key position",
    "unique": "unique", "format": "format", "pattern": "pattern",
    "minimum": "minimum", "maximum": "maximum",
    "exclusive_minimum": "exclusive minimum", "exclusive_maximum": "exclusive maximum",
    "multiple_of": "multiple of", "min_length": "min length",
    "min_items": "min items", "max_items": "max items", "unique_items": "unique items",
}


def _constraint_change(old_col: ColumnSchema, new_col: ColumnSchema,
                       qualified: str) -> list[ColumnChange]:
    """Any constraint difference is a WARNING. An attribute is compared only
    when both sides know it (``None`` = not observable / not stated), so older
    baselines and sources that can't see a constraint never produce noise."""
    old_d = old_col.constraints.known() if old_col.constraints else {}
    new_d = new_col.constraints.known() if new_col.constraints else {}
    differing = [k for k in old_d if k in new_d and old_d[k] != new_d[k]]
    if not differing:
        return []
    phrases = []
    for k in differing:
        label, before, after = _CONSTRAINT_LABELS.get(k, k), old_d[k], new_d[k]
        if isinstance(before, bool) and isinstance(after, bool):
            phrases.append(f"{label} {'added' if after else 'removed'}")
        else:
            phrases.append(f"{label} {before} → {after}")
    return [ColumnChange(
        change_type=ChangeType.CONSTRAINT_CHANGED,
        column_name=qualified,
        old_value={k: old_d[k] for k in differing},
        new_value={k: new_d[k] for k in differing},
        severity=ChangeSeverity.WARNING,
        reasoning=f"Constraints on '{qualified}' changed: {'; '.join(phrases)}. Consumers may rely on "
                  f"these guarantees (joins, deduplication, validation) — verify this is intentional.",
    )]


def _type_attrs(col: ColumnSchema) -> dict:
    return {"col_type": col.col_type.value, "max_length": col.max_length,
            "precision": col.precision, "scale": col.scale}


def _type_attrs_differ(old_col: ColumnSchema, new_col: ColumnSchema) -> bool:
    return (old_col.col_type != new_col.col_type
            or _length(old_col) != _length(new_col)
            or old_col.precision != new_col.precision
            or (old_col.scale or 0) != (new_col.scale or 0))


# ─────────────────────────────────────────────
# Core diff logic (recursive, prefix-aware)
# ─────────────────────────────────────────────

def _diff_column(
    old_col: ColumnSchema,
    new_col: ColumnSchema,
    qualified: str,
    sampled: bool,
) -> list[ColumnChange]:
    """All changes between two versions of one column. Each aspect (type,
    physical type, constraints, nullability, default) is checked independently, so a column can report
    several changes. STRUCT fields and ARRAY items are compared recursively
    (``addr.zip``, ``tags[]``, ``tags[].x``)."""
    changes: list[ColumnChange] = []
    old_t, new_t = old_col.col_type, new_col.col_type

    # Sampled source, value null in this sample: the type wasn't observed.
    # Keep the baseline type; at most the column is now known to be nullable.
    if sampled and new_t == ColumnType.UNKNOWN and old_t != ColumnType.UNKNOWN:
        if new_col.nullable and not old_col.nullable:
            changes.append(ColumnChange(
                change_type=ChangeType.NULLABILITY_RELAXED,
                column_name=qualified,
                old_value={"nullable": False},
                new_value={"nullable": True},
                severity=ChangeSeverity.SAFE,
                reasoning=f"'{qualified}' was null in this sample, so its type couldn't be observed. "
                          f"The baseline type ({old_t.value}) is kept and the column is now "
                          f"recorded as nullable.",
            ))
        return changes

    # Type was unknown in the baseline and is known now.
    if old_t == ColumnType.UNKNOWN and new_t != ColumnType.UNKNOWN:
        why = ("no value was observed when the baseline was captured — null or an empty array"
               if sampled else "the baseline type was not recognized")
        changes.append(ColumnChange(
            change_type=ChangeType.TYPE_RESOLVED,
            column_name=qualified,
            old_value=_type_attrs(old_col),
            new_value=_type_attrs(new_col),
            severity=ChangeSeverity.SAFE,
            reasoning=f"'{qualified}' had an unknown type ({why}); it is now {new_t.value}. "
                      f"Suggest recording {new_t.value} as its type in the next baseline version.",
        ))
        if not sampled and old_col.nullable != new_col.nullable:
            changes.extend(_nullability_changes(old_col, new_col, qualified))
        return changes

    if old_t == ColumnType.STRUCT and new_t == ColumnType.STRUCT:
        changes.extend(_diff_column_list(old_col.fields or [], new_col.fields or [],
                                         f"{qualified}.", sampled))
    elif old_t == ColumnType.ARRAY and new_t == ColumnType.ARRAY:
        # Item types are compared only when both sides recorded them (older
        # baselines and some readers don't).
        if old_col.items is not None and new_col.items is not None:
            changes.extend(_diff_column(old_col.items, new_col.items, f"{qualified}[]", sampled))
    elif _type_attrs_differ(old_col, new_col):
        change_type, severity, reasoning = _classify_type_change(old_col, new_col)
        changes.append(ColumnChange(
            change_type=change_type,
            column_name=qualified,
            old_value=_type_attrs(old_col),
            new_value=_type_attrs(new_col),
            severity=severity,
            reasoning=reasoning,
        ))

    changes.extend(_physical_type_change(old_col, new_col, qualified))
    changes.extend(_constraint_change(old_col, new_col, qualified))

    if old_col.nullable != new_col.nullable:
        # In a sample, "no nulls seen" doesn't make a column NOT NULL.
        if not (sampled and not new_col.nullable):
            changes.extend(_nullability_changes(old_col, new_col, qualified))

    if old_col.default_value != new_col.default_value:
        changes.append(ColumnChange(
            change_type=ChangeType.DEFAULT_CHANGED,
            column_name=qualified,
            old_value={"default_value": old_col.default_value},
            new_value={"default_value": new_col.default_value},
            severity=ChangeSeverity.WARNING,
            reasoning=f"Default value changed from '{old_col.default_value}' "
                      f"to '{new_col.default_value}'. "
                      f"New rows will have different defaults — verify this is intentional."
        ))
    return changes


def _nullability_changes(old_col, new_col, qualified) -> list[ColumnChange]:
    severity, reasoning = _classify_nullability_change(old_col, new_col)
    return [ColumnChange(
        change_type=(ChangeType.NULLABILITY_RELAXED if new_col.nullable
                     else ChangeType.NULLABILITY_TIGHTENED),
        column_name=qualified,
        old_value={"nullable": old_col.nullable},
        new_value={"nullable": new_col.nullable},
        severity=severity,
        reasoning=reasoning,
    )]


def _diff_column_list(
    old_columns: list[ColumnSchema],
    new_columns: list[ColumnSchema],
    name_prefix: str = "",
    sampled: bool = False,
) -> list[ColumnChange]:
    """
    Compare two lists of ColumnSchema and return all changes.

    name_prefix is dot-notation context for nested STRUCT fields,
    e.g. "address." produces change names like "address.street".
    """
    old_cols = {col.name: col for col in old_columns}
    new_cols = {col.name: col for col in new_columns}

    changes: list[ColumnChange] = []

    # ── Detect removed columns ──────────────────────────────────────
    for col_name, old_col in old_cols.items():
        if col_name not in new_cols:
            qualified = f"{name_prefix}{col_name}"
            changes.append(ColumnChange(
                change_type=ChangeType.COLUMN_REMOVED,
                column_name=qualified,
                old_value=old_col.to_dict(),
                new_value=None,
                severity=_classify_removed_column(old_col),
                reasoning=f"Column '{qualified}' ({old_col.col_type.value}) was removed. "
                          f"All consumers reading this column will fail."
            ))

    # ── Detect added columns ────────────────────────────────────────
    for col_name, new_col in new_cols.items():
        if col_name not in old_cols:
            qualified = f"{name_prefix}{col_name}"
            severity = _classify_added_column(new_col, sampled)
            if sampled:
                detail = "New field in the payload — existing readers are unaffected."
            elif severity == ChangeSeverity.BREAKING:
                detail = "NOT NULL with no default — will break INSERT statements on existing pipeline logic."
            else:
                detail = "Nullable or has default — safe addition, existing queries unaffected."
            changes.append(ColumnChange(
                change_type=ChangeType.COLUMN_ADDED,
                column_name=qualified,
                old_value=None,
                new_value=new_col.to_dict(),
                severity=severity,
                reasoning=f"Column '{qualified}' ({new_col.col_type.value}) was added. {detail}",
            ))

    # ── Detect modifications on existing columns ────────────────────
    for col_name, old_col in old_cols.items():
        if col_name in new_cols:
            changes.extend(_diff_column(old_col, new_cols[col_name],
                                        f"{name_prefix}{col_name}", sampled))

    # ── Detect column reordering (meaningless for JSON objects) ─────
    if not sampled:
        old_order = [c.name for c in old_columns if c.name in new_cols]
        new_order = [c.name for c in new_columns if c.name in old_cols]
        if old_order != new_order:
            changes.append(ColumnChange(
                change_type=ChangeType.COLUMN_REORDERED,
                column_name=f"{name_prefix}<multiple>",
                old_value={"order": old_order},
                new_value={"order": new_order},
                severity=ChangeSeverity.WARNING,
                reasoning="Column order changed. Named column access is unaffected, "
                          "but any positional access (SELECT *, CSV exports, legacy COBOL-style consumers) "
                          "may read wrong values silently — the most dangerous kind of bug."
            ))

    return changes


def _diff_keys(old_schema: TableSchema, new_schema: TableSchema) -> list[ColumnChange]:
    """Partition and clustering/sort keys (WARNING: layout and pruning, not correctness)."""
    changes = []
    for attr, change_type, label, effect in (
        ("partition_keys", ChangeType.PARTITION_CHANGED, "Partition keys",
         "Partition pruning, file layout and queries filtering on the old partitioning may change."),
        ("clustering_keys", ChangeType.CLUSTERING_CHANGED, "Clustering/sort keys",
         "Data ordering and clustering-based performance may change."),
    ):
        old_keys = list(getattr(old_schema, attr) or [])
        new_keys = list(getattr(new_schema, attr) or [])
        if old_keys != new_keys:
            changes.append(ColumnChange(
                change_type=change_type,
                column_name=f"<{attr}>",
                old_value={attr: old_keys},
                new_value={attr: new_keys},
                severity=ChangeSeverity.WARNING,
                reasoning=f"{label} changed from {old_keys or 'none'} to {new_keys or 'none'}. {effect}",
            ))
    return changes


# ─────────────────────────────────────────────
# Suggested baseline update (only when every change is SAFE)
# ─────────────────────────────────────────────

def _merge_column(old: ColumnSchema, new: ColumnSchema, sampled: bool) -> ColumnSchema:
    """The column to store in the next baseline. For sampled sources keep what
    the baseline already knew: a type isn't forgotten because a value was null,
    and a nullable column stays nullable when a sample happens to have no nulls."""
    if sampled and new.col_type == ColumnType.UNKNOWN and old.col_type != ColumnType.UNKNOWN:
        return replace(old, nullable=old.nullable or new.nullable)
    merged = replace(new)
    # Attributes the new sample didn't record are kept from the baseline.
    if merged.physical_type is None:
        merged.physical_type = old.physical_type
    if old.constraints is not None:
        # Keep attributes the baseline knew and the new sample doesn't.
        known = {**old.constraints.known(), **(new.constraints.known() if new.constraints else {})}
        merged.constraints = ColumnConstraints.from_dict(known)
    if sampled and old.nullable and not new.nullable:
        merged.nullable = True
    if old.col_type == new.col_type == ColumnType.STRUCT:
        merged.fields = _merge_columns(old.fields or [], new.fields or [], sampled)
    elif old.col_type == new.col_type == ColumnType.ARRAY and old.items and new.items:
        merged.items = _merge_column(old.items, new.items, sampled)
    return merged


def _merge_columns(old_cols, new_cols, sampled) -> list[ColumnSchema]:
    old_map = {c.name: c for c in old_cols}
    return [_merge_column(old_map[c.name], c, sampled) if c.name in old_map else c
            for c in new_cols]


def _type_name(t) -> str:
    return getattr(t, "value", t) or "?"


def _fmt_type(attrs: dict | None) -> str:
    attrs = attrs or {}
    t = _type_name(attrs.get("col_type"))
    if t == ColumnType.VARCHAR.value:
        return _varchar(attrs.get("max_length"))
    if t == ColumnType.DECIMAL.value:
        return _decimal(attrs.get("precision"), attrs.get("scale"))
    return t


def explain_change(change: ColumnChange) -> str:
    """One-line description of what the baseline update records for a change."""
    n, new, old = change.column_name, change.new_value or {}, change.old_value or {}
    ct = ChangeType(change.change_type)
    if ct == ChangeType.COLUMN_ADDED:
        return f"add '{n}' ({_type_name(new.get('col_type'))})"
    if ct == ChangeType.TYPE_RESOLVED:
        return f"set the type of '{n}' to {_type_name(new.get('col_type'))} (was unknown)"
    if ct == ChangeType.TYPE_WIDENED:
        return f"widen '{n}' from {_fmt_type(change.old_value)} to {_fmt_type(new)}"
    if ct == ChangeType.NULLABILITY_RELAXED:
        return f"mark '{n}' as nullable"
    if ct == ChangeType.PHYSICAL_TYPE_CHANGED:
        return (f"change the physical type of '{n}' from "
                f"{(change.old_value or {}).get('physical_type')} to {new.get('physical_type')}")
    if ct == ChangeType.CONSTRAINT_CHANGED:
        return f"update constraints on '{n}': {', '.join(sorted(new))}"
    if ct == ChangeType.COLUMN_REMOVED:
        return f"remove '{n}'"
    if ct == ChangeType.TYPE_CHANGED:
        return f"change the type of '{n}' from {_fmt_type(old)} to {_fmt_type(new)}"
    if ct == ChangeType.NULLABILITY_TIGHTENED:
        return f"mark '{n}' as NOT NULL"
    if ct == ChangeType.DEFAULT_CHANGED:
        return f"change the default of '{n}' to {new.get('default_value')!r}"
    if ct == ChangeType.COLUMN_REORDERED:
        return f"record the new column order {new.get('order')}"
    if ct in (ChangeType.PARTITION_CHANGED, ChangeType.CLUSTERING_CHANGED):
        key = next(iter(new), "keys")
        return f"set {key.replace('_', ' ')} to {new.get(key) or 'none'}"
    return f"{ct.value.replace('_', ' ')} on '{n}'"


def build_suggested_update(old_schema: TableSchema, new_schema: TableSchema,
                           changes: list[ColumnChange]) -> dict:
    """Explanation + the schema to register as the next baseline version."""
    sampled = _is_sampled(old_schema, new_schema)
    merged = replace(
        new_schema,
        columns=(_merge_columns(old_schema.columns, new_schema.columns, sampled)
                 if sampled else new_schema.columns),
    )
    return {
        "action": "update_baseline",
        "based_on_version": old_schema.captured_at,
        "explanation": [explain_change(c) for c in changes],
        "schema": merged.to_dict(),
    }


# ─────────────────────────────────────────────
# Main comparison function
# ─────────────────────────────────────────────

def compare_schemas(old_schema: TableSchema, new_schema: TableSchema) -> ToolResult:
    """
    Core diff engine. Compares two TableSchema objects and returns a SchemaDiff.

    Args:
        old_schema: The registered/baseline schema
        new_schema: The incoming/observed schema

    Returns:
        ToolResult with SchemaDiff in data
    """
    try:
        sampled = _is_sampled(old_schema, new_schema)
        changes = _diff_column_list(old_schema.columns, new_schema.columns, sampled=sampled)
        changes.extend(_diff_keys(old_schema, new_schema))

        # ── Compute overall severity ────────────────────────────────────
        severities = [c.severity for c in changes]
        if ChangeSeverity.BREAKING in severities:
            overall = ChangeSeverity.BREAKING
        elif ChangeSeverity.WARNING in severities:
            overall = ChangeSeverity.WARNING
        else:
            overall = ChangeSeverity.SAFE

        # ── Build human-readable summary ────────────────────────────────
        n_breaking = sum(1 for c in changes if c.severity == ChangeSeverity.BREAKING)
        n_warning  = sum(1 for c in changes if c.severity == ChangeSeverity.WARNING)
        n_safe     = sum(1 for c in changes if c.severity == ChangeSeverity.SAFE)

        if not changes:
            summary = "No schema changes detected. Schemas are identical."
        else:
            parts = []
            if n_breaking: parts.append(f"{n_breaking} breaking")
            if n_warning:  parts.append(f"{n_warning} warning")
            if n_safe:     parts.append(f"{n_safe} safe")
            summary = f"{len(changes)} change(s) detected: {', '.join(parts)}."

        diff = SchemaDiff(
            table_name=old_schema.table_name,
            old_schema_version=old_schema.captured_at,
            new_schema_version=new_schema.captured_at,
            changes=changes,
            overall_severity=overall,
            summary=summary,
        )
        if changes and overall == ChangeSeverity.SAFE:
            diff.suggested_update = {**build_suggested_update(old_schema, new_schema, changes),
                                     "kind": "safe"}

        hint = {
            ChangeSeverity.SAFE:     ("All changes are safe. The diff's suggested_update holds the schema to "
                                      "register as the next baseline version (CLI: --update; MCP: "
                                      "accept_schema_update)."),
            ChangeSeverity.WARNING:  "Some changes need review. Inspect WARNING items before proceeding.",
            ChangeSeverity.BREAKING: "BREAKING changes detected. Do NOT auto-apply. Escalate or generate a migration plan.",
        }[overall]

        return ToolResult(
            success=True,
            data={"diff": diff.to_dict()},
            next_action_hint=hint
        )

    except Exception as e:
        return ToolResult(success=False, error=f"Schema comparison failed: {e}")


def compare_schemas_from_dicts(old_dict: dict, new_dict: dict) -> ToolResult:
    """
    Convenience wrapper — takes raw dicts (as returned by schema acquisition tools)
    and compares them. This is what the agent will call most often.
    """
    old_result = get_schema_from_json(old_dict)
    new_result = get_schema_from_json(new_dict)

    if not old_result.success:
        return ToolResult(success=False, error=f"Old schema invalid: {old_result.error}")
    if not new_result.success:
        return ToolResult(success=False, error=f"New schema invalid: {new_result.error}")

    # Keep the original source and captured_at (get_schema_from_json stamps "now").
    old_schema = TableSchema.from_dict({**old_result.data["schema"],
                                        "captured_at": old_dict.get("captured_at"),
                                        "source": old_dict.get("source", "unknown")})
    new_schema = TableSchema.from_dict({**new_result.data["schema"],
                                        "captured_at": new_dict.get("captured_at"),
                                        "source": new_dict.get("source", "unknown")})
    return compare_schemas(old_schema, new_schema)
