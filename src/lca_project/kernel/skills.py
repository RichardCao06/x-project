"""Machine-readable Skill registry and the only production job entrypoint.

Skills remain deliberately thin.  Their executable meaning lives in the
frontmatter contract: a versioned workflow, input schema and policy.  This
module turns that contract into a persistent Job instead of asking an
interactive agent to interpret Markdown and manually sequence scripts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from lca_project.contracts import Job, load_json
from lca_project.control import ControlPlane
from .registry import CapabilityRegistry
from .workflow import WorkflowSpec, compile_workflow


class SkillError(ValueError):
    pass


WORKFLOW_URI = re.compile(r"^workflow://([a-z0-9][a-z0-9-]*)@([A-Za-z0-9._-]+)$")


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    workflow_id: str
    workflow_version: str
    input_schema: str
    policy_version: str
    path: Path

    @property
    def workflow_ref(self) -> str:
        return f"{self.workflow_id}@{self.workflow_version}"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SkillError(f"{path}: missing YAML frontmatter")
    try:
        block = text.split("---\n", 2)[1]
    except IndexError as exc:
        raise SkillError(f"{path}: unterminated YAML frontmatter") from exc
    result: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"{path}: unsupported frontmatter line: {line}")
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"\'')
    return result


class SkillRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.capabilities = CapabilityRegistry.load_directory(self.root / "capabilities")
        self._skills: dict[str, Skill] = {}
        for path in sorted((self.root / "skills").glob("*/SKILL.md")):
            raw = _frontmatter(path)
            missing = {"name", "version", "workflow", "input_schema", "policy"} - raw.keys()
            if missing:
                raise SkillError(f"{path}: missing executable Skill fields: {sorted(missing)}")
            match = WORKFLOW_URI.fullmatch(raw["workflow"])
            if not match:
                raise SkillError(f"{path}: invalid workflow URI {raw['workflow']!r}")
            skill = Skill(raw["name"], raw["version"], match.group(1), match.group(2),
                          raw["input_schema"], raw["policy"], path)
            if skill.name in self._skills:
                raise SkillError(f"duplicate Skill: {skill.name}")
            self._validate_links(skill)
            self._skills[skill.name] = skill

    def _validate_links(self, skill: Skill) -> None:
        workflow_path = self.root / "workflows" / f"{skill.workflow_ref}.json"
        if not workflow_path.is_file():
            raise SkillError(f"{skill.name}: workflow not found: {skill.workflow_ref}")
        raw = load_json(workflow_path)
        spec = WorkflowSpec.from_mapping(raw)
        if spec.id != skill.workflow_id or spec.version != skill.workflow_version:
            raise SkillError(f"{skill.name}: workflow identity drift")
        compile_workflow(spec, {item.id for item in self.capabilities.all()})
        schema_path = self.root / "contracts" / f"{skill.input_schema}.schema.json"
        if not schema_path.is_file():
            raise SkillError(f"{skill.name}: input schema not found: {skill.input_schema}")
        policy_path = self.root / "policies" / f"{skill.policy_version}.json"
        if not policy_path.is_file():
            raise SkillError(f"{skill.name}: policy not found: {skill.policy_version}")

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillError(f"unknown Skill: {name}") from exc

    def all(self) -> tuple[Skill, ...]:
        return tuple(self._skills[key] for key in sorted(self._skills))


def _validate_request(schema: dict[str, Any], request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise SkillError("Skill request must be an object")
    missing = [key for key in schema.get("required", []) if key not in request]
    if missing:
        raise SkillError(f"Skill request misses required fields: {missing}")
    if schema.get("additionalProperties") is False:
        extras = set(request) - set(schema.get("properties", {}))
        if extras:
            raise SkillError(f"Skill request has unknown fields: {sorted(extras)}")


class SkillInvoker:
    """Validate a request, freeze it in CAS and create one durable workflow run."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.registry = SkillRegistry(self.root)
        self.control = ControlPlane(self.root)

    def invoke(self, name: str, request: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        skill = self.registry.get(name)
        schema = load_json(self.root / "contracts" / f"{skill.input_schema}.schema.json")
        _validate_request(schema, request)
        request_artifact = self.control.artifacts.put_json(request, metadata={
            "schema": skill.input_schema, "skill": skill.name, "skill_version": skill.version,
        })
        stable = hashlib.sha256(json.dumps({
            "skill": skill.name, "version": skill.version, "workflow": skill.workflow_ref,
            "request": request_artifact.digest,
        }, sort_keys=True).encode()).hexdigest()
        target = str(request.get("target") or request.get("industry") or skill.name)
        job = Job(target=target, workflow=skill.workflow_ref,
                  scope={"skill": skill.name, "skill_version": skill.version, "request": request},
                  policy_version=skill.policy_version, input_hashes=(request_artifact.digest,))
        job_id, duplicate = self.control.submit_job(job, idempotency_key=idempotency_key or f"skill:{stable}")
        self.control.events.append("job", job_id, "skill.invoked", {
            "skill": skill.name, "skill_version": skill.version, "workflow": skill.workflow_ref,
            "request_hash": request_artifact.digest,
        }, actor="skill-runtime", event_id=f"skill-invoked:{job_id}")
        return {"job_id": job_id, "deduplicated": duplicate, "skill": skill.name,
                "workflow": skill.workflow_ref, "request_hash": request_artifact.digest}
