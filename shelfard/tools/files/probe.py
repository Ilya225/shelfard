"""
File-format drift probe (Parquet / ORC / Avro).

Loads a stored :class:`~shelfard.models.FileProbeConfig`, resolves
``{{template_vars}}`` and then ``$ENV_VARS`` in the path at run time, reads the
current schema with :class:`~shelfard.tools.files.reader.FileReader`, and diffs
it against the registered baseline.
"""

import os

from ..base import Probe
from ...models import FileProbeConfig, ToolResult


class FileProbe(Probe):
    """Run a stored file-format drift-check configuration."""

    def __init__(self, config: FileProbeConfig, registry) -> None:
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

        from .reader import FileReader

        live = FileReader(path, self.config.schema_name, self.config.file_format).get_schema()
        if not live.success:
            return ToolResult(success=False, error=f"Failed to read live schema: {live.error}")
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
                next_action_hint="Run 'shelfard file snapshot' first to capture a baseline",
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
                "diff": diff.data["diff"],
            },
        )
