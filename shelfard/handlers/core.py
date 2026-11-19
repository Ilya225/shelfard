"""
Transport-agnostic command/query handling.

A *payload* is a frozen dataclass describing one request: a :class:`Command`
(changes state) or a :class:`Query` (only reads). Each payload type has exactly one
:class:`Handler`, registered with :func:`handles`. A :class:`Dispatcher` routes a
payload to its handler and returns a typed :class:`Response`.

Controllers (the CLI, the MCP server, a future REST/gRPC/UI layer) only build
payloads, dispatch them and render responses; all execution lives in handlers.
Handlers never print and never refer to a transport (no CLI flags, no HTTP codes):
failures carry an :class:`ErrorCode` that each controller maps to its own terms.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Callable, Generic, Optional, TypeVar

from ..models import ToolResult

P = TypeVar("P")
T = TypeVar("T")


class Command:
    """Marker base class for payloads that change state."""


class Query:
    """Marker base class for payloads that only read."""


class ErrorCode(str, Enum):
    NOT_FOUND = "not_found"   # the schema / probe / subscription / variable does not exist
    INVALID = "invalid"       # the request itself is malformed or not allowed as stated
    CONFLICT = "conflict"     # the state changed or already exists (stale baseline, overwrite)
    REFUSED = "refused"       # valid request, but a policy says no (e.g. a consumer would break)
    FAILED = "failed"         # the operation was attempted and failed (I/O, source unreachable)


@dataclass(frozen=True)
class Error:
    code: ErrorCode
    message: str
    hint: Optional[str] = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message,
                "hint": self.hint, "details": _serialise(self.details)}


@dataclass
class Response(Generic[T]):
    """Result of a handler: ``result`` on success, ``error`` on failure."""
    result: Optional[T] = None
    error: Optional[Error] = None
    hint: Optional[str] = None          # optional next-step hint on success

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(cls, result: T = None, hint: Optional[str] = None) -> "Response[T]":
        return cls(result=result, hint=hint)

    @classmethod
    def fail(cls, code: ErrorCode, message: str, hint: Optional[str] = None,
             **details: Any) -> "Response[T]":
        return cls(error=Error(code, message, hint, details))

    def with_details(self, **details: Any) -> "Response[T]":
        """A copy of a failed response with extra error details."""
        if self.error is None:
            return self
        merged = {**self.error.details, **details}
        return Response(error=Error(self.error.code, self.error.message, self.error.hint, merged))

    @classmethod
    def from_tool_result(cls, tr: ToolResult, parse: Optional[Callable[[dict], T]] = None,
                         code: Optional[ErrorCode] = None) -> "Response[T]":
        """Adapt a registry/reader ToolResult (the layer below handlers)."""
        if tr.success:
            data = tr.data or {}
            return cls.success(parse(data) if parse else data, hint=tr.next_action_hint)
        return cls.fail(code or classify_error(tr.error), tr.error or "Unknown error",
                        tr.next_action_hint)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "result": _serialise(self.result),
                "error": self.error.to_dict() if self.error else None, "hint": self.hint}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_tool_result(self) -> ToolResult:
        """The legacy ToolResult envelope (``success`` / ``data`` / ``error`` /
        ``next_action_hint``) — what MCP clients and the agent consume."""
        if self.ok:
            return ToolResult(success=True, data=_serialise(self.result), next_action_hint=self.hint)
        return ToolResult(success=False, error=self.error.message, next_action_hint=self.error.hint)

    def to_tool_json(self) -> str:
        return self.to_tool_result().to_json()


_NOT_FOUND_MARKERS = ("not found", "not registered", "does not exist", "no registered",
                      "no probe registered", "no subscription", "no baseline", "no snapshot",
                      "has no versions")


def classify_error(message: Optional[str]) -> ErrorCode:
    """Best-effort ErrorCode for a registry/reader error message."""
    text = (message or "").lower()
    if text.startswith("failed") or "failed:" in text:     # "Failed to read …: path not found"
        return ErrorCode.FAILED
    if any(m in text for m in _NOT_FOUND_MARKERS):
        return ErrorCode.NOT_FOUND
    return ErrorCode.INVALID


def _serialise(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    return value


# ── Handlers and dispatch ─────────────────────────────────────────────────────

class Handler(ABC, Generic[P, T]):
    """Executes one payload type. ``registry`` is the SchemaRegistry to work on;
    ``dispatcher`` lets a handler run other payloads (e.g. snapshot → register probe)."""

    def __init__(self, registry, dispatcher: "Dispatcher") -> None:
        self.registry = registry
        self.dispatcher = dispatcher

    @abstractmethod
    def handle(self, payload: P) -> Response[T]:
        ...


_HANDLERS: dict[type, type[Handler]] = {}


def handles(payload_type: type) -> Callable[[type[Handler]], type[Handler]]:
    """Class decorator: register a Handler for ``payload_type``."""
    def register(handler_cls: type[Handler]) -> type[Handler]:
        if payload_type in _HANDLERS:
            raise RuntimeError(f"{payload_type.__name__} already has a handler")
        _HANDLERS[payload_type] = handler_cls
        return handler_cls
    return register


def registered_payloads() -> dict[type, type[Handler]]:
    return dict(_HANDLERS)


class Dispatcher:
    """Routes payloads to their handlers. ``registry`` defaults to the shared
    local registry (looked up on each dispatch, so tests can patch it)."""

    def __init__(self, registry=None) -> None:
        self._registry = registry

    @property
    def registry(self):
        if self._registry is not None:
            return self._registry
        from ..registry import _default
        return _default

    def dispatch(self, payload) -> Response:
        handler_cls = _HANDLERS.get(type(payload))
        if handler_cls is None:
            raise TypeError(f"No handler registered for {type(payload).__name__}")
        return handler_cls(self.registry, self).handle(payload)


_default_dispatcher = Dispatcher()


def dispatch(payload) -> Response:
    """Dispatch with the default dispatcher (the shared local registry)."""
    return _default_dispatcher.dispatch(payload)
