from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_verify_alignment_targets_explicit_requirement_subject() -> None:
    source = (ROOT / "vendor/lca_cornerstone/scripts/run_wiki_verify_capture.py").read_text(
        encoding="utf-8"
    )
    assert "与断言实际主语对齐" in source
    assert "reference.product_identity" in source
    assert "不得仅因它相对整个活动节点属于参考产品而降为 ADJACENT" in source
