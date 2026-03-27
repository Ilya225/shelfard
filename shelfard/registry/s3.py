"""
S3Registry — Amazon S3-backed SchemaRegistry (stub).

Not yet implemented. Requires: boto3
"""

from __future__ import annotations

from typing import Optional

from ..models import TableSchema, SchemaDiff, ToolResult
from .base import SchemaRegistry

_MSG = "S3Registry is not yet implemented. Use LocalFileRegistry."


class S3Registry(SchemaRegistry):
    """
    Registry backed by Amazon S3.

    Args:
        bucket: S3 bucket name.
        prefix: Key prefix for all registry objects (default: "shelfard/").
    """

    def __init__(self, bucket: str, prefix: str = "shelfard/") -> None:
        self.bucket = bucket
        self.prefix = prefix

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

    def register_checker(self, schema_name, config):
        raise NotImplementedError(_MSG)

    def get_checker(self, schema_name):
        raise NotImplementedError(_MSG)

    def get_all_checkers(self):
        raise NotImplementedError(_MSG)

    def run_checker(self, schema_name):
        raise NotImplementedError(_MSG)

    def set_var(self, name, value):
        raise NotImplementedError(_MSG)

    def get_var(self, name):
        raise NotImplementedError(_MSG)

    def list_vars(self):
        raise NotImplementedError(_MSG)

    def delete_var(self, name):
        raise NotImplementedError(_MSG)

    def get_consumers_affected_by_diff(self, table_name, diff):
        raise NotImplementedError(_MSG)
