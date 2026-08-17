from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "vendor/lca_cornerstone/scripts/wiki_source_discovery.py"


def source_module():
    spec = importlib.util.spec_from_file_location("wiki_source_discovery", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_requires_allowlist_or_explicit_open_discovery() -> None:
    parser = source_module().build_parser()
    restricted = parser.parse_args(["plan", "claims.json", "--allow-domain", "example.com"])
    assert restricted.allow_domain == ["example.com"] and restricted.open_discovery is False
    opened = parser.parse_args(["plan", "claims.json", "--open-discovery"])
    assert opened.allow_domain is None and opened.open_discovery is True


def test_domain_policy_accepts_only_explicit_unambiguous_modes() -> None:
    module = source_module()
    assert module.normalized_domains([]) == []
    assert module.domain_allowed("public.example", []) is True
    assert module.domain_allowed("a.example.com", ["example.com"]) is True
    assert module.domain_allowed("example.net", ["example.com"]) is False


def test_research_plan_expands_bilingual_discovery_tracks(tmp_path: Path) -> None:
    module = source_module()
    claims = {
        "protocol": {"mode": "extract"},
        "claims": [
            {"claim_id": "P030-0", "node_id": "P030", "industry": "ict_equipment",
             "section": "identity", "claim_text": "x", "claim_kind": "external_fact",
             "node_identity": {"display_name": "共生焊料浮渣", "node_type": "product",
                               "facets": {}, "boundary": "foreground"},
             "believed_source": "candidate source", "believed_locator": "tin dross"},
            {"claim_id": "P030-1", "node_id": "P030", "industry": "ict_equipment",
             "section": "process", "claim_text": "y", "claim_kind": "external_fact",
             "node_identity": {"display_name": "共生焊料浮渣", "node_type": "product",
                               "facets": {}, "boundary": "foreground"},
             "believed_source": "candidate source", "believed_locator": "wave solder"},
        ],
    }
    plan = {"protocol": "wiki-research-plan-v1", "node_id": "P030",
            "languages": ["zh", "en"], "research_questions": ["identity", "process"],
            "source_classes": ["government", "technical"],
            "terminology": {"canonical_zh": "共生焊料浮渣", "candidate_aliases_zh": ["锡渣"],
                            "canonical_en": "solder dross", "candidate_aliases_en": ["tin dross"]}}
    claims_path, plan_path, output = tmp_path / "claims.json", tmp_path / "plan.json", tmp_path / "queue.json"
    claims_path.write_text(__import__("json").dumps(claims, ensure_ascii=False), encoding="utf-8")
    plan_path.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    args = module.build_parser().parse_args(["plan", str(claims_path), "--open-discovery",
                                             "--research-plan", str(plan_path), "--output", str(output)])
    assert module.command_plan(args) == 0
    queue = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert [row["research_tracks"][0]["language"] for row in queue["queries"]] == ["zh", "en"]
    assert "锡渣" in queue["queries"][0]["research_tracks"][0]["query"]
    assert "solder dross" in queue["queries"][1]["research_tracks"][0]["query"]


def test_eu_annex_locator_builds_structural_and_topic_anchors() -> None:
    anchors = source_module()._locator_anchors(
        "Annex II point 3.1(o), compatible chassis"
    )
    assert "Annex II" in anchors
    assert "point 3.1(o)" in anchors
    assert "compatible chassis" in anchors


def test_xhtml_manifestation_is_localized_in_representative_payload() -> None:
    payload = b"""<html><body><h1>ANNEX I</h1><p>(9) blade server means a
    server designed for use in a blade chassis.</p></body></html>"""
    media_type, excerpt = source_module().extract_excerpt(
        "http://publications.europa.eu/resource/example.ENG.xhtml",
        payload,
        "application/xhtml+xml",
        12_000,
        "Annex I point 9 blade server",
    )
    assert media_type == "application/xhtml+xml"
    assert "blade server" in excerpt


def test_repeated_topic_prefers_window_with_multiple_locator_anchors() -> None:
    payload = (
        b"<html><body><h1>ANNEX I</h1><p>(9) blade server means a server "
        b"designed for use in a blade chassis.</p>"
        + b"<p>unrelated material</p>" * 1000
        + b"<h1>ANNEX V</h1><p>blade server benchmark table</p></body></html>"
    )
    _, excerpt = source_module().extract_excerpt(
        "http://publications.europa.eu/resource/example.ENG.xhtml",
        payload,
        "application/xhtml+xml",
        2_000,
        "Annex I point 9 blade server",
    )
    assert "means a server designed" in excerpt
    assert "benchmark table" not in excerpt


def test_locator_function_words_cannot_outvote_product_identity_terms() -> None:
    anchors = source_module()._locator_anchors("product title and system overview")
    assert "and" not in anchors
    assert anchors == ["system"]
    payload = (
        b"<html><body><h1>Product title</h1><p>Target system overview.</p>"
        + b"<p>and system reference material</p>" * 800
        + b"</body></html>"
    )
    _, excerpt = source_module().extract_excerpt(
        "https://example.com/spec.html", payload, "text/html", 2_000,
        "product title and system overview",
    )
    assert "Product title" in excerpt
