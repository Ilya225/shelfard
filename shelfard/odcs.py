"""
ODCS (Open Data Contract Standard) v3.1.0 adapter.

Shelfard keeps its own ``TableSchema`` model internally — it records what a
source *looks like*, while an ODCS document is a *contract* about what it
should look like. This module converts between the two:

  * ``to_odcs(tables, version=...)``  → an ODCS document (dict)
  * ``from_odcs(doc)``                → (list[TableSchema], ignored fields)

Round trip is lossless for Shelfard-generated documents. Everything ODCS can
express natively is written natively (``logicalType``, ``required``,
``logicalTypeOptions``, ``primaryKey``, ``unique``, ``physicalType``,
``partitioned``, nested ``properties`` / ``items``); the rest goes into
``customProperties`` entries named ``shelfard.*``:

  * ``shelfard.colType``      exact type when ``logicalType`` alone is ambiguous
                              (TEXT vs VARCHAR, DECIMAL vs FLOAT, STRUCT without fields)
  * ``shelfard.precision`` / ``shelfard.scale`` / ``shelfard.maxLength``
  * ``shelfard.defaultValue``
  * ``shelfard.constraints``  constraint attributes ODCS can't hold for the type
  * object level: ``shelfard.source``, ``shelfard.capturedAt``,
    ``shelfard.partitionKeys`` (when transforms are involved), ``shelfard.clusteringKeys``
  * document level: ``shelfard.generator``

Documents written by hand or by other tools ("foreign") are read from their
native fields only. Their ``physicalType`` strings are free-form (``VARCHAR(2)``)
and are not compared with Shelfard's raw types; constraints are only known
where the contract states them.

YAML needs PyYAML and validation needs jsonschema: ``pip install 'shelfard[odcs]'``.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional

from .models import ChangeSeverity, ColumnConstraints, ColumnSchema, ColumnType, TableSchema, ToolResult

API_VERSION = "v3.1.0"
GENERATOR = "shelfard"
SHELFARD_NS = uuid.UUID("6f1d3c2e-5b7a-4e0f-9c8d-2a1b3c4d5e6f")
_P = "shelfard."

_LOGICAL: dict[ColumnType, str] = {
    ColumnType.INTEGER: "integer", ColumnType.BIGINT: "integer",
    ColumnType.FLOAT: "number", ColumnType.DECIMAL: "number",
    ColumnType.VARCHAR: "string", ColumnType.TEXT: "string",
    ColumnType.BOOLEAN: "boolean", ColumnType.DATE: "date",
    ColumnType.TIMESTAMP: "timestamp", ColumnType.TIME: "time",
    ColumnType.JSON: "object", ColumnType.STRUCT: "object",
    ColumnType.ARRAY: "array",
}

# logicalTypeOptions allowed per logicalType (the JSON Schema forbids others).
_TEMPORAL = {"format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
_NUMERIC = {"multipleOf", "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum", "format"}
_ALLOWED_OPTIONS: dict[str, set[str]] = {
    "string": {"minLength", "maxLength", "pattern", "format"},
    "date": _TEMPORAL,
    "timestamp": _TEMPORAL | {"timezone", "defaultTimezone"},
    "time": _TEMPORAL | {"timezone", "defaultTimezone"},
    "integer": _NUMERIC,
    "number": _NUMERIC,
    "object": {"maxProperties", "minProperties", "required"},
    "array": {"maxItems", "minItems", "uniqueItems"},
    "boolean": set(),
}

# ColumnConstraints attribute → logicalTypeOptions key. ``format`` is only a
# string format for ``string``; for numbers it is the bit width, for dates a pattern.
_CONSTRAINT_OPTS = {
    "pattern": "pattern", "minimum": "minimum", "maximum": "maximum",
    "exclusive_minimum": "exclusiveMinimum", "exclusive_maximum": "exclusiveMaximum",
    "multiple_of": "multipleOf", "min_length": "minLength",
    "min_items": "minItems", "max_items": "maxItems", "unique_items": "uniqueItems",
    "format": "format",
}
_WIDE_INTS = {"i64", "i128", "u32", "u64", "u128"}
_DECIMAL_RE = re.compile(r"^\s*(?:decimal|numeric|number)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", re.I)
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")

_OBJECT_PHYSICAL = {
    "sqlite": "table", "postgresql": "table", "iceberg": "table",
    "parquet": "file", "orc": "file", "avro": "file",
}

_HEADER_KEYS = {"apiVersion", "kind", "id", "name", "version", "status", "schema", "customProperties"}
_OBJECT_KEYS = {"name", "logicalType", "physicalType", "properties", "customProperties"}
_PROPERTY_KEYS = {
    "name", "logicalType", "logicalTypeOptions", "physicalType", "description", "required",
    "primaryKey", "primaryKeyPosition", "unique", "partitioned", "partitionKeyPosition",
    "properties", "items", "customProperties",
}


# ─────────────────────────────────────────────
# Shelfard → ODCS
# ─────────────────────────────────────────────

def contract_id(name: str) -> str:
    """Stable contract id for a schema name (same across versions)."""
    return str(uuid.uuid5(SHELFARD_NS, name))


def _custom(entries: dict) -> list[dict]:
    return [{"property": f"{_P}{k}", "value": v} for k, v in entries.items() if v is not None]


def _coltype_from_logical(logical: Optional[str], options: dict,
                          has_properties: bool = False) -> ColumnType:
    """Default ColumnType for an ODCS logical type (without Shelfard hints)."""
    if logical == "integer":
        return ColumnType.BIGINT if options.get("format") in _WIDE_INTS else ColumnType.INTEGER
    if logical == "number":
        return ColumnType.FLOAT
    if logical == "string":
        return ColumnType.VARCHAR
    if logical == "object":
        return ColumnType.STRUCT if has_properties else ColumnType.JSON
    return {
        "boolean": ColumnType.BOOLEAN, "date": ColumnType.DATE,
        "timestamp": ColumnType.TIMESTAMP, "time": ColumnType.TIME, "array": ColumnType.ARRAY,
    }.get(logical, ColumnType.UNKNOWN)


def _property(col: ColumnSchema, partition_pos: Optional[dict] = None) -> dict:
    prop: dict = {"name": col.name}
    logical = _LOGICAL.get(col.col_type)
    if logical:
        prop["logicalType"] = logical
    if col.physical_type:
        prop["physicalType"] = col.physical_type
    if col.description:
        prop["description"] = col.description
    prop["required"] = not col.nullable

    options: dict = {}
    custom: dict = {}
    allowed = _ALLOWED_OPTIONS.get(logical or "", set())

    if col.col_type == ColumnType.INTEGER:
        options["format"] = "i32"
    elif col.col_type == ColumnType.BIGINT:
        options["format"] = "i64"
    if col.max_length is not None:
        if logical == "string":
            options["maxLength"] = col.max_length
        else:
            custom["maxLength"] = col.max_length
    custom["precision"] = col.precision
    custom["scale"] = col.scale
    custom["defaultValue"] = col.default_value

    c = col.constraints
    if c is not None:
        if c.primary_key is not None:
            prop["primaryKey"] = c.primary_key
        if c.primary_key_position is not None:
            prop["primaryKeyPosition"] = c.primary_key_position
        if c.unique is not None:
            prop["unique"] = c.unique
        leftover = {}
        for attr, key in _CONSTRAINT_OPTS.items():
            value = getattr(c, attr)
            if value is None:
                continue
            fits = key in allowed and (attr != "format" or logical == "string")
            if fits:
                options[key] = value
            else:
                leftover[attr] = value
        if leftover:
            custom["constraints"] = leftover

    if options:
        prop["logicalTypeOptions"] = options

    if partition_pos and col.name in partition_pos:
        prop["partitioned"] = True
        prop["partitionKeyPosition"] = partition_pos[col.name]

    if col.col_type == ColumnType.STRUCT and col.fields is not None:
        prop["properties"] = [_property(f) for f in col.fields]
    if col.col_type == ColumnType.ARRAY and col.items is not None:
        prop["items"] = _property(col.items)

    if _coltype_from_logical(logical, options, "properties" in prop) != col.col_type:
        custom["colType"] = col.col_type.value
    if entries := _custom(custom):
        prop["customProperties"] = entries
    return prop


def _object(table: TableSchema) -> dict:
    top_level = {c.name for c in table.columns}
    plain = [k for k in table.partition_keys if k in top_level]
    partition_pos = {k: i + 1 for i, k in enumerate(table.partition_keys) if k in top_level}
    obj: dict = {"name": table.table_name, "logicalType": "object"}
    if table.source in _OBJECT_PHYSICAL:
        obj["physicalType"] = _OBJECT_PHYSICAL[table.source]
    obj["properties"] = [_property(c, partition_pos) for c in table.columns]
    custom = {"source": table.source, "capturedAt": table.captured_at}
    if len(plain) != len(table.partition_keys):        # transforms / nested keys
        custom["partitionKeys"] = list(table.partition_keys)
    if table.clustering_keys:
        custom["clusteringKeys"] = list(table.clustering_keys)
    obj["customProperties"] = _custom(custom)
    return obj


def to_odcs(tables: list[TableSchema], *, version: str = "1.0.0", status: str = "active",
            name: Optional[str] = None) -> dict:
    """Build an ODCS v3.1.0 document holding one schema object per table."""
    if not tables:
        raise ValueError("to_odcs needs at least one table")
    contract_name = name or ",".join(t.table_name for t in tables)
    return {
        "apiVersion": API_VERSION,
        "kind": "DataContract",
        "id": contract_id(contract_name),
        "name": contract_name,
        "version": version,
        "status": status,
        "schema": [_object(t) for t in tables],
        "customProperties": _custom({"generator": GENERATOR}),
    }


# ─────────────────────────────────────────────
# ODCS → Shelfard
# ─────────────────────────────────────────────

def is_shelfard_generated(doc: dict) -> bool:
    return any(cp.get("property") == f"{_P}generator"
               for cp in doc.get("customProperties") or [] if isinstance(cp, dict))


def _shelfard_custom(element: dict) -> tuple[dict, list[str]]:
    """(shelfard.* values by short key, names of other custom properties)."""
    ours, others = {}, []
    for cp in element.get("customProperties") or []:
        name = cp.get("property", "") if isinstance(cp, dict) else ""
        if name.startswith(_P):
            ours[name[len(_P):]] = cp.get("value")
        else:
            others.append(name or "?")
    return ours, others


class _Ignored:
    """Collects ODCS fields Shelfard can't represent: {field: [paths]}."""

    def __init__(self) -> None:
        self.fields: dict[str, list[str]] = {}

    def add(self, field: str, path: str) -> None:
        self.fields.setdefault(field, []).append(path)


def _column(prop: dict, path: str, generated: bool, ignored: _Ignored) -> ColumnSchema:
    name = prop.get("name") or "item"
    logical = prop.get("logicalType")
    options = prop.get("logicalTypeOptions") or {}
    custom, other_custom = _shelfard_custom(prop)
    for key in prop:
        if key not in _PROPERTY_KEYS or (key == "physicalType" and not generated):
            ignored.add(key, path)
    for other in other_custom:
        ignored.add(f"customProperties.{other}", path)

    has_props = "properties" in prop
    try:
        col_type = (ColumnType(custom["colType"]) if "colType" in custom
                    else _coltype_from_logical(logical, options, has_props))
    except ValueError:
        col_type = ColumnType.UNKNOWN

    precision, scale = custom.get("precision"), custom.get("scale")
    physical = prop.get("physicalType")
    if not generated and logical == "number" and physical and "colType" not in custom:
        m = _DECIMAL_RE.match(str(physical))
        if m:   # foreign contract: DECIMAL(10,2) in physicalType
            col_type = ColumnType.DECIMAL
            precision, scale = int(m.group(1)), int(m.group(2) or 0)

    max_length = options.get("maxLength") if logical == "string" else custom.get("maxLength")

    allowed = _ALLOWED_OPTIONS.get(logical or "", set())
    attrs: dict = dict(custom.get("constraints") or {})
    for attr, key in _CONSTRAINT_OPTS.items():
        if key in options and key in allowed and (attr != "format" or logical == "string"):
            attrs[attr] = options[key]
    for key in options:
        managed = key == "maxLength" or (key == "format" and logical in ("integer", "number"))
        if not managed and key not in _CONSTRAINT_OPTS.values():
            ignored.add(f"logicalTypeOptions.{key}", path)
        elif key == "format" and logical in ("date", "timestamp", "time"):
            ignored.add("logicalTypeOptions.format", path)
    if "primaryKey" in prop:
        attrs["primary_key"] = bool(prop["primaryKey"])
    position = prop.get("primaryKeyPosition")
    if isinstance(position, int) and position > 0:
        attrs["primary_key_position"] = position
    if "unique" in prop:
        attrs["unique"] = bool(prop["unique"])

    fields = None
    if has_props:
        fields = [_column(p, f"{path}.{p.get('name')}", generated, ignored)
                  for p in prop.get("properties") or []]
    items = None
    if isinstance(prop.get("items"), dict):
        items = _column(prop["items"], f"{path}[]", generated, ignored)

    return ColumnSchema(
        name=name,
        col_type=col_type,
        nullable=not prop.get("required", False),
        max_length=max_length,
        precision=precision,
        scale=scale,
        default_value=custom.get("defaultValue"),
        description=prop.get("description"),
        fields=fields,
        items=items,
        physical_type=physical if generated else None,
        constraints=ColumnConstraints.from_dict(attrs),
    )


def _partition_keys(obj: dict, custom: dict) -> list[str]:
    if isinstance(custom.get("partitionKeys"), list):
        return list(custom["partitionKeys"])
    flagged = [(p.get("partitionKeyPosition", -1), i, p.get("name"))
               for i, p in enumerate(obj.get("properties") or []) if p.get("partitioned")]
    flagged.sort(key=lambda t: (t[0] if isinstance(t[0], int) and t[0] > 0 else 10**6, t[1]))
    return [name for _, _, name in flagged]


def from_odcs(doc: dict) -> tuple[list[TableSchema], dict[str, list[str]]]:
    """Convert an ODCS document into TableSchemas.

    Returns (tables, ignored) where ``ignored`` maps each ODCS field Shelfard
    can't represent (``quality``, ``examples``, ``classification``, contract
    sections like ``team``) to the paths where it occurred.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("schema"), list):
        raise ValueError("Not an ODCS document: expected an object with a 'schema' list")
    generated = is_shelfard_generated(doc)
    ignored = _Ignored()
    for key in doc:
        if key not in _HEADER_KEYS:
            ignored.add(key, "<contract>")

    tables = []
    for obj in doc["schema"]:
        if not isinstance(obj, dict) or not obj.get("name"):
            raise ValueError("Every schema object needs a 'name'")
        table = obj["name"]
        custom, other_custom = _shelfard_custom(obj)
        for key in obj:
            if key not in _OBJECT_KEYS:
                ignored.add(key, table)
        for other in other_custom:
            ignored.add(f"customProperties.{other}", table)
        columns = [_column(p, f"{table}.{p.get('name')}", generated, ignored)
                   for p in obj.get("properties") or []]
        tables.append(TableSchema(
            table_name=table,
            columns=columns,
            partition_keys=_partition_keys(obj, custom),
            clustering_keys=list(custom.get("clusteringKeys") or []),
            source=custom.get("source") or "odcs",
            captured_at=custom.get("capturedAt"),
        ))
    return tables, ignored.fields


# ─────────────────────────────────────────────
# Versions
# ─────────────────────────────────────────────

_BUMP = {ChangeSeverity.BREAKING: "major", ChangeSeverity.WARNING: "minor", ChangeSeverity.SAFE: "patch"}


def bump_for(severity) -> str:
    """Semver part to bump for a diff's overall severity."""
    return _BUMP[ChangeSeverity(severity)]


def bump_version(version: Optional[str], part: str) -> Optional[str]:
    """Bump a ``MAJOR.MINOR.PATCH`` string; None if it isn't semver."""
    m = _SEMVER_RE.match(str(version or ""))
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def semver_history(versions: list[TableSchema]) -> list[str]:
    """Contract versions for a registry history: v1 is 1.0.0; each later
    version bumps by its diff against the previous one (BREAKING → major,
    WARNING → minor, SAFE or unchanged → patch)."""
    from .schema_comparison import compare_schemas

    out: list[str] = []
    for i, schema in enumerate(versions):
        if i == 0:
            out.append("1.0.0")
            continue
        result = compare_schemas(versions[i - 1], schema)
        severity = result.data["diff"]["overall_severity"] if result.success else ChangeSeverity.BREAKING
        out.append(bump_version(out[-1], bump_for(severity)))
    return out


# ─────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────

def _yaml():
    try:
        import yaml
    except ImportError as e:
        raise ImportError("YAML support needs PyYAML: pip install 'shelfard[odcs]'") from e
    return yaml


def is_yaml_path(path) -> bool:
    return Path(path).suffix.lower() in (".yaml", ".yml")


def dumps_contract(doc: dict, fmt: str = "json") -> str:
    if fmt == "yaml":
        return _yaml().safe_dump(doc, sort_keys=False, allow_unicode=True)
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def load_contract(path) -> dict:
    """Read an ODCS document from a .json / .yaml / .yml file."""
    text = Path(path).read_text(encoding="utf-8")
    if is_yaml_path(path):
        return _yaml().safe_load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _yaml().safe_load(text)     # YAML file with another extension


def save_contract(doc: dict, path) -> None:
    Path(path).write_text(dumps_contract(doc, "yaml" if is_yaml_path(path) else "json"),
                          encoding="utf-8")


def validate_contract(doc: dict, json_schema: dict) -> list[str]:
    """Validation errors of *doc* against the official ODCS JSON Schema (needs jsonschema)."""
    try:
        import jsonschema
    except ImportError as e:
        raise ImportError("Validation needs jsonschema: pip install 'shelfard[odcs]'") from e
    validator = jsonschema.Draft201909Validator(json_schema)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in validator.iter_errors(doc)]


# ─────────────────────────────────────────────
# Registry integration
# ─────────────────────────────────────────────

def _registry(registry):
    if registry is not None:
        return registry
    from .registry import _default
    return _default


def export_odcs(schema_name: str, version: Optional[int] = None, *, registry=None,
                status: str = "active") -> ToolResult:
    """ODCS document for a registered schema (latest, or 1-based ``version``)."""
    reg = _registry(registry)
    history = reg.get_schema_history(schema_name)
    if not history.success:
        return history
    versions = [TableSchema.from_dict(v) for v in history.data["versions"]]
    if not versions:
        return ToolResult(success=False, error=f"'{schema_name}' has no versions.")
    index = len(versions) - 1 if version is None else version - 1
    if not 0 <= index < len(versions):
        return ToolResult(success=False,
                          error=f"'{schema_name}' has versions 1..{len(versions)}; got {version}.")
    semvers = semver_history(versions[: index + 1])
    doc = to_odcs([versions[index]], version=semvers[index], status=status, name=schema_name)
    return ToolResult(success=True, data={
        "contract": doc,
        "version_number": index + 1,
        "contract_version": semvers[index],
    })


def import_odcs(doc: dict, *, name_prefix: str = "", registry=None) -> ToolResult:
    """Register every schema object of an ODCS document as a new version."""
    from .security import validate_name

    try:
        tables, ignored = from_odcs(doc)
    except ValueError as e:
        return ToolResult(success=False, error=str(e))
    reg = _registry(registry)
    for t in tables:
        if err := validate_name(f"{name_prefix}{t.table_name}"):
            return ToolResult(success=False, error=err)
    registered = []
    for t in tables:
        name = f"{name_prefix}{t.table_name}"
        result = reg.register_schema(name, replace(t, table_name=name))
        if not result.success:
            return result
        registered.append({"name": name, "version_count": result.data["version_count"]})
    return ToolResult(
        success=True,
        data={"registered": registered, "ignored": ignored},
        next_action_hint=("Some ODCS fields have no Shelfard equivalent and were not imported: "
                          + ", ".join(sorted(ignored))) if ignored else None,
    )


# ─────────────────────────────────────────────
# Contract checks and updates
# ─────────────────────────────────────────────

def find_object(doc: dict, name: Optional[str], fallback: Optional[str] = None) -> Optional[dict]:
    """The schema object to check: by name, else the only one, else ``fallback`` by name."""
    objects = [o for o in doc.get("schema") or [] if isinstance(o, dict)]
    if name:
        return next((o for o in objects if o.get("name") == name), None)
    if len(objects) == 1:
        return objects[0]
    return next((o for o in objects if o.get("name") == fallback), None)


def contract_table(doc: dict, obj: dict) -> TableSchema:
    """TableSchema for one schema object of *doc*."""
    tables, _ = from_odcs({**doc, "schema": [obj]})
    return tables[0]


def _set_or_drop(target: dict, key: str, value) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _merge_property(old: dict, new: dict, generated: bool) -> dict:
    """Write Shelfard-managed attributes of *new* into *old*, keeping everything
    else (quality rules, examples, classification, foreign custom properties)."""
    merged = dict(old)
    if "logicalType" in new:
        merged["logicalType"] = new["logicalType"]
    if new.get("required", False) != old.get("required", False):
        merged["required"] = new.get("required", False)
    if new.get("description") and not old.get("description"):
        merged["description"] = new["description"]
    if generated and "physicalType" in new:
        merged["physicalType"] = new["physicalType"]
    for key in ("primaryKey", "unique"):
        if key in new and (generated or new[key] or key in old):
            merged[key] = new[key]
    if "primaryKeyPosition" in new and (generated or "primaryKeyPosition" in old or new.get("primaryKey")):
        merged["primaryKeyPosition"] = new["primaryKeyPosition"]

    logical = merged.get("logicalType")
    allowed = _ALLOWED_OPTIONS.get(logical or "", set())
    new_opts = new.get("logicalTypeOptions") or {}
    opts = {k: v for k, v in (old.get("logicalTypeOptions") or {}).items() if k in allowed}
    if logical == "string":
        _set_or_drop(opts, "maxLength", new_opts.get("maxLength"))   # part of the type
    if logical in ("integer", "number"):
        _set_or_drop(opts, "format", new_opts.get("format"))         # bit width
    for key, value in new_opts.items():
        if key in allowed:
            opts[key] = value
    _set_or_drop(merged, "logicalTypeOptions", opts or None)

    if "properties" in new:
        merged["properties"] = _merge_properties(old.get("properties") or [], new["properties"], generated)
    if "items" in new:
        merged["items"] = (_merge_property(old["items"], new["items"], generated)
                           if isinstance(old.get("items"), dict) else new["items"])

    foreign = [cp for cp in old.get("customProperties") or []
               if not str(cp.get("property", "")).startswith(_P)]
    ours = [cp for cp in new.get("customProperties") or []
            if generated or cp["property"] in (f"{_P}colType", f"{_P}precision", f"{_P}scale")]
    _set_or_drop(merged, "customProperties", (foreign + ours) or None)
    return merged


def _strip_for_foreign(prop: dict) -> dict:
    """A new property for a hand-written contract: no raw physical types or
    Shelfard bookkeeping beyond what the type needs."""
    prop = {k: v for k, v in prop.items() if k != "physicalType"}
    if prop.get("primaryKey") is False:
        prop.pop("primaryKey")
    if prop.get("unique") is False:
        prop.pop("unique")
    custom = [cp for cp in prop.get("customProperties") or []
              if cp["property"] in (f"{_P}colType", f"{_P}precision", f"{_P}scale")]
    _set_or_drop(prop, "customProperties", custom or None)
    if "properties" in prop:
        prop["properties"] = [_strip_for_foreign(p) for p in prop["properties"]]
    if isinstance(prop.get("items"), dict):
        prop["items"] = _strip_for_foreign(prop["items"])
    return prop


def _merge_properties(old: list[dict], new: list[dict], generated: bool) -> list[dict]:
    """Merge property lists: the contract's order is kept, and each new property
    is inserted right after the property that precedes it in *new* (so column
    order matches the source and no reorder is reported on the next check)."""
    new_by_name = {p.get("name"): p for p in new}
    out = []
    for prop in old:
        match = new_by_name.get(prop.get("name"))
        out.append(_merge_property(prop, match, generated) if match else prop)
    old_names = {p.get("name") for p in old}
    for i, prop in enumerate(new):
        if prop.get("name") in old_names:
            continue
        added = prop if generated else _strip_for_foreign(prop)
        names = [p.get("name") for p in out]
        before = next((new[j].get("name") for j in range(i - 1, -1, -1)
                       if new[j].get("name") in names), None)
        out.insert(names.index(before) + 1 if before is not None else 0, added)
    return out


def update_contract(doc: dict, object_name: str, schema: TableSchema, part: str = "patch") -> dict:
    """A copy of *doc* whose schema object *object_name* reflects *schema*.

    Only ``schema[].properties`` (and Shelfard's own custom properties) change;
    every other section and unknown field is kept. ``version`` is bumped by
    *part* when it is semver.
    """
    updated = copy.deepcopy(doc)
    generated = is_shelfard_generated(doc)
    obj = find_object(updated, object_name)
    if obj is None:
        raise ValueError(f"Schema object '{object_name}' not found in the contract")
    fresh = _object(replace(schema, table_name=obj["name"]))
    obj["properties"] = _merge_properties(obj.get("properties") or [], fresh["properties"], generated)
    if generated:
        foreign = [cp for cp in obj.get("customProperties") or []
                   if not str(cp.get("property", "")).startswith(_P)]
        obj["customProperties"] = foreign + fresh["customProperties"]
    new_version = bump_version(updated.get("version"), part)
    if new_version:
        updated["version"] = new_version
    return updated
