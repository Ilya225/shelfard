"""
Base classes for Shelfard tools.

SchemaReader — abstract base for live database/API source introspectors.
Checker      — abstract base for stored drift-check configurations.
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


class Checker(ABC):
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
