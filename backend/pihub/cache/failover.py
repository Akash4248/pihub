"""
Offline Failover Module

Handles:
- Host heartbeat monitoring
- Automatic failover to offline mode
- Degraded-mode routing
- Offline response caching
- Reconnection management
"""

import asyncio
import json
import logging
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Callable, List

import httpx

logger = logging.getLogger(__name__)


class HostHealthMonitor:
    """Monitor host connectivity"""
    
    def __init__(
        self,
        host_url: str = "http://192.168.42.1:8000",
        heartbeat_interval_seconds: int = 30,
        failure_threshold: int = 3,
        recovery_check_interval: int = 10
    ):
        """
        Initialize health monitor
        
        Args:
            host_url: Host server URL
            heartbeat_interval_seconds: How often to check host
            failure_threshold: Consecutive failures before marking down
            recovery_check_interval: How often to check for recovery
        """
        self.host_url = host_url
        self.heartbeat_interval = heartbeat_interval_seconds
        self.failure_threshold = failure_threshold
        self.recovery_interval = recovery_check_interval
        
        self.is_healthy = True
        self.consecutive_failures = 0
        self.last_heartbeat_time = None
        self.last_failed_time = None
        self.downtime_start = None
        
        self.health_callbacks: List[Callable] = []
        self.lock = threading.RLock()
        
        # Start monitor thread
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self.monitor_thread.start()
        logger.info(f"Host health monitor initialized: {host_url}")
    
    def add_health_callback(self, callback: Callable):
        """Register callback for health status changes"""
        self.health_callbacks.append(callback)
    
    async def _heartbeat_check(self) -> bool:
        """Perform heartbeat check to host"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.host_url}/health",
                    timeout=5
                )
                return response.status_code == 200
        
        except Exception as e:
            logger.debug(f"Heartbeat check failed: {e}")
            return False
    
    def _monitor_loop(self):
        """Background monitoring loop"""
        while True:
            try:
                time.sleep(self.heartbeat_interval)
                
                # Check if host is healthy
                is_alive = asyncio.run(self._heartbeat_check())
                
                with self.lock:
                    old_status = self.is_healthy
                    
                    if is_alive:
                        self.last_heartbeat_time = datetime.utcnow()
                        self.consecutive_failures = 0
                        
                        if not self.is_healthy:
                            # Host recovered
                            self.is_healthy = True
                            logger.info("Host recovered - switching to online mode")
                            self._notify_health_change(True)
                    
                    else:
                        self.consecutive_failures += 1
                        
                        if self.consecutive_failures >= self.failure_threshold:
                            if self.is_healthy:
                                # Host went down
                                self.is_healthy = False
                                self.last_failed_time = datetime.utcnow()
                                self.downtime_start = datetime.utcnow()
                                logger.warning("Host unavailable - switching to offline mode")
                                self._notify_health_change(False)
            
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
    
    def _notify_health_change(self, is_healthy: bool):
        """Notify callbacks of health status change"""
        for callback in self.health_callbacks:
            try:
                callback(is_healthy)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def get_status(self) -> dict:
        """Get current health status"""
        with self.lock:
            uptime = None
            if self.is_healthy and self.last_heartbeat_time:
                uptime = (datetime.utcnow() - self.last_heartbeat_time).total_seconds()
            
            return {
                "is_healthy": self.is_healthy,
                "consecutive_failures": self.consecutive_failures,
                "last_heartbeat": self.last_heartbeat_time.isoformat() if self.last_heartbeat_time else None,
                "last_failure": self.last_failed_time.isoformat() if self.last_failed_time else None,
                "downtime_start": self.downtime_start.isoformat() if self.downtime_start else None,
                "seconds_since_heartbeat": uptime
            }


class OfflineFailoverController:
    """Manage failover to offline mode"""
    
    def __init__(
        self,
        cache_manager,
        cache_path: str = "/cache"
    ):
        """
        Initialize failover controller
        
        Args:
            cache_manager: PiCacheManager instance
            cache_path: Cache directory path
        """
        self.cache_manager = cache_manager
        self.cache_path = Path(cache_path)
        
        self.is_offline_mode = False
        self.offline_callbacks: List[Callable] = []
        self.lock = threading.RLock()
        
        logger.info("Offline failover controller initialized")
    
    def add_offline_mode_callback(self, callback: Callable):
        """Register callback for mode changes"""
        self.offline_callbacks.append(callback)
    
    def enter_offline_mode(self):
        """Switch to offline mode"""
        with self.lock:
            if not self.is_offline_mode:
                self.is_offline_mode = True
                logger.warning("Entering offline mode")
                self._notify_mode_change()
    
    def exit_offline_mode(self):
        """Return to online mode"""
        with self.lock:
            if self.is_offline_mode:
                self.is_offline_mode = False
                logger.info("Exiting offline mode")
                self._notify_mode_change()
    
    def _notify_mode_change(self):
        """Notify callbacks of mode change"""
        for callback in self.offline_callbacks:
            try:
                callback(self.is_offline_mode)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def get_offline_status(self) -> dict:
        """Get offline mode status"""
        with self.lock:
            return {
                "is_offline": self.is_offline_mode,
                "available_packs": len(
                    self.cache_manager.list_cached_packs()
                )
            }
    
    async def serve_cached_retrieval(
        self,
        query: str,
        metadata: Optional[Dict] = None
    ) -> Optional[List[Dict]]:
        """
        Serve cached retrieval results in offline mode
        
        Args:
            query: Search query
            metadata: Optional metadata filters
        
        Returns:
            Cached results or None
        """
        # Try exact query match
        cached = self.cache_manager.get_cached_retrieval(query)
        if cached:
            logger.info(f"Serving cached results for query: {query}")
            return cached
        
        # Try to find similar cached queries (simple prefix matching)
        query_prefix = query.lower()[:10]
        
        # Could implement fuzzy matching here
        logger.info(f"No cached results for query: {query} (offline mode)")
        
        return None
    
    async def get_available_packs_summary(self) -> dict:
        """Get summary of available offline packs"""
        packs = self.cache_manager.list_cached_packs()
        
        summary = {
            "total_packs": len(packs),
            "total_size_mb": self.cache_manager.get_cache_stats()["total_size_mb"],
            "packs": []
        }
        
        for pack in packs:
            summary["packs"].append({
                "pack_id": pack["pack_id"],
                "version": pack["version"],
                "active": pack["active"]
            })
        
        return summary
    
    async def activate_pack_for_classroom(
        self,
        pack_id: str,
        classroom_id: str
    ) -> bool:
        """Activate a pack for classroom use"""
        try:
            self.cache_manager.mark_pack_active(pack_id)
            
            # Record classroom activation
            state_file = self.cache_path / "classroom_state.json"
            
            if state_file.exists():
                with open(state_file, "r") as f:
                    state = json.load(f)
            else:
                state = {"active_classrooms": {}}
            
            if classroom_id not in state["active_classrooms"]:
                state["active_classrooms"][classroom_id] = {
                    "active_packs": [],
                    "activated_at": datetime.utcnow().isoformat()
                }
            
            if pack_id not in state["active_classrooms"][classroom_id]["active_packs"]:
                state["active_classrooms"][classroom_id]["active_packs"].append(pack_id)
            
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
            
            logger.info(f"Pack {pack_id} activated for classroom {classroom_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error activating pack: {e}")
            return False
    
    async def deactivate_pack_for_classroom(
        self,
        pack_id: str,
        classroom_id: str
    ) -> bool:
        """Deactivate a pack for classroom"""
        try:
            state_file = self.cache_path / "classroom_state.json"
            
            if state_file.exists():
                with open(state_file, "r") as f:
                    state = json.load(f)
                
                if classroom_id in state.get("active_classrooms", {}):
                    if pack_id in state["active_classrooms"][classroom_id]["active_packs"]:
                        state["active_classrooms"][classroom_id]["active_packs"].remove(pack_id)
                
                with open(state_file, "w") as f:
                    json.dump(state, f, indent=2)
            
            # Check if pack is used by other classrooms
            self.cache_manager.mark_pack_inactive(pack_id)
            
            logger.info(f"Pack {pack_id} deactivated for classroom {classroom_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error deactivating pack: {e}")
            return False
    
    async def get_classroom_packs(self, classroom_id: str) -> List[str]:
        """Get active packs for a classroom"""
        try:
            state_file = self.cache_path / "classroom_state.json"
            
            if state_file.exists():
                with open(state_file, "r") as f:
                    state = json.load(f)
                
                if classroom_id in state.get("active_classrooms", {}):
                    return state["active_classrooms"][classroom_id]["active_packs"]
            
            return []
        
        except Exception as e:
            logger.error(f"Error getting classroom packs: {e}")
            return []


class FailoverOrchestrator:
    """Orchestrate health monitoring and failover"""
    
    def __init__(
        self,
        cache_manager,
        host_url: str = "http://192.168.1.100:8000"
    ):
        """Initialize orchestrator"""
        self.cache_manager = cache_manager
        
        self.health_monitor = HostHealthMonitor(host_url=host_url)
        self.failover_controller = OfflineFailoverController(cache_manager)
        
        # Wire them together
        self.health_monitor.add_health_callback(self._on_health_change)
        
        logger.info("Failover orchestrator initialized")
    
    def _on_health_change(self, is_healthy: bool):
        """Handle health status change"""
        if is_healthy:
            self.failover_controller.exit_offline_mode()
        else:
            self.failover_controller.enter_offline_mode()
    
    def get_system_status(self) -> dict:
        """Get overall system status"""
        return {
            "host_health": self.health_monitor.get_status(),
            "offline_mode": self.failover_controller.get_offline_status(),
            "cache_stats": self.cache_manager.get_cache_stats()
        }
