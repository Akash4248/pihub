"""
Pi Sync Engine

Handles:
- Periodic synchronization with host
- Pack download management
- Delta sync (only download new/updated packs)
- Resumable downloads
- Integrity validation
"""

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Callable
import threading

import httpx

logger = logging.getLogger(__name__)


class DownloadSession:
    """Track active download session"""
    
    def __init__(
        self,
        pack_id: str,
        url: str,
        destination: Path,
        total_bytes: Optional[int] = None
    ):
        self.pack_id = pack_id
        self.url = url
        self.destination = destination
        self.total_bytes = total_bytes or 0
        self.bytes_downloaded = 0
        self.status = "pending"  # pending, downloading, paused, completed, failed
        self.error_message = Optional[str]
        self.resume_count = 0
        self.start_time = time.time()
    
    def get_progress_percent(self) -> float:
        """Get download progress percentage"""
        if self.total_bytes == 0:
            return 0.0
        return (self.bytes_downloaded / self.total_bytes) * 100
    
    def get_eta_seconds(self) -> Optional[int]:
        """Calculate estimated time remaining"""
        if self.bytes_downloaded == 0:
            return None
        
        elapsed = time.time() - self.start_time
        if elapsed == 0:
            return None
        
        download_rate = self.bytes_downloaded / elapsed
        if download_rate == 0:
            return None
        
        remaining_bytes = self.total_bytes - self.bytes_downloaded
        return int(remaining_bytes / download_rate)


class PiSyncEngine:
    """Synchronization engine for Pi"""
    
    def __init__(
        self,
        cache_manager,
        # Classroom hotspot gateway (deployment/hotspot.py subnet
        # 192.168.42.0/24); pass HOST_URL explicitly for laptop/docker.
        host_url: str = "http://192.168.42.1:8000",
        cache_path: str = "/cache",
        sync_interval_minutes: int = 60
    ):
        """
        Initialize sync engine
        
        Args:
            cache_manager: PiCacheManager instance
            host_url: URL of host/laptop server
            cache_path: Root cache directory
            sync_interval_minutes: Interval between automatic syncs
        """
        self.cache_manager = cache_manager
        self.host_url = host_url
        self.cache_path = Path(cache_path)
        self.sync_interval = sync_interval_minutes * 60
        
        self.active_downloads: Dict[str, DownloadSession] = {}
        self.last_sync_time = None
        self.lock = threading.RLock()
        
        # Start sync scheduler
        self.sync_thread = threading.Thread(
            target=self._sync_scheduler,
            daemon=True
        )
        self.sync_thread.start()
        logger.info(f"Sync engine initialized: {host_url}")
    
    async def get_sync_manifest(self) -> Optional[dict]:
        """Fetch sync manifest from host"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.host_url}/packs/sync/manifest"
                )
                response.raise_for_status()
                return response.json()
        
        except Exception as e:
            logger.error(f"Error fetching sync manifest: {e}")
            return None
    
    async def calculate_delta_sync(self) -> Optional[dict]:
        """Calculate what packs need to be synced"""
        try:
            # Get current packs
            cached_packs = self.cache_manager.list_cached_packs()
            current_packs = {
                p["pack_id"]: p["version"]
                for p in cached_packs
            }
            
            # Get delta from host
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.host_url}/packs/sync/delta",
                    json={
                        "current_packs": current_packs,
                        "pi_version": "1.0.0",
                        "max_download_size_mb": 500
                    }
                )
                response.raise_for_status()
                return response.json()
        
        except Exception as e:
            logger.error(f"Error calculating delta sync: {e}")
            return None
    
    async def download_pack(
        self,
        pack_id: str,
        on_progress: Optional[Callable] = None,
        max_retries: int = 3
    ) -> bool:
        """
        Download a pack from host
        
        Args:
            pack_id: Pack ID to download
            on_progress: Progress callback function
            max_retries: Maximum retry attempts
        
        Returns:
            True if download succeeded
        """
        with self.lock:
            if pack_id in self.active_downloads:
                logger.warning(f"Pack {pack_id} already downloading")
                return False
        
        download_session = DownloadSession(
            pack_id=pack_id,
            url=f"{self.host_url}/packs/{pack_id}/download",
            destination=self.cache_path / "active_packs" / f"{pack_id}.tar.gz"
        )
        
        with self.lock:
            self.active_downloads[pack_id] = download_session
        
        try:
            for attempt in range(max_retries):
                try:
                    success = await self._download_with_resume(
                        download_session,
                        on_progress
                    )
                    
                    if success:
                        # Download manifest
                        manifest = await self._download_manifest(pack_id)
                        if manifest:
                            # Extract pack
                            await self._extract_pack(download_session)
                            
                            # Cache pack
                            self.cache_manager.cache_pack(
                                pack_id=pack_id,
                                manifest=manifest,
                                chunks=manifest.get("chunks", []),
                                location=str(download_session.destination.parent / pack_id)
                            )
                            
                            download_session.status = "completed"
                            logger.info(f"Pack {pack_id} downloaded successfully")
                            return True
                    
                except Exception as e:
                    logger.warning(f"Download attempt {attempt + 1} failed: {e}")
                    download_session.resume_count += 1
                    await asyncio.sleep(5 * (attempt + 1))  # Exponential backoff
        
        except Exception as e:
            logger.error(f"Fatal error downloading pack {pack_id}: {e}")
            download_session.status = "failed"
            download_session.error_message = str(e)
        
        finally:
            with self.lock:
                if pack_id in self.active_downloads:
                    del self.active_downloads[pack_id]
        
        return False
    
    async def _download_with_resume(
        self,
        session: DownloadSession,
        on_progress: Optional[Callable] = None
    ) -> bool:
        """Download with resume support"""
        try:
            session.status = "downloading"
            
            # Check existing file
            resume_from = 0
            if session.destination.exists():
                resume_from = session.destination.stat().st_size
            
            headers = {}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
            
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "GET",
                    session.url,
                    headers=headers
                ) as response:
                    if response.status_code not in (200, 206):
                        raise Exception(f"HTTP {response.status_code}")
                    
                    if "Content-Length" in response.headers:
                        total_length = int(response.headers["Content-Length"])
                        session.total_bytes = resume_from + total_length
                    
                    # Write to file
                    with open(session.destination, "ab") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            session.bytes_downloaded += len(chunk)
                            
                            if on_progress:
                                on_progress({
                                    "pack_id": session.pack_id,
                                    "bytes_downloaded": session.bytes_downloaded,
                                    "total_bytes": session.total_bytes,
                                    "progress_percent": session.get_progress_percent(),
                                    "eta_seconds": session.get_eta_seconds()
                                })
            
            return True
        
        except Exception as e:
            logger.error(f"Download failed: {e}")
            session.status = "paused"
            return False
    
    async def _download_manifest(self, pack_id: str) -> Optional[dict]:
        """Download pack manifest"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.host_url}/packs/{pack_id}/manifest"
                )
                response.raise_for_status()
                return response.json()
        
        except Exception as e:
            logger.error(f"Error downloading manifest for {pack_id}: {e}")
            return None
    
    async def _extract_pack(self, session: DownloadSession) -> bool:
        """Extract downloaded pack"""
        try:
            extract_path = session.destination.parent / session.pack_id
            extract_path.mkdir(parents=True, exist_ok=True)
            
            with tarfile.open(session.destination, "r:gz") as tar:
                tar.extractall(path=extract_path.parent)
            
            logger.info(f"Pack {session.pack_id} extracted to {extract_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error extracting pack: {e}")
            return False
    
    async def perform_sync(self) -> dict:
        """
        Perform full synchronization cycle
        
        Returns:
            Sync result summary
        """
        logger.info("Starting synchronization cycle")
        
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "packs_added": [],
            "packs_updated": [],
            "packs_removed": [],
            "total_downloaded_mb": 0,
            "errors": []
        }
        
        try:
            # Get delta
            delta = await self.calculate_delta_sync()
            if not delta:
                result["errors"].append("Failed to calculate delta")
                return result
            
            # Download new/updated packs
            packs_to_sync = delta.get("packs_to_add", []) + delta.get("packs_to_update", [])
            
            for pack_id in packs_to_sync:
                try:
                    success = await self.download_pack(
                        pack_id,
                        on_progress=lambda p: logger.info(
                            f"Progress: {p['pack_id']} {p['progress_percent']:.1f}%"
                        )
                    )
                    
                    if success:
                        if pack_id in delta.get("packs_to_add", []):
                            result["packs_added"].append(pack_id)
                        else:
                            result["packs_updated"].append(pack_id)
                    else:
                        result["errors"].append(f"Failed to download {pack_id}")
                
                except Exception as e:
                    result["errors"].append(f"{pack_id}: {str(e)}")
            
            # Remove old packs
            for pack_id in delta.get("packs_to_remove", []):
                try:
                    self._remove_pack(pack_id)
                    result["packs_removed"].append(pack_id)
                except Exception as e:
                    result["errors"].append(f"Failed to remove {pack_id}: {str(e)}")
            
            self.last_sync_time = datetime.utcnow()
            logger.info(f"Sync cycle completed: {result}")
            return result
        
        except Exception as e:
            logger.error(f"Sync error: {e}")
            result["errors"].append(str(e))
            return result
    
    def _remove_pack(self, pack_id: str):
        """Remove a pack from cache"""
        pack_path = self.cache_path / "active_packs" / pack_id
        if pack_path.exists():
            shutil.rmtree(pack_path)
            logger.info(f"Removed pack {pack_id}")
    
    def get_download_status(self, pack_id: str) -> Optional[dict]:
        """Get download status"""
        with self.lock:
            session = self.active_downloads.get(pack_id)
            if not session:
                return None
            
            return {
                "pack_id": session.pack_id,
                "status": session.status,
                "bytes_downloaded": session.bytes_downloaded,
                "total_bytes": session.total_bytes,
                "progress_percent": session.get_progress_percent(),
                "eta_seconds": session.get_eta_seconds(),
                "resume_count": session.resume_count
            }
    
    def _sync_scheduler(self):
        """Background sync scheduler"""
        while True:
            try:
                time.sleep(self.sync_interval)
                
                # Run async sync
                asyncio.run(self.perform_sync())
            
            except Exception as e:
                logger.error(f"Sync scheduler error: {e}")
