"""argparse definition of the ``shelfard`` CLI (no execution logic)."""

import argparse

from .controllers import (
    cmd_agent,
    cmd_consumer_refresh,
    cmd_consumers,
    cmd_file_check,
    cmd_file_list,
    cmd_file_snapshot,
    cmd_iceberg_list,
    cmd_list,
    cmd_mcp,
    cmd_odcs_check,
    cmd_odcs_export,
    cmd_odcs_import,
    cmd_postgres_check,
    cmd_postgres_snapshot,
    cmd_probe_list,
    cmd_probe_register,
    cmd_probe_run,
    cmd_probe_show,
    cmd_rest_check,
    cmd_rest_snapshot,
    cmd_show,
    cmd_subscribe,
    cmd_unsubscribe,
    cmd_var_get,
    cmd_var_list,
    cmd_var_set,
    cmd_var_unset,
)


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
        "--create-probe", action="store_true", default=False,
        help="Also register a probe config using the same URL and headers after snapshotting",
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
    chk.add_argument(
        "--allow-unaffected", action="store_true",
        help="With --update: also accept WARNING/BREAKING changes that no consumer reads",
    )
    chk.add_argument(
        "--details", action="store_true",
        help="List the individual consumers affected (default: counts only)",
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
        "--create-probe", action="store_true", default=False,
        help="Also register a probe config using the same DSN and table/query after snapshotting",
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
    chk.add_argument(
        "--allow-unaffected", action="store_true",
        help="With --update: also accept WARNING/BREAKING changes that no consumer reads",
    )
    chk.add_argument(
        "--details", action="store_true",
        help="List the individual consumers affected (default: counts only)",
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
        "--create-probe", action="store_true", default=False,
        help="Also register a probe config for the same path after snapshotting",
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
    chk.add_argument(
        "--allow-unaffected", action="store_true",
        help="With --update: also accept WARNING/BREAKING changes that no consumer reads",
    )
    chk.add_argument(
        "--details", action="store_true",
        help="List the individual consumers affected (default: counts only)",
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
        "--create-probe", action="store_true", default=False,
        help="Also register a probe config for the same table path after snapshotting",
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
    chk.add_argument(
        "--allow-unaffected", action="store_true",
        help="With --update: also accept WARNING/BREAKING changes that no consumer reads",
    )
    chk.add_argument(
        "--details", action="store_true",
        help="List the individual consumers affected (default: counts only)",
    )
    chk.set_defaults(func=cmd_file_check)

    lst = ice_cmds.add_parser("list", help="Find Iceberg tables under a warehouse directory")
    lst.add_argument("path", help="Warehouse directory (Hadoop-catalog layout)")
    lst.set_defaults(func=cmd_iceberg_list)


def _add_probe_subcommands(top) -> None:
    probe_p = top.add_parser("probe", help="Manage stored drift-check configurations")
    probe_cmds = probe_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    reg = probe_cmds.add_parser(
        "register",
        help="Register a probe config for a schema",
    )
    reg.add_argument("schema_name", help="Schema name to register a probe for")
    reg.add_argument(
        "--type", dest="probe_type", default="rest", choices=["rest", "postgres", "file", "iceberg"],
        help="Probe type (default: rest)",
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
    reg.set_defaults(func=cmd_probe_register)

    run = probe_cmds.add_parser("run", help="Run the registered probe for a schema")
    run.add_argument("schema_name", help="Schema name whose probe to run")
    run.add_argument(
        "--update", action="store_true",
        help="If every change is SAFE, save the suggested schema as the next baseline version",
    )
    run.add_argument(
        "--allow-unaffected", action="store_true",
        help="With --update: also accept WARNING/BREAKING changes that no consumer reads",
    )
    run.add_argument(
        "--details", action="store_true",
        help="List the individual consumers affected (default: counts only)",
    )
    run.set_defaults(func=cmd_probe_run)

    show = probe_cmds.add_parser("show", help="Display the registered probe config")
    show.add_argument("schema_name", help="Schema name whose probe to show")
    show.set_defaults(func=cmd_probe_show)

    lst = probe_cmds.add_parser("list", help="List all registered probes")
    lst.set_defaults(func=cmd_probe_list)


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
    src.add_argument("--probe", metavar="NAME", help="Read the live schema with this stored probe")
    src.add_argument("--name", metavar="NAME", help="Use the latest registered version of this schema")
    chk.add_argument("--object", metavar="OBJ",
                     help="Schema object in the contract (default: the only one, or the one named like the source)")
    chk.add_argument("--update", action="store_true",
                     help="When every change is SAFE, write them into the contract (bumps the patch version)")
    chk.add_argument("--details", action="store_true",
                     help="With --name: list the individual consumers affected (default: counts only)")
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
    _add_probe_subcommands(top)
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
            "Let the agent register probes and modify template variables "
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
            "Expose register_probe / set_template_var / delete_template_var "
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

    sub_p = top.add_parser("subscribe", help="Save a consumer's projection of a registered schema")
    sub_p.add_argument("table", help="Source schema name")
    sub_p.add_argument(
        "--consumer", metavar="NAME", required=True,
        help="Consumer name",
    )
    sub_p.add_argument(
        "--columns", metavar="COL1,COL2,...",
        help="Comma-separated column paths, e.g. id,address.zip,tags[] (default: all top-level columns)",
    )
    sub_p.set_defaults(func=cmd_subscribe)

    unsub_p = top.add_parser("unsubscribe", help="Remove a consumer's projection")
    unsub_p.add_argument("table", help="Source schema name")
    unsub_p.add_argument("--consumer", metavar="NAME", required=True, help="Consumer name")
    unsub_p.set_defaults(func=cmd_unsubscribe)

    cons_p = top.add_parser("consumers", help="List a schema's consumers and whether their projections are current")
    cons_p.add_argument("table", help="Source schema name")
    cons_p.set_defaults(func=cmd_consumers)

    consumer_p = top.add_parser("consumer", help="Consumer-side actions")
    consumer_cmds = consumer_p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    ref = consumer_cmds.add_parser(
        "refresh", help="Adopt the latest schema shape for the columns this consumer reads")
    ref.add_argument("table", help="Source schema name")
    ref.add_argument("--consumer", metavar="NAME", required=True, help="Consumer name")
    ref.set_defaults(func=cmd_consumer_refresh)

    return parser
