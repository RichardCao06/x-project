#!/usr/bin/env python3
"""Bounded compose -> independent editorial review -> feedback repair loop."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("section_schema", type=Path)
    parser.add_argument("review_schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd-per-call", type=float, required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--workers", type=int, default=1,
                        help="bounded section-agent concurrency; defaults to 1 for transport stability")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    attempts = []
    feedback: Path | None = None
    for number in range(1, max(1, args.max_attempts) + 1):
        content_dir = out / f"content-attempt-{number:02d}"
        content_command = [sys.executable, str(root / "run_wiki_content_sectioned_capture.py"),
                           str(args.verify_output.resolve()), str(args.blueprint.resolve()),
                           str(args.section_schema.resolve()), str(content_dir),
                           "--timeout-seconds", str(args.timeout_seconds), "--workers", str(max(1, args.workers))]
        if feedback:
            content_command.extend(["--editorial-feedback", str(feedback)])
        content_exit = run(content_command)
        record = {"attempt": number, "content_exit": content_exit, "content_dir": str(content_dir)}
        attempts.append(record)
        if content_exit != 0:
            continue
        review_dir = out / f"editorial-review-{number:02d}"
        review_exit = run([
            sys.executable, str(root / "run_wiki_editorial_review_capture.py"),
            str(args.verify_output.resolve()), str(content_dir / "content-result.json"),
            str(args.blueprint.resolve()), str(args.review_schema.resolve()), str(review_dir),
            "--cost-usd", str(args.cost_usd_per_call),
            "--timeout-seconds", str(args.timeout_seconds),
        ])
        record.update({"review_exit": review_exit, "review_dir": str(review_dir)})
        feedback = review_dir / "editorial-review.json"
        if review_exit == 0:
            shutil.copy2(content_dir / "content-result.json", out / "content-result.json")
            shutil.copy2(feedback, out / "editorial-review.json")
            report = {"protocol": "wiki-content-editorial-loop-v1", "verdict": "GO",
                      "attempts": attempts, "selected_attempt": number}
            (out / "loop-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
            return 0
    report = {"protocol": "wiki-content-editorial-loop-v1", "verdict": "NO_GO", "attempts": attempts}
    (out / "loop-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
