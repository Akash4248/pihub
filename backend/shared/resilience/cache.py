from __future__ import annotations

import collections
import time
from typing import Any

from shared.config import get_settings


class ResponseCache:
    """Thread-safe in-memory LRU response cache with per-item TTL."""

    def __init__(self, max_items: int = 1000, default_ttl_seconds: int | None = None) -> None:
        self.max_items = max_items
        settings = get_settings()
        self.default_ttl = default_ttl_seconds or settings.response_cache_ttl_seconds
        self._cache: collections.OrderedDict[str, tuple[Any, float]] = collections.OrderedDict()

    def get(self, key: str) -> Any | None:
        if key not in self._cache:
            return None
        value, expiry = self._cache[key]
        if time.time() > expiry:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        expiry = time.time() + ttl
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (value, expiry)

        if len(self._cache) > self.max_items:
            self._cache.popitem(last=False)

    def invalidate_prefix(self, prefix: str) -> int:
        keys_to_del = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_del:
            del self._cache[k]
        return len(keys_to_del)

    def clear(self) -> None:
        self._cache.clear()


_response_cache: ResponseCache | None = None


def get_response_cache() -> ResponseCache:
    global _response_cache
    if _response_cache is None:
        _response_cache = ResponseCache()
    return _response_cache
