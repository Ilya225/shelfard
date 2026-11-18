"""
Avro schema reading — stdlib only.

Supports:
  * Avro object container files (``.avro``): the writer schema is stored as
    JSON in the file header (``avro.schema`` metadata key), so only the header
    is parsed — no data blocks are decoded and no Avro library is needed.
  * Avro schema files (``.avsc``): plain JSON.

Mapping rules:
  * ``["null", T]`` unions → T, nullable. Any other field is NOT NULL.
  * Unions with more than one non-null branch → UNKNOWN.
  * ``record`` → STRUCT (recursive); ``enum`` → VARCHAR; ``map`` → JSON;
    ``array`` → ARRAY; ``bytes`` / ``fixed`` → UNKNOWN unless a logical type
    applies.
  * Logical types: ``decimal`` → DECIMAL(precision, scale), ``date`` → DATE,
    ``timestamp-*`` / ``local-timestamp-*`` → TIMESTAMP, ``uuid`` → VARCHAR.
  * Field ``default`` is kept as its JSON text (``null`` → ``"null"``) because
    for Avro "has a default" matters for reader/writer compatibility.
  * Field ``doc`` → description.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from ...models import ColumnSchema, ColumnType

_MAGIC = b"Obj\x01"
_MAX_HEADER_BYTES = 16 * 1024 * 1024   # sanity bound for metadata entries

_PRIMITIVES: dict[str, ColumnType] = {
    "null":    ColumnType.UNKNOWN,
    "boolean": ColumnType.BOOLEAN,
    "int":     ColumnType.INTEGER,
    "long":    ColumnType.BIGINT,
    "float":   ColumnType.FLOAT,
    "double":  ColumnType.FLOAT,
    "bytes":   ColumnType.UNKNOWN,
    "string":  ColumnType.TEXT,
}

_LOGICAL: dict[str, ColumnType] = {
    "date":                   ColumnType.DATE,
    "timestamp-millis":       ColumnType.TIMESTAMP,
    "timestamp-micros":       ColumnType.TIMESTAMP,
    "timestamp-nanos":        ColumnType.TIMESTAMP,
    "local-timestamp-millis": ColumnType.TIMESTAMP,
    "local-timestamp-micros": ColumnType.TIMESTAMP,
    "local-timestamp-nanos":  ColumnType.TIMESTAMP,
    "uuid":                   ColumnType.VARCHAR,
}


# ── Container header parsing ────────────────────────────────────────────────

def _read_long(f: BinaryIO) -> int:
    """Read a zig-zag encoded variable-length long."""
    shift = 0
    acc = 0
    while True:
        b = f.read(1)
        if not b:
            raise ValueError("Unexpected end of file in Avro header")
        byte = b[0]
        acc |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 63:
            raise ValueError("Malformed varint in Avro header")
    return (acc >> 1) ^ -(acc & 1)


def _read_bytes(f: BinaryIO) -> bytes:
    n = _read_long(f)
    if n < 0 or n > _MAX_HEADER_BYTES:
        raise ValueError("Malformed length in Avro header")
    data = f.read(n)
    if len(data) != n:
        raise ValueError("Unexpected end of file in Avro header")
    return data


def read_container_schema(path: Path) -> dict:
    """Return the writer schema (parsed JSON) from an Avro container file header."""
    with open(path, "rb") as f:
        if f.read(4) != _MAGIC:
            raise ValueError(f"{path} is not an Avro object container file (bad magic bytes)")
        meta: dict[str, bytes] = {}
        while True:
            count = _read_long(f)
            if count == 0:
                break
            if count < 0:          # negative count → block byte size follows
                count = -count
                _read_long(f)
            for _ in range(count):
                key = _read_bytes(f).decode("utf-8")
                meta[key] = _read_bytes(f)
    if "avro.schema" not in meta:
        raise ValueError(f"{path} has no 'avro.schema' entry in its header")
    return json.loads(meta["avro.schema"].decode("utf-8"))


def read_avro_schema_json(path: Path) -> dict:
    """Load an Avro schema from either a container file or an ``.avsc`` file."""
    if path.suffix.lower() == ".avsc":
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return read_container_schema(path)


# ── Schema → columns ────────────────────────────────────────────────────────

class _Names:
    """Registry of named types (record / enum / fixed) for resolving references."""

    def __init__(self) -> None:
        self._types: dict[str, dict] = {}
        self.in_progress: set[str] = set()   # records being expanded (recursion guard)

    @staticmethod
    def fullname(name: str, namespace: str | None) -> str:
        if "." in name or not namespace:
            return name
        return f"{namespace}.{name}"

    def add(self, schema: dict, namespace: str | None) -> None:
        full = self.fullname(schema["name"], schema.get("namespace", namespace))
        self._types[full] = schema

    def resolve(self, name: str, namespace: str | None) -> dict | None:
        return self._types.get(self.fullname(name, namespace)) or self._types.get(name)


def _map_type(schema, names: _Names, namespace: str | None) -> tuple[ColumnType, bool, dict]:
    """Return (col_type, nullable, extra ColumnSchema kwargs) for an Avro type."""
    # Union
    if isinstance(schema, list):
        branches = [b for b in schema if b != "null"]
        nullable = len(branches) != len(schema)
        if len(branches) == 1:
            col_type, _, extra = _map_type(branches[0], names, namespace)
            return col_type, nullable, extra
        if not branches:
            return ColumnType.UNKNOWN, True, {}
        return ColumnType.UNKNOWN, nullable, {}

    # Primitive name or reference to a named type
    if isinstance(schema, str):
        if schema in _PRIMITIVES:
            return _PRIMITIVES[schema], schema == "null", {}
        named = names.resolve(schema, namespace)
        if named is None:
            return ColumnType.UNKNOWN, False, {}
        return _map_complex(named, names, namespace, define=False)

    if isinstance(schema, dict):
        return _map_complex(schema, names, namespace, define=True)

    return ColumnType.UNKNOWN, False, {}


def _map_complex(schema: dict, names: _Names, namespace: str | None,
                 define: bool) -> tuple[ColumnType, bool, dict]:
    t = schema.get("type")
    logical = schema.get("logicalType")

    if logical == "decimal" and t in ("bytes", "fixed"):
        if define and t == "fixed":
            names.add(schema, namespace)
        return ColumnType.DECIMAL, False, {
            "precision": schema.get("precision"),
            "scale": schema.get("scale", 0),
        }
    if logical in _LOGICAL:
        return _LOGICAL[logical], False, {}

    if t == "record" or t == "error":
        full = names.fullname(schema["name"], schema.get("namespace", namespace))
        if define:
            names.add(schema, namespace)
        if full in names.in_progress:
            # Recursive type (e.g. a linked-list node): don't expand it again.
            return ColumnType.STRUCT, False, {}
        names.in_progress.add(full)
        try:
            inner_ns = full.rpartition(".")[0] or None
            fields = [_field_to_column(f, names, inner_ns) for f in schema.get("fields", [])]
        finally:
            names.in_progress.discard(full)
        return ColumnType.STRUCT, False, {"fields": fields}
    if t == "enum":
        if define:
            names.add(schema, namespace)
        return ColumnType.VARCHAR, False, {}
    if t == "fixed":
        if define:
            names.add(schema, namespace)
        return ColumnType.UNKNOWN, False, {}
    if t == "array":
        # Element types are parsed so named types defined inside are registered.
        _map_type(schema.get("items"), names, namespace)
        return ColumnType.ARRAY, False, {}
    if t == "map":
        _map_type(schema.get("values"), names, namespace)
        return ColumnType.JSON, False, {}
    # {"type": "long"} style wrapper around a primitive (or a nested type)
    if t is not None:
        return _map_type(t, names, namespace)
    return ColumnType.UNKNOWN, False, {}


def _field_to_column(field: dict, names: _Names, namespace: str | None) -> ColumnSchema:
    col_type, nullable, extra = _map_type(field["type"], names, namespace)
    default = json.dumps(field["default"]) if "default" in field else None
    return ColumnSchema(
        name=field["name"],
        col_type=col_type,
        nullable=nullable,
        default_value=default,
        description=field.get("doc"),
        **extra,
    )


def avro_schema_to_columns(schema) -> list[ColumnSchema]:
    """Convert a top-level Avro record schema into a list of ColumnSchema."""
    if isinstance(schema, dict) and schema.get("type") in ("record", "error"):
        col_type, _, extra = _map_complex(schema, _Names(), None, define=True)
        return extra["fields"]
    raise ValueError("Top-level Avro schema must be a record")
