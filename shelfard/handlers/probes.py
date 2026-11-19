"""Stored probes (drift-check configurations): register (command), read and list (queries).

``RegisterProbe(via="mcp")`` applies the MCP security model — the request comes from an
LLM and is untrusted: names validated, no overwrite, env vars must be allowlisted and
declared, hosts must be public, PostgreSQL is table mode only, no file/Iceberg probes.
The probe is stored with ``registered_via="mcp"`` so it is also guarded at run time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from ..models import (
    PostgresProbeConfig, ProbeConfig, RestProbeConfig, probe_config_from_dict,
)
from ..security import (
    check_env_allowed, check_pg_dsn_safe, check_url_safe, extract_env_refs,
    private_hosts_allowed, query_template_error, validate_name,
)
from .core import Command, ErrorCode, Handler, Query, Response, handles
from .sources import _templates

VIA_CLI = "cli"
VIA_MCP = "mcp"

_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegisterProbe(Command):
    config: ProbeConfig
    via: str = VIA_CLI          # "cli" (trusted operator) or "mcp" (untrusted LLM input)


@dataclass(frozen=True)
class GetProbe(Query):
    name: str


@dataclass(frozen=True)
class ListProbes(Query):
    pass


@dataclass
class ProbeRegistered:
    config: ProbeConfig
    registered_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {"schema_name": self.config.schema_name, "registered_at": self.registered_at}


@dataclass
class ProbeInfo:
    config: ProbeConfig

    def to_dict(self) -> dict:
        return {"probe": self.config.to_dict()}


@dataclass
class ProbeSummary:
    schema_name: str
    probe_type: str
    target: str                 # url / dsn / path (unresolved, as stored)
    env: list[str] = field(default_factory=list)
    registered_at: Optional[str] = None
    registered_via: str = VIA_CLI
    url: str = ""
    dsn: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {"schema_name": self.schema_name, "probe_type": self.probe_type,
                "url": self.url, "dsn": self.dsn, "path": self.path, "env": self.env,
                "registered_at": self.registered_at, "registered_via": self.registered_via}


@dataclass
class ProbeList:
    probes: list[ProbeSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"probes": [p.to_dict() for p in self.probes]}


# ── Validation ────────────────────────────────────────────────────────────────

def _invalid(message: str) -> Response:
    return Response.fail(ErrorCode.INVALID, message)


def _check_config(config: ProbeConfig) -> Optional[Response]:
    """Rules for every probe, whoever registers it."""
    if isinstance(config, PostgresProbeConfig):
        if not config.table and not config.query:
            return _invalid("A postgres probe needs either a table or a query.")
        if config.table and config.query:
            return _invalid("A postgres probe takes either a table or a query, not both.")
        if err := query_template_error(config.query):
            return _invalid(err)
    return None


def _check_mcp(registry, config: ProbeConfig) -> Optional[Response]:
    """Extra rules for probes registered through MCP (LLM-controlled input)."""
    name = config.schema_name
    if err := validate_name(name, "schema name"):
        return _invalid(err)
    if not isinstance(config, (RestProbeConfig, PostgresProbeConfig)):
        return Response.fail(ErrorCode.REFUSED,
                             f"{config.probe_type} probes cannot be registered through MCP.")
    if isinstance(config, PostgresProbeConfig):
        if config.query:
            return Response.fail(ErrorCode.REFUSED,
                                 "Custom SQL queries are not allowed through MCP; use table mode.")
        if not _SQL_IDENT.fullmatch(config.table or ""):
            return _invalid(f"Invalid table name {config.table!r}: must be a plain SQL identifier.")
        if not _SQL_IDENT.fullmatch(config.db_schema or ""):
            return _invalid(f"Invalid db_schema {config.db_schema!r}: must be a plain SQL identifier.")
    if registry.get_probe(name).success:
        return Response.fail(
            ErrorCode.CONFLICT,
            f"A probe for '{name}' already exists. Probes cannot be overwritten "
            f"through MCP; change it with 'shelfard probe register' instead.")

    referenced = extract_env_refs(*_templates(config))
    if err := check_env_allowed(sorted(set(config.env) | referenced)):
        return Response.fail(ErrorCode.REFUSED, err)
    undeclared = sorted(referenced - set(config.env))
    if undeclared:
        return _invalid(f"$VAR references not declared in env: {', '.join(undeclared)}")

    if isinstance(config, RestProbeConfig):
        if err := check_url_safe(registry.resolve_template(config.url)):
            return Response.fail(ErrorCode.REFUSED, err)
    elif not private_hosts_allowed():
        err, _ = check_pg_dsn_safe(registry.resolve_template(config.dsn))
        if err:
            return Response.fail(ErrorCode.REFUSED, err)
    return None


# ── Handlers ──────────────────────────────────────────────────────────────────

@handles(RegisterProbe)
class RegisterProbeHandler(Handler[RegisterProbe, ProbeRegistered]):
    def handle(self, c: RegisterProbe) -> Response[ProbeRegistered]:
        config = c.config
        if c.via == VIA_MCP:
            if refused := _check_mcp(self.registry, config):
                return refused
            config = replace(config, registered_via=VIA_MCP)
        if refused := _check_config(config):
            return refused
        tr = self.registry.register_probe(config.schema_name, config)
        if not tr.success:
            return Response.from_tool_result(tr)
        return Response.success(ProbeRegistered(config, tr.data.get("registered_at")),
                                hint=tr.next_action_hint)


@handles(GetProbe)
class GetProbeHandler(Handler[GetProbe, ProbeInfo]):
    def handle(self, q: GetProbe) -> Response[ProbeInfo]:
        tr = self.registry.get_probe(q.name)
        if not tr.success:
            return Response.from_tool_result(tr)
        try:
            return Response.success(ProbeInfo(probe_config_from_dict(tr.data["probe"])))
        except (KeyError, ValueError) as e:
            return Response.fail(ErrorCode.FAILED, f"Invalid probe config for '{q.name}': {e}")


@handles(ListProbes)
class ListProbesHandler(Handler[ListProbes, ProbeList]):
    def handle(self, q: ListProbes) -> Response[ProbeList]:
        def parse(d: dict) -> ProbeList:
            return ProbeList([ProbeSummary(
                schema_name=p["schema_name"], probe_type=p.get("probe_type", "rest"),
                target=p.get("url") or p.get("dsn") or p.get("path") or "",
                env=list(p.get("env") or []), registered_at=p.get("registered_at"),
                registered_via=p.get("registered_via", VIA_CLI),
                url=p.get("url", ""), dsn=p.get("dsn", ""), path=p.get("path", ""),
            ) for p in d["probes"]])
        return Response.from_tool_result(self.registry.get_all_probes(), parse)
