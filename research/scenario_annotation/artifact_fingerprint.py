"""Small shared SHA-256 helpers for immutable Stage 1 artifact binding."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_fileset(paths: Iterable[str | Path]) -> str:
    digest = hashlib.sha256()
    resolved = sorted((Path(value).resolve() for value in paths), key=lambda item: item.as_posix())
    for path in resolved:
        encoded = path.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def fingerprint(path: str | Path) -> dict[str, str]:
    resolved = Path(path).resolve()
    return {"path": resolved.as_posix(), "sha256": sha256_file(resolved)}


def verify_fingerprint(entry: Mapping[str, Any]) -> str | None:
    path = Path(str(entry.get("path", "")))
    if not path.exists():
        return f"missing artifact: {path}"
    actual = sha256_file(path)
    expected = str(entry.get("sha256", ""))
    if actual != expected:
        return f"artifact changed: {path} (expected {expected}, actual {actual})"
    return None

