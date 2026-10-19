# Integrations

Every connector is configured per customer in `config/tenants.yml` with a URL and the names of the
environment variables that hold its credentials. In the codespace every URL points at the estate simulator.

The simulator copies each vendor's API shape as far as we know it. Rows marked **UNCONFIRMED** use an
endpoint or field we have not yet checked against a real system; check them in a customer's lab before the
first live run.

## Customer systems

| System | Used for | Calls | Status |
| --- | --- | --- | --- |
| vCenter (vSphere Automation REST) | Discovery; rebalance skill | `POST /api/session`, `GET /api/vcenter/{cluster,host,datastore,vm}`, `GET /api/vcenter/vm/{id}` | Confirmed shape |
| | Storage vMotion | `POST /api/vcenter/vm/{id}?action=relocate` with `placement.datastore` | **UNCONFIRMED** (needs vSphere 8; otherwise use pyVmomi `RelocateVM_Task`) |
| | Grow a disk | `PATCH /api/vcenter/vm/{id}/hardware/disk/{disk}` with `capacity` | **UNCONFIRMED** (likely pyVmomi `ReconfigVM_Task`) |
| | Per-VM disk IOPS, datastore latency | Metrics from the OTel Collector `vcenter` receiver | Metric names **UNCONFIRMED** |
| Veeam Backup & Replication REST v1 | Discovery; backup skill; session poller | `POST /api/oauth2/token`, `GET /api/v1/jobs`, `/sessions`, `/backups`, `/backupInfrastructure/repositories/states`, `POST /api/v1/jobs/{id}/start` | Confirmed shape |
| | Remove orphaned restore points | `POST /api/v1/backups/{id}/removeFromDisk` | **UNCONFIRMED** |
| Microsoft Entra ID | App-only token per customer | `POST {login}/{tenant}/oauth2/v2.0/token` (client credentials) | Confirmed shape |
| Microsoft Graph: Intune | Devices and owners; sync; remediation | `/deviceManagement/managedDevices`, `.../syncDevice`, `beta .../initiateOnDemandProactiveRemediation` | Remediation is beta |
| Microsoft Graph: users | Sign-in revoke | `/users/{upn}`, `POST /users/{upn}/revokeSignInSessions` | Confirmed shape |
| Microsoft Graph: service health | M365 health poller | `/admin/serviceAnnouncement/healthOverviews`, `/issues` | Confirmed shape |
| Fleet (osquery) | Non-Intune laptops; live queries | `GET /api/v1/fleet/hosts`, `POST /api/v1/fleet/hosts/{id}/query` | Named queries only |
| Edge node | Network devices; typed operations on servers | `POST /v1/ops/{name}` with a fixed schema per op (`service_status`, `restart_service`, `cleanup_disk`, ...) | **Ours, not built yet**; simulated |

## Platform components

| Component | Interface |
| --- | --- |
| VictoriaMetrics | Prometheus query API; every query adds `extra_label=tenant_id=<tenant>` |
| VictoriaLogs | LogsQL over HTTP (`/select/logsql/query`), filtered by the `tenant_id` stream field |
| OpenTelemetry Collector | Prometheus receiver in the codespace (scrapes the simulator); at a customer the edge node runs it with the vcenter, windowsperfcounters, hostmetrics and snmp receivers |
| vmalert | Rules in `alerts/estate.rules.yml`, notifier is the API's Alertmanager v2 intake with `ALERTS_INTAKE_TOKEN` |
| OPA | `POST /v1/data/estate/autonomy/decision` |
| Temporal | Workflow `CaseWorkflow` on task queue `estate-cases` |
| NATS JetStream | Streams `<PREFIX>_INTAKE` and `<PREFIX>_EVENTS`; integrations are durable consumers |
| Microsoft Teams | A Workflows webhook per customer (`teams_webhook_env`); Adaptive Cards for approvals, escalations and resolutions |
| Claude API | Investigator (`claude-opus-5-5`, low effort, structured output) and copilot (`claude-haiku-5-5`); key in `ANTHROPIC_API_KEY` |

## Adding a customer

1. Add the tenant to `config/tenants.yml` with its connectors and the names of its secret variables.
2. Put the secrets in the environment (a vault in production).
3. Describe its business services in `config/estate/<tenant>.yml`.
4. Point its edge node's collector at VictoriaMetrics and VictoriaLogs with `tenant_id` set.
5. Run `python -m estate_ops discover --tenant <id>` and check the estate page.
