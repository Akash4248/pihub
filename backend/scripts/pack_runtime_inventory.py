#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pack_index_entries(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _sqlite_count(db_path: Path, table: str) -> int | None:
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute(
            "select name from sqlite_master where type = 'table' and name = ?",
            (table,),
        ).fetchone()
        if row is None:
            return None
        return int(connection.execute(f"select count(*) from {table}").fetchone()[0])


def inventory(storage_path: Path, pihub_db_path: Path | None = None) -> dict[str, Any]:
    pack_index_path = storage_path / "pack_index.json"
    entries = _pack_index_entries(pack_index_path)
    archives = [
        path
        for pattern in ("*.tar.gz", "*.tgz", "*.zip", "*.tar")
        for path in storage_path.rglob(pattern)
        if path.is_file()
    ] if storage_path.exists() else []
    manifests = list(storage_path.rglob("manifest.json")) if storage_path.exists() else []

    missing_archives: list[str] = []
    missing_manifests: list[str] = []
    for record in entries:
        archive_path = Path(str(record.get("archive_path") or ""))
        pack_dir = Path(str(record.get("pack_dir") or ""))
        if not archive_path.exists():
            missing_archives.append(str(record.get("pack_id")))
        if not (pack_dir / "manifest.json").exists():
            missing_manifests.append(str(record.get("pack_id")))

    report: dict[str, Any] = {
        "storage_path": str(storage_path),
        "pack_index_path": str(pack_index_path),
        "pack_index_exists": pack_index_path.exists(),
        "pack_index_entries": len(entries),
        "archive_count": len(archives),
        "manifest_count": len(manifests),
        "missing_archive_records": missing_archives,
        "missing_manifest_records": missing_manifests,
        "sample_pack_ids": [str(item.get("pack_id")) for item in entries[:5]],
    }
    if pihub_db_path is not None:
        report["pihub_db_path"] = str(pihub_db_path)
        report["pihub_pack_count"] = _sqlite_count(pihub_db_path, "packs")
        report["pihub_pack_cache_count"] = _sqlite_count(pihub_db_path, "pack_cache")
        report["pihub_pack_versions_count"] = _sqlite_count(pihub_db_path, "pack_versions")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect PIHUB runtime pack publication inventory.")
    parser.add_argument("--storage-path", default=os.getenv("PACK_STORAGE_PATH", "/shared/packs"))
    parser.add_argument("--pihub-db-path", default=os.getenv("PIHUB_DB_PATH"))
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    report = inventory(
        Path(args.storage_path),
        Path(args.pihub_db_path) if args.pihub_db_path else None,
    )
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
