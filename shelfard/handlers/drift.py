"""Drift checks (query) and baseline updates (command)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import ChangeSeverity, ProbeConfig, SchemaDiff, probe_config_from_dict
from .core import Command, ErrorCode, Handler, Query, Response, handles
from .sources import describe, read_source


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckDrift(Query):
    """Compare a live schema with the registered baseline of ``name``, with consumer
    impact. ``source=None`` runs the schema's registered probe; otherwise ``source``
    is read ad hoc. Never writes."""
    name: str
    source: Optional[ProbeConfig] = None


@dataclass(frozen=True)
class AcceptUpdate(Command):
    """Save a checked diff's suggested update as the next baseline version.
    Kind "unaffected" (WARNING/BREAKING changes no consumer reads) needs
    ``allow_unaffected``. Consumer projections are never changed."""
    name: str
    diff: SchemaDiff
    baseline_version: str
    allow_unaffected: bool = False


@dataclass
class DriftReport:
    schema_name: str
    diff: SchemaDiff
    baseline_version: str
    baseline_version_number: Optional[int] = None
    target: Optional[str] = None
    source_kind: Optional[str] = None             # rest / postgres / file / iceberg
    metadata: dict = field(default_factory=dict)  # e.g. Iceberg "metadata_file"

    @property
    def suggested_update(self) -> Optional[dict]:
        return self.diff.suggested_update

    def to_dict(self) -> dict:
        return {"schema_name": self.schema_name, "baseline_version": self.baseline_version,
                "baseline_version_number": self.baseline_version_number,
                **self.metadata, "diff": self.diff.to_dict()}


@dataclass
class UpdateResult:
    schema_name: str
    version_count: int
    kind: str
    explanation: list[str] = field(default_factory=list)
    outdated_consumers: list[dict] = field(default_factory=list)
    registered_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {"registered_at": self.registered_at, "version_count": self.version_count,
                "explanation": self.explanation, "kind": self.kind,
                "outdated_consumers": self.outdated_consumers}


# ── Handlers ──────────────────────────────────────────────────────────────────

_RUN_KEYS = {"schema_name", "baseline_version", "baseline_version_number", "diff"}


@handles(CheckDrift)
class CheckDriftHandler(Handler[CheckDrift, DriftReport]):
    def handle(self, q: CheckDrift) -> Response[DriftReport]:
        return self._ad_hoc(q) if q.source is not None else self._registered(q)

    def _registered(self, q: CheckDrift) -> Response[DriftReport]:
        target = kind = None
        stored = self.registry.get_probe(q.name)
        if stored.success:
            try:
                config = probe_config_from_dict(stored.data["probe"])
                target, kind = describe(config, self.registry), config.probe_type
            except ValueError:
                pass
        tr = self.registry.run_probe(q.name)
        if not tr.success:
            return Response.from_tool_result(tr).with_details(target=target, source_kind=kind)
        d = tr.data
        return Response.success(DriftReport(
            schema_name=q.name, diff=SchemaDiff.from_dict(d["diff"]),
            baseline_version=d["baseline_version"],
            baseline_version_number=d.get("baseline_version_number"),
            target=target, source_kind=kind,
            metadata={k: v for k, v in d.items() if k not in _RUN_KEYS},
        ))

    def _ad_hoc(self, q: CheckDrift) -> Response[DriftReport]:
        kind = q.source.probe_type
        read = read_source(q.source, self.registry)
        if not read.ok:
            return read.with_details(source_kind=kind)
        target = read.result.target
        baseline = self.registry.get_registered_schema(q.name)
        if not baseline.success:
            return Response.fail(ErrorCode.NOT_FOUND, f"No snapshot found for '{q.name}'.",
                                 target=target, source_kind=kind)
        diff = self.registry.compare_with_baseline(q.name, baseline.data["schema"],
                                                   read.result.schema.to_dict())
        if not diff.success:
            return Response.fail(ErrorCode.FAILED, f"Comparison failed: {diff.error}",
                                 target=target, source_kind=kind)
        return Response.success(DriftReport(
            schema_name=q.name, diff=SchemaDiff.from_dict(diff.data["diff"]),
            baseline_version=baseline.data["version"],
            baseline_version_number=baseline.data.get("version_number"),
            target=target, source_kind=kind, metadata=read.result.metadata,
        ))


def update_status(diff: SchemaDiff, allow_unaffected: bool) -> Optional[str]:
    """Why AcceptUpdate would refuse this diff up front (None = it may proceed)."""
    update = diff.suggested_update
    if not update:
        return "no_update"
    if update.get("kind", "safe") == "unaffected" and not allow_unaffected:
        return "needs_allow_unaffected"
    return None


@handles(AcceptUpdate)
class AcceptUpdateHandler(Handler[AcceptUpdate, UpdateResult]):
    def handle(self, c: AcceptUpdate) -> Response[UpdateResult]:
        status = update_status(c.diff, c.allow_unaffected)
        if status == "no_update":
            return Response.fail(
                ErrorCode.REFUSED,
                "No update to apply: the check found no changes, or some WARNING/BREAKING "
                "changes affect consumers and need review.", reason=status)
        if status == "needs_allow_unaffected":
            n = sum(1 for ch in c.diff.changes if ch.severity != ChangeSeverity.SAFE)
            return Response.fail(
                ErrorCode.REFUSED,
                "This update contains WARNING/BREAKING changes that no consumer reads; "
                "accepting it needs allow_unaffected (CLI: --update --allow-unaffected).",
                reason=status, risky_changes=n)
        tr = self.registry.accept_update(c.name, c.diff.to_dict(), c.baseline_version,
                                         allow_unaffected=c.allow_unaffected)
        if not tr.success:
            msg = tr.error or ""
            code = (ErrorCode.CONFLICT if "changed since this check" in msg
                    else ErrorCode.REFUSED if "would break" in msg
                    else None)
            return Response.from_tool_result(tr, code=code)
        d = tr.data
        return Response.success(UpdateResult(
            schema_name=c.name, version_count=d["version_count"], kind=d.get("kind", "safe"),
            explanation=d.get("explanation", []), outdated_consumers=d.get("outdated_consumers") or [],
            registered_at=d.get("registered_at"),
        ), hint=tr.next_action_hint)
