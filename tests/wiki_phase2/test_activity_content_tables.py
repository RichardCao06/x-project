from __future__ import annotations

import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[2] / "vendor/lca_cornerstone/scripts/wiki_content_enrich.py"
    spec = importlib.util.spec_from_file_location("wiki_content_enrich", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_activity_blueprint_renders_all_six_required_table_contracts() -> None:
    blueprint = {"node_id": "A039", "node_type": "activity", "evidence_tables": {
        "flows": ["P003 output", "parts input", "electricity", "packaging"],
        "props": ["identity", "model", "mass", "handoff"],
        "emissions": ["air", "water", "waste"],
        "indicators": ["yield", "rework", "energy"],
        "params": ["p1", "p2", "p3", "p4", "p5", "p6"],
        "quality": ["q1", "q2", "q3", "q4", "q5"],
    }}
    rendered = module().render_evidence_tables(blueprint)
    for kind in ("flows", "props", "params", "emissions", "indicators", "quality"):
        assert f"<!-- EV:{kind}:START -->" in rendered
    assert "P003 成品级刀片服务器边界" not in rendered
    assert "A039 单元过程数据" in rendered
