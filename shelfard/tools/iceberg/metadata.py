"""
Apache Iceberg schema reading — stdlib only.

An Iceberg table's schema, partition spec and sort order live in its table
metadata JSON file (``metadata/vN.metadata.json`` or
``metadata/NNNNN-<uuid>.metadata.json``). This module reads that file
directly, so no catalog connection, JVM or ``pyiceberg`` is needed.

Accepted paths:
  * a ``*.metadata.json`` file (optionally gzip-compressed);
  * a table directory containing ``metadata/``;
  * the ``metadata/`` directory itself.

For directories the current metadata file is chosen from ``version-hint.text``
(Hadoop catalog tables, e.g. written by Spark with ``type=hadoop``) or, if
absent, the metadata file with the highest version number.

Mapping rules:
  * ``required: false`` → nullable; ``doc`` → description;
    ``write-default`` (v3) → default_value.
  * struct → STRUCT; list → ARRAY; map / variant → JSON;
    int → INTEGER; long → BIGINT; float/double → FLOAT; decimal(P,S) → DECIMAL;
    string → TEXT; uuid → VARCHAR; date → DATE; timestamp* → TIMESTAMP;
    time → TIME; binary / fixed[L] / geometry / geography → UNKNOWN.
  * ``physical_type`` is the Iceberg type string (``long``, ``decimal(10,2)``,
    ``timestamptz``) or ``struct`` / ``list`` / ``map``.
  * Constraints are recorded for every field: the schema's
    ``identifier-field-ids`` become ``primary_key`` (position = order in that
    list); ``uuid`` fields get ``format: uuid``.
  * Partition keys come from the default partition spec, rendered as the source
    column for identity transforms and ``transform(column)`` otherwise
    (e.g. ``day(created_at)``, ``bucket[16](id)``). The default sort order
    becomes the clustering keys.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from ...models import ColumnConstraints, ColumnSchema, ColumnType

_DECIMAL_RE = re.compile(r"^decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
_VERSION_RE = re.compile(r"^v?(\d+)[.-]")

_PRIMITIVES: dict[str, ColumnType] = {
    "boolean":        ColumnType.BOOLEAN,
    "int":            ColumnType.INTEGER,
    "long":           ColumnType.BIGINT,
    "float":          ColumnType.FLOAT,
    "double":         ColumnType.FLOAT,
    "date":           ColumnType.DATE,
    "timestamp":      ColumnType.TIMESTAMP,
    "timestamptz":    ColumnType.TIMESTAMP,
    "timestamp_ns":   ColumnType.TIMESTAMP,
    "timestamptz_ns": ColumnType.TIMESTAMP,
    "string":         ColumnType.TEXT,
    "uuid":           ColumnType.VARCHAR,
    "time":           ColumnType.TIME,
    "variant":        ColumnType.JSON,
}


def is_iceberg_table_dir(path: Path) -> bool:
    meta = path / "metadata" if (path / "metadata").is_dir() else path
    return meta.is_dir() and any(_is_metadata_file(p) for p in meta.iterdir())


def _is_metadata_file(p: Path) -> bool:
    return p.is_file() and (p.name.endswith(".metadata.json") or p.name.endswith(".metadata.json.gz"))


def _version_of(p: Path) -> int:
    m = _VERSION_RE.match(p.name)
    return int(m.group(1)) if m else -1


def find_metadata_file(path: Path) -> Path:
    """Resolve *path* (file, table dir, or metadata dir) to the current metadata file."""
    if path.is_file():
        return path
    meta_dir = path / "metadata" if (path / "metadata").is_dir() else path
    if not meta_dir.is_dir():
        raise FileNotFoundError(f"Not an Iceberg table directory: {path}")

    hint = meta_dir / "version-hint.text"
    if hint.is_file():
        version = hint.read_text(encoding="utf-8").strip()
        for candidate in (f"v{version}.metadata.json", f"v{version}.gz.metadata.json"):
            if (meta_dir / candidate).is_file():
                return meta_dir / candidate

    candidates = [p for p in meta_dir.iterdir() if _is_metadata_file(p)]
    if not candidates:
        raise FileNotFoundError(f"No *.metadata.json files found in {meta_dir}")
    return max(candidates, key=lambda p: (_version_of(p), p.stat().st_mtime))


def load_table_metadata(metadata_file: Path) -> dict:
    raw = metadata_file.read_bytes()
    if raw[:2] == b"\x1f\x8b":       # gzip magic (".gz.metadata.json" / ".metadata.json.gz")
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def current_schema(metadata: dict) -> dict:
    """Return the current schema dict, handling format v1 (``schema``) and v2+ (``schemas``)."""
    schemas = metadata.get("schemas")
    if schemas:
        current_id = metadata.get("current-schema-id")
        for s in schemas:
            if s.get("schema-id") == current_id:
                return s
        return schemas[-1]
    if "schema" in metadata:
        return metadata["schema"]
    raise ValueError("Iceberg metadata has no schema")


def _map_type(t, identifiers: list[int]) -> tuple[ColumnType, dict]:
    if isinstance(t, str):
        if t in _PRIMITIVES:
            return _PRIMITIVES[t], {"physical_type": t}
        m = _DECIMAL_RE.match(t)
        if m:
            return ColumnType.DECIMAL, {"precision": int(m.group(1)), "scale": int(m.group(2)),
                                        "physical_type": t}
        return ColumnType.UNKNOWN, {"physical_type": t}   # binary, fixed[L], geometry, ...
    if isinstance(t, dict):
        kind = t.get("type")
        if kind == "struct":
            return ColumnType.STRUCT, {
                "fields": [_field_to_column(f, identifiers) for f in t.get("fields", [])],
                "physical_type": "struct",
            }
        if kind == "list":
            return ColumnType.ARRAY, {"physical_type": "list"}
        if kind == "map":
            return ColumnType.JSON, {"physical_type": "map"}
    return ColumnType.UNKNOWN, {}


def _field_to_column(field: dict, identifiers: list[int]) -> ColumnSchema:
    col_type, extra = _map_type(field.get("type"), identifiers)
    default = field.get("write-default")
    position = identifiers.index(field["id"]) + 1 if field.get("id") in identifiers else None
    return ColumnSchema(
        name=field["name"],
        col_type=col_type,
        nullable=not field.get("required", False),
        default_value=json.dumps(default) if default is not None else None,
        description=field.get("doc"),
        constraints=ColumnConstraints(
            primary_key=position is not None,
            primary_key_position=position,
            format="uuid" if field.get("type") == "uuid" else None,
        ),
        **extra,
    )


def _field_names_by_id(fields: list[dict], prefix: str = "") -> dict[int, str]:
    names: dict[int, str] = {}
    for f in fields:
        qualified = f"{prefix}{f['name']}"
        names[f["id"]] = qualified
        t = f.get("type")
        if isinstance(t, dict) and t.get("type") == "struct":
            names.update(_field_names_by_id(t.get("fields", []), f"{qualified}."))
    return names


def _render_transform(transform: str, column: str) -> str:
    return column if transform == "identity" else f"{transform}({column})"


def _source_name(entry: dict, names: dict[int, str]) -> str:
    source_id = entry.get("source-id")
    if source_id is None and entry.get("source-ids"):      # v3 multi-arg transforms
        return ", ".join(names.get(i, f"#{i}") for i in entry["source-ids"])
    return names.get(source_id, f"#{source_id}")


def _partition_keys(metadata: dict, names: dict[int, str]) -> list[str]:
    fields = None
    specs = metadata.get("partition-specs")
    if specs:
        default_id = metadata.get("default-spec-id")
        spec = next((s for s in specs if s.get("spec-id") == default_id), specs[-1])
        fields = spec.get("fields", [])
    elif "partition-spec" in metadata:                       # format v1
        fields = metadata["partition-spec"]
    return [_render_transform(f.get("transform", "identity"), _source_name(f, names))
            for f in fields or []]


def _clustering_keys(metadata: dict, names: dict[int, str]) -> list[str]:
    orders = metadata.get("sort-orders") or []
    default_id = metadata.get("default-sort-order-id")
    order = next((o for o in orders if o.get("order-id") == default_id), None)
    if not order:
        return []
    return [_render_transform(f.get("transform", "identity"), _source_name(f, names))
            for f in order.get("fields", [])]


def read_iceberg_schema(path: Path) -> tuple[list[ColumnSchema], list[str], list[str], Path]:
    """Return (columns, partition_keys, clustering_keys, metadata_file) for an Iceberg table."""
    metadata_file = find_metadata_file(path)
    metadata = load_table_metadata(metadata_file)
    schema = current_schema(metadata)
    fields = schema.get("fields", [])
    names = _field_names_by_id(fields)
    identifiers = list(schema.get("identifier-field-ids") or [])
    columns = [_field_to_column(f, identifiers) for f in fields]
    return columns, _partition_keys(metadata, names), _clustering_keys(metadata, names), metadata_file
