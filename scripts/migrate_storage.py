"""
Migrate a local Shelfard registry to the current storage layout.

  * ``checkers/*.json``  → ``probes/*.json``, key ``checker_type`` → ``probe_type``
  * ``consumers/<consumer>/<table>.json``: versioned subscription snapshots
    → a single, unversioned consumer projection (the latest snapshot's columns
    become the projection's paths and expected shape)

A copy of the registry is written to ``<registry>.bak-<timestamp>`` first.
Idempotent: running it again changes nothing (and makes no new backup).

Usage:
  python scripts/migrate_storage.py [REGISTRY_DIR]     # default: ./schemas
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shelfard.registry.local import upgrade_consumer_file  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def pending_changes(root: Path) -> list[str]:
    todo = []
    old = root / "checkers"
    if old.exists():
        todo += [f"move {p.relative_to(root)}" for p in sorted(old.glob("*.json"))]
    probes = root / "probes"
    if probes.exists():
        todo += [f"rename checker_type in {p.relative_to(root)}"
                 for p in sorted(probes.glob("*.json")) if "checker_type" in _load(p)]
    consumers = root / "consumers"
    if consumers.exists():
        todo += [f"flatten {p.relative_to(root)}"
                 for p in sorted(consumers.glob("*/*.json")) if "versions" in _load(p)]
    return todo


def migrate(root: Path, backup: bool = True) -> list[str]:
    """Apply the migration; returns the actions taken (empty if up to date)."""
    todo = pending_changes(root)
    if not todo:
        return []
    if backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copytree(root, root.with_name(f"{root.name}.bak-{stamp}"))

    old, probes = root / "checkers", root / "probes"
    if old.exists():
        probes.mkdir(exist_ok=True)
        for path in sorted(old.glob("*.json")):
            target = probes / path.name
            if target.exists():
                raise SystemExit(f"Both {path} and {target} exist — resolve manually.")
            shutil.move(str(path), target)
        if not any(old.iterdir()):
            old.rmdir()
    if probes.exists():
        for path in sorted(probes.glob("*.json")):
            data = _load(path)
            if "checker_type" in data:
                data = {("probe_type" if k == "checker_type" else k): v for k, v in data.items()}
                _save(path, data)

    consumers = root / "consumers"
    if consumers.exists():
        for path in sorted(consumers.glob("*/*.json")):
            data = _load(path)
            if "versions" in data:
                _save(path, upgrade_consumer_file(data))
    return todo


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("registry", nargs="?", default="schemas", help="Registry directory (default: ./schemas)")
    args = ap.parse_args()
    root = Path(args.registry).resolve()
    if not root.is_dir():
        raise SystemExit(f"No registry at {root}")
    done = migrate(root)
    if not done:
        print(f"{root} is already up to date.")
        return
    print(f"Migrated {root} (backup: {root.name}.bak-*):")
    for line in done:
        print(f"  • {line}")


if __name__ == "__main__":
    main()
