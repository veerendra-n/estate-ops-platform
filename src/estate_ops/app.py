"""Builds the platform from settings. Shared by the API, the worker and the CLI."""

from __future__ import annotations

import os
import subprocess

from .config import Settings
from .db import Database
from .estate import EstateGraph
from .policy import Policy
from .service import Platform
from .skills import Skills
from .tenancy import Tenants


def anthropic_client():
    """A Claude client when credentials exist, else None (agents then run deterministically)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic

    return anthropic.Anthropic()


def library_version(settings: Settings) -> str | None:
    """The git commit the skills library was loaded from, recorded on every proposal."""
    if os.environ.get("SKILLS_VERSION"):
        return os.environ["SKILLS_VERSION"]
    try:
        out = subprocess.run(  # noqa: S603 - fixed command
            ["git", "-C", str(settings.skills_dir), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build(settings: Settings | None = None, **overrides) -> Platform:
    settings = settings or Settings.from_env()
    parts = {
        "settings": settings,
        "db": Database(settings.database_url),
        "graph": EstateGraph(settings.neo4j_url, settings.neo4j_user, settings.neo4j_password),
        "tenants": Tenants.load(settings.tenants_file),
        "skills": Skills.load(settings.skills_dir, library_version(settings)),
        "policy": Policy(settings.opa_url),
        "anthropic": anthropic_client(),
    }
    parts.update(overrides)
    return Platform(**parts)
