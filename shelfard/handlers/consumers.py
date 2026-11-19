"""Consumer projections: subscribe, unsubscribe, refresh (commands) and listings (queries).

Projections belong to the consuming teams: only these consumer-side commands change
them, never a baseline update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import ChangeSeverity, ConsumerProjection, TableSchema
from .core import Command, Handler, Query, Response, handles


# ── Payloads and results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Subscribe(Command):
    """Save (overwrite) ``consumer``'s projection of ``table``; ``columns=None`` = all
    top-level columns. Paths: ``id``, ``address.zip``, ``tags[]``, ``tags[].sku``."""
    consumer: str
    table: str
    columns: Optional[tuple[str, ...]] = None


@dataclass(frozen=True)
class Unsubscribe(Command):
    consumer: str
    table: str


@dataclass(frozen=True)
class RefreshConsumer(Command):
    """The consumer adopts the latest registered shape for the paths it reads."""
    consumer: str
    table: str


@dataclass(frozen=True)
class GetSubscription(Query):
    consumer: str
    table: str


@dataclass(frozen=True)
class ListSubscriptions(Query):
    """Every projection across all tables (``table=None``) or the consumers of one table."""
    table: Optional[str] = None
    with_state: bool = False    # compare each projection with the latest schema


@dataclass
class ProjectionState:
    """How a projection compares with the latest registered schema."""
    up_to_date: bool
    severity: Optional[ChangeSeverity] = None    # the consumer's own severity when outdated
    columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"up_to_date": self.up_to_date,
                "severity": self.severity.value if self.severity else None,
                "columns": self.columns}


@dataclass
class ProjectionView:
    projection: ConsumerProjection
    state: Optional[ProjectionState] = None

    @property
    def consumer(self) -> str:
        return self.projection.consumer_name

    def to_dict(self) -> dict:
        d = {"consumer": self.projection.consumer_name, **self.projection.to_dict()}
        if self.state is not None:
            d["state"] = self.state.to_dict()
        return d


@dataclass
class ProjectionList:
    consumers: list[ProjectionView] = field(default_factory=list)
    table: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"consumers": [c.to_dict() for c in self.consumers]}
        if self.table is not None:
            d["total"] = len(self.consumers)
        return d


@dataclass
class Unsubscribed:
    consumer: str
    table: str

    def to_dict(self) -> dict:
        return {"consumer": self.consumer, "table": self.table}


@dataclass
class RefreshResult:
    projection: ConsumerProjection
    adopted_changes: list[dict] = field(default_factory=list)   # serialized ColumnChanges

    def to_dict(self) -> dict:
        return {"projection": self.projection.to_dict(), "adopted_changes": self.adopted_changes}


@dataclass
class Subscription:
    projection: ConsumerProjection

    def to_dict(self) -> dict:
        return {"subscription": self.projection.to_dict()}


@dataclass
class Subscribed:
    projection: ConsumerProjection

    def to_dict(self) -> dict:
        return {"projection": self.projection.to_dict(),
                "column_count": len(self.projection.columns)}


# ── Handlers ──────────────────────────────────────────────────────────────────

def projection_state(registry, projection: ConsumerProjection) -> Optional[ProjectionState]:
    """Compare a projection with the latest registered schema (None if there is none)."""
    from ..projection import consumer_diff
    latest = registry.get_registered_schema(projection.source_table)
    if not latest.success:
        return None
    d = consumer_diff(projection, TableSchema.from_dict(latest.data["schema"]))
    if not d["changes"]:
        return ProjectionState(up_to_date=True)
    return ProjectionState(up_to_date=False, severity=ChangeSeverity(d["overall_severity"]),
                           columns=[c["column_name"] for c in d["changes"]])


@handles(Subscribe)
class SubscribeHandler(Handler[Subscribe, Subscribed]):
    def handle(self, c: Subscribe) -> Response[Subscribed]:
        columns = list(c.columns) if c.columns is not None else None
        return Response.from_tool_result(
            self.registry.subscribe_consumer(c.consumer, c.table, columns),
            lambda d: Subscribed(ConsumerProjection.from_dict(d["projection"])))


@handles(Unsubscribe)
class UnsubscribeHandler(Handler[Unsubscribe, Unsubscribed]):
    def handle(self, c: Unsubscribe) -> Response[Unsubscribed]:
        return Response.from_tool_result(
            self.registry.unsubscribe_consumer(c.consumer, c.table),
            lambda d: Unsubscribed(c.consumer, c.table))


@handles(RefreshConsumer)
class RefreshConsumerHandler(Handler[RefreshConsumer, RefreshResult]):
    def handle(self, c: RefreshConsumer) -> Response[RefreshResult]:
        return Response.from_tool_result(
            self.registry.refresh_consumer(c.consumer, c.table),
            lambda d: RefreshResult(ConsumerProjection.from_dict(d["projection"]),
                                    list(d.get("adopted_changes") or [])))


@handles(GetSubscription)
class GetSubscriptionHandler(Handler[GetSubscription, Subscription]):
    def handle(self, q: GetSubscription) -> Response[Subscription]:
        return Response.from_tool_result(
            self.registry.get_consumer_subscription(q.consumer, q.table),
            lambda d: Subscription(ConsumerProjection.from_dict(d["subscription"])))


@handles(ListSubscriptions)
class ListSubscriptionsHandler(Handler[ListSubscriptions, ProjectionList]):
    def handle(self, q: ListSubscriptions) -> Response[ProjectionList]:
        tr = (self.registry.get_consumers_for_table(q.table) if q.table is not None
              else self.registry.get_all_consumers())
        if not tr.success:
            return Response.from_tool_result(tr)
        views = []
        for entry in tr.data["consumers"]:
            projection = ConsumerProjection.from_dict(entry)
            state = projection_state(self.registry, projection) if q.with_state else None
            views.append(ProjectionView(projection, state))
        return Response.success(ProjectionList(views, q.table))
