import type { ReactNode } from "react";
import {
  AlertTriangle,
  BellOff,
  BellPlus,
  CheckCircle2,
  CircleCheckBig,
  FlaskConical,
  Hand,
  Lightbulb,
  Play,
  Search,
  ShieldAlert,
  ShieldCheck,
  Siren,
  ThumbsDown,
  ThumbsUp,
  Wrench,
} from "lucide-react";
import { actorKind, actorLabel, humanize, isObject, modeLabel, ratio, show, usd } from "../format";
import type { Json, JsonObject, LedgerEntry, RunEvidence } from "../types";
import { ActorAvatar } from "./actors";
import { AssetLink, BeforeAfter, BlastRadiusView, Collapsible, JsonBlock, ParamTable, PlanSteps } from "./Evidence";
import { HashChip, ModeTag, RiskTag, Tag, Time } from "./ui";

const str = (b: JsonObject | null | undefined, k: string): string | null => {
  const v = b?.[k];
  return v === undefined || v === null || v === "" ? null : show(v);
};
const arr = (v: Json | undefined): Json[] => (Array.isArray(v) ? v : []);

const KIND_LABEL: Record<string, string> = {
  case_opened: "opened the case",
  alert_folded: "folded an alert in",
  alert_cleared: "alert cleared",
  cases_merged: "merged a related case",
  merged_into: "merged into another case",
  investigation: "investigated",
  diagnosis: "investigated",
  proposal: "proposed a fix",
  run_result: "ran the skill",
  approval_requested: "requested approval",
  approved: "approved",
  rejected: "rejected",
  escalated: "escalated",
  resolved: "resolved the case",
  verified: "verified the fix",
  copilot_question: "asked Copilot",
  copilot_answer: "answered",
};

function CardShell({
  entry,
  icon,
  tone,
  title,
  children,
  aside,
}: {
  entry: LedgerEntry;
  icon?: ReactNode;
  tone?: string;
  title?: ReactNode;
  children?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className={`tl-item tl-${tone ?? "none"}`}>
      <ActorAvatar actor={entry.actor} size={30} />
      <div className="tl-card">
        <div className="tl-head">
          <span className="tl-actor">{actorLabel(entry.actor)}</span>
          <span className="tl-kind">
            {icon}
            {title ?? KIND_LABEL[entry.kind] ?? humanize(entry.kind)}
          </span>
          <span className="tl-spacer" />
          {aside}
          <Time at={entry.at} className="tl-time" />
          <HashChip hash={entry.hash} prev={entry.prev_hash} />
        </div>
        {children && <div className="tl-body">{children}</div>}
      </div>
    </div>
  );
}

/** A thin one-line event (folded alerts, approval requests) that would clutter the conversation as a card. */
function Line({ entry, icon, children, tone }: { entry: LedgerEntry; icon: ReactNode; children: ReactNode; tone?: string }) {
  return (
    <div className={`tl-line tl-${tone ?? "none"}`}>
      <span className="tl-line-icon">{icon}</span>
      <span className="tl-line-text">
        <b>{actorLabel(entry.actor)}</b> {children}
      </span>
      <Time at={entry.at} className="tl-time" />
      <HashChip hash={entry.hash} prev={entry.prev_hash} />
    </div>
  );
}

function Labels({ labels }: { labels: Json | undefined }) {
  if (!isObject(labels)) return null;
  return (
    <div className="labels">
      {Object.entries(labels).map(([k, v]) => (
        <span key={k} className="label">
          <span className="label-k">{k}</span>
          <span className="label-v">{show(v)}</span>
        </span>
      ))}
    </div>
  );
}

function CaseOpened({ e }: { e: LedgerEntry }) {
  const b = e.body ?? {};
  const alert = str(b, "alertname") ?? str(b, "alert") ?? str(b, "title");
  return (
    <CardShell entry={e} icon={<Siren size={13} />} tone="red">
      <div className="tl-alert">
        <div className="tl-alert-name">{alert ?? "Alert"}</div>
        {str(b, "target") && <code>{str(b, "target")}</code>}
        {str(b, "source") && <Tag>{str(b, "source")}</Tag>}
        {str(b, "alert_id") && <span className="muted small mono">{str(b, "alert_id")}</span>}
      </div>
      {str(b, "summary") && <p>{str(b, "summary")}</p>}
      <Labels labels={b.labels} />
    </CardShell>
  );
}

function confidenceOf(v: Json | undefined): number | null {
  if (typeof v === "number") return v > 1 ? v / 100 : v;
  if (v === "high") return 0.9;
  if (v === "medium") return 0.6;
  if (v === "low") return 0.3;
  return null;
}

function Investigation({ e, tenant }: { e: LedgerEntry; tenant: string }) {
  const b = e.body ?? {};
  const conf = confidenceOf(b.confidence);
  const method = str(b, "method");
  const root = str(b, "root_asset");
  const evidence = arr(b.evidence);
  return (
    <CardShell
      entry={e}
      icon={<Search size={13} />}
      tone="violet"
      title="found the likely cause"
      aside={
        <>
          {method && <Tag tone={method === "llm" ? "violet" : "grey"}>{method === "llm" ? "LLM" : humanize(method)}</Tag>}
          {typeof b.ai_cost_usd === "number" && <span className="cost-chip">{usd(b.ai_cost_usd)}</span>}
        </>
      }
    >
      {str(b, "summary") && <p className="lead">{str(b, "summary")}</p>}
      {str(b, "likely_cause") && (
        <div className="cause">
          <Lightbulb size={15} />
          <div>
            <div className="cause-label">Likely cause</div>
            <div>{str(b, "likely_cause")}</div>
          </div>
          {conf !== null && (
            <div className="confidence" title={`Confidence ${show(b.confidence)}`}>
              <div className="conf-label">Confidence</div>
              <div className="conf-bar">
                <div style={{ width: `${conf * 100}%` }} />
              </div>
              <div className="conf-val">{typeof b.confidence === "string" ? humanize(b.confidence) : ratio(conf)}</div>
            </div>
          )}
        </div>
      )}
      {root && (
        <div className="root-asset">
          <span className="muted small">Root asset</span> <AssetLink tenant={tenant} id={root} />
        </div>
      )}
      {evidence.length > 0 && (
        <div className="evidence">
          <div className="section-label">Evidence</div>
          <ul>
            {evidence.map((ev, i) =>
              isObject(ev) ? (
                <li key={i}>
                  <Tag tone="blue" mono>
                    {show(ev.source)}
                  </Tag>
                  <span>{show(ev.finding)}</span>
                </li>
              ) : (
                <li key={i}>{show(ev)}</li>
              ),
            )}
          </ul>
        </div>
      )}
      {str(b, "notes") && <p className="notes">{str(b, "notes")}</p>}
    </CardShell>
  );
}

function ProposalCard({ e }: { e: LedgerEntry }) {
  const b = e.body ?? {};
  const policy = isObject(b.policy) ? b.policy : null;
  const caps = arr(policy?.caps);
  return (
    <CardShell entry={e} icon={<Wrench size={13} />} tone="blue" title="proposed a skill">
      <div className="skill-head">
        <div>
          <div className="skill-title">{str(b, "skill_title") ?? str(b, "skill_id")}</div>
          <code className="muted">{str(b, "skill_id")}</code>
        </div>
        <div className="tags">
          <RiskTag risk={str(b, "risk")} />
          {str(b, "approval") && <Tag>{str(b, "approval") === "required" ? "Approval required" : `Approval ${str(b, "approval")}`}</Tag>}
          <ModeTag mode={str(b, "mode") ?? (policy ? show(policy.mode) : null)} />
        </div>
      </div>
      <div className="grid-2">
        <div>
          <div className="section-label">Target</div>
          <code>{str(b, "target") ?? arr(b.targets).map(show).join(", ")}</code>
          <div className="section-label">Parameters</div>
          <ParamTable params={isObject(b.params) ? b.params : null} />
        </div>
        <BlastRadiusView br={isObject(b.blast_radius) ? b.blast_radius : null} />
      </div>
      {policy && (
        <div className="policy">
          <ShieldCheck size={14} />
          <div>
            <b>Policy: {modeLabel(show(policy.mode))}.</b> {str(policy, "reason")}
            {caps.length > 0 && (
              <div className="caps">
                {caps.map((c) => (
                  <Tag key={show(c)} tone="grey">
                    {show(c)}
                  </Tag>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {str(b, "rollback") && (
        <div className="rollback">
          <span className="section-label inline">Rollback</span> {str(b, "rollback")}
        </div>
      )}
    </CardShell>
  );
}

function RunResult({ e }: { e: LedgerEntry }) {
  const b = e.body ?? {};
  const dry = b.dry_run === true;
  const ev = (isObject(b.evidence) ? b.evidence : b) as RunEvidence & JsonObject;
  const d = ev.decision;
  const outcome = ev.outcome ?? str(b, "outcome");
  const failed = !!ev.error || outcome === "failed";
  const actions = arr(ev.actions as Json);
  const log = arr(ev.log as Json);
  return (
    <CardShell
      entry={e}
      icon={dry ? <FlaskConical size={13} /> : <Play size={13} />}
      tone={dry ? "cyan" : failed ? "red" : "green"}
      title={dry ? "dry run — exact plan" : "ran the skill live"}
      aside={
        <>
          {dry ? <Tag tone="cyan">Dry run</Tag> : <Tag tone={failed ? "red" : "green"}>{outcome ? humanize(outcome) : "Live"}</Tag>}
          {typeof ev.seconds === "number" && <span className="muted small">{ev.seconds.toFixed(1)} s</span>}
        </>
      }
    >
      {d && (
        <div className="decision">
          {d.action && <Tag tone={d.action === "no_action" ? "grey" : "blue"}>{humanize(d.action)}</Tag>}
          {d.reason && <span>{d.reason}</span>}
        </div>
      )}
      {dry && d?.plan && (
        <>
          <div className="section-label">Plan</div>
          <PlanSteps plan={d.plan} />
        </>
      )}
      {!dry && (ev.before !== undefined || ev.after !== undefined) && <BeforeAfter before={ev.before} after={ev.after} />}
      {!dry && actions.length > 0 && (
        <>
          <div className="section-label">Actions taken</div>
          <PlanSteps plan={actions} />
        </>
      )}
      {ev.error && (
        <div className="error-note">
          <AlertTriangle size={14} /> {ev.error}
        </div>
      )}
      {log.length > 0 && (
        <Collapsible label={`Run log (${log.length} lines)`}>
          <pre className="log">{log.map(show).join("\n")}</pre>
        </Collapsible>
      )}
    </CardShell>
  );
}

function Bubble({ e, mine }: { e: LedgerEntry; mine: boolean }) {
  const b = e.body ?? {};
  const text = str(b, mine ? "question" : "answer") ?? "";
  const tools = arr(b.tools);
  return (
    <div className={`bubble-row ${mine ? "mine" : ""}`}>
      {!mine && <ActorAvatar actor={e.actor} size={28} />}
      <div className={`bubble ${mine ? "q" : "a"}`}>
        <div className="bubble-head">
          <b>{actorLabel(e.actor)}</b>
          <Time at={e.at} className="tl-time" />
          <HashChip hash={e.hash} prev={e.prev_hash} />
        </div>
        <div className="bubble-text">{text}</div>
        {!mine && (str(b, "model") || tools.length > 0 || typeof b.ai_cost_usd === "number") && (
          <div className="bubble-meta">
            {str(b, "method") && <span>{str(b, "method")}</span>}
            {str(b, "model") && <span className="mono">{str(b, "model")}</span>}
            {tools.length > 0 && <span>tools: {tools.map(show).join(", ")}</span>}
            {typeof b.ai_cost_usd === "number" && <span>{usd(b.ai_cost_usd)}</span>}
          </div>
        )}
      </div>
      {mine && <ActorAvatar actor={e.actor} size={28} />}
    </div>
  );
}

export function TimelineEntry({ e, tenant }: { e: LedgerEntry; tenant: string }) {
  const b = e.body ?? {};
  switch (e.kind) {
    case "case_opened":
      return <CaseOpened e={e} />;
    case "alert_folded":
      return (
        <Line entry={e} icon={<BellPlus size={13} />}>
          folded in <b>{str(b, "alertname") ?? "an alert"}</b> {str(b, "target") && <code>{str(b, "target")}</code>}
          {str(b, "alert_id") && <span className="muted mono small"> {str(b, "alert_id")}</span>}
        </Line>
      );
    case "alert_cleared":
      return (
        <Line entry={e} icon={<BellOff size={13} />} tone="green">
          cleared {str(b, "alertname") ?? "alert"} {str(b, "target") && <code>{str(b, "target")}</code>}
        </Line>
      );
    case "cases_merged":
      return (
        <Line entry={e} icon={<BellPlus size={13} />}>
          merged <b>{str(b, "merged") ?? "a related case"}</b> into this case, linked through{" "}
          <code>{str(b, "via") ?? "a shared asset"}</code>
        </Line>
      );
    case "merged_into":
      return (
        <Line entry={e} icon={<BellPlus size={13} />}>
          merged this case into <b>{str(b, "into") ?? "a related case"}</b>, linked through{" "}
          <code>{str(b, "via") ?? "a shared asset"}</code>
        </Line>
      );
    case "investigation":
    case "diagnosis":
      return <Investigation e={e} tenant={tenant} />;
    case "proposal":
      return <ProposalCard e={e} />;
    case "run_result":
      return <RunResult e={e} />;
    case "approval_requested":
      return (
        <Line entry={e} icon={<Hand size={13} />} tone="amber">
          is waiting for an engineer to approve{str(b, "reason") ? ` — ${str(b, "reason")}` : ""}
        </Line>
      );
    case "approved":
    case "rejected": {
      const ok = e.kind === "approved";
      const byPolicy = actorKind(e.actor) !== "engineer";
      return (
        <CardShell
          entry={e}
          icon={ok ? <ThumbsUp size={13} /> : <ThumbsDown size={13} />}
          tone={ok ? "green" : "red"}
          title={ok ? (byPolicy ? "approved automatically" : "approved the run") : "rejected the run"}
        >
          {str(b, "note") ? <p className="quote">“{str(b, "note")}”</p> : byPolicy && str(b, "reason") ? <p>{str(b, "reason")}</p> : null}
        </CardShell>
      );
    }
    case "escalated": {
      const check = b.engineer_should_check;
      return (
        <CardShell entry={e} icon={<ShieldAlert size={13} />} tone="red" title="escalated to an engineer">
          <p className="lead">{str(b, "reason")}</p>
          {check !== undefined && check !== null && (
            <div className="check">
              <div className="section-label">Engineer should check</div>
              {Array.isArray(check) ? (
                <ul>
                  {check.map((c, i) => (
                    <li key={i}>{show(c)}</li>
                  ))}
                </ul>
              ) : (
                <p>{show(check)}</p>
              )}
            </div>
          )}
        </CardShell>
      );
    }
    case "resolved":
      return (
        <CardShell entry={e} icon={<CircleCheckBig size={13} />} tone="green" title="resolved the case">
          {str(b, "reason") && <p>{str(b, "reason")}</p>}
        </CardShell>
      );
    case "verified":
      return (
        <CardShell entry={e} icon={<CheckCircle2 size={13} />} tone="green" title="verified the fix">
          {str(b, "summary") ?? str(b, "reason") ? <p>{str(b, "summary") ?? str(b, "reason")}</p> : null}
          {arr(b.checks).length > 0 && (
            <ul className="checks">
              {arr(b.checks).map((c, i) => (
                <li key={i}>
                  <CheckCircle2 size={13} /> {isObject(c) ? `${show(c.check ?? c.name)}: ${show(c.result ?? c.value)}` : show(c)}
                </li>
              ))}
            </ul>
          )}
        </CardShell>
      );
    case "copilot_question":
      return <Bubble e={e} mine />;
    case "copilot_answer":
      return <Bubble e={e} mine={false} />;
    default:
      return (
        <CardShell entry={e} title={humanize(e.kind)}>
          <JsonBlock value={e.body} />
        </CardShell>
      );
  }
}

/** Runs of consecutive folded alerts collapse into one expandable group. */
export function Timeline({ entries, tenant }: { entries: LedgerEntry[]; tenant: string }) {
  const out: ReactNode[] = [];
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (e.kind === "alert_folded") {
      let j = i;
      while (j + 1 < entries.length && entries[j + 1].kind === "alert_folded") j++;
      const run = entries.slice(i, j + 1);
      if (run.length > 2) {
        const targets = Array.from(new Set(run.map((r) => str(r.body, "target")).filter(Boolean)));
        out.push(
          <div key={`fold-${e.id}`} className="tl-fold">
            <Collapsible
              label={
                <span className="fold-label">
                  <BellPlus size={13} />
                  <span>
                    <b>{run.length} more alerts</b> folded into this case by {actorLabel(e.actor)}
                    <span className="muted">
                      {" · "}
                      {targets.slice(0, 4).join(", ")}
                      {targets.length > 4 ? ` +${targets.length - 4} more` : ""}
                    </span>
                  </span>
                </span>
              }
            >
              {run.map((r) => (
                <TimelineEntry key={r.id} e={r} tenant={tenant} />
              ))}
            </Collapsible>
          </div>,
        );
        i = j;
        continue;
      }
    }
    out.push(<TimelineEntry key={e.id} e={e} tenant={tenant} />);
  }
  return <div className="timeline">{out}</div>;
}
