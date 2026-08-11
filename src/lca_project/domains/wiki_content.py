"""Deterministic Wiki draft, type, Golden and preview gates."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import re
from typing import Any


PRODUCT_SECTIONS = (
    "定义与产品身份", "性质与形态", "参考流与交接边界", "规格与相邻节点区分", "在系统中的角色",
    "分类与适用范围", "节点特定采集字段", "区域化补充要求", "数据适用状态与缺口", "出处",
)
ACTIVITY_SECTIONS = (
    "定义与参考活动", "参考产品与参考单位", "单元过程边界", "技术路线与相邻活动区分", "投入产出与脊边对账",
    "直接排放、废物与监测指标边界", "节点特定采集字段", "区域化补充要求", "数据适用状态与缺口", "出处",
)


def _node_type(markdown: str) -> str | None:
    match = re.search(r"(?m)^node_type:\s*([^\s#]+)", markdown)
    return match.group(1).strip("'\"") if match else None


def _sections(markdown: str) -> list[str]:
    return [item.strip() for item in re.findall(r"(?m)^##\s+(.+?)\s*$", markdown)]


def _metrics(markdown: str) -> dict[str, int]:
    return {
        "characters": len(markdown.strip()),
        "sections": len(_sections(markdown)),
        "table_rows": sum(1 for line in markdown.splitlines() if line.lstrip().startswith("|") and "---" not in line),
        "citations": len(re.findall(r"\[\^[^\]]+\]", markdown)),
        "judgments": markdown.count("〔建模判断〕"),
    }


def validate_page_contract(markdown: str) -> dict[str, Any]:
    node_type = _node_type(markdown)
    violations: list[str] = []
    lower = markdown.lower()
    activity_markers = ("<!-- ev:flows", "<!-- ev:emissions", "<!-- ev:indicators")
    activity_language = ("直接排放", "烟气", "废水", "分配按", "共产品分配", "装置收率")
    if node_type == "product" and (any(marker in lower for marker in activity_markers)
                                   or any(marker in markdown for marker in activity_language)):
        violations.append("activity_only_content")
    if node_type not in {"product", "activity"}:
        violations.append("invalid_node_type")
    return {"go": not violations, "node_type": node_type, "violations": violations}


def validate_draft_content(markdown: str, *, baseline: str | None = None) -> dict[str, Any]:
    violations = list(validate_page_contract(markdown)["violations"])
    node_type = _node_type(markdown)
    expected = PRODUCT_SECTIONS if node_type == "product" else ACTIVITY_SECTIONS
    sections = _sections(markdown)
    if tuple(sections) != expected:
        violations.append("section_contract")
    if "〔未核实·模型回忆〕" in markdown:
        violations.append("unclassified_model_memory")
    gap_lines = [line.strip() for line in markdown.splitlines() if "证据尚未达到 CONFIRMED" in line]
    if len(gap_lines) >= 3 or (gap_lines and len(set(gap_lines)) < len(gap_lines)):
        violations.append("repeated_evidence_gap_shell")
    if baseline is not None and not compare_to_golden(baseline, markdown)["go"]:
        violations.append("non_degradation")
    return {"go": not violations, "violations": sorted(set(violations)), "blocked_before_content_apply": bool(violations)}


def compare_to_golden(golden: str, candidate: str) -> dict[str, Any]:
    base, current = _metrics(golden), _metrics(candidate)
    regressions = [key for key in base if current[key] < base[key]]
    # A plain textual baseline may not be a full Wiki page, but losing its
    # semantic tokens remains a deterministic regression signal.
    baseline_terms = {term for term in re.findall(r"[A-Za-z_]{4,}|[\u4e00-\u9fff]{2,}", golden) if term}
    missing_terms = sorted(term for term in baseline_terms if term not in candidate)
    if missing_terms:
        regressions.append("baseline_semantics")
    return {"go": not regressions, "regressions": sorted(set(regressions)), "baseline": base, "candidate": current}


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.headings: list[str] = []; self.page_state: str | None = None
        self._capture = False; self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self.page_state is None and values.get("data-page-state"):
            self.page_state = values["data-page-state"]
        if tag.lower() == "h2":
            self._capture = True; self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture: self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h2" and self._capture:
            self.headings.append("".join(self._parts).strip()); self._capture = False


def validate_preview(html: str, *, node_type: str) -> dict[str, Any]:
    parser = _HeadingParser(); parser.feed(html)
    expected = PRODUCT_SECTIONS if node_type == "product" else ACTIVITY_SECTIONS
    positions = [expected.index(item) for item in parser.headings if item in expected]
    duplicates = [item for item, count in Counter(parser.headings).items() if count > 1]
    ordered = positions == sorted(positions)
    production = parser.page_state == "production"
    violations = ([] if ordered else ["section_order"]) + ([] if not duplicates else ["duplicate_sections"])
    if production: violations.append("preview_claims_production")
    return {"go": not violations, "production": production, "headings": parser.headings, "violations": violations}

