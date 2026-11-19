"""
Unit tests for the schema registry and consumer subscriptions.

Run: conda run -n shelfard python3 tests/registry_tests.py
"""

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shelfard import (
    ColumnSchema, TableSchema, ColumnType, ChangeSeverity, ChangeType,
    LocalFileRegistry,
    get_sqlite_schema, register_schema, get_registered_schema,
    compare_schemas_from_dicts,
)
import shelfard.registry as registry

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
# Helpers
# ─────────────────────────────────────────────

def make_orders_v1():
    return TableSchema(
        table_name="orders",
        columns=[
            ColumnSchema("order_id",    ColumnType.INTEGER,   nullable=False),
            ColumnSchema("customer_id", ColumnType.INTEGER,   nullable=False),
            ColumnSchema("amount",      ColumnType.DECIMAL,   nullable=True, precision=18, scale=4),
            ColumnSchema("status",      ColumnType.VARCHAR,   nullable=True, max_length=50),
            ColumnSchema("created_at",  ColumnType.TIMESTAMP, nullable=False),
        ],
        source="test"
    )


def _make_users_schema():
    return TableSchema(
        table_name="users",
        columns=[
            ColumnSchema("id",         ColumnType.INTEGER,   nullable=False),
            ColumnSchema("email",      ColumnType.VARCHAR,   nullable=False),
            ColumnSchema("name",       ColumnType.TEXT,      nullable=True),
            ColumnSchema("created_at", ColumnType.TIMESTAMP, nullable=False),
        ],
        source="test",
    )


# ─────────────────────────────────────────────
# Schema Registry
# ─────────────────────────────────────────────

section("Schema Registry")


def test_register_and_retrieve():
    with tempfile.TemporaryDirectory() as tmp:
        registry._default._root = Path(tmp)
        schema = make_orders_v1()
        reg = register_schema("orders", schema)
        assert reg.success
        get = get_registered_schema("orders")
        assert get.success
        assert get.data["schema"]["table_name"] == "orders"
        assert len(get.data["schema"]["columns"]) == 5

test("register and retrieve schema", test_register_and_retrieve)


def test_unregistered_table():
    with tempfile.TemporaryDirectory() as tmp:
        registry._default._root = Path(tmp)
        result = get_registered_schema("nonexistent")
        assert not result.success
        assert result.next_action_hint is not None

test("unregistered table returns helpful error", test_unregistered_table)


def test_multiple_versions():
    with tempfile.TemporaryDirectory() as tmp:
        registry._default._root = Path(tmp)
        v1 = make_orders_v1()
        register_schema("orders", v1)
        v2 = TableSchema(
            table_name="orders",
            columns=v1.columns + [ColumnSchema("notes", ColumnType.TEXT, nullable=True)],
            source="test"
        )
        register_schema("orders", v2)
        result = get_registered_schema("orders", version="latest")
        assert result.success
        assert len(result.data["schema"]["columns"]) == 6

test("multiple versions — latest returns newest", test_multiple_versions)


# ─────────────────────────────────────────────
# End-to-End: SQLite → Registry → Compare
# ─────────────────────────────────────────────

section("End-to-End: SQLite → Registry → Compare")


def test_full_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        registry._default._root = Path(tmp)
        db = f"{tmp}/events.db"

        # Create v1
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE events (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type VARCHAR(100) NOT NULL,
                payload TEXT,
                occurred_at TIMESTAMP NOT NULL
            )
        """)
        conn.commit(); conn.close()

        # Introspect and register
        v1_result = get_sqlite_schema(db, "events")
        assert v1_result.success

        raw = v1_result.data["schema"]
        v1_schema = TableSchema(
            table_name=raw["table_name"],
            columns=[ColumnSchema(
                name=c["name"],
                col_type=ColumnType(c["col_type"]),
                nullable=c["nullable"],
                max_length=c.get("max_length"),
            ) for c in raw["columns"]],
            source="sqlite"
        )
        assert register_schema("events", v1_schema).success

        # v2 schema arrives as JSON (simulating Kafka or API payload)
        v2_dict = {
            "table_name": "events",
            "columns": [
                {"name": "event_id",   "col_type": "integer",   "nullable": False},
                {"name": "user_id",    "col_type": "integer",   "nullable": False},
                {"name": "event_type", "col_type": "varchar",   "nullable": False, "max_length": 100},
                {"name": "payload",    "col_type": "json",      "nullable": True},    # text→json: BREAKING
                {"name": "occurred_at","col_type": "timestamp", "nullable": False},
                {"name": "session_id", "col_type": "varchar",   "nullable": True, "max_length": 64},  # new: SAFE
            ]
        }

        registered = get_registered_schema("events")
        assert registered.success

        diff_result = compare_schemas_from_dicts(registered.data["schema"], v2_dict)
        assert diff_result.success

        diff = diff_result.data["diff"]
        change_types = [c["change_type"] for c in diff["changes"]]

        assert ChangeType.COLUMN_ADDED in change_types    # session_id
        assert ChangeType.TYPE_CHANGED in change_types    # payload: text → json
        assert diff["overall_severity"] == ChangeSeverity.BREAKING

        print(f"\n    {diff['summary']}")

test("full pipeline: introspect → register → detect drift", test_full_pipeline)


# ─────────────────────────────────────────────
# Consumer Subscriptions
# ─────────────────────────────────────────────

section("Consumer Subscriptions")


def test_subscribe_consumer_full():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.register_schema("users", _make_users_schema())
        result = r.subscribe_consumer("analytics", "users")
        assert result.success, result.error
        assert result.data["column_count"] == 4
        proj = result.data["projection"]
        assert proj["full"] is True
        assert proj["columns"] == ["id", "email", "name", "created_at"]

test("full subscription projects all top-level columns", test_subscribe_consumer_full)


def test_subscribe_consumer_projection():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.register_schema("users", _make_users_schema())
        result = r.subscribe_consumer("reporting", "users", columns=["email", "created_at"])
        assert result.success, result.error
        assert result.data["column_count"] == 2

        sub = r.get_consumer_subscription("reporting", "users").data["subscription"]
        assert sub["columns"] == ["email", "created_at"]
        assert [c["name"] for c in sub["expected"]] == ["email", "created_at"]
        assert sub["expected"][0]["col_type"] == "varchar"
        # Unversioned: subscribing again overwrites.
        r.subscribe_consumer("reporting", "users", columns=["id"])
        sub = r.get_consumer_subscription("reporting", "users").data["subscription"]
        assert sub["columns"] == ["id"] and "versions" not in sub

test("projection saves the expected shape of the requested columns (overwritten)",
     test_subscribe_consumer_projection)


def test_subscribe_consumer_unknown_source():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        result = r.subscribe_consumer("analytics", "nonexistent")
        assert not result.success
        assert "nonexistent" in result.error.lower() or "not registered" in result.error.lower()
        r.register_schema("users", _make_users_schema())
        result = r.subscribe_consumer("analytics", "users", columns=["email", "nope"])
        assert not result.success and "nope" in result.error

test("unregistered source or unknown column → error", test_subscribe_consumer_unknown_source)


def test_get_consumers_for_table():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        r.register_schema("users", _make_users_schema())
        r.subscribe_consumer("analytics",  "users")
        r.subscribe_consumer("reporting",  "users", columns=["email"])
        result = r.get_consumers_for_table("users")
        assert result.success and result.data["total"] == 2
        consumers = {c["consumer"]: c for c in result.data["consumers"]}
        assert consumers["analytics"]["full"] is True
        assert consumers["reporting"]["columns"] == ["email"]
        assert r.unsubscribe_consumer("reporting", "users").success
        assert r.get_consumers_for_table("users").data["total"] == 1
        assert not r.unsubscribe_consumer("reporting", "users").success

test("get_consumers_for_table lists projections; unsubscribe removes one", test_get_consumers_for_table)


def test_consumer_impact_projection():
    with tempfile.TemporaryDirectory() as tmp:
        r = LocalFileRegistry(tmp)
        old = _make_users_schema()
        r.register_schema("users", old)
        r.subscribe_consumer("email_svc", "users", columns=["email"])
        r.subscribe_consumer("name_svc",  "users", columns=["name"])
        new = TableSchema("users", [c for c in old.columns if c.name != "name"], source="test")
        result = r.compare_with_baseline("users", old.to_dict(), new.to_dict())
        assert result.success, result.error
        d = result.data["diff"]
        (change,) = d["changes"]
        assert change["consumer_impact"] == "consumer_constrained"
        assert change["consumers"] == [{"consumer": "name_svc", "severity": "BREAKING"}]
        assert d["consumer_summary"]["total"] == 2 and d["consumer_summary"]["affected"] == 1
        assert d["suggested_update"] is None             # a consumer reads the removed column

test("projection subscriber only affected when their columns change", test_consumer_impact_projection)


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
