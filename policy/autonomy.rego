# The autonomy dial: how far agents may go with a skill for a customer, decided before every run.
#
# Input (built by src/estate_ops/policy.py):
#   tenant:  {id, autonomy: {default, <skill id>: mode}, change_freeze: [{from, to, reason}]}
#   skill:   {id, risk, approval, max_targets}
#   targets: [asset names]
#   blast_radius: {dependents, services: [names], critical: bool}
#   history: {live_runs_30d, failures_30d}
#   now:     RFC 3339 time of the decision
#
# Output data.estate.autonomy.decision: {mode, reason, wanted, caps}
#   recommend       agents show the plan; a person runs it
#   approve_to_run  dry run, then an engineer approves the live run
#   run_and_notify  dry run, live run and verification without waiting; the team is told
# Every rule can only lower the customer's chosen mode, never raise it.
package estate.autonomy

modes := {"recommend": 0, "approve_to_run": 1, "run_and_notify": 2}

wanted := m if {
	m := object.get(input.tenant.autonomy, input.skill.id, object.get(input.tenant.autonomy, "default", "approve_to_run"))
	modes[m]
} else := "recommend"

caps contains {"mode": "recommend", "reason": sprintf("%d targets is more than this skill's limit of %d", [count(input.targets), input.skill.max_targets])} if {
	count(input.targets) > input.skill.max_targets
}

caps contains {"mode": "recommend", "reason": "high-risk skills are not run by agents"} if {
	input.skill.risk == "high"
}

caps contains {"mode": "approve_to_run", "reason": sprintf("%s always needs an engineer's approval", [input.skill.id])} if {
	input.skill.approval == "required"
}

caps contains {"mode": "approve_to_run", "reason": sprintf("change freeze until %s: %s", [f.to, f.reason])} if {
	some f in object.get(input.tenant, "change_freeze", [])
	now := time.parse_rfc3339_ns(input.now)
	time.parse_rfc3339_ns(f.from) <= now
	now < time.parse_rfc3339_ns(f.to)
}

caps contains {"mode": "approve_to_run", "reason": sprintf("%s failed here %d time(s) in the last 30 days, so it has to earn its autonomy back", [input.skill.id, input.history.failures_30d])} if {
	input.history.failures_30d > 0
}

caps contains {"mode": "approve_to_run", "reason": sprintf("it touches business-critical %s", [concat(", ", input.blast_radius.services)])} if {
	input.blast_radius.critical
	input.skill.risk != "low"
}

binding := [c | some c in caps; modes[c.mode] < modes[wanted]]

lowest := min({modes[c.mode] | some c in binding})

decision := {"mode": wanted, "reason": sprintf("autonomy for %s at %s is %s", [input.skill.id, input.tenant.id, wanted]), "wanted": wanted, "caps": []} if {
	count(binding) == 0
}

decision := {"mode": mode, "reason": concat("; ", sort([c.reason | some c in binding; modes[c.mode] == lowest])), "wanted": wanted, "caps": [c.reason | some c in binding]} if {
	count(binding) > 0
	some mode, rank in modes
	rank == lowest
}
