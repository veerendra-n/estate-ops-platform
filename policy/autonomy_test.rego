package estate.autonomy_test

import data.estate.autonomy

base := {
	"tenant": {"id": "contoso", "autonomy": {"default": "approve_to_run", "backup/cleanup-and-rerun": "run_and_notify"}, "change_freeze": []},
	"skill": {"id": "backup/cleanup-and-rerun", "risk": "low", "approval": "not_required", "max_targets": 1},
	"targets": ["Fabrikam SQL nightly"],
	"blast_radius": {"dependents": 2, "services": [], "critical": false},
	"history": {"live_runs_30d": 4, "failures_30d": 0},
	"now": "2026-12-01T10:00:00Z",
}

test_customer_choice_applies if {
	autonomy.decision.mode == "run_and_notify" with input as base
}

test_default_mode_for_other_skills if {
	inp := json.patch(base, [{"op": "replace", "path": "/skill/id", "value": "windows/restart-service"}])
	autonomy.decision.mode == "approve_to_run" with input as inp
}

test_high_risk_only_recommended if {
	inp := json.patch(base, [{"op": "replace", "path": "/skill/risk", "value": "high"}])
	d := autonomy.decision with input as inp
	d.mode == "recommend"
	contains(d.reason, "high-risk")
}

test_too_many_targets if {
	inp := json.patch(base, [{"op": "replace", "path": "/targets", "value": ["a", "b"]}])
	autonomy.decision.mode == "recommend" with input as inp
}

test_approval_required_caps_run_and_notify if {
	inp := json.patch(base, [{"op": "replace", "path": "/skill/approval", "value": "required"}])
	autonomy.decision.mode == "approve_to_run" with input as inp
}

test_change_freeze if {
	inp := json.patch(base, [{"op": "replace", "path": "/tenant/change_freeze", "value": [{"from": "2026-11-30T00:00:00Z", "to": "2026-12-02T00:00:00Z", "reason": "month-end close"}]}])
	d := autonomy.decision with input as inp
	d.mode == "approve_to_run"
	contains(d.reason, "month-end close")
}

test_freeze_over if {
	inp := json.patch(base, [{"op": "replace", "path": "/tenant/change_freeze", "value": [{"from": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z", "reason": "old"}]}])
	autonomy.decision.mode == "run_and_notify" with input as inp
}

test_recent_failure_loses_autonomy if {
	inp := json.patch(base, [{"op": "replace", "path": "/history/failures_30d", "value": 1}])
	autonomy.decision.mode == "approve_to_run" with input as inp
}

test_critical_service_medium_risk if {
	inp := json.patch(base, [
		{"op": "replace", "path": "/skill/risk", "value": "medium"},
		{"op": "replace", "path": "/blast_radius", "value": {"dependents": 9, "services": ["Checkout"], "critical": true}},
	])
	d := autonomy.decision with input as inp
	d.mode == "approve_to_run"
	contains(d.reason, "Checkout")
}

test_rules_never_raise if {
	inp := json.patch(base, [{"op": "replace", "path": "/tenant/autonomy/backup~1cleanup-and-rerun", "value": "recommend"}])
	autonomy.decision.mode == "recommend" with input as inp
}

test_unknown_mode_is_recommend if {
	inp := json.patch(base, [{"op": "replace", "path": "/tenant/autonomy/backup~1cleanup-and-rerun", "value": "yolo"}])
	autonomy.decision.mode == "recommend" with input as inp
}
