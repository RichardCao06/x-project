#!/usr/bin/env python3
"""Issue a current-job terminology verdict; aliases never self-confirm."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("plan",type=Path); ap.add_argument("verified",type=Path); ap.add_argument("output",type=Path); a=ap.parse_args()
    plan=json.loads(a.plan.read_text(encoding="utf-8")); verified=json.loads(a.verified.read_text(encoding="utf-8")); rows=verified.get("claims") or verified.get("result",{}).get("claims") or []
    quotes=" ".join(str((r.get("verify") or {}).get("supporting_quote", "")) for r in rows if (r.get("verify") or {}).get("verdict")=="CONFIRMED")
    terms=plan.get("terminology",{}); canonical_zh=str(terms.get("canonical_zh", "")); canonical_en=str(terms.get("canonical_en", ""))
    # Two-language equivalence requires a confirmed source excerpt explicitly containing both labels.
    bilingual=bool(canonical_zh and canonical_en and re.search(re.escape(canonical_zh),quotes,re.I) and re.search(re.escape(canonical_en),quotes,re.I))
    verdict={"protocol":"wiki-terminology-verdict-v1","node_id":plan["node_id"],"status":"CONFIRMED_EQUIVALENT" if bilingual else "UNRESOLVED","canonical_zh":canonical_zh,"canonical_en":canonical_en,"candidate_aliases_zh":terms.get("candidate_aliases_zh",[]),"candidate_aliases_en":terms.get("candidate_aliases_en",[]),"aliases_authorized_for_discovery":True,"aliases_authorized_for_identity":bilingual,"reason":"current-job bilingual primary evidence confirmed equivalence" if bilingual else "no current-job confirmed source explicitly establishes bilingual equivalence","plan_sha256":hashlib.sha256(a.plan.read_bytes()).hexdigest(),"verified_sha256":hashlib.sha256(a.verified.read_bytes()).hexdigest()}
    a.output.write_text(json.dumps(verdict,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return 0
if __name__=="__main__": raise SystemExit(main())
