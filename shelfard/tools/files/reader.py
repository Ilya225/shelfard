"""
File-format schema reader: Parquet, ORC and Avro.

``FileReader(path, schema_name, file_format="auto")`` reads the schema from a
local file or directory. Only metadata is read (Parquet/ORC footers, the Avro
container header), never row data.

Format auto-detection:
  * file extension: ``.parquet``/``.parq``, ``.orc``, ``.avro``/``.avsc``
  * otherwise the file's magic bytes (``PAR1``, ``ORC``, ``Obj\\x01``)
  * for directories, the first data file found (names starting with
    ``_`` or ``.`` such as ``_SUCCESS`` / ``.crc`` are skipped)

Iceberg tables are a table format, not files, and are handled by
:mod:`shelfard.tools.iceberg`. Their data folders hold Parquet files, so this
reader refuses Iceberg tables explicitly rather than reading them as a plain
Parquet dataset.

Directories of Parquet/ORC files are read as one dataset with Hive-style
partition discovery (``key=value`` folders). For Avro directories the first
data file's writer schema is used.

Remote URIs (``s3://``, ``gs://``, ``hdfs://``, ...) are not supported yet.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from ..base import SchemaReader
from ...models import TableSchema, ToolResult

FILE_FORMATS = ("parquet", "orc", "avro")

_EXTENSIONS: dict[str, str] = {
    ".parquet": "parquet",
    ".parq":    "parquet",
    ".orc":     "orc",
    ".avro":    "avro",
    ".avsc":    "avro",
}

_MAGIC: list[tuple[bytes, str]] = [
    (b"PAR1", "parquet"),
    (b"ORC", "orc"),
    (b"Obj\x01", "avro"),
]


def _is_hidden(p: Path) -> bool:
    return p.name.startswith((".", "_"))


def _is_iceberg(path: Path) -> bool:
    from ..iceberg.metadata import is_iceberg_table_dir
    name = path.name.lower()
    if path.is_file():
        return name.endswith(".metadata.json") or name.endswith(".metadata.json.gz")
    return path.is_dir() and is_iceberg_table_dir(path)


def _format_from_file(p: Path) -> str | None:
    if p.suffix.lower() in _EXTENSIONS:
        return _EXTENSIONS[p.suffix.lower()]
    try:
        with open(p, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    return None


def _data_files(root: Path):
    """Yield non-hidden files under *root* in a stable order (hidden dirs are pruned)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for name in sorted(filenames):
            if not name.startswith((".", "_")):
                yield Path(dirpath) / name


def detect_format(path: Path) -> str | None:
    """Best-effort detection of the file format at *path* (file or directory)."""
    if _is_iceberg(path):
        return None
    if path.is_file():
        return _format_from_file(path)
    if path.is_dir():
        for f in _data_files(path):
            fmt = _format_from_file(f)
            if fmt:
                return fmt
    return None


def _first_avro_file(path: Path) -> Path:
    if path.is_file():
        return path
    for f in _data_files(path):
        if f.suffix.lower() in (".avro", ".avsc") or _format_from_file(f) == "avro":
            return f
    raise FileNotFoundError(f"No Avro files found under {path}")


class FileReader(SchemaReader):
    """Reads the schema of a Parquet / ORC / Avro file or dataset."""

    def __init__(self, path: str, schema_name: str, file_format: str = "auto"):
        self.path = path
        self.schema_name = schema_name
        self.file_format = (file_format or "auto").lower()

    def _resolve(self) -> tuple[Path | None, str | None, ToolResult | None]:
        if "://" in self.path:
            return None, None, ToolResult(
                success=False,
                error=f"Remote paths are not supported yet: {self.path}",
                next_action_hint="Copy the file or table metadata locally, or mount the bucket.",
            )
        path = Path(self.path).expanduser()
        if not path.exists():
            return None, None, ToolResult(success=False, error=f"Path not found: {self.path}")

        if _is_iceberg(path):
            return None, None, ToolResult(
                success=False,
                error=f"{self.path} is an Iceberg table, not a plain file dataset.",
                next_action_hint="Use 'shelfard iceberg snapshot' / get_iceberg_schema() instead.",
            )

        fmt = self.file_format
        if fmt == "auto":
            fmt = detect_format(path)
            if fmt is None:
                return None, None, ToolResult(
                    success=False,
                    error=f"Could not detect the file format of {self.path}",
                    next_action_hint=f"Pass the format explicitly: one of {', '.join(FILE_FORMATS)}.",
                )
        elif fmt not in FILE_FORMATS:
            return None, None, ToolResult(
                success=False,
                error=f"Unsupported file format {fmt!r}. Supported: {', '.join(FILE_FORMATS)}.",
            )
        return path, fmt, None

    def get_schema(self) -> ToolResult:
        path, fmt, err = self._resolve()
        if err:
            return err

        partition_keys: list[str] = []
        data: dict = {"file_format": fmt}
        try:
            if fmt in ("parquet", "orc"):
                from .arrow import read_arrow_schema
                columns, partition_keys = read_arrow_schema(path, fmt)
            else:
                from .avro import avro_schema_to_columns, read_avro_schema_json
                source_file = _first_avro_file(path)
                columns = avro_schema_to_columns(read_avro_schema_json(source_file))
                data["source_file"] = str(source_file)
        except ImportError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read {fmt} schema from {self.path}: {e}")

        schema = TableSchema(
            table_name=self.schema_name,
            columns=columns,
            partition_keys=partition_keys,
            source=fmt,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        data["schema"] = schema.to_dict()
        return ToolResult(
            success=True,
            data=data,
            next_action_hint="Now call get_registered_schema() and compare_schemas() to detect drift.",
        )

    def list_tables(self) -> ToolResult:
        """List readable sources directly under a directory (or the file itself)."""
        path = Path(self.path).expanduser()
        if "://" in self.path or not path.exists():
            return ToolResult(success=False, error=f"Path not found: {self.path}")
        if path.is_file():
            entries = [path] if _format_from_file(path) else []
        else:
            entries = [p for p in sorted(path.iterdir()) if not _is_hidden(p) and detect_format(p)]
        tables = [{"name": p.name, "path": str(p), "file_format": detect_format(p)} for p in entries]
        return ToolResult(
            success=True,
            data={"tables": tables, "count": len(tables)},
            next_action_hint="Call get_file_schema() on any path you want to inspect.",
        )


# ── Module-level wrappers ─────────────────────────────────────────────────

def get_file_schema(path: str, schema_name: str, file_format: str = "auto") -> ToolResult:
    return FileReader(path, schema_name, file_format).get_schema()


def list_file_tables(path: str) -> ToolResult:
    return FileReader(path, "").list_tables()
