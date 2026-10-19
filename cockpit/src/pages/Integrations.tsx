import { Plug } from "lucide-react";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { humanize, isObject, show } from "../format";
import type { Integration } from "../types";
import { Empty, ErrorNote, Loading, PageHeader, Panel, Tag, Time } from "../components/ui";

const KIND_TONE: Record<string, string> = { alerts: "red", inventory: "blue", events: "violet" };
const STATE_LABEL: Record<string, string> = { ok: "Healthy", failing: "Failing", waiting: "Waiting", off: "Off" };

function Detail({ d }: { d: Integration["detail"] }) {
  if (d === null || d === undefined || d === "") return <span className="muted">—</span>;
  if (typeof d === "string") return <span className="small">{d}</span>;
  if (isObject(d)) {
    const entries = Object.entries(d);
    if (!entries.length) return <span className="muted">—</span>;
    return (
      <div className="attrs">
        {entries.slice(0, 5).map(([k, v]) => (
          <span key={k} className="attr">
            <span className="attr-k">{k}</span> {show(v)}
          </span>
        ))}
      </div>
    );
  }
  return <span className="small">{show(d)}</span>;
}

export default function Integrations() {
  const { tenants } = useApp();
  const { data, error, loading } = usePoll<Integration[]>("/api/v1/integrations", 10000);
  const groups = tenants
    .map((t) => ({ t, rows: (data ?? []).filter((r) => r.tenant_id === t.id) }))
    .filter((g) => g.rows.length);
  // Rows for tenants not in /tenants (should not happen, but never hide data).
  const known = new Set(tenants.map((t) => t.id));
  const orphans = (data ?? []).filter((r) => !known.has(r.tenant_id));
  if (orphans.length) groups.push({ t: { id: "other", name: "Other" } as never, rows: orphans });

  const failing = (data ?? []).filter((r) => r.state === "failing").length;
  return (
    <div className="page">
      <PageHeader
        title="Integrations"
        sub={
          data ? (
            <>
              {data.length} connectors · {failing ? <span className="tone-text-red">{failing} failing</span> : "all healthy"}
            </>
          ) : (
            "Connectors that feed alerts, inventory and events into each customer's estate"
          )
        }
      />
      <ErrorNote error={error} />
      {loading ? (
        <Loading />
      ) : !groups.length ? (
        <Empty icon={<Plug size={22} />} title="No connectors configured" />
      ) : (
        groups.map(({ t, rows }) => (
          <Panel key={t.id} title={t.name} icon={<Plug size={15} />} pad={false}>
            <div className="table-wrap">
              <table className="table table-fixed">
                <colgroup>
                  <col style={{ width: "18%" }} />
                  <col style={{ width: "10%" }} />
                  <col style={{ width: "10%" }} />
                  <col style={{ width: "10%" }} />
                  <col style={{ width: "10%" }} />
                  <col style={{ width: "20%" }} />
                  <col className="hide-md" style={{ width: "22%" }} />
                </colgroup>
                <thead>
                  <tr>
                    <th>Connector</th>
                    <th>Kind</th>
                    <th>State</th>
                    <th>Last run</th>
                    <th>Last OK</th>
                    <th>Last error</th>
                    <th className="hide-md">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={`${r.tenant_id}-${r.name}`} className={r.state === "failing" ? "row-failing" : ""}>
                      <td>
                        <b>{r.name}</b>
                      </td>
                      <td>
                        <Tag tone={KIND_TONE[r.kind] ?? "grey"}>{humanize(r.kind)}</Tag>
                      </td>
                      <td>
                        <span className={`state state-${r.state}`}>
                          <span className="dot" /> {STATE_LABEL[r.state] ?? humanize(r.state)}
                        </span>
                      </td>
                      <td className="nowrap muted">
                        <Time at={r.last_run_at} />
                      </td>
                      <td className="nowrap muted">
                        <Time at={r.last_ok_at} />
                      </td>
                      <td className="err-cell">{r.last_error ? <span className="tone-text-red small">{r.last_error}</span> : <span className="muted">—</span>}</td>
                      <td className="hide-md">
                        <Detail d={r.detail} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        ))
      )}
    </div>
  );
}
