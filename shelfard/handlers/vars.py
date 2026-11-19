"""Template variables (``{{name}}``): set / unset (commands), get / list (queries).

With ``via="mcp"``, variables referenced by a registered probe cannot be changed or
deleted, so an LLM cannot redirect a stored probe (and its credentials).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .core import Command, ErrorCode, Handler, Query, Response, handles
from .probes import VIA_CLI, VIA_MCP

_TEMPLATE_REF = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class SetVar(Command):
    name: str
    value: str
    via: str = VIA_CLI


@dataclass(frozen=True)
class UnsetVar(Command):
    name: str
    via: str = VIA_CLI


@dataclass(frozen=True)
class GetVar(Query):
    name: str


@dataclass(frozen=True)
class ListVars(Query):
    pass


@dataclass
class Var:
    name: str
    value: str

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value}


@dataclass
class VarDeleted:
    name: str

    def to_dict(self) -> dict:
        return {"deleted": self.name}


@dataclass
class VarList:
    vars: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"vars": dict(self.vars)}


# ── Handlers ──────────────────────────────────────────────────────────────────

def vars_used_by_probes(registry) -> set[str]:
    """Every ``{{var}}`` name referenced by any registered probe."""
    used: set[str] = set()
    listing = registry.get_all_probes()
    for summary in listing.data.get("probes", []) if listing.success else []:
        res = registry.get_probe(summary["schema_name"])
        if res.success:
            used.update(_TEMPLATE_REF.findall(json.dumps(res.data["probe"])))
    return used


@handles(SetVar)
class SetVarHandler(Handler[SetVar, Var]):
    def handle(self, c: SetVar) -> Response[Var]:
        if c.via == VIA_MCP and c.name in vars_used_by_probes(self.registry):
            return Response.fail(
                ErrorCode.REFUSED,
                f"Template variable '{c.name}' is used by a registered probe and cannot be "
                f"changed through MCP. Use 'shelfard var set {c.name} <value>' instead.")
        return Response.from_tool_result(self.registry.set_var(c.name, c.value),
                                         lambda d: Var(d["name"], d["value"]))


@handles(UnsetVar)
class UnsetVarHandler(Handler[UnsetVar, VarDeleted]):
    def handle(self, c: UnsetVar) -> Response[VarDeleted]:
        if c.via == VIA_MCP and c.name in vars_used_by_probes(self.registry):
            return Response.fail(
                ErrorCode.REFUSED,
                f"Template variable '{c.name}' is used by a registered probe and cannot be "
                f"deleted through MCP. Use 'shelfard var unset {c.name}' instead.")
        return Response.from_tool_result(self.registry.delete_var(c.name),
                                         lambda d: VarDeleted(d["deleted"]))


@handles(GetVar)
class GetVarHandler(Handler[GetVar, Var]):
    def handle(self, q: GetVar) -> Response[Var]:
        return Response.from_tool_result(self.registry.get_var(q.name),
                                         lambda d: Var(d["name"], d["value"]))


@handles(ListVars)
class ListVarsHandler(Handler[ListVars, VarList]):
    def handle(self, q: ListVars) -> Response[VarList]:
        return Response.from_tool_result(self.registry.list_vars(),
                                         lambda d: VarList(dict(d["vars"])))
