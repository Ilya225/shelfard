"""
Tests for the Iceberg table reader and checker.

Iceberg table metadata is written by hand (it is plain JSON), so no Spark,
JVM or pyiceberg is needed. `scripts/spark_fixtures.py` produces a real
Spark-written Iceberg table for manual end-to-end checks.

Run: python tests/iceberg_tests.py
"""

import gzip
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import (
    ColumnType, IcebergCheckerConfig, LocalFileRegistry,
    get_iceberg_schema, list_iceberg_tables, compare_schemas_from_dicts,
)
from shelfard.cli import _schema_from_result

# ─────────────────────────────────────────────
# Minimal test framework
# ─────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def test(name, fn):
    global passed, failed
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
        print(f"  ✓ {name}")
        passed += 1
    except AssertionError as e:
        print(f"  ✗ {name}")
        errors.append((name, str(e) or traceback.format_exc()))
        failed += 1
    except Exception:
        print(f"  ✗ {name} [ERROR]")
        errors.append((name, traceback.format_exc()))
        failed += 1


def section(name):
    print(f"\n── {name} ──")


def cols(result) -> dict:
    assert result.success, result.error
    return {c["name"]: c for c in result.data["schema"]["columns"]}


# ─────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────

def iceberg_metadata(schemas: list[dict], current_id: int, **extra) -> dict:
    md = {
        "format-version": 2,
        "table-uuid": "00000000-0000-0000-0000-000000000000",
        "location": "file:///tmp/warehouse/db/orders",
        "current-schema-id": current_id,
        "schemas": schemas,
        "default-spec-id": 0,
        "partition-specs": [{"spec-id": 0, "fields": []}],
        "default-sort-order-id": 0,
        "sort-orders": [{"order-id": 0, "fields": []}],
    }
    md.update(extra)
    return md


ICE_SCHEMA_0 = {"type": "struct", "schema-id": 0, "fields": [
    {"id": 1, "name": "order_id", "required": True, "type": "long"},
    {"id": 2, "name": "status", "required": False, "type": "string"},
]}
ICE_SCHEMA_1 = {"type": "struct", "schema-id": 1, "fields": [
    {"id": 1, "name": "order_id", "required": True, "type": "long", "doc": "Primary key"},
    {"id": 3, "name": "customer", "required": True, "type": {"type": "struct", "fields": [
        {"id": 4, "name": "id", "required": True, "type": "long"},
        {"id": 5, "name": "email", "required": False, "type": "string"},
    ]}},
    {"id": 6, "name": "amount", "required": False, "type": "decimal(10, 2)"},
    {"id": 7, "name": "created_at", "required": False, "type": "timestamptz"},
    {"id": 8, "name": "tags", "required": False,
     "type": {"type": "list", "element-id": 9, "element": "string", "element-required": False}},
    {"id": 10, "name": "attrs", "required": False,
     "type": {"type": "map", "key-id": 11, "key": "string", "value-id": 12, "value": "string",
              "value-required": False}},
    {"id": 13, "name": "ref", "required": False, "type": "uuid"},
    {"id": 14, "name": "blob", "required": False, "type": "fixed[16]"},
    {"id": 15, "name": "qty", "required": False, "type": "int", "write-default": 1},
]}


def write_iceberg_table(root: Path, versions: dict[str, dict], hint: str | None = None) -> Path:
    meta = root / "metadata"
    meta.mkdir(parents=True)
    for name, md in versions.items():
        data = json.dumps(md).encode()
        (meta / name).write_bytes(gzip.compress(data) if ".gz." in name else data)
    if hint is not None:
        (meta / "version-hint.text").write_text(hint)
    return root


# ─────────────────────────────────────────────
# Iceberg
# ─────────────────────────────────────────────

section("Iceberg")


def _ice_v2(**extra):
    return iceberg_metadata([ICE_SCHEMA_0, ICE_SCHEMA_1], current_id=1, **extra)


def test_iceberg_types(tmp):
    md = _ice_v2(
        **{"partition-specs": [{"spec-id": 0, "fields": [
            {"name": "created_at_day", "transform": "day", "source-id": 7, "field-id": 1000},
            {"name": "cid_bucket", "transform": "bucket[16]", "source-id": 4, "field-id": 1001},
        ]}],
           "sort-orders": [{"order-id": 1, "fields": [
               {"transform": "identity", "source-id": 1, "direction": "asc", "null-order": "nulls-first"},
           ]}],
           "default-sort-order-id": 1}
    )
    root = write_iceberg_table(tmp / "orders", {"v1.metadata.json": md})
    r = get_iceberg_schema(str(root), "orders")
    s = r.data["schema"]
    assert s["source"] == "iceberg"
    assert s["partition_keys"] == ["day(created_at)", "bucket[16](customer.id)"]
    assert s["clustering_keys"] == ["order_id"]
    c = cols(r)
    assert "status" not in c                         # schema 0 is not current
    assert c["order_id"]["col_type"] == ColumnType.BIGINT and not c["order_id"]["nullable"]
    assert c["order_id"]["description"] == "Primary key"
    assert c["customer"]["col_type"] == ColumnType.STRUCT
    assert [f["name"] for f in c["customer"]["fields"]] == ["id", "email"]
    assert (c["amount"]["precision"], c["amount"]["scale"]) == (10, 2)
    assert c["created_at"]["col_type"] == ColumnType.TIMESTAMP
    assert c["tags"]["col_type"] == ColumnType.ARRAY
    assert c["attrs"]["col_type"] == ColumnType.JSON
    assert c["ref"]["col_type"] == ColumnType.VARCHAR
    assert c["blob"]["col_type"] == ColumnType.UNKNOWN
    assert c["qty"]["default_value"] == "1"

test("current schema, types, partition transforms, sort order", test_iceberg_types)


def test_iceberg_version_hint(tmp):
    old = iceberg_metadata([ICE_SCHEMA_0], current_id=0)
    root = write_iceberg_table(tmp / "t", {"v1.metadata.json": old, "v2.metadata.json": _ice_v2(),
                                           "v3.metadata.json": old}, hint="2")
    r = get_iceberg_schema(str(root), "t")
    assert r.data["metadata_file"].endswith("v2.metadata.json")
    assert "customer" in cols(r)

test("table dir with version-hint.text uses the hinted version", test_iceberg_version_hint)


def test_iceberg_highest_version(tmp):
    old = iceberg_metadata([ICE_SCHEMA_0], current_id=0)
    root = write_iceberg_table(tmp / "t", {
        "00001-aaaa.metadata.json": old,
        "00010-bbbb.gz.metadata.json": _ice_v2(),
        "00002-cccc.metadata.json": old,
    })
    r = get_iceberg_schema(str(root / "metadata"), "t")    # metadata dir itself is accepted
    assert r.success, r.error
    assert r.data["metadata_file"].endswith("00010-bbbb.gz.metadata.json")
    assert "customer" in cols(r)

test("no hint: highest-numbered (gzip) metadata file wins", test_iceberg_highest_version)


def test_iceberg_format_v1(tmp):
    md = {"format-version": 1, "schema": ICE_SCHEMA_0,
          "partition-spec": [{"name": "status", "transform": "identity", "source-id": 2}]}
    p = tmp / "v1.metadata.json"
    p.write_text(json.dumps(md))
    r = get_iceberg_schema(str(p), "t")
    assert set(cols(r)) == {"order_id", "status"}
    assert r.data["schema"]["partition_keys"] == ["status"]

test("format v1 metadata (single schema, partition-spec)", test_iceberg_format_v1)


# ─────────────────────────────────────────────
# Reader / checker plumbing
# ─────────────────────────────────────────────

section("Reader & checker")


def test_errors(tmp):
    r = get_iceberg_schema("s3://bucket/warehouse/db/orders", "x")
    assert not r.success and "Remote" in r.error
    r = get_iceberg_schema(str(tmp / "missing"), "x")
    assert not r.success and "not found" in r.error
    (tmp / "plain").mkdir()
    (tmp / "plain" / "part-0.parquet").write_bytes(b"PAR1")
    r = get_iceberg_schema(str(tmp / "plain"), "x")
    assert not r.success and "Not an Iceberg table" in r.error

test("remote path, missing path, non-Iceberg directory → clean errors", test_errors)


def test_list_tables(tmp):
    wh = tmp / "warehouse"
    write_iceberg_table(wh / "db" / "orders", {"v1.metadata.json": _ice_v2()})
    write_iceberg_table(wh / "db" / "users", {"v1.metadata.json": _ice_v2()})
    write_iceberg_table(wh / "sales" / "eu" / "invoices", {"v1.metadata.json": _ice_v2()})
    (wh / "db" / "not_a_table").mkdir()
    r = list_iceberg_tables(str(wh))
    assert [t["name"] for t in r.data["tables"]] == ["db.orders", "db.users", "sales.eu.invoices"]

test("list_iceberg_tables walks a warehouse, returns dotted identifiers", test_list_tables)


def test_schema_evolution_drift(tmp):
    old = iceberg_metadata([ICE_SCHEMA_0], current_id=0)
    root = write_iceberg_table(tmp / "t", {"v1.metadata.json": old, "v2.metadata.json": _ice_v2()})
    before = get_iceberg_schema(str(root / "metadata" / "v1.metadata.json"), "t").data["schema"]
    after = get_iceberg_schema(str(root), "t").data["schema"]
    by_col = {c["column_name"]: c for c in compare_schemas_from_dicts(before, after).data["diff"]["changes"]}
    assert by_col["status"]["severity"] == "BREAKING"           # dropped column
    assert by_col["customer"]["severity"] == "BREAKING"         # required column added, no default
    assert by_col["amount"]["severity"] == "SAFE"               # optional column added

test("older metadata.json vs current: evolution is classified", test_schema_evolution_drift)


def test_checker_roundtrip(tmp):
    reg = LocalFileRegistry(tmp / "registry")
    old = iceberg_metadata([ICE_SCHEMA_0], current_id=0)
    root = write_iceberg_table(tmp / "wh" / "db" / "orders", {"v1.metadata.json": old}, hint="1")

    base = get_iceberg_schema(str(root), "orders")
    assert reg.register_schema("orders", _schema_from_result(base)).success
    assert reg.set_var("warehouse", str(tmp / "wh")).success
    cfg = IcebergCheckerConfig(schema_name="orders", path="{{warehouse}}/db/orders")
    assert reg.register_checker("orders", cfg).success
    assert reg.get_all_checkers().data["checkers"][0]["checker_type"] == "iceberg"

    r = reg.run_checker("orders")
    assert r.success, r.error
    assert r.data["diff"]["changes"] == []

    # A writer commits a schema change: new metadata version + updated hint.
    (root / "metadata" / "v2.metadata.json").write_text(json.dumps(_ice_v2()))
    (root / "metadata" / "version-hint.text").write_text("2")
    r = reg.run_checker("orders")
    assert r.success, r.error
    assert r.data["metadata_file"].endswith("v2.metadata.json")
    assert "status" in [c["column_name"] for c in r.data["diff"]["changes"]]

test("IcebergChecker via registry: {{var}} path, no drift, then a new commit", test_checker_roundtrip)


def test_checker_env(tmp):
    reg = LocalFileRegistry(tmp / "registry")
    cfg = IcebergCheckerConfig(schema_name="orders", path="$SHELFARD_TEST_WH/db/orders",
                               env=["SHELFARD_TEST_WH"])
    assert reg.register_checker("orders", cfg).success
    os.environ.pop("SHELFARD_TEST_WH", None)
    r = reg.run_checker("orders")
    assert not r.success and "SHELFARD_TEST_WH" in r.error

test("IcebergChecker: missing env var → error", test_checker_env)


# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed", end="")
if failed:
    print(f"  ({failed} failed)")
    print("\nFailures:")
    for name, err in errors:
        print(f"\n  ✗ {name}")
        print(f"    {err}")
else:
    print(" ✓")
print('='*50)
sys.exit(0 if failed == 0 else 1)
