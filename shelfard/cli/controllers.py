"""
CLI controllers: argparse ``args`` → handler payload → dispatch → presenter.

No execution logic lives here — only translating CLI arguments into payloads,
choosing which payloads to dispatch (e.g. ``check --update`` = ``CheckDrift`` then
``AcceptUpdate``), CLI-specific hints, and exit codes:
0 = ok / no drift / update applied, 1 = drift, 2 = error.
"""

from __future__ import annotations

import sys

from .. import odcs
from ..handlers import (
    AcceptUpdate, CheckContract, CheckDrift, ErrorCode, ExportContract, GetProbe, GetSchema,
    GetVar, ImportContract, ListFileTables, ListIcebergTables, ListProbes, ListSchemas,
    ListSubscriptions, ListVars, RefreshConsumer, RegisterProbe, SetVar, SnapshotSchema,
    Subscribe, Unsubscribe, UnsetVar, declared_env, dispatch, update_status,
)
from ..models import (
    ColumnSchema, FileProbeConfig, IcebergProbeConfig, PostgresProbeConfig, RestProbeConfig,
    TableSchema,
)
from . import presenters as show
from .presenters import green, red, yellow


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _header_entries(header_list, warn: bool) -> list[dict[str, str]]:
    """['Key=Value', ...] → [{Key: Value}, ...], skipping (and warning on) malformed entries."""
    entries = []
    for item in header_list or []:
        if "=" not in item:
            if warn:
                print(f"Warning: ignoring malformed --header {item!r} (expected KEY=VALUE)")
            continue
        k, _, v = item.partition("=")
        entries.append({k.strip(): v.strip()})
    return entries


def _rest_source(args) -> RestProbeConfig:
    headers = _header_entries(args.header, warn=True)
    if args.bearer:
        headers.append({"Authorization": f"Bearer {args.bearer}"})
    return RestProbeConfig(schema_name=args.name, url=args.url, headers=headers, env=[])


def _postgres_source(args) -> PostgresProbeConfig:
    return PostgresProbeConfig(schema_name=args.name, dsn=args.dsn, env=[],
                               table=args.table or None, query=args.query or None,
                               db_schema=args.db_schema)


def _file_source(args):
    if args.source == "iceberg":
        return IcebergProbeConfig(schema_name=args.name, path=args.path, env=[])
    return FileProbeConfig(schema_name=args.name, path=args.path, env=[],
                           file_format=args.format or "auto")


def _flags(args) -> tuple[bool, bool, bool]:
    return (bool(getattr(args, "update", False)), bool(getattr(args, "allow_unaffected", False)),
            bool(getattr(args, "details", False)))


# ── Snapshots and checks ──────────────────────────────────────────────────────

def _snapshot(source, args) -> int:
    resp = dispatch(SnapshotSchema(source, create_probe=bool(getattr(args, "create_probe", False))))
    if resp.ok:
        show.progress(source.probe_type, resp.result.target)
        show.snapshot(resp.result, source.probe_type)
        return 0
    show.progress(source.probe_type, resp.error.details.get("target"))
    return show.error(resp)


def _check(name: str, source, args, snapshot_hint) -> int:
    """``source=None`` runs the registered probe. ``snapshot_hint(target)`` is the CLI
    command to suggest when there is no baseline yet."""
    resp = dispatch(CheckDrift(name, source))
    if not resp.ok:
        show.progress(resp.error.details.get("source_kind"), resp.error.details.get("target"))
        if resp.error.code == ErrorCode.NOT_FOUND and source is not None:
            show.fail(resp.error.message)
            print(f"  Run:  {snapshot_hint(resp.error.details.get('target'))}")
            return 2
        return show.error(resp)
    report = resp.result
    show.progress(report.source_kind, report.target)
    return _finish_check(name, report, *_flags(args))


def _finish_check(name: str, report, update: bool, allow_unaffected: bool, details: bool) -> int:
    """Print the diff and the suggested baseline update — applying it (AcceptUpdate)
    if ``update`` is set and the update is allowed."""
    diff = report.diff
    show.diff(name, diff, report.baseline_version, details)
    if not diff.changes:
        return 0
    status = update_status(diff, allow_unaffected)
    if status == "no_update":
        if update:
            show.update_ignored()
        return 1
    hint = show.suggested_update(diff, report.baseline_version_number)
    if not update:
        print(f"  {hint}")
        return 1
    if status == "needs_allow_unaffected":
        show.update_needs_allow_unaffected(hint)
        return 1
    resp = dispatch(AcceptUpdate(name, diff, report.baseline_version, allow_unaffected))
    return show.update_applied(name, resp, details)


def cmd_rest_snapshot(args) -> int:
    return _snapshot(_rest_source(args), args)


def cmd_rest_check(args) -> int:
    return _check(args.name, _rest_source(args), args,
                  lambda target: f"shelfard rest snapshot {target} --name {args.name}")


def cmd_postgres_snapshot(args) -> int:
    return _snapshot(_postgres_source(args), args)


def cmd_postgres_check(args) -> int:
    table_hint = f"--table {args.table}" if args.table else "--query '...'"
    return _check(args.name, _postgres_source(args), args,
                  lambda _: f"shelfard postgres snapshot --dsn ... {table_hint} --name {args.name}")


def cmd_file_snapshot(args) -> int:
    return _snapshot(_file_source(args), args)


def cmd_file_check(args) -> int:
    return _check(args.name, _file_source(args), args,
                  lambda _: f"shelfard {args.source} snapshot {args.path} --name {args.name}")


def cmd_probe_run(args) -> int:
    return _check(args.schema_name, None, args, None)


# ── Discovery ─────────────────────────────────────────────────────────────────

def cmd_file_list(args) -> int:
    resp = dispatch(ListFileTables(args.path))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.source_listing(resp.result, "No Parquet, ORC or Avro sources found.", "Sources", True)
    return 0


def cmd_iceberg_list(args) -> int:
    resp = dispatch(ListIcebergTables(args.path))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.source_listing(resp.result, "No Iceberg tables found.", "Iceberg tables", False)
    return 0


# ── Registered schemas ────────────────────────────────────────────────────────

def cmd_show(args) -> int:
    resp = dispatch(GetSchema(args.table, with_probe=True))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.schema(args.table, resp.result)
    return 0


def cmd_list(args) -> int:
    if args.what == "schemas":
        resp = dispatch(ListSchemas())
        if not resp.ok:
            return show.error(resp, hint=False)
        show.schema_list(resp.result)
    else:  # subscriptions
        resp = dispatch(ListSubscriptions(with_state=True))
        if not resp.ok:
            return show.error(resp, hint=False)
        show.subscriptions(resp.result)
    return 0


# ── Consumers ─────────────────────────────────────────────────────────────────

def _split_columns(raw):
    return tuple(c.strip() for c in raw.split(",") if c.strip()) if raw else None


def cmd_subscribe(args) -> int:
    columns = _split_columns(args.columns)
    resp = dispatch(Subscribe(args.consumer, args.table, columns))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.subscribed(args.consumer, args.table, resp.result, all_columns=columns is None)
    return 0


def cmd_unsubscribe(args) -> int:
    resp = dispatch(Unsubscribe(args.consumer, args.table))
    if not resp.ok:
        return show.error(resp, hint=False)
    print(green(f"✓ '{args.consumer}' unsubscribed from '{args.table}'"))
    return 0


def cmd_consumer_refresh(args) -> int:
    resp = dispatch(RefreshConsumer(args.consumer, args.table))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.refreshed(args.consumer, args.table, resp.result)
    return 0


def cmd_consumers(args) -> int:
    resp = dispatch(ListSubscriptions(table=args.table, with_state=True))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.table_consumers(args.table, resp.result)
    return 0


# ── Probes ────────────────────────────────────────────────────────────────────

def _probe_config(args):
    """Build the probe config from ``probe register`` flags; (config, None) or (None, error)."""
    probe_type = args.probe_type or "rest"
    if probe_type == "rest":
        if not args.url:
            return None, "--url is required for rest probes"
        return RestProbeConfig(schema_name=args.schema_name, url=args.url,
                               headers=_header_entries(args.header, warn=True),
                               env=args.env or []), None
    if probe_type == "postgres":
        if not args.dsn:
            return None, "--dsn is required for postgres probes"
        if not args.table and not args.query:
            return None, "Either --table or --query is required for postgres probes"
        if args.table and args.query:
            return None, "Provide either --table or --query, not both"
        return PostgresProbeConfig(schema_name=args.schema_name, dsn=args.dsn, env=args.env or [],
                                   table=args.table or None, query=args.query or None,
                                   db_schema=args.db_schema or "public",
                                   sample_size=args.sample_size or 100), None
    if probe_type in ("file", "iceberg"):
        if not args.path:
            return None, f"--path is required for {probe_type} probes"
        if probe_type == "file":
            config = FileProbeConfig(schema_name=args.schema_name, path=args.path, env=[],
                                     file_format=args.file_format or "auto")
        else:
            config = IcebergProbeConfig(schema_name=args.schema_name, path=args.path, env=[])
        config.env = args.env or declared_env(config)
        return config, None
    return None, f"Unknown probe type: {probe_type!r}"


def cmd_probe_register(args) -> int:
    config, err = _probe_config(args)
    if err:
        return show.fail(err)
    resp = dispatch(RegisterProbe(config))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.probe_registered(args.schema_name, resp.result.config)
    return 0


def cmd_probe_show(args) -> int:
    resp = dispatch(GetProbe(args.schema_name))
    if not resp.ok:
        return show.error(resp, hint=False)
    show.probe(args.schema_name, resp.result.config)
    return 0


def cmd_probe_list(_args) -> int:
    resp = dispatch(ListProbes())
    if not resp.ok:
        return show.error(resp, hint=False)
    show.probe_list(resp.result)
    return 0


# ── Template variables ────────────────────────────────────────────────────────

def cmd_var_set(args) -> int:
    resp = dispatch(SetVar(args.name, args.value))
    if not resp.ok:
        return show.error(resp, hint=False)
    print(green(f"✓ Set {{{{ {args.name} }}}} = {args.value!r}"))
    return 0


def cmd_var_get(args) -> int:
    resp = dispatch(GetVar(args.name))
    if not resp.ok:
        return show.error(resp)
    print(f"{args.name} = {resp.result.value!r}")
    return 0


def cmd_var_list(_args) -> int:
    resp = dispatch(ListVars())
    if not resp.ok:
        return show.error(resp, hint=False)
    show.var_list(resp.result)
    return 0


def cmd_var_unset(args) -> int:
    resp = dispatch(UnsetVar(args.name))
    if not resp.ok:
        return show.error(resp, hint=False)
    print(green(f"✓ Deleted template variable '{args.name}'"))
    return 0


# ── ODCS contracts ────────────────────────────────────────────────────────────

def cmd_odcs_export(args) -> int:
    resp = dispatch(ExportContract(args.name, args.version))
    if not resp.ok:
        return show.error(resp, hint=False, file=sys.stderr)
    fmt = args.format or ("yaml" if args.out and odcs.is_yaml_path(args.out) else "json")
    try:
        text = odcs.dumps_contract(resp.result.contract, fmt)
    except ImportError as e:
        return show.fail(str(e), file=sys.stderr)
    if not args.out:
        sys.stdout.write(text)
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(green(f"✓ Wrote {args.out}") +
          f"  (contract version {resp.result.contract_version}, "
          f"registry version {resp.result.version_number})")
    return 0


def _load_contract_or_report(path):
    try:
        return odcs.load_contract(path)
    except (OSError, ValueError, ImportError) as e:
        print(red(f"✗ Cannot read contract {path}: {e}"))
    except Exception as e:     # yaml.YAMLError and friends
        print(red(f"✗ Cannot parse contract {path}: {e}"))
    return None


def cmd_odcs_import(args) -> int:
    doc = _load_contract_or_report(args.contract)
    if doc is None:
        return 2
    resp = dispatch(ImportContract(doc, args.name_prefix or ""))
    if not resp.ok:
        return show.error(resp, hint=False)
    for entry in resp.result.registered:
        print(green(f"✓ Registered '{entry['name']}'") + f"  (version {entry['version_count']})")
    show.ignored_fields(resp.result.ignored)
    return 0


def cmd_odcs_check(args) -> int:
    doc = _load_contract_or_report(args.contract)
    if doc is None:
        return 2
    resp = dispatch(CheckContract(doc, probe=args.probe, name=args.name, object=args.object))
    if not resp.ok:
        objects = resp.error.details.get("objects")
        if objects is not None:
            print(red(f"✗ No schema object to check in {args.contract}") +
                  f" (objects: {', '.join(map(str, objects)) or 'none'}); pass --object NAME.")
            return 2
        return show.error(resp, hint=False)

    check = resp.result
    show.diff(f"{check.label}' vs contract '{check.object_name}", check.diff,
              f"contract version {check.contract_version}", getattr(args, "details", False))
    if not check.diff.changes:
        return 0
    if check.updated_contract is None:
        if args.update:
            print(yellow("  --update ignored: only all-SAFE changes are written to the contract."))
        return 1
    print(f"  Suggested contract update (version {check.contract_version} → "
          f"{check.new_version or check.contract_version}):")
    for line in check.diff.suggested_update.get("explanation", []):
        print(f"    • {line}")
    if not args.update:
        print("  Run the same command with --update to write it to the contract.")
        return 1
    try:
        odcs.save_contract(check.updated_contract, args.contract)
    except (OSError, ImportError) as e:
        return show.fail(f"Contract not updated: {e}")
    print(green(f"✓ Contract updated: {args.contract} is now version {check.updated_contract.get('version')}"))
    return 0


# ── Agent / MCP server ────────────────────────────────────────────────────────

def cmd_agent(args) -> int:
    from ..agent import run_agent
    run_agent(model=args.model, allow_writes=args.allow_writes)
    return 0


def cmd_mcp(args) -> int:
    from ..mcp_server import run
    run(allow_writes=True if args.allow_writes else None)
    return 0
