"""Machine-readable Skill registry and the only production job entrypoint.

Skills remain deliberately thin. Standard SKILL.md metadata owns discovery
and human/agent instructions; the adjacent route manifest owns the versioned
workflow, input schema and policy. This module turns that machine contract into
a persistent Job instead of asking an interactive agent to interpret Markdown
and manually sequence scripts.
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
    description: str
    version: str
    workflow_id: str
    workflow_version: str
    input_schema: str
    policy_version: str
    path: Path
    route_path: Path

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
            frontmatter = _frontmatter(path)
            if "name" not in frontmatter:
                raise SkillError(f"{path}: missing Skill name")
            route_path = path.parent / "skill.manifest.json"
            if route_path.is_file():
                route = load_json(route_path)
                missing = {"schema_version", "name", "version", "workflow", "input_schema", "policy"} - route.keys()
                if missing:
                    raise SkillError(f"{route_path}: missing executable Skill fields: {sorted(missing)}")
                if route["schema_version"] != "lca-skill-route-v1":
                    raise SkillError(f"{route_path}: unsupported route schema {route['schema_version']!r}")
                if route["name"] != frontmatter["name"]:
                    raise SkillError(f"{path}: Skill name differs from route manifest")
                raw = {key: str(value) for key, value in route.items() if key in {
                    "name", "version", "workflow", "input_schema", "policy"
                }}
            else:
                # Backward-compatible migration path for the remaining project
                # Skills. New and revised Skills use a standard SKILL.md plus a
                # separate executable route manifest.
                raw = frontmatter
                missing = {"name", "version", "workflow", "input_schema", "policy"} - raw.keys()
                if missing:
                    raise SkillError(f"{path}: missing executable Skill fields: {sorted(missing)}")
                route_path = path
            match = WORKFLOW_URI.fullmatch(raw["workflow"])
            if not match:
                raise SkillError(f"{path}: invalid workflow URI {raw['workflow']!r}")
            skill = Skill(raw["name"], frontmatter.get("description", ""), raw["version"],
                          match.group(1), match.group(2), raw["input_schema"], raw["policy"],
                          path, route_path)
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


def _validate_value(schema: dict[str, Any], value: Any, path: str) -> None:
    expected = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected in checks and not checks[expected](value):
        raise SkillError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise SkillError(f"{path} must be one of {schema['enum']}")
    if expected == "string":
        if len(value) < int(schema.get("minLength", 0)):
            raise SkillError(f"{path} is shorter than minLength")
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(str(pattern), value) is None:
            raise SkillError(f"{path} does not match {pattern}")
    elif expected == "array":
        if len(value) < int(schema.get("minItems", 0)):
            raise SkillError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SkillError(f"{path} has more than maxItems")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                raise SkillError(f"{path} must contain unique items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item_schema, item, f"{path}[{index}]")
    elif expected == "object":
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise SkillError(f"{path} misses required fields: {missing}")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise SkillError(f"{path} has unknown fields: {sorted(extras)}")
        for key, item in value.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict):
                _validate_value(property_schema, item, f"{path}.{key}")


def _normalize_request(schema: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise SkillError("Skill request must be an object")
    normalized = json.loads(json.dumps(request, ensure_ascii=False))
    for key, property_schema in schema.get("properties", {}).items():
        if key not in normalized and isinstance(property_schema, dict) and "default" in property_schema:
            normalized[key] = property_schema["default"]
    _validate_value(schema, normalized, "request")
    return normalized


class SkillInvoker:
    """Validate a request, freeze it in CAS and create one durable workflow run."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.registry = SkillRegistry(self.root)
        self.control = ControlPlane(self.root)

    def validate_request(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Normalize one request against the registered Skill contract without creating a Job."""
        skill = self.registry.get(name)
        schema = load_json(self.root / "contracts" / f"{skill.input_schema}.schema.json")
        return _normalize_request(schema, request)

    @staticmethod
    def _intent_request(request: dict[str, Any]) -> dict[str, Any]:
        """Return stable business identity fields, excluding execution labels."""
        keys = ("industry", "nodes", "publication_mode", "target")
        return {key: request[key] for key in keys if key in request}

    def _legacy_intent_job(self, *, skill: Skill, request: dict[str, Any],
                           target: str) -> str | None:
        intent = self._intent_request(request)
        for row in self.control.state._connection().execute(
            "SELECT id,payload FROM jobs WHERE status!='superseded' ORDER BY created_at DESC"
        ):
            payload = json.loads(row["payload"])
            scope = payload.get("scope") or {}
            workflow = str(payload.get("workflow") or "")
            if (payload.get("target") == target and scope.get("skill") == skill.name
                    and workflow == skill.workflow_ref
                    and self._intent_request(scope.get("request") or {}) == intent):
                return str(row["id"])
        return None

    def _refresh_duplicate_binding(self, job_id: str, *, skill: Skill,
                                   request: dict[str, Any], request_hash: str,
                                   route_hash: str, stable_key: str) -> bool:
        stored = self.control.state.get("jobs", job_id)
        if stored is None:
            raise SkillError(f"deduplicated Job disappeared: {job_id}")
        payload = dict(stored["payload"])
        previous_hashes = tuple(payload.get("input_hashes") or ())
        changed = previous_hashes != (request_hash, route_hash)
        payload["idempotency_key"] = stable_key
        payload["input_hashes"] = [request_hash, route_hash]
        payload["scope"] = {"skill": skill.name, "skill_version": skill.version,
                            "request": request}
        payload["binding_revision"] = int(payload.get("binding_revision", 0)) + (1 if changed else 0)
        payload["route_hash"] = route_hash
        self.control.state.upsert_entity(
            "jobs", job_id, stored["status"], payload,
            program_id=stored.get("program_id"), industry_id=stored.get("industry_id"),
            workflow_id=stored.get("workflow_id"),
        )
        if not changed:
            return False
        self.control.events.append("job", job_id, "job.binding_refreshed", {
            "request_hash": request_hash, "route_hash": route_hash,
            "binding_revision": payload["binding_revision"],
        }, actor="skill-runtime")
        connection = self.control.state._connection()
        has_runs = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestrator_runs'"
        ).fetchone()
        run = (connection.execute(
            "SELECT run_id,status FROM orchestrator_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone() if has_runs else None)
        if run and (stored["status"] in {
            "failed", "repairable", "manual_review", "candidate", "stalled", "quarantined",
        } or run["status"] in {"failed", "repairable", "manual_review", "quarantined", "succeeded"}):
            from .orchestrator import PersistentOrchestrator
            PersistentOrchestrator(self.root).rewind_from(
                str(run["run_id"]), "plan",
                reason="stable Job intent rebound to refreshed Skill inputs",
                actor="skill-runtime",
            )
        return True

    def invoke(self, name: str, request: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        skill = self.registry.get(name)
        request = self.validate_request(name, request)
        request_artifact = self.control.artifacts.put_json(request, metadata={
            "schema": skill.input_schema, "skill": skill.name, "skill_version": skill.version,
        })
        route_artifact = self.control.artifacts.put_bytes(skill.route_path.read_bytes(), media_type=(
            "application/json" if skill.route_path.suffix == ".json" else "text/markdown"
        ), metadata={"schema": "lca-skill-route-v1", "skill": skill.name, "skill_version": skill.version})
        nodes = request.get("nodes") or []
        target = (f"{request['industry']}::{nodes[0]}" if request.get("industry") and len(nodes) == 1
                  else str(request.get("target") or request.get("industry") or skill.name))
        stable = hashlib.sha256(json.dumps({
            "skill": skill.name,
            "workflow_major": f"{skill.workflow_id}@{skill.workflow_version.split('.', 1)[0]}",
            "intent": self._intent_request(request),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        stable_key = idempotency_key or f"skill-intent:{stable}"
        job = Job(target=target, workflow=skill.workflow_ref,
                  scope={"skill": skill.name, "skill_version": skill.version, "request": request},
                  policy_version=skill.policy_version,
                  input_hashes=(request_artifact.digest, route_artifact.digest))
        legacy_job = None if idempotency_key else self._legacy_intent_job(
            skill=skill, request=request, target=target
        )
        if legacy_job:
            job_id, duplicate = legacy_job, True
        else:
            job_id, duplicate = self.control.submit_job(job, idempotency_key=stable_key)
        binding_refreshed = False
        if duplicate:
            binding_refreshed = self._refresh_duplicate_binding(
                job_id, skill=skill, request=request,
                request_hash=request_artifact.digest, route_hash=route_artifact.digest,
                stable_key=stable_key,
            )
        self.control.events.append("job", job_id, "skill.invoked", {
            "skill": skill.name, "skill_version": skill.version, "workflow": skill.workflow_ref,
            "request_hash": request_artifact.digest, "route_hash": route_artifact.digest,
            "stable_intent_key": stable_key, "binding_refreshed": binding_refreshed,
        }, actor="skill-runtime")
        return {"job_id": job_id, "deduplicated": duplicate, "skill": skill.name,
                "workflow": skill.workflow_ref, "policy": skill.policy_version,
                "request_hash": request_artifact.digest, "route_hash": route_artifact.digest,
                "binding_refreshed": binding_refreshed,
                "status": "accepted"}
