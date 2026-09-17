"""Print bounded diagnostics for one public HTTPS URL.

This is a manual investigation tool.  It intentionally prints only a short
body prefix and never sends cookies, authorization headers, or user headers.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.services.public_web_document_service import (
    PublicWebDocumentService,
    PublicWebReadError,
)


async def _run(url: str) -> dict:
    reader = PublicWebDocumentService(repository=None)
    try:
        return await reader.diagnose_url(url=url)
    except PublicWebReadError as exc:
        return {"ok": False, "reason_code": exc.reason_code}


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a public HTTPS page")
    parser.add_argument("url")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.url)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
