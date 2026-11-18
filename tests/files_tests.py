"""
Tests for the file-format readers: Parquet, ORC and Avro.

Fixtures are generated on the fly — Parquet/ORC with pyarrow, Avro container
headers with the stdlib — so no Spark / JVM is needed. Iceberg tests live in
tests/iceberg_tests.py.
(`scripts/spark_fixtures.py` produces real Spark-written tables for manual
end-to-end checks.)

Run: python tests/files_tests.py
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import (
    ColumnType, FileCheckerConfig, FileReader, LocalFileRegistry,
    get_file_schema, list_file_tables,
)
from shelfard.tools.files import detect_format

try:
    import pyarrow as pa
    import pyarrow.orc as pa_orc
    import pyarrow.parquet as pq
    HAVE_PYARROW = True
except ImportError:
    HAVE_PYARROW = False

# ─────────────────────────────────────────────
# Minimal test framework
# ─────────────────────────────────────────────

passed = 0
failed = 0
skipped = 0
errors = []


def test(name, fn, requires_pyarrow=False):
    global passed, failed, skipped
    if requires_pyarrow and not HAVE_PYARROW:
        print(f"  - {name} [SKIPPED: pyarrow not installed]")
        skipped += 1
        return
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

def _zigzag(n: int) -> bytes:
    n = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _avro_bytes(b: bytes) -> bytes:
    return _zigzag(len(b)) + b


def write_avro(path: Path, schema: dict, negative_block_count: bool = False) -> Path:
    """Write a valid Avro container file with the given schema and zero data blocks."""
    meta = {"avro.schema": json.dumps(schema).encode(), "avro.codec": b"null"}
    entries = b"".join(_avro_bytes(k.encode()) + _avro_bytes(v) for k, v in meta.items())
    if negative_block_count:
        header_map = _zigzag(-len(meta)) + _zigzag(len(entries)) + entries + _zigzag(0)
    else:
        header_map = _zigzag(len(meta)) + entries + _zigzag(0)
    path.write_bytes(b"Obj\x01" + header_map + os.urandom(16))
    return path


ORDERS_AVRO = {
    "type": "record", "name": "Order", "namespace": "com.example",
    "fields": [
        {"name": "order_id", "type": "long", "doc": "Primary key"},
        {"name": "customer", "type": {
            "type": "record", "name": "Customer",
            "fields": [
                {"name": "id", "type": "long"},
                {"name": "email", "type": ["null", "string"], "default": None},
            ],
        }},
        {"name": "billing", "type": ["null", "Customer"]},            # named-type reference
        {"name": "quantity", "type": ["int", "null"]},                # Spark puts null last
        {"name": "amount", "type": {"type": "fixed", "name": "amt", "size": 5,
                                    "logicalType": "decimal", "precision": 10, "scale": 2}},
        {"name": "status", "type": {"type": "enum", "name": "Status", "symbols": ["NEW", "DONE"]},
         "default": "NEW"},
        {"name": "tags", "type": {"type": "array", "items": "string"}},
        {"name": "attrs", "type": {"type": "map", "values": "string"}},
        {"name": "created_at", "type": {"type": "long", "logicalType": "timestamp-micros"}},
        {"name": "order_date", "type": {"type": "int", "logicalType": "date"}},
        {"name": "ref", "type": {"type": "string", "logicalType": "uuid"}},
        {"name": "payload", "type": ["null", "string", "long"]},     # multi-branch union
        {"name": "raw", "type": "bytes"},
    ],
}


# ─────────────────────────────────────────────
# Avro
# ─────────────────────────────────────────────

section("Avro")


def test_avro_types(tmp):
    c = cols(get_file_schema(str(write_avro(tmp / "o.avro", ORDERS_AVRO)), "orders"))
    assert c["order_id"]["col_type"] == ColumnType.BIGINT and not c["order_id"]["nullable"]
    assert c["order_id"]["description"] == "Primary key"
    assert c["customer"]["col_type"] == ColumnType.STRUCT and not c["customer"]["nullable"]
    sub = {f["name"]: f for f in c["customer"]["fields"]}
    assert sub["email"]["nullable"] and sub["email"]["col_type"] == ColumnType.TEXT
    assert sub["email"]["default_value"] == "null"
    assert c["billing"]["col_type"] == ColumnType.STRUCT and c["billing"]["nullable"]
    assert [f["name"] for f in c["billing"]["fields"]] == ["id", "email"]
    assert c["quantity"]["col_type"] == ColumnType.INTEGER and c["quantity"]["nullable"]
    assert c["amount"]["col_type"] == ColumnType.DECIMAL
    assert (c["amount"]["precision"], c["amount"]["scale"]) == (10, 2)
    assert c["status"]["col_type"] == ColumnType.VARCHAR and c["status"]["default_value"] == '"NEW"'
    assert c["tags"]["col_type"] == ColumnType.ARRAY
    assert c["attrs"]["col_type"] == ColumnType.JSON
    assert c["created_at"]["col_type"] == ColumnType.TIMESTAMP
    assert c["order_date"]["col_type"] == ColumnType.DATE
    assert c["ref"]["col_type"] == ColumnType.VARCHAR
    assert c["payload"]["col_type"] == ColumnType.UNKNOWN and c["payload"]["nullable"]
    assert c["raw"]["col_type"] == ColumnType.UNKNOWN

test("container header: types, unions, named refs, logical types, defaults", test_avro_types)


def test_avro_negative_block_count(tmp):
    p = write_avro(tmp / "o.avro", ORDERS_AVRO, negative_block_count=True)
    assert "order_id" in cols(get_file_schema(str(p), "orders"))

test("container header with negative block count (size-prefixed map)", test_avro_negative_block_count)


def test_avro_recursive(tmp):
    schema = {"type": "record", "name": "Node", "fields": [
        {"name": "value", "type": "int"},
        {"name": "next", "type": ["null", "Node"]},
    ]}
    c = cols(get_file_schema(str(write_avro(tmp / "n.avro", schema)), "nodes"))
    assert c["next"]["col_type"] == ColumnType.STRUCT and c["next"]["nullable"]

test("recursive record does not loop forever", test_avro_recursive)


def test_avsc(tmp):
    p = tmp / "orders.avsc"
    p.write_text(json.dumps(ORDERS_AVRO))
    r = get_file_schema(str(p), "orders")
    assert r.data["file_format"] == "avro"
    assert len(cols(r)) == len(ORDERS_AVRO["fields"])

test(".avsc schema file", test_avsc)


def test_avro_bad_magic(tmp):
    p = tmp / "bad.avro"
    p.write_bytes(b"not avro at all")
    r = get_file_schema(str(p), "bad")
    assert not r.success and "magic" in r.error

test("bad magic bytes → clean error", test_avro_bad_magic)


def test_avro_directory_skips_hidden(tmp):
    d = tmp / "orders"
    d.mkdir()
    (d / "_SUCCESS").write_text("")
    (d / ".part-0.avro.crc").write_bytes(b"junk")
    write_avro(d / "part-0.avro", ORDERS_AVRO)
    r = get_file_schema(str(d), "orders")
    assert r.data["file_format"] == "avro"
    assert r.data["source_file"].endswith("part-0.avro")

test("directory of Avro files: format detected, _SUCCESS/.crc ignored", test_avro_directory_skips_hidden)


def test_avro_drift(tmp):
    v2 = json.loads(json.dumps(ORDERS_AVRO))
    v2["fields"] = [f for f in v2["fields"] if f["name"] != "raw"]
    v2["fields"].append({"name": "discount", "type": ["null", "double"], "default": None})
    v2["fields"].append({"name": "region", "type": "string"})       # NOT NULL, no default
    for f in v2["fields"]:
        if f["name"] == "quantity":
            f["type"] = ["long", "null"]

    from shelfard import compare_schemas_from_dicts
    old = get_file_schema(str(write_avro(tmp / "v1.avro", ORDERS_AVRO)), "o").data["schema"]
    new = get_file_schema(str(write_avro(tmp / "v2.avro", v2)), "o").data["schema"]
    diff = compare_schemas_from_dicts(old, new).data["diff"]
    by_col = {c["column_name"]: c for c in diff["changes"]}
    assert by_col["raw"]["severity"] == "BREAKING"
    assert by_col["region"]["severity"] == "BREAKING"     # required, no default
    assert by_col["discount"]["severity"] == "SAFE"
    assert by_col["quantity"]["change_type"] == "type_widened"

test("drift between two Avro schemas is classified", test_avro_drift)


# ─────────────────────────────────────────────
# Parquet / ORC
# ─────────────────────────────────────────────

section("Parquet / ORC")


def _arrow_table():
    schema = pa.schema([
        pa.field("order_id", pa.int64(), nullable=False),
        pa.field("customer", pa.struct([
            pa.field("id", pa.int64(), nullable=False),
            pa.field("email", pa.string()),
        ]), nullable=False),
        pa.field("quantity", pa.int32()),
        pa.field("amount", pa.decimal128(10, 2)),
        pa.field("active", pa.bool_()),
        pa.field("score", pa.float64()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("attrs", pa.map_(pa.string(), pa.string())),
        pa.field("created_at", pa.timestamp("us", tz="UTC")),
        pa.field("order_date", pa.date32()),
        pa.field("category", pa.dictionary(pa.int32(), pa.string())),
        pa.field("raw", pa.binary()),
    ])
    return pa.Table.from_pylist([], schema=schema)


def test_parquet_file(tmp):
    pq.write_table(_arrow_table(), tmp / "o.parquet")
    r = get_file_schema(str(tmp / "o.parquet"), "orders")
    assert r.data["schema"]["source"] == "parquet"
    c = cols(r)
    assert c["order_id"]["col_type"] == ColumnType.BIGINT and not c["order_id"]["nullable"]
    assert c["customer"]["col_type"] == ColumnType.STRUCT
    sub = {f["name"]: f for f in c["customer"]["fields"]}
    assert not sub["id"]["nullable"] and sub["email"]["nullable"]
    assert c["quantity"]["col_type"] == ColumnType.INTEGER
    assert (c["amount"]["col_type"], c["amount"]["precision"], c["amount"]["scale"]) == \
        (ColumnType.DECIMAL, 10, 2)
    assert c["active"]["col_type"] == ColumnType.BOOLEAN
    assert c["score"]["col_type"] == ColumnType.FLOAT
    assert c["tags"]["col_type"] == ColumnType.ARRAY
    assert c["attrs"]["col_type"] == ColumnType.JSON
    assert c["created_at"]["col_type"] == ColumnType.TIMESTAMP
    assert c["order_date"]["col_type"] == ColumnType.DATE
    assert c["category"]["col_type"] == ColumnType.TEXT
    assert c["raw"]["col_type"] == ColumnType.UNKNOWN

test("Parquet file: types, nested struct, required/optional", test_parquet_file, requires_pyarrow=True)


def test_parquet_hive_partitioned(tmp):
    root = tmp / "orders"
    t = pa.table({"order_id": pa.array([1, 2], pa.int64()), "region": ["eu", "us"]})
    pq.write_to_dataset(t, root, partition_cols=["region"])
    (root / "_SUCCESS").write_text("")
    r = get_file_schema(str(root), "orders")
    assert r.success, r.error
    assert r.data["file_format"] == "parquet"
    assert r.data["schema"]["partition_keys"] == ["region"]
    assert set(cols(r)) == {"order_id", "region"}

test("Parquet directory: Hive partitions become partition_keys", test_parquet_hive_partitioned,
     requires_pyarrow=True)


def test_orc_file(tmp):
    t = pa.table({"order_id": pa.array([1], pa.int64()), "name": ["a"]},
                 schema=pa.schema([pa.field("order_id", pa.int64(), nullable=False),
                                   pa.field("name", pa.string())]))
    pa_orc.write_table(t, tmp / "o.orc")
    r = get_file_schema(str(tmp / "o.orc"), "orders")
    c = cols(r)
    assert r.data["schema"]["source"] == "orc"
    assert c["order_id"]["col_type"] == ColumnType.BIGINT
    assert c["order_id"]["nullable"]       # ORC has no nullability → conservative
    assert r.data["schema"]["partition_keys"] == []

test("ORC file: types read, every column nullable", test_orc_file, requires_pyarrow=True)


def test_magic_detection(tmp):
    pq.write_table(_arrow_table(), tmp / "no_extension")
    assert detect_format(tmp / "no_extension") == "parquet"
    pa_orc.write_table(pa.table({"a": [1]}), tmp / "orc_no_ext")
    assert detect_format(tmp / "orc_no_ext") == "orc"
    write_avro(tmp / "avro_no_ext", ORDERS_AVRO)
    assert detect_format(tmp / "avro_no_ext") == "avro"

test("format detection from magic bytes", test_magic_detection, requires_pyarrow=True)


def test_parquet_drift(tmp):
    from shelfard import compare_schemas_from_dicts
    pq.write_table(pa.table({"id": pa.array([1], pa.int32()), "name": ["a"]}), tmp / "v1.parquet")
    pq.write_table(pa.table({"id": pa.array([1], pa.int64())}), tmp / "v2.parquet")
    old = get_file_schema(str(tmp / "v1.parquet"), "t").data["schema"]
    new = get_file_schema(str(tmp / "v2.parquet"), "t").data["schema"]
    diff = compare_schemas_from_dicts(old, new).data["diff"]
    by_col = {c["column_name"]: c for c in diff["changes"]}
    assert by_col["id"]["change_type"] == "type_widened"
    assert by_col["name"]["severity"] == "BREAKING"

test("drift between two Parquet files is classified", test_parquet_drift, requires_pyarrow=True)


# ─────────────────────────────────────────────
# Reader / checker plumbing
# ─────────────────────────────────────────────

section("Reader & checker")


def test_errors(tmp):
    r = get_file_schema("s3://bucket/orders", "x")
    assert not r.success and "Remote" in r.error
    r = get_file_schema(str(tmp / "missing"), "x")
    assert not r.success and "not found" in r.error
    (tmp / "notes.txt").write_text("hello")
    r = get_file_schema(str(tmp / "notes.txt"), "x")
    assert not r.success and "detect" in r.error
    r = FileReader(str(tmp / "notes.txt"), "x", "csv").get_schema()
    assert not r.success and "Unsupported" in r.error

test("remote path, missing path, unknown format → clean errors", test_errors)


def _minimal_iceberg_table(root: Path) -> Path:
    (root / "metadata").mkdir(parents=True)
    (root / "metadata" / "v1.metadata.json").write_text(json.dumps({
        "format-version": 2, "current-schema-id": 0,
        "schemas": [{"type": "struct", "schema-id": 0,
                     "fields": [{"id": 1, "name": "id", "required": True, "type": "long"}]}],
    }))
    (root / "data").mkdir()
    write_avro(root / "data" / "part-0.avro", ORDERS_AVRO)   # data files inside the table
    return root


def test_list_tables(tmp):
    write_avro(tmp / "a.avro", ORDERS_AVRO)
    _minimal_iceberg_table(tmp / "ice")
    (tmp / "readme.txt").write_text("x")
    r = list_file_tables(str(tmp))
    found = {t["name"]: t["file_format"] for t in r.data["tables"]}
    assert found == {"a.avro": "avro"}        # Iceberg tables are not file datasets

test("list_file_tables finds sources one level deep, skips Iceberg tables", test_list_tables)


def test_iceberg_refused(tmp):
    table = _minimal_iceberg_table(tmp / "ice")
    for target in (table, table / "metadata" / "v1.metadata.json"):
        r = get_file_schema(str(target), "x")
        assert not r.success and "Iceberg" in r.error, r.error
        assert "iceberg" in r.next_action_hint

test("Iceberg table / metadata file → refused with a pointer to the iceberg reader", test_iceberg_refused)


def test_file_checker_roundtrip(tmp):
    reg = LocalFileRegistry(tmp / "registry")
    data = tmp / "data"
    data.mkdir()
    write_avro(data / "orders.avro", ORDERS_AVRO)

    base = get_file_schema(str(data / "orders.avro"), "orders")
    from shelfard.cli import _schema_from_result
    assert reg.register_schema("orders", _schema_from_result(base)).success
    assert reg.set_var("data_root", str(data)).success
    cfg = FileCheckerConfig(schema_name="orders", path="{{data_root}}/orders.avro")
    assert reg.register_checker("orders", cfg).success
    assert reg.get_all_checkers().data["checkers"][0]["path"] == "{{data_root}}/orders.avro"

    r = reg.run_checker("orders")
    assert r.success, r.error
    assert r.data["diff"]["changes"] == []

    v2 = json.loads(json.dumps(ORDERS_AVRO))
    v2["fields"] = v2["fields"][:-1]                      # drop "raw"
    write_avro(data / "orders.avro", v2)
    r = reg.run_checker("orders")
    assert r.success, r.error
    assert [c["column_name"] for c in r.data["diff"]["changes"]] == ["raw"]

test("FileChecker via registry: {{var}} path, no drift, then drift", test_file_checker_roundtrip)


def test_file_checker_env(tmp):
    reg = LocalFileRegistry(tmp / "registry")
    cfg = FileCheckerConfig(schema_name="orders", path="$SHELFARD_TEST_DATA/o.avro",
                            env=["SHELFARD_TEST_DATA"])
    assert reg.register_checker("orders", cfg).success
    os.environ.pop("SHELFARD_TEST_DATA", None)
    r = reg.run_checker("orders")
    assert not r.success and "SHELFARD_TEST_DATA" in r.error

test("FileChecker: missing env var → error", test_file_checker_env)


# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed", end="")
if skipped:
    print(f"  ({skipped} skipped)", end="")
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
