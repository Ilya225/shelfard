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
import re

from mcp.server.fastmcp import FastMCP

from .registry import (
    get_all_consumers,
    get_all_probes,
    get_all_schemas,
    get_consumer_subscription,
    get_registered_schema,
    get_probe,
    register_probe as _register_probe,
    resolve_template as _resolve_template,
    run_probe as _run_probe,
    set_var as _set_var,
    get_var as _get_var,
    list_vars as _list_vars,
    delete_var as _delete_var,
    accept_update as _accept_update,
)
from .models import ToolResult
from .security import (
    check_env_allowed, check_pg_dsn_safe, check_url_safe, extract_env_refs,
    private_hosts_allowed, validate_name,
)

ALLOW_WRITES_ENV = "SHELFARD_MCP_ALLOW_WRITES"

_TEMPLATE_REF = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _error(message: str) -> str:
    return ToolResult(success=False, error=message).to_json()


# ── Read-only tools ───────────────────────────────────────────────────────────

def get_schema(schema_name: str) -> str:
    """Retrieve the latest saved schema for a named data source from the registry.
    Returns column names, types, nullability, version timestamp, and probe info if registered."""
    result = get_registered_schema(schema_name)
    if not result.success:
        return result.to_json()
    probe_result = get_probe(schema_name)
    if probe_result.success:
        data = _json.loads(result.to_json())
        data["data"]["probe"] = probe_result.data["probe"]
        return _json.dumps(data, indent=2)
    return result.to_json()


def get_schemas() -> str:
    """List all schemas stored in the registry with summary info:
    name, version count, latest version timestamp, source, and column count."""
    return get_all_schemas().to_json()


def get_subscriptions() -> str:
    """List all consumer subscriptions across all tables."""
    return get_all_consumers().to_json()


def get_subscription(consumer_name: str, table_name: str) -> str:
    """Get a specific consumer's subscription for a given table,
    including subscribed columns and the schema snapshot at subscription time."""
    return get_consumer_subscription(consumer_name, table_name).to_json()


def get_probe_config(schema_name: str) -> str:
    """Get the registered probe config for a schema (url, env vars, headers)."""
    return get_probe(schema_name).to_json()


def live_check_schema(schema_name: str) -> str:
    """Run the registered probe for a schema against the live endpoint and return the drift result.
    When every change is SAFE, the diff includes suggested_update (explanation + next baseline);
    it is applied only through accept_schema_update. Every change is labelled with its
    consumer impact (no_consumers / consumer_constrained, with each consumer's own severity)
    and consumer_summary gives total/affected consumer counts and outdated projections."""
    return _run_probe(schema_name).to_json()


def get_schema_odcs(schema_name: str, version: int | None = None) -> str:
    """Export a registered schema as an ODCS v3.1.0 data contract (JSON). ``version`` is the
    1-based registry version (default: latest); the contract version is semver derived from
    the history (BREAKING → major, WARNING → minor, SAFE → patch)."""
    from .odcs import export_odcs
    return export_odcs(schema_name, version).to_json()


def get_template_var(name: str) -> str:
    """Retrieve a stored template variable by name."""
    return _get_var(name).to_json()


def list_template_vars() -> str:
    """List all stored template variables and their values."""
    return _list_vars().to_json()


# ── Write tools (only exposed with --allow-writes) ────────────────────────────

def _vars_used_by_probes() -> set[str]:
    """Return every {{var}} name referenced by any registered probe."""
    used: set[str] = set()
    listing = get_all_probes()
    for summary in listing.data.get("probes", []) if listing.success else []:
        res = get_probe(summary["schema_name"])
        if res.success:
            used.update(_TEMPLATE_REF.findall(_json.dumps(res.data["probe"])))
    return used


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
    from .models import RestProbeConfig

    if err := validate_name(schema_name, "schema name"):
        return _error(err)
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

    if err := _check_not_registered(schema_name):
        return _error(err)

    header_values = [v for h in header_list for v in h.values()]
    if err := _check_env_refs(env_list, url, *header_values):
        return _error(err)

    if err := check_url_safe(_resolve_template(url)):
        return _error(err)

    config = RestProbeConfig(
        schema_name=schema_name,
        url=url,
        headers=header_list,
        env=env_list,
        registered_via="mcp",
    )
    return _register_probe(schema_name, config).to_json()


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
    from .models import PostgresProbeConfig

    if err := validate_name(schema_name, "schema name"):
        return _error(err)
    if not _SQL_IDENT.fullmatch(table or ""):
        return _error(f"Invalid table name {table!r}: must be a plain SQL identifier.")
    if not _SQL_IDENT.fullmatch(db_schema or ""):
        return _error(f"Invalid db_schema {db_schema!r}: must be a plain SQL identifier.")
    try:
        env_list = _json.loads(env)
    except _json.JSONDecodeError as e:
        return _error(f"env must be a JSON array: {e}")
    if err := _check_env_list(env_list):
        return _error(err)
    if err := _check_not_registered(schema_name):
        return _error(err)
    if err := _check_env_refs(env_list, dsn):
        return _error(err)

    if not private_hosts_allowed():
        err, _ = check_pg_dsn_safe(_resolve_template(dsn))
        if err:
            return _error(err)

    config = PostgresProbeConfig(
        schema_name=schema_name,
        dsn=dsn,
        env=env_list,
        table=table,
        db_schema=db_schema,
        registered_via="mcp",
    )
    return _register_probe(schema_name, config).to_json()


def _check_env_list(env_list) -> str | None:
    if not (isinstance(env_list, list) and all(isinstance(n, str) for n in env_list)):
        return "env must be a JSON array of strings."
    return None


def _check_not_registered(schema_name: str) -> str | None:
    if get_probe(schema_name).success:
        return (
            f"A probe for '{schema_name}' already exists. Probes cannot be overwritten "
            f"through MCP; change it with 'shelfard probe register' instead."
        )
    return None


def _check_env_refs(env_list: list[str], *templates: str) -> str | None:
    """Env vars must be allowlisted and every $VAR referenced must be declared in env."""
    referenced = extract_env_refs(*templates)
    if err := check_env_allowed(sorted(set(env_list) | referenced)):
        return err
    undeclared = sorted(referenced - set(env_list))
    if undeclared:
        return f"$VAR references not declared in env: {', '.join(undeclared)}"
    return None


def set_template_var(name: str, value: str) -> str:
    """Store a named template variable for use as {{name}} in probe configs and snapshot
    commands. Values are plain text — use $ENV_VAR for secrets. Variables already used by a
    registered probe cannot be changed through MCP."""
    if name in _vars_used_by_probes():
        return _error(
            f"Template variable '{name}' is used by a registered probe and cannot be "
            f"changed through MCP. Use 'shelfard var set {name} <value>' instead."
        )
    return _set_var(name, value).to_json()


def delete_template_var(name: str) -> str:
    """Delete a stored template variable by name. Variables used by a registered probe
    cannot be deleted through MCP."""
    if name in _vars_used_by_probes():
        return _error(
            f"Template variable '{name}' is used by a registered probe and cannot be "
            f"deleted through MCP. Use 'shelfard var unset {name}' instead."
        )
    return _delete_var(name).to_json()


def accept_schema_update(schema_name: str, allow_unaffected: bool = False) -> str:
    """Re-run the registered probe for a schema and save the suggested schema as the next
    baseline version. Applies only if every change is SAFE, or — with allow_unaffected=True —
    if every WARNING/BREAKING change touches no consumer. Consumer projections are never changed;
    the result lists consumers whose projection is now outdated."""
    check = _run_probe(schema_name)
    if not check.success:
        return check.to_json()
    return _accept_update(
        schema_name, check.data["diff"], check.data["baseline_version"],
        allow_unaffected=allow_unaffected,
    ).to_json()


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
