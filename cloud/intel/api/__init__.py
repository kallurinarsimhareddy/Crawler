"""The platform's REST API, mounted into CareerCloud's FastAPI app under ``/api/v1``.

Each track owns one module in :data:`ROUTER_MODULES` exporting ``router``.
Workspace resources live under ``/api/v1/w/{workspace_id}/...``.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

__all__ = ["ROUTER_MODULES", "mount"]

log = logging.getLogger(__name__)

ROUTER_MODULES = (
    "cloud.intel.api.routes_workspaces",
    "cloud.intel.api.routes_crm",
    "cloud.intel.api.routes_imports",
    "cloud.intel.api.routes_intel",
    "cloud.intel.api.routes_sources",
    "cloud.intel.api.routes_ai",
    "cloud.intel.api.routes_scraper",
    "cloud.intel.api.routes_gtm",
    "cloud.intel.api.routes_analytics",
    "cloud.intel.api.routes_agent",
)


def mount(app: Any, platform: Any) -> None:
    app.state.platform = platform
    for name in ROUTER_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            log.warning("platform routes %s are not built yet; skipping", name)
            continue
        app.include_router(module.router, prefix="/api/v1")
