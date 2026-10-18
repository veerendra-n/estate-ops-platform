"""Run ops-foundation runbooks through Semaphore and read back their evidence.

Convention: one Semaphore project per tenant, one task template per runbook named after the runbook id
(for example "sql/log-backup-shrink"), pointing at the playbook path from the catalog. The platform passes
run parameters as extra variables. API shapes follow Semaphore's REST API v2 and still need a lab check
(docs/integrations.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

EVIDENCE_PREFIX = "RUNBOOK_EVIDENCE "
EVIDENCE_SCHEMA = "ops-foundation/evidence/v1"
FINISHED = {"success", "error", "stopped", "rejected"}


@dataclass
class TaskState:
    status: str
    finished: bool
    evidence: list[dict[str, Any]]


def parse_evidence(lines: list[str]) -> list[dict[str, Any]]:
    """Pull RUNBOOK_EVIDENCE JSON out of task output. Ansible may wrap the line in quotes or a msg key."""
    found = []
    for line in lines:
        idx = line.find(EVIDENCE_PREFIX)
        if idx < 0:
            continue
        text = line[idx + len(EVIDENCE_PREFIX) :].strip().rstrip('",')
        item = None
        for candidate in (text, f'"{text}"'):
            try:
                item = json.loads(candidate)
                if isinstance(item, str):  # the line was JSON escaped inside a quoted string
                    item = json.loads(item)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(item, dict):
            continue
        if item.get("schema") == EVIDENCE_SCHEMA:
            found.append(item)
    return found


class Semaphore:
    def __init__(self, base_url: str, token: str | None, client: httpx.Client | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = client or httpx.Client(base_url=base_url, timeout=30.0)
        self.http.headers.update(headers)
        self._templates: dict[int, dict[str, int]] = {}

    def template_id(self, project_id: int, runbook_id: str) -> int:
        if project_id not in self._templates:
            self._templates[project_id] = {t["name"]: t["id"] for t in self.templates(project_id)}
        try:
            return self._templates[project_id][runbook_id]
        except KeyError:
            raise LookupError(f"Semaphore project {project_id} has no template named {runbook_id!r}") from None

    def templates(self, project_id: int) -> list[dict[str, Any]]:
        resp = self.http.get(f"/api/project/{project_id}/templates", params={"sort": "name", "order": "asc"})
        resp.raise_for_status()
        return resp.json()

    def create_template(self, project_id: int, template: dict[str, Any]) -> int:
        resp = self.http.post(f"/api/project/{project_id}/templates", json={"project_id": project_id, **template})
        resp.raise_for_status()
        self._templates.pop(project_id, None)
        return int(resp.json()["id"])

    def start(self, project_id: int, runbook_id: str, extra_vars: dict[str, Any]) -> int:
        body = {"template_id": self.template_id(project_id, runbook_id), "environment": json.dumps(extra_vars)}
        resp = self.http.post(f"/api/project/{project_id}/tasks", json=body)
        resp.raise_for_status()
        return int(resp.json()["id"])

    def state(self, project_id: int, task_id: int) -> TaskState:
        resp = self.http.get(f"/api/project/{project_id}/tasks/{task_id}")
        resp.raise_for_status()
        status = resp.json().get("status", "unknown")
        if status not in FINISHED:
            return TaskState(status, False, [])
        out = self.http.get(f"/api/project/{project_id}/tasks/{task_id}/output")
        out.raise_for_status()
        lines = [o.get("output", "") for o in out.json()]
        return TaskState(status, True, parse_evidence(lines))
