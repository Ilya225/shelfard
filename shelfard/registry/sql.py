"""
SQLRegistry — SQL database-backed SchemaRegistry (stub).

Not yet implemented. Intended to support PostgreSQL, MySQL, SQLite, etc.
via a standard connection string (e.g. psycopg2, sqlalchemy).
"""

from __future__ import annotations

from .base import SchemaRegistry

_MSG = "SQLRegistry is not yet implemented. Use LocalFileRegistry."


class SQLRegistry(SchemaRegistry):
    """
    Registry backed by a SQL database.

    Args:
        connection_string: Database URL (e.g. "postgresql://user:pass@host/db").
    """

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string

    def register_schema(self, table_name, schema):
        raise NotImplementedError(_MSG)

    def get_registered_schema(self, table_name, version="latest"):
        raise NotImplementedError(_MSG)

    def get_all_schemas(self):
        raise NotImplementedError(_MSG)

    def subscribe_consumer(self, consumer_name, table_name, columns=None):
        raise NotImplementedError(_MSG)

    def get_consumer_subscription(self, consumer_name, table_name):
        raise NotImplementedError(_MSG)

    def get_consumers_for_table(self, table_name):
        raise NotImplementedError(_MSG)

    def get_all_consumers(self):
        raise NotImplementedError(_MSG)

    def get_consumers_affected_by_diff(self, table_name, diff):
        raise NotImplementedError(_MSG)
