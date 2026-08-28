"""Safely clean only rows named in a verified synthetic-data audit plan."""

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
from app.synthetic_data import CleanupPlanError, cleanup_from_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--backup-confirmed", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute and not args.backup_confirmed:
        parser.error("--execute requires --backup-confirmed")
    raw_url = os.environ.get(args.database_url_env, "").strip()
    if not raw_url:
        parser.error(f"environment variable {args.database_url_env} is empty")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    engine = build_engine(raw_url)
    try:
        result = cleanup_from_plan(
            engine,
            plan,
            execute=args.execute,
            backup_confirmed=args.backup_confirmed,
        )
    except CleanupPlanError as exc:
        parser.error(str(exc))
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
