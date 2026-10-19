import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ChevronDown, ChevronRight, Users, Zap } from "lucide-react";
import { humanize, isObject, show } from "../format";
import type { BlastRadius, Json, JsonObject } from "../types";
import { Tag } from "./ui";

/** A plan step may be a string or an object such as {action, vm, from, to, detail}. */
export function PlanSteps({ plan }: { plan: Json[] | undefined | null }) {
  if (!plan || plan.length === 0) return <div className="muted small">No steps — nothing to change.</div>;
  return (
    <ol className="plan">
      {plan.map((step, i) => {
        if (!isObject(step)) {
          return (
            <li key={i}>
              <span className="plan-n">{i + 1}</span>
              <div className="plan-main">{show(step)}</div>
            </li>
          );
        }
        const mainKey = ["step", "action", "do", "title", "what"].find((k) => typeof step[k] === "string");
        const main = mainKey ? String(step[mainKey]) : null;
        const detail = typeof step.detail === "string" ? step.detail : null;
        const rest = Object.entries(step).filter(([k]) => k !== mainKey && k !== "detail");
        const hasMove = "from" in step && "to" in step;
        return (
          <li key={i}>
            <span className="plan-n">{i + 1}</span>
            <div className="plan-main">
              {main && <div className="plan-title">{humanize(main)}</div>}
              {detail && <div className="plan-detail">{detail}</div>}
              {hasMove && (
                <div className="plan-move">
                  <code>{show(step.from)}</code>
                  <ArrowRight size={13} />
                  <code>{show(step.to)}</code>
                </div>
              )}
              <div className="plan-attrs">
                {rest
                  .filter(([k]) => !(hasMove && (k === "from" || k === "to")))
                  .map(([k, v]) => (
                    <span key={k} className="attr">
                      <span className="attr-k">{k}</span> {show(v)}
                    </span>
                  ))}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function flat(v: Json | undefined): Record<string, string> {
  if (!isObject(v)) return v === undefined || v === null ? {} : { value: show(v) };
  const out: Record<string, string> = {};
  for (const [k, x] of Object.entries(v)) out[k] = show(x);
  return out;
}

export function BeforeAfter({ before, after }: { before: Json | undefined; after: Json | undefined }) {
  const b = flat(before);
  const a = flat(after);
  const keys = Array.from(new Set([...Object.keys(b), ...Object.keys(a)]));
  if (keys.length === 0) return null;
  return (
    <div className="before-after">
      <div className="ba-col">
        <div className="ba-head">Before</div>
        {keys.map((k) => (
          <div key={k} className={`ba-row ${b[k] !== a[k] ? "changed" : ""}`}>
            <span className="ba-k">{k}</span>
            <span className="ba-v">{b[k] ?? "—"}</span>
          </div>
        ))}
      </div>
      <div className="ba-arrow">
        <ArrowRight size={16} />
      </div>
      <div className="ba-col after">
        <div className="ba-head">After</div>
        {keys.map((k) => (
          <div key={k} className={`ba-row ${b[k] !== a[k] ? "changed" : ""}`}>
            <span className="ba-k">{k}</span>
            <span className="ba-v">{a[k] ?? "—"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function BlastRadiusView({ br }: { br: BlastRadius | JsonObject | null | undefined }) {
  if (!br) return null;
  const services = (Array.isArray(br.services) ? br.services : []) as Json[];
  const critical = br.critical === true;
  const users = typeof br.users === "number" ? br.users : null;
  const dependents = typeof br.dependents === "number" ? br.dependents : null;
  return (
    <div className={`blast ${critical ? "critical" : ""}`}>
      <div className="blast-head">
        <Zap size={13} /> Blast radius
        {critical && <Tag tone="red">Business critical</Tag>}
      </div>
      <div className="blast-services">
        {services.length ? services.map((s) => <Tag key={show(s)}>{show(s)}</Tag>) : <span className="muted small">No services affected</span>}
      </div>
      <div className="blast-stats">
        {users !== null && (
          <span>
            <Users size={12} /> {users.toLocaleString()} users
          </span>
        )}
        {dependents !== null && <span>{dependents} dependent assets</span>}
      </div>
    </div>
  );
}

export function AssetLink({ tenant, id, children }: { tenant: string; id: string; children?: ReactNode }) {
  return (
    <Link to={`/estate/${tenant}/assets/${encodeURIComponent(id)}`} className="asset-link">
      {children ?? id}
    </Link>
  );
}

export function Collapsible({ label, children, defaultOpen = false }: { label: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`collapsible ${open ? "open" : ""}`}>
      <button className="collapse-btn" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} {label}
      </button>
      {open && <div className="collapse-body">{children}</div>}
    </div>
  );
}

export function JsonBlock({ value }: { value: Json | undefined }) {
  return <pre className="json">{JSON.stringify(value ?? null, null, 2)}</pre>;
}

export function ParamTable({ params }: { params: JsonObject | null | undefined }) {
  if (!params || Object.keys(params).length === 0) return <span className="muted small">No parameters</span>;
  return (
    <div className="params">
      {Object.entries(params).map(([k, v]) => (
        <span key={k} className="attr">
          <span className="attr-k">{k}</span> {show(v)}
        </span>
      ))}
    </div>
  );
}
