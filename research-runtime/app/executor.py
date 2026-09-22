"""Bounded argv-only process execution for public-data processing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Sequence

from .policy import ExecPolicy


@dataclass(frozen=True)
class ExecResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SafeExecutor:
    def __init__(self, policy: ExecPolicy) -> None:
        self.policy = policy
        self.policy.workspace.mkdir(parents=True, exist_ok=True)

    async def run(self, argv: Sequence[str], *, timeout_seconds: int = 30) -> ExecResult:
        values = self.policy.validate(argv, timeout_seconds=timeout_seconds)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.policy.workspace),
            "TMPDIR": "/tmp",
        }

        def execute() -> ExecResult:
            try:
                completed = subprocess.run(
                    values,
                    cwd=self.policy.workspace,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
                return ExecResult(
                    values,
                    completed.returncode,
                    completed.stdout[: self.policy.max_output_bytes].decode("utf-8", "replace"),
                    completed.stderr[: self.policy.max_output_bytes].decode("utf-8", "replace"),
                )
            except subprocess.TimeoutExpired as exc:
                return ExecResult(
                    values,
                    -9,
                    (exc.stdout or b"")[: self.policy.max_output_bytes].decode("utf-8", "replace")
                    if isinstance(exc.stdout, bytes) else str(exc.stdout or "")[: self.policy.max_output_bytes],
                    (exc.stderr or b"")[: self.policy.max_output_bytes].decode("utf-8", "replace")
                    if isinstance(exc.stderr, bytes) else str(exc.stderr or "")[: self.policy.max_output_bytes],
                    timed_out=True,
                )

        return await asyncio.to_thread(execute)

