import type { Json } from "./types";

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto", style: "short" });

export function toMs(t: string | number | null | undefined): number | null {
  if (t === null || t === undefined || t === "") return null;
  if (typeof t === "number") return t < 1e12 ? t * 1000 : t; // unix seconds or ms
  const ms = Date.parse(t);
  return Number.isNaN(ms) ? null : ms;
}

export function relTime(t: string | number | null | undefined, now = Date.now()): string {
  const ms = toMs(t);
  if (ms === null) return "—";
  const s = Math.round((ms - now) / 1000);
  const a = Math.abs(s);
  if (a < 10) return "just now";
  if (a < 60) return rtf.format(s, "second");
  if (a < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (a < 86400) return rtf.format(Math.round(s / 3600), "hour");
  if (a < 86400 * 30) return rtf.format(Math.round(s / 86400), "day");
  return new Date(ms).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function absTime(t: string | number | null | undefined): string {
  const ms = toMs(t);
  if (ms === null) return "";
  return new Date(ms).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function clockTime(t: string | number | null | undefined): string {
  const ms = toMs(t);
  if (ms === null) return "";
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
}

/** "1h 04m", "12m 30s", "45s" */
export function duration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

export function minutes(m: number | null | undefined): string {
  if (m === null || m === undefined) return "—";
  if (m < 1) return `${Math.round(m * 60)} s`;
  if (m < 90) return `${m.toFixed(m < 10 ? 1 : 0)} min`;
  return `${(m / 60).toFixed(1)} h`;
}

export function usd(v: number | null | undefined, digits?: number): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const d = digits ?? (Math.abs(v) < 1 && v !== 0 ? 3 : 2);
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}

export function num(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-US");
}

export function compact(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(digits)}%`;
}

/** A 0..1 ratio as a percentage. */
export function ratio(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? "—" : pct(v * 100, digits);
}

const AGENTS: Record<string, string> = {
  investigator: "Investigator agent",
  remediator: "Remediator agent",
  verifier: "Verifier agent",
  copilot: "Copilot",
  diagnosis: "Investigator agent",
};

export function actorLabel(actor: string | null | undefined): string {
  if (!actor) return "Unknown";
  const [kind, ...rest] = actor.split(":");
  const name = rest.join(":");
  switch (kind) {
    case "agent":
      return AGENTS[name] ?? `${titleCase(name)} agent`;
    case "engineer":
      return name || "Engineer";
    case "source":
      return name ? `Source · ${name}` : "Source";
    case "triage":
      return "Triage";
    case "policy":
      return "Policy (OPA)";
    case "platform":
      return "Platform";
    default:
      return actor;
  }
}

export type ActorKind = "agent" | "engineer" | "source" | "system";
export function actorKind(actor: string | null | undefined): ActorKind {
  const k = (actor ?? "").split(":")[0];
  if (k === "agent") return "agent";
  if (k === "engineer") return "engineer";
  if (k === "source") return "source";
  return "system";
}

export function titleCase(s: string): string {
  return s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function humanize(s: string | null | undefined): string {
  if (!s) return "—";
  const t = s.replace(/[_-]+/g, " ").replace(/\b(sql|vm|ip|etl|os|llm|ai|m365)\b/gi, (w) => w.toUpperCase());
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export function statusLabel(s: string): string {
  return (
    {
      open: "Open",
      diagnosing: "Diagnosing",
      awaiting_approval: "Awaiting approval",
      running: "Running",
      resolved: "Resolved",
      escalated: "Escalated",
      merged: "Merged",
      proposed: "Proposed",
      dry_running: "Dry run",
      approved: "Approved",
      rejected: "Rejected",
      done: "Done",
      failed: "Failed",
    }[s] ?? humanize(s)
  );
}

export function modeLabel(m: string | null | undefined): string {
  return (
    { recommend: "Recommend only", approve_to_run: "Approve to run", run_and_notify: "Run & notify" }[m ?? ""] ?? humanize(m)
  );
}

/** Render a JSON scalar (or short structure) as a compact string. */
export function show(v: Json | undefined): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

export function isObject(v: unknown): v is Record<string, Json> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

export function shortHash(h: string | null | undefined): string {
  return h ? h.replace(/^sha256:/, "").slice(0, 8) : "";
}

export function assetName(id: string): string {
  const i = id.indexOf(":");
  return i >= 0 ? id.slice(i + 1) : id;
}

export function assetKind(id: string): string {
  const i = id.indexOf(":");
  return i >= 0 ? id.slice(0, i) : "";
}
