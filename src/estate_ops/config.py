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
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None
    nats_url: str = "nats://localhost:4222"
    # Prefix for NATS subjects and stream names, so several platform instances (or test runs) can share a server.
    nats_prefix: str = "estate"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "estate-cases"
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
    # An approval nobody answers escalates the case after this long.
    approval_timeout_hours: float = 72.0
    # Where engineers reach the console; used for links in tickets and Teams cards, and the SSO callback.
    public_url: str = "http://localhost:8080"
    # HaloPSA: one instance for the managed service; each tenant names its Halo client id.
    halo_url: str | None = None
    halo_client_id: str | None = None
    halo_client_secret: str | None = None
    halo_tenant: str | None = None
    halo_ticket_type_id: int = 1
    halo_closed_status_id: int = 9
    # P1..P4 to Halo priority ids.
    halo_priorities: dict[str, int] = field(default_factory=lambda: {"P1": 1, "P2": 2, "P3": 3, "P4": 4})
    # NetBox: the inventory source of truth that discovery reads into the estate graph.
    netbox_url: str | None = None
    netbox_token: str | None = None
    discovery_interval_minutes: float = 60.0
    # Console sign-in with OpenID Connect (Entra ID, Google Workspace, Keycloak...). Without an issuer the
    # console falls back to the shared CONSOLE_TOKEN, which is for local use only.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_allowed: tuple[str, ...] = ()
    session_secret: str | None = None

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
            neo4j_url=_env("NEO4J_URL", "bolt://localhost:7687"),
            neo4j_user=_env("NEO4J_USER", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD"),
            nats_url=_env("NATS_URL", "nats://localhost:4222"),
            nats_prefix=_env("NATS_PREFIX", "estate"),
            temporal_address=_env("TEMPORAL_ADDRESS", "localhost:7233"),
            temporal_namespace=_env("TEMPORAL_NAMESPACE", "default"),
            task_queue=_env("TEMPORAL_TASK_QUEUE", "estate-cases"),
            run_poll_seconds=float(_env("RUN_POLL_SECONDS", "3")),
            approval_timeout_hours=float(_env("APPROVAL_TIMEOUT_HOURS", "72")),
            public_url=_env("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            halo_url=_env("HALO_URL"),
            halo_client_id=_env("HALO_CLIENT_ID"),
            halo_client_secret=_env("HALO_CLIENT_SECRET"),
            halo_tenant=_env("HALO_TENANT"),
            halo_ticket_type_id=int(_env("HALO_TICKET_TYPE_ID", "1")),
            halo_closed_status_id=int(_env("HALO_CLOSED_STATUS_ID", "9")),
            netbox_url=_env("NETBOX_URL"),
            netbox_token=_env("NETBOX_TOKEN"),
            discovery_interval_minutes=float(_env("DISCOVERY_INTERVAL_MINUTES", "60")),
            oidc_issuer=_env("OIDC_ISSUER"),
            oidc_client_id=_env("OIDC_CLIENT_ID"),
            oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
            oidc_allowed=tuple(d.strip().lower() for d in (_env("OIDC_ALLOWED") or "").split(",") if d.strip()),
            session_secret=_env("SESSION_SECRET"),
        )
