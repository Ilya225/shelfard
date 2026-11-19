"""
Command/query handlers — the execution layer behind every controller.

Controllers (CLI, MCP, a future REST/gRPC/UI layer) build a payload, dispatch it and
render the typed :class:`Response`::

    from shelfard.handlers import dispatch, CheckDrift

    resp = dispatch(CheckDrift("orders"))          # run the registered probe
    if resp.ok:
        print(resp.result.diff.overall_severity)
    else:
        print(resp.error.code, resp.error.message)

Use ``Dispatcher(registry)`` to work on a specific SchemaRegistry. Importing this
package registers every handler.
"""

from .core import (
    Command, Dispatcher, Error, ErrorCode, Handler, Query, Response,
    classify_error, dispatch, handles, registered_payloads,
)
from .sources import (
    SourceRead, declared_env, describe, extract_env_vars, read_source, resolve_local_path,
    with_declared_env,
)
from .schemas import (
    GetSchema, ListSchemas, SchemaList, SchemaSummary, SchemaVersion, SnapshotResult,
    SnapshotSchema,
)
from .drift import AcceptUpdate, CheckDrift, DriftReport, UpdateResult, update_status
from .consumers import (
    GetSubscription, ListSubscriptions, ProjectionList, ProjectionState, ProjectionView,
    RefreshConsumer, RefreshResult, Subscribe, Subscribed, Subscription, Unsubscribe,
    Unsubscribed,
)
from .probes import (
    VIA_CLI, VIA_MCP, GetProbe, ListProbes, ProbeInfo, ProbeList, ProbeRegistered,
    ProbeSummary, RegisterProbe,
)
from .vars import GetVar, ListVars, SetVar, UnsetVar, Var, VarDeleted, VarList
from .contracts import (
    CheckContract, ContractCheck, ContractExport, ContractImport, ExportContract,
    ImportContract,
)
from .discovery import ListFileTables, ListIcebergTables, SourceEntry, SourceListing

__all__ = [
    # core
    "Command", "Query", "Handler", "Dispatcher", "Response", "Error", "ErrorCode",
    "dispatch", "handles", "registered_payloads", "classify_error",
    # sources
    "SourceRead", "read_source", "describe", "declared_env", "with_declared_env",
    "extract_env_vars", "resolve_local_path",
    # schemas
    "GetSchema", "ListSchemas", "SnapshotSchema",
    "SchemaVersion", "SchemaSummary", "SchemaList", "SnapshotResult",
    # drift
    "CheckDrift", "AcceptUpdate", "DriftReport", "UpdateResult", "update_status",
    # consumers
    "Subscribe", "Unsubscribe", "RefreshConsumer", "GetSubscription", "ListSubscriptions",
    "Subscribed", "Unsubscribed", "RefreshResult", "Subscription",
    "ProjectionList", "ProjectionView", "ProjectionState",
    # probes
    "RegisterProbe", "GetProbe", "ListProbes", "ProbeRegistered", "ProbeInfo", "ProbeList",
    "ProbeSummary", "VIA_CLI", "VIA_MCP",
    # vars
    "SetVar", "UnsetVar", "GetVar", "ListVars", "Var", "VarDeleted", "VarList",
    # contracts
    "ExportContract", "ImportContract", "CheckContract",
    "ContractExport", "ContractImport", "ContractCheck",
    # discovery
    "ListFileTables", "ListIcebergTables", "SourceEntry", "SourceListing",
]
