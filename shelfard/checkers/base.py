"""Checker — abstract base class for stored drift-check configurations."""

from abc import ABC, abstractmethod

from ..models import ToolResult


class Checker(ABC):
    @abstractmethod
    def run(self) -> ToolResult:
        """
        Run the drift check.

        Returns ToolResult with data={
            "schema_name": str,
            "baseline_version": str,
            "diff": dict,       # SchemaDiff serialized
        }
        """
