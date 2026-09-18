"""Entrypoint for the standalone research runtime container."""

from __future__ import annotations

import os
import uvicorn

from .server import app


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("RESEARCH_BIND_HOST", "0.0.0.0"), port=int(os.environ.get("RESEARCH_PORT", "8080")))

