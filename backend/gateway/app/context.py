"""Registry for the gateway FastAPI instance.

Service modules need access to the shared ``httpx.AsyncClient`` resting on
``app.state`` without importing ``app.main`` (which would be circular). The
composition root binds the application here at import time; helpers resolve it
lazily so tests can still swap ``app.state.http`` for a mock between requests.
"""

from __future__ import annotations

from typing import Any

import httpx

_APPLICATION: Any = None


def bind_application(application: Any) -> None:
    global _APPLICATION
    _APPLICATION = application


def get_application() -> Any:
    if _APPLICATION is None:
        raise RuntimeError(
            "Gateway application is not bound. Import app.main before calling this code."
        )
    return _APPLICATION


def http_client() -> httpx.AsyncClient:
    return get_application().state.http
