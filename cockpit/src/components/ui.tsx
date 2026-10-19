import { createContext, useContext, type ReactNode } from "react";
import { AlertTriangle, Loader2, Search, X } from "lucide-react";
import { useNow } from "../hooks";
import { absTime, duration, relTime, shortHash, statusLabel, toMs } from "../format";

/* ---- one shared clock for every relative time and SLA countdown ---- */

const NowContext = createContext<number>(Date.now());
export function NowProvider({ children }: { children: ReactNode }) {
  const now = useNow(1000);
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>;
}
export const useClock = () => useContext(NowContext);

export function Time({ at, className }: { at: string | number | null | undefined; className?: string }) {
  const now = useClock();
  if (at === null || at === undefined) return <span className={`muted ${className ?? ""}`}>—</span>;
  return (
    <time className={className} title={absTime(at)} dateTime={String(at)}>
      {relTime(at, now)}
    </time>
  );
}

/* ---- chips and pills ---- */

export function SeverityChip({ severity }: { severity: string }) {
  const s = (severity || "P4").toUpperCase();
  return <span className={`sev sev-${s.toLowerCase()}`}>{s}</span>;
}

const STATUS_TONE: Record<string, string> = {
  open: "blue",
  diagnosing: "violet",
  awaiting_approval: "amber",
  running: "cyan",
  resolved: "green",
  escalated: "red",
  merged: "grey",
  proposed: "blue",
  dry_running: "violet",
  approved: "green",
  rejected: "red",
  done: "green",
  failed: "red",
  ok: "green",
  failing: "red",
  waiting: "amber",
  off: "grey",
};

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const tone = STATUS_TONE[status] ?? "grey";
  return (
    <span className={`pill tone-${tone}`}>
      <span className={`dot ${status === "running" || status === "diagnosing" || status === "dry_running" ? "pulse" : ""}`} />
      {label ?? statusLabel(status)}
    </span>
  );
}

export function Tag({ children, tone = "grey", title, mono }: { children: ReactNode; tone?: string; title?: string; mono?: boolean }) {
  return (
    <span className={`tag tone-${tone} ${mono ? "mono" : ""}`} title={title}>
      {children}
    </span>
  );
}

export function HashChip({ hash, prev }: { hash: string | null | undefined; prev?: string | null }) {
  if (!hash) return null;
  return (
    <span className="hash" title={`hash ${hash}${prev ? `\nprev ${prev}` : ""}`}>
      #{shortHash(hash)}
    </span>
  );
}

const RISK_TONE: Record<string, string> = { low: "green", medium: "amber", high: "red", critical: "red" };
export function RiskTag({ risk }: { risk: string | null | undefined }) {
  if (!risk) return null;
  return <Tag tone={RISK_TONE[risk] ?? "grey"}>{risk} risk</Tag>;
}

const MODE_TONE: Record<string, string> = { recommend: "grey", approve_to_run: "amber", run_and_notify: "green" };
export function ModeTag({ mode }: { mode: string | null | undefined }) {
  if (!mode) return null;
  const label = { recommend: "Recommend", approve_to_run: "Approve to run", run_and_notify: "Run & notify" }[mode] ?? mode;
  return <Tag tone={MODE_TONE[mode] ?? "grey"}>{label}</Tag>;
}

/* ---- SLA clock ---- */

export function SlaClock({
  label,
  start,
  due,
  done,
  compact,
}: {
  label?: string;
  start: string | null | undefined;
  due: string | null | undefined;
  done: string | null | undefined;
  compact?: boolean;
}) {
  const now = useClock();
  const dueMs = toMs(due);
  if (dueMs === null) return <span className="muted">—</span>;
  const doneMs = toMs(done);
  const startMs = toMs(start) ?? dueMs - 3600_000;
  const total = Math.max(1, dueMs - startMs);
  let tone: string;
  let text: string;
  let frac: number;
  if (doneMs !== null) {
    const met = doneMs <= dueMs;
    tone = met ? "met" : "breached";
    text = met ? "Met" : `Missed by ${duration(doneMs - dueMs)}`;
    frac = met ? Math.max(0, (dueMs - doneMs) / total) : 0;
  } else {
    const left = dueMs - now;
    frac = Math.max(0, Math.min(1, left / total));
    if (left < 0) {
      tone = "breached";
      text = `Breached ${duration(-left)}`;
    } else {
      tone = frac < 0.25 ? "risk" : "ok";
      text = duration(left);
    }
  }
  const title = `${label ?? "SLA"} due ${absTime(due)}${doneMs !== null ? `\nstopped ${absTime(done)}` : ""}`;
  return (
    <span className={`sla sla-${tone} ${compact ? "sla-compact" : ""}`} title={title}>
      <svg viewBox="0 0 20 20" className="sla-ring" aria-hidden>
        <circle cx="10" cy="10" r="8" className="sla-track" />
        <circle
          cx="10"
          cy="10"
          r="8"
          className="sla-fill"
          strokeDasharray={`${(frac * 50.27).toFixed(2)} 50.27`}
          transform="rotate(-90 10 10)"
        />
      </svg>
      {label && <span className="sla-label">{label}</span>}
      <span className="sla-text">{text}</span>
    </span>
  );
}

/* ---- layout bits ---- */

export function Panel({
  title,
  icon,
  actions,
  children,
  className,
  pad = true,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  pad?: boolean;
}) {
  return (
    <section className={`panel ${className ?? ""}`}>
      {(title || actions) && (
        <header className="panel-head">
          <h3>
            {icon}
            {title}
          </h3>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className={pad ? "panel-body" : ""}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, sub, actions }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <p className="muted">{sub}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Kpi({
  label,
  value,
  sub,
  tone,
  icon,
  children,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: string;
  icon?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`kpi ${tone ? `kpi-${tone}` : ""}`}>
      <div className="kpi-label">
        {icon}
        {label}
      </div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
      {children}
    </div>
  );
}

export function Progress({ value, max, warnAt = 0.8 }: { value: number; max: number; warnAt?: number }) {
  const f = max > 0 ? value / max : 0;
  const tone = f >= 1 ? "red" : f >= warnAt ? "amber" : "accent";
  return (
    <div className="progress" title={`${Math.round(f * 100)}%`}>
      <div className={`progress-bar tone-${tone}`} style={{ width: `${Math.min(100, f * 100)}%` }} />
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <div className="empty-title">{title}</div>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading">
      <Loader2 className="spin" size={16} /> {label}…
    </div>
  );
}

export function ErrorNote({ error }: { error: Error | undefined }) {
  if (!error) return null;
  return (
    <div className="error-note">
      <AlertTriangle size={14} /> {error.message}
    </div>
  );
}

export function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <label className="search">
      <Search size={14} />
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder ?? "Search"} />
      {value && (
        <button className="icon-btn ghost" onClick={() => onChange("")} aria-label="Clear search">
          <X size={12} />
        </button>
      )}
    </label>
  );
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  label,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  label?: string;
}) {
  return (
    <label className="select">
      {label && <span className="select-label">{label}</span>}
      <select value={value} onChange={(e) => onChange(e.target.value as T)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; count?: number }[];
}) {
  return (
    <div className="segmented" role="tablist">
      {options.map((o) => (
        <button key={o.value} role="tab" aria-selected={o.value === value} className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
          {o.count !== undefined && <span className="seg-count">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ---- tiny charts ---- */

export function Sparkline({ values, width = 120, height = 32 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return <svg width={width} height={height} />;
  const max = Math.max(...values, 0.0001);
  const step = width / (values.length - 1);
  const pts = values.map((v, i) => [i * step, height - 2 - (v / max) * (height - 4)] as const);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="spark">
      <path d={area} className="spark-area" />
      <path d={line} className="spark-line" />
    </svg>
  );
}

export function Bars({
  data,
  height = 120,
  format,
}: {
  data: { label: string; value: number; title?: string }[];
  height?: number;
  format?: (v: number) => string;
}) {
  const max = Math.max(...data.map((d) => d.value), 0.0001);
  return (
    <div className="bars" style={{ height }}>
      {data.map((d) => (
        <div key={d.label} className="bar-col" title={d.title ?? `${d.label}: ${format ? format(d.value) : d.value}`}>
          <div className="bar" style={{ height: `${Math.max(d.value > 0 ? 2 : 0, (d.value / max) * 100)}%` }} />
        </div>
      ))}
    </div>
  );
}

export function KeyValues({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v], i) => (
        <div key={i} className="kv-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
