"""Deterministic capability registry.

Capabilities are declarative manifests, not prompt text.  This gives the
orchestrator a small, auditable allow-list of executable work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Capability:
    id: str
    version: str
    command: tuple[str, ...]
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    retryable_codes: frozenset[str] = frozenset()
    side_effects: str = "none"
    description: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Capability":
        # Accept the public Capability.v1 protocol.  The older ``id/command``
        # spelling remains readable for frozen run manifests.
        if "capability_id" in raw:
            raw = dict(raw)
            raw["id"] = raw["capability_id"]
            executor = raw.get("executor")
            entrypoint = raw.get("entrypoint")
            # A public manifest may provide an explicit protocol command.  It
            # must contain {input}/{output}; raw legacy entrypoints are only a
            # compatibility fallback and are not production-ready.
            if raw.get("command"):
                pass
            elif executor == "python" and entrypoint:
                raw["command"] = ["{python}", str(entrypoint)]
            elif executor == "agent":
                raw["command"] = ["{agent_runtime}", str(entrypoint)]
            elif entrypoint:
                raw["command"] = [str(entrypoint)]
            if raw.get("side_effects") in {"staged_apply", "release_apply"}:
                raw["side_effects"] = "staged"
            # External schemas are referenced by stable IDs; inline schemas
            # are still supported by the executor.
            for key in ("input_schema", "output_schema"):
                if isinstance(raw.get(key), str):
                    raw[key] = {"$ref": raw[key]}
        required = ("id", "version", "command")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise RegistryError(f"capability missing: {', '.join(missing)}")
        command = raw["command"]
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise RegistryError(f"{raw['id']}: command must be a non-empty string list")
        if raw.get("production_ready") is True and not ({"{input}", "{output}"} <= set(command)):
            raise RegistryError(f"{raw['id']}: production-ready command must bind {{input}} and {{output}}")
        timeout = int(raw.get("timeout_seconds", 300))
        if timeout < 1 or timeout > 3600:
            raise RegistryError(f"{raw['id']}: timeout_seconds must be 1..3600")
        side_effects = raw.get("side_effects", "none")
        if side_effects not in {"none", "staged"}:
            raise RegistryError(f"{raw['id']}: side_effects must be none or staged")
        return cls(
            id=str(raw["id"]), version=str(raw["version"]), command=tuple(command),
            input_schema=dict(raw.get("input_schema", {})), output_schema=dict(raw.get("output_schema", {})),
            timeout_seconds=timeout, retryable_codes=frozenset(raw.get("retryable_codes", [])),
            side_effects=side_effects, description=str(raw.get("description", "")),
        )


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        prior = self._items.get(capability.id)
        if prior and prior != capability:
            raise RegistryError(f"duplicate capability id: {capability.id}")
        self._items[capability.id] = capability

    def get(self, capability_id: str) -> Capability:
        try:
            return self._items[capability_id]
        except KeyError as exc:
            raise RegistryError(f"capability not registered: {capability_id}") from exc

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    @classmethod
    def load_directory(cls, directory: str | Path) -> "CapabilityRegistry":
        registry = cls()
        for path in sorted(Path(directory).glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                registry.register(Capability.from_mapping(json.load(handle)))
        return registry
