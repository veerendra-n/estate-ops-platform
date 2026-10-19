// Dev-only fixture cases: Contoso's Checkout slowdown (awaiting approval) and Fabrikam's backup failure
// (fixed end to end by agents), plus a handful of older cases.
import type { CaseFull, JsonObject, LedgerEntry, Proposal, Signal } from "../types";

export interface MockCase {
  case: CaseFull;
  signals: Signal[];
  proposals: Proposal[];
  timeline: LedgerEntry[];
  external_refs: JsonObject[];
  ai_cost_usd: number;
  blast_radius: { services: string[]; critical: boolean; users: number; dependents: number } | null;
}

/** Deterministic fake sha256: good enough to look like a hash chain. */
export function fakeHash(input: string): string {
  let out = "";
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let round = 0; out.length < 64; round++) {
    for (let i = 0; i < input.length; i++) {
      h1 = Math.imul(h1 ^ input.charCodeAt(i), 16777619) >>> 0;
      h2 = Math.imul(h2 + input.charCodeAt(i) + round, 2246822519) >>> 0;
    }
    out += ((h1 ^ h2) >>> 0).toString(16).padStart(8, "0");
    h1 = (h1 + 0x9e3779b9) >>> 0;
  }
  return out.slice(0, 64);
}

type E = { at: string; actor: string; kind: string; body: JsonObject };

export function buildCases(ago: (min: number) => string, inMin: (min: number) => string): MockCase[] {
  const cases: MockCase[] = [];
  const base = (c: Partial<CaseFull> & Pick<CaseFull, "id" | "tenant_id" | "ref" | "title" | "severity" | "status">): CaseFull => ({
    tenant_name: c.tenant_id === "contoso" ? "Contoso Retail" : "Fabrikam Health",
    type: "incident",
    source: "vmalert",
    service: null,
    target: null,
    summary: null,
    opened_at: ago(60),
    updated_at: ago(30),
    responded_at: null,
    resolved_at: null,
    respond_due: null,
    resolve_due: null,
    signals: 0,
    assets: [],
    parent_id: null,
    requester: null,
    ...c,
  });
  const add = (c: CaseFull, entries: E[], extra: Partial<MockCase> = {}) => {
    cases.push({
      case: c,
      signals: [],
      proposals: [],
      timeline: entries.map((e) => ({ ...e, id: 0, hash: "", prev_hash: null })),
      external_refs: [],
      ai_cost_usd: 0,
      blast_radius: null,
      ...extra,
    });
  };

  /* ---------------- Scenario 1: Contoso Checkout slowdown ---------------- */
  {
    const t0 = 16; // minutes ago the first alert arrived
    const at = (sec: number) => ago(t0 - sec / 60);
    const sig = (sec: number, alertname: string, target: string, kind: string, extra: Record<string, string> = {}): Signal => ({
      alert_id: `vmalert-${fakeHash(alertname + target).slice(0, 10)}`,
      source: "vmalert",
      target,
      asset_id: `${kind}:${target}`,
      labels: { alertname, severity: "warning", tenant_id: "contoso", ...extra },
      received_at: at(sec),
    });
    const servers = [
      "CTO-SHOP-SQL01",
      "CTO-SHOP-API01",
      "CTO-SHOP-API02",
      "CTO-SHOP-API03",
      "CTO-SHOP-WEB01",
      "CTO-SHOP-WEB02",
      "CTO-SHOP-WEB03",
      "CTO-SHOP-WEB04",
      "CTO-SHOP-CMS01",
      "CTO-SHOP-CMS02",
      "CTO-SHOP-SEARCH01",
      "CTO-BI-ETL01",
    ];
    const signals: Signal[] = [
      sig(0, "DatastoreLatencyHigh", "CTO-DS-CRIT-02", "datastore", { value: "38 ms", threshold: "20 ms" }),
      ...servers.map((s, i) =>
        sig(20 + i * 9, "ServerDiskLatencyHigh", s, "server", { value: s === "CTO-SHOP-SQL01" ? "15 s IO stalls" : `${(44 + ((i * 37) % 60)).toFixed(0)} ms` }),
      ),
      sig(130, "ServiceLatencyHigh", "Checkout", "service", { value: "2540 ms p95", threshold: "800 ms", severity: "critical" }),
      sig(150, "ServiceErrorsHigh", "Checkout", "service", { value: "3.2%", threshold: "1%", severity: "critical" }),
      sig(170, "ServiceLatencyHigh", "Online shop", "service", { value: "1910 ms p95", threshold: "1000 ms" }),
      sig(185, "HostCPUHigh", "CTO-BI-ETL01", "server", { value: "92%", threshold: "85%" }),
      sig(200, "DatastoreIOPSHigh", "CTO-DS-CRIT-02", "datastore", { value: "41k IOPS", threshold: "30k" }),
      sig(215, "ServerDiskLatencyHigh", "CTO-SHOP-REDIS01", "server", { value: "26 ms" }),
    ];
    const opened = at(0);
    const plan: JsonObject[] = [
      {
        action: "storage_vmotion",
        vm: "CTO-BI-ETL01",
        from: "CTO-DS-CRIT-02",
        to: "CTO-DS-GEN-02",
        detail: "Move the ETL VM's disks (612 GB) to the general-purpose datastore, online, no reboot.",
        target_latency_ms: 2.4,
        target_free_gb: 20268,
      },
      { action: "verify", detail: "Wait for CTO-DS-CRIT-02 latency < 10 ms and Checkout p95 < 800 ms for 5 minutes." },
    ];
    const blast = { services: ["Checkout", "Online shop", "Sales analytics"], critical: true, users: 3640, dependents: 17 };
    const policy = {
      mode: "approve_to_run",
      reason: "Contoso allows storage moves only with engineer approval while a business-critical service is in the blast radius.",
      caps: ["max_targets=1", "business_hours=allowed", "vmotion_concurrency=1"],
    };
    const proposal: Proposal = {
      id: "prop-c12-1",
      skill_id: "vmware/rebalance-datastore",
      skill_version: "1.4.2",
      targets: ["server:CTO-BI-ETL01"],
      params: { vm: "CTO-BI-ETL01", source_datastore: "CTO-DS-CRIT-02", min_free_pct: 20, max_latency_ms: 10 },
      risk: "medium",
      mode: "approve_to_run",
      policy,
      status: "awaiting_approval",
      dry_run_evidence: {
        decision: {
          action: "move",
          reason: "CTO-BI-ETL01 drives 78% of CTO-DS-CRIT-02's IO and is not business critical; CTO-DS-GEN-02 has the most headroom (2.4 ms, 20268 GB free).",
          plan,
        },
        before: { datastore: "CTO-DS-CRIT-02", datastore_latency_ms: 38, etl_io_share_pct: 78 },
        outcome: "planned",
        seconds: 6.8,
      },
      evidence: null,
      outcome: null,
      decided_by: null,
      decided_at: null,
      created_at: at(250),
    };
    const entries: E[] = [
      {
        at: opened,
        actor: "triage",
        kind: "case_opened",
        body: {
          alert_id: signals[0].alert_id,
          alertname: "DatastoreLatencyHigh",
          source: "vmalert",
          target: "CTO-DS-CRIT-02",
          summary: "Write latency on datastore CTO-DS-CRIT-02 is 38 ms (threshold 20 ms) for 5 minutes.",
          labels: signals[0].labels as JsonObject,
        },
      },
      ...signals.slice(1).map(
        (s): E => ({
          at: s.received_at,
          actor: "triage",
          kind: "alert_folded",
          body: { alert_id: s.alert_id, alertname: s.labels?.alertname ?? "", target: s.target, reason: "same storage path as CTO-DS-CRIT-02" },
        }),
      ),
      {
        at: at(235),
        actor: "agent:investigator",
        kind: "investigation",
        body: {
          summary:
            "Nineteen alerts across Checkout, Online shop and 13 VMs share one cause: every affected VM sits on datastore CTO-DS-CRIT-02, whose latency rose from 2 ms to 38 ms at the same moment the Sales analytics ETL started.",
          likely_cause:
            "CTO-BI-ETL01 (Sales analytics, not business critical) is running a full reload with no IO throttle and drives ~78% of CTO-DS-CRIT-02's IO. Checkout's SQL server CTO-SHOP-SQL01 sees 15 s IO stalls as a result.",
          confidence: 0.86,
          root_asset: "server:CTO-BI-ETL01",
          method: "llm",
          evidence: [
            { source: "vcenter", finding: "CTO-DS-CRIT-02 write latency 38 ms (7-day p95: 2.1 ms); 14 VMs on this datastore." },
            { source: "vcenter", finding: "CTO-BI-ETL01 issues 32k of 41k IOPS on the datastore (78%) since 09:58." },
            { source: "edge", finding: "CTO-SHOP-SQL01 error log: 412 'I/O requests taking longer than 15 seconds' messages in 10 min." },
            { source: "metrics", finding: "Checkout p95 2540 ms (normal 310 ms), error rate 3.2% — both started within 60 s of the datastore spike." },
            { source: "estate graph", finding: "Checkout, Online shop and Sales analytics all depend on VMs stored on CTO-DS-CRIT-02." },
            { source: "changes", finding: "No configuration changes on the datastore, hosts or SAN in the last 24 h." },
          ],
          notes: "Throttling the ETL job inside the VM is not possible without a change on the BI side; moving the VM off the critical datastore is the fastest safe relief.",
          ai_cost_usd: 0.184,
        },
      },
      {
        at: at(240),
        actor: "agent:remediator",
        kind: "proposal",
        body: {
          proposal_id: proposal.id,
          skill_id: proposal.skill_id,
          skill_title: "Rebalance a hot datastore",
          target: "CTO-BI-ETL01",
          params: proposal.params,
          risk: "medium",
          approval: "required",
          mode: "approve_to_run",
          policy,
          blast_radius: blast,
          rollback: "Storage vMotion CTO-BI-ETL01 back to CTO-DS-CRIT-02 (same skill, reversed).",
        },
      },
      {
        at: at(250),
        actor: "agent:remediator",
        kind: "run_result",
        body: { proposal_id: proposal.id, dry_run: true, evidence: proposal.dry_run_evidence as JsonObject },
      },
      { at: at(252), actor: "policy", kind: "approval_requested", body: { proposal_id: proposal.id, reason: "approve_to_run for Contoso Retail" } },
      { at: ago(4), actor: "engineer:Priya Shah", kind: "copilot_question", body: { question: "Is it safe to move the ETL VM during business hours?" } },
      {
        at: ago(3.9),
        actor: "agent:copilot",
        kind: "copilot_answer",
        body: {
          answer:
            "Yes. Storage vMotion is online: CTO-BI-ETL01 keeps running and only Sales analytics sees slower ETL while the 612 GB copy runs (about 6–8 minutes at current throughput). CTO-DS-GEN-02 is at 2.4 ms with 20 TB free, so the move will not create a new hotspot. Checkout should recover as soon as the copy finishes, because the ETL's IO leaves CTO-DS-CRIT-02. The skill moves one VM at a time and can be reversed with the same skill.",
          method: "llm",
          model: "claude-haiku-5-5",
          tools: ["estate.neighbours", "metrics.query", "skills.describe"],
          ai_cost_usd: 0.0071,
        },
      },
    ];
    add(
      base({
        id: "c-contoso-12",
        tenant_id: "contoso",
        ref: "CONTOSO-00012",
        title: "Datastore CTO-DS-CRIT-02 latency high",
        severity: "P2",
        status: "awaiting_approval",
        service: "Checkout",
        target: "CTO-DS-CRIT-02",
        summary: "CTO-BI-ETL01 saturates CTO-DS-CRIT-02; Checkout slow.",
        opened_at: opened,
        updated_at: ago(3.9),
        responded_at: at(40),
        respond_due: at(15 * 60),
        resolve_due: at(4 * 3600),
        signals: signals.length,
        assets: [
          "datastore:CTO-DS-CRIT-02",
          "server:CTO-BI-ETL01",
          "server:CTO-SHOP-SQL01",
          "service:Checkout",
          "service:Online shop",
          "service:Sales analytics",
          "datastore:CTO-DS-GEN-02",
        ],
      }),
      entries,
      { signals, proposals: [proposal], ai_cost_usd: 0.1911, blast_radius: blast, external_refs: [{ system: "teams", ref: "msg 19:ops-contoso/1729330128", created_at: at(260) }] },
    );
  }

  /* ---------------- Scenario 2: Fabrikam backup failure, fixed in 3 minutes ---------------- */
  {
    const t0 = 23;
    const at = (sec: number) => ago(t0 - sec / 60);
    const signals: Signal[] = [
      {
        alert_id: "veeam-7f31c9a2",
        source: "veeam",
        target: "EHR SQL nightly",
        asset_id: "backup_job:EHR SQL nightly",
        labels: { alertname: "veeam-job-failed", job: "EHR SQL nightly", result: "Failed", reason: "Not enough free space on repository FAB-REPO-01" },
        received_at: at(0),
      },
      {
        alert_id: "vmalert-1c40be77",
        source: "vmalert",
        target: "FAB-REPO-01",
        asset_id: "backup_repository:FAB-REPO-01",
        labels: { alertname: "BackupRepositoryLow", value: "0.4% free", threshold: "10%", severity: "warning" },
        received_at: at(12),
      },
    ];
    const plan: JsonObject[] = [
      { action: "delete_restore_points", target: "FAB-OLD-LIS01", detail: "Decommissioned lab system, retention ended 2025-11-02.", frees_gb: 720 },
      { action: "delete_restore_points", target: "FAB-OLD-LIS02", detail: "Decommissioned lab system, retention ended 2025-11-02.", frees_gb: 690 },
      { action: "rerun_job", target: "EHR SQL nightly", detail: "Start the failed job again once free space > 8%." },
    ];
    const policy = { mode: "run_and_notify", reason: "Low-risk backup housekeeping on retired systems may run without approval for Fabrikam Health.", caps: ["only_retired_assets", "max_delete_gb=2000"] };
    const live = {
      decision: { action: "free_and_rerun", reason: "Freed space by removing restore points of retired systems, then reran the job.", plan },
      before: { result: "failed", free_gb: 66, free_pct: 0.4 },
      actions: <JsonObject[]>[
        { action: "delete_restore_points", target: "FAB-OLD-LIS01", freed_gb: 720 },
        { action: "delete_restore_points", target: "FAB-OLD-LIS02", freed_gb: 690 },
        { action: "rerun_job", target: "EHR SQL nightly", session: "EHR SQL nightly (Retry 1)" },
      ],
      after: { result: "success", free_gb: 1459, free_pct: 8.9 },
      outcome: "fixed",
      error: null,
      seconds: 101.4,
      log: [
        "09:31:02 connect veeam-b&r FAB-VBR01 ok",
        "09:31:04 repository FAB-REPO-01 free 66 GB (0.4%)",
        "09:31:05 delete restore points FAB-OLD-LIS01 (720 GB) ok",
        "09:31:19 delete restore points FAB-OLD-LIS02 (690 GB) ok",
        "09:31:33 repository FAB-REPO-01 free 1476 GB (9.0%)",
        "09:31:34 start job 'EHR SQL nightly' (retry)",
        "09:32:43 job 'EHR SQL nightly' finished: Success, 17 GB transferred",
      ],
    };
    const proposal: Proposal = {
      id: "prop-f07-1",
      skill_id: "backup/free-space-and-rerun",
      skill_version: "2.1.0",
      targets: ["backup_job:EHR SQL nightly"],
      params: { job: "EHR SQL nightly", repository: "FAB-REPO-01", target_free_pct: 8, only_retired: true },
      risk: "low",
      mode: "run_and_notify",
      policy,
      status: "done",
      dry_run_evidence: { decision: { action: "free_and_rerun", reason: "Two retired systems hold 1410 GB of restore points on FAB-REPO-01.", plan }, before: live.before, outcome: "planned", seconds: 3.1 },
      evidence: live,
      outcome: "fixed",
      decided_by: "policy",
      decided_at: at(58),
      created_at: at(50),
    };
    const blast = { services: ["EHR"], critical: true, users: 2100, dependents: 2 };
    const entries: E[] = [
      {
        at: at(0),
        actor: "triage",
        kind: "case_opened",
        body: {
          alert_id: "veeam-7f31c9a2",
          alertname: "veeam-job-failed",
          source: "veeam",
          target: "EHR SQL nightly",
          summary: "Backup job 'EHR SQL nightly' failed: not enough free space on repository FAB-REPO-01.",
          labels: signals[0].labels as JsonObject,
        },
      },
      { at: at(12), actor: "triage", kind: "alert_folded", body: { alert_id: "vmalert-1c40be77", alertname: "BackupRepositoryLow", target: "FAB-REPO-01" } },
      {
        at: at(40),
        actor: "agent:investigator",
        kind: "investigation",
        body: {
          summary: "The EHR SQL nightly backup failed because its repository FAB-REPO-01 is full (0.4% free).",
          likely_cause: "Restore points of two retired lab systems (FAB-OLD-LIS01, FAB-OLD-LIS02) still occupy 1410 GB on FAB-REPO-01 after their retention ended.",
          confidence: "high",
          root_asset: "backup_repository:FAB-REPO-01",
          method: "deterministic",
          evidence: [
            { source: "veeam", finding: "Job session 'EHR SQL nightly' failed at 00:41: 'Not enough free space on FAB-REPO-01'." },
            { source: "veeam", finding: "FAB-REPO-01: 66 GB free of 16384 GB (0.4%)." },
            { source: "estate graph", finding: "FAB-OLD-LIS01 and FAB-OLD-LIS02 are marked decommissioned; retention ended 2025-11-02." },
          ],
          notes: "Matched known pattern 'repository full + retired restore points'; no model call needed.",
          ai_cost_usd: 0,
        },
      },
      {
        at: at(50),
        actor: "agent:remediator",
        kind: "proposal",
        body: {
          proposal_id: proposal.id,
          skill_id: proposal.skill_id,
          skill_title: "Free repository space and rerun backup",
          target: "EHR SQL nightly",
          params: proposal.params,
          risk: "low",
          approval: "policy",
          mode: "run_and_notify",
          policy,
          blast_radius: blast,
          rollback: "None needed: only restore points of retired systems are removed; Veeam keeps them in the recycle bin for 7 days.",
        },
      },
      { at: at(56), actor: "agent:remediator", kind: "run_result", body: { proposal_id: proposal.id, dry_run: true, evidence: proposal.dry_run_evidence as JsonObject } },
      { at: at(58), actor: "policy", kind: "approved", body: { proposal_id: proposal.id, reason: policy.reason } },
      { at: at(160), actor: "agent:remediator", kind: "run_result", body: { proposal_id: proposal.id, dry_run: false, evidence: live as unknown as JsonObject } },
      {
        at: at(170),
        actor: "agent:verifier",
        kind: "verified",
        body: {
          summary: "Last session of 'EHR SQL nightly' succeeded and the repository is back above its 8% floor.",
          checks: [
            { check: "job result", result: "Success" },
            { check: "FAB-REPO-01 free", result: "1459 GB (8.9%)" },
            { check: "restore point age", result: "2 min" },
          ],
        },
      },
      { at: at(172), actor: "source:veeam", kind: "alert_cleared", body: { alertname: "veeam-job-failed", target: "EHR SQL nightly" } },
      { at: at(176), actor: "platform", kind: "resolved", body: { reason: "Fixed by backup/free-space-and-rerun and verified." } },
    ];
    add(
      base({
        id: "c-fabrikam-7",
        tenant_id: "fabrikam",
        ref: "FABRIKAM-00007",
        title: "Backup job EHR SQL nightly failed",
        severity: "P2",
        status: "resolved",
        source: "veeam",
        service: "EHR",
        target: "EHR SQL nightly",
        summary: "Repository full; freed retired restore points and reran.",
        opened_at: at(0),
        updated_at: at(176),
        responded_at: at(40),
        resolved_at: at(176),
        respond_due: at(15 * 60),
        resolve_due: at(4 * 3600),
        signals: 2,
        assets: ["backup_job:EHR SQL nightly", "backup_repository:FAB-REPO-01", "database:EHR", "service:EHR"],
      }),
      entries,
      { signals, proposals: [proposal], ai_cost_usd: 0, blast_radius: blast, external_refs: [{ system: "teams", ref: "msg 19:ops-fabrikam/1729329903", created_at: at(178) }] },
    );
  }

  /* ---------------- Older and smaller cases ---------------- */
  const simple = (
    id: string,
    tenant_id: string,
    ref: string,
    title: string,
    severity: string,
    status: string,
    openedMin: number,
    o: Partial<CaseFull>,
    entries: E[],
    extra: Partial<MockCase> = {},
  ) => {
    const opened = ago(openedMin);
    add(
      base({
        id,
        tenant_id,
        ref,
        title,
        severity,
        status,
        opened_at: opened,
        updated_at: entries.length ? entries[entries.length - 1].at : opened,
        ...o,
      }),
      entries,
      extra,
    );
  };

  // Contoso POS endpoint offline: respond clock nearly out (SLA at risk).
  simple(
    "c-contoso-13",
    "contoso",
    "CONTOSO-00013",
    "Endpoint POS-114 offline in Store 14",
    "P3",
    "open",
    50,
    { service: "Store POS", target: "POS-114", source: "edge", respond_due: inMin(10), resolve_due: inMin(7 * 60 + 10), signals: 1, assets: ["endpoint:POS-114", "service:Store POS"], type: "incident" },
    [
      { at: ago(50), actor: "triage", kind: "case_opened", body: { alertname: "EndpointOffline", source: "edge", target: "POS-114", labels: { store: "Store 14", last_seen: "48 min" } } },
      { at: ago(49), actor: "agent:investigator", kind: "investigation", body: { summary: "POS-114 stopped reporting; the store's other tills are online, so the store network is fine.", likely_cause: "Device powered off or unplugged at the till.", confidence: 0.55, method: "deterministic", root_asset: "endpoint:POS-114", evidence: [{ source: "edge", finding: "Last heartbeat 48 min ago." }, { source: "microsoft", finding: "Intune: last check-in 52 min ago." }], notes: "No skill can power a till on; needs a person in the store." } },
    ],
  );
  simple(
    "c-contoso-11",
    "contoso",
    "CONTOSO-00011",
    "TLS certificate for shop.contoso.com expires in 6 days",
    "P3",
    "escalated",
    60 * 30,
    { service: "Online shop", target: "shop.contoso.com", source: "metrics", type: "request", responded_at: ago(60 * 30 - 1), respond_due: ago(60 * 29), resolve_due: inMin(60 * 50), signals: 1, assets: ["service:Online shop", "cloud_resource:contoso-shop-cdn"] },
    [
      { at: ago(60 * 30), actor: "triage", kind: "case_opened", body: { alertname: "CertificateExpiringSoon", source: "metrics", target: "shop.contoso.com", labels: { days_left: "6", issuer: "DigiCert" } } },
      { at: ago(60 * 30 - 1), actor: "agent:investigator", kind: "investigation", body: { summary: "The certificate is managed outside Azure Front Door, by the customer's own DigiCert account.", likely_cause: "Manual certificate renewal has not happened.", confidence: 0.9, method: "deterministic", evidence: [{ source: "metrics", finding: "Certificate notAfter 2026-10-25." }] } },
      { at: ago(60 * 30 - 2), actor: "platform", kind: "escalated", body: { reason: "No skill covers certificates in the customer's DigiCert account.", engineer_should_check: ["Ask Contoso's web team to renew in DigiCert", "Upload the new certificate to Front Door"] } },
    ],
  );
  simple(
    "c-contoso-10",
    "contoso",
    "CONTOSO-00010",
    "SQL agent job IndexMaintenance failed",
    "P3",
    "resolved",
    60 * 26,
    { service: "Checkout", target: "IndexMaintenance", source: "vmalert", responded_at: ago(60 * 26 - 1), resolved_at: ago(60 * 26 - 6), respond_due: ago(60 * 25), resolve_due: ago(60 * 18), signals: 1, assets: ["sql_agent_job:IndexMaintenance", "sql_instance:CTO-SHOP-SQL01\\MSSQL"] },
    [
      { at: ago(60 * 26), actor: "triage", kind: "case_opened", body: { alertname: "SqlAgentJobFailed", source: "vmalert", target: "IndexMaintenance" } },
      { at: ago(60 * 26 - 1), actor: "agent:investigator", kind: "investigation", body: { summary: "Job hit a deadlock with a long-running report.", likely_cause: "Deadlock victim on step 2 (rebuild IX_Orders_Created).", confidence: 0.8, method: "deterministic", evidence: [{ source: "edge", finding: "Error 1205 in job history." }] } },
      { at: ago(60 * 26 - 2), actor: "agent:remediator", kind: "proposal", body: { skill_id: "sql/rerun-agent-job", skill_title: "Rerun a failed SQL agent job", target: "IndexMaintenance", risk: "low", approval: "policy", mode: "run_and_notify" } },
      { at: ago(60 * 26 - 3), actor: "policy", kind: "approved", body: { reason: "run_and_notify for low-risk SQL jobs" } },
      { at: ago(60 * 26 - 5), actor: "agent:remediator", kind: "run_result", body: { dry_run: false, evidence: { before: { last_outcome: "failed" }, after: { last_outcome: "succeeded" }, outcome: "fixed", seconds: 182 } } },
      { at: ago(60 * 26 - 6), actor: "platform", kind: "resolved", body: { reason: "Fixed by sql/rerun-agent-job." } },
    ],
  );
  simple(
    "c-contoso-9",
    "contoso",
    "CONTOSO-00009",
    "Teams call quality degraded in Store 14",
    "P4",
    "resolved",
    60 * 72,
    { service: "Teams", target: "Teams", source: "microsoft", responded_at: ago(60 * 72 - 2), resolved_at: ago(60 * 70), respond_due: ago(60 * 64), resolve_due: ago(60 * 24), signals: 3, assets: ["m365_service:Teams"] },
    [
      { at: ago(60 * 72), actor: "triage", kind: "case_opened", body: { alertname: "TeamsCallQualityPoor", source: "microsoft", target: "Store 14" } },
      { at: ago(60 * 70), actor: "platform", kind: "resolved", body: { reason: "nothing to do: alert cleared on its own after the ISP's maintenance window" } },
    ],
  );
  // Fabrikam: a running case and an older escalation.
  simple(
    "c-fabrikam-8",
    "fabrikam",
    "FABRIKAM-00008",
    "Intune compliance sync stalled",
    "P3",
    "running",
    6,
    { service: "Intune", target: "Intune", source: "microsoft", responded_at: ago(5.5), respond_due: inMin(54), resolve_due: inMin(8 * 60 - 6), signals: 1, assets: ["m365_service:Intune"] },
    [
      { at: ago(6), actor: "triage", kind: "case_opened", body: { alertname: "IntuneSyncStalled", source: "microsoft", target: "Intune", labels: { devices_pending: "143" } } },
      { at: ago(5.5), actor: "agent:investigator", kind: "investigation", body: { summary: "143 devices have not reported compliance for 6 h.", likely_cause: "The Intune connector's sync job is stuck after a token refresh.", confidence: 0.7, method: "deterministic", evidence: [{ source: "microsoft", finding: "Connector last sync 6 h ago." }] } },
      { at: ago(5), actor: "agent:remediator", kind: "proposal", body: { skill_id: "m365/restart-intune-sync", skill_title: "Restart Intune device sync", target: "Intune", risk: "low", approval: "policy", mode: "run_and_notify" } },
      { at: ago(4.8), actor: "policy", kind: "approved", body: { reason: "run_and_notify for Fabrikam's M365 housekeeping" } },
    ],
  );
  simple(
    "c-fabrikam-5",
    "fabrikam",
    "FABRIKAM-00005",
    "Disk latency on FAB-PACS-DB01",
    "P2",
    "escalated",
    60 * 50,
    { service: "PACS imaging", target: "FAB-PACS-DB01", responded_at: ago(60 * 50 - 1), respond_due: ago(60 * 49 + 45), resolve_due: ago(60 * 46), signals: 4, assets: ["server:FAB-PACS-DB01", "datastore:FAB-DS-01"] },
    [
      { at: ago(60 * 50), actor: "triage", kind: "case_opened", body: { alertname: "ServerDiskLatencyHigh", source: "vmalert", target: "FAB-PACS-DB01", labels: { value: "64 ms" } } },
      { at: ago(60 * 50 - 1), actor: "agent:investigator", kind: "investigation", body: { summary: "Latency is local to the VM's virtual disk; the datastore is healthy.", likely_cause: "In-guest antivirus full scan on the PACS metadata volume.", confidence: 0.6, method: "llm", evidence: [{ source: "vcenter", finding: "FAB-DS-01 latency 2.2 ms." }, { source: "edge", finding: "MsMpEng.exe at 70% disk time." }], ai_cost_usd: 0.142 } },
      { at: ago(60 * 50 - 2), actor: "platform", kind: "escalated", body: { reason: "Changing antivirus exclusions is outside the skill library.", engineer_should_check: ["Add the PACS DB folders to Defender exclusions", "Move the scan window outside clinic hours"] } },
    ],
    { ai_cost_usd: 0.142 },
  );
  simple(
    "c-fabrikam-4",
    "fabrikam",
    "FABRIKAM-00004",
    "Patient portal 5xx errors",
    "P1",
    "resolved",
    60 * 98,
    { service: "Patient portal", target: "FAB-PORTAL-WEB01", responded_at: ago(60 * 98 - 1), resolved_at: ago(60 * 98 - 9), respond_due: ago(60 * 98 - 5), resolve_due: ago(60 * 96), signals: 6, assets: ["server:FAB-PORTAL-WEB01", "service:Patient portal"] },
    [
      { at: ago(60 * 98), actor: "triage", kind: "case_opened", body: { alertname: "ServiceErrorsHigh", source: "vmalert", target: "Patient portal", labels: { value: "11%" } } },
      { at: ago(60 * 98 - 2), actor: "agent:remediator", kind: "proposal", body: { skill_id: "linux/restart-service", skill_title: "Restart a systemd service", target: "FAB-PORTAL-WEB01", risk: "low", approval: "required", mode: "approve_to_run" } },
      { at: ago(60 * 98 - 4), actor: "engineer:Marco Ruiz", kind: "approved", body: { note: "Go — portal is down for patients." } },
      { at: ago(60 * 98 - 9), actor: "platform", kind: "resolved", body: { reason: "Fixed by linux/restart-service and verified." } },
    ],
  );

  return cases;
}

/** Give every entry an id and chain hashes per tenant, in time order. */
export function chainLedger(cases: MockCase[]): void {
  const all = cases.flatMap((c) => c.timeline.map((e) => ({ e, tenant: c.case.tenant_id, caseId: c.case.id })));
  all.sort((a, b) => Date.parse(a.e.at) - Date.parse(b.e.at));
  const prev: Record<string, string | null> = {};
  let id = 1000;
  for (const { e, tenant, caseId } of all) {
    if (!e.id) e.id = ++id;
    if (!e.hash) {
      e.prev_hash = prev[tenant] ?? null;
      e.hash = fakeHash(`${e.prev_hash}|${tenant}|${caseId}|${e.actor}|${e.kind}|${JSON.stringify(e.body)}`);
    }
    prev[tenant] = e.hash;
  }
  for (const c of cases) c.timeline.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}
