from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_wiki_content_stage.py"
SPEC = importlib.util.spec_from_file_location("run_wiki_content_stage", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_validation_feedback_is_machine_generated_and_claim_scoped(tmp_path: Path) -> None:
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"validation_error": "未 CONFIRMED: A039-13 and A039-8"}), encoding="utf-8")
    output = MODULE._validation_feedback(usage, 1, tmp_path / "feedback.json")
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["failed_claim_ids"] == ["A039-13", "A039-8"]
    assert result["failure_code"] == "DETERMINISTIC_CONTENT_GATE"
