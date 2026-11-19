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
    "unsubscribe_consumer",
    "refresh_consumer",
    "assess_consumer_impact",
    "compare_with_baseline",
    # probe shims
    "register_probe",
    "get_probe",
    "get_all_probes",
    "run_probe",
    # template variable shims
    "set_var",
    "get_var",
    "list_vars",
    "delete_var",
    "resolve_template",
    "accept_update",
    "get_schema_history",
    "fetch_probe_schema",
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
unsubscribe_consumer         = _default.unsubscribe_consumer
refresh_consumer             = _default.refresh_consumer
assess_consumer_impact       = _default.assess_consumer_impact
compare_with_baseline        = _default.compare_with_baseline

register_probe             = _default.register_probe
get_probe                  = _default.get_probe
get_all_probes             = _default.get_all_probes
run_probe                  = _default.run_probe

set_var                      = _default.set_var
get_var                      = _default.get_var
list_vars                    = _default.list_vars
delete_var                   = _default.delete_var
resolve_template             = _default.resolve_template
accept_update                = _default.accept_update
get_schema_history           = _default.get_schema_history
fetch_probe_schema         = _default.fetch_probe_schema
