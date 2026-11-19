"""
Base classes for Shelfard tools.

SchemaReader — abstract base for live database/API source introspectors.
Probe      — abstract base for stored drift-check configurations.
"""

from abc import ABC, abstractmethod

from ..models import ToolResult


class SchemaReader(ABC):

    @abstractmethod
    def get_schema(self) -> ToolResult:
        """
        Introspect the source and return its normalized schema.
        The target (table name, endpoint URL, etc.) is provided at construction time.

        Returns:
            ToolResult with data={"schema": TableSchema.to_dict()} on success.
        """
        ...

    @abstractmethod
    def list_tables(self) -> ToolResult:
        """
        Return all user-visible table names in the source.

        Returns:
            ToolResult with data={"tables": [...], "count": int} on success.
        """
        ...


class Probe(ABC):
    def fetch(self) -> ToolResult:
        """Read the live schema without comparing it (data={"schema": {...}}).
        Used by ODCS contract checks; built-in probes all implement it."""
        return ToolResult(success=False,
                          error=f"{type(self).__name__} does not support fetching the live schema.")

    @abstractmethod
    def run(self) -> ToolResult:
        """
        Run the drift check.

        Returns ToolResult with data={
            "schema_name": str,
            "baseline_version": str,          # captured_at of the baseline
            "baseline_version_number": int,   # 1-based position in the history
            "diff": dict,       # SchemaDiff serialized; includes suggested_update when all SAFE
        }
        """


def compare_against_baseline(registry, table_name: str, baseline: dict, live: dict) -> ToolResult:
    """Diff *live* against *baseline* with consumer impact when *registry* is a
    SchemaRegistry (plain comparison otherwise, e.g. a stand-in in tests)."""
    from ..registry.base import SchemaRegistry
    if isinstance(registry, SchemaRegistry):
        return registry.compare_with_baseline(table_name, baseline, live)
    from ..schema_comparison import compare_schemas_from_dicts
    return compare_schemas_from_dicts(baseline, live)


def probe_for(config, registry) -> Probe:
    """The Probe implementation for a probe config (registered or ad hoc).
    Raises ValueError for an unknown ``probe_type``."""
    probe_type = getattr(config, "probe_type", None)
    if probe_type == "rest":
        from .rest import RestProbe
        return RestProbe(config, registry)
    if probe_type == "postgres":
        from .postgres import PostgresProbe
        return PostgresProbe(config, registry)
    if probe_type == "file":
        from .files import FileProbe
        return FileProbe(config, registry)
    if probe_type == "iceberg":
        from .iceberg import IcebergProbe
        return IcebergProbe(config, registry)
    raise ValueError(f"Unknown probe type: {probe_type!r}")
