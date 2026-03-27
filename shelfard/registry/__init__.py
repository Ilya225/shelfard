"""
shelfard.registry — pluggable schema registry.

Public API:
    SchemaRegistry      — ABC defining the full registry contract
    LocalFileRegistry   — file-based implementation (default)
    S3Registry          — Amazon S3 backend (stub)
    GCSRegistry         — Google Cloud Storage backend (stub)
    SQLRegistry         — SQL database backend (stub)

Module-level convenience functions delegate to a default LocalFileRegistry
instance, preserving backward compatibility with existing call sites.
"""

from .base import SchemaRegistry
from .local import LocalFileRegistry
from .s3 import S3Registry
from .gcs import GCSRegistry
from .sql import SQLRegistry

__all__ = [
    "SchemaRegistry",
    "LocalFileRegistry",
    "S3Registry",
    "GCSRegistry",
    "SQLRegistry",
    # convenience shims
    "register_schema",
    "get_registered_schema",
    "get_all_schemas",
    "subscribe_consumer",
    "get_consumer_subscription",
    "get_consumers_for_table",
    "get_all_consumers",
    "get_consumers_affected_by_diff",
    # checker shims
    "register_checker",
    "get_checker",
    "get_all_checkers",
    "run_checker",
    # template variable shims
    "set_var",
    "get_var",
    "list_vars",
    "delete_var",
]

# ── Default instance ──────────────────────────────────────────────────────────
# Backward-compatible module-level functions. Agent and existing tests import
# these directly: `from .registry import get_all_schemas, get_registered_schema`
_default = LocalFileRegistry()

register_schema              = _default.register_schema
get_registered_schema        = _default.get_registered_schema
get_all_schemas              = _default.get_all_schemas
subscribe_consumer           = _default.subscribe_consumer
get_consumer_subscription    = _default.get_consumer_subscription
get_consumers_for_table      = _default.get_consumers_for_table
get_all_consumers            = _default.get_all_consumers
get_consumers_affected_by_diff = _default.get_consumers_affected_by_diff

register_checker             = _default.register_checker
get_checker                  = _default.get_checker
get_all_checkers             = _default.get_all_checkers
run_checker                  = _default.run_checker

set_var                      = _default.set_var
get_var                      = _default.get_var
list_vars                    = _default.list_vars
delete_var                   = _default.delete_var
