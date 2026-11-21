#!/usr/bin/env python3
"""Validate every envelope and its content-addressed source provenance."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"/"current"
OUT=ROOT/"structured"

REQUIRED={"source","doc_kind","doc_kind_confidence","languages","spec_version","extracted_by","doc_date","facts","unreadable","proposed_fact_types"}
FACT_REQUIRED={"fact_type","subject","subject_raw","attribute","value","unit","as_of","locator","evidence","confidence"}

def main():
    contract=json.loads((OUT/"contract"/"fact_types.json").read_text())
    known={x.get("name") for x in contract.get("fact_types",[])}
    errors=[]; count=0; facts=0
    docs=list((OUT/"docs").glob("*.json"))
    for path in docs:
        try: env=json.loads(path.read_text())
        except Exception as e: errors.append(f"{path.name}: invalid JSON: {e}"); continue
        count+=1
        miss=REQUIRED-set(env)
        if miss: errors.append(f"{path.name}: missing envelope fields {sorted(miss)}")
        src=env.get("source",{}); rel=src.get("path"); actual=DATA/rel if rel else None
        if not actual or not actual.is_file(): errors.append(f"{path.name}: missing source {rel}")
        else:
            digest=hashlib.sha256(actual.read_bytes()).hexdigest()
            if digest != src.get("sha256"): errors.append(f"{path.name}: source hash mismatch for {rel}")
            if actual.stat().st_size != src.get("bytes"): errors.append(f"{path.name}: source byte count mismatch for {rel}")
        if env.get("doc_kind") not in {p.stem for p in (OUT/"contract"/"doc_kinds").glob("*.md")}: errors.append(f"{path.name}: unregistered doc_kind {env.get('doc_kind')}")
        for i,f in enumerate(env.get("facts",[])):
            facts+=1; miss=FACT_REQUIRED-set(f)
            if miss: errors.append(f"{path.name} fact {i}: missing {sorted(miss)}")
            ft=f.get("fact_type")
            if ft not in known and not str(ft).startswith("x_"): errors.append(f"{path.name} fact {i}: unregistered fact_type {ft}")
            if not isinstance(f.get("subject"),dict) or not isinstance(f.get("subject_raw"),dict): errors.append(f"{path.name} fact {i}: subject must be object")
            if not isinstance(f.get("evidence"),str) or not f.get("evidence"): errors.append(f"{path.name} fact {i}: evidence must be non-empty")
    result={"ok":not errors,"documents":count,"facts":facts,"errors":errors}
    print(json.dumps(result,indent=2))
    return 0 if not errors else 1
if __name__=="__main__": sys.exit(main())
