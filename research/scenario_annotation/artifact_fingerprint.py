"""Content fingerprints with repository-relative artifact identities."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logical_path(path: str | Path, repository_root: str | Path) -> str:
    """Return a stable POSIX path and reject artifacts outside the repository."""
    resolved = Path(path).resolve()
    root = Path(repository_root).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact is outside the repository root: {resolved}") from exc
    return relative.as_posix()


def sha256_fileset(
    paths: Iterable[str | Path], *, repository_root: str | Path
) -> str:
    """Hash sorted repository-relative paths and their content digests."""
    entries = sorted(
        (logical_path(path, repository_root), Path(path).resolve()) for path in paths
    )
    digest = hashlib.sha256()
    for path_name, path in entries:
        encoded = path_name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def fingerprint(path: str | Path, *, repository_root: str | Path) -> dict[str, str]:
    resolved = Path(path).resolve()
    return {
        "logical_path": logical_path(resolved, repository_root),
        "sha256": sha256_file(resolved),
    }


def verify_fingerprint(
    entry: Mapping[str, str], *, repository_root: str | Path
) -> str | None:
    path_name = PurePosixPath(str(entry.get("logical_path", "")))
    if path_name.is_absolute() or not path_name.parts or ".." in path_name.parts:
        return f"invalid logical artifact path: {path_name}"
    root = Path(repository_root).resolve()
    path = (root / Path(*path_name.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return f"artifact resolves outside repository root: {path_name}"
    if not path.is_file():
        return f"missing artifact: {path_name}"
    actual = sha256_file(path)
    expected = str(entry.get("sha256", ""))
    if actual != expected:
        return f"artifact changed: {path_name} (expected {expected}, actual {actual})"
    return None
