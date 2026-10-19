import { useMemo, useState } from "react";
import { Bot, CircleDollarSign, Receipt } from "lucide-react";
import { qs } from "../api";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { actorLabel, compact, humanize, num, usd } from "../format";
import type { CostView } from "../types";
import { Bars, ErrorNote, Loading, PageHeader, Panel, Progress, Segmented, Select } from "../components/ui";

export default function Cost() {
  const { tenants, tenantName } = useApp();
  const [tenant, setTenant] = useState("");
  const [days, setDays] = useState<"7" | "30" | "90">("30");
  const { data, error, loading } = usePoll<CostView>(`/api/v1/cost${qs({ tenant, days })}`, 15000);

  const daily = useMemo(() => {
    if (!data) return [];
    const byDay = new Map<string, number>();
    const n = Number(days);
    for (let i = n - 1; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400_000).toISOString().slice(0, 10);
      byDay.set(d, 0);
    }
    for (const r of data.daily) {
      const d = r.day.slice(0, 10);
      if (byDay.has(d)) byDay.set(d, (byDay.get(d) ?? 0) + r.cost_usd);
    }
    return Array.from(byDay.entries()).map(([day, v]) => ({ label: day, value: v, title: `${day}: ${usd(v)}` }));
  }, [data, days]);
  const total = daily.reduce((a, d) => a + d.value, 0);
  const agents = useMemo(() => [...(data?.by_agent ?? [])].sort((a, b) => b.cost_usd - a.cost_usd), [data]);
  const totalCalls = agents.reduce((a, r) => a + r.calls, 0);
  const cacheIn = agents.reduce((a, r) => a + r.cache_read_tokens, 0);
  const allIn = agents.reduce((a, r) => a + r.input_tokens + r.cache_read_tokens, 0);

  return (
    <div className="page">
      <PageHeader
        title="AI cost"
        sub="Every model call is metered per customer and per case. Deterministic checks run first, so most cases cost cents."
        actions={
          <div className="row-gap">
            <Select value={tenant} onChange={setTenant} options={[{ value: "", label: "All customers" }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]} />
            <Segmented
              value={days}
              onChange={setDays}
              options={[
                { value: "7", label: "7d" },
                { value: "30", label: "30d" },
                { value: "90", label: "90d" },
              ]}
            />
          </div>
        }
      />
      <ErrorNote error={error} />
      {loading || !data ? (
        <Loading />
      ) : (
        <>
          <div className="cost-tenants">
            {data.tenants.map((t) => (
              <div key={t.id} className="panel cost-tenant">
                <div className="row-between">
                  <b>{t.name}</b>
                  <span className="muted small">Diagnosis: {t.diagnosis === "llm" ? "LLM" : humanize(t.diagnosis)}</span>
                </div>
                <div className="cost-big">
                  {usd(t.ai_spend_month_usd, 2)} <span className="muted">/ {usd(t.ai_budget_usd_month, 0)} this month</span>
                </div>
                <Progress value={t.ai_spend_month_usd} max={t.ai_budget_usd_month} />
                <div className="row-between small">
                  <span className="muted">{t.cases_with_ai} cases used AI</span>
                  <span>
                    <span className="muted">Per case</span> <b>{usd(t.cost_per_case_usd)}</b>
                  </span>
                </div>
              </div>
            ))}
          </div>

          <Panel
            title={`Daily spend · last ${days} days`}
            icon={<CircleDollarSign size={15} />}
            actions={
              <span className="small">
                <span className="muted">Total</span> <b>{usd(total, 2)}</b>
              </span>
            }
          >
            <Bars data={daily} height={140} format={(v) => usd(v)} />
            <div className="bars-axis muted small">
              <span>{daily[0]?.label}</span>
              <span>{daily[daily.length - 1]?.label}</span>
            </div>
          </Panel>

          <Panel
            title="By agent and model"
            icon={<Bot size={15} />}
            pad={false}
            actions={
              <span className="small muted">
                {num(totalCalls)} calls · {allIn ? Math.round((cacheIn / allIn) * 100) : 0}% of input served from cache
              </span>
            }
          >
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    {!tenant && <th>Customer</th>}
                    <th>Agent</th>
                    <th>Model</th>
                    <th className="num">Calls</th>
                    <th className="num">Tokens in</th>
                    <th className="num">Tokens out</th>
                    <th className="num">Cache read</th>
                    <th className="num">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.map((r, i) => (
                    <tr key={i}>
                      {!tenant && <td>{tenantName(r.tenant_id)}</td>}
                      <td>
                        <b>{actorLabel(r.agent.includes(":") ? r.agent : `agent:${r.agent}`)}</b>
                      </td>
                      <td>
                        <code>{r.model ?? "deterministic"}</code>
                      </td>
                      <td className="num">{num(r.calls)}</td>
                      <td className="num">{compact(r.input_tokens)}</td>
                      <td className="num">{compact(r.output_tokens)}</td>
                      <td className="num">{compact(r.cache_read_tokens)}</td>
                      <td className="num">
                        <b>{usd(r.cost_usd)}</b>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
          <p className="muted small foot-note">
            <Receipt size={12} /> Spend is metered on every model call and checked against the customer's monthly budget before the next one.
          </p>
        </>
      )}
    </div>
  );
}
