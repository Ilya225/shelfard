"""
ODCS v3.1.0 adapter tests: lossless round trip for every reader's output,
validation against the official JSON Schema, semver derivation from the
registry history, importing foreign contracts, contract checks / --update
(hand-written sections kept), and the MCP export tool.

The official schema is vendored at tests/fixtures/odcs-json-schema-v3.1.0.json
(from github.com/bitol-io/open-data-contract-standard, Apache-2.0).

Run: conda run -n shelfard python3 tests/odcs_tests.py
"""

import io
import json
import sqlite3
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import odcs  # noqa: E402
from shelfard.models import (  # noqa: E402
    ColumnConstraints as K, ColumnSchema as C, ColumnType as T, TableSchema,
)
from shelfard.parsers.json_file_reader import _build_table_schema  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "odcs-json-schema-v3.1.0.json"
ODCS_SCHEMA = json.loads(FIXTURE.read_text())
try:
    import jsonschema  # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False
try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

passed = 0
failed = 0
skipped = 0
errors = []


def test(name, fn, requires=None):
    global passed, failed, skipped
    if requires is not None and not requires[0]:
        print(f"  - {name} [SKIPPED: {requires[1]} not installed]")
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


def assert_valid(doc):
    if HAVE_JSONSCHEMA:
        errs = odcs.validate_contract(doc, ODCS_SCHEMA)
        assert not errs, errs


def assert_roundtrip(table: TableSchema):
    doc = odcs.to_odcs([table])
    assert_valid(doc)
    tables, ignored = odcs.from_odcs(json.loads(json.dumps(doc)))   # through JSON
    assert ignored == {}, ignored
    back = tables[0].to_dict()
    expected = table.to_dict()
    for a, b in zip(expected["columns"], back["columns"]):
        assert a == b, f"\n  expected {a}\n  got      {b}"
    assert back == expected, (back, expected)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def registry_tmp(tmp):
    from shelfard import registry
    return mock.patch.object(registry._default, "_root", Path(tmp))


def run_cli(*argv):
    from shelfard import cli
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["shelfard", *argv]), redirect_stdout(out):
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


# ─────────────────────────────────────────────
# Round trip
# ─────────────────────────────────────────────

section("Round trip (Shelfard → ODCS → Shelfard)")

KITCHEN_SINK = TableSchema("orders", [
    C("id", T.BIGINT, nullable=False, physical_type="bigint",
      constraints=K(primary_key=True, primary_key_position=1, unique=False)),
    C("ext", T.VARCHAR, physical_type="uuid", constraints=K(primary_key=False, unique=True, format="uuid")),
    C("name", T.VARCHAR, max_length=100, default_value="'x'", description="Customer name"),
    C("notes", T.TEXT),
    C("amount", T.DECIMAL, precision=10, scale=2),
    C("ratio", T.FLOAT, constraints=K(minimum=0, maximum=1)),
    C("at", T.TIME, constraints=K(format="HH:mm")),
    C("blob", T.UNKNOWN, physical_type="bytea"),
    C("meta", T.JSON),
    C("addr", T.STRUCT, fields=[C("zip", T.VARCHAR, nullable=False, constraints=K(pattern="^[0-9]{5}$"))]),
    C("empty", T.STRUCT, fields=None),
    C("tags", T.ARRAY, items=C("item", T.VARCHAR, nullable=False), constraints=K(max_items=5)),
    C("n", T.INTEGER, max_length=11, constraints=K(format="weird")),
    C("day", T.DATE),
], partition_keys=["day", "days(at)"], clustering_keys=["id"], source="iceberg",
    captured_at="2026-11-18T00:00:00+00:00")


def test_kitchen_sink(tmp):
    assert_roundtrip(KITCHEN_SINK)
    doc = odcs.to_odcs([KITCHEN_SINK])
    props = {p["name"]: p for p in doc["schema"][0]["properties"]}
    assert props["id"]["logicalType"] == "integer" and props["id"]["logicalTypeOptions"] == {"format": "i64"}
    assert props["id"]["primaryKey"] is True and props["id"]["required"] is True
    assert props["ext"]["logicalTypeOptions"] == {"format": "uuid"} and props["ext"]["unique"] is True
    assert props["name"]["logicalTypeOptions"] == {"maxLength": 100}
    assert props["ratio"]["logicalTypeOptions"] == {"minimum": 0, "maximum": 1}
    assert props["addr"]["properties"][0]["logicalTypeOptions"] == {"pattern": "^[0-9]{5}$"}
    assert props["tags"]["items"]["logicalType"] == "string"
    assert "logicalType" not in props["blob"]                      # UNKNOWN
    assert props["day"]["partitioned"] and props["day"]["partitionKeyPosition"] == 1
    assert doc["schema"][0]["physicalType"] == "table"


test("every field kind survives the round trip; native ODCS fields where possible", test_kitchen_sink)


def test_sqlite_roundtrip(tmp):
    from shelfard import get_sqlite_schema
    path = str(tmp / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, email VARCHAR(80) UNIQUE NOT NULL, "
                 "price DECIMAL(10,2) DEFAULT 0, at TIME, data BLOB)")
    conn.close()
    r = get_sqlite_schema(path, "t")
    assert r.success, r.error
    assert_roundtrip(TableSchema.from_dict(r.data["schema"]))


test("SQLite reader output", test_sqlite_roundtrip)


def test_postgres_roundtrip(tmp):
    from shelfard.tools.postgres.reader import _normalize_type, _string_format
    from shelfard.tools.sql.base import introspect_table_via_information_schema

    class Cursor:
        def __init__(self):
            self.results = [
                [("id", "bigint", "NO", None, 64, 0, "nextval('s')", "int8"),
                 ("ext", "uuid", "YES", None, None, None, None, "uuid"),
                 ("at", "time with time zone", "YES", None, None, None, None, "timetz"),
                 ("tags", "ARRAY", "YES", None, None, None, None, "_text")],
                [("id", "PRIMARY KEY", "t_pkey", 1), ("ext", "UNIQUE", "t_ext_key", 1)],
            ]

        def execute(self, *a):
            pass

        def fetchall(self):
            return self.results.pop(0)

    cols = introspect_table_via_information_schema(Cursor(), "t", _normalize_type,
                                                   string_format=_string_format)
    assert_roundtrip(TableSchema("t", cols, source="postgresql", captured_at="2026-01-01"))


test("PostgreSQL reader output (information_schema)", test_postgres_roundtrip)


def test_json_roundtrip(tmp):
    payload = {"id": 1, "name": None, "price": 9.5, "tags": [], "ids": [1, None],
               "created": "2026-01-01T00:00:00", "user": {"email": "a@b.c", "flags": [{"x": True}]}}
    assert_roundtrip(_build_table_schema(payload, "users"))


test("REST / JSON inference output (UNKNOWN, arrays, nested)", test_json_roundtrip)


def test_avro_roundtrip(tmp):
    from shelfard import get_file_schema
    avsc = {"type": "record", "name": "R", "fields": [
        {"name": "id", "type": "long", "doc": "PK"},
        {"name": "ref", "type": {"type": "string", "logicalType": "uuid"}},
        {"name": "at", "type": {"type": "int", "logicalType": "time-millis"}},
        {"name": "amount", "type": {"type": "bytes", "logicalType": "decimal", "precision": 9, "scale": 3}},
        {"name": "s", "type": {"type": "enum", "name": "S", "symbols": ["A"]}, "default": "A"},
        {"name": "sub", "type": {"type": "record", "name": "Sub", "fields": [
            {"name": "x", "type": ["null", "string"], "default": None}]}},
        {"name": "m", "type": {"type": "map", "values": "long"}},
    ]}
    (tmp / "r.avsc").write_text(json.dumps(avsc))
    r = get_file_schema(str(tmp / "r.avsc"), "r")
    assert r.success, r.error
    assert_roundtrip(TableSchema.from_dict(r.data["schema"]))


test("Avro reader output", test_avro_roundtrip)


def test_iceberg_roundtrip(tmp):
    from shelfard import get_iceberg_schema
    md = {"format-version": 2, "current-schema-id": 0, "default-spec-id": 0, "default-sort-order-id": 1,
          "schemas": [{"type": "struct", "schema-id": 0, "identifier-field-ids": [1], "fields": [
              {"id": 1, "name": "id", "required": True, "type": "long"},
              {"id": 2, "name": "created_at", "required": False, "type": "timestamptz"},
              {"id": 3, "name": "c", "required": True, "type": {"type": "struct", "fields": [
                  {"id": 4, "name": "email", "required": False, "type": "string"}]}},
          ]}],
          "partition-specs": [{"spec-id": 0, "fields": [
              {"name": "d", "transform": "day", "source-id": 2, "field-id": 1000}]}],
          "sort-orders": [{"order-id": 1, "fields": [
              {"transform": "identity", "source-id": 1, "direction": "asc", "null-order": "nulls-first"}]}]}
    (tmp / "t" / "metadata").mkdir(parents=True)
    (tmp / "t" / "metadata" / "v1.metadata.json").write_text(json.dumps(md))
    r = get_iceberg_schema(str(tmp / "t"), "t")
    assert r.success, r.error
    schema = TableSchema.from_dict(r.data["schema"])
    assert schema.partition_keys == ["day(created_at)"]
    assert_roundtrip(schema)


test("Iceberg reader output (transform partition keys, sort order)", test_iceberg_roundtrip)


try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAVE_PYARROW = True
except ImportError:
    HAVE_PYARROW = False


def test_parquet_roundtrip(tmp):
    from shelfard import get_file_schema
    t = pa.table({"id": pa.array([1], pa.int64()), "at": pa.array([1], pa.time32("ms")),
                  "amount": pa.array([1], pa.decimal128(10, 2)),
                  "tags": pa.array([["a"]], pa.list_(pa.string())),
                  "s": pa.array([{"x": 1}], pa.struct([pa.field("x", pa.int32())]))})
    base = tmp / "ds" / "region=eu"
    base.mkdir(parents=True)
    pq.write_table(t, base / "part-0.parquet")
    r = get_file_schema(str(tmp / "ds"), "ds")
    assert r.success, r.error
    schema = TableSchema.from_dict(r.data["schema"])
    assert schema.partition_keys == ["region"]
    assert_roundtrip(schema)


test("Parquet dataset output (hive partition column)", test_parquet_roundtrip, (HAVE_PYARROW, "pyarrow"))


def test_validation_detects_bad_doc(tmp):
    doc = odcs.to_odcs([KITCHEN_SINK])
    doc["schema"][0]["properties"][0]["logicalTypeOptions"]["maxLength"] = 5   # not allowed for integer
    assert odcs.validate_contract(doc, ODCS_SCHEMA), "expected a validation error"


test("the vendored JSON Schema rejects invalid options", test_validation_detects_bad_doc,
     (HAVE_JSONSCHEMA, "jsonschema"))


# ─────────────────────────────────────────────
# Versions
# ─────────────────────────────────────────────

section("Semantic versions from the registry history")


def test_semver_bumps(tmp):
    assert odcs.bump_version("1.2.3", "major") == "2.0.0"
    assert odcs.bump_version("1.2.3", "minor") == "1.3.0"
    assert odcs.bump_version("1.2.3", "patch") == "1.2.4"
    assert odcs.bump_version("draft", "patch") is None


test("bump_version", test_semver_bumps)


def test_export_semver_history(tmp):
    from shelfard import registry
    v1 = TableSchema("orders", [C("id", T.BIGINT, nullable=False), C("s", T.VARCHAR)], source="postgresql")
    v2 = TableSchema("orders", v1.columns + [C("note", T.TEXT)], source="postgresql")          # SAFE
    v3 = TableSchema("orders", [C("id", T.BIGINT, nullable=False), C("s", T.VARCHAR, default_value="'a'"),
                                C("note", T.TEXT)], source="postgresql")                           # WARNING
    v4 = TableSchema("orders", [C("id", T.BIGINT, nullable=False)], source="postgresql")          # BREAKING
    with registry_tmp(tmp):
        for v in (v1, v2, v3, v4, v4):                                                            # v5: unchanged
            registry.register_schema("orders", v)
        versions = [odcs.export_odcs("orders", n).data["contract_version"] for n in range(1, 6)]
        assert versions == ["1.0.0", "1.0.1", "1.1.0", "2.0.0", "2.0.1"], versions
        latest = odcs.export_odcs("orders").data
        assert latest["version_number"] == 5 and latest["contract"]["version"] == "2.0.1"
        assert latest["contract"]["id"] == odcs.contract_id("orders")
        assert not odcs.export_odcs("orders", 9).success
        assert not odcs.export_odcs("missing").success


test("export derives semver: SAFE → patch, WARNING → minor, BREAKING → major", test_export_semver_history)


# ─────────────────────────────────────────────
# Foreign contracts
# ─────────────────────────────────────────────

section("Contracts written by hand / other tools")

FOREIGN_YAML = """\
apiVersion: v3.1.0
kind: DataContract
id: 53581432-6c55-4ba2-a65f-72344a91553a
name: orders
version: 1.0.0
status: active
team:
  name: sales-data
slaProperties:
  - property: latency
    value: 4
    unit: d
schema:
  - name: orders
    physicalType: table
    description: All orders
    quality:
      - metric: rowCount
        mustBeGreaterThan: 0
    properties:
      - name: order_id
        logicalType: integer
        physicalType: BIGINT
        required: true
        primaryKey: true
        primaryKeyPosition: 1
      - name: country
        logicalType: string
        physicalType: VARCHAR(2)
        required: true
        logicalTypeOptions:
          maxLength: 2
          pattern: "^[A-Z]{2}$"
        classification: public
        examples: [FR, DE]
      - name: amount
        logicalType: number
        physicalType: DECIMAL(10,2)
      - name: created_at
        logicalType: timestamp
        logicalTypeOptions:
          timezone: true
"""


def test_foreign_import_mapping(tmp):
    doc = odcs.load_contract(_write(tmp / "c.yaml", FOREIGN_YAML))
    assert not odcs.is_shelfard_generated(doc)
    (table,), ignored = odcs.from_odcs(doc)
    c = table.column_map()
    assert c["order_id"].col_type == T.INTEGER and not c["order_id"].nullable
    assert c["order_id"].constraints.primary_key and c["order_id"].constraints.primary_key_position == 1
    assert c["order_id"].physical_type is None                 # free-form, not compared
    assert c["country"].col_type == T.VARCHAR and c["country"].max_length == 2
    assert c["country"].constraints.pattern == "^[A-Z]{2}$" and c["country"].constraints.unique is None
    assert (c["amount"].col_type, c["amount"].precision, c["amount"].scale) == (T.DECIMAL, 10, 2)
    assert c["created_at"].col_type == T.TIMESTAMP and c["created_at"].nullable
    assert table.source == "odcs"
    assert set(ignored) >= {"team", "slaProperties", "quality", "description", "classification",
                            "examples", "physicalType", "logicalTypeOptions.timezone"}, ignored
    assert ignored["classification"] == ["orders.country"]


test("foreign contract: native fields mapped, unsupported fields reported", test_foreign_import_mapping,
     (HAVE_YAML, "pyyaml"))


def test_cli_import_and_export(tmp):
    from shelfard import registry
    with registry_tmp(tmp):
        code, out = run_cli("odcs", "import", str(_write(tmp / "c.yaml", FOREIGN_YAML)), "--name-prefix", "c_")
        assert code == 0, out
        assert "Registered 'c_orders'" in out and "classification" in out, out
        assert registry.get_registered_schema("c_orders").success
        code, out = run_cli("odcs", "export", "c_orders", "--out", str(tmp / "out.yaml"))
        assert code == 0 and "contract version 1.0.0" in out, out
        doc = odcs.load_contract(tmp / "out.yaml")
        assert odcs.is_shelfard_generated(doc) and doc["schema"][0]["name"] == "c_orders"
        assert_valid(doc)
        code, out = run_cli("odcs", "export", "c_orders", "--format", "json")
        assert code == 0 and json.loads(out)["kind"] == "DataContract", out


test("CLI: odcs import (with prefix) and export (YAML file, JSON stdout)", test_cli_import_and_export,
     (HAVE_YAML, "pyyaml"))


def test_import_rejects_bad_names(tmp):
    with registry_tmp(tmp):
        doc = {"apiVersion": "v3.1.0", "kind": "DataContract", "id": "x", "version": "1", "status": "a",
               "schema": [{"name": "../etc", "properties": []}]}
        r = odcs.import_odcs(doc)
        assert not r.success
        assert not odcs.import_odcs({"schema": "nope"}).success


test("import validates names and document shape", test_import_rejects_bad_names)


# ─────────────────────────────────────────────
# Contract checks
# ─────────────────────────────────────────────

section("odcs check")


def _orders(*extra, source="postgresql"):
    return TableSchema("orders", [
        C("order_id", T.BIGINT, nullable=False, physical_type="bigint",
          constraints=K(primary_key=True, primary_key_position=1, unique=False)),
        C("status", T.VARCHAR, max_length=20, physical_type="character varying",
          constraints=K(primary_key=False, unique=False)),
        *extra,
    ], source=source)


def test_check_update_keeps_hand_written_sections(tmp):
    from shelfard import registry
    with registry_tmp(tmp):
        registry.register_schema("orders", _orders())
        contract = tmp / "orders.json"
        doc = odcs.export_odcs("orders").data["contract"]
        doc["team"] = {"name": "sales-data"}                                  # hand-written additions
        doc["schema"][0]["quality"] = [{"metric": "rowCount", "mustBeGreaterThan": 0}]
        doc["schema"][0]["properties"][1]["classification"] = "public"
        doc["schema"][0]["properties"][1]["customProperties"] = [{"property": "owner", "value": "ops"}]
        odcs.save_contract(doc, contract)

        registry.register_schema("orders", _orders(
            C("note", T.TEXT, physical_type="text", constraints=K(primary_key=False, unique=False))))
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders")
        assert code == 1, out
        assert "Suggested contract update (version 1.0.0 → 1.0.1)" in out and "add 'note'" in out, out
        assert odcs.load_contract(contract)["version"] == "1.0.0"            # not written without --update

        code, out = run_cli("odcs", "check", str(contract), "--name", "orders", "--update")
        assert code == 0 and "is now version 1.0.1" in out, out
        updated = odcs.load_contract(contract)
        assert updated["team"] == {"name": "sales-data"}
        obj = updated["schema"][0]
        assert obj["quality"] == [{"metric": "rowCount", "mustBeGreaterThan": 0}]
        props = {p["name"]: p for p in obj["properties"]}
        assert props["status"]["classification"] == "public"
        assert {"property": "owner", "value": "ops"} in props["status"]["customProperties"]
        assert props["note"]["logicalType"] == "string"
        assert [p["name"] for p in obj["properties"]] == ["order_id", "status", "note"]
        assert_valid(updated)

        code, out = run_cli("odcs", "check", str(contract), "--name", "orders")
        assert code == 0 and "No drift" in out, out


test("--update writes SAFE changes, keeps team/quality/classification, bumps patch",
     test_check_update_keeps_hand_written_sections)


def test_update_inserts_in_source_order(tmp):
    contract = odcs.to_odcs([TableSchema("t", [C("a", T.INTEGER), C("c", T.INTEGER)], source="parquet")])
    live = TableSchema("t", [C("z", T.INTEGER), C("a", T.INTEGER), C("b", T.INTEGER), C("c", T.INTEGER)],
                       source="parquet")
    updated = odcs.update_contract(contract, "t", live)
    assert [p["name"] for p in updated["schema"][0]["properties"]] == ["z", "a", "b", "c"]
    assert updated["version"] == "1.0.1"


test("update inserts new properties where the source has them (no reorder on next check)",
     test_update_inserts_in_source_order)


def test_check_warning_not_written(tmp):
    from shelfard import registry
    with registry_tmp(tmp):
        registry.register_schema("orders", _orders())
        contract = tmp / "orders.json"
        odcs.save_contract(odcs.export_odcs("orders").data["contract"], contract)
        before = contract.read_text()
        live = _orders()
        live.columns[0].constraints = K(primary_key=False, unique=False)       # PK dropped
        registry.register_schema("orders", live)
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders", "--update")
        assert code == 1 and "constraint_changed" in out and "--update ignored" in out, out
        assert contract.read_text() == before


test("WARNING drift (PK removed) is reported and never written", test_check_warning_not_written)


def test_check_foreign_contract(tmp):
    from shelfard import registry
    with registry_tmp(tmp):
        contract = _write(tmp / "orders.yaml", FOREIGN_YAML)
        live = TableSchema("orders", [
            C("order_id", T.BIGINT, nullable=False, physical_type="bigint",
              constraints=K(primary_key=True, primary_key_position=1, unique=False)),
            C("country", T.VARCHAR, nullable=False, max_length=2, physical_type="character varying",
              constraints=K(primary_key=False, unique=False)),
            C("amount", T.DECIMAL, precision=10, scale=2, physical_type="numeric"),
            C("created_at", T.TIMESTAMP, physical_type="timestamp with time zone"),
            C("channel", T.VARCHAR, physical_type="character varying"),
        ], source="postgresql")
        registry.register_schema("orders", live)
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders", "--update")
        assert code == 0, out
        # Pattern (contract-only), physicalType strings: not compared → only SAFE changes.
        assert "type_widened" in out and "column_added" in out and "physical_type_changed" not in out, out
        doc = odcs.load_contract(contract)
        props = {p["name"]: p for p in doc["schema"][0]["properties"]}
        assert props["country"]["physicalType"] == "VARCHAR(2)"               # theirs, untouched
        assert props["country"]["logicalTypeOptions"]["pattern"] == "^[A-Z]{2}$"
        assert props["country"]["examples"] == ["FR", "DE"]
        assert props["order_id"]["logicalTypeOptions"] == {"format": "i64"}
        assert "physicalType" not in props["channel"] and "unique" not in props["channel"]
        assert doc["team"] == {"name": "sales-data"} and doc["version"] == "1.0.1"
        assert_valid(doc)
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders")
        assert code == 0 and "No drift" in out, out


test("foreign contract: only stated facts compared; --update keeps their physicalType/pattern",
     test_check_foreign_contract, (HAVE_YAML, "pyyaml"))


def test_check_with_probe(tmp):
    from shelfard import FileProbeConfig, registry
    avsc = {"type": "record", "name": "R", "fields": [{"name": "id", "type": "long"}]}
    path = _write(tmp / "r.avsc", json.dumps(avsc))
    with registry_tmp(tmp):
        registry.register_probe("r", FileProbeConfig(schema_name="r", path=str(path)))
        live = registry.fetch_probe_schema("r")
        assert live.success and live.data["schema"]["columns"][0]["physical_type"] == "long", live
        contract = tmp / "r.json"
        odcs.save_contract(odcs.to_odcs([TableSchema.from_dict(live.data["schema"])]), contract)
        code, out = run_cli("odcs", "check", str(contract), "--probe", "r")
        assert code == 0 and "No drift" in out, out
        avsc["fields"].append({"name": "extra", "type": ["null", "string"], "default": None})
        path.write_text(json.dumps(avsc))
        code, out = run_cli("odcs", "check", str(contract), "--probe", "r")
        assert code == 1 and "column_added" in out, out
        code, out = run_cli("odcs", "check", str(contract), "--probe", "missing")
        assert code == 2, out


test("odcs check --probe reads the live schema through a stored probe", test_check_with_probe)


def test_check_object_selection(tmp):
    from shelfard import registry
    with registry_tmp(tmp):
        registry.register_schema("orders", _orders())
        doc = odcs.to_odcs([_orders(), TableSchema("other", [C("x", T.INTEGER)])], name="multi")
        contract = tmp / "multi.json"
        odcs.save_contract(doc, contract)
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders")   # matched by name
        assert code == 0, out
        code, out = run_cli("odcs", "check", str(contract), "--name", "orders", "--object", "nope")
        assert code == 2 and "pass --object" in out, out


test("schema object chosen by --object, else the one named like the source", test_check_object_selection)


# ─────────────────────────────────────────────
# MCP
# ─────────────────────────────────────────────

section("MCP")


def test_mcp_export_tool(tmp):
    from shelfard import mcp_server, registry
    with registry_tmp(tmp):
        registry.register_schema("orders", _orders())
        out = json.loads(mcp_server.get_schema_odcs("orders"))
        assert out["success"] and out["data"]["contract"]["kind"] == "DataContract", out
        assert mcp_server.get_schema_odcs in mcp_server.READ_TOOLS
        assert not json.loads(mcp_server.get_schema_odcs("../x"))["success"]


test("get_schema_odcs is a read tool returning the contract", test_mcp_export_tool)


# ─────────────────────────────────────────────
# Summary
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
