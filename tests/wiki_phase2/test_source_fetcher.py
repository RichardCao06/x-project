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
