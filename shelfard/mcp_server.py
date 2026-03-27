"""
Shelfard MCP server — exposes registry tools over the Model Context Protocol.

Run standalone (stdio transport, for use with any MCP client):
    shelfard mcp
    python -m shelfard.mcp_server

Claude Desktop / Cursor config:
    {
      "mcpServers": {
        "shelfard": {
          "command": "shelfard",
          "args": ["mcp"]
        }
      }
    }
"""

from mcp.server.fastmcp import FastMCP

import json as _json

from .registry import (
    get_all_consumers,
    get_all_schemas,
    get_consumer_subscription,
    get_registered_schema,
    get_checker,
    register_checker as _register_checker,
    run_checker as _run_checker,
    set_var as _set_var,
    get_var as _get_var,
    list_vars as _list_vars,
    delete_var as _delete_var,
)


mcp = FastMCP(name="shelfard")


@mcp.tool()
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


@mcp.tool()
def get_schemas() -> str:
    """List all schemas stored in the registry with summary info:
    name, version count, latest version timestamp, source, and column count."""
    return get_all_schemas().to_json()


@mcp.tool()
def get_subscriptions() -> str:
    """List all consumer subscriptions across all tables."""
    return get_all_consumers().to_json()


@mcp.tool()
def get_subscription(consumer_name: str, table_name: str) -> str:
    """Get a specific consumer's subscription for a given table,
    including subscribed columns and the schema snapshot at subscription time."""
    return get_consumer_subscription(consumer_name, table_name).to_json()


@mcp.tool()
def register_checker(
    schema_name: str,
    url: str,
    env: str = "[]",
    headers: str = "[]",
) -> str:
    """Register a REST checker for a schema.
    env: JSON array of env var names, e.g. '["BEARER_TOKEN"]'
    headers: JSON array of header dicts, e.g. '[{"Authorization": "$BEARER_TOKEN"}]'"""
    from .models import RestCheckerConfig
    config = RestCheckerConfig(
        schema_name=schema_name,
        url=url,
        headers=_json.loads(headers),
        env=_json.loads(env),
    )
    return _register_checker(schema_name, config).to_json()


@mcp.tool()
def get_checker_config(schema_name: str) -> str:
    """Get the registered checker config for a schema (url, env vars, headers)."""
    return get_checker(schema_name).to_json()


@mcp.tool()
def live_check_schema(schema_name: str) -> str:
    """Run the registered checker for a schema against the live endpoint and return the drift result."""
    return _run_checker(schema_name).to_json()


@mcp.tool()
def set_template_var(name: str, value: str) -> str:
    """Store a named template variable for use as {{name}} in checker configs and snapshot
    commands. Values are plain text — use $ENV_VAR for secrets."""
    return _set_var(name, value).to_json()


@mcp.tool()
def get_template_var(name: str) -> str:
    """Retrieve a stored template variable by name."""
    return _get_var(name).to_json()


@mcp.tool()
def list_template_vars() -> str:
    """List all stored template variables and their values."""
    return _list_vars().to_json()


@mcp.tool()
def delete_template_var(name: str) -> str:
    """Delete a stored template variable by name."""
    return _delete_var(name).to_json()


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
