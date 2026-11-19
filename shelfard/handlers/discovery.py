"""Discovery: find readable sources under a local directory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .core import Handler, Query, Response, handles
from .sources import resolve_local_path


@dataclass(frozen=True)
class ListFileTables(Query):
    """Parquet / ORC / Avro sources in a directory (one level deep)."""
    path: str


@dataclass(frozen=True)
class ListIcebergTables(Query):
    """Iceberg tables under a Hadoop-catalog warehouse directory."""
    path: str


@dataclass
class SourceEntry:
    name: str
    path: str
    file_format: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "path": self.path}
        if self.file_format is not None:
            d["file_format"] = self.file_format
        return d


@dataclass
class SourceListing:
    path: str
    tables: list[SourceEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "tables": [t.to_dict() for t in self.tables]}


def _listing(path: str, tr) -> Response[SourceListing]:
    return Response.from_tool_result(tr, lambda d: SourceListing(path, [
        SourceEntry(t["name"], t["path"], t.get("file_format")) for t in d["tables"]]))


@handles(ListFileTables)
class ListFileTablesHandler(Handler[ListFileTables, SourceListing]):
    def handle(self, q: ListFileTables) -> Response[SourceListing]:
        from ..tools.files import list_file_tables
        path = resolve_local_path(self.registry, q.path)
        return _listing(path, list_file_tables(path))


@handles(ListIcebergTables)
class ListIcebergTablesHandler(Handler[ListIcebergTables, SourceListing]):
    def handle(self, q: ListIcebergTables) -> Response[SourceListing]:
        from ..tools.iceberg import list_iceberg_tables
        path = resolve_local_path(self.registry, q.path)
        return _listing(path, list_iceberg_tables(path))
