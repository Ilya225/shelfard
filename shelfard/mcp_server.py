"""
Shelfard MCP server — exposes registry tools over the Model Context Protocol.

Run standalone (stdio transport, for use with any MCP client):
    shelfard mcp                    # read-only tools + live checks of existing probes
    shelfard mcp --allow-writes     # also register probes / modify template vars
    python -m shelfard.mcp_server [--allow-writes]

Claude Desktop / Cursor config:
    {
      "mcpServers": {
        "shelfard": {
          "command": "shelfard",
          "args": ["mcp"]
        }
      }
    }

Security model
--------------
Tool inputs come from an LLM, and the LLM reads untrusted data (e.g. field names
returned by remote APIs), so every tool input is treated as attacker-controlled:

* Write tools (``register_probe``, ``register_postgres_probe``,
  ``set_template_var``, ``delete_template_var``, ``accept_schema_update``)
  are only exposed with
  ``--allow-writes`` (or ``SHELFARD_MCP_ALLOW_WRITES=1``).
* Probes registered through MCP may only reference env vars prefixed with
  ``SHELFARD_`` or listed in ``SHELFARD_MCP_ENV_ALLOWLIST``, may only target public
  hosts (unless ``SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1``), and may not overwrite an
  existing probe. They are stored with ``registered_via="mcp"`` and re-checked when
  they run: REST connections are verified on the connected socket (incl. redirects) and
  PostgreSQL connections are pinned to a validated IP (DNS-rebinding guard).
* PostgreSQL probes registered through MCP are table-mode only (no custom SQL).
* ``accept_schema_update`` takes only a schema name (and ``allow_unaffected``): it
  re-runs the stored probe server-side and registers the suggested baseline only if
  every change is SAFE — or, with ``allow_unaffected``, if every WARNING/BREAKING
  change touches no consumer — so the LLM can never supply schema content.
* Consumer projections are never changed through MCP (no subscribe / refresh tools):
  they belong to the consuming teams.
* Template variables referenced by an existing probe cannot be changed through
  MCP, so a stored probe (and its credentials) cannot be redirected.
* All names used in file paths are validated by the registry (path-traversal guard).
* ODCS is export-only over MCP (``get_schema_odcs``); importing contracts or writing
  contract files stays a CLI action, so the LLM can't supply schemas or choose paths.
"""

from __future__ import annotations

import argparse
import json as _json
import os

from mcp.server.fastmcp import FastMCP

from .handlers import (
    VIA_MCP, AcceptUpdate, CheckDrift, ExportContract, GetProbe, GetSchema, GetSubscription,
    GetVar, ListSchemas, ListSubscriptions, ListVars, RegisterProbe, Response, SetVar, UnsetVar,
    dispatch,
)
from .handlers.core import ErrorCode
from .models import PostgresProbeConfig, RestProbeConfig

ALLOW_WRITES_ENV = "SHELFARD_MCP_ALLOW_WRITES"


def _error(message: str) -> str:
    return Response.fail(ErrorCode.INVALID, message).to_tool_json()


# Tools return the legacy ToolResult envelope (success / data / error / next_action_hint)
# via Response.to_tool_json(), so MCP clients see the same JSON as before handlers.

# ── Read-only tools ───────────────────────────────────────────────────────────

def get_schema(schema_name: str) -> str:
    """Retrieve the latest saved schema for a named data source from the registry.
    Returns column names, types, nullability, version timestamp, and probe info if registered."""
    return dispatch(GetSchema(schema_name, with_probe=True)).to_tool_json()


def get_schemas() -> str:
    """List all schemas stored in the registry with summary info:
    name, version count, latest version timestamp, source, and column count."""
    return dispatch(ListSchemas()).to_tool_json()


def get_subscriptions() -> str:
    """List all consumer subscriptions across all tables."""
    return dispatch(ListSubscriptions()).to_tool_json()


def get_subscription(consumer_name: str, table_name: str) -> str:
    """Get a specific consumer's subscription for a given table,
    including subscribed columns and the schema snapshot at subscription time."""
    return dispatch(GetSubscription(consumer_name, table_name)).to_tool_json()


def get_probe_config(schema_name: str) -> str:
    """Get the registered probe config for a schema (url, env vars, headers)."""
    return dispatch(GetProbe(schema_name)).to_tool_json()


def live_check_schema(schema_name: str) -> str:
    """Run the registered probe for a schema against the live endpoint and return the drift result.
    When every change is SAFE, the diff includes suggested_update (explanation + next baseline);
    it is applied only through accept_schema_update. Every change is labelled with its
    consumer impact (no_consumers / consumer_constrained, with each consumer's own severity)
    and consumer_summary gives total/affected consumer counts and outdated projections."""
    return dispatch(CheckDrift(schema_name)).to_tool_json()


def get_schema_odcs(schema_name: str, version: int | None = None) -> str:
    """Export a registered schema as an ODCS v3.1.0 data contract (JSON). ``version`` is the
    1-based registry version (default: latest); the contract version is semver derived from
    the history (BREAKING → major, WARNING → minor, SAFE → patch)."""
    return dispatch(ExportContract(schema_name, version)).to_tool_json()


def get_template_var(name: str) -> str:
    """Retrieve a stored template variable by name."""
    return dispatch(GetVar(name)).to_tool_json()


def list_template_vars() -> str:
    """List all stored template variables and their values."""
    return dispatch(ListVars()).to_tool_json()


# ── Write tools (only exposed with --allow-writes) ────────────────────────────
# The MCP security checks live in the handlers (RegisterProbe / SetVar / UnsetVar with
# via="mcp"); this layer only parses the JSON-string arguments.

def register_probe(
    schema_name: str,
    url: str,
    env: str = "[]",
    headers: str = "[]",
) -> str:
    """Register a new REST probe for a schema (cannot overwrite an existing one).
    url: public http(s) URL; may use {{template_vars}} and $ENV_VAR placeholders.
    env: JSON array of env var names, e.g. '["SHELFARD_API_TOKEN"]'. Only names prefixed
         with SHELFARD_ (or listed in SHELFARD_MCP_ENV_ALLOWLIST) are allowed.
    headers: JSON array of header dicts, e.g. '[{"Authorization": "Bearer $SHELFARD_API_TOKEN"}]'"""
    try:
        env_list = _json.loads(env)
        header_list = _json.loads(headers)
    except _json.JSONDecodeError as e:
        return _error(f"env and headers must be JSON arrays: {e}")
    if err := _check_env_list(env_list):
        return _error(err)
    if not (
        isinstance(header_list, list)
        and all(
            isinstance(h, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in h.items())
            for h in header_list
        )
    ):
        return _error("headers must be a JSON array of {name: value} string objects.")
    config = RestProbeConfig(schema_name=schema_name, url=url, headers=header_list, env=env_list)
    return dispatch(RegisterProbe(config, via=VIA_MCP)).to_tool_json()


def register_postgres_probe(
    schema_name: str,
    dsn: str,
    table: str,
    db_schema: str = "public",
    env: str = "[]",
) -> str:
    """Register a new PostgreSQL probe (table mode) for a schema; cannot overwrite one.
    dsn: e.g. 'postgresql://reader:$SHELFARD_PG_PASSWORD@db.example.com:5432/app'. The host
         must be literal or a {{template_var}} and resolve to a public address; $VAR may be
         used for credentials only.
    table: table or view to introspect. Custom SQL queries are not allowed through MCP.
    env: JSON array of env var names, e.g. '["SHELFARD_PG_PASSWORD"]'. Only names prefixed
         with SHELFARD_ (or listed in SHELFARD_MCP_ENV_ALLOWLIST) are allowed."""
    try:
        env_list = _json.loads(env)
    except _json.JSONDecodeError as e:
        return _error(f"env must be a JSON array: {e}")
    if err := _check_env_list(env_list):
        return _error(err)
    config = PostgresProbeConfig(schema_name=schema_name, dsn=dsn, env=env_list,
                                 table=table, db_schema=db_schema)
    return dispatch(RegisterProbe(config, via=VIA_MCP)).to_tool_json()


def _check_env_list(env_list) -> str | None:
    if not (isinstance(env_list, list) and all(isinstance(n, str) for n in env_list)):
        return "env must be a JSON array of strings."
    return None


def set_template_var(name: str, value: str) -> str:
    """Store a named template variable for use as {{name}} in probe configs and snapshot
    commands. Values are plain text — use $ENV_VAR for secrets. Variables already used by a
    registered probe cannot be changed through MCP."""
    return dispatch(SetVar(name, value, via=VIA_MCP)).to_tool_json()


def delete_template_var(name: str) -> str:
    """Delete a stored template variable by name. Variables used by a registered probe
    cannot be deleted through MCP."""
    return dispatch(UnsetVar(name, via=VIA_MCP)).to_tool_json()


def accept_schema_update(schema_name: str, allow_unaffected: bool = False) -> str:
    """Re-run the registered probe for a schema and save the suggested schema as the next
    baseline version. Applies only if every change is SAFE, or — with allow_unaffected=True —
    if every WARNING/BREAKING change touches no consumer. Consumer projections are never changed;
    the result lists consumers whose projection is now outdated."""
    check = dispatch(CheckDrift(schema_name))
    if not check.ok:
        return check.to_tool_json()
    report = check.result
    return dispatch(AcceptUpdate(schema_name, report.diff, report.baseline_version,
                                 allow_unaffected=allow_unaffected)).to_tool_json()


READ_TOOLS = (
    get_schema, get_schemas, get_subscriptions, get_subscription,
    get_probe_config, live_check_schema, get_schema_odcs, get_template_var, list_template_vars,
)
WRITE_TOOLS = (
    register_probe, register_postgres_probe, set_template_var, delete_template_var,
    accept_schema_update,
)


def build_server(allow_writes: bool = False) -> FastMCP:
    """Create the FastMCP server; write tools are registered only if ``allow_writes``."""
    server = FastMCP(name="shelfard")
    for fn in READ_TOOLS + (WRITE_TOOLS if allow_writes else ()):
        server.tool()(fn)
    return server


def run(allow_writes: bool | None = None) -> None:
    if allow_writes is None:
        allow_writes = os.environ.get(ALLOW_WRITES_ENV) == "1"
    build_server(allow_writes).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shelfard-mcp")
    parser.add_argument(
        "--allow-writes", action="store_true", default=None,
        help="Expose tools that register probes and modify template variables.",
    )
    run(parser.parse_args(argv).allow_writes)


if __name__ == "__main__":
    main()
