import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Activity, AlertOctagon, Bot, CircleDollarSign, Clock, Hand, Inbox, Layers } from "lucide-react";
import { useApp } from "../context";
import { usePoll, useStored } from "../hooks";
import { qs } from "../api";
import { actorLabel, humanize, usd } from "../format";
import { entrySummary } from "../summaries";
import type { CaseRow, Overview } from "../types";
import { ActorAvatar } from "../components/actors";
import {
  Empty,
  ErrorNote,
  Kpi,
  Loading,
  PageHeader,
  Panel,
  Progress,
  SearchBox,
  Segmented,
  Select,
  SeverityChip,
  SlaClock,
  StatusPill,
  Time,
} from "../components/ui";

type StatusFilter = "open" | "awaiting_approval" | "running" | "resolved" | "escalated" | "all";
const CLOSED = new Set(["resolved", "escalated", "merged"]);

function matches(c: CaseRow, f: StatusFilter): boolean {
  if (f === "all") return true;
  if (f === "open") return !CLOSED.has(c.status);
  if (f === "running") return c.status === "running" || c.status === "diagnosing";
  return c.status === f;
}

function useDebounced<T>(v: T, ms = 250): T {
  const [d, setD] = useState(v);
  useEffect(() => {
    const t = window.setTimeout(() => setD(v), ms);
    return () => window.clearTimeout(t);
  }, [v, ms]);
  return d;
}

export default function Queue() {
  const { tenants } = useApp();
  const nav = useNavigate();
  const [tenant, setTenant] = useStored<string>("cockpit.queue.tenant", "");
  const [status, setStatus] = useStored<StatusFilter>("cockpit.queue.status", "open");
  const [q, setQ] = useState("");
  const dq = useDebounced(q.trim());

  const ov = usePoll<Overview>(`/api/v1/overview${qs({ tenant })}`);
  const cases = usePoll<CaseRow[]>(`/api/v1/cases${qs({ tenant, q: dq })}`);

  const rows = useMemo(() => {
    const all = cases.data ?? [];
    const filtered = all.filter((c) => matches(c, status));
    // Cases waiting on a person float to the top of the open views.
    if (status === "open" || status === "all")
      filtered.sort((a, b) => Number(b.status === "awaiting_approval") - Number(a.status === "awaiting_approval"));
    return filtered;
  }, [cases.data, status]);

  const counts = useMemo(() => {
    const all = cases.data ?? [];
    const n = (f: StatusFilter) => all.filter((c) => matches(c, f)).length;
    return { open: n("open"), awaiting_approval: n("awaiting_approval"), running: n("running"), resolved: n("resolved"), escalated: n("escalated"), all: all.length };
  }, [cases.data]);

  const t = ov.data?.totals;
  const tenantsOv = ov.data?.tenants ?? [];
  const spend = tenantsOv.reduce((a, x) => a + (x.ai_spend_month_usd || 0), 0);
  const budget = tenantsOv.reduce((a, x) => a + (x.ai_budget_usd_month || 0), 0);
  const slaRisk = t?.sla_at_risk ?? tenantsOv.reduce((a, x) => a + (x.sla_at_risk || 0), 0);
  const agentShare = t && t.resolved_7d ? Math.round((t.resolved_by_agents_7d / t.resolved_7d) * 100) : null;

  return (
    <div className="page">
      <PageHeader
        title="Queue"
        sub={tenant ? `Cases for ${tenants.find((x) => x.id === tenant)?.name ?? tenant}` : "Every case across your customers, newest activity first"}
        actions={
          <Select
            value={tenant}
            onChange={setTenant}
            label="Customer"
            options={[{ value: "", label: "All customers" }, ...tenants.map((x) => ({ value: x.id, label: x.name }))]}
          />
        }
      />

      <div className="kpis">
        <Kpi label="Open cases" icon={<Inbox size={14} />} value={t?.open ?? "—"} sub={t ? `${t.opened_24h} opened in 24 h` : undefined} />
        <Kpi
          label="Awaiting approval"
          icon={<Hand size={14} />}
          value={t?.awaiting_approval ?? "—"}
          tone={t && t.awaiting_approval > 0 ? "amber" : undefined}
          sub={t && t.awaiting_approval > 0 ? "An engineer needs to decide" : "Nothing waiting on you"}
        />
        <Kpi
          label="Resolved by agents · 7d"
          icon={<Bot size={14} />}
          value={
            t ? (
              <>
                {t.resolved_by_agents_7d}
                <span className="kpi-of"> / {t.resolved_7d}</span>
              </>
            ) : (
              "—"
            )
          }
          sub={agentShare !== null ? `${agentShare}% with no engineer in the loop` : "No resolutions yet"}
          tone="green"
        />
        <Kpi
          label="SLA at risk"
          icon={<Clock size={14} />}
          value={slaRisk}
          tone={slaRisk > 0 ? "red" : undefined}
          sub={slaRisk > 0 ? "Under 25% of the clock left" : "All clocks healthy"}
        />
        <Kpi label="AI spend this month" icon={<CircleDollarSign size={14} />} value={usd(spend, 2)} sub={`of ${usd(budget, 0)} budget`}>
          <Progress value={spend} max={budget} />
        </Kpi>
      </div>

      <div className="queue-grid">
        <Panel
          className="cases-panel"
          pad={false}
          title={
            <>
              Cases <span className="count">{rows.length}</span>
            </>
          }
          icon={<Layers size={15} />}
          actions={<SearchBox value={q} onChange={setQ} placeholder="Search ref, title, target" />}
        >
          <div className="toolbar">
            <Segmented
              value={status}
              onChange={setStatus}
              options={[
                { value: "open", label: "Open", count: counts.open },
                { value: "awaiting_approval", label: "Awaiting approval", count: counts.awaiting_approval },
                { value: "running", label: "Running", count: counts.running },
                { value: "resolved", label: "Resolved", count: counts.resolved },
                { value: "escalated", label: "Escalated", count: counts.escalated },
                { value: "all", label: "All", count: counts.all },
              ]}
            />
          </div>
          <ErrorNote error={cases.error} />
          {cases.loading ? (
            <Loading label="Loading cases" />
          ) : rows.length === 0 ? (
            <Empty icon={<Inbox size={22} />} title="No cases here">
              {status === "open" ? "Quiet estate. Agents will open a case when alerts arrive." : "Try another filter."}
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table cases-table">
                <thead>
                  <tr>
                    <th className="col-sev">Sev</th>
                    <th>Case</th>
                    <th className="hide-xl">Customer</th>
                    <th className="hide-lg">Type</th>
                    <th>Status</th>
                    <th className="hide-md">Service</th>
                    <th className="num" title="Alerts folded into this case">Alerts</th>
                    <th>SLA</th>
                    <th className="right">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => {
                    const responding = !c.responded_at && !c.resolved_at;
                    return (
                      <tr
                        key={c.id}
                        className={`clickable ${c.status === "awaiting_approval" ? "row-awaiting" : ""}`}
                        onClick={() => nav(`/cases/${c.id}`)}
                      >
                        <td className="col-sev">
                          <SeverityChip severity={c.severity} />
                        </td>
                        <td className="col-case">
                          <div className="case-sub">
                            <Link to={`/cases/${c.id}`} className="case-ref" onClick={(e) => e.stopPropagation()}>
                              {c.ref}
                            </Link>
                            <span className="case-cust"> · {c.tenant_name}</span>
                          </div>
                          <div className="case-title">{c.title}</div>
                        </td>
                        <td className="hide-xl nowrap">{c.tenant_name}</td>
                        <td className="hide-lg muted">{humanize(c.type)}</td>
                        <td>
                          <StatusPill status={c.status} />
                        </td>
                        <td className="hide-md">{c.service ?? <span className="muted">—</span>}</td>
                        <td className="num">
                          <span className={`alerts-count ${c.signals > 5 ? "many" : ""}`}>{c.signals}</span>
                        </td>
                        <td className="nowrap">
                          {responding ? (
                            <SlaClock compact label="Respond" start={c.opened_at} due={c.respond_due} done={c.responded_at} />
                          ) : (
                            <SlaClock compact label="Resolve" start={c.opened_at} due={c.resolve_due} done={c.resolved_at} />
                          )}
                        </td>
                        <td className="right muted nowrap">
                          <Time at={c.updated_at} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel className="feed-panel" title="Live activity" icon={<Activity size={15} />} actions={<span className="live-dot" title="Refreshing every 3 s" />}>
          <ErrorNote error={ov.error} />
          {!ov.data ? (
            <Loading />
          ) : ov.data.feed.length === 0 ? (
            <Empty icon={<AlertOctagon size={20} />} title="No activity yet" />
          ) : (
            <ol className="feed">
              {ov.data.feed.slice(0, 25).map((f) => (
                <li key={f.id} className={`feed-item kind-${f.kind}`}>
                  <ActorAvatar actor={f.actor} size={24} />
                  <div className="feed-body">
                    <div className="feed-line">
                      <span className="feed-actor">{actorLabel(f.actor)}</span>
                      {f.case_id && (
                        <Link to={`/cases/${f.case_id}`} className="feed-ref">
                          {f.ref}
                        </Link>
                      )}
                    </div>
                    <div className="feed-text">{entrySummary(f.kind, f.body)}</div>
                    <Time at={f.at} className="feed-time" />
                  </div>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </div>
    </div>
  );
}
