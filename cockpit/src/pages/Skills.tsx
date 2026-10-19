import { useState } from "react";
import { BookOpen, Undo2, Wrench, Zap } from "lucide-react";
import { qs } from "../api";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { humanize, ratio, show } from "../format";
import type { Skill } from "../types";
import { Empty, ErrorNote, Loading, ModeTag, PageHeader, RiskTag, SearchBox, Select, Tag, Time } from "../components/ui";

function SkillCard({ s }: { s: Skill }) {
  return (
    <article className="panel skill-card">
      <header className="skill-card-head">
        <div className="skill-icon">
          <Wrench size={16} />
        </div>
        <div className="grow">
          <div className="skill-title">{s.title}</div>
          <code className="muted small">
            {s.id} · v{s.version}
          </code>
        </div>
        <div className="tags">
          <Tag>{humanize(s.domain)}</Tag>
          <RiskTag risk={s.risk} />
        </div>
      </header>
      {s.summary && <p className="skill-summary">{s.summary}</p>}
      <div className="skill-facts">
        <div>
          <span className="fact-k">Approval</span>
          <span>{humanize(s.approval)}</span>
        </div>
        <div>
          <span className="fact-k">Target</span>
          <span>
            {humanize(s.target_kind)}
            {s.max_targets > 1 ? ` · up to ${s.max_targets}` : ""}
          </span>
        </div>
        <div>
          <span className="fact-k">Connectors</span>
          <span>{s.connectors.join(", ") || "—"}</span>
        </div>
      </div>
      {s.triggers.length > 0 && (
        <div className="skill-row">
          <span className="fact-k">
            <Zap size={12} /> Triggers
          </span>
          <div className="tags">
            {s.triggers.map((t) => (
              <Tag key={t} tone="violet" mono>
                {t}
              </Tag>
            ))}
          </div>
        </div>
      )}
      {s.params.length > 0 && (
        <div className="skill-row">
          <span className="fact-k">Params</span>
          <div className="param-list">
            {s.params.map((p) => (
              <div key={p.name} className="param" title={p.description}>
                <code>{p.name}</code>
                <span className="muted">{p.type}</span>
                {p.default !== null && p.default !== undefined && <span className="muted">= {show(p.default)}</span>}
                {(p.min !== null || p.max !== null) && (
                  <span className="muted">
                    [{p.min ?? ""}–{p.max ?? ""}]
                  </span>
                )}
                {p.choices && <span className="muted">{p.choices.map(show).join(" | ")}</span>}
                {p.required && <Tag tone="amber">required</Tag>}
              </div>
            ))}
          </div>
        </div>
      )}
      {s.rollback && (
        <div className="skill-row">
          <span className="fact-k">
            <Undo2 size={12} /> Rollback
          </span>
          <span className="small">{s.rollback}</span>
        </div>
      )}
      <div className="skill-tenants">
        {s.tenants.map((t) => {
          const rate = t.success_rate;
          return (
            <div key={t.tenant_id} className="skill-tenant">
              <div className="st-head">
                <b>{t.tenant_name}</b>
                <ModeTag mode={t.mode} />
              </div>
              <div className="st-why muted small">{t.why}</div>
              <div className="st-stats">
                <span>
                  <b>{t.proposals}</b> runs
                </span>
                <span className="ok">
                  <b>{t.fixed}</b> fixed
                </span>
                <span className={t.failed ? "bad" : ""}>
                  <b>{t.failed}</b> failed
                </span>
                <span className="st-rate" title="Success rate">
                  <span className="rate-bar">
                    <span style={{ width: `${(rate ?? 0) * 100}%` }} />
                  </span>
                  {ratio(rate)}
                </span>
              </div>
              {t.last_proposed && (
                <div className="muted small">
                  Last proposed <Time at={t.last_proposed} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </article>
  );
}

export default function Skills() {
  const { tenants } = useApp();
  const [tenant, setTenant] = useState("");
  const [q, setQ] = useState("");
  const [domain, setDomain] = useState("");
  const { data, error, loading } = usePoll<{ library_version: string; skills: Skill[] }>(`/api/v1/skills${qs({ tenant })}`, 30000);
  const domains = Array.from(new Set((data?.skills ?? []).map((s) => s.domain))).sort();
  const needle = q.trim().toLowerCase();
  const skills = (data?.skills ?? []).filter(
    (s) =>
      (!domain || s.domain === domain) &&
      (!needle || [s.id, s.title, s.summary, ...s.triggers].some((x) => x?.toLowerCase().includes(needle))),
  );
  return (
    <div className="page">
      <PageHeader
        title="Skills"
        sub={
          <>
            Tested skills in this platform's library{data && <> · library <code>{data.library_version}</code></>}. Policy decides how
            much autonomy each gets per customer.
          </>
        }
        actions={
          <div className="row-gap">
            <Select value={tenant} onChange={setTenant} options={[{ value: "", label: "All customers" }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]} />
            <Select value={domain} onChange={setDomain} options={[{ value: "", label: "All domains" }, ...domains.map((d) => ({ value: d, label: humanize(d) }))]} />
            <SearchBox value={q} onChange={setQ} placeholder="Search skills" />
          </div>
        }
      />
      <ErrorNote error={error} />
      {loading ? (
        <Loading />
      ) : skills.length === 0 ? (
        <Empty icon={<BookOpen size={22} />} title="No skills match" />
      ) : (
        <div className="skill-grid">
          {skills.map((s) => (
            <SkillCard key={s.id} s={s} />
          ))}
        </div>
      )}
    </div>
  );
}
