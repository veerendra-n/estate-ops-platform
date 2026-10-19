// Shapes returned by the Estate Ops API (see the cockpit contract in the README).
// Fields the backend may omit are optional; free-form blobs are `Json`.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export type Severity = "P1" | "P2" | "P3" | "P4" | string;
export type CaseStatus =
  | "open"
  | "diagnosing"
  | "awaiting_approval"
  | "running"
  | "resolved"
  | "escalated"
  | string;
export type ProposalStatus =
  | "proposed"
  | "dry_running"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "running"
  | "done"
  | "failed"
  | string;
export type Mode = "recommend" | "approve_to_run" | "run_and_notify" | string;

export interface Me {
  sso: boolean;
  user: { name: string; email: string } | null;
  signed_in: boolean;
}

export interface Health {
  ok: boolean;
  version: string;
  skills: number;
  model: string | null;
}

export interface TenantOverview {
  id: string;
  name: string;
  industry: string;
  open: number;
  awaiting_approval: number;
  escalated_7d: number;
  resolved_7d: number;
  resolved_by_agents_7d: number;
  opened_24h: number;
  median_resolve_minutes_7d: number | null;
  sla_at_risk: number;
  sla_breached_30d: number;
  sla_met_pct_30d: { respond?: number | null; resolve?: number | null } | number | null;
  ai_spend_month_usd: number;
  ai_budget_usd_month: number;
  assets: number;
  autonomy_default: Mode;
}

export interface FeedItem {
  id: number | string;
  at: string;
  tenant_id: string;
  actor: string;
  kind: string;
  body: JsonObject | null;
  case_id: string | null;
  ref: string | null;
  title: string | null;
}

export interface Overview {
  tenants: TenantOverview[];
  totals: {
    open: number;
    awaiting_approval: number;
    resolved_7d: number;
    resolved_by_agents_7d: number;
    opened_24h: number;
    sla_at_risk: number;
  };
  feed: FeedItem[];
}

export interface CaseRow {
  id: string;
  tenant_id: string;
  tenant_name: string;
  ref: string;
  type: string;
  title: string;
  severity: Severity;
  status: CaseStatus;
  source: string | null;
  service: string | null;
  target: string | null;
  summary: string | null;
  opened_at: string;
  updated_at: string;
  responded_at: string | null;
  resolved_at: string | null;
  respond_due: string | null;
  resolve_due: string | null;
  signals: number;
}

export interface CaseFull extends CaseRow {
  assets: string[];
  parent_id: string | null;
  requester: string | null;
}

export interface Signal {
  alert_id: string;
  source: string;
  target: string;
  asset_id: string | null;
  labels: Record<string, string> | null;
  received_at: string;
}

export interface Decision {
  action?: string;
  reason?: string;
  plan?: Json[];
}

export interface RunEvidence {
  decision?: Decision;
  before?: Json;
  actions?: Json[];
  after?: Json;
  outcome?: string;
  error?: string | null;
  seconds?: number;
  log?: Json[];
}

export interface Policy {
  mode?: Mode;
  reason?: string;
  caps?: string[];
}

export interface BlastRadius {
  services: string[];
  critical: boolean;
  users: number;
  dependents: number;
}

export interface Proposal {
  id: string;
  skill_id: string;
  skill_version: string;
  targets: string[];
  params: JsonObject;
  risk: string;
  mode: Mode;
  policy: Policy | null;
  status: ProposalStatus;
  dry_run_evidence: RunEvidence | null;
  evidence: RunEvidence | null;
  outcome: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface LedgerEntry {
  id: number | string;
  at: string;
  actor: string;
  kind: string;
  body: JsonObject | null;
  hash: string;
  prev_hash: string | null;
}

export interface CaseDetail {
  case: CaseFull;
  signals: Signal[];
  proposals: Proposal[];
  timeline: LedgerEntry[];
  external_refs: Json[];
  ai_cost_usd: number;
  blast_radius: BlastRadius | null;
}

export interface AskAnswer {
  answer: string;
  method: string;
  model: string | null;
  tools: string[];
  ai_cost_usd: number;
}

export interface Asset {
  id: string;
  kind: string;
  name: string;
  attrs: JsonObject;
  sources: string[];
  seen_at: string | null;
}

export interface EstateChange {
  id: number | string;
  at: string;
  source: string;
  asset_id: string;
  asset_name: string;
  change: "added" | "removed" | "changed" | string;
  before: JsonObject | null;
  after: JsonObject | null;
}

export interface EstateView {
  stats: { total: number; kinds: Record<string, number>; sources: Record<string, number> };
  assets: Asset[];
  changes: EstateChange[];
}

export interface Relation {
  src: string;
  rel: string;
  dst: string;
}

export interface AssetView {
  asset: Asset;
  graph: { centre: string; assets: Asset[]; relations: Relation[] };
  cases: { id: string; ref: string; title: string; severity: Severity; status: CaseStatus; opened_at: string }[];
}

export interface SkillParam {
  name: string;
  type: string;
  default: Json;
  min: number | null;
  max: number | null;
  choices: Json[] | null;
  required: boolean;
  description: string;
}

export interface SkillTenant {
  tenant_id: string;
  tenant_name: string;
  mode: Mode;
  why: string;
  proposals: number;
  fixed: number;
  no_action: number;
  failed: number;
  rejected: number;
  success_rate: number | null;
  last_proposed: string | null;
}

export interface Skill {
  id: string;
  version: string;
  title: string;
  summary: string;
  domain: string;
  risk: string;
  approval: string;
  target_kind: string;
  connectors: string[];
  max_targets: number;
  params: SkillParam[];
  triggers: string[];
  rollback: string | null;
  tenants: SkillTenant[];
}

export interface Tenant {
  id: string;
  name: string;
  industry: string;
  diagnosis: string;
  autonomy: Mode | JsonObject;
  ai_spend_month_usd: number;
  ai_budget_usd_month: number;
  ledger_ok: boolean;
  ledger_broken_at: number | string | null;
}

export interface Integration {
  tenant_id: string;
  tenant_name: string;
  name: string;
  kind: string;
  state: "ok" | "failing" | "waiting" | "off" | string;
  last_run_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  detail: string | JsonObject | null;
}

export interface CostView {
  days: number;
  tenants: {
    id: string;
    name: string;
    diagnosis: string;
    ai_spend_month_usd: number;
    ai_budget_usd_month: number;
    cases_with_ai: number;
    cost_per_case_usd: number | null;
  }[];
  by_agent: {
    tenant_id: string;
    agent: string;
    model: string | null;
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cost_usd: number;
  }[];
  daily: { tenant_id: string; day: string; cost_usd: number }[];
}

export interface Fault {
  id: string;
  tenant: string;
  title: string;
  what: string;
  active: boolean;
}

export interface LabEvent {
  at: number;
  tenant: string;
  what: string;
  [key: string]: Json;
}
