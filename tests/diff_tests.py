"""
Diff-engine regression tests: per-column multi-change reporting, sampled
(REST/JSON) sources, array item types, VARCHAR/DECIMAL bounds, partition and
clustering keys, and the suggested baseline update for all-SAFE diffs.

Run: conda run -n shelfard python3 tests/diff_tests.py
"""

import io
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import LocalFileRegistry, compare_schemas, compare_schemas_from_dicts
from shelfard.models import (
    ChangeSeverity as S, ChangeType as CT, ColumnChange, ColumnSchema as C, ColumnType as T,
    SchemaDiff, TableSchema,
)
from shelfard.parsers.json_file_reader import _build_column_schema as infer

# ─────────────────────────────────────────────
# Minimal test framework
# ─────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except AssertionError as e:
        print(f"  ✗ {name}")
        errors.append((name, str(e)))
        failed += 1
    except Exception:
        print(f"  ✗ {name} [ERROR]")
        errors.append((name, traceback.format_exc()))
        failed += 1


def rest(*cols, **kw):
    return TableSchema("t", list(cols), source="rest_api", **kw)


def db(*cols, **kw):
    return TableSchema("t", list(cols), source="postgresql", **kw)


def diff(old, new):
    result = compare_schemas(old, new)
    assert result.success, result.error
    return result.data["diff"]


def kinds(d):
    return [(c["column_name"], c["change_type"], c["severity"]) for c in d["changes"]]


# ─────────────────────────────────────────────
# Reported bugs
# ─────────────────────────────────────────────

print("\nReported diff-engine bugs")


def test_multiple_changes_on_one_column():
    d = diff(db(C("a", T.INTEGER, True)), db(C("a", T.BIGINT, False)))
    assert kinds(d) == [("a", CT.TYPE_WIDENED, S.SAFE), ("a", CT.NULLABILITY_TIGHTENED, S.BREAKING)], kinds(d)
    assert d["overall_severity"] == S.BREAKING
    assert d["suggested_update"] is None


test("1: type widened AND nullability tightened are both reported", test_multiple_changes_on_one_column)


def test_struct_nullability_reported():
    old = db(C("addr", T.STRUCT, False, fields=[C("zip", T.VARCHAR)]))
    new = db(C("addr", T.STRUCT, True, fields=[C("zip", T.VARCHAR)]))
    assert kinds(diff(old, new)) == [("addr", CT.NULLABILITY_RELAXED, S.SAFE)]


test("1b: nullability change on a STRUCT column is reported", test_struct_nullability_reported)


def test_null_then_value_is_type_resolved():
    d = diff(rest(infer("name", None)), rest(infer("name", "Ann")))
    assert kinds(d) == [("name", CT.TYPE_RESOLVED, S.SAFE)], kinds(d)
    assert d["overall_severity"] == S.SAFE
    upd = d["suggested_update"]
    assert upd and upd["explanation"] == ["set the type of 'name' to varchar (was unknown)"], upd
    col = upd["schema"]["columns"][0]
    assert col["col_type"] == "varchar" and col["nullable"] is True, col  # null was seen once


test("2: REST field null → 'Ann' is SAFE type_resolved with suggestion", test_null_then_value_is_type_resolved)


def test_value_then_null_keeps_type():
    d = diff(rest(infer("name", "Ann")), rest(infer("name", None)))
    assert kinds(d) == [("name", CT.NULLABILITY_RELAXED, S.SAFE)], kinds(d)
    col = d["suggested_update"]["schema"]["columns"][0]
    assert col["col_type"] == "varchar" and col["nullable"] is True, col


test("2b: REST field 'Ann' → null keeps varchar, marks nullable", test_value_then_null_keeps_type)


def test_null_again_is_no_change():
    old = rest(C("name", T.VARCHAR, True))
    assert diff(old, rest(infer("name", None)))["changes"] == []
    assert diff(old, rest(infer("name", "Bob")))["changes"] == []  # no nulls ≠ NOT NULL


test("2c: sampled nullable field seen null / non-null again → no change", test_null_again_is_no_change)


def test_unknown_resolved_non_sampled():
    d = diff(db(C("x", T.UNKNOWN)), db(C("x", T.INTEGER)))
    assert kinds(d) == [("x", CT.TYPE_RESOLVED, S.SAFE)]
    d = diff(db(C("x", T.INTEGER)), db(C("x", T.UNKNOWN)))
    assert kinds(d) == [("x", CT.TYPE_CHANGED, S.BREAKING)], kinds(d)


test("2d: database UNKNOWN → int is resolved; int → UNKNOWN stays BREAKING", test_unknown_resolved_non_sampled)


def test_array_item_type_change():
    d = diff(rest(infer("tags", [1, 2])), rest(infer("tags", [{"x": 1}])))
    assert kinds(d) == [("tags[]", CT.TYPE_CHANGED, S.BREAKING)], kinds(d)


test("3: tags [1,2] → [{x:1}] is BREAKING on 'tags[]'", test_array_item_type_change)


def test_array_items_nested_and_empty():
    d = diff(rest(infer("tags", [{"x": 1}])), rest(infer("tags", [{"x": "a"}])))
    assert kinds(d) == [("tags[].x", CT.TYPE_CHANGED, S.BREAKING)], kinds(d)
    d = diff(rest(infer("tags", [])), rest(infer("tags", ["a"])))
    assert kinds(d) == [("tags[]", CT.TYPE_RESOLVED, S.SAFE)], kinds(d)
    assert diff(rest(infer("tags", ["a"])), rest(infer("tags", [])))["changes"] == []
    # Baselines from before item types were recorded don't compare items.
    assert diff(rest(C("tags", T.ARRAY, False)), rest(infer("tags", [1])))["changes"] == []


test("3b: nested array items, empty arrays, legacy baselines", test_array_items_nested_and_empty)


def test_varchar_bounds():
    assert kinds(diff(db(C("s", T.VARCHAR)), db(C("s", T.VARCHAR, max_length=10)))) == \
        [("s", CT.TYPE_CHANGED, S.BREAKING)]
    assert kinds(diff(db(C("s", T.VARCHAR, max_length=10)), db(C("s", T.VARCHAR)))) == \
        [("s", CT.TYPE_WIDENED, S.SAFE)]
    assert kinds(diff(db(C("s", T.VARCHAR, max_length=10)), db(C("s", T.VARCHAR, max_length=5)))) == \
        [("s", CT.TYPE_CHANGED, S.BREAKING)]
    assert kinds(diff(db(C("s", T.VARCHAR, max_length=5)), db(C("s", T.VARCHAR, max_length=10)))) == \
        [("s", CT.TYPE_WIDENED, S.SAFE)]
    # A legacy 0 means unbounded too.
    assert diff(db(C("s", T.VARCHAR, max_length=0)), db(C("s", T.VARCHAR)))["changes"] == []


test("4: unbounded VARCHAR → VARCHAR(10) is narrowing (BREAKING)", test_varchar_bounds)


def test_rest_added_field_safe():
    d = diff(rest(infer("id", 1)), rest(infer("id", 1), infer("extra", "x")))
    assert kinds(d) == [("extra", CT.COLUMN_ADDED, S.SAFE)], kinds(d)
    assert d["suggested_update"]["explanation"] == ["add 'extra' (varchar)"]
    # Databases keep the INSERT-safety rule.
    d = diff(db(C("id", T.INTEGER)), db(C("id", T.INTEGER), C("extra", T.VARCHAR, False)))
    assert kinds(d) == [("extra", CT.COLUMN_ADDED, S.BREAKING)]


test("5: REST response gaining a non-null field is SAFE", test_rest_added_field_safe)


def test_rest_key_order_ignored():
    old = rest(infer("a", 1), infer("b", 2))
    new = rest(infer("b", 2), infer("a", 1))
    assert diff(old, new)["changes"] == []
    assert kinds(diff(db(C("a", T.INTEGER), C("b", T.INTEGER)), db(C("b", T.INTEGER), C("a", T.INTEGER)))) == \
        [("<multiple>", CT.COLUMN_REORDERED, S.WARNING)]


test("5b: JSON key order is not drift; database column order still is", test_rest_key_order_ignored)


def test_partition_and_clustering_keys():
    col = C("d", T.DATE)
    d = diff(db(col, partition_keys=["day(d)"]), db(col, partition_keys=["month(d)"]))
    assert kinds(d) == [("<partition_keys>", CT.PARTITION_CHANGED, S.WARNING)], kinds(d)
    d = diff(db(col, clustering_keys=["d"]), db(col))
    assert kinds(d) == [("<clustering_keys>", CT.CLUSTERING_CHANGED, S.WARNING)], kinds(d)


test("6: partition day → month and clustering changes are WARNING", test_partition_and_clustering_keys)


def test_keys_survive_dict_wrapper():
    col = {"name": "d", "col_type": "date"}
    old = {"table_name": "t", "columns": [col], "clustering_keys": ["d"]}
    new = {"table_name": "t", "columns": [col], "clustering_keys": []}
    d = compare_schemas_from_dicts(old, new).data["diff"]
    assert kinds(d) == [("<clustering_keys>", CT.CLUSTERING_CHANGED, S.WARNING)], kinds(d)


test("6b: compare_schemas_from_dicts keeps clustering_keys", test_keys_survive_dict_wrapper)


def test_decimal_scale():
    dec = lambda p, s: C("m", T.DECIMAL, precision=p, scale=s)  # noqa: E731
    d = diff(db(dec(10, 4)), db(dec(10, 2)))
    assert kinds(d) == [("m", CT.TYPE_CHANGED, S.WARNING)] and "scale decreased" in d["changes"][0]["reasoning"]
    d = diff(db(dec(10, 2)), db(dec(10, 4)))  # more scale, fewer integer digits
    assert kinds(d) == [("m", CT.TYPE_CHANGED, S.WARNING)] and "integer digits" in d["changes"][0]["reasoning"]
    assert kinds(diff(db(dec(10, 2)), db(dec(12, 4)))) == [("m", CT.TYPE_WIDENED, S.SAFE)]


test("7: DECIMAL scale and integer-digit decreases are WARNING", test_decimal_scale)


# ─────────────────────────────────────────────
# Suggested update + applying it
# ─────────────────────────────────────────────

print("\nSuggested baseline update")


def test_no_suggestion_without_changes_or_with_warnings():
    assert diff(db(C("a", T.INTEGER)), db(C("a", T.INTEGER)))["suggested_update"] is None
    col = C("d", T.DATE)
    d = diff(db(col, partition_keys=["a"]), db(col, C("x", T.INTEGER), partition_keys=["b"]))
    assert d["overall_severity"] == S.WARNING and d["suggested_update"] is None


test("no suggestion for no-change or WARNING/BREAKING diffs", test_no_suggestion_without_changes_or_with_warnings)


def test_merged_schema_keeps_known_types_nested():
    old = rest(infer("user", {"name": "Ann", "nick": None}))
    new = rest(infer("user", {"name": None, "nick": "A", "age": 3}))
    d = diff(old, new)
    assert d["overall_severity"] == S.SAFE, kinds(d)
    fields = {f["name"]: f for f in d["suggested_update"]["schema"]["columns"][0]["fields"]}
    assert fields["name"]["col_type"] == "varchar" and fields["name"]["nullable"] is True
    assert fields["nick"]["col_type"] == "varchar" and fields["nick"]["nullable"] is True
    assert fields["age"]["col_type"] == "integer"


test("merged schema keeps known types and nullability in nested fields", test_merged_schema_keeps_known_types_nested)


def _registry_with(schema):
    tmp = tempfile.TemporaryDirectory()
    reg = LocalFileRegistry(tmp.name)
    assert reg.register_schema("orders", schema).success
    return tmp, reg


def test_accept_update_increments_version():
    tmp, reg = _registry_with(rest(infer("name", None)))
    with tmp:
        base = reg.get_registered_schema("orders").data
        assert base["version_number"] == 1 and base["version_count"] == 1
        d = diff(TableSchema.from_dict(base["schema"]), rest(infer("name", "Ann")))
        res = reg.accept_update("orders", d, base["version"])
        assert res.success, res.error
        assert res.data["version_count"] == 2 and res.data["explanation"], res.data
        latest = reg.get_registered_schema("orders").data
        assert latest["version_number"] == 2
        assert latest["schema"]["columns"][0]["col_type"] == "varchar"
        # The new baseline makes the same response drift-free.
        assert diff(TableSchema.from_dict(latest["schema"]), rest(infer("name", "Ann")))["changes"] == []


test("accept_update registers the merged schema as version 2", test_accept_update_increments_version)


def test_accept_refuses_unsafe_or_stale():
    tmp, reg = _registry_with(db(C("a", T.INTEGER)))
    with tmp:
        base = reg.get_registered_schema("orders").data
        breaking = diff(db(C("a", T.INTEGER)), db())
        res = reg.accept_update("orders", breaking, base["version"])
        assert not res.success and "No update to apply" in res.error
        safe = diff(db(C("a", T.INTEGER)), db(C("a", T.BIGINT)))
        assert reg.register_schema("orders", db(C("a", T.INTEGER), captured_at="later")).success
        res = reg.accept_update("orders", safe, base["version"])
        assert not res.success and "changed since this check" in res.error, res.error
        assert reg.get_registered_schema("orders").data["version_count"] == 2


test("accept_update refuses BREAKING diffs and stale baselines", test_accept_refuses_unsafe_or_stale)


def test_impact_analysis_array_names():
    with tempfile.TemporaryDirectory() as tmp:
        reg = LocalFileRegistry(tmp)
        reg.register_schema("orders", rest(infer("tags", [1]), infer("id", 1)))
        reg.subscribe_consumer("bi", "orders", ["tags"])
        d = compare_schemas(rest(infer("tags", [1]), infer("id", 1)),
                            rest(infer("tags", ["a"]), infer("id", 1))).data["diff"]
        res = reg.assess_consumer_impact("orders", d, rest(infer("tags", [1]), infer("id", 1)).to_dict(),
                                         rest(infer("tags", ["a"]), infer("id", 1)).to_dict())
        annotated = res.data["diff"]
        assert annotated["changes"][0]["column_name"] == "tags[]"
        assert [c["consumer"] for c in annotated["changes"][0]["consumers"]] == ["bi"], annotated
        assert annotated["consumer_summary"]["affected"] == 1


test("impact analysis maps 'tags[]' changes to the 'tags' subscription", test_impact_analysis_array_names)


# ─────────────────────────────────────────────
# CLI and MCP
# ─────────────────────────────────────────────

print("\nCLI --update and MCP accept_schema_update")


def test_cli_update_flow():
    from shelfard import cli, registry
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(registry._default, "_root", Path(tmp)):
        registry.register_schema("orders", rest(infer("name", None)))
        live = mock.Mock(**{"get_schema.return_value": mock.Mock(
            success=True, data={"schema": rest(infer("name", "Ann")).to_dict()})})
        args = SimpleNamespace(name="orders", url="https://x", header=None, bearer=None, update=False)
        with mock.patch.object(cli, "RestEndpointReader", return_value=live):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.cmd_rest_check(args)
            assert code == 1, out.getvalue()
            text = out.getvalue()
            assert "Suggested baseline update (version 1 → 2)" in text, text
            assert "set the type of 'name' to varchar" in text and "--update" in text
            assert registry.get_registered_schema("orders").data["version_count"] == 1

            args.update = True
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.cmd_rest_check(args)
            assert code == 0, out.getvalue()
            assert "is now version 2" in out.getvalue(), out.getvalue()
            assert registry.get_registered_schema("orders").data["version_count"] == 2


test("rest check suggests the update; --update saves version 2", test_cli_update_flow)


def test_cli_update_ignored_for_breaking():
    from shelfard import cli, registry
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(registry._default, "_root", Path(tmp)):
        registry.register_schema("orders", rest(infer("name", "Ann")))
        live = mock.Mock(**{"get_schema.return_value": mock.Mock(
            success=True, data={"schema": rest(infer("other", 1)).to_dict()})})
        args = SimpleNamespace(name="orders", url="https://x", header=None, bearer=None, update=True)
        with mock.patch.object(cli, "RestEndpointReader", return_value=live):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.cmd_rest_check(args)
        # No consumers: the removal is "unaffected", which --update alone won't apply.
        assert code == 1 and "--update alone only applies all-SAFE" in out.getvalue(), out.getvalue()
        assert registry.get_registered_schema("orders").data["version_count"] == 1
        registry.register_schema("orders", rest(infer("name", "Ann")))
        registry.subscribe_consumer("crm", "orders", ["name"])
        with mock.patch.object(cli, "RestEndpointReader", return_value=live):
            out = io.StringIO()
            with redirect_stdout(out):
                args.allow_unaffected = True
                code = cli.cmd_rest_check(args)
        assert code == 1 and "--update ignored" in out.getvalue(), out.getvalue()
        assert registry.get_registered_schema("orders").data["version_count"] == 2


test("--update never applies BREAKING drift a consumer reads", test_cli_update_ignored_for_breaking)


def test_mcp_accept_tool():
    import json
    from shelfard import mcp_server, registry
    from shelfard.models import ToolResult
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(registry._default, "_root", Path(tmp)):
        registry.register_schema("orders", rest(infer("name", None)))
        base = registry.get_registered_schema("orders").data
        d = diff(TableSchema.from_dict(base["schema"]), rest(infer("name", "Ann")))
        fake = ToolResult(success=True, data={"diff": d, "baseline_version": base["version"]})
        with mock.patch.object(mcp_server, "_run_probe", return_value=fake):
            out = json.loads(mcp_server.accept_schema_update("orders"))
        assert out["success"] and out["data"]["version_count"] == 2, out
        assert mcp_server.accept_schema_update in mcp_server.WRITE_TOOLS
        assert mcp_server.accept_schema_update not in mcp_server.READ_TOOLS


test("MCP accept_schema_update re-runs the check and saves version 2", test_mcp_accept_tool)


def test_agent_requires_approval():
    from shelfard import agent
    assert "accept_schema_update" in agent.WRITE_TOOLS


test("agent asks for approval before accept_schema_update", test_agent_requires_approval)


# ─────────────────────────────────────────────
# TIME, physical types, constraints
# ─────────────────────────────────────────────

print("\nTIME, physical types and constraints")

from shelfard.models import ColumnConstraints as K  # noqa: E402


def test_time_vs_timestamp_breaking():
    d = diff(db(C("t", T.TIME)), db(C("t", T.TIMESTAMP)))
    assert kinds(d) == [("t", CT.TYPE_CHANGED, S.BREAKING)], kinds(d)


test("TIME → TIMESTAMP is BREAKING (no widening rule)", test_time_vs_timestamp_breaking)


def test_physical_type_warning():
    d = diff(db(C("at", T.TIMESTAMP, physical_type="timestamp with time zone")),
             db(C("at", T.TIMESTAMP, physical_type="timestamp without time zone")))
    assert kinds(d) == [("at", CT.PHYSICAL_TYPE_CHANGED, S.WARNING)], kinds(d)
    assert "timestamp with time zone" in d["changes"][0]["reasoning"]
    assert d["suggested_update"] is None


test("same type, different physical type → WARNING", test_physical_type_warning)


def test_physical_type_not_compared():
    # Not recorded on one side (older baseline) → nothing to compare.
    assert kinds(diff(db(C("a", T.TEXT)), db(C("a", T.TEXT, physical_type="text")))) == []
    # A type change already explains the physical change.
    d = diff(db(C("a", T.INTEGER, physical_type="int4")), db(C("a", T.BIGINT, physical_type="int8")))
    assert kinds(d) == [("a", CT.TYPE_WIDENED, S.SAFE)], kinds(d)
    # Sampled null: type (and JSON type) unknown in this sample.
    d = diff(rest(infer("a", "x")), rest(infer("a", None)))
    assert all(c["change_type"] != CT.PHYSICAL_TYPE_CHANGED for c in d["changes"]), kinds(d)
    # Containers: nested fields / items are diffed themselves.
    assert kinds(diff(db(C("s", T.STRUCT, physical_type="struct<a: int64>", fields=[C("a", T.BIGINT)])),
                      db(C("s", T.STRUCT, physical_type="struct<a: int64, b: string>",
                           fields=[C("a", T.BIGINT), C("b", T.TEXT)])))) == [("s.b", CT.COLUMN_ADDED, S.SAFE)]
    # UNKNOWN on both sides with different raw types: nothing meaningful to say.
    assert kinds(diff(db(C("b", T.UNKNOWN, physical_type="bytea")),
                      db(C("b", T.UNKNOWN, physical_type="blob")))) == []


test("physical type compared only when both sides know it and the type is the same",
     test_physical_type_not_compared)


def test_constraint_warnings():
    old = db(C("id", T.BIGINT, nullable=False, constraints=K(primary_key=True, primary_key_position=1,
                                                             unique=False)),
             C("email", T.VARCHAR, constraints=K(primary_key=False, unique=False)),
             C("tags", T.ARRAY, constraints=K(max_items=5)))
    new = db(C("id", T.BIGINT, nullable=False, constraints=K(primary_key=False, unique=False)),
             C("email", T.VARCHAR, constraints=K(primary_key=False, unique=True)),
             C("tags", T.ARRAY, constraints=K(max_items=10)))
    d = diff(old, new)
    assert kinds(d) == [("id", CT.CONSTRAINT_CHANGED, S.WARNING),
                        ("email", CT.CONSTRAINT_CHANGED, S.WARNING),
                        ("tags", CT.CONSTRAINT_CHANGED, S.WARNING)], kinds(d)
    by = {c["column_name"]: c for c in d["changes"]}
    assert "primary key removed" in by["id"]["reasoning"], by["id"]["reasoning"]
    assert by["id"]["old_value"] == {"primary_key": True}, by["id"]   # position unknown on new side
    assert "unique added" in by["email"]["reasoning"]
    assert "max items 5 → 10" in by["tags"]["reasoning"]
    assert d["overall_severity"] == S.WARNING and d["suggested_update"] is None


test("PK removed / unique added / maxItems changed → WARNING", test_constraint_warnings)


def test_constraints_unknown_not_compared():
    # Older baseline without constraints, or a source that can't observe them.
    assert kinds(diff(db(C("id", T.BIGINT)), db(C("id", T.BIGINT, constraints=K(primary_key=True))))) == []
    # Contract states a pattern; Postgres can't observe patterns (None) → no change.
    assert kinds(diff(db(C("e", T.VARCHAR, constraints=K(pattern="^.+@.+$"))),
                      db(C("e", T.VARCHAR, constraints=K(primary_key=False))))) == []


test("constraint attributes compared only when both sides know them", test_constraints_unknown_not_compared)


def test_old_baseline_dict_no_noise():
    old = {"table_name": "t", "source": "postgresql", "captured_at": "2026-01-01",
           "columns": [{"name": "id", "col_type": "bigint", "nullable": False}]}
    new = db(C("id", T.BIGINT, nullable=False, physical_type="bigint",
               constraints=K(primary_key=True, primary_key_position=1, unique=False))).to_dict()
    r = compare_schemas_from_dicts(old, new)
    assert r.success and r.data["diff"]["changes"] == [], r.data


test("baselines saved before physical types/constraints existed report no drift",
     test_old_baseline_dict_no_noise)


def test_resolved_uuid_records_physical_and_constraints():
    old = db(C("ext", T.UNKNOWN))                                   # saved when uuid was unmapped
    new = db(C("ext", T.VARCHAR, physical_type="uuid", constraints=K(primary_key=False, format="uuid")))
    d = diff(old, new)
    assert kinds(d) == [("ext", CT.TYPE_RESOLVED, S.SAFE)], kinds(d)
    col = d["suggested_update"]["schema"]["columns"][0]
    assert col["physical_type"] == "uuid" and col["constraints"]["format"] == "uuid", col


test("UNKNOWN → VARCHAR (uuid) is type_resolved; update records physical type + format",
     test_resolved_uuid_records_physical_and_constraints)


def test_sampled_merge_keeps_constraints():
    old = rest(C("id", T.INTEGER, nullable=False, physical_type="integer", constraints=K(minimum=0)))
    new = rest(infer("id", 5), infer("extra", "x"))
    col = diff(old, new)["suggested_update"]["schema"]["columns"][0]
    assert col["constraints"]["minimum"] == 0 and col["physical_type"] == "integer", col


test("sampled update keeps constraints the baseline knew", test_sampled_merge_keeps_constraints)


def test_sqlite_reader_pk_unique_physical():
    import sqlite3
    from shelfard import get_sqlite_schema
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "t.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT, email VARCHAR(80) UNIQUE, at TIME, "
                     "x INT, y INT, PRIMARY KEY (b, a))")
        conn.execute("CREATE UNIQUE INDEX t_xy ON t (x, y)")     # composite: not per-column unique
        conn.commit()
        conn.close()
        r = get_sqlite_schema(path, "t")
        assert r.success, r.error
        c = {col["name"]: col for col in r.data["schema"]["columns"]}
    assert c["b"]["constraints"]["primary_key"] and c["b"]["constraints"]["primary_key_position"] == 1
    assert c["a"]["constraints"]["primary_key_position"] == 2
    assert c["email"]["constraints"] == {**c["email"]["constraints"], "primary_key": False, "unique": True}
    assert c["x"]["constraints"]["unique"] is False
    assert c["at"]["col_type"] == "time"
    assert c["email"]["physical_type"] == "VARCHAR(80)"


test("SQLite reader: composite PK positions, single-column UNIQUE, TIME, physical type",
     test_sqlite_reader_pk_unique_physical)


def test_json_physical_types():
    assert infer("a", "x").physical_type == "string"
    assert infer("a", 1).physical_type == "integer"
    assert infer("a", 1.5).physical_type == "number"
    assert infer("a", None).physical_type is None
    assert infer("a", {"b": True}).fields[0].physical_type == "boolean"


test("JSON inference records the JSON type as physical_type", test_json_physical_types)


# ─────────────────────────────────────────────
# Summary
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
