#!/usr/bin/env python3
"""Bounded compose/review/repair loop for one frozen Wiki node.

This process owns every model wait.  It emits one terminal report and never
requires a chat agent to poll or decide whether another attempt is warranted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


CLAIM_RE = re.compile(r"([PA]\d{3}-\d+)")


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, check=False).returncode


def _validation_feedback(usage_path: Path, attempt: int, output: Path) -> Path:
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    error = str(usage.get("validation_error") or "content contract failed")
    claims = sorted(set(CLAIM_RE.findall(error)))
    feedback = {
        "protocol": "wiki-content-repair-feedback-v1", "attempt": attempt,
        "failure_code": "DETERMINISTIC_CONTENT_GATE", "validation_error": error,
        "failed_claim_ids": claims,
        "required_repairs": [
            "逐项修复确定性校验错误，不得降低 Content Blueprint 的深度阈值。",
            "任何非 CONFIRMED external_fact 不得进入事实正文或 evidence_claim_ids。",
            "保持已确认 claim 的语义边界，不得为通过 Gate 编造新事实。",
        ],
    }
    output.write_text(json.dumps(feedback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("content_schema", type=Path)
    parser.add_argument("review_schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd-per-call", type=float, required=True)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--content-model", default="gpt-5.6-terra")
    parser.add_argument("--content-reasoning-effort", default="medium")
    parser.add_argument("--review-transport-attempts", type=int, default=2)
    parser.add_argument("--initial-feedback", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 3:
        raise ValueError("--max-attempts must be between 1 and 3")
    project_root = Path(__file__).resolve().parents[1]
    scripts = project_root / "vendor" / "lca_cornerstone" / "scripts"
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []
    model_calls = 0
    feedback = args.initial_feedback.resolve() if args.initial_feedback else None
    for number in range(1, args.max_attempts + 1):
        content_dir = out / f"content-attempt-{number:02d}"
        content_cmd = [sys.executable, str(scripts / "run_wiki_content_capture.py"),
                       str(args.verify_output.resolve()), str(args.blueprint.resolve()),
                       str(args.content_schema.resolve()), str(content_dir),
                       "--cost-usd", str(args.cost_usd_per_call),
                       "--timeout-seconds", str(args.timeout_seconds),
                       "--model", args.content_model,
                       "--reasoning-effort", args.content_reasoning_effort]
        if feedback:
            content_cmd.extend(["--editorial-feedback", str(feedback)])
        content_exit = _run(content_cmd)
        model_calls += 1
        record = {"attempt": number, "content_exit": content_exit, "content_dir": str(content_dir),
                  "content_model": args.content_model}
        attempts.append(record)
        if content_exit != 0:
            usage = content_dir / "content-usage.json"
            if usage.is_file():
                feedback = _validation_feedback(usage, number, out / f"repair-feedback-{number:02d}.json")
            continue
        review_exit, review = 1, None
        for review_attempt in range(1, max(1, args.review_transport_attempts) + 1):
            review_dir = out / f"editorial-review-{number:02d}-{review_attempt:02d}"
            review_exit = _run([sys.executable, str(scripts / "run_wiki_editorial_review_capture.py"),
                                str(args.verify_output.resolve()), str(content_dir / "content-result.json"),
                                str(args.blueprint.resolve()), str(args.review_schema.resolve()), str(review_dir),
                                "--cost-usd", str(args.cost_usd_per_call),
                                "--timeout-seconds", str(args.timeout_seconds)])
            model_calls += 1
            record.setdefault("review_attempts", []).append({"attempt": review_attempt,
                "exit": review_exit, "review_dir": str(review_dir)})
            review = review_dir / "editorial-review.json"
            if review_exit != 1:
                break
        record.update({"review_exit": review_exit, "review_dir": str(review.parent)})
        if review_exit == 0:
            shutil.copy2(content_dir / "content-result.json", out / "content-result.json")
            shutil.copy2(review, out / "editorial-review.json")
            report = {"protocol": "wiki-content-stage-v1", "verdict": "GO", "attempts": attempts,
                      "selected_attempt": number, "model_calls": model_calls}
            (out / "stage-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False)); return 0
        if review and review.is_file():
            feedback = review
        elif review_exit == 1:
            break
    report = {"protocol": "wiki-content-stage-v1", "verdict": "NO_GO", "attempts": attempts,
              "model_calls": model_calls}
    (out / "stage-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
