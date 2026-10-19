// Dev-only fake backend. Enabled with VITE_MOCK=1; main.tsx imports it dynamically behind
// `import.meta.env.VITE_MOCK`, so production builds never include it.
//
// Extra knobs for screenshots: add `?mock=signin` to the URL to start signed out.
import type { CostView, Fault, Integration, JsonObject, LabEvent, LedgerEntry, Overview, Skill, Tenant } from "../types";
import { buildCases, chainLedger, fakeHash, type MockCase } from "./cases";
import { contosoEstate, fabrikamEstate, type MockEstate } from "./estate";

const NOW = Date.now();
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();
const inMin = (min: number) => new Date(NOW + min * 60_000).toISOString();
const live = () => new Date().toISOString();

const TENANTS: Tenant[] = [
  { id: "contoso", name: "Contoso Retail", industry: "Retail · 42 stores", diagnosis: "llm", autonomy: { default: "approve_to_run", "backup/free-space-and-rerun": "run_and_notify" }, ai_spend_month_usd: 0, ai_budget_usd_month: 150, ledger_ok: true, ledger_broken_at: null },
  { id: "fabrikam", name: "Fabrikam Health", industry: "Healthcare · 3 hospitals", diagnosis: "llm", autonomy: { default: "run_and_notify", "vmware/rebalance-datastore": "approve_to_run" }, ai_spend_month_usd: 0, ai_budget_usd_month: 100, ledger_ok: true, ledger_broken_at: null },
];

const cases: MockCase[] = buildCases(ago, inMin);
chainLedger(cases);
const estates: Record<string, MockEstate> = { contoso: contosoEstate(ago), fabrikam: fabrikamEstate(ago) };

/* ---------------- cost ---------------- */
const daily: CostView["daily"] = [];
for (let d = 89; d >= 0; d--) {
  const day = new Date(NOW - d * 86400_000).toISOString().slice(0, 10);
  const wobble = (n: number) => Math.abs(Math.sin(d * 1.7 + n)) ;
  daily.push({ tenant_id: "contoso", day, cost_usd: +(0.4 + wobble(1) * 2.6 + (d % 7 === 2 ? 3.1 : 0)).toFixed(3) });
  daily.push({ tenant_id: "fabrikam", day, cost_usd: +(0.2 + wobble(4) * 1.4).toFixed(3) });
}
const monthStart = new Date(new Date(NOW).getFullYear(), new Date(NOW).getMonth(), 1).getTime();
const monthSpend = (t: string) => +daily.filter((r) => r.tenant_id === t && Date.parse(r.day) >= monthStart).reduce((a, r) => a + r.cost_usd, 0).toFixed(2);
for (const t of TENANTS) t.ai_spend_month_usd = monthSpend(t.id);

const byAgent: CostView["by_agent"] = [
  { tenant_id: "contoso", agent: "investigator", model: "claude-opus-5-5", calls: 61, input_tokens: 412_000, output_tokens: 58_300, cache_read_tokens: 2_910_000, cost_usd: 38.42 },
  { tenant_id: "contoso", agent: "remediator", model: "claude-opus-5-5", calls: 22, input_tokens: 96_100, output_tokens: 14_900, cache_read_tokens: 701_000, cost_usd: 9.87 },
  { tenant_id: "contoso", agent: "copilot", model: "claude-haiku-5-5", calls: 148, input_tokens: 610_000, output_tokens: 74_200, cache_read_tokens: 3_400_000, cost_usd: 4.12 },
  { tenant_id: "contoso", agent: "verifier", model: null, calls: 40, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cost_usd: 0 },
  { tenant_id: "fabrikam", agent: "investigator", model: "claude-opus-5-5", calls: 23, input_tokens: 171_000, output_tokens: 22_800, cache_read_tokens: 1_120_000, cost_usd: 15.06 },
  { tenant_id: "fabrikam", agent: "copilot", model: "claude-haiku-5-5", calls: 57, input_tokens: 228_000, output_tokens: 26_100, cache_read_tokens: 1_310_000, cost_usd: 1.58 },
  { tenant_id: "fabrikam", agent: "verifier", model: null, calls: 19, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cost_usd: 0 },
];

/* ---------------- skills ---------------- */
type SkillDef = Omit<Skill, "tenants"> & { stats: Record<string, [number, number, number, number, number]> };
const SKILLS: SkillDef[] = [
  {
    id: "vmware/rebalance-datastore", version: "1.4.2", title: "Rebalance a hot datastore",
    summary: "Moves the noisiest non-critical VM off an overloaded datastore with storage vMotion, to the datastore with the most latency and space headroom.",
    domain: "virtualization", risk: "medium", approval: "required", target_kind: "server", connectors: ["vcenter"], max_targets: 1,
    params: [
      { name: "vm", type: "string", default: null, min: null, max: null, choices: null, required: true, description: "VM to move" },
      { name: "min_free_pct", type: "int", default: 20, min: 10, max: 50, choices: null, required: false, description: "Free space the destination must keep" },
      { name: "max_latency_ms", type: "float", default: 10, min: 1, max: 30, choices: null, required: false, description: "Destination latency ceiling" },
    ],
    triggers: ["DatastoreLatencyHigh", "DatastoreIOPSHigh"], rollback: "Storage vMotion the VM back to its original datastore.",
    stats: { contoso: [6, 5, 0, 0, 1], fabrikam: [1, 1, 0, 0, 0] },
  },
  {
    id: "backup/free-space-and-rerun", version: "2.1.0", title: "Free repository space and rerun backup",
    summary: "Removes restore points of retired systems past retention until the repository is above its floor, then reruns the failed job.",
    domain: "backup", risk: "low", approval: "policy", target_kind: "backup_job", connectors: ["veeam"], max_targets: 3,
    params: [
      { name: "target_free_pct", type: "int", default: 8, min: 5, max: 30, choices: null, required: false, description: "Stop deleting once this much is free" },
      { name: "only_retired", type: "bool", default: true, min: null, max: null, choices: null, required: false, description: "Never touch active systems" },
    ],
    triggers: ["veeam-job-failed", "BackupRepositoryLow"], rollback: "Deleted restore points stay in the Veeam recycle bin for 7 days.",
    stats: { contoso: [3, 3, 0, 0, 0], fabrikam: [9, 8, 1, 0, 0] },
  },
  {
    id: "sql/rerun-agent-job", version: "1.0.3", title: "Rerun a failed SQL agent job",
    summary: "Reruns a SQL Server agent job that failed for a transient reason (deadlock, timeout) and checks the new outcome.",
    domain: "database", risk: "low", approval: "policy", target_kind: "sql_agent_job", connectors: ["edge"], max_targets: 1,
    params: [{ name: "max_retries", type: "int", default: 1, min: 1, max: 3, choices: null, required: false, description: "Retries before escalating" }],
    triggers: ["SqlAgentJobFailed"], rollback: null,
    stats: { contoso: [11, 9, 1, 1, 0], fabrikam: [4, 4, 0, 0, 0] },
  },
  {
    id: "linux/restart-service", version: "1.2.0", title: "Restart a systemd service",
    summary: "Restarts one unit on one host after checking it is the failing component and that a peer is serving traffic.",
    domain: "linux", risk: "medium", approval: "required", target_kind: "server", connectors: ["edge"], max_targets: 1,
    params: [{ name: "unit", type: "string", default: null, min: null, max: null, choices: null, required: true, description: "systemd unit name" }],
    triggers: ["ServiceErrorsHigh", "ProcessDown"], rollback: "None: a restart is idempotent.",
    stats: { contoso: [2, 1, 0, 0, 1], fabrikam: [3, 3, 0, 0, 0] },
  },
  {
    id: "m365/restart-intune-sync", version: "0.9.1", title: "Restart Intune device sync",
    summary: "Re-authorises the Intune connector and triggers a device sync for devices that stopped reporting compliance.",
    domain: "m365", risk: "low", approval: "policy", target_kind: "m365_service", connectors: ["microsoft"], max_targets: 1,
    params: [{ name: "scope", type: "choice", default: "stale", min: null, max: null, choices: ["stale", "all"], required: false, description: "Which devices to sync" }],
    triggers: ["IntuneSyncStalled"], rollback: null,
    stats: { contoso: [0, 0, 0, 0, 0], fabrikam: [2, 1, 0, 0, 0] },
  },
  {
    id: "windows/clear-temp-space", version: "1.1.0", title: "Clear temporary files on a Windows server",
    summary: "Clears Windows Update cache, IIS logs older than 14 days and user temp folders until the volume has headroom.",
    domain: "windows", risk: "low", approval: "policy", target_kind: "server", connectors: ["edge"], max_targets: 5,
    params: [{ name: "min_free_pct", type: "int", default: 15, min: 5, max: 40, choices: null, required: false, description: "Target free space" }],
    triggers: ["DiskSpaceLow"], rollback: null,
    stats: { contoso: [14, 13, 1, 0, 0], fabrikam: [6, 6, 0, 0, 0] },
  },
];

function modeFor(t: Tenant, skill: SkillDef): [string, string] {
  const a = t.autonomy as Record<string, string>;
  if (a[skill.id]) return [a[skill.id], `${t.name} sets ${skill.id} to ${a[skill.id]} explicitly.`];
  if (skill.approval === "required") return ["approve_to_run", "The skill requires approval; policy never lets it run unattended."];
  if (skill.risk !== "low") return ["approve_to_run", `${skill.risk} risk skills need an engineer for ${t.name}.`];
  return [a.default, `${t.name}'s default autonomy for low-risk skills.`];
}

function skillsView(tenant: string | null) {
  return {
    library_version: "ops-foundation 2026.10.2",
    skills: SKILLS.map(({ stats, ...s }) => ({
      ...s,
      tenants: TENANTS.filter((t) => !tenant || t.id === tenant).map((t) => {
        const [mode, why] = modeFor(t, { stats, ...s });
        const [proposals, fixed, noAction, failed, rejected] = stats[t.id] ?? [0, 0, 0, 0, 0];
        return { tenant_id: t.id, tenant_name: t.name, mode, why, proposals, fixed, no_action: noAction, failed, rejected, success_rate: proposals ? fixed / proposals : null, last_proposed: proposals ? ago(60 * (proposals * 7)) : null };
      }),
    })),
  };
}

/* ---------------- integrations ---------------- */
const integrations: Integration[] = [
  ["contoso", "vcenter", "inventory", "ok", 1, 1, null, { vms: 21, hosts: 4, datastores: 4 }],
  ["contoso", "veeam", "inventory", "ok", 4, 4, null, { jobs: 6, repositories: 1 }],
  ["contoso", "microsoft", "inventory", "ok", 6, 6, null, { devices: 214, users: 1310 }],
  ["contoso", "edge", "events", "ok", 0.3, 0.3, null, { agents: 19, version: "0.14.2" }],
  ["contoso", "metrics alerts (vmalert)", "alerts", "ok", 0.2, 0.2, null, { rules: 48, firing: 19 }],
  ["contoso", "declared services", "inventory", "ok", 60, 60, null, "services.yml · 4 services"],
  ["contoso", "teams", "events", "ok", 3.8, 3.8, null, { channel: "ops-contoso" }],
  ["fabrikam", "vcenter", "inventory", "ok", 2, 2, null, { vms: 9, hosts: 2, datastores: 1 }],
  ["fabrikam", "veeam", "inventory", "ok", 3, 3, null, { jobs: 2, repositories: 1 }],
  ["fabrikam", "microsoft", "inventory", "failing", 5, 185, "AADSTS7000222: the client secret for app 'estate-ops-fabrikam' expired", { tenant: "fabrikamhealth.onmicrosoft.com" }],
  ["fabrikam", "edge", "events", "waiting", 12, 12, null, "waiting for the edge agent at Hospital 3 to check in"],
  ["fabrikam", "metrics alerts (vmalert)", "alerts", "ok", 0.2, 0.2, null, { rules: 31, firing: 0 }],
  ["fabrikam", "declared services", "inventory", "ok", 60, 60, null, "services.yml · 3 services"],
  ["fabrikam", "teams", "events", "off", null, null, null, "disabled by customer"],
].map(([tenant_id, name, kind, state, run, ok, err, detail]) => ({
  tenant_id: tenant_id as string,
  tenant_name: TENANTS.find((t) => t.id === tenant_id)!.name,
  name: name as string,
  kind: kind as string,
  state: state as string,
  last_run_at: run === null ? null : ago(run as number),
  last_ok_at: ok === null ? null : ago(ok as number),
  last_error: err as string | null,
  detail: detail as string | JsonObject,
}));

/* ---------------- lab ---------------- */
const faults: Fault[] = [
  { id: "checkout-slow", tenant: "contoso", title: "Checkout slows down", what: "CTO-BI-ETL01 starts an unthrottled full reload on CTO-DS-CRIT-02; Checkout's SQL server stalls.", active: true },
  { id: "backup-repo-full", tenant: "fabrikam", title: "EHR backup fails", what: "FAB-REPO-01 fills up and the EHR SQL nightly job fails.", active: false },
  { id: "pos-offline", tenant: "contoso", title: "A till goes offline", what: "POS-114 in Store 14 stops sending heartbeats.", active: false },
  { id: "portal-5xx", tenant: "fabrikam", title: "Patient portal errors", what: "The portal web service on FAB-PORTAL-WEB01 starts returning 5xx.", active: false },
];
const labEvents: LabEvent[] = [
  { at: NOW / 1000 - 16 * 60 - 30, tenant: "contoso", what: "fault injected: checkout-slow" },
  { at: NOW / 1000 - 16 * 60 - 20, tenant: "contoso", what: "datastore latency rising", asset: "CTO-DS-CRIT-02", latency_ms: 21 },
  { at: NOW / 1000 - 16 * 60, tenant: "contoso", what: "alert firing", alertname: "DatastoreLatencyHigh", target: "CTO-DS-CRIT-02" },
  { at: NOW / 1000 - 15 * 60, tenant: "contoso", what: "alerts firing", count: 12, alertname: "ServerDiskLatencyHigh" },
  { at: NOW / 1000 - 14 * 60, tenant: "contoso", what: "service degraded", service: "Checkout", p95_ms: 2540, errors_pct: 3.2 },
  { at: NOW / 1000 - 23 * 60 - 10, tenant: "fabrikam", what: "fault injected: backup-repo-full" },
  { at: NOW / 1000 - 23 * 60, tenant: "fabrikam", what: "job failed", job: "EHR SQL nightly" },
  { at: NOW / 1000 - 20 * 60, tenant: "fabrikam", what: "restore points deleted", asset: "FAB-OLD-LIS01", gb: 720 },
  { at: NOW / 1000 - 20 * 60 + 14, tenant: "fabrikam", what: "restore points deleted", asset: "FAB-OLD-LIS02", gb: 690 },
  { at: NOW / 1000 - 20 * 60 + 100, tenant: "fabrikam", what: "job succeeded", job: "EHR SQL nightly" },
];
const labEvent = (tenant: string, what: string, extra: JsonObject = {}) => labEvents.push({ at: Date.now() / 1000, tenant, what, ...extra });

/* ---------------- ledger writes ---------------- */
function append(c: MockCase, actor: string, kind: string, body: JsonObject): LedgerEntry {
  const all = cases.flatMap((x) => (x.case.tenant_id === c.case.tenant_id ? x.timeline : []));
  const last = all.reduce<LedgerEntry | null>((a, e) => (!a || Number(e.id) > Number(a.id) ? e : a), null);
  const maxId = Math.max(...cases.flatMap((x) => x.timeline.map((e) => Number(e.id))));
  const e: LedgerEntry = { id: maxId + 1, at: live(), actor, kind, body, prev_hash: last?.hash ?? null, hash: "" };
  e.hash = fakeHash(`${e.prev_hash}|${c.case.tenant_id}|${c.case.id}|${actor}|${kind}|${JSON.stringify(body)}`);
  c.timeline.push(e);
  c.case.updated_at = e.at;
  return e;
}

function approve(c: MockCase, pid: string, note: string) {
  const p = c.proposals.find((x) => x.id === pid)!;
  p.status = "running";
  p.decided_by = "engineer:Priya Shah";
  p.decided_at = live();
  c.case.status = "running";
  append(c, "engineer:Priya Shah", "approved", { proposal_id: pid, note });
  if (c.case.id !== "c-contoso-12") return;
  labEvent("contoso", "storage vMotion started", { vm: "CTO-BI-ETL01", to: "CTO-DS-GEN-02" });
  window.setTimeout(() => {
    const evidence = {
      decision: p.dry_run_evidence?.decision ?? {},
      before: { datastore: "CTO-DS-CRIT-02", "CTO-DS-CRIT-02 latency_ms": 38, "Checkout p95_ms": 2540, "Checkout errors_pct": 3.2 },
      actions: [{ action: "storage_vmotion", vm: "CTO-BI-ETL01", from: "CTO-DS-CRIT-02", to: "CTO-DS-GEN-02", moved_gb: 612 }],
      after: { datastore: "CTO-DS-GEN-02", "CTO-DS-CRIT-02 latency_ms": 3.1, "Checkout p95_ms": 340, "Checkout errors_pct": 0.1 },
      outcome: "fixed",
      error: null,
      seconds: 418.6,
      log: ["relocate task-8812 queued", "copying 612 GB", "relocate task-8812 completed", "CTO-DS-CRIT-02 latency 3.1 ms"],
    };
    p.status = "done";
    p.outcome = "fixed";
    p.evidence = evidence as never;
    append(c, "agent:remediator", "run_result", { proposal_id: pid, dry_run: false, evidence: evidence as unknown as JsonObject });
    append(c, "agent:verifier", "verified", { summary: "Datastore latency and Checkout are back to normal for 5 minutes.", checks: [{ check: "CTO-DS-CRIT-02 latency", result: "3.1 ms" }, { check: "Checkout p95", result: "340 ms" }] });
    append(c, "platform", "resolved", { reason: "Fixed by vmware/rebalance-datastore and verified." });
    c.case.status = "resolved";
    c.case.resolved_at = live();
    const etl = estates.contoso.assets.find((a) => a.name === "CTO-BI-ETL01")!;
    etl.attrs.datastore = "CTO-DS-GEN-02";
    const r = estates.contoso.relations.find((x) => x.src === etl.id && x.rel === "STORED_ON")!;
    r.dst = "datastore:CTO-DS-GEN-02";
    estates.contoso.changes.unshift({ id: 9999, at: live(), source: "vcenter", asset_id: etl.id, asset_name: etl.name, change: "changed", before: { datastore: "CTO-DS-CRIT-02" }, after: { datastore: "CTO-DS-GEN-02" } });
    labEvent("contoso", "storage vMotion finished", { vm: "CTO-BI-ETL01", seconds: 418.6 });
    faults[0].active = false;
  }, 3000);
}

/* ---------------- views ---------------- */
const FEED_KINDS = new Set(["case_opened", "investigation", "proposal", "approval_requested", "approved", "rejected", "run_result", "escalated", "resolved", "verified"]);
const closed = (s: string) => s === "resolved" || s === "escalated";
const week = NOW - 7 * 86400_000;

function overview(tenant: string | null): Overview {
  const per = TENANTS.filter((t) => !tenant || t.id === tenant).map((t) => {
    const cs = cases.filter((c) => c.case.tenant_id === t.id).map((c) => c.case);
    const resolved7 = cs.filter((c) => c.status === "resolved" && Date.parse(c.resolved_at ?? "") > week);
    const byAgents = resolved7.filter((c) => !cases.find((x) => x.case.id === c.id)!.timeline.some((e) => (e.kind === "approved" || e.kind === "rejected") && e.actor.startsWith("engineer:")));
    const mins = resolved7.map((c) => (Date.parse(c.resolved_at!) - Date.parse(c.opened_at)) / 60000).sort((a, b) => a - b);
    const atRisk = cs.filter((c) => {
      if (closed(c.status)) return false;
      const [due, done] = c.responded_at ? [c.resolve_due, c.resolved_at] : [c.respond_due, c.responded_at];
      if (!due || done) return false;
      const left = Date.parse(due) - Date.now();
      return left / (Date.parse(due) - Date.parse(c.opened_at)) < 0.25;
    }).length;
    return {
      id: t.id,
      name: t.name,
      industry: t.industry,
      open: cs.filter((c) => !closed(c.status)).length,
      awaiting_approval: cs.filter((c) => c.status === "awaiting_approval").length,
      escalated_7d: cs.filter((c) => c.status === "escalated" && Date.parse(c.updated_at) > week).length,
      resolved_7d: resolved7.length,
      resolved_by_agents_7d: byAgents.length,
      opened_24h: cs.filter((c) => Date.parse(c.opened_at) > NOW - 86400_000).length,
      median_resolve_minutes_7d: mins.length ? +mins[Math.floor(mins.length / 2)].toFixed(1) : null,
      sla_at_risk: atRisk,
      sla_breached_30d: t.id === "contoso" ? 1 : 0,
      sla_met_pct_30d: t.id === "contoso" ? { respond: 100, resolve: 96.4 } : { respond: 100, resolve: 100 },
      ai_spend_month_usd: t.ai_spend_month_usd,
      ai_budget_usd_month: t.ai_budget_usd_month,
      assets: estates[t.id].assets.length,
      autonomy_default: (t.autonomy as Record<string, string>).default,
    };
  });
  const sum = (k: "open" | "awaiting_approval" | "resolved_7d" | "resolved_by_agents_7d" | "opened_24h" | "sla_at_risk") => per.reduce((a, x) => a + x[k], 0);
  const feed = cases
    .filter((c) => !tenant || c.case.tenant_id === tenant)
    .flatMap((c) => c.timeline.filter((e) => FEED_KINDS.has(e.kind)).map((e) => ({ id: e.id, at: e.at, tenant_id: c.case.tenant_id, actor: e.actor, kind: e.kind, body: e.body, case_id: c.case.id, ref: c.case.ref, title: c.case.title })))
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
    .slice(0, 40);
  return {
    tenants: per,
    totals: { open: sum("open"), awaiting_approval: sum("awaiting_approval"), resolved_7d: sum("resolved_7d"), resolved_by_agents_7d: sum("resolved_by_agents_7d"), opened_24h: sum("opened_24h"), sla_at_risk: sum("sla_at_risk") },
    feed,
  };
}

function caseRows(tenant: string | null, status: string | null, q: string | null) {
  const needle = q?.toLowerCase();
  return cases
    .map((c) => c.case)
    .filter((c) => !tenant || c.tenant_id === tenant)
    .filter((c) => !status || (status === "active" ? !closed(c.status) : c.status === status))
    .filter((c) => !needle || [c.ref, c.title, c.target ?? "", c.service ?? ""].some((x) => x.toLowerCase().includes(needle)))
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .map(({ assets: _a, parent_id: _p, requester: _r, ...row }) => row);
}

function assetView(tenant: string, id: string, hops: number) {
  const est = estates[tenant];
  const asset = est?.assets.find((a) => a.id === id);
  if (!asset) return null;
  const seen = new Set([id]);
  let frontier = [id];
  for (let h = 0; h < hops; h++) {
    const next: string[] = [];
    for (const r of est.relations) {
      for (const [a, b] of [[r.src, r.dst], [r.dst, r.src]]) if (frontier.includes(a) && !seen.has(b)) (seen.add(b), next.push(b));
    }
    frontier = next;
  }
  const assets = est.assets.filter((a) => seen.has(a.id));
  return {
    asset,
    graph: { centre: id, assets, relations: est.relations.filter((r) => seen.has(r.src) && seen.has(r.dst)) },
    cases: cases.filter((c) => c.case.tenant_id === tenant && c.case.assets.includes(id)).map((c) => ({ id: c.case.id, ref: c.case.ref, title: c.case.title, severity: c.case.severity, status: c.case.status, opened_at: c.case.opened_at })),
  };
}

/* ---------------- router ---------------- */
let signedIn = !new URLSearchParams(location.search).has("mock") || new URLSearchParams(location.search).get("mock") !== "signin";

async function route(method: string, url: URL, body: JsonObject, auth: string | null): Promise<[number, unknown]> {
  const p = url.pathname;
  const qp = (k: string) => url.searchParams.get(k) || null;
  if (p === "/auth/me") return [200, { sso: false, user: signedIn ? { name: "Priya Shah", email: "priya.shah@ops.example" } : null, signed_in: signedIn }];
  if (p === "/auth/logout") return ((signedIn = false), [200, { ok: true }]);
  if (p === "/healthz") return [200, { ok: true, version: "0.9.0-dev", skills: SKILLS.length, model: "claude-opus-5-5" }];
  if (!signedIn) {
    if (auth) signedIn = true;
    else return [401, { detail: "not signed in" }];
  }
  let m: RegExpMatchArray | null;
  if (p === "/api/v1/tenants") return [200, TENANTS];
  if (p === "/api/v1/overview") return [200, overview(qp("tenant"))];
  if (p === "/api/v1/cases") return [200, caseRows(qp("tenant"), qp("status"), qp("q"))];
  if ((m = p.match(/^\/api\/v1\/cases\/([^/]+)$/))) {
    const c = cases.find((x) => x.case.id === decodeURIComponent(m![1]));
    return c ? [200, c] : [404, { detail: "case not found" }];
  }
  if ((m = p.match(/^\/api\/v1\/cases\/([^/]+)\/ask$/)) && method === "POST") {
    const c = cases.find((x) => x.case.id === decodeURIComponent(m![1]));
    if (!c) return [404, { detail: "case not found" }];
    const question = String(body.question ?? "");
    if (/rate ?limit/i.test(question)) return [429, { detail: "too many questions; try again in a minute" }];
    await new Promise((r) => setTimeout(r, 1400));
    const answer = {
      answer:
        c.case.id === "c-contoso-12"
          ? "The plan touches one VM (CTO-BI-ETL01). Checkout and Online shop keep running throughout; only the Sales analytics ETL slows down during the 6–8 minute copy. If latency on CTO-DS-GEN-02 rose above 10 ms the skill would stop and leave the VM where it is."
          : `Looking at ${c.case.ref}: ${c.case.summary ?? c.case.title}. Nothing else in the estate graph depends on ${c.case.target ?? "this asset"} beyond what is listed under affected assets.`,
      method: "llm",
      model: "claude-haiku-5-5",
      tools: ["estate.neighbours", "case.timeline"],
      ai_cost_usd: 0.0064,
    };
    append(c, "engineer:Priya Shah", "copilot_question", { question });
    append(c, "agent:copilot", "copilot_answer", answer);
    c.ai_cost_usd += answer.ai_cost_usd;
    return [200, answer];
  }
  if ((m = p.match(/^\/api\/v1\/proposals\/([^/]+)\/(approve|reject)$/)) && method === "POST") {
    const c = cases.find((x) => x.proposals.some((pr) => pr.id === m![1]));
    const pr = c?.proposals.find((x) => x.id === m![1]);
    if (!c || !pr) return [404, { detail: "proposal not found" }];
    if (pr.status !== "awaiting_approval") return [409, { detail: `proposal is ${pr.status}, not awaiting approval` }];
    await new Promise((r) => setTimeout(r, 500));
    const note = String(body.note ?? "");
    if (m[2] === "approve") approve(c, pr.id, note);
    else {
      pr.status = "rejected";
      pr.decided_by = "engineer:Priya Shah";
      pr.decided_at = live();
      append(c, "engineer:Priya Shah", "rejected", { proposal_id: pr.id, note });
      append(c, "platform", "escalated", { reason: "Proposal rejected by an engineer.", engineer_should_check: ["Agree a fix with the customer", "Throttle the ETL job on the BI side"] });
      c.case.status = "escalated";
    }
    return [200, { ok: true, status: pr.status }];
  }
  if ((m = p.match(/^\/api\/v1\/estate\/([^/]+)$/))) {
    const est = estates[m[1]];
    if (!est) return [404, { detail: "unknown tenant" }];
    const q = qp("q")?.toLowerCase();
    const kind = qp("kind");
    const kinds: Record<string, number> = {};
    const sources: Record<string, number> = {};
    for (const a of est.assets) {
      kinds[a.kind] = (kinds[a.kind] ?? 0) + 1;
      for (const s of a.sources) sources[s] = (sources[s] ?? 0) + 1;
    }
    return [
      200,
      {
        stats: { total: est.assets.length, kinds, sources },
        assets: est.assets.filter((a) => (!kind || a.kind === kind) && (!q || a.name.toLowerCase().includes(q) || a.id.toLowerCase().includes(q))),
        changes: [...est.changes].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)),
      },
    ];
  }
  if ((m = p.match(/^\/api\/v1\/estate\/([^/]+)\/assets\/(.+)$/))) {
    const v = assetView(m[1], decodeURIComponent(m[2]), Number(qp("hops") ?? 2));
    return v ? [200, v] : [404, { detail: "asset not found" }];
  }
  if (p === "/api/v1/skills") return [200, skillsView(qp("tenant"))];
  if (p === "/api/v1/integrations") return [200, integrations.filter((i) => !qp("tenant") || i.tenant_id === qp("tenant"))];
  if (p === "/api/v1/cost") {
    const t = qp("tenant");
    const days = Number(qp("days") ?? 30);
    const since = NOW - days * 86400_000;
    return [
      200,
      {
        days,
        tenants: TENANTS.filter((x) => !t || x.id === t).map((x) => {
          const n = cases.filter((c) => c.case.tenant_id === x.id).length * 9 + 4;
          return { id: x.id, name: x.name, diagnosis: x.diagnosis, ai_spend_month_usd: x.ai_spend_month_usd, ai_budget_usd_month: x.ai_budget_usd_month, cases_with_ai: n, cost_per_case_usd: +(x.ai_spend_month_usd / n).toFixed(4) };
        }),
        by_agent: byAgent.filter((r) => !t || r.tenant_id === t),
        daily: daily.filter((r) => (!t || r.tenant_id === t) && Date.parse(r.day) >= since - 86400_000),
      } satisfies CostView,
    ];
  }
  if (p === "/api/v1/lab/faults") return [200, faults];
  if (p === "/api/v1/lab/events") return [200, labEvents.slice(-200)];
  if (p === "/api/v1/lab/reset" && method === "POST") {
    faults.forEach((f) => (f.active = false));
    labEvent("contoso", "estate reset");
    labEvent("fabrikam", "estate reset");
    return [200, { ok: true }];
  }
  if ((m = p.match(/^\/api\/v1\/lab\/faults\/([^/]+)$/)) && method === "POST") {
    const f = faults.find((x) => x.id === decodeURIComponent(m![1]));
    if (!f) return [404, { detail: "unknown fault" }];
    f.active = true;
    labEvent(f.tenant, `fault injected: ${f.id}`);
    return [200, { ok: true }];
  }
  return [404, { detail: `mock: no route for ${method} ${p}` }];
}

export function installMock(): void {
  const real = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.origin);
    if (url.origin !== location.origin || !/^\/(api|auth|healthz)/.test(url.pathname)) return real(input, init);
    const method = (init?.method ?? "GET").toUpperCase();
    let body: JsonObject = {};
    try {
      body = init?.body ? JSON.parse(String(init.body)) : {};
    } catch {
      /* not JSON */
    }
    const headers = new Headers(init?.headers);
    await new Promise((r) => setTimeout(r, 60 + Math.random() * 120));
    const [status, data] = await route(method, url, body, headers.get("Authorization"));
    return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  };
  console.info("[cockpit] mock backend installed (VITE_MOCK=1)");
}
