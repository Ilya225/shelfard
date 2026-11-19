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
  shelfard odcs check    <contract> (--probe NAME | --name NAME) [--object OBJ] [--update]
  shelfard show          <table>
  shelfard list          {schemas,subscriptions}
  shelfard subscribe     <table> --consumer NAME [--columns PATH1,PATH2,...]
  shelfard unsubscribe   <table> --consumer NAME
  shelfard consumers     <table>
  shelfard consumer refresh <table> --consumer NAME
  shelfard agent
  shelfard --help

Layout: ``parser`` (argparse), ``controllers`` (args → handler payloads →
dispatch), ``presenters`` (console output). Execution lives in ``shelfard.handlers``.
"""

import sys

from .controllers import (  # noqa: F401  (re-exported)
    _schema_from_result,
    cmd_agent, cmd_consumer_refresh, cmd_consumers, cmd_file_check, cmd_file_list,
    cmd_file_snapshot, cmd_iceberg_list, cmd_list, cmd_mcp, cmd_odcs_check, cmd_odcs_export,
    cmd_odcs_import, cmd_postgres_check, cmd_postgres_snapshot, cmd_probe_list,
    cmd_probe_register, cmd_probe_run, cmd_probe_show, cmd_rest_check, cmd_rest_snapshot,
    cmd_show, cmd_subscribe, cmd_unsubscribe, cmd_var_get, cmd_var_list, cmd_var_set,
    cmd_var_unset,
)
from .parser import build_parser  # noqa: F401


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))
