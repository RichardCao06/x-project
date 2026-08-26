from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

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


def test_frozen_provider_results_reject_scout_binding_drift() -> None:
    queue = {"research_scout": {"path": "/scout/repair.json", "sha256": "a" * 64},
             "queries": []}
    frozen = {
        "protocol": {"version": "wiki-frozen-search-v1", "kind": "query-search-results"},
        "backend": "configured-multi-provider-v1",
        "research_scout": {"path": "/scout/base.json", "sha256": "b" * 64},
        "queries": [], "usage": {"search_requests": 0, "cost_usd": 0.0},
    }

    with pytest.raises(ValueError, match="research scout binding"):
        source_module().frozen_search_records(frozen, queue)


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
    scout_path = tmp_path / "research-scout-diversity-repair.json"
    claims_path.write_text(__import__("json").dumps(claims, ensure_ascii=False), encoding="utf-8")
    plan_path.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    scout_path.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "P030",
        "candidates": [],
    }), encoding="utf-8")
    args = module.build_parser().parse_args(["plan", str(claims_path), "--open-discovery",
                                             "--research-plan", str(plan_path),
                                             "--research-scout", str(scout_path),
                                             "--output", str(output)])
    assert module.command_plan(args) == 0
    queue = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert [row["research_tracks"][0]["language"] for row in queue["queries"]] == ["zh", "en"]
    assert "锡渣" in queue["queries"][0]["research_tracks"][0]["query"]
    assert "solder dross" in queue["queries"][1]["research_tracks"][0]["query"]
    assert queue["research_scout"] == {
        "path": str(scout_path.resolve()),
        "sha256": hashlib.sha256(scout_path.read_bytes()).hexdigest(),
    }
def test_requirement_route_preserves_canonical_url_for_fetch_and_verify(tmp_path: Path) -> None:
    module = source_module()
    claim = {
        "claim_id": "A019-0", "node_id": "A019", "industry": "ict_equipment",
        "section": "identity", "claim_text": "x", "claim_kind": "external_fact",
        "requirement_id": "activity.identity.definition",
        "node_identity": {"display_name": "服务器 BIOS 配置", "node_type": "activity",
                          "facets": {}, "boundary": "foreground"},
        "believed_source": "AMAX Engineering — Server Manufacturing Levels Defined",
        "believed_locator": "Setting BIOS and firmware",
    }
    plan = {
        "protocol": "wiki-research-plan-v1", "node_id": "A019",
        "languages": ["zh", "en"], "research_questions": ["identity"],
        "source_classes": ["manufacturer_technical"],
        "terminology": {"canonical_zh": "服务器 BIOS 配置", "canonical_en": "server BIOS configuration"},
        "requirement_routes": {
            "activity.identity.definition": {
                "source": "AMAX Engineering — Server Manufacturing Levels Defined",
                "locator": "Rack Integration Services; Setting BIOS and firmware",
            }
        },
        "advisory_candidates": [{
            "title": "AMAX Engineering — Server Manufacturing Levels Defined",
            "url": "https://www.amax.com/server-manufacturing-levels-defined/",
            "current_job_status": "candidate_unverified",
        }],
    }
    claims_path = tmp_path / "claims.json"
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "queue.json"
    claims_path.write_text(json.dumps({"protocol": {"mode": "extract"}, "claims": [claim]}), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    args = module.build_parser().parse_args([
        "plan", str(claims_path), "--open-discovery", "--research-plan", str(plan_path),
        "--output", str(output),
    ])
    assert module.command_plan(args) == 0
    queue = json.loads(output.read_text(encoding="utf-8"))
    assert queue["queries"][0]["routed_candidates"] == [{
        "url": "https://www.amax.com/server-manufacturing-levels-defined/",
        "title": "AMAX Engineering — Server Manufacturing Levels Defined",
        "locator": "Rack Integration Services; Setting BIOS and firmware",
        "provider": "research_plan_requirement_route",
        "snippet": "",
    }]

    search_hash = queue["queries"][0]["search_hash"]

    def fake_get(url: str, **_: object) -> tuple[bytes, str, str, int]:
        assert url == "https://www.amax.com/server-manufacturing-levels-defined/"
        return (
            b"<html><body>Rack Integration Services: Setting BIOS and firmware.</body></html>",
            url, "text/html", 0,
        )

    evidence = module.execute_queue(
        queue, cache_dir=tmp_path / "cache", max_searches=20, max_fetches=40,
        max_candidates_per_claim=2, timeout=1, max_search_bytes=100_000,
        max_fetch_bytes=100_000, max_excerpt_chars=10_000, max_redirects=1,
        allowlist=[], frozen_searches={search_hash: {"status": "not_found", "results": []}},
        http_get=fake_get,
    )
    routed = evidence["claims"][0]["candidates"]
    assert len(routed) == 1
    assert routed[0]["url"] == "https://www.amax.com/server-manufacturing-levels-defined/"
    assert routed[0]["search_provider"] == "research_plan_requirement_route"
    assert "Setting BIOS and firmware" in routed[0]["excerpt"]


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


def test_chinese_locator_term_list_compiles_independent_anchors() -> None:
    anchors = source_module()._locator_anchors(
        "handoff.entry_exit_state；正文定位词：保存、恢复、二进制文件、固件和 BIOS 设置"
    )
    assert "handoff.entry_exit_state" not in anchors
    assert {"保存", "恢复", "二进制文件", "固件和 BIOS 设置"} <= set(anchors)


def test_chinese_locator_term_list_localizes_non_contiguous_terms() -> None:
    payload = (
        "<html><body><p>服务器配置实用程序可将固件和 BIOS 设置保存为二进制文件，"
        "并在需要时恢复这些设置。</p></body></html>"
    ).encode()
    _, excerpt = source_module().extract_excerpt(
        "https://example.com/server.html", payload, "text/html", 12_000,
        "handoff.entry_exit_state；正文定位词：保存、恢复、二进制文件、固件和 BIOS 设置",
    )
    assert "二进制文件" in excerpt


def test_claim_specific_locator_overrides_research_question_route() -> None:
    module = source_module()
    candidate = {"locator": "handoff.entry_exit_state.collection_state"}
    claim_locator = (
        "handoff.entry_exit_state；正文定位词：保存、恢复、二进制文件、固件和 BIOS 设置"
    )
    assert module.resolve_evidence_locator(candidate, claim_locator) == claim_locator
    assert module.resolve_evidence_locator(candidate, "") == candidate["locator"]


def test_fetch_cache_is_url_level_and_does_not_bind_first_claim_locator(tmp_path: Path) -> None:
    module = source_module()
    payload = b"<html><body>alpha evidence and beta evidence</body></html>"
    payload_path = tmp_path / "fetch.payload"
    payload_path.write_bytes(payload)
    cached = {
        "record": {
            "status": "fetched",
            "url": "https://example.com/evidence",
            "content_type": "text/html",
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    }
    assert module._cached_fetch_is_safe(cached, payload_path, [], 10_000, 10_000)
    assert "excerpt" not in cached["record"]
    assert "excerpt_locator" not in cached["record"]
    assert "alpha evidence" in module.extract_excerpt(
        cached["record"]["url"], payload, "text/html", 10_000, "alpha evidence"
    )[1]
    assert "beta evidence" in module.extract_excerpt(
        cached["record"]["url"], payload, "text/html", 10_000, "beta evidence"
    )[1]


def test_transient_fetch_failure_is_not_replayed_as_permanent_cache_hit(tmp_path: Path) -> None:
    cached = {"record": {
        "status": "error", "url": "https://example.com/evidence",
        "error": "temporary TLS failure",
    }}
    assert not source_module()._cached_fetch_is_safe(
        cached, tmp_path / "missing.payload", [], 10_000, 10_000
    )


def test_provider_snippet_fallback_is_explicit_hash_bound_and_replayable(tmp_path: Path) -> None:
    module = source_module()
    candidate = {
        "url": "https://support.example.com/server-bios",
        "title": "Server BIOS configuration",
        "snippet": "The utility saves firmware and BIOS settings as a binary file and restores them later.",
        "provider": "research_scout",
        "locator": "handoff.entry_exit_state.collection_state",
    }
    evidence = module.provider_snippet_evidence(
        candidate,
        claim_locator="handoff.entry_exit_state; locator terms: saves, binary file, BIOS settings",
        fetch_cache=tmp_path,
        max_excerpt_chars=10_000,
        rank=1,
    )
    assert evidence is not None
    assert evidence["evidence_transport"] == "search_provider_snippet"
    payload = Path(evidence["payload_path"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == evidence["content_sha256"]
    assert "binary file" in evidence["excerpt"]
