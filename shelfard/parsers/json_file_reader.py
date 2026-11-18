"""
JSON file schema reader.

Infers a TableSchema from actual values in a JSON file — useful for snapshotting
REST API responses and detecting payload drift over time.

This is a document parser, not a live source introspector, so it does not
implement the SchemaReader ABC.
"""

import json
import os
import re
from datetime import datetime, timezone

from ..models import ColumnSchema, ColumnType, TableSchema, ToolResult
from ..registry import register_schema


_ISO_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}')
_ISO_DATE_RE     = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _infer_column_type(value) -> ColumnType:
    if value is None:
        return ColumnType.UNKNOWN
    if isinstance(value, bool):   # must check before int — bool is int subclass
        return ColumnType.BOOLEAN
    if isinstance(value, int):
        return ColumnType.INTEGER
    if isinstance(value, float):
        return ColumnType.FLOAT
    if isinstance(value, list):
        return ColumnType.ARRAY
    if isinstance(value, str):
        if _ISO_DATETIME_RE.match(value):
            return ColumnType.TIMESTAMP
        if _ISO_DATE_RE.match(value):
            return ColumnType.DATE
        return ColumnType.VARCHAR
    return ColumnType.UNKNOWN


def _json_type(value):
    """The JSON type of a value — recorded as ``physical_type`` (None for null)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _build_column_schema(key: str, value) -> ColumnSchema:
    """Build a ColumnSchema for a single key/value pair.

    Nested dicts become STRUCT columns; lists become ARRAY columns whose
    ``items`` schema is inferred from the first non-null element (UNKNOWN if
    the list is empty or all-null). A null value is UNKNOWN and nullable —
    it only says the value was null in this sample, not what its type is.
    """
    if isinstance(value, dict):
        nested = [_build_column_schema(k, v) for k, v in value.items()]
        return ColumnSchema(name=key, col_type=ColumnType.STRUCT, nullable=False, fields=nested,
                            physical_type="object")
    if isinstance(value, list):
        sample = next((v for v in value if v is not None), None)
        items = _build_column_schema("item", sample)
        items.nullable = any(v is None for v in value)  # an empty list says nothing
        return ColumnSchema(name=key, col_type=ColumnType.ARRAY, nullable=False, items=items,
                            physical_type="array")
    return ColumnSchema(name=key, col_type=_infer_column_type(value), nullable=(value is None),
                        physical_type=_json_type(value))


def _load_json_object(file_path: str) -> ToolResult:
    if not os.path.exists(file_path):
        return ToolResult(
            success=False,
            error=f"File not found: {file_path}",
            next_action_hint="Provide a valid path to a JSON file.",
        )
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return ToolResult(success=False, error=f"Invalid JSON in {file_path}: {e}")

    if isinstance(data, list):
        if not data:
            return ToolResult(success=False, error="JSON array is empty — cannot infer schema.")
        data = data[0]

    if not isinstance(data, dict):
        return ToolResult(
            success=False,
            error=f"Expected a JSON object (or array of objects), got {type(data).__name__}.",
        )

    return ToolResult(success=True, data={"obj": data})


def _build_table_schema(obj: dict, schema_name: str) -> TableSchema:
    columns = [_build_column_schema(key, value) for key, value in obj.items()]
    return TableSchema(
        table_name=schema_name,
        columns=columns,
        source="json_file",
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def infer_schema_from_json_file(file_path: str, schema_name: str) -> ToolResult:
    """Read a JSON file, infer TableSchema, return without registering.

    Returns ToolResult with data={"schema": schema.to_dict()}
    """
    load_result = _load_json_object(file_path)
    if not load_result.success:
        return load_result

    schema = _build_table_schema(load_result.data["obj"], schema_name)
    return ToolResult(success=True, data={"schema": schema.to_dict()})


def read_and_register_json_file(file_path: str, schema_name: str) -> ToolResult:
    """Read a JSON file, infer TableSchema, and register it in the schema registry.

    Returns ToolResult from register_schema() on success.
    """
    load_result = _load_json_object(file_path)
    if not load_result.success:
        return load_result

    schema = _build_table_schema(load_result.data["obj"], schema_name)
    return register_schema(schema_name, schema)
