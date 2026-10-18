// Estate Ops console: case queue and incident workspace. Plain modules, no build step.
// Reads the platform API; the case conversation is the ledger for that case.

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: keep it in memory */ } },
};

const VIEWS = {
  active: { title: "Needs attention", sub: "Waiting on an engineer: approvals and escalations", statuses: ["awaiting_approval", "escalated"] },
  approval: { title: "Awaiting approval", sub: "A tested runbook has been dry run and needs your decision", statuses: ["awaiting_approval"] },
  agents: { title: "With agents", sub: "Being diagnosed or running a runbook", statuses: ["open", "diagnosing", "running"] },
  closed: { title: "Resolved", sub: "Fixed, or nothing needed doing", statuses: ["resolved"] },
};
const STATUS = {
  open: ["New", "c-n"], diagnosing: ["Diagnosing", "c-agent"], awaiting_approval: ["Needs approval", "c-warn"],
  running: ["Running runbook", "c-agent"], resolved: ["Resolved", "c-ok"], escalated: ["Escalated to you", "c-bad"],
};
const OUTCOME = {
  dry_run: ["Dry run ok", "c-agent"], fixed: ["Fixed and verified", "c-ok"], no_action: ["Nothing to do", "c-ok"],
  escalated: ["Runbook escalated", "c-warn"], pending_device: ["Queued on device", "c-agent"], failed: ["Failed", "c-bad"],
  timed_out: ["Timed out", "c-bad"],
};

const state = { view: "active", tenant: null, cases: [], selected: null, detail: null, me: store.get("eo-name"), token: store.get("eo-token") };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(opts.headers || {}) },
  });
  if (res.status === 401) { signIn(); throw new Error("Sign in again"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status}`);
  return body;
}

function toast(text) {
  const el = $("#toast"); el.textContent = text; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), 3500);
}

function signIn() {
  const dlg = $("#signin");
  if (dlg.open) return;
  $("#si-name").value = state.me || "";
  dlg.showModal();
}
$("#signin-form").addEventListener("submit", () => {
  state.me = $("#si-name").value.trim(); state.token = $("#si-token").value.trim();
  store.set("eo-name", state.me); store.set("eo-token", state.token);
  refresh();
});

const when = (t) => new Date(t).toLocaleString([], { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
const chip = ([label, cls]) => `<span class="chip ${cls}">${esc(label)}</span>`;

// ---- rail and queue ----------------------------------------------------------------------------------------

function renderRail(tenants) {
  for (const [key, v] of Object.entries(VIEWS)) {
    $(`#n-${key}`).textContent = state.cases.filter((c) => v.statuses.includes(c.status) && (!state.tenant || c.tenant_id === state.tenant)).length;
  }
  if (tenants) {
    $("#tenants").innerHTML = [{ id: null, name: "All customers" }, ...tenants].map((t) => `
      <button type="button" data-tenant="${esc(t.id ?? "")}" aria-pressed="${state.tenant === t.id}">
        <span>${esc(t.name.split(" (")[0])}</span>
        ${t.id ? `<span class="spend" title="AI spend this month">$${t.ai_spend_month_usd.toFixed(2)}${t.ledger_ok ? "" : " ⚠"}</span>` : ""}
      </button>`).join("");
  }
  $("#whoami").textContent = state.me ? `Signed in as ${state.me}` : "";
}

function renderQueue() {
  const v = VIEWS[state.view];
  $("#qtitle").textContent = v.title; $("#qsub").textContent = v.sub;
  const rows = state.cases.filter((c) => v.statuses.includes(c.status) && (!state.tenant || c.tenant_id === state.tenant));
  $("#cases").innerHTML = rows.map((c) => `
    <button type="button" class="case" data-case="${c.id}" aria-current="${c.id === state.selected}">
      <span class="top"><span>${esc(c.ref)} · ${esc(c.severity)} · ${esc(c.tenant_name.split(" (")[0])}</span><span>${when(c.updated_at)}</span></span>
      <span class="title">${esc(c.title)}</span>
      <span>${chip(STATUS[c.status] || [c.status, "c-n"])} ${c.signals > 1 ? `<span class="muted">${c.signals} alerts folded</span>` : ""}</span>
      ${c.summary ? `<span class="sum">${esc(c.summary)}</span>` : ""}
    </button>`).join("") || `<p class="note" style="padding:16px">Nothing here.</p>`;
}

document.querySelector(".views").addEventListener("click", (e) => {
  const b = e.target.closest("[data-view]"); if (!b) return;
  state.view = b.dataset.view;
  document.querySelectorAll(".views button").forEach((x) => x.setAttribute("aria-current", x === b ? "page" : "false"));
  renderQueue(); renderRail();
});
$("#tenants").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tenant]"); if (!b) return;
  state.tenant = b.dataset.tenant || null; refresh();
});
$("#cases").addEventListener("click", (e) => {
  const b = e.target.closest("[data-case]"); if (!b) return;
  state.selected = b.dataset.case; renderQueue(); loadCase();
});

// ---- workspace -----------------------------------------------------------------------------------------------

const ACTORS = {
  triage: ["TR", "Triage agent", "agent"], "agent:diagnosis": ["DX", "Diagnosis agent", "agent"],
  "agent:remediator": ["RM", "Remediator agent", "agent"], platform: ["EO", "Platform", ""], policy: ["PO", "Autonomy policy", ""],
};
function actor(a) {
  if (ACTORS[a]) return ACTORS[a];
  if (a.startsWith("engineer:")) { const n = a.slice(9); return [n.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase(), n, "person"]; }
  if (a.startsWith("runbook:")) return ["RB", `Runbook ${a.slice(8)}`, ""];
  return ["··", a, ""];
}
const pre = (obj) => `<pre>${esc(JSON.stringify(obj, null, 2))}</pre>`;

function entryBody(e, d) {
  const b = e.body;
  switch (e.kind) {
    case "case_opened":
      return `Opened this case from OpenObserve alert <span class="mono">${esc(b.alert_id)}</span>${b.target ? ` on <strong>${esc(b.target)}</strong>` : ""}.
        ${b.known_alert ? "" : " This alert has no runbook linked to it in ops-foundation."}
        <details><summary>Alert row</summary>${pre(b.labels)}</details>`;
    case "diagnosis":
      return `<div><strong>${esc(b.summary)}</strong></div>
        <div>Likely cause: ${esc(b.likely_cause)} ${chip([`${b.confidence} confidence`, b.confidence === "high" ? "c-ok" : b.confidence === "medium" ? "c-agent" : "c-n"])}</div>
        <ol class="ev">${(b.evidence || []).map((x) => `<li>${esc(x.finding)} <span class="src">${esc(x.source)}</span></li>`).join("")}</ol>
        ${b.engineer_should_check ? `<div class="note">Check: ${esc(b.engineer_should_check)}</div>` : ""}
        <details><summary>How this was worked out</summary>
          <div>${b.method === "llm" ? "Claude, with read-only tools" : "Deterministic: the alert's own runbook link, no model call"} · AI cost $${(b.ai_cost_usd || 0).toFixed(4)}</div>
          ${(b.notes || []).concat(b.budget ? [b.budget] : []).map((n) => `<div>${esc(n)}</div>`).join("")}
        </details>`;
    case "proposal":
      return `Proposes <strong>${esc(b.runbook_title)}</strong> <span class="mono">${esc(b.runbook_id)} v${esc(b.runbook_version)}</span> on <strong>${esc(b.targets)}</strong>.
        <div class="row" style="margin-top:4px">${chip([`risk ${b.risk}`, b.risk === "low" ? "c-ok" : "c-warn"])} ${chip([`approval ${b.approval.replace("_", " ")}`, "c-n"])} ${chip([b.mode.replaceAll("_", " "), "c-agent"])}</div>
        <div class="note">${esc(b.policy)}. It runs as a dry run first.</div>
        <details><summary>Parameters and rollback</summary>${pre(b.params)}<div>${esc(b.rollback || "")}</div></details>`;
    case "run_started":
      return `${b.dry_run ? "Dry run" : "Live run"} started in Semaphore, task <span class="mono">${esc(b.semaphore_task_id)}</span> (run <span class="mono">${esc(b.run_id)}</span>).`;
    case "run_result": {
      const ev = b.evidence || {};
      const dec = ev.decision || {};
      return `<div>${chip(OUTCOME[b.outcome] || [b.outcome, "c-n"])} ${dec.action ? `Decision: <span class="mono">${esc(dec.action)}</span>` : ""}</div>
        ${dec.reason ? `<div>${esc(dec.reason)}</div>` : ""}
        ${ev.error ? `<div class="chip c-bad">${esc(ev.error)}</div>` : ""}
        ${b.evidence_rejected ? `<div class="chip c-bad">${b.evidence_rejected} evidence line(s) for another customer or host were ignored</div>` : ""}
        <details><summary>Evidence: before, actions, after</summary>${pre({ before: ev.before, actions: ev.actions, after: ev.after })}</details>`;
    }
    case "approval_requested": {
      const p = d.proposals.find((x) => x.id === b.proposal_id);
      const open = p && p.status === "awaiting_approval";
      return `The dry run says it would <strong>${esc(b.action)}</strong>. ${esc(b.reason)}
        ${open ? `<div class="approval" data-proposal="${p.id}">
          <strong>Approve ${esc(p.runbook_id)} on ${esc(p.targets)}?</strong>
          <textarea placeholder="Note for the record (optional)" aria-label="Approval note"></textarea>
          <div class="row"><button class="btn primary" data-act="approve" type="button">Approve and run</button>
          <button class="btn" data-act="reject" type="button">Reject</button>
          <span class="note">Signed as ${esc(state.me || "…")}</span></div></div>` : ""}`;
    }
    case "approved": return `Approved${b.by === "policy" ? " by the autonomy policy after a clean dry run" : ""}. ${esc(b.note || "")}`;
    case "rejected": return `Rejected. ${esc(b.note || "")}`;
    case "escalated": return `<span class="chip c-bad">Escalated</span> ${esc(b.reason)}${b.engineer_should_check ? `<div class="note">${esc(b.engineer_should_check)}</div>` : ""}`;
    case "resolved": return `<span class="chip c-ok">Resolved</span> ${esc(b.reason)}`;
    default: return pre(b);
  }
}

function renderCase() {
  const d = state.detail; if (!d) return;
  const c = d.case;
  const last = d.proposals.at(-1);
  $("#workspace").innerHTML = `
    <section class="card">
      <header class="wshead">
        <div class="crumb">${esc(c.tenant_name)} · <span class="mono">${esc(c.ref)}</span></div>
        <h1>${esc(c.title)}</h1>
        <div class="row">${chip(STATUS[c.status] || [c.status, "c-n"])} <span class="muted">${esc(c.severity)} · opened ${when(c.opened_at)}</span></div>
      </header>
      <div class="tl">${d.timeline.map((e) => {
        const [ini, name, cls] = actor(e.actor);
        return `<div class="tm"><div class="av ${cls}">${esc(ini)}</div><div><div class="hd"><strong>${esc(name)}</strong><span class="t">${when(e.at)}</span></div>${entryBody(e, d)}</div></div>`;
      }).join("")}</div>
    </section>
    <aside class="card side">
      <h2>Details</h2>
      <div class="fld"><span class="k">Customer</span><span>${esc(c.tenant_name)}</span></div>
      <div class="fld"><span class="k">Target</span><span class="mono">${esc(c.target || "—")}</span></div>
      <div class="fld"><span class="k">Priority</span><span>${esc(c.severity)}</span></div>
      <div class="fld"><span class="k">Alerts</span><span>${d.signals.length} (${esc([...new Set(d.signals.map((s) => s.alert_id))].join(", "))})</span></div>
      <div class="fld"><span class="k">Runbook</span><span class="mono">${last ? `${esc(last.runbook_id)} · ${esc(last.status.replaceAll("_", " "))}` : "none"}</span></div>
      <div class="fld"><span class="k">AI cost</span><span class="mono">$${d.ai_cost_usd.toFixed(4)}</span></div>
      <div class="fld"><span class="k">Ledger</span><span class="mono" title="Hash of the latest entry">${esc((d.timeline.at(-1)?.hash || "").slice(0, 16))}…</span></div>
      <p class="note" style="padding:0 16px 14px">Every entry in this conversation is in the tamper-evident ledger.</p>
    </aside>`;
}

$("#workspace").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const box = b.closest("[data-proposal]");
  if (!state.me) return signIn();
  box.querySelectorAll("button").forEach((x) => (x.disabled = true));
  try {
    await api(`/api/v1/proposals/${box.dataset.proposal}/${b.dataset.act}`, {
      method: "POST", body: JSON.stringify({ who: state.me, note: box.querySelector("textarea").value }),
    });
    toast(b.dataset.act === "approve" ? "Approved. The runbook runs now." : "Rejected and escalated.");
    await refresh();
  } catch (err) { toast(err.message); box.querySelectorAll("button").forEach((x) => (x.disabled = false)); }
});

async function loadCase() {
  if (!state.selected) return;
  const keepNote = document.querySelector(".approval textarea")?.value;
  state.detail = await api(`/api/v1/cases/${state.selected}`);
  renderCase();
  const ta = document.querySelector(".approval textarea"); if (ta && keepNote) ta.value = keepNote;
}

async function refresh() {
  if (!state.token) return signIn();
  try {
    const [cases, tenants] = await Promise.all([api("/api/v1/cases"), api("/api/v1/tenants")]);
    state.cases = cases;
    renderRail(tenants); renderQueue();
    if (state.selected && document.activeElement?.tagName !== "TEXTAREA") await loadCase();
  } catch (err) { if (err.message !== "Sign in again") toast(err.message); }
}

refresh();
setInterval(refresh, 4000);
