"""Prepare bounded, auditable discovery-translation repairs for one Wiki Job."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .store import canonical


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# This is a repair vocabulary, not an identity dictionary.  Values may only be
# used to expand discovery queries.  Canonical bilingual identity still needs
# the normal terminology-verification evidence.
AUDITED_FRAGMENT_REPAIR_GLOSSARY = {
    "通用计算": "general-purpose computing",
    "刀片式": "blade form factor",
    "刀片服务器": "blade server",
    "通用服务器": "general-purpose server",
    "全闪存": "all-flash",
    "机械硬盘": "hard disk drive",
    "固态硬盘": "solid-state drive",
    "消费级": "consumer-grade",
    "企业级": "enterprise-grade",
    "独立显卡": "discrete graphics card",
    "网络交换": "network switching",
    "存储控制": "storage control",
    "系统集成": "system integration",
    "整机总装": "final system assembly",
    "机架式": "rack-mounted form factor",
    "机架": "rack-mounted",
    "服务器": "server",
    "计算": "computing",
    "通用": "general-purpose",
    "刀片": "blade",
}


def _unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def translate_fragment(fragment: str) -> str | None:
    """Translate a known fragment completely; never hide residual CJK text."""
    translated = str(fragment or "").strip()
    if not translated:
        return None
    for zh, en in sorted(
        AUDITED_FRAGMENT_REPAIR_GLOSSARY.items(), key=lambda item: len(item[0]), reverse=True
    ):
        translated = translated.replace(zh, f" {en} ")
    translated = " ".join(translated.replace("，", " ").replace(",", " ").split())
    if not translated or CJK_RE.search(translated):
        return None
    return translated


def build_repair_artifact(plan: dict[str, Any]) -> dict[str, Any]:
    """Build a Job-local override only when every reported fragment is covered."""
    terminology = plan.get("terminology") or {}
    translation = terminology.get("query_translation") or {}
    fragments = _unique(list(translation.get("unmatched_fragments") or []))
    repairs: dict[str, str] = {}
    unresolved: list[str] = []
    for fragment in fragments:
        translated = translate_fragment(fragment)
        if translated is None:
            unresolved.append(fragment)
        else:
            repairs[fragment] = translated
    source = {
        "node_id": str(plan.get("node_id") or ""),
        "node_name": str(plan.get("node_name") or ""),
        "source_terms": _unique(list(translation.get("source_terms") or [])),
        "unmatched_fragments": fragments,
        "source_plan_sha256": hashlib.sha256(canonical(plan).encode()).hexdigest(),
    }
    artifact: dict[str, Any] = {
        "protocol": "wiki-research-translation-repair-v1",
        "status": "ready" if repairs and not unresolved else "unresolved",
        "authority": "discovery_only",
        "identity_authorized": False,
        **source,
        "repairs": repairs,
        "unresolved_fragments": unresolved,
        "validation": {
            "all_fragments_covered": bool(fragments) and not unresolved,
            "english_values_have_no_cjk": bool(repairs) and all(
                not CJK_RE.search(value) for value in repairs.values()
            ),
        },
    }
    artifact["artifact_sha256"] = hashlib.sha256(canonical(artifact).encode()).hexdigest()
    return artifact


def write_repair_artifact(path: Path, artifact: dict[str, Any]) -> bool:
    """Persist atomically and report whether the causal input actually changed."""
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)
    return True
