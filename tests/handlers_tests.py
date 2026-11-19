"""
Command/query handlers: dispatcher routing, Response envelope, typed results, error
codes, the CQRS split of checks (CheckDrift query + AcceptUpdate command), MCP-only
rules behind via="mcp", and registry injection.

Run: conda run -n shelfard python3 tests/handlers_tests.py
"""

import json
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import LocalFileRegistry  # noqa: E402
from shelfard.handlers import (  # noqa: E402
    VIA_MCP, AcceptUpdate, CheckContract, CheckDrift, Command, Dispatcher, ErrorCode,
    ExportContract, GetProbe, GetSchema, GetVar, ListFileTables, ListProbes, ListSchemas,
    ListSubscriptions, Query, RefreshConsumer, RegisterProbe, Response, SetVar, SnapshotSchema,
    Subscribe, UnsetVar, classify_error, registered_payloads,
)
from shelfard.models import (  # noqa: E402
    ChangeSeverity, ConsumerImpact, FileProbeConfig, PostgresProbeConfig, RestProbeConfig,
    SchemaDiff, ToolResult,
)

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


def avro(path: Path, *fields):
    path.write_text(json.dumps({"type": "record", "name": "R",
                                "fields": [{"name": n, "type": t} for n, t in fields]}))
    return path


def setup(tmp: Path):
    """An injected registry (not the shared default) with orders v1 from an Avro file."""
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    src = avro(tmp / "o.avsc", ("id", "int"), ("qty", "int"), ("status", "string"))
    resp = d.dispatch(SnapshotSchema(FileProbeConfig(schema_name="orders", path=str(src), env=[]),
                                     create_probe=True))
    assert resp.ok, resp.error
    return d, src


def v2(src: Path):
    """id/qty widened, status removed, note added."""
    avro(src, ("id", "long"), ("qty", "long"), ("note", ["null", "string"]))


# ─────────────────────────────────────────────
section("Core: dispatch and Response")


def test_every_payload_has_a_handler(tmp):
    payloads = registered_payloads()
    assert len(payloads) >= 22, len(payloads)
    for p in payloads:
        assert issubclass(p, (Command, Query)), p


test("every payload is a Command or Query with one handler", test_every_payload_has_a_handler)


def test_unknown_payload(tmp):
    @dataclass(frozen=True)
    class Nope(Query):
        pass
    try:
        Dispatcher(LocalFileRegistry(tmp)).dispatch(Nope())
    except TypeError as e:
        assert "Nope" in str(e)
    else:
        raise AssertionError("expected TypeError")


test("an unregistered payload raises TypeError", test_unknown_payload)


def test_response_envelopes(tmp):
    ok = Response.success({"a": 1}, hint="next")
    assert ok.ok and ok.to_dict() == {"ok": True, "result": {"a": 1}, "error": None, "hint": "next"}
    legacy = json.loads(ok.to_tool_json())
    assert legacy == {"success": True, "data": {"a": 1}, "error": None, "next_action_hint": "next"}
    bad = Response.fail(ErrorCode.NOT_FOUND, "missing", "do x", target="t")
    assert not bad.ok and bad.to_dict()["error"] == {
        "code": "not_found", "message": "missing", "hint": "do x", "details": {"target": "t"}}
    assert json.loads(bad.to_tool_json()) == {
        "success": False, "data": None, "error": "missing", "next_action_hint": "do x"}
    adapted = Response.from_tool_result(ToolResult(success=False, error="No probe registered for 'x'."))
    assert adapted.error.code == ErrorCode.NOT_FOUND


test("Response serialises to its own and the legacy ToolResult envelope", test_response_envelopes)


def test_classify(tmp):
    assert classify_error("No registered schema found for 'x'.") == ErrorCode.NOT_FOUND
    assert classify_error("Failed to read live schema: Path not found: /x") == ErrorCode.FAILED
    assert classify_error("Invalid schema name") == ErrorCode.INVALID


test("registry error messages map to error codes", test_classify)


# ─────────────────────────────────────────────
section("Schemas")


def test_get_schema(tmp):
    d, _ = setup(tmp)
    resp = d.dispatch(GetSchema("orders", with_probe=True))
    assert resp.ok and resp.result.version_count == 1, resp
    assert [c.name for c in resp.result.schema.columns] == ["id", "qty", "status"]
    assert resp.result.probe.probe_type == "file"
    assert d.dispatch(GetSchema("nope")).error.code == ErrorCode.NOT_FOUND
    assert [s.name for s in d.dispatch(ListSchemas()).result.schemas] == ["orders"]


test("GetSchema returns a typed SchemaVersion; NOT_FOUND when missing", test_get_schema)


def test_snapshot_registers_probe_with_declared_env(tmp):
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    avro(tmp / "o.avsc", ("id", "int"))
    with mock.patch.dict(os.environ, {"DATA_DIR": str(tmp)}):
        resp = d.dispatch(SnapshotSchema(
            FileProbeConfig(schema_name="o", path="$DATA_DIR/o.avsc", env=[]), create_probe=True))
    assert resp.ok, resp.error
    assert resp.result.target == f"{tmp}/o.avsc" and resp.result.version_count == 1
    probe = d.dispatch(GetProbe("o")).result.config
    assert probe.path == "$DATA_DIR/o.avsc" and probe.env == ["DATA_DIR"], probe


test("SnapshotSchema reads ad hoc and stores the raw path with declared env", test_snapshot_registers_probe_with_declared_env)


def test_snapshot_read_failure(tmp):
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    resp = d.dispatch(SnapshotSchema(FileProbeConfig(schema_name="o", path=str(tmp / "x.avsc"), env=[])))
    assert resp.error.code == ErrorCode.FAILED and resp.error.details["target"] == str(tmp / "x.avsc")
    q = d.dispatch(SnapshotSchema(PostgresProbeConfig(schema_name="q", dsn="x", env=[], query="select {{a}}")))
    assert q.error.code == ErrorCode.INVALID and "target" not in q.error.details


test("SnapshotSchema: unreadable source → FAILED, templated SQL → INVALID", test_snapshot_read_failure)


# ─────────────────────────────────────────────
section("Drift: CheckDrift query + AcceptUpdate command")


def test_check_ad_hoc_equals_registered(tmp):
    d, src = setup(tmp)
    v2(src)
    ad_hoc = d.dispatch(CheckDrift("orders", FileProbeConfig(schema_name="orders", path=str(src), env=[])))
    stored = d.dispatch(CheckDrift("orders"))
    assert ad_hoc.ok and stored.ok, (ad_hoc.error, stored.error)
    assert isinstance(ad_hoc.result.diff, SchemaDiff)
    def comparable(diff):        # each read has its own captured_at timestamp
        d = diff.to_dict()
        d.pop("new_schema_version")
        d["suggested_update"]["schema"].pop("captured_at")
        return d
    assert comparable(ad_hoc.result.diff) == comparable(stored.result.diff)
    assert stored.result.target == str(src) and stored.result.source_kind == "file"
    assert stored.result.baseline_version_number == 1
    # a query never writes
    assert d.dispatch(GetSchema("orders")).result.version_count == 1


test("CheckDrift: ad-hoc source and registered probe give the same typed diff", test_check_ad_hoc_equals_registered)


def test_check_errors(tmp):
    d, src = setup(tmp)
    missing = d.dispatch(CheckDrift("nosnap", FileProbeConfig(schema_name="nosnap", path=str(src), env=[])))
    assert missing.error.code == ErrorCode.NOT_FOUND and missing.error.details["target"] == str(src)
    assert d.dispatch(CheckDrift("noprobe")).error.code == ErrorCode.NOT_FOUND


test("CheckDrift: no baseline / no probe → NOT_FOUND", test_check_errors)


def test_accept_rules(tmp):
    d, src = setup(tmp)
    d.dispatch(Subscribe("report", "orders", ("id", "qty")))
    v2(src)
    report = d.dispatch(CheckDrift("orders")).result
    assert report.diff.suggested_update["kind"] == "unaffected"
    refused = d.dispatch(AcceptUpdate("orders", report.diff, report.baseline_version))
    assert refused.error.code == ErrorCode.REFUSED
    assert refused.error.details["reason"] == "needs_allow_unaffected"
    stale = d.dispatch(AcceptUpdate("orders", report.diff, "1999-01-01", allow_unaffected=True))
    assert stale.error.code == ErrorCode.CONFLICT, stale.error
    done = d.dispatch(AcceptUpdate("orders", report.diff, report.baseline_version, allow_unaffected=True))
    assert done.ok and done.result.version_count == 2, done.error
    assert done.result.outdated_consumers == [
        {"consumer": "report", "severity": ChangeSeverity.SAFE, "changes": ["id", "qty"]}]
    refreshed = d.dispatch(RefreshConsumer("report", "orders"))
    assert [c["column_name"] for c in refreshed.result.adopted_changes] == ["id", "qty"]


test("AcceptUpdate: unaffected needs the flag, stale baseline → CONFLICT, outdated consumers listed", test_accept_rules)


def test_accept_refused_when_consumer_breaks(tmp):
    d, src = setup(tmp)
    d.dispatch(Subscribe("crm", "orders", ("status",)))
    v2(src)
    report = d.dispatch(CheckDrift("orders")).result
    removed = next(c for c in report.diff.changes if c.column_name == "status")
    assert removed.consumer_impact == ConsumerImpact.CONSUMER_CONSTRAINED
    assert report.diff.suggested_update is None
    resp = d.dispatch(AcceptUpdate("orders", report.diff, report.baseline_version, allow_unaffected=True))
    assert resp.error.code == ErrorCode.REFUSED and resp.error.details["reason"] == "no_update"


test("AcceptUpdate refuses BREAKING drift a consumer reads", test_accept_refused_when_consumer_breaks)


def test_schema_diff_round_trip(tmp):
    d, src = setup(tmp)
    d.dispatch(Subscribe("report", "orders", ("id",)))
    v2(src)
    tr = d.registry.run_probe("orders")
    raw = tr.data["diff"]
    assert SchemaDiff.from_dict(raw).to_dict() == raw
    assert SchemaDiff.from_dict(json.loads(json.dumps(raw, default=str))).to_dict() == raw


test("SchemaDiff.from_dict inverts to_dict (also from JSON)", test_schema_diff_round_trip)


# ─────────────────────────────────────────────
section("Consumers")


def test_subscriptions_with_state(tmp):
    d, src = setup(tmp)
    assert d.dispatch(Subscribe("bad", "orders", ("nope",))).error.code == ErrorCode.INVALID
    d.dispatch(Subscribe("report", "orders", ("id",)))
    listed = d.dispatch(ListSubscriptions(table="orders", with_state=True)).result
    assert [v.consumer for v in listed.consumers] == ["report"] and listed.consumers[0].state.up_to_date
    assert listed.to_dict()["total"] == 1
    v2(src)
    report = d.dispatch(CheckDrift("orders")).result
    d.dispatch(AcceptUpdate("orders", report.diff, report.baseline_version, allow_unaffected=True))
    state = d.dispatch(ListSubscriptions(with_state=True)).result.consumers[0].state
    assert not state.up_to_date and state.severity == ChangeSeverity.SAFE and state.columns == ["id"]


test("ListSubscriptions reports each projection's state against the latest schema", test_subscriptions_with_state)


# ─────────────────────────────────────────────
section("Probes and variables (CLI vs MCP)")


def test_register_probe_rules(tmp):
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    sql = PostgresProbeConfig(schema_name="q", dsn="x", env=[], query="select {{a}}")
    assert d.dispatch(RegisterProbe(sql)).error.code == ErrorCode.INVALID
    rest = RestProbeConfig(schema_name="u", url="https://93.184.216.34/x", headers=[], env=[])
    with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        first = d.dispatch(RegisterProbe(rest, via=VIA_MCP))
        assert first.ok, first.error
        assert d.dispatch(GetProbe("u")).result.config.registered_via == "mcp"
        assert d.dispatch(RegisterProbe(rest, via=VIA_MCP)).error.code == ErrorCode.CONFLICT
    assert d.dispatch(RegisterProbe(rest)).ok                    # CLI may overwrite
    file_probe = FileProbeConfig(schema_name="f", path="/tmp/x.avsc", env=[])
    assert d.dispatch(RegisterProbe(file_probe, via=VIA_MCP)).error.code == ErrorCode.REFUSED
    leaky = RestProbeConfig(schema_name="k", url="https://x.example/?k=$HOME", headers=[], env=["HOME"])
    assert d.dispatch(RegisterProbe(leaky, via=VIA_MCP)).error.code == ErrorCode.REFUSED
    assert [p.schema_name for p in d.dispatch(ListProbes()).result.probes] == ["u"]


test("RegisterProbe: SQL templates INVALID; via=mcp marks, refuses overwrite/file/non-allowlisted env", test_register_probe_rules)


def test_vars_via_mcp(tmp):
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    d.dispatch(RegisterProbe(RestProbeConfig(schema_name="r", url="https://{{host}}/x", headers=[], env=[])))
    assert d.dispatch(SetVar("host", "a.example")).ok
    assert d.dispatch(SetVar("host", "evil.example", via=VIA_MCP)).error.code == ErrorCode.REFUSED
    assert d.dispatch(UnsetVar("host", via=VIA_MCP)).error.code == ErrorCode.REFUSED
    assert d.dispatch(GetVar("host")).result.value == "a.example"
    assert d.dispatch(SetVar("other", "v", via=VIA_MCP)).ok
    assert d.dispatch(GetVar("missing")).error.code == ErrorCode.NOT_FOUND


test("SetVar/UnsetVar via=mcp refuse variables used by a probe", test_vars_via_mcp)


# ─────────────────────────────────────────────
section("Contracts and discovery")


def test_contract_check(tmp):
    d, src = setup(tmp)
    contract = d.dispatch(ExportContract("orders")).result.contract
    avro(src, ("id", "int"), ("qty", "int"), ("status", "string"), ("extra", ["null", "int"]))
    report = d.dispatch(CheckDrift("orders")).result
    d.dispatch(AcceptUpdate("orders", report.diff, report.baseline_version))
    check = d.dispatch(CheckContract(contract, name="orders")).result
    assert [c.column_name for c in check.diff.changes] == ["extra"]
    assert check.updated_contract is not None and check.new_version == "1.0.1", check.new_version
    assert contract.get("version") == "1.0.0"                    # input left untouched
    nope = d.dispatch(CheckContract(contract, name="orders", object="nope"))
    assert nope.error.code == ErrorCode.NOT_FOUND and nope.error.details["objects"] == ["orders"]
    assert d.dispatch(CheckContract(contract)).error.code == ErrorCode.INVALID


test("CheckContract returns the diff and the updated contract (written by the caller)", test_contract_check)


def test_list_files(tmp):
    d = Dispatcher(LocalFileRegistry(tmp / "reg"))
    avro(tmp / "a.avsc", ("id", "int"))
    listing = d.dispatch(ListFileTables(str(tmp))).result
    assert [(t.name, t.file_format) for t in listing.tables] == [("a.avsc", "avro")], listing


test("ListFileTables returns typed entries", test_list_files)


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
