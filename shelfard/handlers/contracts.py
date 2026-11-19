"""ODCS data contracts: export, check (queries) and import (command).

Handlers take parsed contract documents; reading and writing contract files is the
controller's job (the CLI does it, MCP never does).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .. import odcs
from ..models import SchemaDiff, TableSchema
from ..schema_comparison import compare_schemas
from .core import Command, ErrorCode, Handler, Query, Response, handles


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExportContract(Query):
    name: str
    version: Optional[int] = None       # 1-based registry version (default: latest)


@dataclass(frozen=True)
class ImportContract(Command):
    document: dict
    name_prefix: str = ""


@dataclass(frozen=True)
class CheckContract(Query):
    """Compare a contract's schema object with a live schema (``probe``) or the latest
    registered version of ``name`` (with consumer impact). When every change is SAFE the
    result carries the updated contract document; writing it is up to the caller."""
    document: dict
    probe: Optional[str] = None
    name: Optional[str] = None
    object: Optional[str] = None


@dataclass
class ContractExport:
    contract: dict
    version_number: int
    contract_version: str

    def to_dict(self) -> dict:
        return {"contract": self.contract, "version_number": self.version_number,
                "contract_version": self.contract_version}


@dataclass
class ContractImport:
    registered: list[dict] = field(default_factory=list)    # [{name, version_count}]
    ignored: dict = field(default_factory=dict)              # ODCS field → paths not imported

    def to_dict(self) -> dict:
        return {"registered": self.registered, "ignored": self.ignored}


@dataclass
class ContractCheck:
    label: str                       # the probe or schema name checked
    object_name: str
    contract_version: str
    diff: SchemaDiff
    updated_contract: Optional[dict] = None    # set when the changes can be written (all SAFE)

    @property
    def new_version(self) -> Optional[str]:
        return (self.updated_contract or {}).get("version")

    def to_dict(self) -> dict:
        return {"label": self.label, "object": self.object_name,
                "contract_version": self.contract_version, "diff": self.diff.to_dict(),
                "updated_contract": self.updated_contract}


# ── Handlers ──────────────────────────────────────────────────────────────────

@handles(ExportContract)
class ExportContractHandler(Handler[ExportContract, ContractExport]):
    def handle(self, q: ExportContract) -> Response[ContractExport]:
        return Response.from_tool_result(
            odcs.export_odcs(q.name, q.version, registry=self.registry),
            lambda d: ContractExport(d["contract"], d["version_number"], d["contract_version"]))


@handles(ImportContract)
class ImportContractHandler(Handler[ImportContract, ContractImport]):
    def handle(self, c: ImportContract) -> Response[ContractImport]:
        return Response.from_tool_result(
            odcs.import_odcs(c.document, name_prefix=c.name_prefix or "", registry=self.registry),
            lambda d: ContractImport(list(d["registered"]), dict(d["ignored"])))


@handles(CheckContract)
class CheckContractHandler(Handler[CheckContract, ContractCheck]):
    def handle(self, q: CheckContract) -> Response[ContractCheck]:
        if bool(q.probe) == bool(q.name):
            return Response.fail(ErrorCode.INVALID, "Give exactly one of probe or name.")
        label = q.probe or q.name
        live = (self.registry.fetch_probe_schema(q.probe) if q.probe
                else self.registry.get_registered_schema(q.name))
        if not live.success:
            return Response.from_tool_result(live)

        doc = q.document
        obj = odcs.find_object(doc, q.object, fallback=label)
        if obj is None:
            names = [o.get("name") for o in doc.get("schema") or [] if isinstance(o, dict)]
            return Response.fail(ErrorCode.NOT_FOUND, "No schema object to check in the contract",
                                 objects=names)
        try:
            contract_schema = odcs.contract_table(doc, obj)
        except ValueError as e:
            return Response.fail(ErrorCode.INVALID, str(e))

        result = compare_schemas(contract_schema, TableSchema.from_dict(live.data["schema"]))
        if not result.success:
            return Response.from_tool_result(result, code=ErrorCode.FAILED)
        diff = result.data["diff"]
        if q.name:     # a registered schema: show the impact on its consumers
            impact = self.registry.assess_consumer_impact(q.name, diff, contract_schema.to_dict(),
                                                          live.data["schema"])
            if impact.success:
                diff = impact.data["diff"]
                if (diff.get("suggested_update") or {}).get("kind") == "unaffected":
                    diff["suggested_update"] = None      # contracts take all-SAFE updates only

        check = ContractCheck(label=label, object_name=obj["name"],
                              contract_version=doc.get("version", "?"),
                              diff=SchemaDiff.from_dict(diff))
        suggestion = diff.get("suggested_update")
        if diff["changes"] and suggestion:
            check.updated_contract = odcs.update_contract(
                doc, obj["name"], TableSchema.from_dict(suggestion["schema"]))
        return Response.success(check)
