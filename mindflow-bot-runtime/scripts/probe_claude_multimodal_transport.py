"""Report reproducible evidence for MindFlow's Path-B image transport choice.

This probe is intentionally offline and never prints credentials. It verifies
the installed SDK, bundled CLI, query contract, exported block types, and the
configured model name. A native path is considered stable only when the pinned
SDK exposes a typed image block contract that can be passed through its public
client API.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
import os
from pathlib import Path
from typing import get_type_hints


def _configured_model(project_root: Path) -> str:
    value = str(os.environ.get("CLAUDE_MODEL") or "").strip()
    if value:
        return value
    env_path = project_root / ".env"
    if not env_path.is_file():
        return "<not configured>"
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("CLAUDE_MODEL="):
            return line.split("=", 1)[1].strip().strip('"\'') or "<not configured>"
    return "<not configured>"


def main() -> int:
    import claude_agent_sdk as sdk
    from claude_agent_sdk import _cli_version

    package_root = Path(sdk.__file__).resolve().parent
    public_image_types = sorted(
        name for name in dir(sdk) if "image" in name.lower()
    )
    source_image_markers: list[str] = []
    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "imageblock" in source or '"type": "image"' in source:
            source_image_markers.append(str(path.relative_to(package_root)))
    bundled_cli = package_root / "_bundled" / (
        "claude.exe" if os.name == "nt" else "claude"
    )
    query_signature = str(inspect.signature(sdk.ClaudeSDKClient.query))
    user_content_annotation = str(
        get_type_hints(sdk.UserMessage).get("content", "<unknown>")
    )
    stable_native_image_contract = bool(
        public_image_types and "Image" in user_content_annotation
    )
    evidence = {
        "claude_agent_sdk_version": importlib.metadata.version(
            "claude-agent-sdk"
        ),
        "bundled_cli_version": getattr(
            _cli_version, "__cli_version__", "<unknown>"
        ),
        "bundled_cli_present": bundled_cli.is_file(),
        "query_signature": query_signature,
        "public_user_message_content": user_content_annotation,
        "public_image_types": public_image_types,
        "internal_image_markers": source_image_markers,
        "internal_image_marker_note": (
            "Internal markers may describe MCP tool-result images; they are not a public user-input contract."
        ),
        "configured_model": _configured_model(Path(__file__).resolve().parents[1]),
        "stable_native_image_contract": stable_native_image_contract,
        "selected_path": "A" if stable_native_image_contract else "B",
        "reason": (
            "Pinned SDK exposes a public typed native-image contract."
            if stable_native_image_contract
            else "Pinned SDK and bundled CLI expose no public typed native-image block contract; use Generic Vision and pass validated text context to Claude."
        ),
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
