"""
Shelfard — schema drift detection CLI.

Usage:
  shelfard rest snapshot <url> --name NAME [--bearer TOKEN] [--header KEY=VALUE ...]
  shelfard rest check    <url> --name NAME [--bearer TOKEN] [--header KEY=VALUE ...] [--update]
  shelfard file snapshot <path> --name NAME [--format auto|parquet|orc|avro]
  shelfard file check    <path> --name NAME [--format ...] [--update]
  shelfard file list     <dir>
  shelfard iceberg snapshot <table-path> --name NAME
  shelfard iceberg check    <table-path> --name NAME [--update]
  shelfard iceberg list     <warehouse-dir>
  shelfard odcs export   <name> [--version N] [--format json|yaml] [--out FILE]
  shelfard odcs import   <contract> [--name-prefix PREFIX]
  shelfard odcs check    <contract> (--checker NAME | --name NAME) [--object OBJ] [--update]
  shelfard show          <table>
  shelfard list          {schemas,subscriptions}
  shelfard subscribe     <table> --consumer NAME [--columns COL1,COL2,...]
  shelfard agent
  shelfard --help
"""

import argparse
import os
import re
import sys

from shelfard import (
    ColumnSchema, RestEndpointReader, PostgresReader, TableSchema,
    compare_schemas_from_dicts, get_registered_schema, register_schema,
    get_all_schemas, get_all_consumers, subscribe_consumer,
    RestCheckerConfig, PostgresCheckerConfig, FileCheckerConfig, FileReader,
    list_file_tables, IcebergCheckerConfig, IcebergReader, list_iceberg_tables,
    register_checker, get_checker, get_all_checkers, run_checker,
    set_var, get_var, list_vars, delete_var, accept_safe_update,
)
from shelfard import odcs
from shelfard.models import ChangeSeverity, ChangeType
from shelfard.registry import _default as _registry
from shelfard.security import query_template_error


# ── ANSI colours ──────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()


def _colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def red(t: str)    -> str: return _colour(t, "31")
def yellow(t: str) -> str: return _colour(t, "33")
def green(t: str)  -> str: return _colour(t, "32")
def bold(t: str)   -> str: return _colour(t, "1")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_template_vars(text: str) -> str:
    """Resolve {{var_name}} references from the registry before use."""
    return _registry.resolve_template(text)


def _extract_env_vars(*templates: str) -> list[str]:
    """Return unique $VAR_NAME names found across all template strings, in order of first appearance."""
    seen: set[str] = set()
    result: list[str] = []
    for t in templates:
        for name in re.findall(r'\$([A-Z_][A-Z0-9_]*)', t):
            if name not in seen:
                result.append(name)
                seen.add(name)
    return result


def _parse_headers(header_list: list[str]) -> dict[str, str]:
    """Parse ['Key=Value', ...] into a dict, warning on malformed entries."""
    headers: dict[str, str] = {}
    for item in header_list or []:
        if "=" not in item:
            print(f"Warning: ignoring malformed --header {item!r} (expected KEY=VALUE)")
            continue
        k, _, v = item.partition("=")
        headers[k.strip()] = v.strip()
    return headers


def _schema_from_result(result) -> TableSchema:
    """Reconstruct a TableSchema from a ToolResult's data dict."""
    s = result.data["schema"]
    return TableSchema(
        table_name=s["table_name"],
        columns=[ColumnSchema.from_dict(c) for c in s["columns"]],
        partition_keys=s.get("partition_keys", []),
        clustering_keys=s.get("clustering_keys", []),
        source=s.get("source", "unknown"),
        captured_at=s.get("captured_at"),
    )


def _print_diff(schema_name: str, diff: dict, baseline_version: str) -> None:
    if not diff["changes"]:
        print(green(f"✓ No drift detected for '{schema_name}'") +
              f"  (last snapshot: {baseline_version})")
        return

    severity = diff["overall_severity"]
    if severity == ChangeSeverity.SAFE:
        print(green(f"✓ Safe schema changes for '{schema_name}'"))
    else:
        icon = "✗" if severity == ChangeSeverity.BREAKING else "⚠"
        header_fn = red if severity == ChangeSeverity.BREAKING else yellow
        print(header_fn(f"{icon} Schema drift detected for '{schema_name}'"))
    print(f"  {diff['summary']}")
    print()

    for change in diff["changes"]:
        sev = change["severity"]
        label = f"[{ChangeSeverity(sev).value:<8}]"
        if sev == ChangeSeverity.BREAKING:
            label = red(label)
        elif sev == ChangeSeverity.WARNING:
            label = yellow(label)
        else:
            label = green(label)

        print(f"  {label} {ChangeType(change['change_type']).value:<25} '{change['column_name']}'")
        print(f"             {change['reasoning']}")
        print()


def _finish_check(schema_name: str, diff: dict, baseline: dict, update: bool) -> int:
    """Print the diff and, when every change is SAFE, the suggested baseline
    update — applying it if ``update`` is set. ``baseline`` is the data of a
    get_registered_schema() result (or a checker result's baseline fields).

    Exit codes: 0 no drift or update applied, 1 drift, 2 update failed.
    """
    _print_diff(schema_name, diff, baseline["version"])
    if not diff["changes"]:
        return 0
    suggestion = diff.get("suggested_update")
    if not suggestion:
        if update:
            print(yellow("  --update ignored: only all-SAFE changes are applied automatically. "
                         "Review the changes, then re-snapshot to accept them."))
        return 1

    current = baseline.get("version_number")
    target = f"version {current} → {current + 1}" if current else "a new version"
    print(f"  Suggested baseline update ({target}):")
    for line in suggestion.get("explanation", []):
        print(f"    • {line}")
    if not update:
        print("  Run the same command with --update to save it.")
        return 1

    result = accept_safe_update(schema_name, diff, baseline["version"])
    if not result.success:
        print(red(f"✗ Baseline not updated: {result.error}"))
        return 2
    print(green(f"✓ Baseline updated: '{schema_name}' is now version {result.data['version_count']}"))
    return 0


def _print_schema(schema_dict: dict, indent: int = 0) -> None:
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
            _print_schema({"columns": col["fields"]}, indent + 1)
        elif col.get("items"):
            _print_schema({"columns": [{**col["items"], "name": "[]"}]}, indent + 1)


# ── REST commands ─────────────────────────────────────────────────────────────

def cmd_rest_snapshot(args) -> int:
    schema_name = args.name
    headers = _parse_headers(args.header)
    url = _resolve_template_vars(args.url)

    print(f"Fetching {url} …")
    result = RestEndpointReader(
        url, schema_name,
        bearer_token=args.bearer or None,
        headers=headers or None,
    ).get_schema()

    if not result.success:
        print(red(f"✗ Failed to fetch schema: {result.error}"))
        return 2

    schema = _schema_from_result(result)
    reg = register_schema(schema_name, schema)
    if not reg.success:
        print(red(f"✗ Failed to register schema: {reg.error}"))
        return 2

    version = reg.data["version_count"]
    col_count = len(schema.columns)
    print(green(f"✓ Snapshot saved: '{schema_name}' (version {version}, {col_count} top-level columns)"))

    if getattr(args, "create_checker", False):
        headers_list = []
        for item in args.header or []:
            if "=" not in item:
                continue
            k, _, v = item.partition("=")
            headers_list.append({k.strip(): v.strip()})
        if args.bearer:
            headers_list.append({"Authorization": f"Bearer {args.bearer}"})
        header_values = [v for h in headers_list for v in h.values()]
        env = _extract_env_vars(args.url, *header_values)
        config = RestCheckerConfig(
            schema_name=schema_name,
            url=args.url,
            headers=headers_list,
            env=env,
        )
        cr = register_checker(schema_name, config)
        if cr.success:
            env_str = f"{len(env)} env var{'s' if len(env) != 1 else ''}"
            print(green(f"✓ Checker registered for '{schema_name}'") + f"  (rest · {env_str})")
        else:
            print(yellow(f"⚠ Snapshot saved but checker registration failed: {cr.error}"))

    return 0


def cmd_rest_check(args) -> int:
    schema_name = args.name
    headers = _parse_headers(args.header)
    url = _resolve_template_vars(args.url)

    print(f"Fetching {url} …")
    new_result = RestEndpointReader(
        url, schema_name,
        bearer_token=args.bearer or None,
        headers=headers or None,
    ).get_schema()

    if not new_result.success:
        print(red(f"✗ Failed to fetch schema: {new_result.error}"))
        return 2

    baseline_result = get_registered_schema(schema_name)
    if not baseline_result.success:
        print(red(f"✗ No snapshot found for '{schema_name}'."))
        print(f"  Run:  shelfard rest snapshot {url} --name {args.name}")
        return 2

    diff_result = compare_schemas_from_dicts(
        baseline_result.data["schema"],
        new_result.data["schema"],
    )
    if not diff_result.success:
        print(red(f"✗ Comparison failed: {diff_result.error}"))
        return 2

    return _finish_check(schema_name, diff_result.data["diff"], baseline_result.data,
                         getattr(args, "update", False))


# ── PostgreSQL commands ───────────────────────────────────────────────────────

def _query_template_rejected(query) -> bool:
    """Print an error and return True if a --query contains {{var}} templates."""
    err = query_template_error(query)
    if err:
        print(red(f"✗ {err}"))
    return bool(err)


def cmd_postgres_snapshot(args) -> int:
    schema_name = args.name
    if _query_template_rejected(args.query):
        return 2
    dsn = _resolve_template_vars(args.dsn)
    query = args.query or None
    target = args.table or "(custom query)"
    print(f"Reading PostgreSQL '{target}' …")

    result = PostgresReader(
        dsn,
        schema_name,
        table=args.table or None,
        query=query,
        db_schema=args.db_schema,
    ).get_schema()

    if not result.success:
        print(red(f"✗ Failed to read schema: {result.error}"))
        if result.next_action_hint:
            print(f"  {result.next_action_hint}")
        return 2

    schema = _schema_from_result(result)
    reg = register_schema(schema_name, schema)
    if not reg.success:
        print(red(f"✗ Failed to register schema: {reg.error}"))
        return 2

    version = reg.data["version_count"]
    col_count = len(schema.columns)
    print(green(f"✓ Snapshot saved: '{schema_name}' (version {version}, {col_count} top-level columns)"))

    if getattr(args, "create_checker", False):
        templates = [args.dsn] + ([args.query] if args.query else [])
        env = _extract_env_vars(*templates)
        config = PostgresCheckerConfig(
            schema_name=schema_name,
            dsn=args.dsn,
            env=env,
            table=args.table or None,
            query=args.query or None,
            db_schema=args.db_schema,
        )
        cr = register_checker(schema_name, config)
        if cr.success:
            env_str = f"{len(env)} env var{'s' if len(env) != 1 else ''}"
            print(green(f"✓ Checker registered for '{schema_name}'") + f"  (postgres · {env_str})")
        else:
            print(yellow(f"⚠ Snapshot saved but checker registration failed: {cr.error}"))

    return 0


def cmd_postgres_check(args) -> int:
    schema_name = args.name
    if _query_template_rejected(args.query):
        return 2
    dsn = _resolve_template_vars(args.dsn)
    query = args.query or None
    target = args.table or "(custom query)"
    print(f"Reading PostgreSQL '{target}' …")

    new_result = PostgresReader(
        dsn,
        schema_name,
        table=args.table or None,
        query=query,
        db_schema=args.db_schema,
    ).get_schema()

    if not new_result.success:
        print(red(f"✗ Failed to read schema: {new_result.error}"))
        if new_result.next_action_hint:
            print(f"  {new_result.next_action_hint}")
        return 2

    baseline_result = get_registered_schema(schema_name)
    if not baseline_result.success:
        print(red(f"✗ No snapshot found for '{schema_name}'."))
        table_hint = f"--table {args.table}" if args.table else "--query '...'"
        print(f"  Run:  shelfard postgres snapshot --dsn ... {table_hint} --name {schema_name}")
        return 2

    diff_result = compare_schemas_from_dicts(
        baseline_result.data["schema"],
        new_result.data["schema"],
    )
    if not diff_result.success:
        print(red(f"✗ Comparison failed: {diff_result.error}"))
        return 2

    return _finish_check(schema_name, diff_result.data["diff"], baseline_result.data,
                         getattr(args, "update", False))


# ── File-format commands (Parquet / ORC / Avro) ───────────────────────────────

def _resolve_file_path(raw: str) -> str:
    """Resolve {{vars}} from the registry, then $VARS from the environment."""
    path = _resolve_template_vars(raw)
    for name in _extract_env_vars(path):
        if name in os.environ:
            path = path.replace(f"${name}", os.environ[name])
    return path


def _read_file_schema(args):
    path = _resolve_file_path(args.path)
    print(f"Reading {path} …")
    if args.source == "iceberg":
        result = IcebergReader(path, args.name).get_schema()
    else:
        result = FileReader(path, args.name, args.format).get_schema()
    if not result.success:
        print(red(f"✗ Failed to read schema: {result.error}"))
        if result.next_action_hint:
            print(f"  {result.next_action_hint}")
    return result


def cmd_file_snapshot(args) -> int:
    schema_name = args.name
    result = _read_file_schema(args)
    if not result.success:
        return 2

    schema = _schema_from_result(result)
    reg = register_schema(schema_name, schema)
    if not reg.success:
        print(red(f"✗ Failed to register schema: {reg.error}"))
        return 2

    version = reg.data["version_count"]
    print(green(f"✓ Snapshot saved: '{schema_name}' (version {version}, {schema.source}, "
                f"{len(schema.columns)} top-level columns)"))
    if result.data.get("metadata_file"):
        print(f"  metadata: {result.data['metadata_file']}")
    if schema.partition_keys:
        print(f"  partitioned by: {', '.join(schema.partition_keys)}")
    if schema.clustering_keys:
        print(f"  sorted by: {', '.join(schema.clustering_keys)}")

    if getattr(args, "create_checker", False):
        env = _extract_env_vars(args.path)
        if args.source == "iceberg":
            config = IcebergCheckerConfig(schema_name=schema_name, path=args.path, env=env)
        else:
            config = FileCheckerConfig(
                schema_name=schema_name, path=args.path, env=env, file_format=args.format,
            )
        cr = register_checker(schema_name, config)
        if cr.success:
            print(green(f"✓ Checker registered for '{schema_name}'") + f"  ({config.checker_type})")
        else:
            print(yellow(f"⚠ Snapshot saved but checker registration failed: {cr.error}"))
    return 0


def cmd_file_check(args) -> int:
    schema_name = args.name
    new_result = _read_file_schema(args)
    if not new_result.success:
        return 2

    baseline_result = get_registered_schema(schema_name)
    if not baseline_result.success:
        print(red(f"✗ No snapshot found for '{schema_name}'."))
        print(f"  Run:  shelfard {args.source} snapshot {args.path} --name {schema_name}")
        return 2

    diff_result = compare_schemas_from_dicts(
        baseline_result.data["schema"],
        new_result.data["schema"],
    )
    if not diff_result.success:
        print(red(f"✗ Comparison failed: {diff_result.error}"))
        return 2

    return _finish_check(schema_name, diff_result.data["diff"], baseline_result.data,
                         getattr(args, "update", False))


def cmd_file_list(args) -> int:
    result = list_file_tables(_resolve_file_path(args.path))
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    tables = result.data["tables"]
    if not tables:
        print("No Parquet, ORC or Avro sources found.")
        return 0
    print(f"\nSources ({len(tables)}):\n")
    for t in tables:
        print(f"  {t['name']:<30} {t['file_format']:<8} {t['path']}")
    print()
    return 0


def cmd_iceberg_list(args) -> int:
    result = list_iceberg_tables(_resolve_file_path(args.path))
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    tables = result.data["tables"]
    if not tables:
        print("No Iceberg tables found.")
        return 0
    print(f"\nIceberg tables ({len(tables)}):\n")
    for t in tables:
        print(f"  {t['name']:<30} {t['path']}")
    print()
    return 0


# ── show command ──────────────────────────────────────────────────────────────

def cmd_show(args) -> int:
    result = get_registered_schema(args.table)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    schema = result.data["schema"]
    version = result.data["version"]
    col_count = len(schema["columns"])
    source = schema.get("source", "unknown")

    print()
    print(bold(f"Schema: {args.table}") +
          f"  (source: {source} · version: {version} · {col_count} columns)")

    checker_result = get_checker(args.table)
    if checker_result.success:
        c = checker_result.data["checker"]
        print(f"Checker: {c['checker_type']}  (registered {c['registered_at']})")

    print()
    _print_schema(schema)
    print()
    return 0


# ── list command ──────────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    if args.what == "schemas":
        result = get_all_schemas()
        if not result.success:
            print(red(f"✗ {result.error}"))
            return 2

        schemas = result.data["schemas"]
        if not schemas:
            print("No schemas registered yet.")
            return 0

        print(f"\nSchemas ({len(schemas)}):\n")
        for s in schemas:
            ver_word = "version" if s["version_count"] == 1 else "versions"
            print(
                f"  {s['name']:<20} {s['column_count']} cols   "
                f"{s['version_count']} {ver_word:<10} "
                f"{s.get('source') or 'unknown':<14} {s['latest_version']}"
            )
        print()

    else:  # subscriptions
        result = get_all_consumers()
        if not result.success:
            print(red(f"✗ {result.error}"))
            return 2

        consumers = result.data["consumers"]
        if not consumers:
            print("No subscriptions registered yet.")
            return 0

        print(f"\nConsumer subscriptions ({len(consumers)}):\n")
        for c in consumers:
            cols = c.get("subscribed_columns")
            cols_str = ", ".join(cols) if cols else "all columns"
            print(
                f"  {c['consumer']:<20} {c['source_table']:<14} "
                f"{cols_str:<30} {c['subscribed_at']}"
            )
        print()

    return 0


# ── subscribe command ─────────────────────────────────────────────────────────

def cmd_subscribe(args) -> int:
    columns = None
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]

    result = subscribe_consumer(args.consumer, args.table, columns)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    col_count = result.data["column_count"]
    if columns:
        cols_display = ", ".join(columns)
        print(green(f"✓ Subscribed '{args.consumer}' to '{args.table}' — {col_count} columns: {cols_display}"))
    else:
        print(green(f"✓ Subscribed '{args.consumer}' to '{args.table}' — all {col_count} columns captured."))

    if result.next_action_hint:
        print(f"  Note: {result.next_action_hint}")

    return 0


# ── Checker commands ──────────────────────────────────────────────────────────

def cmd_checker_register(args) -> int:
    checker_type = args.checker_type or "rest"

    if checker_type == "rest":
        if not args.url:
            print(red("✗ --url is required for rest checkers"))
            return 2
        headers = []
        for item in args.header or []:
            if "=" not in item:
                print(f"Warning: ignoring malformed --header {item!r} (expected KEY=VALUE)")
                continue
            k, _, v = item.partition("=")
            headers.append({k.strip(): v.strip()})
        config = RestCheckerConfig(
            schema_name=args.schema_name,
            url=args.url,
            headers=headers,
            env=args.env or [],
        )

    elif checker_type == "postgres":
        if not args.dsn:
            print(red("✗ --dsn is required for postgres checkers"))
            return 2
        if not args.table and not args.query:
            print(red("✗ Either --table or --query is required for postgres checkers"))
            return 2
        if args.table and args.query:
            print(red("✗ Provide either --table or --query, not both"))
            return 2
        if _query_template_rejected(args.query):
            return 2
        config = PostgresCheckerConfig(
            schema_name=args.schema_name,
            dsn=args.dsn,
            env=args.env or [],
            table=args.table or None,
            query=args.query or None,
            db_schema=args.db_schema or "public",
            sample_size=args.sample_size or 100,
        )

    elif checker_type == "file":
        if not args.path:
            print(red("✗ --path is required for file checkers"))
            return 2
        config = FileCheckerConfig(
            schema_name=args.schema_name,
            path=args.path,
            env=args.env or _extract_env_vars(args.path),
            file_format=args.file_format or "auto",
        )

    elif checker_type == "iceberg":
        if not args.path:
            print(red("✗ --path is required for iceberg checkers"))
            return 2
        config = IcebergCheckerConfig(
            schema_name=args.schema_name,
            path=args.path,
            env=args.env or _extract_env_vars(args.path),
        )

    else:
        print(red(f"✗ Unknown checker type: {checker_type!r}"))
        return 2

    result = register_checker(args.schema_name, config)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    env_count = len(config.env)
    env_str = f"{env_count} env var{'s' if env_count != 1 else ''}"
    print(green(f"✓ Checker registered for '{args.schema_name}'") +
          f"  ({checker_type} · {env_str})")
    return 0


def cmd_checker_run(args) -> int:
    checker_result = get_checker(args.schema_name)
    if not checker_result.success:
        print(red(f"✗ {checker_result.error}"))
        return 2

    c = checker_result.data["checker"]
    if c.get("checker_type") == "postgres":
        target = c.get("table") or "(custom query)"
        print(f"Checking PostgreSQL '{target}' …")
    elif c.get("checker_type") in ("file", "iceberg"):
        print(f"Reading {c.get('path', '')} …")
    else:
        print(f"Fetching {c.get('url', '')} …")

    result = run_checker(args.schema_name)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    baseline = {"version": result.data["baseline_version"],
                "version_number": result.data.get("baseline_version_number")}
    return _finish_check(args.schema_name, result.data["diff"], baseline, args.update)


def cmd_checker_show(args) -> int:
    result = get_checker(args.schema_name)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    c = result.data["checker"]
    checker_type = c.get("checker_type", "rest")
    print(f"\nChecker: {args.schema_name}")
    print(f"  type:  {checker_type}")

    if checker_type == "postgres":
        print(f"  dsn:   {c.get('dsn', '')}")
        if c.get("table"):
            print(f"  table: {c['table']}")
        if c.get("query"):
            print(f"  query: {c['query']}")
        if c.get("db_schema") and c["db_schema"] != "public":
            print(f"  schema: {c['db_schema']}")
        if c.get("env"):
            print(f"  env:   {', '.join(c['env'])}")
    elif checker_type in ("file", "iceberg"):
        print(f"  path:   {c.get('path', '')}")
        if checker_type == "file":
            print(f"  format: {c.get('file_format', 'auto')}")
        if c.get("env"):
            print(f"  env:    {', '.join(c['env'])}")
    else:
        print(f"  url:   {c.get('url', '')}")
        if c.get("env"):
            print(f"  env:   {', '.join(c['env'])}")
        if c.get("headers"):
            print("  headers:")
            for entry in c["headers"]:
                for k, v in entry.items():
                    print(f"    {k}: {v}")

    print()
    return 0


def cmd_checker_list(_args) -> int:
    result = get_all_checkers()
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2

    checkers = result.data["checkers"]
    if not checkers:
        print("No checkers registered yet.")
        return 0

    print(f"\nCheckers ({len(checkers)}):\n")
    for c in checkers:
        env_str = ", ".join(c["env"]) if c.get("env") else "—"
        target = c.get("url") or c.get("dsn") or c.get("path") or "—"
        print(
            f"  {c['schema_name']:<20} {c['checker_type']:<8} "
            f"{target:<45} {env_str:<20} {c.get('registered_at') or ''}"
        )
    print()
    return 0


# ── Var commands ──────────────────────────────────────────────────────────────

def cmd_var_set(args) -> int:
    result = set_var(args.name, args.value)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    print(green(f"✓ Set {{{{ {args.name} }}}} = {args.value!r}"))
    return 0


def cmd_var_get(args) -> int:
    result = get_var(args.name)
    if not result.success:
        print(red(f"✗ {result.error}"))
        if result.next_action_hint:
            print(f"  {result.next_action_hint}")
        return 2
    print(f"{args.name} = {result.data['value']!r}")
    return 0


def cmd_var_list(_args) -> int:
    result = list_vars()
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    vars_dict = result.data["vars"]
    if not vars_dict:
        print("No template variables set.")
        return 0
    print(f"\nTemplate variables ({len(vars_dict)}):\n")
    for name, value in sorted(vars_dict.items()):
        print(f"  {{{{ {name:<20} }}}}  =  {value!r}")
    print()
    return 0


def cmd_var_unset(args) -> int:
    result = delete_var(args.name)
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    print(green(f"✓ Deleted template variable '{args.name}'"))
    return 0


# ── Agent command ─────────────────────────────────────────────────────────────

# ── ODCS commands ─────────────────────────────────────────────────────────────

def cmd_odcs_export(args) -> int:
    result = odcs.export_odcs(args.name, args.version)
    if not result.success:
        print(red(f"✗ {result.error}"), file=sys.stderr)
        return 2
    fmt = args.format or ("yaml" if args.out and odcs.is_yaml_path(args.out) else "json")
    try:
        text = odcs.dumps_contract(result.data["contract"], fmt)
    except ImportError as e:
        print(red(f"✗ {e}"), file=sys.stderr)
        return 2
    if not args.out:
        sys.stdout.write(text)
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(green(f"✓ Wrote {args.out}") +
          f"  (contract version {result.data['contract_version']}, "
          f"registry version {result.data['version_number']})")
    return 0


def _load_contract_or_report(path):
    try:
        return odcs.load_contract(path)
    except (OSError, ValueError, ImportError) as e:
        print(red(f"✗ Cannot read contract {path}: {e}"))
    except Exception as e:     # yaml.YAMLError and friends
        print(red(f"✗ Cannot parse contract {path}: {e}"))
    return None


def _print_ignored(ignored: dict) -> None:
    if not ignored:
        return
    print(yellow("  Not represented in Shelfard (kept only in the contract):"))
    for field, paths in sorted(ignored.items()):
        shown = ", ".join(paths[:3]) + (f", … (+{len(paths) - 3})" if len(paths) > 3 else "")
        print(f"    • {field}: {shown}")


def cmd_odcs_import(args) -> int:
    doc = _load_contract_or_report(args.contract)
    if doc is None:
        return 2
    result = odcs.import_odcs(doc, name_prefix=args.name_prefix or "")
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    for entry in result.data["registered"]:
        print(green(f"✓ Registered '{entry['name']}'") + f"  (version {entry['version_count']})")
    _print_ignored(result.data["ignored"])
    return 0


def cmd_odcs_check(args) -> int:
    from shelfard.registry import fetch_checker_schema
    from shelfard.schema_comparison import compare_schemas

    doc = _load_contract_or_report(args.contract)
    if doc is None:
        return 2
    if args.checker:
        label = args.checker
        live = fetch_checker_schema(args.checker)
    else:
        label = args.name
        live = get_registered_schema(args.name)
    if not live.success:
        print(red(f"✗ {live.error}"))
        return 2

    obj = odcs.find_object(doc, args.object, fallback=label)
    if obj is None:
        names = [o.get("name") for o in doc.get("schema") or [] if isinstance(o, dict)]
        print(red(f"✗ No schema object to check in {args.contract}") +
              f" (objects: {', '.join(map(str, names)) or 'none'}); pass --object NAME.")
        return 2
    try:
        contract_schema = odcs.contract_table(doc, obj)
    except ValueError as e:
        print(red(f"✗ {e}"))
        return 2

    result = compare_schemas(contract_schema, TableSchema.from_dict(live.data["schema"]))
    if not result.success:
        print(red(f"✗ {result.error}"))
        return 2
    diff = result.data["diff"]
    version = doc.get("version", "?")
    _print_diff(f"{label}' vs contract '{obj['name']}", diff, f"contract version {version}")
    if not diff["changes"]:
        return 0

    suggestion = diff.get("suggested_update")
    if not suggestion:
        if args.update:
            print(yellow("  --update ignored: only all-SAFE changes are written to the contract."))
        return 1
    new_version = odcs.bump_version(version, "patch") or version
    print(f"  Suggested contract update (version {version} → {new_version}):")
    for line in suggestion.get("explanation", []):
        print(f"    • {line}")
    if not args.update:
        print("  Run the same command with --update to write it to the contract.")
        return 1
    updated = odcs.update_contract(doc, obj["name"], TableSchema.from_dict(suggestion["schema"]))
    try:
        odcs.save_contract(updated, args.contract)
    except (OSError, ImportError) as e:
        print(red(f"✗ Contract not updated: {e}"))
        return 2
    print(green(f"✓ Contract updated: {args.contract} is now version {updated.get('version')}"))
    return 0


def cmd_agent(args) -> int:
    from shelfard.agent import run_agent
    run_agent(model=args.model, allow_writes=args.allow_writes)
    return 0


# ── MCP server command ────────────────────────────────────────────────────────

def cmd_mcp(args) -> int:
    from shelfard.mcp_server import run
    run(allow_writes=True if args.allow_writes else None)
    return 0


# ── Parser ────────────────────────────────────────────────────────────────────

def _add_rest_subcommands(top) -> None:
    rest_p = top.add_parser("rest", help="REST API endpoint reader")
    rest_cmds = rest_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Shared options for all REST commands
    rest_base = argparse.ArgumentParser(add_help=False)
    rest_base.add_argument("url", help="Endpoint URL to fetch")
    rest_base.add_argument(
        "--name", metavar="NAME", required=True,
        help="Schema name used to store and retrieve from the registry",
    )
    rest_base.add_argument(
        "--bearer", metavar="TOKEN",
        help="Send Authorization: Bearer <TOKEN> with the request",
    )
    rest_base.add_argument(
        "--header", metavar="KEY=VALUE", action="append", default=[],
        help="Extra request header; can be repeated: --header X-Api-Key=abc",
    )

    snap = rest_cmds.add_parser(
        "snapshot", parents=[rest_base],
        help="Fetch the endpoint and save its schema to the registry",
    )
    snap.add_argument(
        "--create-checker", action="store_true", default=False,
        help="Also register a checker config using the same URL and headers after snapshotting",
    )
    snap.set_defaults(func=cmd_rest_snapshot)

    chk = rest_cmds.add_parser(
        "check", parents=[rest_base],
        help="Fetch the endpoint and compare its schema against the saved snapshot",
    )
    chk.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    chk.set_defaults(func=cmd_rest_check)


def _add_postgres_subcommands(top) -> None:
    pg_p = top.add_parser("postgres", help="PostgreSQL schema reader")
    pg_cmds = pg_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Shared base options for all postgres commands
    pg_base = argparse.ArgumentParser(add_help=False)
    pg_base.add_argument(
        "--dsn", metavar="DSN", required=True,
        help="PostgreSQL connection string (may contain $VAR placeholders)",
    )
    pg_base.add_argument(
        "--name", metavar="NAME", required=True,
        help="Schema name used to store and retrieve from the registry",
    )
    pg_base.add_argument(
        "--table", metavar="TABLE", default=None,
        help="Table or view name to introspect (table mode)",
    )
    pg_base.add_argument(
        "--query", metavar="SQL", default=None,
        help="SQL query whose result shape defines the schema (query mode)",
    )
    pg_base.add_argument(
        "--db-schema", metavar="SCHEMA", default="public",
        help="PostgreSQL schema namespace to search in (default: public)",
    )

    snap = pg_cmds.add_parser(
        "snapshot", parents=[pg_base],
        help="Read a PostgreSQL table or query and save its schema to the registry",
    )
    snap.add_argument(
        "--create-checker", action="store_true", default=False,
        help="Also register a checker config using the same DSN and table/query after snapshotting",
    )
    snap.set_defaults(func=cmd_postgres_snapshot)

    chk = pg_cmds.add_parser(
        "check", parents=[pg_base],
        help="Read a PostgreSQL table or query and compare against the saved snapshot",
    )
    chk.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    chk.set_defaults(func=cmd_postgres_check)


_FILE_FORMAT_CHOICES = ["auto", "parquet", "orc", "avro"]


def _add_file_subcommands(top) -> None:
    file_p = top.add_parser("file", help="Parquet / ORC / Avro files and datasets")
    file_cmds = file_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    file_base = argparse.ArgumentParser(add_help=False)
    file_base.add_argument(
        "path",
        help="File or directory of files (e.g. a partitioned dataset); "
             "may contain {{var}} and $VAR placeholders",
    )
    file_base.add_argument(
        "--name", metavar="NAME", required=True,
        help="Schema name used to store and retrieve from the registry",
    )
    file_base.add_argument(
        "--format", default="auto", choices=_FILE_FORMAT_CHOICES,
        help="File format (default: auto-detect from extension or magic bytes)",
    )
    file_base.set_defaults(source="file")

    snap = file_cmds.add_parser(
        "snapshot", parents=[file_base],
        help="Read a file's / table's schema and save it to the registry",
    )
    snap.add_argument(
        "--create-checker", action="store_true", default=False,
        help="Also register a checker config for the same path after snapshotting",
    )
    snap.set_defaults(func=cmd_file_snapshot)

    chk = file_cmds.add_parser(
        "check", parents=[file_base],
        help="Read a file's / table's schema and compare it against the saved snapshot",
    )
    chk.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    chk.set_defaults(func=cmd_file_check)

    lst = file_cmds.add_parser("list", help="List Parquet / ORC / Avro sources in a directory")
    lst.add_argument("path", help="Directory to scan (one level deep)")
    lst.set_defaults(func=cmd_file_list)


def _add_iceberg_subcommands(top) -> None:
    ice_p = top.add_parser("iceberg", help="Apache Iceberg tables")
    ice_cmds = ice_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    ice_base = argparse.ArgumentParser(add_help=False)
    ice_base.add_argument(
        "path",
        help="Table directory, its metadata/ directory, or a specific *.metadata.json "
             "(snapshots that version); may contain {{var}} and $VAR placeholders",
    )
    ice_base.add_argument(
        "--name", metavar="NAME", required=True,
        help="Schema name used to store and retrieve from the registry",
    )
    ice_base.set_defaults(source="iceberg", format=None)

    snap = ice_cmds.add_parser(
        "snapshot", parents=[ice_base],
        help="Read the table's current schema and save it to the registry",
    )
    snap.add_argument(
        "--create-checker", action="store_true", default=False,
        help="Also register a checker config for the same table path after snapshotting",
    )
    snap.set_defaults(func=cmd_file_snapshot)

    chk = ice_cmds.add_parser(
        "check", parents=[ice_base],
        help="Read the table's current schema and compare it against the saved snapshot",
    )
    chk.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    chk.set_defaults(func=cmd_file_check)

    lst = ice_cmds.add_parser("list", help="Find Iceberg tables under a warehouse directory")
    lst.add_argument("path", help="Warehouse directory (Hadoop-catalog layout)")
    lst.set_defaults(func=cmd_iceberg_list)


def _add_checker_subcommands(top) -> None:
    checker_p = top.add_parser("checker", help="Manage stored drift-check configurations")
    checker_cmds = checker_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    reg = checker_cmds.add_parser(
        "register",
        help="Register a checker config for a schema",
    )
    reg.add_argument("schema_name", help="Schema name to register a checker for")
    reg.add_argument(
        "--type", dest="checker_type", default="rest", choices=["rest", "postgres", "file", "iceberg"],
        help="Checker type (default: rest)",
    )
    # REST options
    reg.add_argument("--url", default=None,
                     help="[rest] Endpoint URL (may contain $VAR placeholders)")
    reg.add_argument(
        "--header", metavar="KEY=VALUE", action="append", default=[],
        help="[rest] Header to send; values may contain $VAR placeholders (repeatable)",
    )
    # PostgreSQL options
    reg.add_argument("--dsn", default=None,
                     help="[postgres] Connection string (may contain $VAR placeholders)")
    reg.add_argument("--table", default=None,
                     help="[postgres] Table or view name (table mode)")
    reg.add_argument("--query", default=None,
                     help="[postgres] SQL query whose result defines the schema (query mode)")
    reg.add_argument("--db-schema", default="public",
                     help="[postgres] PostgreSQL schema namespace (default: public)")
    reg.add_argument("--sample-size", type=int, default=100,
                     help="[postgres] Rows to sample for nullability inference (default: 100)")
    # File options
    reg.add_argument("--path", default=None,
                     help="[file/iceberg] File, dataset directory or Iceberg table path "
                          "(may contain {{var}} / $VAR)")
    reg.add_argument("--format", dest="file_format", default="auto", choices=_FILE_FORMAT_CHOICES,
                     help="[file] File format (default: auto-detect)")
    # Common
    reg.add_argument(
        "--env", metavar="VAR", action="append", default=[],
        help="Env var name required at run time (repeatable)",
    )
    reg.set_defaults(func=cmd_checker_register)

    run = checker_cmds.add_parser("run", help="Run the registered checker for a schema")
    run.add_argument("schema_name", help="Schema name whose checker to run")
    run.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    run.set_defaults(func=cmd_checker_run)

    show = checker_cmds.add_parser("show", help="Display the registered checker config")
    show.add_argument("schema_name", help="Schema name whose checker to show")
    show.set_defaults(func=cmd_checker_show)

    lst = checker_cmds.add_parser("list", help="List all registered checkers")
    lst.set_defaults(func=cmd_checker_list)


def _add_var_subcommands(top) -> None:
    var_p = top.add_parser("var", help="Manage template variables ({{var_name}} syntax)")
    var_cmds = var_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    set_p = var_cmds.add_parser("set", help="Set a template variable")
    set_p.add_argument("name", help="Variable name (alphanumeric + underscore)")
    set_p.add_argument("value", help="Variable value (stored as plain text)")
    set_p.set_defaults(func=cmd_var_set)

    get_p = var_cmds.add_parser("get", help="Get a template variable's value")
    get_p.add_argument("name", help="Variable name")
    get_p.set_defaults(func=cmd_var_get)

    lst = var_cmds.add_parser("list", help="List all template variables")
    lst.set_defaults(func=cmd_var_list)

    unset_p = var_cmds.add_parser("unset", help="Delete a template variable")
    unset_p.add_argument("name", help="Variable name to delete")
    unset_p.set_defaults(func=cmd_var_unset)


def _add_odcs_subcommands(top) -> None:
    odcs_p = top.add_parser("odcs", help="Open Data Contract Standard (ODCS v3.1.0) contracts")
    odcs_cmds = odcs_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    exp = odcs_cmds.add_parser("export", help="Export a registered schema as an ODCS contract")
    exp.add_argument("name", help="Registered schema name")
    exp.add_argument("--version", type=int, metavar="N",
                     help="Registry version to export (1-based; default: latest)")
    exp.add_argument("--format", choices=["json", "yaml"],
                     help="Output format (default: from --out extension, else json)")
    exp.add_argument("--out", metavar="FILE", help="Write to FILE instead of stdout")
    exp.set_defaults(func=cmd_odcs_export)

    imp = odcs_cmds.add_parser("import", help="Register the schema objects of an ODCS contract")
    imp.add_argument("contract", help="Contract file (.json, .yaml, .yml)")
    imp.add_argument("--name-prefix", metavar="PREFIX",
                     help="Prefix for registered schema names")
    imp.set_defaults(func=cmd_odcs_import)

    chk = odcs_cmds.add_parser("check", help="Compare a live or registered schema with a contract")
    chk.add_argument("contract", help="Contract file (.json, .yaml, .yml)")
    src = chk.add_mutually_exclusive_group(required=True)
    src.add_argument("--checker", metavar="NAME", help="Read the live schema with this stored checker")
    src.add_argument("--name", metavar="NAME", help="Use the latest registered version of this schema")
    chk.add_argument("--object", metavar="OBJ",
                     help="Schema object in the contract (default: the only one, or the one named like the source)")
    chk.add_argument("--update", action="store_true",
                     help="When every change is SAFE, write them into the contract (bumps the patch version)")
    chk.set_defaults(func=cmd_odcs_check)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shelfard",
        description=(
            "Schema drift detection CLI.\n\n"
            "Capture the shape of a data source once, then re-check it any time\n"
            "to detect unexpected schema changes before they break pipelines."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    top = parser.add_subparsers(
        dest="command", required=True,
        metavar="COMMAND",
        help="Command to run",
    )

    _add_rest_subcommands(top)
    _add_postgres_subcommands(top)
    _add_file_subcommands(top)
    _add_iceberg_subcommands(top)
    _add_checker_subcommands(top)
    _add_var_subcommands(top)
    _add_odcs_subcommands(top)

    agent_p = top.add_parser(
        "agent",
        help="Start an interactive schema assistant (Claude, OpenAI, or Ollama)",
    )
    agent_p.add_argument(
        "--model", metavar="MODEL", default=None,
        help=(
            "Model to use: provider shorthand ('anthropic', 'openai', 'ollama'), a specific "
            "model ID ('claude-sonnet-4-6', 'gpt-4o', ...), or 'ollama:<model>' for a local "
            "Ollama model ('ollama:qwen2.5:7b'). If omitted, auto-detected from "
            "ANTHROPIC_API_KEY, OPENAI_API_KEY, or OLLAMA_MODEL."
        ),
    )
    agent_p.add_argument(
        "--allow-writes", action="store_true",
        help=(
            "Let the agent register checkers and modify template variables "
            "(off by default; write tools are guarded but still risky with untrusted data)."
        ),
    )
    agent_p.set_defaults(func=cmd_agent)

    mcp_p = top.add_parser(
        "mcp",
        help="Start the Shelfard MCP server (stdio transport)",
    )
    mcp_p.add_argument(
        "--allow-writes", action="store_true",
        help=(
            "Expose register_checker / set_template_var / delete_template_var "
            "(or set SHELFARD_MCP_ALLOW_WRITES=1). Off by default."
        ),
    )
    mcp_p.set_defaults(func=cmd_mcp)

    show_p = top.add_parser("show", help="Display a registered schema")
    show_p.add_argument("table", help="Schema name to display")
    show_p.set_defaults(func=cmd_show)

    list_p = top.add_parser("list", help="List registered schemas or consumer subscriptions")
    list_p.add_argument(
        "what",
        choices=["schemas", "subscriptions"],
        help="What to list",
    )
    list_p.set_defaults(func=cmd_list)

    sub_p = top.add_parser("subscribe", help="Subscribe a consumer to a registered schema")
    sub_p.add_argument("table", help="Source schema name")
    sub_p.add_argument(
        "--consumer", metavar="NAME", required=True,
        help="Consumer name",
    )
    sub_p.add_argument(
        "--columns", metavar="COL1,COL2,...",
        help="Comma-separated columns to subscribe to (default: all columns)",
    )
    sub_p.set_defaults(func=cmd_subscribe)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
