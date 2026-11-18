"""
Parquet and ORC schema reading via Apache Arrow (``pyarrow``).

Only file footers / metadata are read — no row data is scanned. ``pyarrow``
is an optional dependency (``pip install 'shelfard[files]'``) and is imported
lazily so the rest of Shelfard works without it.

Nullability: Parquet records REQUIRED vs OPTIONAL per field and Arrow
preserves it. ORC has no nullability concept, so every ORC column comes back
nullable (conservative).
"""

from __future__ import annotations

from pathlib import Path

from ...models import ColumnSchema, ColumnType


def _require_pyarrow():
    try:
        import pyarrow  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on environment
        raise ImportError(
            "Reading Parquet/ORC requires pyarrow. Install it with: pip install 'shelfard[files]'"
        ) from e
    import pyarrow as pa
    return pa


def _normalize_type(t) -> tuple[ColumnType, dict]:
    """Map an Arrow DataType to (ColumnType, extra ColumnSchema kwargs)."""
    pa = _require_pyarrow()
    types = pa.types

    if types.is_dictionary(t):
        return _normalize_type(t.value_type)
    if types.is_boolean(t):
        return ColumnType.BOOLEAN, {}
    if types.is_int8(t) or types.is_int16(t) or types.is_int32(t) \
            or types.is_uint8(t) or types.is_uint16(t):
        return ColumnType.INTEGER, {}
    if types.is_int64(t) or types.is_uint32(t) or types.is_uint64(t):
        return ColumnType.BIGINT, {}
    if types.is_floating(t):
        return ColumnType.FLOAT, {}
    if types.is_decimal(t):
        return ColumnType.DECIMAL, {"precision": t.precision, "scale": t.scale}
    if types.is_string(t) or types.is_large_string(t) or _is(types, "is_string_view", t):
        return ColumnType.TEXT, {}
    if types.is_date(t):
        return ColumnType.DATE, {}
    if types.is_timestamp(t):
        return ColumnType.TIMESTAMP, {}
    if types.is_struct(t):
        return ColumnType.STRUCT, {"fields": [_column(t.field(i)) for i in range(t.num_fields)]}
    if types.is_map(t):
        # Before is_list: in some Arrow versions a map is also a list of key/value structs.
        return ColumnType.JSON, {}
    if types.is_list(t) or types.is_large_list(t) or types.is_fixed_size_list(t) \
            or _is(types, "is_list_view", t) or _is(types, "is_large_list_view", t):
        return ColumnType.ARRAY, {}
    # binary, time, duration, interval, null, union, extension types, ...
    return ColumnType.UNKNOWN, {}


def _is(types, predicate: str, t) -> bool:
    """Call a pyarrow.types predicate that may not exist in older pyarrow versions."""
    fn = getattr(types, predicate, None)
    return bool(fn and fn(t))


def _column(field) -> ColumnSchema:
    col_type, extra = _normalize_type(field.type)
    return ColumnSchema(name=field.name, col_type=col_type, nullable=field.nullable, **extra)


def arrow_schema_to_columns(schema) -> list[ColumnSchema]:
    return [_column(f) for f in schema]


def read_arrow_schema(path: Path, file_format: str) -> tuple[list[ColumnSchema], list[str]]:
    """Read the schema of a Parquet/ORC file or a directory of them.

    Directories are read as an Arrow dataset with Hive-style partition
    discovery (``key=value`` sub-directories); discovered partition columns are
    appended to the schema and returned as partition keys. Files whose names
    start with ``_`` or ``.`` (``_SUCCESS``, ``.crc``) are ignored.

    Returns (columns, partition_keys).
    """
    _require_pyarrow()

    if path.is_file():
        if file_format == "parquet":
            import pyarrow.parquet as pq
            schema = pq.read_schema(path)
        else:
            import pyarrow.orc as orc
            schema = orc.ORCFile(str(path)).schema
        return arrow_schema_to_columns(schema), []

    import pyarrow.dataset as ds

    dataset = ds.dataset(str(path), format=file_format, partitioning="hive")
    if not dataset.files:
        raise ValueError(f"No {file_format} files found under {path}")
    # Partition columns are the ones in the dataset schema that no data file stores.
    physical = next(iter(dataset.get_fragments())).physical_schema.names
    partition_keys = [n for n in dataset.schema.names if n not in physical]
    return arrow_schema_to_columns(dataset.schema), partition_keys
