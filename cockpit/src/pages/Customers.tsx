import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Building2, CheckCircle2, Link2Off } from "lucide-react";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { humanize, isObject, minutes, num, pct, usd } from "../format";
import type { Overview, Tenant, TenantOverview } from "../types";
import { ErrorNote, Loading, ModeTag, PageHeader, Progress, Tag } from "../components/ui";

function slaPct(v: TenantOverview["sla_met_pct_30d"], which: "respond" | "resolve"): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return v;
  return v[which] ?? null;
}

function autonomyDefault(t: Tenant | undefined, o: TenantOverview): string {
  if (o.autonomy_default) return o.autonomy_default;
  const a = t?.autonomy;
  if (typeof a === "string") return a;
  if (isObject(a) && typeof a.default === "string") return a.default;
  return "approve_to_run";
}

function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className={`cstat ${tone ? `tone-text-${tone}` : ""}`}>
      <div className="cstat-v">{value}</div>
      <div className="cstat-k">{label}</div>
    </div>
  );
}

export default function Customers() {
  const { tenants } = useApp();
  const ov = usePoll<Overview>("/api/v1/overview");
  const tl = usePoll<Tenant[]>("/api/v1/tenants", 30000);
  const byId = new Map((tl.data ?? tenants).map((t) => [t.id, t]));

  return (
    <div className="page">
      <PageHeader title="Customers" sub="How each customer's estate is running and what the agents are doing for them" />
      <ErrorNote error={ov.error} />
      {!ov.data ? (
        <Loading />
      ) : (
        <div className="customer-grid">
          {ov.data.tenants.map((o) => {
            const t = byId.get(o.id);
            const respond = slaPct(o.sla_met_pct_30d, "respond");
            const resolve = slaPct(o.sla_met_pct_30d, "resolve");
            const auto = autonomyDefault(t, o);
            return (
              <article key={o.id} className="panel customer-card">
                <header className="cust-head">
                  <div className="cust-logo">{o.name.slice(0, 1)}</div>
                  <div className="grow">
                    <h2>{o.name}</h2>
                    <div className="muted small">{o.industry || t?.industry}</div>
                  </div>
                  {t &&
                    (t.ledger_ok ? (
                      <Tag tone="green" title="Every ledger entry's hash chains to the one before">
                        <CheckCircle2 size={12} /> Ledger intact
                      </Tag>
                    ) : (
                      <Tag tone="red" title="The hash chain does not verify from this entry on">
                        <Link2Off size={12} /> Ledger broken at #{t.ledger_broken_at}
                      </Tag>
                    ))}
                </header>
                <div className="cstats">
                  <Stat label="Open cases" value={<Link to="/">{o.open}</Link>} />
                  <Stat label="Awaiting approval" value={o.awaiting_approval} tone={o.awaiting_approval ? "amber" : undefined} />
                  <Stat
                    label="Resolved 7d · by agents"
                    value={
                      <>
                        {o.resolved_7d} <span className="muted">· {o.resolved_by_agents_7d}</span>
                      </>
                    }
                  />
                  <Stat label="Median resolve" value={minutes(o.median_resolve_minutes_7d)} />
                </div>
                <div className="cust-section">
                  <div className="section-label">SLA · last 30 days</div>
                  <div className="sla-row">
                    <div>
                      <span className="muted small">Respond met</span> <b>{pct(respond)}</b>
                    </div>
                    <div>
                      <span className="muted small">Resolve met</span> <b>{pct(resolve)}</b>
                    </div>
                    <div className={o.sla_breached_30d ? "tone-text-red" : ""}>
                      <span className="muted small">Breaches</span> <b>{o.sla_breached_30d ?? 0}</b>
                    </div>
                    <div className={o.sla_at_risk ? "tone-text-amber" : ""}>
                      <span className="muted small">At risk now</span> <b>{o.sla_at_risk ?? 0}</b>
                    </div>
                  </div>
                </div>
                <div className="cust-section">
                  <div className="spend-line">
                    <span className="section-label inline">AI spend this month</span>
                    <span>
                      <b>{usd(o.ai_spend_month_usd, 2)}</b> <span className="muted">/ {usd(o.ai_budget_usd_month, 0)}</span>
                    </span>
                  </div>
                  <Progress value={o.ai_spend_month_usd} max={o.ai_budget_usd_month} />
                </div>
                <footer className="cust-foot">
                  <Link to={`/estate/${o.id}`} className="cust-link">
                    <Building2 size={13} /> {num(o.assets)} estate assets
                  </Link>
                  <span className="row-gap small">
                    <span className="muted">Autonomy default</span>
                    <ModeTag mode={auto} />
                  </span>
                  {t?.diagnosis && (
                    <span className="small muted" title="How cases are investigated for this customer">
                      Diagnosis: {t.diagnosis === "llm" ? "LLM" : humanize(t.diagnosis)}
                    </span>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
