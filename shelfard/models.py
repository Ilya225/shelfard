"""
Core data models for the schema drift agent.
Everything is typed, serializable, and carries enough context
for the LLM to reason about it without needing to call more tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
import json


# ─────────────────────────────────────────────
# Schema primitives
# ─────────────────────────────────────────────

class ColumnType(str, Enum):
    """Normalized type system — maps vendor types to these buckets."""
    INTEGER   = "integer"
    BIGINT    = "bigint"
    FLOAT     = "float"
    DECIMAL   = "decimal"
    VARCHAR   = "varchar"
    TEXT      = "text"
    BOOLEAN   = "boolean"
    DATE      = "date"
    TIMESTAMP = "timestamp"
    TIME      = "time"                     # time of day, no date
    JSON      = "json"
    ARRAY     = "array"
    STRUCT    = "struct"
    UNKNOWN   = "unknown"


@dataclass
class ColumnConstraints:
    """Declared constraints on a column — mirrors ODCS property flags and
    ``logicalTypeOptions``. ``max_length`` stays on ColumnSchema (it is part of
    the type).

    Every attribute is tri-state: ``None`` means unknown (not observable by the
    reader / not stated by the contract) and is never compared; ``False`` or a
    value is an observed fact. E.g. the Postgres reader sets ``primary_key`` to
    True/False for every column, while a pattern is always ``None`` for it.

    ``minimum``/``maximum``/``exclusive_*`` are numbers for numeric columns and
    ISO strings for date/time columns. ``format`` is a semantic string format
    (``uuid``, ``email``, ``uri``, ...).
    """
    primary_key: Optional[bool] = None
    primary_key_position: Optional[int] = None   # 1-based position in a composite key
    unique: Optional[bool] = None
    format: Optional[str] = None
    pattern: Optional[str] = None
    minimum: Optional[object] = None
    maximum: Optional[object] = None
    exclusive_minimum: Optional[object] = None
    exclusive_maximum: Optional[object] = None
    multiple_of: Optional[float] = None
    min_length: Optional[int] = None
    min_items: Optional[int] = None
    max_items: Optional[int] = None
    unique_items: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def known(self) -> dict:
        """Only the attributes that have a value (True/False included)."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["ColumnConstraints"]:
        """Build from a dict; returns None when no attribute has a value."""
        if not d:
            return None
        known = cls.__dataclass_fields__
        c = cls(**{k: v for k, v in d.items() if k in known})
        return c if c.known() else None


def _known(c: Optional[ColumnConstraints]) -> dict:
    return c.known() if c is not None else {}


@dataclass
class ColumnSchema:
    name: str
    col_type: ColumnType
    nullable: bool = True
    max_length: Optional[int] = None       # for varchar
    precision: Optional[int] = None        # for decimal
    scale: Optional[int] = None            # for decimal
    default_value: Optional[str] = None
    description: Optional[str] = None
    fields: Optional[list[ColumnSchema]] = field(default=None)  # sub-fields for STRUCT columns
    items: Optional[ColumnSchema] = None   # element schema for ARRAY columns (None = not recorded)
    physical_type: Optional[str] = None    # raw vendor type, e.g. "character varying", "int64" (None = not recorded)
    constraints: Optional[ColumnConstraints] = None  # None = nothing known (see ColumnConstraints)

    def to_dict(self) -> dict:
        return asdict(self)

    def __eq__(self, other):
        if not isinstance(other, ColumnSchema):
            return False
        return (
            self.col_type == other.col_type and
            self.nullable == other.nullable and
            self.max_length == other.max_length and
            self.precision == other.precision and
            self.scale == other.scale and
            self.fields == other.fields and
            self.items == other.items and
            self.physical_type == other.physical_type and
            _known(self.constraints) == _known(other.constraints)
        )

    @classmethod
    def from_dict(cls, col: dict) -> ColumnSchema:
        """Recursively reconstruct a ColumnSchema (and its nested fields) from a dict."""
        col_type_str = col.get("col_type", "unknown")
        try:
            col_type = ColumnType(col_type_str.lower())
        except ValueError:
            col_type = ColumnType.UNKNOWN

        nested = None
        if col_type == ColumnType.STRUCT and col.get("fields"):
            nested = [cls.from_dict(f) for f in col["fields"]]
        items = None
        if col_type == ColumnType.ARRAY and col.get("items"):
            items = cls.from_dict(col["items"])

        return cls(
            name=col["name"],
            col_type=col_type,
            nullable=col.get("nullable", True),
            max_length=col.get("max_length"),
            precision=col.get("precision"),
            scale=col.get("scale"),
            default_value=col.get("default_value"),
            description=col.get("description"),
            fields=nested,
            items=items,
            physical_type=col.get("physical_type"),
            constraints=ColumnConstraints.from_dict(col.get("constraints")),
        )


@dataclass
class TableSchema:
    table_name: str
    columns: list[ColumnSchema]
    partition_keys: list[str] = field(default_factory=list)
    clustering_keys: list[str] = field(default_factory=list)
    source: str = "unknown"          # "sqlite", "snowflake", "bigquery", etc.
    captured_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "TableSchema":
        return cls(
            table_name=d["table_name"],
            columns=[ColumnSchema.from_dict(c) for c in d.get("columns", [])],
            partition_keys=list(d.get("partition_keys") or []),
            clustering_keys=list(d.get("clustering_keys") or []),
            source=d.get("source", "unknown"),
            captured_at=d.get("captured_at"),
        )

    def column_map(self) -> dict[str, ColumnSchema]:
        return {col.name: col for col in self.columns}

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


@dataclass
class ConsumerSubscription:
    """
    Records that a named consumer depends on (a projection of) a source schema.

    subscribed_columns=None means the consumer subscribed to the full schema.
    subscribed_columns=[...] means the consumer only reads those columns (projection).
    schema holds the TableSchema snapshot at subscription time (projected or full).
    """
    consumer_name: str
    source_table: str
    subscribed_columns: Optional[list[str]]   # None = full subscription
    schema: TableSchema                        # projected or full snapshot
    subscribed_at: str                         # ISO timestamp
    source_schema_version: str                 # captured_at of source at subscription time

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ConsumerSubscription:
        schema_d = d["schema"]
        schema = TableSchema(
            table_name=schema_d["table_name"],
            columns=[ColumnSchema.from_dict(c) for c in schema_d["columns"]],
            partition_keys=schema_d.get("partition_keys", []),
            clustering_keys=schema_d.get("clustering_keys", []),
            source=schema_d.get("source", "registry"),
            captured_at=schema_d.get("captured_at"),
        )
        return cls(
            consumer_name=d["consumer_name"],
            source_table=d["source_table"],
            subscribed_columns=d.get("subscribed_columns"),
            schema=schema,
            subscribed_at=d["subscribed_at"],
            source_schema_version=d["source_schema_version"],
        )


# ─────────────────────────────────────────────
# Diff models
# ─────────────────────────────────────────────

class ChangeSeverity(str, Enum):
    SAFE     = "SAFE"      # additive, non-breaking
    WARNING  = "WARNING"   # potentially breaking, needs review
    BREAKING = "BREAKING"  # will break downstream consumers


class ChangeType(str, Enum):
    COLUMN_ADDED        = "column_added"
    COLUMN_REMOVED      = "column_removed"
    TYPE_WIDENED        = "type_widened"       # e.g. int → bigint, varchar(50) → varchar(200)
    TYPE_CHANGED        = "type_changed"       # e.g. int → varchar — almost always breaking
    NULLABILITY_RELAXED = "nullability_relaxed" # NOT NULL → NULL — usually safe
    NULLABILITY_TIGHTENED = "nullability_tightened" # NULL → NOT NULL — breaking if data exists
    DEFAULT_CHANGED     = "default_changed"
    COLUMN_REORDERED    = "column_reordered"   # safe for named access, breaking for positional
    TYPE_RESOLVED       = "type_resolved"      # UNKNOWN → concrete type (e.g. a field first seen as null)
    PARTITION_CHANGED   = "partition_changed"  # partition keys / transforms changed
    CLUSTERING_CHANGED  = "clustering_changed" # clustering / sort keys changed
    PHYSICAL_TYPE_CHANGED = "physical_type_changed"  # same normalized type, different vendor type
    CONSTRAINT_CHANGED  = "constraint_changed"  # primary key / unique / range / pattern / ... changed


@dataclass
class ColumnChange:
    change_type: ChangeType
    column_name: str
    old_value: Optional[dict] = None    # serialized ColumnSchema or specific field
    new_value: Optional[dict] = None
    severity: ChangeSeverity = ChangeSeverity.SAFE
    reasoning: str = ""                 # human-readable explanation of severity decision

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SchemaDiff:
    table_name: str
    old_schema_version: Optional[str]
    new_schema_version: Optional[str]
    changes: list[ColumnChange]
    overall_severity: ChangeSeverity = ChangeSeverity.SAFE
    summary: str = ""
    # Present when every change is SAFE: the explanation of each change and the
    # schema to register as the next baseline version (see schema_comparison).
    suggested_update: Optional[dict] = None

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    @property
    def breaking_changes(self) -> list[ColumnChange]:
        return [c for c in self.changes if c.severity == ChangeSeverity.BREAKING]

    @property
    def safe_changes(self) -> list[ColumnChange]:
        return [c for c in self.changes if c.severity == ChangeSeverity.SAFE]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


# ─────────────────────────────────────────────
# Checker config
# ─────────────────────────────────────────────

@dataclass
class RestCheckerConfig:
    """Stored configuration for a REST-based drift check."""
    schema_name: str
    url: str
    headers: list[dict[str, str]]   # e.g. [{"Authorization": "$BEARER_TOKEN"}]
    env: list[str]                  # e.g. ["BEARER_TOKEN"] — names only, never values
    checker_type: str = "rest"
    registered_at: Optional[str] = None
    registered_via: str = "cli"     # "cli" (trusted) or "mcp" (LLM-registered, guarded at run time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "RestCheckerConfig":
        return cls(
            schema_name=d["schema_name"],
            url=d["url"],
            headers=d.get("headers", []),
            env=d.get("env", []),
            checker_type=d.get("checker_type", "rest"),
            registered_at=d.get("registered_at"),
            registered_via=d.get("registered_via", "cli"),
        )


@dataclass
class PostgresCheckerConfig:
    """Stored configuration for a PostgreSQL drift check.

    Two modes:
      - table mode: set ``table`` to introspect a single table or view
      - query mode: set ``query`` to infer schema from a custom SQL query

    ``dsn`` may contain ``$VAR`` placeholders (e.g. ``postgresql://user:$PG_PASS@host/db``).
    ``env`` lists the env var *names* whose values are substituted at run time.
    Values are never stored.
    """
    schema_name: str
    dsn: str                        # e.g. "postgresql://user:$PG_PASS@host/db"
    env: list[str]                  # required env var names (never values)
    table: Optional[str] = None     # table/view name  (table mode)
    query: Optional[str] = None     # SQL query        (query mode)
    db_schema: str = "public"       # PostgreSQL schema namespace
    sample_size: int = 100          # rows to sample for nullability inference
    checker_type: str = "postgres"
    registered_at: Optional[str] = None
    registered_via: str = "cli"     # "cli" (trusted) or "mcp" (LLM-registered, guarded at run time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "PostgresCheckerConfig":
        return cls(
            schema_name=d["schema_name"],
            dsn=d["dsn"],
            env=d.get("env", []),
            table=d.get("table"),
            query=d.get("query"),
            db_schema=d.get("db_schema", "public"),
            sample_size=d.get("sample_size", 100),
            checker_type=d.get("checker_type", "postgres"),
            registered_at=d.get("registered_at"),
            registered_via=d.get("registered_via", "cli"),
        )


@dataclass
class FileCheckerConfig:
    """Stored configuration for a file-format drift check (Parquet / ORC / Avro).

    ``path`` is a local file or directory (e.g. a partitioned dataset). It may
    contain ``{{var}}`` template variables and ``$VAR`` placeholders, resolved
    at run time. ``file_format`` is one of ``auto``, ``parquet``, ``orc``, ``avro``.
    """
    schema_name: str
    path: str
    env: list[str] = field(default_factory=list)
    file_format: str = "auto"
    checker_type: str = "file"
    registered_at: Optional[str] = None
    registered_via: str = "cli"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "FileCheckerConfig":
        return cls(
            schema_name=d["schema_name"],
            path=d["path"],
            env=d.get("env", []),
            file_format=d.get("file_format", "auto"),
            checker_type=d.get("checker_type", "file"),
            registered_at=d.get("registered_at"),
            registered_via=d.get("registered_via", "cli"),
        )


@dataclass
class IcebergCheckerConfig:
    """Stored configuration for an Iceberg table drift check.

    ``path`` is a local table directory, its ``metadata/`` directory, or a
    ``*.metadata.json`` file. It may contain ``{{var}}`` template variables and
    ``$VAR`` placeholders, resolved at run time.
    """
    schema_name: str
    path: str
    env: list[str] = field(default_factory=list)
    checker_type: str = "iceberg"
    registered_at: Optional[str] = None
    registered_via: str = "cli"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "IcebergCheckerConfig":
        return cls(
            schema_name=d["schema_name"],
            path=d["path"],
            env=d.get("env", []),
            checker_type=d.get("checker_type", "iceberg"),
            registered_at=d.get("registered_at"),
            registered_via=d.get("registered_via", "cli"),
        )


# ─────────────────────────────────────────────
# Tool result wrapper
# ─────────────────────────────────────────────

@dataclass
class ToolResult:
    """
    Wrapper returned by every tool. The agent sees this — it needs to be
    self-describing enough that the LLM knows what happened and what to do next.
    """
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    next_action_hint: Optional[str] = None   # optional nudge for the agent

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)
