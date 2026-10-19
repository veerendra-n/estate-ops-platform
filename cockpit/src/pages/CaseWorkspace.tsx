import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Bell,
  Boxes,
  CircleDollarSign,
  ExternalLink,
  Hand,
  Loader2,
  Send,
  ShieldCheck,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import { ApiError, post } from "../api";
import { usePoll } from "../hooks";
import { assetKind, assetName, humanize, isObject, modeLabel, show, usd } from "../format";
import type { AskAnswer, CaseDetail, LedgerEntry, Proposal } from "../types";
import { Timeline } from "../components/Timeline";
import { AssetLink, BlastRadiusView, PlanSteps } from "../components/Evidence";
import { ActorAvatar } from "../components/actors";
import { Empty, ErrorNote, Loading, ModeTag, Panel, RiskTag, SeverityChip, SlaClock, StatusPill, Tag, Time } from "../components/ui";

function ApprovalBox({ p, detail, onDone }: { p: Proposal; detail: CaseDetail; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const proposalEntry = [...detail.timeline].reverse().find((e) => e.kind === "proposal" && (e.body?.proposal_id === p.id || e.body?.skill_id === p.skill_id));
  const title = (proposalEntry?.body?.skill_title as string | undefined) ?? p.skill_id;
  const br = detail.blast_radius ?? (isObject(proposalEntry?.body?.blast_radius) ? proposalEntry?.body?.blast_radius : null);
  const decision = p.dry_run_evidence?.decision;

  const decide = async (what: "approve" | "reject") => {
    setBusy(what);
    setError(null);
    try {
      await post(`/api/v1/proposals/${p.id}/${what}`, { note, who: "" });
      setNote("");
      onDone();
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? `Refused: ${e.detail}` : (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="approval">
      <div className="approval-head">
        <Hand size={16} />
        <div>
          <div className="approval-title">Your approval is needed</div>
          <div className="muted small">Policy allows this skill to run only after an engineer approves.</div>
        </div>
      </div>
      <div className="approval-skill">
        <div className="skill-title">{title}</div>
        <div className="tags">
          <code className="muted">
            {p.skill_id}@{p.skill_version}
          </code>
          <RiskTag risk={p.risk} />
          <ModeTag mode={p.mode} />
        </div>
        <div className="small">
          Target{p.targets.length > 1 ? "s" : ""}: {p.targets.map((t) => <code key={t}>{t}</code>)}
        </div>
      </div>
      <div className="section-label">Dry-run plan</div>
      {decision?.reason && <p className="small muted">{decision.reason}</p>}
      <PlanSteps plan={decision?.plan} />
      <BlastRadiusView br={br} />
      {p.policy?.reason && (
        <div className="policy small">
          <ShieldCheck size={14} />
          <div>
            <b>{modeLabel(p.policy.mode ?? p.mode)}:</b> {p.policy.reason}
          </div>
        </div>
      )}
      <textarea className="note" placeholder="Optional note for the ledger" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
      {error && <div className="error-note">{error}</div>}
      <div className="approval-actions">
        <button className="btn danger-ghost" disabled={!!busy} onClick={() => void decide("reject")}>
          {busy === "reject" ? <Loader2 className="spin" size={15} /> : <ThumbsDown size={15} />} Reject
        </button>
        <button className="btn primary grow" disabled={!!busy} onClick={() => void decide("approve")}>
          {busy === "approve" ? <Loader2 className="spin" size={15} /> : <ThumbsUp size={15} />} Approve &amp; run
        </button>
      </div>
    </section>
  );
}

interface Pending {
  key: number;
  question: string;
  seen: number; // copilot_question entries with this text when asked
  answer?: AskAnswer;
  error?: string;
}

function Composer({ onAsk }: { onAsk: (question: string) => Promise<void> }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const question = q.trim();
    if (!question || busy) return;
    setBusy(true);
    setQ("");
    await onAsk(question);
    setBusy(false);
  };
  return (
    <form className="composer" onSubmit={submit}>
      <Sparkles size={16} className="composer-icon" />
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Copilot about this case — e.g. is it safe to do this now?" disabled={busy} />
      <button className="btn primary" disabled={busy || !q.trim()}>
        {busy ? <Loader2 className="spin" size={15} /> : <Send size={15} />} Ask
      </button>
    </form>
  );
}

export default function CaseWorkspace() {
  const { id = "" } = useParams();
  const { data, error, loading, reload } = usePoll<CaseDetail>(`/api/v1/cases/${encodeURIComponent(id)}`);
  const [pending, setPending] = useState<Pending[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const lastLen = useRef(0);

  const timeline = data?.timeline ?? [];
  const countQ = (entries: LedgerEntry[], text: string) => entries.filter((e) => e.kind === "copilot_question" && e.body?.question === text).length;

  // Drop a locally shown Q&A once the ledger has caught up with it.
  const visiblePending = pending.filter((p) => !(p.answer && countQ(timeline, p.question) > p.seen));

  useEffect(() => {
    const len = timeline.length + visiblePending.length;
    if (len > lastLen.current && lastLen.current !== 0) endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    lastLen.current = len;
  }, [timeline.length, visiblePending.length]);

  useEffect(() => setPending([]), [id]);

  const ask = async (question: string) => {
    const key = Date.now();
    const p: Pending = { key, question, seen: countQ(timeline, question) };
    setPending((x) => [...x, p]);
    try {
      const answer = await post<AskAnswer>(`/api/v1/cases/${encodeURIComponent(id)}/ask`, { question });
      setPending((x) => x.map((y) => (y.key === key ? { ...y, answer } : y)));
      reload();
    } catch (e) {
      const msg =
        e instanceof ApiError && e.status === 429
          ? `Copilot is rate limited: ${e.detail || "try again in a minute"}.`
          : (e as Error).message;
      setPending((x) => x.map((y) => (y.key === key ? { ...y, error: msg } : y)));
    }
  };

  const awaiting = useMemo(() => data?.proposals.filter((p) => p.status === "awaiting_approval") ?? [], [data]);

  if (loading) return <div className="page"><Loading label="Loading case" /></div>;
  if (!data)
    return (
      <div className="page">
        <Link to="/" className="back">
          <ArrowLeft size={14} /> Queue
        </Link>
        <ErrorNote error={error ?? new Error("Case not found")} />
      </div>
    );

  const c = data.case;
  const latestProposal = data.proposals[data.proposals.length - 1];

  return (
    <div className="page case-page">
      <Link to="/" className="back">
        <ArrowLeft size={14} /> Queue
      </Link>
      <header className={`case-head ${c.status === "awaiting_approval" ? "awaiting" : ""}`}>
        <div className="case-head-main">
          <div className="case-head-top">
            <SeverityChip severity={c.severity} />
            <span className="case-head-ref">{c.ref}</span>
            <StatusPill status={c.status} />
            <Tag>{humanize(c.type)}</Tag>
          </div>
          <h1>{c.title}</h1>
          <div className="case-meta">
            <span>
              <span className="muted">Customer</span> {c.tenant_name}
            </span>
            {c.service && (
              <span>
                <span className="muted">Service</span> {c.service}
              </span>
            )}
            {c.target && (
              <span>
                <span className="muted">Target</span> <code>{c.target}</code>
              </span>
            )}
            <span>
              <span className="muted">Opened</span> <Time at={c.opened_at} />
            </span>
            {c.requester && (
              <span>
                <span className="muted">Requester</span> {c.requester}
              </span>
            )}
          </div>
        </div>
        <div className="case-head-side">
          <div className="clock-box">
            <SlaClock label="Respond" start={c.opened_at} due={c.respond_due} done={c.responded_at} />
            <SlaClock label="Resolve" start={c.opened_at} due={c.resolve_due} done={c.resolved_at} />
          </div>
          <div className="cost-box" title="AI spend on this case">
            <CircleDollarSign size={14} />
            <span className="muted">AI cost</span>
            <b>{usd(data.ai_cost_usd)}</b>
          </div>
        </div>
      </header>
      <ErrorNote error={error} />

      <div className="case-grid">
        <div className="case-main">
          <Timeline entries={timeline} tenant={c.tenant_id} />
          {visiblePending.map((p) => (
            <div key={p.key} className="pending-qa">
              <div className="bubble-row mine">
                <div className="bubble q">
                  <div className="bubble-head">
                    <b>You</b>
                  </div>
                  <div className="bubble-text">{p.question}</div>
                </div>
              </div>
              <div className="bubble-row">
                <ActorAvatar actor="agent:copilot" size={28} />
                <div className={`bubble a ${p.error ? "err" : ""}`}>
                  <div className="bubble-head">
                    <b>Copilot</b>
                  </div>
                  {p.answer ? (
                    <>
                      <div className="bubble-text">{p.answer.answer}</div>
                      <div className="bubble-meta">
                        <span>{p.answer.method}</span>
                        {p.answer.model && <span className="mono">{p.answer.model}</span>}
                        {p.answer.tools.length > 0 && <span>tools: {p.answer.tools.join(", ")}</span>}
                        <span>{usd(p.answer.ai_cost_usd)}</span>
                      </div>
                    </>
                  ) : p.error ? (
                    <div className="bubble-text">{p.error}</div>
                  ) : (
                    <div className="typing">
                      <span />
                      <span />
                      <span />
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
          <div ref={endRef} />
          <Composer onAsk={ask} />
        </div>

        <aside className="case-side">
          {awaiting.map((p) => (
            <ApprovalBox key={p.id} p={p} detail={data} onDone={reload} />
          ))}
          {awaiting.length === 0 && latestProposal && (
            <Panel title="Remediation" icon={<ShieldCheck size={15} />}>
              <div className="skill-title">{latestProposal.skill_id}</div>
              <div className="tags">
                <StatusPill status={latestProposal.status} />
                <ModeTag mode={latestProposal.mode} />
                <RiskTag risk={latestProposal.risk} />
              </div>
              {latestProposal.decided_by && (
                <p className="small muted">
                  Decided by {latestProposal.decided_by.replace(/^engineer:/, "")} <Time at={latestProposal.decided_at} />
                </p>
              )}
              {latestProposal.outcome && (
                <p className="small">
                  Outcome: <b>{humanize(latestProposal.outcome)}</b>
                </p>
              )}
            </Panel>
          )}

          <Panel
            title={
              <>
                Alerts <span className="count">{data.signals.length}</span>
              </>
            }
            icon={<Bell size={15} />}
            pad={false}
          >
            {data.signals.length === 0 ? (
              <Empty title="No alerts" />
            ) : (
              <ul className="signals">
                {data.signals.map((s, i) => (
                  <li key={`${s.alert_id}-${i}`}>
                    <div className="sig-top">
                      <span className="sig-name">{s.labels?.alertname ?? s.alert_id}</span>
                      <Time at={s.received_at} className="muted small" />
                    </div>
                    <div className="sig-sub">
                      {s.asset_id ? (
                        <AssetLink tenant={c.tenant_id} id={s.asset_id}>
                          {s.target}
                        </AssetLink>
                      ) : (
                        <code>{s.target}</code>
                      )}
                      <span className="muted small">{s.source}</span>
                      {s.labels?.value && <span className="sig-val">{s.labels.value}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel
            title={
              <>
                Affected assets <span className="count">{c.assets?.length ?? 0}</span>
              </>
            }
            icon={<Boxes size={15} />}
            pad={false}
          >
            {!c.assets?.length ? (
              <Empty title="No assets linked yet" />
            ) : (
              <ul className="assets-list">
                {c.assets.map((a) => (
                  <li key={a}>
                    <AssetLink tenant={c.tenant_id} id={a}>
                      <span className="asset-name">{assetName(a)}</span>
                    </AssetLink>
                    <span className="kind-tag">{humanize(assetKind(a))}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          {data.external_refs.length > 0 && (
            <Panel title="External" icon={<ExternalLink size={15} />}>
              <ul className="ext-refs">
                {data.external_refs.map((r, i) =>
                  isObject(r) ? (
                    <li key={i}>
                      <Tag>{show(r.system)}</Tag> <code>{show(r.ref)}</code>
                    </li>
                  ) : (
                    <li key={i}>{show(r)}</li>
                  ),
                )}
              </ul>
            </Panel>
          )}
        </aside>
      </div>
    </div>
  );
}
