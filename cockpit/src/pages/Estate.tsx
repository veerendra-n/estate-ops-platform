import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { ArrowRight, Database, GitCommitHorizontal, History, Network, Radio } from "lucide-react";
import { qs } from "../api";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { humanize, isObject, show } from "../format";
import type { EstateView, JsonObject } from "../types";
import { Empty, ErrorNote, Loading, PageHeader, Panel, SearchBox, Segmented, Select, Tag, Time } from "../components/ui";

export function EstateIndex() {
  const { tenants } = useApp();
  if (!tenants.length) return <div className="page"><Loading /></div>;
  return <Navigate to={`/estate/${tenants[0].id}`} replace />;
}

const KEY_ATTRS = ["ip", "os", "vcpu", "memory_gb", "host", "cluster", "datastore", "capacity_gb", "free_gb", "latency_ms", "critical", "power_state", "version", "location", "model", "owner"];

function keyAttrs(attrs: JsonObject): [string, string][] {
  const keys = Object.keys(attrs);
  const pick = [...KEY_ATTRS.filter((k) => k in attrs), ...keys.filter((k) => !KEY_ATTRS.includes(k))].slice(0, 4);
  return pick.map((k) => [k, show(attrs[k])]);
}

function useDebounced<T>(v: T, ms = 250): T {
  const [d, setD] = useState(v);
  useEffect(() => {
    const t = window.setTimeout(() => setD(v), ms);
    return () => window.clearTimeout(t);
  }, [v, ms]);
  return d;
}

function Diff({ before, after }: { before: JsonObject | null; after: JsonObject | null }) {
  const b = isObject(before) ? before : {};
  const a = isObject(after) ? after : {};
  const keys = Array.from(new Set([...Object.keys(b), ...Object.keys(a)])).filter((k) => show(b[k]) !== show(a[k]));
  if (!keys.length) return <span className="muted">—</span>;
  return (
    <div className="diff">
      {keys.map((k) => (
        <div key={k} className="diff-row">
          <span className="attr-k">{k}</span>
          {k in b && <span className="diff-old">{show(b[k])}</span>}
          {k in b && k in a && <ArrowRight size={11} className="muted" />}
          {k in a && <span className="diff-new">{show(a[k])}</span>}
        </div>
      ))}
    </div>
  );
}

export default function Estate() {
  const { tenant = "" } = useParams();
  const { tenants } = useApp();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const dq = useDebounced(q.trim());
  const { data, error, loading } = usePoll<EstateView>(`/api/v1/estate/${tenant}${qs({ q: dq, kind })}`, 15000);
  // Stats for the whole estate, independent of the table filter.
  const all = usePoll<EstateView>(`/api/v1/estate/${tenant}`, 30000);
  const stats = all.data?.stats ?? data?.stats;

  useEffect(() => {
    setKind("");
    setQ("");
  }, [tenant]);

  const kinds = useMemo(() => Object.entries(stats?.kinds ?? {}).sort((a, b) => b[1] - a[1]), [stats]);
  const sources = useMemo(() => Object.entries(stats?.sources ?? {}).sort((a, b) => b[1] - a[1]), [stats]);
  const maxKind = Math.max(1, ...kinds.map((k) => k[1]));

  return (
    <div className="page">
      <PageHeader
        title="Estate"
        sub="Every asset the connectors discovered, merged into one graph per customer"
        actions={
          <Segmented value={tenant} onChange={(t) => nav(`/estate/${t}`)} options={tenants.map((t) => ({ value: t.id, label: t.name }))} />
        }
      />
      <ErrorNote error={error} />
      <div className="estate-stats">
        <div className="panel stat-total">
          <div className="kpi-label">
            <Network size={14} /> Assets
          </div>
          <div className="kpi-value">{stats?.total ?? "—"}</div>
          <div className="kpi-sub">{kinds.length} kinds · {sources.length} sources</div>
        </div>
        <Panel title="By kind" icon={<Database size={15} />} className="stat-kinds">
          <div className="kind-bars">
            {kinds.map(([k, n]) => (
              <button key={k} className={`kind-bar ${kind === k ? "on" : ""}`} onClick={() => setKind(kind === k ? "" : k)} title={`Filter: ${k}`}>
                <span className="kind-bar-label">{humanize(k)}</span>
                <span className="kind-bar-track">
                  <span style={{ width: `${(n / maxKind) * 100}%` }} />
                </span>
                <span className="kind-bar-n">{n}</span>
              </button>
            ))}
          </div>
        </Panel>
        <Panel title="By source" icon={<Radio size={15} />} className="stat-sources">
          <div className="source-list">
            {sources.map(([s, n]) => (
              <div key={s} className="source-row">
                <Tag tone="blue" mono>
                  {s}
                </Tag>
                <span className="num">{n}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel
        pad={false}
        title={
          <>
            Assets <span className="count">{data?.assets.length ?? 0}</span>
          </>
        }
        icon={<Database size={15} />}
        actions={
          <div className="row-gap">
            <Select value={kind} onChange={setKind} options={[{ value: "", label: "All kinds" }, ...kinds.map(([k]) => ({ value: k, label: humanize(k) }))]} />
            <SearchBox value={q} onChange={setQ} placeholder="Search assets" />
          </div>
        }
      >
        {loading ? (
          <Loading label="Loading estate" />
        ) : !data?.assets.length ? (
          <Empty icon={<Database size={22} />} title="No assets match" />
        ) : (
          <div className="table-wrap tall">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Kind</th>
                  <th>Sources</th>
                  <th className="hide-md">Key attributes</th>
                  <th className="right">Seen</th>
                </tr>
              </thead>
              <tbody>
                {data.assets.map((a) => (
                  <tr key={a.id} className="clickable" onClick={() => nav(`/estate/${tenant}/assets/${encodeURIComponent(a.id)}`)}>
                    <td>
                      <Link to={`/estate/${tenant}/assets/${encodeURIComponent(a.id)}`} className="strong-link" onClick={(e) => e.stopPropagation()}>
                        {a.name}
                      </Link>
                      {a.attrs.critical === true && <Tag tone="red">critical</Tag>}
                    </td>
                    <td>
                      <span className="kind-tag">{humanize(a.kind)}</span>
                    </td>
                    <td>
                      <div className="tags">
                        {a.sources.map((s) => (
                          <Tag key={s} mono>
                            {s}
                          </Tag>
                        ))}
                      </div>
                    </td>
                    <td className="hide-md">
                      <div className="attrs">
                        {keyAttrs(a.attrs).map(([k, v]) => (
                          <span key={k} className="attr">
                            <span className="attr-k">{k}</span> {v}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="right muted nowrap">
                      <Time at={a.seen_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel pad={false} title="Recent changes" icon={<History size={15} />}>
        {!data?.changes.length ? (
          <Empty icon={<GitCommitHorizontal size={20} />} title="No changes recorded" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Source</th>
                  <th>Asset</th>
                  <th>Change</th>
                  <th>Before → after</th>
                </tr>
              </thead>
              <tbody>
                {data.changes.map((ch) => (
                  <tr key={ch.id}>
                    <td className="muted nowrap">
                      <Time at={ch.at} />
                    </td>
                    <td>
                      <Tag mono>{ch.source}</Tag>
                    </td>
                    <td>
                      <Link to={`/estate/${tenant}/assets/${encodeURIComponent(ch.asset_id)}`} className="strong-link">
                        {ch.asset_name}
                      </Link>
                    </td>
                    <td>
                      <Tag tone={ch.change === "added" ? "green" : ch.change === "removed" ? "red" : "amber"}>{ch.change}</Tag>
                    </td>
                    <td>
                      <Diff before={ch.before} after={ch.after} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
