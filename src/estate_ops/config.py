"""Settings from the environment. Nothing secret has a default; see .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    tenants_file: Path
    catalog_file: Path
    alerts_dir: Path | None
    seed_file: Path | None
    semaphore_url: str
    semaphore_token: str | None
    openobserve_url: str
    openobserve_user: str | None
    openobserve_password: str | None
    # Per-agent models. Opus at low effort for diagnosis; change per tenant workload once measured.
    diagnosis_model: str = "claude-opus-5-5"
    diagnosis_effort: str = "low"
    diagnosis_max_turns: int = 4
    # USD per million tokens: input, output, cache read, cache write. Update when prices change.
    prices: dict[str, tuple[float, float, float, float]] = field(
        default_factory=lambda: {
            "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
            "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
            "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
        }
    )
    run_poll_seconds: float = 3.0
    run_timeout_seconds: float = 1800.0

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(__file__).resolve().parents[2]
        alerts = _env("ALERTS_DIR")
        seed = _env("SEED_FILE", str(root / "config/seed/lab.yml"))
        return cls(
            database_url=_env("DATABASE_URL", "postgresql://postgres@localhost:5432/estate_ops"),
            tenants_file=Path(_env("TENANTS_FILE", str(root / "config/tenants.yml"))),
            catalog_file=Path(_env("CATALOG_FILE", str(root / "tests/fixtures/catalog.json"))),
            alerts_dir=Path(alerts) if alerts else None,
            seed_file=Path(seed) if seed else None,
            semaphore_url=_env("SEMAPHORE_URL", "http://localhost:8090/semaphore"),
            semaphore_token=_env("SEMAPHORE_TOKEN"),
            openobserve_url=_env("OPENOBSERVE_URL", "http://localhost:8090/openobserve"),
            openobserve_user=_env("OPENOBSERVE_USER"),
            openobserve_password=_env("OPENOBSERVE_PASSWORD"),
            diagnosis_model=_env("DIAGNOSIS_MODEL", "claude-opus-5-5"),
            diagnosis_effort=_env("DIAGNOSIS_EFFORT", "low"),
            run_poll_seconds=float(_env("RUN_POLL_SECONDS", "3")),
        )
