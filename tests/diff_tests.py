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


def test_accept_safe_update_increments_version():
    tmp, reg = _registry_with(rest(infer("name", None)))
    with tmp:
        base = reg.get_registered_schema("orders").data
        assert base["version_number"] == 1 and base["version_count"] == 1
        d = diff(TableSchema.from_dict(base["schema"]), rest(infer("name", "Ann")))
        res = reg.accept_safe_update("orders", d, base["version"])
        assert res.success, res.error
        assert res.data["version_count"] == 2 and res.data["explanation"], res.data
        latest = reg.get_registered_schema("orders").data
        assert latest["version_number"] == 2
        assert latest["schema"]["columns"][0]["col_type"] == "varchar"
        # The new baseline makes the same response drift-free.
        assert diff(TableSchema.from_dict(latest["schema"]), rest(infer("name", "Ann")))["changes"] == []


test("accept_safe_update registers the merged schema as version 2", test_accept_safe_update_increments_version)


def test_accept_refuses_unsafe_or_stale():
    tmp, reg = _registry_with(db(C("a", T.INTEGER)))
    with tmp:
        base = reg.get_registered_schema("orders").data
        breaking = diff(db(C("a", T.INTEGER)), db())
        res = reg.accept_safe_update("orders", breaking, base["version"])
        assert not res.success and "No safe update" in res.error
        safe = diff(db(C("a", T.INTEGER)), db(C("a", T.BIGINT)))
        assert reg.register_schema("orders", db(C("a", T.INTEGER), captured_at="later")).success
        res = reg.accept_safe_update("orders", safe, base["version"])
        assert not res.success and "changed since this check" in res.error, res.error
        assert reg.get_registered_schema("orders").data["version_count"] == 2


test("accept_safe_update refuses BREAKING diffs and stale baselines", test_accept_refuses_unsafe_or_stale)


def test_impact_analysis_array_names():
    with tempfile.TemporaryDirectory() as tmp:
        reg = LocalFileRegistry(tmp)
        reg.register_schema("orders", rest(infer("tags", [1]), infer("id", 1)))
        reg.subscribe_consumer("bi", "orders", ["tags"])
        d = compare_schemas(rest(infer("tags", [1]), infer("id", 1)),
                            rest(infer("tags", ["a"]), infer("id", 1))).data["diff"]
        changes = [ColumnChange(**c) for c in d["changes"]]
        res = reg.get_consumers_affected_by_diff("orders", SchemaDiff("orders", None, None, changes))
        assert [a["consumer"] for a in res.data["affected"]] == ["bi"], res.data


test("impact analysis maps 'tags[]' changes to the 'tags' subscription", test_impact_analysis_array_names)


# ─────────────────────────────────────────────
# CLI and MCP
# ─────────────────────────────────────────────

print("\nCLI --update and MCP accept_safe_schema_update")


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
        assert code == 1 and "--update ignored" in out.getvalue(), out.getvalue()
        assert registry.get_registered_schema("orders").data["version_count"] == 1


test("--update does not apply BREAKING drift", test_cli_update_ignored_for_breaking)


def test_mcp_accept_tool():
    import json
    from shelfard import mcp_server, registry
    from shelfard.models import ToolResult
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(registry._default, "_root", Path(tmp)):
        registry.register_schema("orders", rest(infer("name", None)))
        base = registry.get_registered_schema("orders").data
        d = diff(TableSchema.from_dict(base["schema"]), rest(infer("name", "Ann")))
        fake = ToolResult(success=True, data={"diff": d, "baseline_version": base["version"]})
        with mock.patch.object(mcp_server, "_run_checker", return_value=fake):
            out = json.loads(mcp_server.accept_safe_schema_update("orders"))
        assert out["success"] and out["data"]["version_count"] == 2, out
        assert mcp_server.accept_safe_schema_update in mcp_server.WRITE_TOOLS
        assert mcp_server.accept_safe_schema_update not in mcp_server.READ_TOOLS


test("MCP accept_safe_schema_update re-runs the check and saves version 2", test_mcp_accept_tool)


def test_agent_requires_approval():
    from shelfard import agent
    assert "accept_safe_schema_update" in agent.WRITE_TOOLS


test("agent asks for approval before accept_safe_schema_update", test_agent_requires_approval)


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
