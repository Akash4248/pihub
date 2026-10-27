"""PhET simulation catalog + static simulations configuration.

The catalog and simulation bundle live on the mounted ``SIMULATIONS_DIR``;
previously this data was served by the retired experiment-service.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

SIMULATIONS_DIR = Path(os.getenv("SIMULATIONS_DIR", "/shared/simulations"))


def experiments_catalog_payload() -> dict[str, Any]:
    catalog_path = SIMULATIONS_DIR / "catalog.json"
    if not catalog_path.exists():
        return {"experiments": [], "simulations": [], "total": 0}

    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[EXPERIMENT_CATALOG] failed to read %s: %s", catalog_path, exc)
        return {"experiments": [], "simulations": [], "total": 0}

    simulations: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        simulation = dict(entry)
        simulation["local_url"] = f"/simulations/{slug}/index.html"
        simulation["manifest"] = {
            "id": f"phet-{slug}",
            "title": slug.replace("-", " ").title(),
            "provider": "phet",
            "type": "simulation",
        }
        simulations.append(simulation)

    return {"experiments": [], "simulations": simulations, "total": len(simulations)}
