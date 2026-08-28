"""Immutable build identity injected into deployable images."""

from __future__ import annotations

import logging
import os


BUILD_REVISION = os.environ.get("BUILD_REVISION", "development").strip() or "development"


def announce_build(component: str) -> None:
    logging.getLogger("mindflow.build").warning(
        "component=%s build_revision=%s", component, BUILD_REVISION
    )
