import type { JsonObject } from "./types";
import { show } from "./format";

const s = (b: JsonObject | null | undefined, k: string): string | null => {
  const v = b?.[k];
  return v === undefined || v === null || v === "" ? null : show(v);
};

/** One line describing a ledger entry, for the live activity feed. */
export function entrySummary(kind: string, body: JsonObject | null): string {
  switch (kind) {
    case "case_opened":
      return `Case opened from ${s(body, "alertname") ?? s(body, "alert") ?? s(body, "source") ?? "an alert"}`;
    case "alert_folded":
      return `Alert folded in${s(body, "target") ? ` · ${s(body, "target")}` : ""}`;
    case "alert_cleared":
      return "Alert cleared at source";
    case "investigation":
    case "diagnosis":
      return s(body, "likely_cause") ?? s(body, "summary") ?? "Investigation finished";
    case "proposal":
      return `Proposed ${s(body, "skill_title") ?? s(body, "skill_id") ?? "a skill"}${s(body, "target") ? ` on ${s(body, "target")}` : ""}`;
    case "approval_requested":
      return "Waiting for an engineer to approve";
    case "approved":
      return `Approved${s(body, "note") ? `: “${s(body, "note")}”` : ""}`;
    case "rejected":
      return `Rejected${s(body, "note") ? `: “${s(body, "note")}”` : ""}`;
    case "run_result": {
      const dry = body?.dry_run === true;
      const ev = (body?.evidence ?? body) as JsonObject | null;
      const outcome = s(ev, "outcome") ?? s(body, "outcome");
      return dry ? "Dry run finished — plan ready" : `Run finished${outcome ? `: ${outcome}` : ""}`;
    }
    case "verified":
      return "Verifier confirmed the fix";
    case "escalated":
      return `Escalated${s(body, "reason") ? `: ${s(body, "reason")}` : ""}`;
    case "cases_merged":
      return `Merged ${s(body, "merged") ?? "a related case"} into this case`;
    case "merged_into":
      return `Merged into ${s(body, "into") ?? "another case"}${s(body, "why") ? `: ${s(body, "why")}` : ""}`;
    case "resolved":
      return `Resolved${s(body, "reason") ? `: ${s(body, "reason")}` : ""}`;
    case "copilot_question":
      return `Asked: ${s(body, "question") ?? ""}`;
    case "copilot_answer":
      return "Copilot answered";
    default:
      return kind.replace(/_/g, " ");
  }
}
