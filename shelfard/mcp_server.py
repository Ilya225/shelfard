"""
Shelfard MCP server — exposes registry tools over the Model Context Protocol.

Run standalone (stdio transport, for use with any MCP client):
    shelfard mcp                    # read-only tools + live checks of existing checkers
    shelfard mcp --allow-writes     # also register checkers / modify template vars
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

* Write tools (``register_checker``, ``set_template_var``, ``delete_template_var``)
  are only exposed with ``--allow-writes`` (or ``SHELFARD_MCP_ALLOW_WRITES=1``).
* Checkers registered through MCP may only reference env vars prefixed with
  ``SHELFARD_`` or listed in ``SHELFARD_MCP_ENV_ALLOWLIST``, may only target public
  http(s) hosts (unless ``SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1``), and may not
  overwrite an existing checker.
* Template variables referenced by an existing checker cannot be changed through
  MCP, so a stored checker (and its credentials) cannot be redirected.
* All names used in file paths are validated by the registry (path-traversal guard).
"""

from __future__ import annotations

import argparse
import json as _json
import os
import re

from mcp.server.fastmcp import FastMCP

from .registry import (
    get_all_consumers,
    get_all_checkers,
    get_all_schemas,
    get_consumer_subscription,
    get_registered_schema,
    get_checker,
    register_checker as _register_checker,
    resolve_template as _resolve_template,
    run_checker as _run_checker,
    set_var as _set_var,
    get_var as _get_var,
    list_vars as _list_vars,
    delete_var as _delete_var,
)
from .models import ToolResult
from .security import check_env_allowed, check_url_safe, extract_env_refs, validate_name

ALLOW_WRITES_ENV = "SHELFARD_MCP_ALLOW_WRITES"

_TEMPLATE_REF = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _error(message: str) -> str:
    return ToolResult(success=False, error=message).to_json()


# ── Read-only tools ───────────────────────────────────────────────────────────

def get_schema(schema_name: str) -> str:
    """Retrieve the latest saved schema for a named data source from the registry.
    Returns column names, types, nullability, version timestamp, and checker info if registered."""
    result = get_registered_schema(schema_name)
    if not result.success:
        return result.to_json()
    checker_result = get_checker(schema_name)
    if checker_result.success:
        data = _json.loads(result.to_json())
        data["data"]["checker"] = checker_result.data["checker"]
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


def get_checker_config(schema_name: str) -> str:
    """Get the registered checker config for a schema (url, env vars, headers)."""
    return get_checker(schema_name).to_json()


def live_check_schema(schema_name: str) -> str:
    """Run the registered checker for a schema against the live endpoint and return the drift result."""
    return _run_checker(schema_name).to_json()


def get_template_var(name: str) -> str:
    """Retrieve a stored template variable by name."""
    return _get_var(name).to_json()


def list_template_vars() -> str:
    """List all stored template variables and their values."""
    return _list_vars().to_json()


# ── Write tools (only exposed with --allow-writes) ────────────────────────────

def _vars_used_by_checkers() -> set[str]:
    """Return every {{var}} name referenced by any registered checker."""
    used: set[str] = set()
    listing = get_all_checkers()
    for summary in listing.data.get("checkers", []) if listing.success else []:
        res = get_checker(summary["schema_name"])
        if res.success:
            used.update(_TEMPLATE_REF.findall(_json.dumps(res.data["checker"])))
    return used


def register_checker(
    schema_name: str,
    url: str,
    env: str = "[]",
    headers: str = "[]",
) -> str:
    """Register a new REST checker for a schema (cannot overwrite an existing one).
    url: public http(s) URL; may use {{template_vars}} and $ENV_VAR placeholders.
    env: JSON array of env var names, e.g. '["SHELFARD_API_TOKEN"]'. Only names prefixed
         with SHELFARD_ (or listed in SHELFARD_MCP_ENV_ALLOWLIST) are allowed.
    headers: JSON array of header dicts, e.g. '[{"Authorization": "Bearer $SHELFARD_API_TOKEN"}]'"""
    from .models import RestCheckerConfig

    if err := validate_name(schema_name, "schema name"):
        return _error(err)
    try:
        env_list = _json.loads(env)
        header_list = _json.loads(headers)
    except _json.JSONDecodeError as e:
        return _error(f"env and headers must be JSON arrays: {e}")
    if not (isinstance(env_list, list) and all(isinstance(n, str) for n in env_list)):
        return _error("env must be a JSON array of strings.")
    if not (
        isinstance(header_list, list)
        and all(
            isinstance(h, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in h.items())
            for h in header_list
        )
    ):
        return _error("headers must be a JSON array of {name: value} string objects.")

    if get_checker(schema_name).success:
        return _error(
            f"A checker for '{schema_name}' already exists. Checkers cannot be overwritten "
            f"through MCP; change it with 'shelfard checker register' instead."
        )

    header_values = [v for h in header_list for v in h.values()]
    referenced = extract_env_refs(url, *header_values)
    if err := check_env_allowed(sorted(set(env_list) | referenced)):
        return _error(err)
    undeclared = sorted(referenced - set(env_list))
    if undeclared:
        return _error(f"$VAR references not declared in env: {', '.join(undeclared)}")

    if err := check_url_safe(_resolve_template(url)):
        return _error(err)

    config = RestCheckerConfig(
        schema_name=schema_name,
        url=url,
        headers=header_list,
        env=env_list,
    )
    return _register_checker(schema_name, config).to_json()


def set_template_var(name: str, value: str) -> str:
    """Store a named template variable for use as {{name}} in checker configs and snapshot
    commands. Values are plain text — use $ENV_VAR for secrets. Variables already used by a
    registered checker cannot be changed through MCP."""
    if name in _vars_used_by_checkers():
        return _error(
            f"Template variable '{name}' is used by a registered checker and cannot be "
            f"changed through MCP. Use 'shelfard var set {name} <value>' instead."
        )
    return _set_var(name, value).to_json()


def delete_template_var(name: str) -> str:
    """Delete a stored template variable by name. Variables used by a registered checker
    cannot be deleted through MCP."""
    if name in _vars_used_by_checkers():
        return _error(
            f"Template variable '{name}' is used by a registered checker and cannot be "
            f"deleted through MCP. Use 'shelfard var unset {name}' instead."
        )
    return _delete_var(name).to_json()


READ_TOOLS = (
    get_schema, get_schemas, get_subscriptions, get_subscription,
    get_checker_config, live_check_schema, get_template_var, list_template_vars,
)
WRITE_TOOLS = (register_checker, set_template_var, delete_template_var)


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
        help="Expose tools that register checkers and modify template variables.",
    )
    run(parser.parse_args(argv).allow_writes)


if __name__ == "__main__":
    main()
