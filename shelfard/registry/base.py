"""
SchemaRegistry — abstract base class.

Any backend (local filesystem, S3, GCS, SQL) must implement this interface.
All methods return ToolResult so they are usable directly as LLM agent tools.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from ..models import ConsumerProjection, TableSchema, ToolResult, RestProbeConfig, PostgresProbeConfig, FileProbeConfig, IcebergProbeConfig


class SchemaRegistry(ABC):

    # ── Source schemas ────────────────────────────────────────────────────────

    @abstractmethod
    def register_schema(self, table_name: str, schema: TableSchema) -> ToolResult:
        """Save a new version of a source schema to the registry."""
        ...

    @abstractmethod
    def get_registered_schema(self, table_name: str, version: str = "latest") -> ToolResult:
        """
        Retrieve a registered source schema.

        version: "latest" or a specific ISO timestamp string.
        Returns ToolResult with data={"schema": {...}, "version": "..."}.
        """
        ...

    @abstractmethod
    def get_all_schemas(self) -> ToolResult:
        """List all source schemas with summary metadata."""
        ...

    def get_schema_history(self, table_name: str) -> ToolResult:
        """
        All registered versions of a source schema, oldest first.

        Returns ToolResult with data={"versions": [schema dict, ...]}. Optional
        for backends (used by ODCS export to derive semantic versions); the
        default reports it as unsupported.
        """
        return ToolResult(success=False,
                          error=f"{type(self).__name__} does not support schema history.")

    # ── Consumer subscriptions ────────────────────────────────────────────────

    @abstractmethod
    def subscribe_consumer(
        self,
        consumer_name: str,
        table_name: str,
        columns: Optional[list[str]] = None,
    ) -> ToolResult:
        """
        Register a consumer's dependency on a source schema.

        Saves (overwrites) the consumer's projection: the column paths it reads
        (``id``, ``address.zip``, ``tags[]``) and their shape in the latest
        registered schema. ``columns=None`` means all top-level columns. Unknown
        paths are an error. Not versioned.
        Returns ToolResult with data={"projection": ConsumerProjection.to_dict()}.
        """
        ...

    @abstractmethod
    def unsubscribe_consumer(self, consumer_name: str, table_name: str) -> ToolResult:
        """Remove a consumer's projection on a table."""
        ...

    def refresh_consumer(self, consumer_name: str, table_name: str) -> ToolResult:
        """
        The consumer adopts the latest registered shape for the paths it reads
        (a consumer-side action; baseline updates never do this). Refuses if a
        path no longer exists. Optional; the default reports it as unsupported.
        """
        return ToolResult(success=False,
                          error=f"{type(self).__name__} does not support refreshing projections.")

    @abstractmethod
    def get_consumer_subscription(self, consumer_name: str, table_name: str) -> ToolResult:
        """
        Retrieve a consumer's projection on a table.

        Returns ToolResult with data={"subscription": ConsumerProjection.to_dict()}.
        """
        ...

    @abstractmethod
    def get_consumers_for_table(self, table_name: str) -> ToolResult:
        """
        List all consumers that have subscribed to a given source table.

        Returns ToolResult with data={"consumers": [ConsumerProjection dict + "consumer"]}.
        """
        ...

    @abstractmethod
    def get_all_consumers(self) -> ToolResult:
        """
        List every consumer subscription across all tables.

        Returns ToolResult with data={"consumers": [...]}.
        """
        ...

    # ── Probes ──────────────────────────────────────────────────────────────

    @abstractmethod
    def register_probe(
        self,
        schema_name: str,
        config: RestProbeConfig | PostgresProbeConfig | FileProbeConfig | IcebergProbeConfig,
    ) -> ToolResult:
        """Store a probe config for the given schema."""
        ...

    @abstractmethod
    def get_probe(self, schema_name: str) -> ToolResult:
        """
        Retrieve the probe config for the given schema.

        Returns ToolResult with data={"probe": RestProbeConfig.to_dict()}.
        """
        ...

    @abstractmethod
    def get_all_probes(self) -> ToolResult:
        """
        List all registered probes with summary metadata.

        Returns ToolResult with data={"probes": [...]}.
        """
        ...

    @abstractmethod
    def run_probe(self, schema_name: str) -> ToolResult:
        """
        Load the probe config for schema_name, instantiate the appropriate
        Probe, and call run().

        Returns the same ToolResult as Probe.run().
        """
        ...

    # ── Template variables ────────────────────────────────────────────────────

    @abstractmethod
    def set_var(self, name: str, value: str) -> ToolResult:
        """Store a named template variable. Overwrites if already set."""
        ...

    @abstractmethod
    def get_var(self, name: str) -> ToolResult:
        """
        Retrieve a stored template variable by name.

        Returns ToolResult with data={"name": ..., "value": ...}.
        """
        ...

    @abstractmethod
    def list_vars(self) -> ToolResult:
        """
        List all stored template variables.

        Returns ToolResult with data={"vars": {"name": "value", ...}}.
        """
        ...

    @abstractmethod
    def delete_var(self, name: str) -> ToolResult:
        """
        Delete a stored template variable by name.

        Returns ToolResult with success=False and an informative error if not found.
        """
        ...

    def resolve_template(self, template: str) -> str:
        """
        Replace every {{name}} in template with the stored variable value.

        Unknown variables are left as-is (no KeyError). Resolution is performed
        with a single regex pass to avoid double-substitution.
        """
        def _replace(match: re.Match) -> str:
            result = self.get_var(match.group(1))
            return result.data["value"] if result.success else match.group(0)
        return re.sub(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", _replace, template)

    def fetch_probe_schema(self, schema_name: str) -> ToolResult:
        """Read the live schema through a stored probe without comparing it
        (data={"schema": {...}}). Optional; the default reports it as unsupported."""
        return ToolResult(success=False,
                          error=f"{type(self).__name__} does not support fetching live schemas.")

    # ── Consumer impact ───────────────────────────────────────────────────────

    def get_projections(self, table_name: str) -> list[ConsumerProjection]:
        """All consumer projections on *table_name* (empty on error)."""
        result = self.get_consumers_for_table(table_name)
        if not result.success:
            return []
        return [ConsumerProjection.from_dict(c) for c in result.data["consumers"]]

    def assess_consumer_impact(self, table_name: str, diff: dict, old_schema: dict,
                               new_schema: dict) -> ToolResult:
        """
        Annotate a serialized SchemaDiff with consumer impact: each change gets
        ``consumer_impact`` (no_consumers / consumer_constrained) and the affected
        consumers with their own severity; ``consumer_summary`` holds the total
        and affected counts and outdated projections; ``suggested_update`` is
        recomputed (``kind`` "safe", or "unaffected" when every WARNING/BREAKING
        change touches no consumer). Concrete: works for any backend.
        """
        from ..projection import annotate
        try:
            annotated = annotate(diff, self.get_projections(table_name),
                                 TableSchema.from_dict(old_schema), TableSchema.from_dict(new_schema))
        except Exception as e:
            return ToolResult(success=False, error=f"Consumer impact analysis failed: {e}")
        return ToolResult(success=True, data={"diff": annotated})

    def compare_with_baseline(self, table_name: str, baseline: dict, live: dict) -> ToolResult:
        """Diff *live* against *baseline* (schema dicts) and add consumer impact.
        Returns ToolResult with data={"diff": {...}} like compare_schemas."""
        from ..schema_comparison import compare_schemas_from_dicts
        result = compare_schemas_from_dicts(baseline, live)
        if not result.success:
            return result
        impact = self.assess_consumer_impact(table_name, result.data["diff"], baseline, live)
        if not impact.success:
            return impact
        return ToolResult(success=True, data=impact.data, next_action_hint=result.next_action_hint)

    # ── Baseline updates ──────────────────────────────────────────────────────

    def accept_update(self, table_name: str, diff: dict, baseline_version: str,
                      allow_unaffected: bool = False) -> ToolResult:
        """
        Register a diff's ``suggested_update`` schema as the next baseline version.

        ``diff`` is a serialized SchemaDiff with consumer impact (a probe run or
        ``compare_with_baseline``). Updates of kind "safe" (every change SAFE) are
        always accepted; kind "unaffected" (WARNING/BREAKING changes that no
        consumer reads) only with ``allow_unaffected=True``, and only if that is
        still true for the consumers subscribed right now.

        Refuses if the latest registered version is no longer ``baseline_version``.
        Consumer projections are never modified — consumers whose projection now
        differs from the baseline are returned as ``outdated_consumers``.
        Concrete: works for any backend.
        """
        from ..projection import consumer_diff, outdated_after, touches

        update = (diff or {}).get("suggested_update")
        if not update:
            return ToolResult(
                success=False,
                error="No update to apply: the check found no changes, or some WARNING/BREAKING "
                      "changes affect consumers and need review.",
            )
        kind = update.get("kind", "safe")
        if kind == "unaffected" and not allow_unaffected:
            return ToolResult(
                success=False,
                error="This update contains WARNING/BREAKING changes that no consumer reads; "
                      "accepting it needs allow_unaffected (CLI: --update --allow-unaffected).",
            )
        latest = self.get_registered_schema(table_name)
        if not latest.success:
            return latest
        if latest.data["version"] != baseline_version:
            return ToolResult(
                success=False,
                error=f"Baseline for '{table_name}' changed since this check "
                      f"(expected {baseline_version}, found {latest.data['version']}). Run the check again.",
            )

        new_schema = TableSchema.from_dict(update["schema"])
        projections = self.get_projections(table_name)
        if kind == "unaffected":
            risky = [c["column_name"] for c in diff.get("changes", []) if c["severity"] != "SAFE"]
            for p in projections:
                hits = [c["column_name"] for c in consumer_diff(p, new_schema)["changes"]
                        if c["severity"] != "SAFE" and touches(c["column_name"], risky)]
                if hits:
                    return ToolResult(
                        success=False,
                        error=f"Consumer '{p.consumer_name}' now reads {', '.join(hits)}, which this "
                              f"update would break. Run the check again.",
                    )

        result = self.register_schema(table_name, new_schema)
        if result.success:
            result.data["explanation"] = update.get("explanation", [])
            result.data["kind"] = kind
            result.data["outdated_consumers"] = outdated_after(projections, new_schema)
            result.next_action_hint = (
                f"Baseline updated to version {result.data['version_count']}. "
                "Consumer projections were not changed; outdated consumers can refresh theirs "
                "(shelfard consumer refresh <table> --consumer NAME)."
            )
        return result
