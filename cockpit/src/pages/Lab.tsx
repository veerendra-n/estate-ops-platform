import { useState } from "react";
import { Link } from "react-router-dom";
import { FlaskConical, Lightbulb, Loader2, RotateCcw, Syringe, Terminal } from "lucide-react";
import { post } from "../api";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { clockTime, show } from "../format";
import type { Fault, LabEvent } from "../types";
import { Empty, ErrorNote, Loading, PageHeader, Panel, Tag, Time } from "../components/ui";

export default function Lab() {
  const { tenantName } = useApp();
  const faults = usePoll<Fault[]>("/api/v1/lab/faults");
  const events = usePoll<LabEvent[]>("/api/v1/lab/events");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<Error | undefined>();

  const act = async (key: string, path: string) => {
    setBusy(key);
    setErr(undefined);
    try {
      await post(path);
      faults.reload();
      events.reload();
    } catch (e) {
      setErr(e as Error);
    } finally {
      setBusy(null);
    }
  };

  const evs = [...(events.data ?? [])].sort((a, b) => b.at - a.at);

  return (
    <div className="page">
      <PageHeader
        title="Lab"
        sub="Demo control panel for the simulated customer estates"
        actions={
          <button className="btn" disabled={!!busy} onClick={() => void act("reset", "/api/v1/lab/reset")}>
            {busy === "reset" ? <Loader2 className="spin" size={15} /> : <RotateCcw size={15} />} Reset simulated estates
          </button>
        }
      />
      <div className="guide">
        <Lightbulb size={16} />
        <div>
          Inject <b>Checkout slows down</b>, then watch the <Link to="/">Queue</Link>: about 20 alerts fold into one case, the investigator finds the
          noisy ETL VM, and the rebalance skill waits for your approval.
        </div>
      </div>
      <ErrorNote error={err ?? faults.error} />
      <div className="lab-grid">
        <Panel title="Faults" icon={<FlaskConical size={15} />} pad={false}>
          {faults.loading ? (
            <Loading />
          ) : !faults.data?.length ? (
            <Empty title="The simulator offers no faults" />
          ) : (
            <ul className="faults">
              {faults.data.map((f) => (
                <li key={f.id} className={f.active ? "active" : ""}>
                  <div className="fault-main">
                    <div className="row-gap">
                      <b>{f.title}</b>
                      <Tag>{tenantName(f.tenant)}</Tag>
                      {f.active && <Tag tone="red">Active</Tag>}
                    </div>
                    <div className="muted small">{f.what}</div>
                  </div>
                  <button className={`btn ${f.active ? "" : "primary"}`} disabled={!!busy || f.active} onClick={() => void act(f.id, `/api/v1/lab/faults/${encodeURIComponent(f.id)}`)}>
                    {busy === f.id ? <Loader2 className="spin" size={15} /> : <Syringe size={15} />} {f.active ? "Injected" : "Inject"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="Simulator events" icon={<Terminal size={15} />} pad={false} actions={<span className="live-dot" title="Refreshing every 3 s" />}>
          <ErrorNote error={events.error} />
          {!evs.length ? (
            <Empty title="No events yet" />
          ) : (
            <ol className="sim-events">
              {evs.slice(0, 200).map((e, i) => {
                const { at, tenant, what, ...rest } = e;
                const extra = Object.entries(rest).filter(([, v]) => v !== null && v !== undefined && v !== "");
                return (
                  <li key={`${at}-${i}`}>
                    <span className="mono muted" title={new Date(at * 1000).toLocaleString()}>
                      {clockTime(at)}
                    </span>
                    <Tag>{tenantName(tenant)}</Tag>
                    <span className="sim-what">{what}</span>
                    <span className="sim-extra">
                      {extra.map(([k, v]) => (
                        <span key={k} className="attr">
                          <span className="attr-k">{k}</span> {show(v)}
                        </span>
                      ))}
                    </span>
                    <Time at={at} className="muted small sim-ago" />
                  </li>
                );
              })}
            </ol>
          )}
        </Panel>
      </div>
    </div>
  );
}
