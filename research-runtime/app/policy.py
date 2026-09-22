"""Network and process policy for the isolated research runtime."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data.ec2.internal"})
EXECUTABLES = frozenset({"curl", "python3", "git"})
_SHELL_TOKENS = re.compile(r"[;&|`$<>\n\r]")


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def resolve_public_addresses(hostname: str, port: int = 443) -> tuple[str, ...]:
    host = hostname.rstrip(".").casefold()
    if host in METADATA_HOSTS or host in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("private or metadata host is not allowed")
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("host resolution failed") from exc
    addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
    if not addresses or not all(is_public_ip(address) for address in addresses):
        raise ValueError("private or metadata address is not allowed")
    return addresses


def validate_public_url(url: str) -> tuple[str, tuple[str, ...]]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("only public HTTPS URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    addresses = resolve_public_addresses(parsed.hostname, parsed.port or 443)
    return parsed.geturl(), addresses


@dataclass(frozen=True)
class ExecPolicy:
    workspace: Path
    max_timeout_seconds: int = 30
    max_output_bytes: int = 256_000
    max_args: int = 24
    max_arg_chars: int = 2000

    def validate(self, argv: Iterable[str], *, timeout_seconds: int) -> tuple[str, ...]:
        values = tuple(argv)
        if not values or len(values) > self.max_args:
            raise ValueError("executable arguments are missing or too many")
        executable = Path(values[0]).name
        if executable not in EXECUTABLES:
            raise ValueError("executable is not allowed")
        if any(not isinstance(item, str) or not item or len(item) > self.max_arg_chars for item in values):
            raise ValueError("invalid executable argument")
        if any(_SHELL_TOKENS.search(item) for item in values):
            raise ValueError("shell syntax is not allowed")
        self._validate_command_semantics(values)
        if isinstance(timeout_seconds, bool) or not 1 <= int(timeout_seconds) <= self.max_timeout_seconds:
            raise ValueError("timeout is outside the allowed range")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be absolute")
        return values

    def _validate_command_semantics(self, values: tuple[str, ...]) -> None:
        executable = Path(values[0]).name
        lowered = tuple(item.casefold() for item in values)
        if executable == "curl":
            forbidden_flags = {
                "--proxy", "-x", "--connect-to", "--resolve", "--interface",
                "--config", "-k", "--insecure", "--cookie", "-b", "--cookie-jar",
                "-c", "--header", "-h", "--upload-file", "-t", "-d", "--data",
                "--data-raw", "--data-binary", "-x",
            }
            if any(item in forbidden_flags or item.startswith("--proxy=") for item in lowered):
                raise ValueError("curl network overrides, credentials and writes are not allowed")
            for item in values[1:]:
                if item.casefold().startswith(("http://", "https://")):
                    validate_public_url(item)
                if item in {"-o", "--output", "-O", "--remote-name"}:
                    raise ValueError("curl downloads must be handled by the runtime HTTP client")
        elif executable == "python3":
            joined = " ".join(lowered[1:])
            forbidden = (
                "socket", "subprocess", "requests", "httpx", "urllib", "http.client",
                "open(", "pathlib", "shutil", "__import__", "importlib", "eval(", "exec(",
                "os.environ", "os.system", "popen(",
            )
            if any(token in joined for token in forbidden):
                raise ValueError("python network, process and filesystem access is not allowed")
            if "-m" in lowered:
                module_index = lowered.index("-m")
                if module_index + 1 >= len(lowered) or lowered[module_index + 1] not in {"json.tool", "json"}:
                    raise ValueError("python module is not allowlisted")
        elif executable == "git":
            forbidden_subcommands = {"push", "fetch", "pull", "remote", "config", "credential", "submodule"}
            if any(item in forbidden_subcommands for item in lowered[1:]):
                raise ValueError("git command is not read-only")
            for item in values[1:]:
                if item.casefold().startswith(("http://", "https://")):
                    validate_public_url(item)

        for item in values[1:]:
            if item.startswith(("https://", "http://")):
                continue
            if item.startswith(("/", "\\", "..")) or "/../" in item.replace("\\", "/"):
                candidate = (self.workspace / item).resolve()
                if candidate != self.workspace and self.workspace not in candidate.parents:
                    raise ValueError("filesystem path escapes research workspace")
