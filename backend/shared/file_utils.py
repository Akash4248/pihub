"""Crash-safe file writes shared by backend services.

Pattern: write to a temp sibling, flush + fsync, then os.replace (atomic on
POSIX). A crash mid-write can never leave a torn target file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".part"
    )
    try:
        with os.fdopen(fd, "wb") as sink:
            sink.write(data)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | Path, obj: Any, indent: int = 2) -> Path:
    return atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False))
