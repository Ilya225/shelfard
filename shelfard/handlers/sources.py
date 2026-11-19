"""
Sources: a data source is described by a probe config — registered or ad hoc.

``RestProbeConfig`` / ``PostgresProbeConfig`` / ``FileProbeConfig`` /
``IcebergProbeConfig`` already hold everything needed to read a live schema, so
ad-hoc snapshots and checks use the same read path as stored probes
(``Probe.fetch()``): ``{{var}}`` then ``$VAR`` resolution, the SQL-template ban
and the MCP run-time guards all apply uniformly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace

from ..models import (
    FileProbeConfig, IcebergProbeConfig, PostgresProbeConfig, ProbeConfig, RestProbeConfig,
    TableSchema,
)  # noqa: F401  (re-exported for controllers)
from ..security import query_template_error
from ..tools.base import probe_for
from .core import ErrorCode, Response

_ENV_REF = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


def extract_env_vars(*templates: str) -> list[str]:
    """Unique ``$VAR_NAME`` names across the templates, in order of first appearance."""
    seen: list[str] = []
    for t in templates:
        for name in _ENV_REF.findall(t or ""):
            if name not in seen:
                seen.append(name)
    return seen


def _templates(config: ProbeConfig) -> list[str]:
    if isinstance(config, RestProbeConfig):
        return [config.url, *(v for h in config.headers for v in h.values())]
    if isinstance(config, PostgresProbeConfig):
        return [config.dsn] + ([config.query] if config.query else [])
    return [config.path]


def declared_env(config: ProbeConfig) -> list[str]:
    """Every ``$VAR`` the config references (what a registered probe must declare)."""
    return extract_env_vars(*_templates(config))


def with_declared_env(config: ProbeConfig) -> ProbeConfig:
    """The config with ``env`` set to every ``$VAR`` it references."""
    return replace(config, env=declared_env(config))


def _ad_hoc(config: ProbeConfig) -> ProbeConfig:
    """For one-off reads: substitute the ``$VARS`` that are set, leave the rest literal."""
    return replace(config, env=[n for n in declared_env(config) if n in os.environ])


def resolve_local_path(registry, raw: str) -> str:
    """Resolve ``{{vars}}`` from the registry, then the ``$VARS`` that are set."""
    path = registry.resolve_template(raw)
    for name in extract_env_vars(path):
        if name in os.environ:
            path = path.replace(f"${name}", os.environ[name])
    return path


def describe(config: ProbeConfig, registry) -> str:
    """Human-readable target of a source (URL / path with ``{{vars}}`` resolved)."""
    if isinstance(config, RestProbeConfig):
        return registry.resolve_template(config.url)
    if isinstance(config, PostgresProbeConfig):
        return f"PostgreSQL '{config.table or '(custom query)'}'"
    return resolve_local_path(registry, config.path)


def source_kind(config: ProbeConfig) -> str:
    return config.probe_type


@dataclass
class SourceRead:
    """A live schema read from a source."""
    schema: TableSchema
    target: str
    metadata: dict = field(default_factory=dict)   # reader extras, e.g. Iceberg "metadata_file"

    def to_dict(self) -> dict:
        return {"schema": self.schema.to_dict(), "target": self.target, **self.metadata}


def read_source(config: ProbeConfig, registry, ad_hoc: bool = True) -> Response[SourceRead]:
    """Read the live schema described by ``config``. ``ad_hoc`` reads substitute only
    the ``$VARS`` that are set; registered probes require their declared ``env``."""
    if isinstance(config, PostgresProbeConfig) and (err := query_template_error(config.query)):
        return Response.fail(ErrorCode.INVALID, err)       # rejected before anything is read
    target = describe(config, registry)
    try:
        probe = probe_for(_ad_hoc(config) if ad_hoc else config, registry)
    except ValueError as e:
        return Response.fail(ErrorCode.INVALID, str(e), target=target)
    tr = probe.fetch()
    if not tr.success:
        return Response.fail(ErrorCode.FAILED, tr.error or "Read failed", tr.next_action_hint,
                             target=target)
    data = dict(tr.data or {})
    schema = TableSchema.from_dict(data.pop("schema"))
    return Response.success(SourceRead(schema, target, data))


__all__ = [
    "SourceRead", "read_source", "describe", "declared_env", "with_declared_env",
    "extract_env_vars", "resolve_local_path", "source_kind", "ProbeConfig",
    "RestProbeConfig", "PostgresProbeConfig", "FileProbeConfig", "IcebergProbeConfig",
]
