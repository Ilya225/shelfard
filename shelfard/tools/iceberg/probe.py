"""
Iceberg drift probe.

Loads a stored :class:`~shelfard.models.IcebergProbeConfig`, resolves
``{{template_vars}}`` and then ``$ENV_VARS`` in the table path at run time,
reads the table's current schema, and diffs it against the registered baseline.
"""

import os

from ..base import Probe
from ...models import IcebergProbeConfig, ToolResult


class IcebergProbe(Probe):
    """Run a stored Iceberg drift-check configuration."""

    def __init__(self, config: IcebergProbeConfig, registry) -> None:
        self.config = config
        self.registry = registry

    def fetch(self) -> ToolResult:
        """Resolve the stored config and read the live schema (no comparison).
        Returns the reader's ToolResult (data={"schema": {...}, ...})."""
        missing = [name for name in self.config.env if name not in os.environ]
        if missing:
            return ToolResult(
                success=False,
                error=f"Missing required environment variables: {', '.join(missing)}",
            )

        path = self.registry.resolve_template(self.config.path)
        for name in self.config.env:
            path = path.replace(f"${name}", os.environ[name])

        from .reader import IcebergReader

        live = IcebergReader(path, self.config.schema_name).get_schema()
        if not live.success:
            return ToolResult(success=False, error=f"Failed to read live schema: {live.error}",
                              next_action_hint=getattr(live, "next_action_hint", None))
        return live

    def run(self) -> ToolResult:
        live = self.fetch()
        if not live.success:
            return live

        baseline = self.registry.get_registered_schema(self.config.schema_name)
        if not baseline.success:
            return ToolResult(
                success=False,
                error=f"No baseline found for '{self.config.schema_name}'",
                next_action_hint="Run 'shelfard iceberg snapshot' first to capture a baseline",
            )

        from ..base import compare_against_baseline

        diff = compare_against_baseline(self.registry, self.config.schema_name,
                                        baseline.data["schema"], live.data["schema"])
        if not diff.success:
            return ToolResult(success=False, error=diff.error)

        return ToolResult(
            success=True,
            data={
                "schema_name": self.config.schema_name,
                "baseline_version": baseline.data["version"],
                "baseline_version_number": baseline.data.get("version_number"),
                "metadata_file": live.data["metadata_file"],
                "diff": diff.data["diff"],
            },
        )
