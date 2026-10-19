"""The skills library: every action the platform can take on a customer's estate.

A skill is a folder under skills/<domain>/<name>/ with
- skill.yml: id, version, title, risk, approval, the asset kind it targets, the connectors it may use,
  its parameters, the alerts that trigger it (and which alert labels fill which parameters), rollback;
- skill.py: four functions in the same shape for every skill:
    gather(ctx)                    read-only facts about the target, through ctx's connectors
    decide(facts, params)          pure: {"action": <name>|"none"|"escalate", "reason": ..., "plan": [...]}
    act(ctx, facts, decision)      performs the plan, returns what it did (live runs only)
    verify(ctx, facts, decision)   reads the target again: {"ok": bool, ...}
- tests.yml: decide() cases (facts, params, expected action), run by the test suite with no estate.

A dry run stops after decide, so an engineer approves the exact plan the live run will follow, and the
live run gathers and decides again in case the estate moved. Every run returns one evidence record that
goes into the case ledger.
"""

from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

RISKS = ("low", "medium", "high")
OUTCOMES = ("dry_run", "no_action", "escalated", "fixed", "failed")


class SkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class Skill:
    id: str
    version: str
    title: str
    summary: str
    domain: str
    risk: str
    approval: str  # required | not_required
    target_kind: str
    connectors: tuple[str, ...]
    params: tuple[dict[str, Any], ...]
    triggers: tuple[dict[str, Any], ...]
    max_targets: int = 1
    rollback: str = ""
    notes: str = ""
    path: Path = field(default=Path("."), compare=False)

    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "domain": self.domain,
            "risk": self.risk,
            "approval": self.approval,
            "target_kind": self.target_kind,
            "connectors": list(self.connectors),
            "max_targets": self.max_targets,
            "params": list(self.params),
            "triggers": [t["alert"] for t in self.triggers],
            "rollback": self.rollback,
        }


class Skills:
    def __init__(self, skills: list[Skill], version: str | None = None):
        self._by_id = {s.id: s for s in skills}
        self._modules: dict[str, ModuleType] = {}
        self.version = version  # git commit of the library, when known

    @classmethod
    def load(cls, root: Path, version: str | None = None) -> Skills:
        skills = []
        for meta in sorted(root.glob("*/*/skill.yml")):
            raw = yaml.safe_load(meta.read_text(encoding="utf-8"))
            expected = f"{meta.parent.parent.name}/{meta.parent.name}"
            if raw.get("id") != expected:
                raise ValueError(f"{meta}: id must be {expected}")
            if raw.get("risk") not in RISKS or raw.get("approval") not in ("required", "not_required"):
                raise ValueError(f"{meta}: risk must be one of {RISKS} and approval required or not_required")
            if not (meta.parent / "skill.py").is_file():
                raise ValueError(f"{meta.parent}: skill.py is missing")
            skills.append(
                Skill(
                    id=raw["id"],
                    version=str(raw["version"]),
                    title=raw["title"],
                    summary=raw.get("summary", ""),
                    domain=raw.get("domain", expected.split("/")[0]),
                    risk=raw["risk"],
                    approval=raw["approval"],
                    target_kind=raw["target"]["kind"],
                    connectors=tuple(raw.get("connectors") or ()),
                    params=tuple(raw.get("params") or ()),
                    triggers=tuple(raw.get("triggers") or ()),
                    max_targets=int(raw.get("max_targets", 1)),
                    rollback=raw.get("rollback", ""),
                    notes=raw.get("notes", ""),
                    path=meta.parent,
                )
            )
        return cls(skills, version)

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def __iter__(self):
        return iter(self._by_id[i] for i in self.ids())

    def get(self, skill_id: str) -> Skill:
        try:
            return self._by_id[skill_id]
        except KeyError:
            raise KeyError(f"skill {skill_id!r} is not in the library") from None

    def module(self, skill_id: str) -> ModuleType:
        if skill_id not in self._modules:
            skill = self.get(skill_id)
            spec = importlib.util.spec_from_file_location(
                f"estate_skills.{skill_id.replace('/', '.').replace('-', '_')}", skill.path / "skill.py"
            )
            if spec is None or spec.loader is None:
                raise SkillError(f"cannot load {skill_id}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for fn in ("gather", "decide", "act", "verify"):
                if not callable(getattr(mod, fn, None)):
                    raise SkillError(f"{skill_id}: skill.py has no {fn}()")
            self._modules[skill_id] = mod
        return self._modules[skill_id]

    def for_alert(self, alert_id: str) -> list[str]:
        return sorted(s.id for s in self._by_id.values() if any(t["alert"] == alert_id for t in s.triggers))

    def params_from_alert(self, skill_id: str, alert_id: str, labels: dict[str, Any]) -> dict[str, Any]:
        """The parameters an alert's labels fill, as its trigger in skill.yml maps them."""
        trigger = next((t for t in self.get(skill_id).triggers if t["alert"] == alert_id), None)
        out = {}
        for param, label in ((trigger or {}).get("params") or {}).items():
            if labels.get(label) not in (None, ""):
                out[param] = labels[label]
        return out

    def summary(self, skill_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """What an agent needs to choose a skill."""
        out = []
        for sid in skill_ids or self.ids():
            s = self.get(sid)
            out.append(
                {
                    "id": s.id,
                    "title": s.title,
                    "summary": s.summary,
                    "risk": s.risk,
                    "approval": s.approval,
                    "target_kind": s.target_kind,
                    "params": [
                        {k: p[k] for k in ("name", "type", "required", "default", "choices", "description") if k in p}
                        for p in s.params
                    ],
                }
            )
        return out

    def validate_params(self, skill_id: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Check parameters against skill.yml, fill defaults. Returns (clean params, errors)."""
        skill = self.get(skill_id)
        specs = {p["name"]: p for p in skill.params}
        clean: dict[str, Any] = {}
        errors: list[str] = []
        for name, value in params.items():
            spec = specs.get(name)
            if spec is None:
                errors.append(f"{name} is not a parameter of {skill_id}")
                continue
            if value is None or value == "":
                continue
            try:
                clean[name] = _coerce(spec, value)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
        for name, spec in specs.items():
            if name not in clean and "default" in spec:
                clean[name] = spec["default"]
            if spec.get("required") and name not in clean:
                errors.append(f"{name} is required by {skill_id}")
        return clean, errors


def _coerce(spec: dict[str, Any], value: Any) -> Any:
    kind = spec.get("type", "string")
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError(f"expected a {kind}")
        number = int(value) if kind == "integer" else float(value)
        if "min" in spec and number < spec["min"] or "max" in spec and number > spec["max"]:
            raise ValueError(f"must be between {spec.get('min')} and {spec.get('max')}")
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError("expected true or false")
    if kind == "enum":
        if value not in spec.get("choices", []):
            raise ValueError(f"expected one of {spec.get('choices')}")
        return value
    text = str(value)
    if len(text) > 256 or any(c in text for c in "\n\r\x00"):
        raise ValueError("expected a short single-line string")
    return text


# ---- running a skill -----------------------------------------------------------------------------------


@dataclass
class Context:
    """What a skill sees: its tenant, one target, its parameters and only the connectors skill.yml names."""

    tenant: Any
    target: str
    params: dict[str, Any]
    skill: Skill
    _connectors: Any
    graph: Any = None
    dry_run: bool = True
    approval_ref: str | None = None
    log: list[str] = field(default_factory=list)
    sleep: Any = time.sleep  # verify() waits with this; tests pass a no-op
    actions: list[dict[str, Any]] = field(default_factory=list)  # what act() did, for verify()

    def connector(self, name: str) -> Any:
        if name not in self.skill.connectors:
            raise SkillError(f"{self.skill.id} did not declare the {name} connector")
        return getattr(self._connectors, name)()

    def note(self, text: str) -> None:
        self.log.append(text[:300])


def run(skills: Skills, ctx: Context, live: bool) -> dict[str, Any]:
    """Gather, decide and, for a live run with an action, act and verify. Never raises for estate errors:
    a failure becomes the "failed" outcome with its error, so the case records it."""
    skill = ctx.skill
    mod = skills.module(skill.id)
    evidence: dict[str, Any] = {
        "skill": skill.id,
        "version": skill.version,
        "tenant": ctx.tenant.id,
        "target": ctx.target,
        "dry_run": not live,
        "params": ctx.params,
    }
    started = time.monotonic()
    stage = "gather"
    try:
        facts = mod.gather(ctx)
        evidence["before"] = facts
        stage = "decide"
        decision = mod.decide(facts, ctx.params)
        evidence["decision"] = decision
        action = decision.get("action")
        if action == "none":
            evidence["outcome"] = "no_action"
        elif action == "escalate":
            evidence["outcome"] = "escalated"
        elif not live:
            evidence["outcome"] = "dry_run"
        else:
            if skill.approval == "required" and not ctx.approval_ref:
                raise SkillError("a live run of this skill needs an approval reference")
            stage = "act"
            ctx.actions = evidence["actions"] = mod.act(ctx, facts, decision)
            stage = "verify"
            after = mod.verify(ctx, facts, decision)
            evidence["after"] = after
            evidence["outcome"] = "fixed" if after.get("ok") else "failed"
            if not after.get("ok"):
                evidence["error"] = after.get("reason") or "verification failed"
    except Exception as exc:
        evidence["outcome"] = "failed"
        evidence["error"] = f"{stage}: {type(exc).__name__}: {exc}"[:500]
    evidence["seconds"] = round(time.monotonic() - started, 2)
    if ctx.log:
        evidence["log"] = ctx.log
    return evidence
