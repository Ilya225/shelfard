"""
Apache Iceberg table schema reader.

``IcebergReader(path, schema_name)`` reads the current schema of an Iceberg
table from its table metadata (see :mod:`.metadata`). ``path`` may be:

  * a table directory (containing ``metadata/``),
  * the ``metadata/`` directory itself, or
  * a specific ``*.metadata.json`` file — useful to snapshot an older version.

``list_tables()`` scans a warehouse directory (Hadoop-catalog layout,
``<warehouse>/<namespace...>/<table>/metadata/``) and returns dotted table
identifiers such as ``db.orders``.

Only local paths are supported; catalog lookups (REST, Glue, Hive Metastore)
and object-storage URIs are tracked in KNOWN_ISSUES.md (FEAT-6).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from ..base import SchemaReader
from ...models import TableSchema, ToolResult
from .metadata import is_iceberg_table_dir, read_iceberg_schema


class IcebergReader(SchemaReader):
    """Reads the current schema of a local Iceberg table."""

    def __init__(self, path: str, schema_name: str):
        self.path = path
        self.schema_name = schema_name

    def get_schema(self) -> ToolResult:
        if "://" in self.path:
            return ToolResult(
                success=False,
                error=f"Remote paths are not supported yet: {self.path}",
                next_action_hint="Point at a local copy or mount of the table's metadata directory.",
            )
        path = Path(self.path).expanduser()
        if not path.exists():
            return ToolResult(success=False, error=f"Path not found: {self.path}")
        if path.is_dir() and not is_iceberg_table_dir(path):
            return ToolResult(
                success=False,
                error=f"Not an Iceberg table (no metadata/*.metadata.json): {self.path}",
                next_action_hint="Call list_iceberg_tables() on the warehouse directory to find tables.",
            )

        try:
            columns, partition_keys, clustering_keys, metadata_file = read_iceberg_schema(path)
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read Iceberg metadata from {self.path}: {e}")

        schema = TableSchema(
            table_name=self.schema_name,
            columns=columns,
            partition_keys=partition_keys,
            clustering_keys=clustering_keys,
            source="iceberg",
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        return ToolResult(
            success=True,
            data={"schema": schema.to_dict(), "metadata_file": str(metadata_file)},
            next_action_hint="Now call get_registered_schema() and compare_schemas() to detect drift.",
        )

    def list_tables(self) -> ToolResult:
        """Find Iceberg tables under a warehouse directory."""
        root = Path(self.path).expanduser()
        if "://" in self.path or not root.is_dir():
            return ToolResult(success=False, error=f"Directory not found: {self.path}")

        tables = []
        for dirpath, dirnames, _ in os.walk(root):
            current = Path(dirpath)
            if is_iceberg_table_dir(current) and (current / "metadata").is_dir():
                rel = current.relative_to(root)
                name = ".".join(rel.parts) if rel.parts else current.name
                tables.append({"name": name, "path": str(current)})
                dirnames[:] = []          # don't descend into data/ or metadata/
                continue
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        tables.sort(key=lambda t: t["name"])
        return ToolResult(
            success=True,
            data={"tables": tables, "count": len(tables)},
            next_action_hint="Call get_iceberg_schema() on any table path you want to inspect.",
        )


# ── Module-level wrappers ─────────────────────────────────────────────────

def get_iceberg_schema(path: str, schema_name: str) -> ToolResult:
    return IcebergReader(path, schema_name).get_schema()


def list_iceberg_tables(warehouse: str) -> ToolResult:
    return IcebergReader(warehouse, "").list_tables()
