from __future__ import annotations

import os
from pathlib import Path


def _has_pdfs(path: Path) -> bool:
    try:
        return path.is_dir() and next(path.rglob("*.pdf"), None) is not None
    except OSError:
        return False


def _candidate_roots(start: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("TEXTBOOKS_ROOT", "TEXTBOOKS_DIR", "PDF_LIBRARY_PATH"):
        value = os.getenv(env_name)
        if value:
            candidates.append(Path(value).expanduser())

    origin = (start or Path(__file__)).resolve()
    for ancestor in (origin, *origin.parents):
        candidates.append(ancestor / "TEXTBOOKS")

    candidates.append(Path("/shared/textbooks"))
    candidates.append(Path("/home/akash/Desktop/NOTIMP/TEXTBOOKS"))
    return candidates


def find_textbooks_root(start: Path | None = None) -> Path | None:
    seen: set[str] = set()
    for candidate in _candidate_roots(start):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if _has_pdfs(resolved):
            return resolved
    return None


def default_textbooks_root(fallback: Path) -> str:
    root = find_textbooks_root()
    return str(root) if root else str(fallback)
