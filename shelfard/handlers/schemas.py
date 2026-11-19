"""Registered source schemas: read, list, snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import ProbeConfig, TableSchema, probe_config_from_dict
from .core import Command, ErrorCode, Handler, Query, Response, handles
from .sources import read_source, with_declared_env


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GetSchema(Query):
    name: str
    version: str = "latest"
    with_probe: bool = False        # also return the registered probe config, if any


@dataclass(frozen=True)
class ListSchemas(Query):
    pass


@dataclass(frozen=True)
class SnapshotSchema(Command):
    """Read a source and register its schema as a new version of ``source.schema_name``.
    With ``create_probe``, also store ``source`` as the schema's probe (``env`` set to
    every ``$VAR`` it references)."""
    source: ProbeConfig
    create_probe: bool = False


@dataclass
class SchemaVersion:
    name: str
    schema: TableSchema
    version: str                           # captured_at of this version
    version_number: Optional[int] = None   # 1-based position in the history
    version_count: Optional[int] = None
    probe: Optional[ProbeConfig] = None

    def to_dict(self) -> dict:
        d = {"schema": self.schema.to_dict(), "version": self.version,
             "version_number": self.version_number, "version_count": self.version_count}
        if self.probe is not None:
            d["probe"] = self.probe.to_dict()
        return d


@dataclass
class SchemaSummary:
    name: str
    version_count: int
    latest_version: Optional[str]
    source: Optional[str]
    column_count: int

    def to_dict(self) -> dict:
        return {"name": self.name, "version_count": self.version_count,
                "latest_version": self.latest_version, "source": self.source,
                "column_count": self.column_count}


@dataclass
class SchemaList:
    schemas: list[SchemaSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"schemas": [s.to_dict() for s in self.schemas]}


@dataclass
class SnapshotResult:
    name: str
    schema: TableSchema
    version_count: int
    target: str
    metadata: dict = field(default_factory=dict)
    probe: Optional[ProbeConfig] = None        # set when a probe was registered
    probe_error: Optional[str] = None          # set when probe registration failed

    def to_dict(self) -> dict:
        return {"name": self.name, "schema": self.schema.to_dict(),
                "version_count": self.version_count, "target": self.target, **self.metadata,
                "probe": self.probe.to_dict() if self.probe else None,
                "probe_error": self.probe_error}


# ── Handlers ──────────────────────────────────────────────────────────────────

def load_schema(registry, name: str, version: str = "latest") -> Response[SchemaVersion]:
    tr = registry.get_registered_schema(name, version)
    if not tr.success:
        return Response.from_tool_result(tr)
    d = tr.data
    return Response.success(SchemaVersion(
        name=name, schema=TableSchema.from_dict(d["schema"]), version=d["version"],
        version_number=d.get("version_number"), version_count=d.get("version_count"),
    ), hint=tr.next_action_hint)


@handles(GetSchema)
class GetSchemaHandler(Handler[GetSchema, SchemaVersion]):
    def handle(self, q: GetSchema) -> Response[SchemaVersion]:
        resp = load_schema(self.registry, q.name, q.version)
        if resp.ok and q.with_probe:
            probe = self.registry.get_probe(q.name)
            if probe.success:
                resp.result.probe = probe_config_from_dict(probe.data["probe"])
        return resp


@handles(ListSchemas)
class ListSchemasHandler(Handler[ListSchemas, SchemaList]):
    def handle(self, q: ListSchemas) -> Response[SchemaList]:
        return Response.from_tool_result(
            self.registry.get_all_schemas(),
            lambda d: SchemaList([SchemaSummary(
                name=s["name"], version_count=s["version_count"],
                latest_version=s.get("latest_version"), source=s.get("source"),
                column_count=s.get("column_count", 0)) for s in d["schemas"]]),
        )


@handles(SnapshotSchema)
class SnapshotSchemaHandler(Handler[SnapshotSchema, SnapshotResult]):
    def handle(self, c: SnapshotSchema) -> Response[SnapshotResult]:
        name = c.source.schema_name
        read = read_source(c.source, self.registry)
        if not read.ok:
            return read
        reg = self.registry.register_schema(name, read.result.schema)
        if not reg.success:
            return Response.fail(ErrorCode.FAILED, reg.error or "Failed to register schema",
                                 target=read.result.target)
        result = SnapshotResult(name=name, schema=read.result.schema,
                                version_count=reg.data["version_count"],
                                target=read.result.target, metadata=read.result.metadata)
        if c.create_probe:
            from .probes import RegisterProbe
            config = with_declared_env(c.source)
            probe = self.dispatcher.dispatch(RegisterProbe(config))
            if probe.ok:
                result.probe = probe.result.config
            else:
                result.probe_error = probe.error.message
        return Response.success(result)
