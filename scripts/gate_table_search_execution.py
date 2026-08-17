#!/usr/bin/env python3
"""Gate table search execution; planned rows never count as searched."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

TERMINAL={"found","not_found","fetched","verified","rejected","error","budget_skipped"}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("matrix",type=Path); ap.add_argument("output",type=Path); ap.add_argument("--allow-partial",action="store_true"); a=ap.parse_args()
    m=json.loads(a.matrix.read_text(encoding="utf-8")); rows=m.get("queries",[]); statuses=[r.get("status") for r in rows]
    executed=[s for s in statuses if s in TERMINAL]; success=[s for s in statuses if s in {"found","not_found","fetched","verified","rejected"}]
    complete=bool(rows) and len(success)==len(rows); partial=bool(success)
    decision="PASS" if complete else ("PARTIAL" if a.allow_partial and partial else "BLOCKED")
    fetched=[c for r in rows for c in r.get("results",[]) if c.get("fetch_status")=="fetched"]
    out={"protocol":"wiki-table-search-execution-gate-v1","decision":decision,"checks":{"queries_exist":bool(rows),"planned_is_not_executed":all(s!="planned" for s in statuses),"all_successfully_terminal":complete,"some_executed":partial,"fetched_payloads_hash_bound":all(bool(c.get("content_sha256")) and Path(c.get("payload_path","")).is_file() for c in fetched)},"counts":{"total":len(rows),"attempted":len(executed),"successful_terminal":len(success),"planned":statuses.count("planned"),"fetched":len(fetched),"failed":sum(s in {"error","budget_skipped"} for s in statuses)},"matrix_sha256":hashlib.sha256(a.matrix.read_bytes()).hexdigest()}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return 0 if decision in {"PASS","PARTIAL"} else 2
if __name__=="__main__": raise SystemExit(main())
