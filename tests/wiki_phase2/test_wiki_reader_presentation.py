"""Reader presentation must not destroy the underlying evidence contract."""
from __future__ import annotations

from build_wiki_bundle import assemble_body, ev_is_null, md_inline, render_typed, source_url


def test_reader_hides_repeated_modeling_badge_but_keeps_explicit_sources() -> None:
    rendered = md_inline(
        "内部边界判断。 〔建模判断〕 [^internal-review] "
        "外部事实。 ✅已核实 [^ku-1234abcd] "
        "仍缺型号数据。 〔证据缺口〕 [^internal-review]"
    )
    assert "〔建模判断〕" not in rendered
    assert rendered.count('href="#source-internal-review"') == 1
    assert rendered.count(">方法说明</a>") == 1
    assert "✅已核实" not in rendered
    assert 'href="#source-ku-1234abcd"' in rendered
    assert 'title="ku-1234abcd">来源</a>' in rendered
    assert "〔证据缺口〕" in rendered


def test_reader_uses_one_structured_source_area_not_duplicate_footnotes() -> None:
    page = """---
schema_version: wiki-v2
node_type: product
---
## 定义与产品身份

刀片服务器属于服务器。 [^ku-example]

## 出处

[^ku-example]: https://example.com/original，§1 —— 抓取摘录:「原文」
"""
    body = page.split("---", 2)[2].strip()
    rendered = assemble_body(body, page)
    assert 'href="#source-ku-example"' in rendered
    assert "刀片服务器属于服务器" in rendered
    assert "<h3>出处</h3>" not in rendered
    assert "抓取摘录" not in rendered


def test_source_url_prefers_registry_url_and_supports_legacy_locator() -> None:
    assert source_url({
        "url": "https://example.com/original",
        "locator": "Annex I point 9",
    }) == "https://example.com/original"
    assert source_url({
        "locator": "https://legacy.example/doc.pdf；p.3",
    }) == "https://legacy.example/doc.pdf"


def test_explicit_gaps_do_not_inflate_reader_value_coverage() -> None:
    page = """---
node_type: product
---
<!-- EV:quality:START -->
| field | unit | basis | 中国项目值 CN | 中国源 CN | proxy_policy | pedigree |
|---|---|---|---|---|---|---|
| BOM质量闭合 | status | reference | 缺口：没有型号级 BOM | manufacturer-spec | 不得反推 | explicit_gap |
<!-- EV:quality:END -->
"""
    rendered = render_typed(page)
    assert ev_is_null("缺口：没有型号级 BOM")
    assert "中国值 0/1" in rendered
    assert "缺口证据 1/1" in rendered
    assert "中国值 1/1" not in rendered
