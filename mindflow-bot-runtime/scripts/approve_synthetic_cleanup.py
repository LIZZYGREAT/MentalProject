"""Approve reviewed audit candidates and create a fresh dependency-safe cleanup plan."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from app.db import build_engine
from app.synthetic_data import CleanupPlanError, approve_cleanup_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--approve-id", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args()
    raw_url = os.environ.get(args.database_url_env, "").strip()
    if not raw_url:
        parser.error(f"environment variable {args.database_url_env} is empty")
    report = json.loads(args.audit_report.read_text(encoding="utf-8"))
    engine = build_engine(raw_url)
    try:
        plan = approve_cleanup_candidates(engine, report, args.approve_id)
    except CleanupPlanError as exc:
        parser.error(str(exc))
    finally:
        engine.dispose()
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(args.out),
        "approved_ids": args.approve_id,
        "plan_digest": plan["plan_digest"],
        "expected_cleanup_counts": plan["expected_cleanup_counts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
