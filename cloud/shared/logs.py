"""Logging setup shared by the API and the worker.

``text`` for a terminal, ``json`` for journald and log shippers: one JSON object
per line with ``ts``, ``level``, ``logger``, ``message`` and any structured
fields passed as ``extra={"fields": {...}}``. Tokens, passwords and connection
strings are never logged. :func:`redact` strips credentials from any URL that
does end up in a message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

__all__ = ["JsonFormatter", "configure_logging", "redact"]

_CREDENTIALS_IN_URL = re.compile(r"([a-z][a-z0-9+.-]*://)([^:/@\s]+):([^@\s]+)@", re.IGNORECASE)
_BEARER = re.compile(r"(bearer\s+)[a-z0-9._\-]+", re.IGNORECASE)


def redact(text: str) -> str:
    return _BEARER.sub(r"\1[redacted]", _CREDENTIALS_IN_URL.sub(r"\1\2:[redacted]@", text))


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self._service,
            "env": self._environment,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update({key: value for key, value in fields.items() if key not in payload})
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(*, fmt: str, level: str, service: str, environment: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(service=service, environment=environment))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):  # replaced by the API's own structured access log
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = False
