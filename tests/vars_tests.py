"""
Unit tests for template variable storage and {{var_name}} resolution.

Run: conda run -n shelfard python3 tests/vars_tests.py
"""

import os
import sys
import tempfile
import traceback
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import LocalFileRegistry
from shelfard.models import RestCheckerConfig, PostgresCheckerConfig

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
    except Exception as e:
        print(f"  ✗ {name} [ERROR]")
        errors.append((name, traceback.format_exc()))
        failed += 1


def section(name):
    print(f"\n── {name} ──")


# ─────────────────────────────────────────────
# Var Storage
# ─────────────────────────────────────────────

section("Var Storage")


def test_set_var_stores_value():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.set_var("db_host", "localhost")
        assert result.success, result.error
        assert result.data["name"] == "db_host"
        assert result.data["value"] == "localhost"

test("set_var stores value and returns success", test_set_var_stores_value)


def test_get_var_retrieves_stored_value():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("todo_host", "http://localhost:8080")
        result = r.get_var("todo_host")
        assert result.success, result.error
        assert result.data["name"] == "todo_host"
        assert result.data["value"] == "http://localhost:8080"

test("get_var retrieves stored value", test_get_var_retrieves_stored_value)


def test_get_var_unknown_returns_failure():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.get_var("nonexistent")
        assert not result.success
        assert "nonexistent" in result.error
        assert result.next_action_hint is not None

test("get_var unknown name returns failure with hint", test_get_var_unknown_returns_failure)


def test_set_var_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("api_host", "http://old-host.com")
        r.set_var("api_host", "http://new-host.com")
        result = r.get_var("api_host")
        assert result.success
        assert result.data["value"] == "http://new-host.com"

test("set_var overwrites existing value", test_set_var_overwrites)


def test_list_vars_returns_all():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("host_a", "http://a.com")
        r.set_var("host_b", "http://b.com")
        r.set_var("host_c", "http://c.com")
        result = r.list_vars()
        assert result.success, result.error
        vars_dict = result.data["vars"]
        assert vars_dict["host_a"] == "http://a.com"
        assert vars_dict["host_b"] == "http://b.com"
        assert vars_dict["host_c"] == "http://c.com"

test("list_vars returns all variables", test_list_vars_returns_all)


def test_list_vars_empty():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.list_vars()
        assert result.success, result.error
        assert result.data["vars"] == {}

test("list_vars on empty registry returns empty dict", test_list_vars_empty)


def test_delete_var_removes():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("to_remove", "value")
        del_result = r.delete_var("to_remove")
        assert del_result.success, del_result.error
        assert del_result.data["deleted"] == "to_remove"
        get_result = r.get_var("to_remove")
        assert not get_result.success

test("delete_var removes variable", test_delete_var_removes)


def test_delete_var_nonexistent():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.delete_var("ghost")
        assert not result.success
        assert "ghost" in result.error

test("delete_var nonexistent name returns failure", test_delete_var_nonexistent)


# ─────────────────────────────────────────────
# Template Resolution
# ─────────────────────────────────────────────

section("Template Resolution")


def test_resolve_template_replaces_known():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("host", "api.example.com")
        result = r.resolve_template("https://{{host}}/v1")
        assert result == "https://api.example.com/v1", repr(result)

test("resolve_template replaces known var", test_resolve_template_replaces_known)


def test_resolve_template_leaves_unknown_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.resolve_template("http://{{unknown}}/path")
        assert result == "http://{{unknown}}/path", repr(result)

test("resolve_template leaves unknown vars unchanged", test_resolve_template_leaves_unknown_unchanged)


def test_resolve_template_multiple_vars():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("host", "localhost")
        r.set_var("port", "5432")
        result = r.resolve_template("{{host}}:{{port}}")
        assert result == "localhost:5432", repr(result)

test("resolve_template resolves multiple vars in one string", test_resolve_template_multiple_vars)


def test_resolve_template_no_placeholders():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        template = "http://hardcoded.example.com/api"
        result = r.resolve_template(template)
        assert result == template

test("resolve_template with no placeholders is a no-op", test_resolve_template_no_placeholders)


# ─────────────────────────────────────────────
# Checker Integration
# ─────────────────────────────────────────────

section("Checker Integration")


def test_rest_checker_resolves_template_vars():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("base_url", "http://localhost:8080")

        from shelfard.tools.rest.checker import RestChecker
        config = RestCheckerConfig(
            schema_name="test_api",
            url="{{base_url}}/api/v1",
            headers=[],
            env=[],
        )

        captured_url = []

        class _FakeResult:
            success = False
            error = "abort_early"
            data = {}

        class _FakeReader:
            def __init__(self, url, *args, **kwargs):
                captured_url.append(url)
            def get_schema(self):
                return _FakeResult()

        checker = RestChecker(config, r)
        with mock.patch("shelfard.tools.rest.checker.RestEndpointReader", _FakeReader):
            checker.run()

        assert len(captured_url) == 1, "Reader was not called"
        assert captured_url[0] == "http://localhost:8080/api/v1", repr(captured_url[0])

test("RestChecker resolves {{vars}} in URL before $ENV_VAR", test_rest_checker_resolves_template_vars)


def test_postgres_checker_resolves_template_vars():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("db_host", "localhost")

        from shelfard.tools.postgres.checker import PostgresChecker
        config = PostgresCheckerConfig(
            schema_name="test_db",
            dsn="postgresql://user@{{db_host}}/mydb",
            env=[],
            table="users",
        )

        captured_dsn = []

        class _FakeResult:
            success = False
            error = "abort_early"
            data = {}

        class _FakeReader:
            def __init__(self, dsn, *args, **kwargs):
                captured_dsn.append(dsn)
            def get_schema(self):
                return _FakeResult()

        checker = PostgresChecker(config, r)
        with mock.patch("shelfard.tools.postgres.reader.PostgresReader", _FakeReader):
            checker.run()

        assert len(captured_dsn) == 1, "Reader was not called"
        assert captured_dsn[0] == "postgresql://user@localhost/mydb", repr(captured_dsn[0])

test("PostgresChecker resolves {{vars}} in DSN", test_postgres_checker_resolves_template_vars)


# ─────────────────────────────────────────────
# File Persistence
# ─────────────────────────────────────────────

section("File Persistence")


def test_vars_json_path():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.set_var("x", "1")
        assert (Path(tmp) / "vars.json").exists()

test("vars.json created at correct path", test_vars_json_path)


def test_vars_survive_reinstantiation():
    with tempfile.TemporaryDirectory() as tmp:
        r1 = LocalFileRegistry(tmp)
        r1.set_var("persist_me", "still_here")

        r2 = LocalFileRegistry(tmp)
        result = r2.get_var("persist_me")
        assert result.success, result.error
        assert result.data["value"] == "still_here"

test("vars survive registry re-instantiation", test_vars_survive_reinstantiation)


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
