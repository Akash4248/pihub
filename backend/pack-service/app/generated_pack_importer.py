import json
import logging
from pathlib import Path
from typing import Any

from app.pack_storage.pack_repository import PackRepository
import httpx

logger = logging.getLogger(__name__)

class GeneratedPackImporter:
    def __init__(self, pack_repository: PackRepository, content_pipeline_url: str):
        self.pack_repository = pack_repository
        self.content_pipeline_url = content_pipeline_url

    async def import_pack(self, source_dir: Path) -> dict[str, Any]:
        """Import the raw generated_pack folder into the official Pack Service storage."""
        if not source_dir.exists() or not source_dir.is_dir():
            raise ValueError(f"Source directory {source_dir} does not exist.")

        pack_id = "generated_pack_v1"
        
        # Determine total items to approximate artifact_counts
        artifacts = {}
        for file_path in source_dir.glob("*.json"):
            if file_path.name.isupper():
                continue # Skip reports like RUN_REPORT.json
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                artifacts[file_path.stem] = data
            except Exception as e:
                logger.warning(f"Failed to parse {file_path.name}: {e}")

        pack_data = {
            "pack_id": pack_id,
            "version": "1.0.0",
            "grade": 0,  # Generic grade for mixed pack
            "subject": "mixed",
            "chapter": "generated_pack",
            "language": "english",
            "artifacts": artifacts,
            "generation_metadata": {"source": "kaggle_generated_pack"},
            "quality_scores": {}
        }

        logger.info(f"Saving generated pack: {pack_id}")
        record = self.pack_repository.save_pack(pack_data)
        logger.info(f"Imported pack stored successfully: {pack_id}")
        return record
