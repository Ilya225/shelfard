#!/usr/bin/env python3
"""
Shelfard — schema drift detection CLI.

Usage:
  shelfard rest snapshot <url> [--name NAME] [--bearer TOKEN] [--header KEY=VALUE ...]
  shelfard rest check    <url> [--name NAME] [--bearer TOKEN] [--header KEY=VALUE ...]
  shelfard --help
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from tools import (
    ColumnSchema, RestEndpointReader, TableSchema,
    compare_schemas_from_dicts, get_registered_schema, register_schema,
)
from tools.models import ChangeSeverity


# ── ANSI colours ──────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()


def _colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def red(t: str)    -> str: return _colour(t, "31")
def yellow(t: str) -> str: return _colour(t, "33")
def green(t: str)  -> str: return _colour(t, "32")
def bold(t: str)   -> str: return _colour(t, "1")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _url_to_schema_name(url: str) -> str:
    """Derive a stable, filesystem-safe schema name from a URL.

    https://api.example.com/users/1  →  "api_example_com_users_1"
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_")
    host = parsed.netloc.replace(".", "_").replace(":", "_")
    raw = f"{host}_{path}" if path else host
    return re.sub(r"[^a-z0-9_]", "_", raw.lower()).strip("_") or "schema"


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
    icon = "✗" if severity == ChangeSeverity.BREAKING else "⚠"
    header_fn = red if severity == ChangeSeverity.BREAKING else yellow

    print(header_fn(f"{icon} Schema drift detected for '{schema_name}'"))
    print(f"  {diff['summary']}")
    print()

    for change in diff["changes"]:
        sev = change["severity"]
        label = f"[{sev:<8}]"
        if sev == ChangeSeverity.BREAKING:
            label = red(label)
        elif sev == ChangeSeverity.WARNING:
            label = yellow(label)
        else:
            label = green(label)

        print(f"  {label} {change['change_type']:<25} '{change['column_name']}'")
        print(f"             {change['reasoning']}")
        print()


# ── REST commands ─────────────────────────────────────────────────────────────

def cmd_rest_snapshot(args) -> int:
    schema_name = args.name or _url_to_schema_name(args.url)
    headers = _parse_headers(args.header)

    print(f"Fetching {args.url} …")
    result = RestEndpointReader(
        args.url, schema_name,
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
    return 0


def cmd_rest_check(args) -> int:
    schema_name = args.name or _url_to_schema_name(args.url)
    headers = _parse_headers(args.header)

    print(f"Fetching {args.url} …")
    new_result = RestEndpointReader(
        args.url, schema_name,
        bearer_token=args.bearer or None,
        headers=headers or None,
    ).get_schema()

    if not new_result.success:
        print(red(f"✗ Failed to fetch schema: {new_result.error}"))
        return 2

    baseline_result = get_registered_schema(schema_name)
    if not baseline_result.success:
        print(red(f"✗ No snapshot found for '{schema_name}'."))
        print(f"  Run:  shelfard rest snapshot {args.url}" +
              (f" --name {schema_name}" if args.name else ""))
        return 2

    diff_result = compare_schemas_from_dicts(
        baseline_result.data["schema"],
        new_result.data["schema"],
    )
    if not diff_result.success:
        print(red(f"✗ Comparison failed: {diff_result.error}"))
        return 2

    diff = diff_result.data["diff"]
    _print_diff(schema_name, diff, baseline_result.data["version"])
    return 0 if not diff["changes"] else 1


# ── Parser ────────────────────────────────────────────────────────────────────

def _add_rest_subcommands(readers) -> None:
    rest_p = readers.add_parser("rest", help="REST API endpoint reader")
    rest_cmds = rest_p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Shared options for all REST commands
    rest_base = argparse.ArgumentParser(add_help=False)
    rest_base.add_argument("url", help="Endpoint URL to fetch")
    rest_base.add_argument(
        "--name", metavar="NAME",
        help="Schema name stored in the registry (default: derived from URL)",
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
    snap.set_defaults(func=cmd_rest_snapshot)

    chk = rest_cmds.add_parser(
        "check", parents=[rest_base],
        help="Fetch the endpoint and compare its schema against the saved snapshot",
    )
    chk.set_defaults(func=cmd_rest_check)


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

    readers = parser.add_subparsers(
        dest="reader", required=True,
        metavar="READER",
        help="Data source type",
    )

    _add_rest_subcommands(readers)
    # Future: _add_sqlite_subcommands(readers)
    # Future: _add_postgres_subcommands(readers)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
