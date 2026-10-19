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
    skills_dir: Path
    # Business services, owners and criticality each customer declares (config/estate/<tenant>.yml).
    declared_dir: Path
    # Telemetry: VictoriaMetrics (Prometheus API) and VictoriaLogs, fed by the OpenTelemetry Collector.
    metrics_url: str = "http://localhost:8428"
    logs_url: str = "http://localhost:9428"
    # Open Policy Agent with policy/autonomy.rego loaded.
    opa_url: str = "http://localhost:8181"
    # Bearer token vmalert presents to the Alertmanager intake (it serves every customer: rules set tenant_id).
    alerts_token_env: str = "ALERTS_INTAKE_TOKEN"  # noqa: S105 - the variable's name, not a secret
    # The estate simulator, when one is attached (demo and test environments): the cockpit's Lab page.
    sim_url: str | None = None
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
    # The case copilot answers engineers' questions; a small model keeps a question to a fraction of a cent.
    copilot_model: str = "claude-haiku-5-5"
    # USD per million tokens: input, output, cache read, cache write. Update when prices change.
    prices: dict[str, tuple[float, float, float, float]] = field(
        default_factory=lambda: {
            "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
            "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
            "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
        }
    )
    # Alerts that arrive within this many seconds of a case opening are folded in before investigation starts.
    settle_seconds: float = 45.0
    # Alerts on assets this close in the estate graph, within this window, belong to the same case.
    correlate_hops: int = 2
    correlate_minutes: float = 30.0
    # An approval nobody answers escalates the case after this long.
    approval_timeout_hours: float = 72.0
    # Where engineers reach the console; used for links in tickets and Teams cards, and the SSO callback.
    public_url: str = "http://localhost:8080"
    discovery_interval_minutes: float = 15.0
    # Alert sources the worker polls (Veeam sessions, Microsoft 365 service health).
    poll_interval_minutes: float = 1.0
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
        return cls(
            database_url=_env("DATABASE_URL", "postgresql://postgres@localhost:5432/estate_ops"),
            tenants_file=Path(_env("TENANTS_FILE", str(root / "config/tenants.yml"))),
            skills_dir=Path(_env("SKILLS_DIR", str(root / "skills"))),
            declared_dir=Path(_env("DECLARED_DIR", str(root / "config/estate"))),
            metrics_url=_env("METRICS_URL", "http://localhost:8428"),
            logs_url=_env("LOGS_URL", "http://localhost:9428"),
            opa_url=_env("OPA_URL", "http://localhost:8181"),
            sim_url=_env("SIM_URL"),
            diagnosis_model=_env("DIAGNOSIS_MODEL", "claude-opus-5-5"),
            diagnosis_effort=_env("DIAGNOSIS_EFFORT", "low"),
            copilot_model=_env("COPILOT_MODEL", "claude-haiku-5-5"),
            neo4j_url=_env("NEO4J_URL", "bolt://localhost:7687"),
            neo4j_user=_env("NEO4J_USER", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD"),
            nats_url=_env("NATS_URL", "nats://localhost:4222"),
            nats_prefix=_env("NATS_PREFIX", "estate"),
            temporal_address=_env("TEMPORAL_ADDRESS", "localhost:7233"),
            temporal_namespace=_env("TEMPORAL_NAMESPACE", "default"),
            task_queue=_env("TEMPORAL_TASK_QUEUE", "estate-cases"),
            settle_seconds=float(_env("SETTLE_SECONDS", "45")),
            correlate_minutes=float(_env("CORRELATE_MINUTES", "30")),
            approval_timeout_hours=float(_env("APPROVAL_TIMEOUT_HOURS", "72")),
            public_url=_env("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            discovery_interval_minutes=float(_env("DISCOVERY_INTERVAL_MINUTES", "15")),
            poll_interval_minutes=float(_env("POLL_INTERVAL_MINUTES", "1")),
            oidc_issuer=_env("OIDC_ISSUER"),
            oidc_client_id=_env("OIDC_CLIENT_ID"),
            oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
            oidc_allowed=tuple(d.strip().lower() for d in (_env("OIDC_ALLOWED") or "").split(",") if d.strip()),
            session_secret=_env("SESSION_SECRET"),
        )
