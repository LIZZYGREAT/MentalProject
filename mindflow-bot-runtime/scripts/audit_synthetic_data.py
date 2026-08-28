"""Read-only report of synthetic-looking forecast, warning, and care rows."""

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
from app.synthetic_data import audit_synthetic_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--report-out", type=Path, help="optional path for the signed full audit report")
    parser.add_argument("--plan-out", type=Path, help="optional path for the signed cleanup plan")
    args = parser.parse_args()
    raw_url = os.environ.get(args.database_url_env, "").strip()
    if not raw_url:
        parser.error(f"environment variable {args.database_url_env} is empty")
    engine = build_engine(raw_url)
    try:
        report = audit_synthetic_data(engine)
    finally:
        engine.dispose()
    if args.plan_out:
        args.plan_out.write_text(
            json.dumps(report["cleanup_plan"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.report_out:
        args.report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
