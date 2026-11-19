"""
Consumer projections and consumer impact: path projection, per-consumer
severity, change labels (no_consumers / consumer_constrained), "N of M
consumers affected", update kinds (safe / unaffected), accept rules, consumer
refresh, CLI output and flags, MCP, and the storage migration script.

Run: conda run -n shelfard python3 tests/consumer_tests.py
"""

import io
import json
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import LocalFileRegistry  # noqa: E402
from shelfard.models import (  # noqa: E402
    ColumnSchema as C, ColumnType as T, ConsumerImpact, ConsumerProjection, TableSchema,
)
from shelfard.projection import consumer_diff, missing_paths, parse_path, project, touches  # noqa: E402

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


def orders(*cols, **kw):
    return TableSchema("orders", list(cols), source=kw.pop("source", "postgresql"), **kw)


V1 = orders(
    C("order_id", T.BIGINT, nullable=False),
    C("customer", T.STRUCT, fields=[C("id", T.BIGINT, nullable=False), C("email", T.VARCHAR)]),
    C("quantity", T.INTEGER),
    C("status", T.VARCHAR),
    C("tags", T.ARRAY, items=C("item", T.VARCHAR)),
    partition_keys=["order_id"],
)
V2 = orders(                                  # status removed, customer.email removed,
    C("order_id", T.BIGINT, nullable=False),  # quantity widened, discount added,
    C("customer", T.STRUCT, fields=[C("id", T.BIGINT, nullable=False)]),   # partition changed
    C("quantity", T.BIGINT),
    C("tags", T.ARRAY, items=C("item", T.VARCHAR)),
    C("discount", T.FLOAT),
    partition_keys=["quantity"],
)


def reg_with(tmp, schema=V1) -> LocalFileRegistry:
    r = LocalFileRegistry(tmp)
    assert r.register_schema("orders", schema).success
    return r


def impact(r, old=V1, new=V2) -> dict:
    res = r.compare_with_baseline("orders", old.to_dict(), new.to_dict())
    assert res.success, res.error
    return res.data["diff"]


def by_name(d) -> dict:
    return {c["column_name"]: c for c in d["changes"]}


# ─────────────────────────────────────────────
# Paths and projection
# ─────────────────────────────────────────────

section("Paths and projection")


def test_paths(tmp):
    assert parse_path("tags[].sku") == ["tags", "[]", "sku"]
    assert parse_path("address.zip") == ["address", "zip"]
    assert parse_path("m[][]") == ["m", "[]", "[]"]
    assert touches("customer.email", ["customer"])            # nested under a read column
    assert touches("customer", ["customer.email"])            # parent of a read field
    assert not touches("customer.id", ["customer.email"])
    assert touches("tags[]", ["tags"]) and not touches("tag", ["tags"])
    assert not touches("<partition_keys>", ["order_id"]) and not touches("<multiple>", ["a"])


test("path parsing and touches()", test_paths)


def test_project(tmp):
    p = project(V1, ["quantity", "customer.email", "order_id"])
    assert [c.name for c in p.columns] == ["quantity", "customer", "order_id"]     # path order
    assert [f.name for f in p.columns[1].fields] == ["email"]
    assert p.partition_keys == []
    assert [c.name for c in project(V1, ["customer", "customer.email"]).columns[0].fields] == ["id", "email"]
    assert missing_paths(V1, ["customer.email", "tags[]", "nope", "customer.zip", "status[]"]) == \
        ["nope", "customer.zip", "status[]"]


test("project() keeps only the paths, in path order; missing_paths()", test_project)


def test_consumer_severity(tmp):
    def proj(*paths):
        return ConsumerProjection("c", "orders", list(paths), project(V1, paths).columns, source="postgresql")
    assert consumer_diff(proj("quantity"), V2)["overall_severity"] == "SAFE"          # int → bigint
    assert consumer_diff(proj("status"), V2)["overall_severity"] == "BREAKING"        # removed
    assert consumer_diff(proj("customer.email"), V2)["overall_severity"] == "BREAKING"
    assert consumer_diff(proj("customer.id", "order_id"), V2)["changes"] == []        # untouched


test("each consumer gets its own severity", test_consumer_severity)


# ─────────────────────────────────────────────
# Impact labels and summary
# ─────────────────────────────────────────────

section("Impact labels and summary")


def test_labels_and_counts(tmp):
    r = reg_with(tmp)
    r.subscribe_consumer("report", "orders", ["order_id", "quantity"])
    r.subscribe_consumer("crm", "orders", ["customer.email"])
    r.subscribe_consumer("ops", "orders", ["order_id"])
    d = impact(r)
    c = by_name(d)
    assert c["status"]["consumer_impact"] == ConsumerImpact.NO_CONSUMERS
    assert c["customer.email"]["consumer_impact"] == ConsumerImpact.CONSUMER_CONSTRAINED
    assert c["customer.email"]["consumers"] == [{"consumer": "crm", "severity": "BREAKING"}]
    assert c["quantity"]["consumers"] == [{"consumer": "report", "severity": "SAFE"}]
    assert c["discount"]["consumer_impact"] == ConsumerImpact.NO_CONSUMERS
    assert c["<partition_keys>"]["consumer_impact"] == ConsumerImpact.NO_CONSUMERS
    s = d["consumer_summary"]
    assert (s["total"], s["affected"]) == (3, 2), s
    assert s["by_severity"] == {"SAFE": 1, "WARNING": 0, "BREAKING": 1}
    assert d["suggested_update"] is None                       # crm would break


test("changes labelled; '2 of 3 consumers affected' with own severities", test_labels_and_counts)


def test_zero_consumers(tmp):
    d = impact(reg_with(tmp))
    assert d["consumer_summary"]["total"] == 0 and d["consumer_summary"]["affected"] == 0
    assert all(c["consumer_impact"] == "no_consumers" for c in d["changes"])
    assert d["suggested_update"]["kind"] == "unaffected"
    assert any("BREAKING, no consumer reads it" in line for line in d["suggested_update"]["explanation"])


test("no consumers: BREAKING changes are 'no consumers'; update kind 'unaffected'", test_zero_consumers)


def test_safe_kind(tmp):
    r = reg_with(tmp)
    r.subscribe_consumer("report", "orders", ["quantity"])
    new = orders(*[c for c in V1.columns] + [C("note", T.TEXT)], partition_keys=["order_id"])
    d = impact(r, V1, new)
    assert d["suggested_update"]["kind"] == "safe"
    assert d["consumer_summary"]["affected"] == 0


test("all-SAFE diff → update kind 'safe'", test_safe_kind)


# ─────────────────────────────────────────────
# Accepting updates and refreshing projections
# ─────────────────────────────────────────────

section("accept_update and consumer refresh")


def test_accept_rules(tmp):
    r = reg_with(tmp)
    r.subscribe_consumer("report", "orders", ["order_id", "quantity"])
    base = r.get_registered_schema("orders").data["version"]
    d = impact(r)
    assert d["suggested_update"]["kind"] == "unaffected"
    res = r.accept_update("orders", d, base)
    assert not res.success and "allow_unaffected" in res.error
    before = r.get_consumer_subscription("report", "orders").data["subscription"]
    # A consumer subscribing to a removed column after the check blocks the update.
    r.subscribe_consumer("crm", "orders", ["status"])
    res = r.accept_update("orders", d, base, allow_unaffected=True)
    assert not res.success and "crm" in res.error, res.error
    r.unsubscribe_consumer("crm", "orders")
    res = r.accept_update("orders", d, base, allow_unaffected=True)
    assert res.success, res.error
    assert res.data["version_count"] == 2 and res.data["kind"] == "unaffected"
    assert [o["consumer"] for o in res.data["outdated_consumers"]] == ["report"]
    # Projections are never changed by baseline updates.
    assert r.get_consumer_subscription("report", "orders").data["subscription"] == before


test("unaffected update needs allow_unaffected, re-checks consumers, leaves projections", test_accept_rules)


def test_refresh(tmp):
    r = reg_with(tmp)
    r.subscribe_consumer("report", "orders", ["quantity"])
    r.subscribe_consumer("crm", "orders", ["status"])
    r.register_schema("orders", V2)                            # baseline moved on
    d = impact(r, V2, V2)                                      # no baseline drift…
    outdated = {o["consumer"]: o for o in d["consumer_summary"]["outdated"]}
    assert outdated["report"]["severity"] == "SAFE" and outdated["crm"]["severity"] == "BREAKING"
    res = r.refresh_consumer("report", "orders")
    assert res.success and res.data["adopted_changes"], res
    exp = r.get_consumer_subscription("report", "orders").data["subscription"]["expected"]
    assert exp[0]["col_type"] == "bigint"
    assert "report" not in {o["consumer"] for o in impact(r, V2, V2)["consumer_summary"]["outdated"]}
    res = r.refresh_consumer("crm", "orders")
    assert not res.success and "status" in res.error and "re-subscribe" in res.error


test("outdated projections reported; refresh adopts new shape or refuses on removed paths", test_refresh)


# ─────────────────────────────────────────────
# CLI and MCP
# ─────────────────────────────────────────────

section("CLI and MCP")


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


def _file_probe(tmp, r_root, name, fields):
    avsc = tmp / f"{name}.avsc"
    avsc.write_text(json.dumps({"type": "record", "name": "R", "fields": fields}))
    return avsc


def test_cli_flow(tmp):
    from shelfard import registry
    fields_v1 = [{"name": "id", "type": "long"}, {"name": "qty", "type": "int"},
                 {"name": "status", "type": "string"}]
    path = _file_probe(tmp, tmp, "o", fields_v1)
    with mock.patch.object(registry._default, "_root", tmp / "reg"):
        assert run_cli("file", "snapshot", str(path), "--name", "o")[0] == 0
        assert run_cli("subscribe", "o", "--consumer", "report", "--columns", "id,qty")[0] == 0
        assert run_cli("probe", "register", "o", "--type", "file", "--path", str(path))[0] == 0
        path.write_text(json.dumps({"type": "record", "name": "R", "fields": [
            {"name": "id", "type": "long"}, {"name": "qty", "type": "long"}]}))

        code, out = run_cli("probe", "run", "o")
        assert code == 1, out
        assert "[BREAKING · no consumers]" in out and "'status'" in out, out
        assert "[SAFE · consumer constrained (1)]" in out, out
        assert "Consumers: 1 of 1 affected — 1 SAFE" in out, out
        assert "report" not in out, out          # counts only, no consumer names
        assert "--details" in out and "--update --allow-unaffected" in out, out
        code, out = run_cli("probe", "run", "o", "--details")
        assert "Consumers: report SAFE" in out and "• SAFE report: qty" in out, out

        code, out = run_cli("probe", "run", "o", "--update")
        assert code == 1 and "--update alone only applies all-SAFE" in out, out
        code, out = run_cli("probe", "run", "o", "--update", "--allow-unaffected")
        assert code == 0 and "is now version 2" in out, out
        assert "1 now differs from the baseline (1 SAFE)" in out and "report" not in out, out
        assert "shelfard consumer refresh o --consumer NAME" in out, out

        code, out = run_cli("probe", "run", "o")
        assert code == 0 and "No drift" in out and "Outdated projections: 1 (1 SAFE)" in out, out
        code, out = run_cli("probe", "run", "o", "--details")
        assert "• SAFE report: qty" in out, out
        code, out = run_cli("consumers", "o")
        assert "report" in out and "outdated (SAFE: qty)" in out, out
        code, out = run_cli("consumer", "refresh", "o", "--consumer", "report")
        assert code == 0 and "adopted" in out, out
        code, out = run_cli("probe", "run", "o")
        assert code == 0 and "Outdated" not in out, out
        code, out = run_cli("list", "subscriptions")
        assert "up to date" in out, out
        assert run_cli("unsubscribe", "o", "--consumer", "report")[0] == 0
        assert run_cli("subscribe", "o", "--consumer", "x", "--columns", "nope")[0] == 2


test("CLI: labels, 'N of M', --allow-unaffected, outdated + consumer refresh", test_cli_flow)


def test_mcp_accept(tmp):
    from shelfard import mcp_server, registry
    from shelfard.models import ToolResult
    with mock.patch.object(registry._default, "_root", tmp):
        registry.register_schema("orders", V1)
        base = registry.get_registered_schema("orders").data["version"]
        d = registry.compare_with_baseline("orders", V1.to_dict(), V2.to_dict()).data["diff"]
        fake = ToolResult(success=True, data={"diff": d, "baseline_version": base})
        with mock.patch.object(mcp_server, "_run_probe", return_value=fake):
            out = json.loads(mcp_server.accept_schema_update("orders"))
            assert not out["success"] and "allow_unaffected" in out["error"], out
            out = json.loads(mcp_server.accept_schema_update("orders", allow_unaffected=True))
        assert out["success"] and out["data"]["version_count"] == 2, out
        assert not hasattr(mcp_server, "refresh_consumer")


test("MCP accept_schema_update(allow_unaffected=True); no refresh tool", test_mcp_accept)


# ─────────────────────────────────────────────
# Storage migration
# ─────────────────────────────────────────────

section("Storage migration")


def test_migration(tmp):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import migrate_storage

    root = tmp / "schemas"
    (root / "checkers").mkdir(parents=True)
    (root / "checkers" / "users.json").write_text(json.dumps(
        {"schema_name": "users", "url": "https://x", "headers": [], "env": [], "checker_type": "rest"}))
    (root / "consumers" / "crm").mkdir(parents=True)
    old_sub = {"consumer_name": "crm", "source_table": "users", "versions": [
        {"consumer_name": "crm", "source_table": "users", "subscribed_columns": ["email"],
         "schema": {"table_name": "users", "columns": [{"name": "email", "col_type": "varchar",
                                                        "nullable": False}]},
         "subscribed_at": "2026-01-01", "source_schema_version": "v1"}]}
    (root / "consumers" / "crm" / "users.json").write_text(json.dumps(old_sub))

    done = migrate_storage.migrate(root)
    assert len(done) == 2, done
    assert list(tmp.glob("schemas.bak-*")), "backup missing"
    assert not (root / "checkers").exists()
    probe = json.loads((root / "probes" / "users.json").read_text())
    assert probe["probe_type"] == "rest" and "checker_type" not in probe
    sub = json.loads((root / "consumers" / "crm" / "users.json").read_text())
    assert sub["columns"] == ["email"] and sub["expected"][0]["name"] == "email" and "versions" not in sub
    assert migrate_storage.migrate(root) == []                 # idempotent
    assert len(list(tmp.glob("schemas.bak-*"))) == 1
    r = LocalFileRegistry(root)
    assert r.get_probe("users").data["probe"]["probe_type"] == "rest"
    assert r.get_consumer_subscription("crm", "users").success


test("migrate_storage: backup, checkers → probes, consumer files flattened, idempotent", test_migration)


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
