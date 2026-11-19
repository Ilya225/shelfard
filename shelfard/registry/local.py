"""
LocalFileRegistry — file-based SchemaRegistry implementation.

Storage layout:
    <registry_dir>/
    ├── sources/
    │   └── <table_name>.json          # versioned source schema history
    └── consumers/
        └── <consumer_name>/
            └── <table_name>.json      # versioned consumer subscription history

Each JSON file stores: {"table_name": "...", "versions": [...]}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import (
    ColumnSchema, TableSchema, ConsumerProjection, ToolResult,
    RestProbeConfig, PostgresProbeConfig, FileProbeConfig, IcebergProbeConfig,
)
from .base import SchemaRegistry
from ..projection import missing_paths, project
from ..security import ensure_within, query_template_error, validate_name, validate_var_name


class LocalFileRegistry(SchemaRegistry):
    """
    File-based registry. Default root is <project_root>/schemas/.

    Pass registry_dir to override (useful in tests to isolate state).
    """

    def __init__(self, registry_dir: Optional[Path | str] = None) -> None:
        if registry_dir is None:
            registry_dir = Path(__file__).parent.parent.parent / "schemas"
        self._root = Path(registry_dir)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _sources_dir(self) -> Path:
        return self._root / "sources"

    def _consumers_dir(self) -> Path:
        return self._root / "consumers"

    def _safe_path(self, *parts: str) -> Path:
        """Join validated name parts under the registry root (path-traversal guard)."""
        for part in parts:
            err = validate_name(part.removesuffix(".json"))
            if err:
                raise ValueError(err)
        return ensure_within(self._root, self._root.joinpath(*parts))

    @staticmethod
    def _invalid_names(**names: str) -> Optional[ToolResult]:
        """Return a failed ToolResult if any name is unsafe for use in a file path."""
        for kind, value in names.items():
            err = validate_name(value, kind.replace("_", " "))
            if err:
                return ToolResult(success=False, error=err)
        return None

    def _source_path(self, table_name: str) -> Path:
        return self._safe_path("sources", f"{table_name}.json")

    def _consumer_path(self, consumer_name: str, table_name: str) -> Path:
        return self._safe_path("consumers", consumer_name, f"{table_name}.json")

    @staticmethod
    def _load_json(path: Path) -> dict:
        with open(path) as f:
            return json.load(f)

    @staticmethod
    def _save_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    # ── Source schemas ────────────────────────────────────────────────────────

    def register_schema(self, table_name: str, schema: TableSchema) -> ToolResult:
        """Save a new version of a source schema."""
        if invalid := self._invalid_names(table_name=table_name):
            return invalid
        path = self._source_path(table_name)
        try:
            if path.exists():
                registry_data = self._load_json(path)
            else:
                registry_data = {"table_name": table_name, "versions": []}

            schema.captured_at = datetime.now(timezone.utc).isoformat()
            registry_data["versions"].append(schema.to_dict())
            self._save_json(path, registry_data)

            return ToolResult(
                success=True,
                data={
                    "registered_at": schema.captured_at,
                    "version_count": len(registry_data["versions"]),
                },
                next_action_hint="Schema registered. Future drift checks will compare against this version.",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to register schema: {e}")

    def get_registered_schema(self, table_name: str, version: str = "latest") -> ToolResult:
        """Retrieve a source schema version from the registry."""
        if invalid := self._invalid_names(table_name=table_name):
            return invalid
        path = self._source_path(table_name)

        if not path.exists():
            return ToolResult(
                success=False,
                error=f"No registered schema found for '{table_name}'. "
                      f"This may be a new table — consider registering it.",
                next_action_hint="If this is a new table, call register_schema() to baseline it.",
            )

        try:
            registry_data = self._load_json(path)

            versions = registry_data["versions"]
            if version == "latest":
                index = len(versions) - 1
            else:
                index = next((i for i, v in enumerate(versions) if v["captured_at"] == version), None)
                if index is None:
                    return ToolResult(
                        success=False,
                        error=f"Version '{version}' not found for table '{table_name}'.",
                    )
            entry = versions[index]

            columns = [ColumnSchema.from_dict(col) for col in entry["columns"]]
            schema = TableSchema(
                table_name=table_name,
                columns=columns,
                partition_keys=entry.get("partition_keys", []),
                clustering_keys=entry.get("clustering_keys", []),
                source=entry.get("source", "registry"),
                captured_at=entry["captured_at"],
            )

            return ToolResult(
                success=True,
                data={"schema": schema.to_dict(), "version": entry["captured_at"],
                      "version_number": index + 1, "version_count": len(versions)},
                next_action_hint="Use compare_schemas() to diff this against an incoming schema.",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read registry: {e}")

    def get_schema_history(self, table_name: str) -> ToolResult:
        """All registered versions of a source schema, oldest first."""
        if invalid := self._invalid_names(table_name=table_name):
            return invalid
        path = self._source_path(table_name)
        if not path.exists():
            return ToolResult(success=False,
                              error=f"No registered schema found for '{table_name}'.")
        try:
            versions = self._load_json(path)["versions"]
            history = [TableSchema.from_dict({**v, "table_name": table_name}).to_dict()
                       for v in versions]
            return ToolResult(success=True, data={"versions": history})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read registry: {e}")

    def get_all_schemas(self) -> ToolResult:
        """List all source schemas with summary metadata."""
        sources_dir = self._sources_dir()
        if not sources_dir.exists():
            return ToolResult(success=True, data={"schemas": []})

        schemas = []
        for path in sorted(sources_dir.glob("*.json")):
            try:
                data = self._load_json(path)
                versions = data.get("versions", [])
                latest = versions[-1] if versions else None
                schemas.append({
                    "name": data.get("table_name", path.stem),
                    "version_count": len(versions),
                    "latest_version": latest["captured_at"] if latest else None,
                    "source": latest.get("source", "unknown") if latest else None,
                    "column_count": len(latest.get("columns", [])) if latest else 0,
                })
            except Exception:
                pass

        return ToolResult(success=True, data={"schemas": schemas})

    # ── Consumer subscriptions ────────────────────────────────────────────────

    def subscribe_consumer(
        self,
        consumer_name: str,
        table_name: str,
        columns: Optional[list[str]] = None,
    ) -> ToolResult:
        """
        Save (overwrite) a consumer's projection on a source schema.

        columns=None  → all top-level columns of the latest schema.
        columns=[...] → the given paths (``email``, ``address.zip``, ``tags[]``).
        """
        if invalid := self._invalid_names(consumer_name=consumer_name, table_name=table_name):
            return invalid
        source_result = self.get_registered_schema(table_name)
        if not source_result.success:
            return ToolResult(
                success=False,
                error=f"Cannot subscribe to '{table_name}': source schema not registered. "
                      f"Register the source schema first.",
                next_action_hint=f"Call register_schema('{table_name}', ...) to baseline the source.",
            )
        schema = TableSchema.from_dict(source_result.data["schema"])
        full = columns is None
        paths = [c.name for c in schema.columns] if full else list(dict.fromkeys(columns))
        if not paths:
            return ToolResult(success=False, error="No columns to subscribe to.")
        missing = missing_paths(schema, paths)
        if missing:
            return ToolResult(
                success=False,
                error=f"Unknown column path(s) in '{table_name}': {missing}. "
                      f"Top-level columns: {[c.name for c in schema.columns]}",
            )

        path = self._consumer_path(consumer_name, table_name)
        now = datetime.now(timezone.utc).isoformat()
        projection = ConsumerProjection(
            consumer_name=consumer_name,
            source_table=table_name,
            columns=paths,
            expected=project(schema, paths).columns,
            source=schema.source,
            full=full,
            subscribed_at=now,
            updated_at=now,
        )
        try:
            self._save_json(path, projection.to_dict())
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to save subscription: {e}")
        return ToolResult(
            success=True,
            data={"projection": projection.to_dict(), "column_count": len(paths)},
        )

    def _load_projection(self, path: Path) -> ConsumerProjection:
        return ConsumerProjection.from_dict(upgrade_consumer_file(self._load_json(path)))

    def unsubscribe_consumer(self, consumer_name: str, table_name: str) -> ToolResult:
        if invalid := self._invalid_names(consumer_name=consumer_name, table_name=table_name):
            return invalid
        path = self._consumer_path(consumer_name, table_name)
        if not path.exists():
            return ToolResult(success=False,
                              error=f"No subscription found for consumer '{consumer_name}' on '{table_name}'.")
        path.unlink()
        try:
            path.parent.rmdir()          # remove the consumer dir when it was the last table
        except OSError:
            pass
        return ToolResult(success=True, data={"consumer": consumer_name, "table": table_name})

    def refresh_consumer(self, consumer_name: str, table_name: str) -> ToolResult:
        """Re-project the consumer's paths from the latest registered schema."""
        current = self.get_consumer_subscription(consumer_name, table_name)
        if not current.success:
            return current
        projection = ConsumerProjection.from_dict(current.data["subscription"])
        latest = self.get_registered_schema(table_name)
        if not latest.success:
            return latest
        schema = TableSchema.from_dict(latest.data["schema"])
        missing = missing_paths(schema, projection.columns)
        if missing:
            return ToolResult(
                success=False,
                error=f"'{table_name}' no longer has {missing}; re-subscribe '{consumer_name}' "
                      f"with the columns it reads now.",
            )
        before = projection.expected_schema()
        projection.expected = project(schema, projection.columns).columns
        projection.source = schema.source
        projection.updated_at = datetime.now(timezone.utc).isoformat()
        from ..schema_comparison import compare_schemas
        changed = compare_schemas(before, projection.expected_schema()).data["diff"]["changes"]
        try:
            self._save_json(self._consumer_path(consumer_name, table_name), projection.to_dict())
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to save subscription: {e}")
        return ToolResult(success=True, data={"projection": projection.to_dict(),
                                              "adopted_changes": changed})

    def get_consumer_subscription(self, consumer_name: str, table_name: str) -> ToolResult:
        """Retrieve a consumer's projection on a table."""
        if invalid := self._invalid_names(consumer_name=consumer_name, table_name=table_name):
            return invalid
        path = self._consumer_path(consumer_name, table_name)
        if not path.exists():
            return ToolResult(
                success=False,
                error=f"No subscription found for consumer '{consumer_name}' on '{table_name}'.",
                next_action_hint=f"Call subscribe_consumer('{consumer_name}', '{table_name}') to create one.",
            )
        try:
            return ToolResult(success=True, data={"subscription": self._load_projection(path).to_dict()})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read subscription: {e}")

    def get_consumers_for_table(self, table_name: str) -> ToolResult:
        """All consumer projections on a source table."""
        if invalid := self._invalid_names(table_name=table_name):
            return invalid
        consumers = []
        consumers_dir = self._consumers_dir()
        if consumers_dir.exists():
            for consumer_dir in sorted(consumers_dir.iterdir()):
                sub_path = consumer_dir / f"{table_name}.json"
                if not consumer_dir.is_dir() or not sub_path.exists():
                    continue
                try:
                    consumers.append({"consumer": consumer_dir.name,
                                      **self._load_projection(sub_path).to_dict()})
                except Exception:
                    pass
        return ToolResult(success=True, data={"consumers": consumers, "total": len(consumers)})

    def get_all_consumers(self) -> ToolResult:
        """Every consumer projection across all tables."""
        consumers = []
        consumers_dir = self._consumers_dir()
        if consumers_dir.exists():
            for consumer_dir in sorted(consumers_dir.iterdir()):
                if not consumer_dir.is_dir():
                    continue
                for sub_path in sorted(consumer_dir.glob("*.json")):
                    try:
                        consumers.append({"consumer": consumer_dir.name,
                                          **self._load_projection(sub_path).to_dict()})
                    except Exception:
                        pass
        return ToolResult(success=True, data={"consumers": consumers})

    # ── Probes ──────────────────────────────────────────────────────────────

    def _probes_dir(self) -> Path:
        return self._root / "probes"

    def _probe_path(self, schema_name: str) -> Path:
        return self._safe_path("probes", f"{schema_name}.json")

    def _vars_path(self) -> Path:
        return self._root / "vars.json"

    def _load_vars(self) -> dict:
        path = self._vars_path()
        return self._load_json(path) if path.exists() else {}

    def _save_vars(self, data: dict) -> None:
        self._save_json(self._vars_path(), data)

    def register_probe(
        self,
        schema_name: str,
        config: RestProbeConfig | PostgresProbeConfig | FileProbeConfig | IcebergProbeConfig,
    ) -> ToolResult:
        """Store a probe config. Always overwrites — probes are not versioned."""
        if invalid := self._invalid_names(schema_name=schema_name):
            return invalid
        if config.schema_name != schema_name:
            return ToolResult(
                success=False,
                error=f"Config schema_name {config.schema_name!r} does not match {schema_name!r}.",
            )
        if err := query_template_error(getattr(config, "query", None)):
            return ToolResult(success=False, error=err)
        config.registered_at = datetime.now(timezone.utc).isoformat()
        path = self._probe_path(schema_name)
        try:
            self._save_json(path, config.to_dict())
            return ToolResult(
                success=True,
                data={"schema_name": schema_name, "registered_at": config.registered_at},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to register probe: {e}")

    def get_probe(self, schema_name: str) -> ToolResult:
        """Retrieve the probe config for the given schema."""
        if invalid := self._invalid_names(schema_name=schema_name):
            return invalid
        path = self._probe_path(schema_name)
        if not path.exists():
            return ToolResult(
                success=False,
                error=f"No probe registered for '{schema_name}'.",
            )
        try:
            raw = _legacy_probe(self._load_json(path))
            return ToolResult(success=True, data={"probe": raw})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read probe: {e}")

    def get_all_probes(self) -> ToolResult:
        """List all registered probes with summary metadata."""
        probes_dir = self._probes_dir()
        if not probes_dir.exists():
            return ToolResult(success=True, data={"probes": []})
        result = []
        for f in sorted(probes_dir.glob("*.json")):
            try:
                c = _legacy_probe(self._load_json(f))
                result.append({
                    "schema_name": c["schema_name"],
                    "probe_type": c.get("probe_type", "rest"),
                    "url": c.get("url", ""),       # rest probes
                    "dsn": c.get("dsn", ""),       # postgres probes
                    "path": c.get("path", ""),     # file / iceberg probes
                    "env": c.get("env", []),
                    "registered_at": c.get("registered_at"),
                    "registered_via": c.get("registered_via", "cli"),
                })
            except Exception:
                pass
        return ToolResult(success=True, data={"probes": result})

    def _build_probe(self, schema_name: str):
        """Return (probe, None) for a stored config, or (None, error ToolResult)."""
        from ..models import probe_config_from_dict
        from ..tools.base import probe_for

        result = self.get_probe(schema_name)
        if not result.success:
            return None, result
        try:
            return probe_for(probe_config_from_dict(result.data["probe"]), self), None
        except ValueError as e:
            return None, ToolResult(success=False, error=str(e))

    def run_probe(self, schema_name: str) -> ToolResult:
        """Load the probe config and run the live drift check."""
        probe, error = self._build_probe(schema_name)
        return error if error else probe.run()

    def fetch_probe_schema(self, schema_name: str) -> ToolResult:
        """Run only the fetch half of a stored probe: the live schema, no diff."""
        probe, error = self._build_probe(schema_name)
        return error if error else probe.fetch()

    # ── Template variables ────────────────────────────────────────────────────

    def set_var(self, name: str, value: str) -> ToolResult:
        """Store a named template variable. Overwrites if already set."""
        if err := validate_var_name(name):
            return ToolResult(success=False, error=err)
        try:
            vars_data = self._load_vars()
            vars_data[name] = value
            self._save_vars(vars_data)
            return ToolResult(
                success=True,
                data={"name": name, "value": value},
                next_action_hint=f"Use {{{{{{name}}}}}} in probe configs or snapshot URLs.",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to set var '{name}': {e}")

    def get_var(self, name: str) -> ToolResult:
        """Retrieve a stored template variable by name."""
        try:
            vars_data = self._load_vars()
            if name not in vars_data:
                return ToolResult(
                    success=False,
                    error=f"Template variable '{name}' not found.",
                    next_action_hint=f"Set it with: shelfard var set {name} <value>",
                )
            return ToolResult(success=True, data={"name": name, "value": vars_data[name]})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to get var '{name}': {e}")

    def list_vars(self) -> ToolResult:
        """List all stored template variables."""
        try:
            vars_data = self._load_vars()
            return ToolResult(success=True, data={"vars": vars_data})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to list vars: {e}")

    def delete_var(self, name: str) -> ToolResult:
        """Delete a stored template variable by name."""
        try:
            vars_data = self._load_vars()
            if name not in vars_data:
                return ToolResult(
                    success=False,
                    error=f"Template variable '{name}' does not exist.",
                )
            del vars_data[name]
            self._save_vars(vars_data)
            return ToolResult(success=True, data={"deleted": name})
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to delete var '{name}': {e}")

    # ── Impact analysis ───────────────────────────────────────────────────────


def _legacy_probe(config: dict) -> dict:
    """Probe configs saved before the checker → probe rename used ``checker_type``."""
    if "probe_type" not in config and "checker_type" in config:
        config = {**config, "probe_type": config["checker_type"]}
        config.pop("checker_type")
    return config


def upgrade_consumer_file(data: dict) -> dict:
    """Convert a pre-projection subscription file (a ``versions`` list of
    snapshots) to a ConsumerProjection dict, using its latest entry."""
    if "versions" not in data:
        return data
    latest = data["versions"][-1]
    snapshot = latest.get("schema") or {}
    columns = snapshot.get("columns") or []
    subscribed = latest.get("subscribed_columns")
    return {
        "consumer_name": latest.get("consumer_name", data.get("consumer_name")),
        "source_table": latest.get("source_table", data.get("source_table")),
        "columns": list(subscribed) if subscribed else [c["name"] for c in columns],
        "expected": columns,
        "source": "unknown",
        "full": subscribed is None,
        "subscribed_at": latest.get("subscribed_at"),
        "updated_at": latest.get("subscribed_at"),
    }
