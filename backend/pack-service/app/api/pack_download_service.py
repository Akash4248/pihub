from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Generator

from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from shared.config import get_settings

settings = get_settings()

def _file_chunk_generator(path: Path, chunk_size: int = 65536) -> Generator[bytes, None, None]:
    """Stream file contents in 64KB chunks to avoid reading entire file into RAM."""
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk

class PackDownloadService:
    def download(self, record: dict[str, Any]):
        archive_path = record.get("archive_path")
        if not archive_path:
            raise HTTPException(status_code=404, detail="Pack archive not found")

        path = Path(archive_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Pack archive file missing")

        # Stream chunked bytes to prevent OOM buffer spikes
        return StreamingResponse(
            _file_chunk_generator(path),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )
