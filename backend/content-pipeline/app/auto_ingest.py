"""
Auto-ingestion engine for curriculum-aware content pipeline.

Monitors content/ directory for new PDFs and automatically:
1. Extracts text
2. Detects metadata
3. Generates chunks
4. Computes embeddings
5. Inserts into Qdrant

Uses watchdog file system monitoring for real-time processing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class ContentWatchHandler(FileSystemEventHandler):
    """File system event handler for content directory."""
    
    def __init__(self, on_pdf_created: Callable[[Path], None]):
        self.on_pdf_created = on_pdf_created
    
    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return
        
        path = Path(event.src_path)
        
        # Only process PDF files
        if path.suffix.lower() != ".pdf":
            return
        
        logger.info(f"PDF detected: {path}")
        self.on_pdf_created(path)
    
    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events (for safety, re-process)."""
        if event.is_directory:
            return
        
        path = Path(event.src_path)
        if path.suffix.lower() == ".pdf":
            logger.info(f"PDF modified: {path}")
            # Could implement version tracking here
            # For now, skip re-ingestion
            pass


class AutoIngestionEngine:
    """
    Main auto-ingestion engine.
    
    Monitors content/ directory and processes new PDFs.
    """
    
    def __init__(self, content_dir: Path, ingest_callback: Callable[[Path], Awaitable[None]]):
        """
        Initialize auto-ingestion engine.
        
        Args:
            content_dir: Path to monitor (e.g., /storage/content/)
            ingest_callback: Async callback to ingest PDF
                Signature: async def ingest_pdf(file_path: Path)
        """
        self.content_dir = content_dir
        self.ingest_callback = ingest_callback
        self.observer: Observer | None = None
        self.processed_files: set[str] = set()
    
    async def start(self) -> None:
        """Start watching directory for new PDFs."""
        self.content_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Starting auto-ingestion engine: {self.content_dir}")
        
        # Process existing PDFs first
        await self._process_existing_pdfs()
        
        # Start file watcher
        handler = ContentWatchHandler(on_pdf_created=self._on_pdf_created)
        self.observer = Observer()
        self.observer.schedule(handler, str(self.content_dir), recursive=True)
        self.observer.start()
        
        logger.info("Auto-ingestion engine started")
    
    async def stop(self) -> None:
        """Stop watching directory."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("Auto-ingestion engine stopped")
    
    async def _process_existing_pdfs(self) -> None:
        """Process any existing PDFs in directory."""
        pdfs = list(self.content_dir.rglob("*.pdf"))
        logger.info(f"Found {len(pdfs)} existing PDFs")
        
        for pdf_path in pdfs:
            if str(pdf_path) not in self.processed_files:
                await self._ingest_pdf(pdf_path)
    
    def _on_pdf_created(self, path: Path) -> None:
        """Handle new PDF (called from file watcher thread)."""
        try:
            loop = getattr(self, "_loop", None) or asyncio.get_event_loop()
            if loop and loop.is_running():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._ingest_pdf(path)))
        except Exception as exc:
            logger.warning("Could not schedule pdf ingestion safely: %s", exc)

    
    async def _ingest_pdf(self, path: Path) -> None:
        """Ingest a PDF file."""
        if str(path) in self.processed_files:
            logger.debug(f"Already processed: {path}")
            return
        
        try:
            logger.info(f"Ingesting PDF: {path}")
            
            # Call the ingestion callback
            await self.ingest_callback(path)
            
            self.processed_files.add(str(path))
            logger.info(f"Successfully ingested: {path}")
        
        except Exception as exc:
            logger.error(f"Failed to ingest {path}: {exc}")


# Integration helper for FastAPI

class AutoIngestionService:
    """
    Wrapper service for integration with FastAPI.
    
    Lifecycle:
    - startup: Start watching directory
    - shutdown: Stop watching directory
    """
    
    def __init__(self, content_dir: Path, ingest_callback: Callable):
        self.engine = AutoIngestionEngine(content_dir, ingest_callback)
        self.startup_done = False
    
    async def startup(self) -> None:
        """Start auto-ingestion on app startup."""
        await self.engine.start()
        self.startup_done = True
    
    async def shutdown(self) -> None:
        """Stop auto-ingestion on app shutdown."""
        await self.engine.stop()
        self.startup_done = False


# Example usage

async def example_ingestion_callback(pdf_path: Path) -> None:
    """Example callback for ingesting PDF."""
    logger.info(f"EXAMPLE: Would ingest {pdf_path}")
    # In real usage, this would:
    # 1. Extract text from PDF
    # 2. Extract metadata from path
    # 3. Detect chapters
    # 4. Create chunks
    # 5. Generate embeddings
    # 6. Insert into Qdrant
    await asyncio.sleep(1)  # Simulate processing


async def main():
    """Example usage."""
    logging.basicConfig(level=logging.INFO)
    
    content_dir = Path("/storage/content/")
    service = AutoIngestionService(content_dir, example_ingestion_callback)
    
    try:
        await service.startup()
        
        # Watch for 10 seconds
        await asyncio.sleep(10)
    
    finally:
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
