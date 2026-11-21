#!/usr/bin/env python3
"""Run the deterministic reconciliation self-check and print its report."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from build_structured import run_check
if __name__=="__main__":
    import json
    print(json.dumps(run_check(),indent=2,ensure_ascii=False))
