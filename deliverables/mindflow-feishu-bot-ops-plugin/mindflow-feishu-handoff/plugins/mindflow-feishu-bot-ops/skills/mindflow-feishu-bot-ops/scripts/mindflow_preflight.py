#!/usr/bin/env python3
"""Offline-first MindFlow production preflight; never prints secret values."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable
from urllib.parse import urlparse


SECRET_NAMES = {
    "FLASK_SECRET_KEY",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
    "DEEPSEEK_API_KEY",
    "TOKEN_ENCRYPTION_KEY",
    "BOOTSTRAP_ADMIN_PASSWORD",
}
REQUIRED_FILES = (
    "compose.yaml",
    "Dockerfile",
    ".env.example",
    "entry/app.py",
    "entry/feishu_bot.py",
    "entry/care_worker.py",
    "services/event_semantics.py",
    "services/event_lifecycle.py",
    "services/proactive_care.py",
    "services/feishu_message_processor.py",
    "skills/mental-health-care/SKILL.md",
    "tests/test_event_lifecycle_bot.py",
)
REQUIRED_SCOPES = {
    "auth:user.id:read",
    "offline_access",
    "calendar:calendar:readonly",
}
PLACEHOLDER_MARKERS = ("replace-with", "changeme", "example-secret", "your-")


class Report:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, level: str, check: str, detail: str) -> None:
        self.items.append({"level": level, "check": check, "detail": detail})

    @property
    def errors(self) -> int:
        return sum(item["level"] == "ERROR" for item in self.items)

    @property
    def warnings(self) -> int:
        return sum(item["level"] == "WARN" for item in self.items)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum():
            values[key] = value.strip().strip('"').strip("'")
    return values


def enabled(values: dict[str, str], name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return values.get(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


def present(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(normalized) and not any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def is_https(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.netloc)
    except ValueError:
        return False


def check_required_files(root: Path, report: Report) -> None:
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        report.add("ERROR", "source-layout", "Missing: " + ", ".join(missing))
    else:
        report.add("PASS", "source-layout", "Required runtime and configuration files are present.")


def check_lifecycle_contract(root: Path, report: Report) -> None:
    checks = {
        "services/event_lifecycle.py": (
            "forecast_assumption",
            "completion_policy",
            "calendar_text_rule",
            "apply_user_appraisals",
        ),
        "services/prediction_service.py": (
            "prediction_input.v4",
            "forecast_as_of",
            "forward_update",
            "_splice_forward_points",
        ),
        "services/care_tools.py": (
            "care_get_event_confirmations",
            "care_record_event_outcome",
            "care_record_event_appraisal",
        ),
        "services/proactive_care.py": (
            "quiet_start",
            "max_daily_messages",
            "completion_check",
            "preemptive_care",
        ),
    }
    missing = []
    for relative, tokens in checks.items():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        content = path.read_text(encoding="utf-8-sig")
        missing.extend(f"{relative}:{token}" for token in tokens if token not in content)
    report.add(
        "ERROR" if missing else "PASS",
        "event-lifecycle-contract",
        "Missing contract markers: " + ", ".join(missing)
        if missing
        else "Temporal lifecycle, outcome tools, appraisal memory, and proactive policy markers are present.",
    )


def check_environment(values: dict[str, str], report: Report, *, require_deepseek_agent: bool) -> None:
    env_name = values.get("APP_ENV", "").lower()
    report.add("PASS" if env_name == "production" else "WARN", "app-environment", f"APP_ENV is {env_name or 'unset'}.")

    secret = values.get("FLASK_SECRET_KEY", "")
    if present(secret) and len(secret) >= 32:
        report.add("PASS", "flask-secret", "A non-placeholder secret of adequate length is configured.")
    else:
        report.add("ERROR", "flask-secret", "Set a random, stable FLASK_SECRET_KEY of at least 32 characters.")

    report.add(
        "PASS" if enabled(values, "SESSION_COOKIE_SECURE") else "ERROR",
        "secure-cookie",
        "SESSION_COOKIE_SECURE must be true behind production HTTPS.",
    )

    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        report.add(
            "PASS" if present(values.get(name, "")) else "ERROR",
            name.lower(),
            f"{name} is configured." if present(values.get(name, "")) else f"Set {name} on the server.",
        )

    for name in ("FEISHU_REDIRECT_URI", "FEISHU_FRONTEND_ORIGIN", "FEISHU_BIND_BASE_URL"):
        report.add(
            "PASS" if is_https(values.get(name, "")) else "ERROR",
            name.lower(),
            f"{name} uses a public HTTPS URL." if is_https(values.get(name, "")) else f"Set {name} to the exact public HTTPS URL.",
        )

    scopes = set(values.get("FEISHU_OAUTH_SCOPES", "").replace(",", " ").split())
    missing_scopes = sorted(REQUIRED_SCOPES - scopes)
    report.add(
        "ERROR" if missing_scopes else "PASS",
        "feishu-oauth-scopes",
        "Missing scopes: " + ", ".join(missing_scopes) if missing_scopes else "Required OAuth scopes are listed.",
    )

    report.add(
        "PASS" if enabled(values, "FEISHU_BOT_ENABLED") else "ERROR",
        "feishu-bot",
        "FEISHU_BOT_ENABLED must be true for bot deployment.",
    )
    report.add(
        "PASS" if values.get("FEISHU_BOT_TRANSPORT", "").lower() == "websocket" else "ERROR",
        "feishu-transport",
        "FEISHU_BOT_TRANSPORT must be websocket for this handoff.",
    )
    report.add(
        "PASS" if enabled(values, "FEISHU_BOT_PRIVATE_CHAT_ONLY") else "WARN",
        "private-chat-only",
        "Keep private-chat-only enabled unless group processing has separate approval.",
    )
    report.add(
        "PASS" if enabled(values, "CARE_WORKER_ENABLED", True) else "ERROR",
        "care-worker",
        "CARE_WORKER_ENABLED must be true.",
    )
    for name, lower, upper in (
        ("CARE_PROACTIVE_INTERVAL_SECONDS", 15, 3600),
        ("CARE_PROACTIVE_LEAD_MINUTES", 15, 360),
        ("CROSS_DAY_UNFINISHED_MAX_DAYS", 1, 14),
    ):
        try:
            number = int(values.get(name, ""))
            valid = lower <= number <= upper
        except ValueError:
            valid = False
        report.add(
            "PASS" if valid else "ERROR",
            name.lower(),
            f"{name} is within the reviewed range."
            if valid
            else f"Set {name} within {lower}–{upper}.",
        )
    agent_enabled = enabled(values, "CARE_AGENT_ENABLED")
    if require_deepseek_agent:
        agent_ready = agent_enabled and all(
            present(values.get(name, ""))
            for name in ("CARE_AGENT_PROVIDER", "CARE_AGENT_MODEL", "DEEPSEEK_API_KEY")
        )
        report.add(
            "WARN" if agent_ready else "ERROR",
            "deepseek-care-agent",
            "Agent configuration is present; still verify consent, allowlisted tool loop, output safety, and fallback live."
            if agent_ready
            else "Target requires the DeepSeek Care Agent; enable it and configure provider, model, and API key after implementation review.",
        )
    else:
        report.add(
            "WARN" if agent_enabled else "PASS",
            "general-care-agent",
            "Care Agent is enabled and requires separate tool-loop/privacy/safety acceptance."
            if agent_enabled
            else "Care Agent is disabled for deterministic-path deployment.",
        )

    if enabled(values, "SEMANTIC_API_ENABLED"):
        semantic_ready = all(
            present(values.get(name, ""))
            for name in ("SEMANTIC_API_URL", "SEMANTIC_API_MODEL", "DEEPSEEK_API_KEY")
        )
        report.add(
            "WARN" if semantic_ready else "ERROR",
            "deepseek-semantics",
            "External semantics is enabled; verify per-user consent at the call site and test with synthetic data."
            if semantic_ready
            else "External semantics is enabled but endpoint, model, or API key is missing.",
        )
        if enabled(values, "SEMANTIC_API_THINKING"):
            report.add("WARN", "deepseek-thinking", "Thinking mode is enabled and requires separate parser/privacy review.")
    else:
        report.add("PASS", "deepseek-semantics", "External semantics starts disabled, as recommended.")

    configured_bootstrap = present(values.get("BOOTSTRAP_ADMIN_PASSWORD", ""))
    if configured_bootstrap:
        report.add("WARN", "bootstrap-admin", "Remove bootstrap admin credentials after the initial account is created.")


def executable(names: Iterable[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def run_check(command: list[str], cwd: Path, name: str, report: Report) -> None:
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("ERROR", name, f"Could not complete: {type(exc).__name__}.")
        return
    if completed.returncode == 0:
        report.add("PASS", name, "Command completed successfully.")
    else:
        report.add("ERROR", name, f"Command failed with exit code {completed.returncode}; inspect locally redacted output.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--run-build", action="store_true")
    parser.add_argument("--run-compose-check", action="store_true")
    parser.add_argument(
        "--require-deepseek-agent",
        action="store_true",
        help="Require the target DeepSeek conversation Agent configuration.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = root / env_path
    report = Report()
    check_required_files(root, report)
    check_lifecycle_contract(root, report)

    if env_path.is_file():
        try:
            values = parse_env(env_path)
            check_environment(values, report, require_deepseek_agent=args.require_deepseek_agent)
        except (OSError, UnicodeError):
            report.add("ERROR", "environment-file", "The environment file could not be read as UTF-8.")
    else:
        report.add("ERROR", "environment-file", f"Environment file is missing: {env_path.name}")

    if args.run_tests:
        run_check([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"], root, "python-tests", report)
    if args.run_build:
        npm = executable(("npm.cmd", "npm"))
        if npm:
            run_check([npm, "run", "build"], root, "frontend-build", report)
        else:
            report.add("ERROR", "frontend-build", "npm is not available.")
    if args.run_compose_check:
        docker = executable(("docker.exe", "docker"))
        if docker:
            run_check([docker, "compose", "config", "--quiet"], root, "compose-config", report)
        else:
            report.add("ERROR", "compose-config", "Docker is not available.")

    summary = {"errors": report.errors, "warnings": report.warnings, "result": "FAIL" if report.errors else "PASS"}
    if args.format == "json":
        print(json.dumps({"summary": summary, "checks": report.items}, ensure_ascii=False, indent=2))
    else:
        for item in report.items:
            print(f"[{item['level']}] {item['check']}: {item['detail']}")
        print(f"Summary: {summary['result']} ({summary['errors']} errors, {summary['warnings']} warnings)")
        print("Secret values were not printed.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
