"""Builds the platform from settings. Shared by the API, the worker and the CLI."""

from __future__ import annotations

import os

from .catalog import Catalog
from .config import Settings
from .db import Database
from .semaphore import Semaphore
from .service import Platform
from .telemetry import OpenObserve
from .tenancy import Tenants


def anthropic_client():
    """A Claude client when credentials exist, else None (diagnosis then runs deterministically)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic

    return anthropic.Anthropic()


def build(settings: Settings | None = None, **overrides) -> Platform:
    settings = settings or Settings.from_env()
    parts = {
        "settings": settings,
        "db": Database(settings.database_url),
        "tenants": Tenants.load(settings.tenants_file),
        "catalog": Catalog.load(settings.catalog_file, settings.alerts_dir),
        "semaphore": Semaphore(settings.semaphore_url, settings.semaphore_token),
        "telemetry": OpenObserve(settings.openobserve_url, settings.openobserve_user, settings.openobserve_password),
        "anthropic": anthropic_client(),
    }
    parts.update(overrides)
    return Platform(**parts)
