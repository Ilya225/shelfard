"""
Console rendering for the CLI — every ``print`` lives here.

Presenters take typed handler results (``SchemaDiff``, ``DriftReport``, …) and
format them for a terminal. Hints that name CLI flags (``--update``,
``--details``) belong here, not in handlers.
"""

from __future__ import annotations

import sys
from typing import Optional

from ..handlers import Response
from ..models import ChangeSeverity, ConsumerImpact, SchemaDiff

# ── ANSI colours ──────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()


def _colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def red(t: str)    -> str: return _colour(t, "31")
def yellow(t: str) -> str: return _colour(t, "33")
def green(t: str)  -> str: return _colour(t, "32")
def bold(t: str)   -> str: return _colour(t, "1")


def sev_colour(sev, text: str) -> str:
    if sev == ChangeSeverity.BREAKING:
        return red(text)
    if sev == ChangeSeverity.WARNING:
        return yellow(text)
    return green(text)


# ── Errors and progress ───────────────────────────────────────────────────────

def error(resp: Response, *, hint: bool = True, file=None) -> int:
    """Print a failed response (message + handler hint); returns exit code 2."""
    print(red(f"✗ {resp.error.message}"), file=file)
    if hint and resp.error.hint:
        print(f"  {resp.error.hint}", file=file)
    return 2


def fail(message: str, file=None) -> int:
    print(red(f"✗ {message}"), file=file)
    return 2


def progress(kind: Optional[str], target: Optional[str]) -> None:
    """The "Fetching … / Reading …" line for a source read."""
    if not target:
        return
    verb = "Fetching" if kind == "rest" else "Reading"
    print(f"{verb} {target} …")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# ── Diffs and consumer impact ─────────────────────────────────────────────────

_SEV_ORDER = (ChangeSeverity.BREAKING, ChangeSeverity.WARNING, ChangeSeverity.SAFE)


def _impact_label(change) -> str:
    """Per-change label suffix: never lists consumers (there may be hundreds)."""
    if change.consumer_impact is None:
        return ""
    if change.consumer_impact == ConsumerImpact.NO_CONSUMERS:
        return " · no consumers"
    return f" · consumer constrained ({len(change.consumers)})"


def severity_counts(entries: list[dict]) -> str:
    """``[{severity}, …]`` → ``"1 BREAKING, 3 SAFE"`` (coloured, worst first)."""
    counts = {s: 0 for s in _SEV_ORDER}
    for e in entries:
        counts[ChangeSeverity(e["severity"])] += 1
    return ", ".join(sev_colour(s, f"{n} {s.value}") for s, n in counts.items() if n)


def _consumer_list(entries: list[dict]) -> str:
    """``[{consumer, severity}, …]`` → ``"crm BREAKING, report SAFE"`` (coloured)."""
    return ", ".join(sev_colour(e["severity"], f"{e['consumer']} {ChangeSeverity(e['severity']).value}")
                     for e in entries)


def _consumer_line(entry: dict) -> str:
    sev = ChangeSeverity(entry["severity"])
    return f"    • {sev_colour(sev, sev.value)} {entry['consumer']}: {', '.join(entry['changes'])}"


def consumers(diff: SchemaDiff, details: bool = False) -> None:
    """Consumer counts after a diff; individual consumers only with ``details``."""
    summary = diff.consumer_summary
    if summary is None:
        return
    table = diff.table_name
    if diff.changes:
        counts = severity_counts(summary["consumers"])
        print(f"  Consumers: {summary['affected']} of {summary['total']} affected"
              + (f" — {counts}" if counts else ""))
        if details:
            for c in summary["consumers"]:
                print(_consumer_line(c))
    outdated = summary.get("outdated") or []
    if outdated:
        print(f"  Outdated projections: {len(outdated)} ({severity_counts(outdated)}) — consumers can "
              f"refresh with 'shelfard consumer refresh {table} --consumer NAME'")
        if details:
            for o in outdated:
                print(_consumer_line(o))
    if not details and (summary["consumers"] and diff.changes or outdated):
        print(f"  Run with --details to list them, or 'shelfard consumers {table}'.")


def diff(schema_name: str, d: SchemaDiff, baseline_version: str, details: bool = False) -> None:
    if not d.changes:
        print(green(f"✓ No drift detected for '{schema_name}'") +
              f"  (last snapshot: {baseline_version})")
        consumers(d, details)
        return

    severity = d.overall_severity
    if severity == ChangeSeverity.SAFE:
        print(green(f"✓ Safe schema changes for '{schema_name}'"))
    else:
        icon = "✗" if severity == ChangeSeverity.BREAKING else "⚠"
        header_fn = red if severity == ChangeSeverity.BREAKING else yellow
        print(header_fn(f"{icon} Schema drift detected for '{schema_name}'"))
    print(f"  {d.summary}")
    print()

    labels = [f"[{ChangeSeverity(c.severity).value}{_impact_label(c)}]" for c in d.changes]
    width = max(len(label) for label in labels)
    for change, label in zip(d.changes, labels):
        label = sev_colour(change.severity, label.ljust(width))
        print(f"  {label} {change.change_type.value:<25} '{change.column_name}'")
        print(f"             {change.reasoning}")
        if details and change.consumers:
            print(f"             Consumers: {_consumer_list(change.consumers)}")
        print()
    consumers(d, details)


# ── Baseline updates ──────────────────────────────────────────────────────────

def update_ignored() -> None:
    print(yellow("  --update ignored: WARNING/BREAKING changes affect consumers. "
                 "Review them, then re-snapshot to accept."))


def suggested_update(d: SchemaDiff, version_number: Optional[int]) -> str:
    """Print the suggested baseline update; returns the hint for applying it."""
    suggestion = d.suggested_update
    target = f"version {version_number} → {version_number + 1}" if version_number else "a new version"
    print()
    print(f"  Suggested baseline update ({target}):")
    for line in suggestion.get("explanation", []):
        print(f"    • {line}")
    if suggestion.get("kind", "safe") == "unaffected":
        n = sum(1 for c in d.changes if c.severity != ChangeSeverity.SAFE)
        print(yellow(f"  Contains {n} WARNING/BREAKING change(s) that no consumer reads."))
        return "Run the same command with --update --allow-unaffected to save it."
    return "Run the same command with --update to save it."


def update_needs_allow_unaffected(hint: str) -> None:
    print(yellow(f"  --update alone only applies all-SAFE updates. {hint}"))


def update_applied(schema_name: str, resp: Response, details: bool = False) -> int:
    if not resp.ok:
        print(red(f"✗ Baseline not updated: {resp.error.message}"))
        return 2
    result = resp.result
    print(green(f"✓ Baseline updated: '{schema_name}' is now version {result.version_count}"))
    outdated = result.outdated_consumers
    if outdated:
        verb = "differs" if len(outdated) == 1 else "differ"
        print(f"  Consumer projections are unchanged; {len(outdated)} now {verb} from the baseline "
              f"({severity_counts(outdated)}). Consumers can refresh with "
              f"'shelfard consumer refresh {schema_name} --consumer NAME'.")
        if details:
            for o in outdated:
                print(_consumer_line(o))
        else:
            print(f"  Run with --details to list them, or 'shelfard consumers {schema_name}'.")
    return 0


# ── Schemas ───────────────────────────────────────────────────────────────────

def schema_columns(schema_dict: dict, indent: int = 0) -> None:
    """Print columns recursively, indenting nested STRUCT fields."""
    pad = "  " * indent
    for col in schema_dict["columns"]:
        prefix = "." if indent > 0 else ""
        name = f"{pad}{prefix}{col['name']}"
        col_type = col["col_type"].upper()
        nullable = "nullable" if col["nullable"] else "NOT NULL"
        notes = []
        if col.get("max_length"):
            notes.append(f"max {col['max_length']}")
        if col.get("precision"):
            notes.append(f"precision {col['precision']},{col.get('scale', 0)}")
        if col.get("physical_type"):
            notes.append(col["physical_type"])
        c = col.get("constraints") or {}
        if c.get("primary_key"):
            pos = c.get("primary_key_position")
            notes.append(f"PK{f' #{pos}' if pos else ''}")
        if c.get("unique"):
            notes.append("unique")
        if c.get("format"):
            notes.append(f"format {c['format']}")
        note_str = f"   ({', '.join(notes)})" if notes else ""
        print(f"  {name:<24} {col_type:<12} {nullable}{note_str}")
        if col.get("fields"):
            schema_columns({"columns": col["fields"]}, indent + 1)
        elif col.get("items"):
            schema_columns({"columns": [{**col["items"], "name": "[]"}]}, indent + 1)


def schema(name: str, result) -> None:
    """A SchemaVersion (with its probe, if any)."""
    schema_dict = result.schema.to_dict()
    print()
    print(bold(f"Schema: {name}") +
          f"  (source: {result.schema.source or 'unknown'} · version: {result.version} · "
          f"{len(schema_dict['columns'])} columns)")
    if result.probe is not None:
        print(f"Probe: {result.probe.probe_type}  (registered {result.probe.registered_at})")
    print()
    schema_columns(schema_dict)
    print()


def schema_list(result) -> None:
    if not result.schemas:
        print("No schemas registered yet.")
        return
    print(f"\nSchemas ({len(result.schemas)}):\n")
    for s in result.schemas:
        ver_word = "version" if s.version_count == 1 else "versions"
        print(
            f"  {s.name:<20} {s.column_count} cols   "
            f"{s.version_count} {ver_word:<10} "
            f"{s.source or 'unknown':<14} {s.latest_version}"
        )
    print()


def snapshot(result, kind: str) -> None:
    schema_obj = result.schema
    cols = len(schema_obj.columns)
    if kind in ("file", "iceberg"):
        print(green(f"✓ Snapshot saved: '{result.name}' (version {result.version_count}, "
                    f"{schema_obj.source}, {cols} top-level columns)"))
        if result.metadata.get("metadata_file"):
            print(f"  metadata: {result.metadata['metadata_file']}")
        if schema_obj.partition_keys:
            print(f"  partitioned by: {', '.join(schema_obj.partition_keys)}")
        if schema_obj.clustering_keys:
            print(f"  sorted by: {', '.join(schema_obj.clustering_keys)}")
    else:
        print(green(f"✓ Snapshot saved: '{result.name}' (version {result.version_count}, "
                    f"{cols} top-level columns)"))
    if result.probe is not None:
        detail = kind if kind in ("file", "iceberg") else f"{kind} · {plural(len(result.probe.env), 'env var')}"
        print(green(f"✓ Probe registered for '{result.name}'") + f"  ({detail})")
    elif result.probe_error:
        print(yellow(f"⚠ Snapshot saved but probe registration failed: {result.probe_error}"))


# ── Consumers ─────────────────────────────────────────────────────────────────

def projection_state(state) -> str:
    """'  · up to date' / '  · outdated (SAFE: a, b)' against the latest schema."""
    if state is None:
        return ""
    if state.up_to_date:
        return "  · up to date"
    return "  · " + sev_colour(state.severity, f"outdated ({state.severity.value}: {', '.join(state.columns)})")


def subscriptions(result) -> None:
    if not result.consumers:
        print("No subscriptions registered yet.")
        return
    print(f"\nConsumer subscriptions ({len(result.consumers)}):\n")
    for view in result.consumers:
        p = view.projection
        cols_str = ", ".join(p.columns)
        if p.full:
            cols_str = f"all columns ({cols_str})"
        print(
            f"  {view.consumer:<20} {p.source_table:<14} "
            f"{cols_str:<30} updated {p.updated_at}{projection_state(view.state)}"
        )
    print()


def table_consumers(table: str, result) -> None:
    print(f"\nConsumers of '{table}': {len(result.consumers)}\n")
    for view in result.consumers:
        cols = ", ".join(view.projection.columns)
        print(f"  {view.consumer:<20} {cols:<40}{projection_state(view.state)}")
    print()


def subscribed(consumer: str, table: str, result, all_columns: bool) -> None:
    scope = "all columns" if all_columns else "columns"
    print(green(f"✓ '{consumer}' subscribed to '{table}' — {scope}: {', '.join(result.projection.columns)}"))
    print("  The expected shape of these columns is saved; future checks report the impact on them.")


def refreshed(consumer: str, table: str, result) -> None:
    if not result.adopted_changes:
        print(green(f"✓ '{consumer}' projection on '{table}' was already up to date"))
        return
    print(green(f"✓ '{consumer}' adopted the current shape of '{table}':"))
    for c in result.adopted_changes:
        print(f"    • {c['column_name']}: {c['reasoning']}")


# ── Probes ────────────────────────────────────────────────────────────────────

def probe(name: str, config) -> None:
    probe_type = config.probe_type
    print(f"\nProbe: {name}")
    print(f"  type:  {probe_type}")
    if probe_type == "postgres":
        print(f"  dsn:   {config.dsn}")
        if config.table:
            print(f"  table: {config.table}")
        if config.query:
            print(f"  query: {config.query}")
        if config.db_schema and config.db_schema != "public":
            print(f"  schema: {config.db_schema}")
        if config.env:
            print(f"  env:   {', '.join(config.env)}")
    elif probe_type in ("file", "iceberg"):
        print(f"  path:   {config.path}")
        if probe_type == "file":
            print(f"  format: {config.file_format or 'auto'}")
        if config.env:
            print(f"  env:    {', '.join(config.env)}")
    else:
        print(f"  url:   {config.url}")
        if config.env:
            print(f"  env:   {', '.join(config.env)}")
        if config.headers:
            print("  headers:")
            for entry in config.headers:
                for k, v in entry.items():
                    print(f"    {k}: {v}")
    print()


def probe_list(result) -> None:
    if not result.probes:
        print("No probes registered yet.")
        return
    print(f"\nProbes ({len(result.probes)}):\n")
    for p in result.probes:
        env_str = ", ".join(p.env) if p.env else "—"
        print(
            f"  {p.schema_name:<20} {p.probe_type:<8} "
            f"{p.target or '—':<45} {env_str:<20} {p.registered_at or ''}"
        )
    print()


def probe_registered(name: str, config) -> None:
    print(green(f"✓ Probe registered for '{name}'") +
          f"  ({config.probe_type} · {plural(len(config.env), 'env var')})")


# ── Template variables ────────────────────────────────────────────────────────

def var_list(result) -> None:
    if not result.vars:
        print("No template variables set.")
        return
    print(f"\nTemplate variables ({len(result.vars)}):\n")
    for name, value in sorted(result.vars.items()):
        print(f"  {{{{ {name:<20} }}}}  =  {value!r}")
    print()


# ── Sources ───────────────────────────────────────────────────────────────────

def source_listing(result, empty: str, title: str, with_format: bool) -> None:
    if not result.tables:
        print(empty)
        return
    print(f"\n{title} ({len(result.tables)}):\n")
    for t in result.tables:
        if with_format:
            print(f"  {t.name:<30} {t.file_format or '':<8} {t.path}")
        else:
            print(f"  {t.name:<30} {t.path}")
    print()


# ── ODCS ──────────────────────────────────────────────────────────────────────

def ignored_fields(ignored: dict) -> None:
    if not ignored:
        return
    print(yellow("  Not represented in Shelfard (kept only in the contract):"))
    for field_name, paths in sorted(ignored.items()):
        shown = ", ".join(paths[:3]) + (f", … (+{len(paths) - 3})" if len(paths) > 3 else "")
        print(f"    • {field_name}: {shown}")
