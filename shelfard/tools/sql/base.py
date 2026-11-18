"""
Shared SQL introspection utilities for DB-API 2.0 compliant databases.

These helpers are database-agnostic and can be reused by PostgreSQL, MySQL,
and any other SQL backend that supports:
  - information_schema.columns  (for table/view introspection)
  - cursor.description          (for query-mode schema inference)
  - standard LIMIT clause       (for nullability sampling)
"""

from typing import Callable, Optional

from ...models import ColumnConstraints, ColumnSchema, ColumnType

StringFormatFn = Callable[[Optional[str]], Optional[str]]


def sample_query(cursor, query: str, sample_size: int = 100):
    """
    Execute *query* wrapped in a LIMIT subquery and return (description, rows).

    Uses the subquery pattern ``SELECT * FROM (...) AS _q LIMIT N`` so the
    original query is never modified.  Returns the cursor.description (column
    metadata) and all fetched rows.
    """
    wrapped = f"SELECT * FROM ({query}) AS _shelfard_q LIMIT {sample_size}"
    cursor.execute(wrapped)
    rows = cursor.fetchall()
    return cursor.description, rows


def build_columns_from_query_result(
    desc,
    rows: list,
    type_code_map: dict,
    *,
    physical_types: Optional[dict] = None,
    string_format: Optional[StringFormatFn] = None,
) -> list[ColumnSchema]:
    """
    Build a ColumnSchema list from cursor.description and sampled rows.

    Nullability contract:
      - If *rows* is non-empty and every value in a column is non-NULL  →  NOT NULL
      - If *rows* is empty, or any value is NULL                         →  nullable

    type_code_map: dict[type_code (int) -> ColumnType]
      Callers pre-build this by resolving the vendor-specific type codes
      (e.g. PostgreSQL OIDs) to ColumnType values.
    physical_types: optional dict[type_code -> raw type name] stored as
      ``physical_type``.
    string_format: optional fn(raw type) -> string format (e.g. "uuid"). Query
      mode records no key constraints, so ``constraints`` is only set for
      columns whose raw type implies a format.
    """
    physical_types = physical_types or {}
    columns = []
    for i, col_desc in enumerate(desc):
        col_type = type_code_map.get(col_desc.type_code, ColumnType.UNKNOWN)

        # Nullability inferred from sampled data
        if rows:
            is_nullable = any(row[i] is None for row in rows)
        else:
            is_nullable = True  # conservative: no data → assume nullable

        max_length = None
        if col_type == ColumnType.VARCHAR:
            # internal_size is the character length for char-family columns;
            # -1 means unlimited (e.g. text), 0 means unknown
            size = col_desc.internal_size
            if size and size > 0:
                max_length = size

        precision = col_desc.precision if col_type == ColumnType.DECIMAL else None
        scale = (
            int(col_desc.scale)
            if col_type == ColumnType.DECIMAL and col_desc.scale is not None
            else None
        )

        raw = physical_types.get(col_desc.type_code)
        fmt = string_format(raw) if string_format else None
        columns.append(ColumnSchema(
            name=col_desc.name,
            col_type=col_type,
            nullable=is_nullable,
            max_length=max_length,
            precision=precision,
            scale=scale,
            physical_type=raw or None,
            constraints=ColumnConstraints(format=fmt) if fmt else None,
        ))
    return columns


def introspect_table_via_information_schema(
    cursor,
    table_name: str,
    normalize_type: Callable[[str], ColumnType],
    *,
    db_schema: str = "public",
    string_format: Optional[StringFormatFn] = None,
) -> list[ColumnSchema]:
    """
    Introspect a table or view using information_schema.columns.

    Compatible with PostgreSQL and MySQL — both expose the same
    information_schema interface.  Returns columns in ordinal_position order.

    Args:
        cursor:         DB-API 2.0 cursor
        table_name:     table or view name
        normalize_type: vendor-specific fn(raw_type_str) -> ColumnType
        db_schema:      database schema name (default "public" for PostgreSQL)
        string_format:  optional fn(raw type) -> string format (e.g. "uuid")

    ``physical_type`` is ``data_type`` (``udt_name`` for ARRAY / USER-DEFINED).
    Primary-key and single-column UNIQUE constraints are read from
    ``information_schema.table_constraints``; if that query fails the columns
    are returned with ``constraints=None`` (not recorded).
    """
    cursor.execute(
        """
        SELECT column_name, data_type, is_nullable,
               character_maximum_length, numeric_precision, numeric_scale,
               column_default, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (db_schema, table_name),
    )
    rows = cursor.fetchall()

    keys = _key_constraints(cursor, table_name, db_schema)

    columns = []
    for (col_name, raw_type, is_nullable_str,
         char_max_len, num_precision, num_scale, col_default, udt_name) in rows:

        physical = udt_name if raw_type in ("ARRAY", "USER-DEFINED") and udt_name else raw_type
        col_type = normalize_type(physical if raw_type == "USER-DEFINED" else raw_type)
        nullable = is_nullable_str.upper() == "YES"

        max_length = char_max_len if col_type == ColumnType.VARCHAR else None
        precision = (
            int(num_precision)
            if col_type == ColumnType.DECIMAL and num_precision is not None
            else None
        )
        scale = (
            int(num_scale)
            if col_type == ColumnType.DECIMAL and num_scale is not None
            else None
        )

        columns.append(ColumnSchema(
            name=col_name,
            col_type=col_type,
            nullable=nullable,
            max_length=max_length,
            precision=precision,
            scale=scale,
            default_value=str(col_default) if col_default is not None else None,
            physical_type=physical,
            constraints=_column_constraints(keys, col_name, physical, string_format),
        ))
    return columns


def _key_constraints(cursor, table_name: str, db_schema: str) -> Optional[dict]:
    """{column: {"pk_position": int?, "unique": bool}} or None if unavailable."""
    try:
        cursor.execute(
            """
            SELECT kcu.column_name, tc.constraint_type, tc.constraint_name,
                   kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            WHERE tc.table_schema = %s AND tc.table_name = %s
              AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
            ORDER BY tc.constraint_name, kcu.ordinal_position
            """,
            (db_schema, table_name),
        )
        rows = cursor.fetchall()
    except Exception:
        return None
    if not isinstance(rows, (list, tuple)):
        return None

    size: dict[str, int] = {}
    for _, _, constraint, _ in rows:
        size[constraint] = size.get(constraint, 0) + 1
    keys: dict[str, dict] = {}
    for column, kind, constraint, position in rows:
        entry = keys.setdefault(column, {"pk_position": None, "unique": False})
        if kind == "PRIMARY KEY":
            entry["pk_position"] = int(position)
        elif size[constraint] == 1:     # a composite UNIQUE doesn't make one column unique
            entry["unique"] = True
    return keys


def _column_constraints(keys: Optional[dict], column: str, raw_type: Optional[str],
                        string_format: Optional[StringFormatFn]) -> Optional[ColumnConstraints]:
    fmt = string_format(raw_type) if string_format else None
    if keys is None:
        return ColumnConstraints(format=fmt) if fmt else None
    entry = keys.get(column, {})
    pk_position = entry.get("pk_position")
    return ColumnConstraints(
        primary_key=pk_position is not None,
        primary_key_position=pk_position,
        unique=bool(entry.get("unique")),
        format=fmt,
    )
