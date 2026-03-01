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

from .registry import (
    get_all_consumers,
    get_all_schemas,
    get_consumer_subscription,
    get_registered_schema,
)


mcp = FastMCP(name="shelfard")


@mcp.tool()
def get_schema(schema_name: str) -> str:
    """Retrieve the latest saved schema for a named data source from the registry.
    Returns column names, types, nullability, and the version timestamp."""
    return get_registered_schema(schema_name).to_json()


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


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
