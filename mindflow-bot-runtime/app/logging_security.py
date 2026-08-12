"""Process-wide redaction for credentials embedded in log messages."""

from __future__ import annotations

import logging
import re
from typing import Any


_QUERY_SECRET = re.compile(r"(?i)(access_key|ticket)=([^&\s]+)")
_installed = False


def _redact(value: Any) -> Any:
    return _QUERY_SECRET.sub(r"\1=[redacted]", value) if isinstance(value, str) else value


def install_credential_redaction() -> None:
    """Redact SDK URL query credentials before any handler sees a record."""

    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.msg = _redact(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: _redact(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(_redact(value) for value in record.args)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
