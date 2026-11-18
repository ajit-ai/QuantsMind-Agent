"""Structured application logging.

Rules enforced by this module:

* API keys, ``Authorization`` headers and other credentials are **never**
  written to a log record.
* Operator loggers use the ``quantsmind_agent`` namespace.
* File logging lands in a configurable per-user directory (see
  :mod:`quantsmind_agent.persistence.paths`) with rotation.
* Debug mode adds tracebacks; normal mode shows actionable messages only.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from quantsmind_agent.persistence.paths import AppPaths, default_paths

__all__ = [
    "REDACTED",
    "get_logger",
    "redact",
    "setup_logging",
    "shutdown_logging",
]

REDACTED = "***redacted***"

#: Header names whose values must never reach a log file.
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "x-api-key",
    "x-goog-api-key",
    "cookie",
    "set-cookie",
}

#: Environment/config key names whose values must never reach a log file.
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "bearer",
}

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[^\s\"',}]+"),
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?)[^\s\"',}]+"),
)

_configured = False


def redact(value: Any) -> str:
    """Return ``value`` with any credential-looking substring removed."""

    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + REDACTED) if m.groups() else REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs credentials from a record's message and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def _sensitive(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key.lower() in _SENSITIVE_KEYS else redact(value))
        for key, value in mapping.items()
    }


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger.

    ``get_logger(__name__)`` inside the package gives
    ``quantsmind_agent.<module>``.
    """

    if not name.startswith("quantsmind_agent"):
        name = f"quantsmind_agent.{name}"
    return logging.getLogger(name)


def setup_logging(
    *,
    level: str = "INFO",
    debug: bool = False,
    log_file: Path | None = None,
    paths: AppPaths | None = None,
    file_enabled: bool = True,
    max_bytes: int = 2_000_000,
    backup_count: int = 3,
    console: bool = True,
) -> logging.Logger:
    """Configure the ``quantsmind_agent`` logger tree.

    Repeated calls are safe: handlers are replaced, not duplicated.

    Parameters
    ----------
    debug:
        Enables ``DEBUG`` level plus tracebacks on errors. This is the
        developer mode referenced in the documentation.
    log_file:
        Explicit destination; defaults to ``paths.log_file``.
    console:
        Attach a stderr handler. Turn this off under test runners.
    """

    global _configured

    resolved = paths or default_paths()
    root = logging.getLogger("quantsmind_agent")
    root.setLevel(logging.DEBUG if debug else getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    scrub = RedactingFilter()

    if console:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        stream.addFilter(scrub)
        root.addHandler(stream)

    if file_enabled:
        destination = log_file or resolved.log_file
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                destination,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError:
            root.warning("Could not open the log file at %s; continuing without it", destination)
        else:
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s [%(process)d] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            file_handler.addFilter(scrub)
            root.addHandler(file_handler)

    _configured = True
    return root


def shutdown_logging() -> None:
    """Flush and detach every handler (used on application exit and in tests)."""

    root = logging.getLogger("quantsmind_agent")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with suppress(Exception):  # closing must never break shutdown
            handler.close()


def is_configured() -> bool:
    """Whether :func:`setup_logging` has run in this process."""

    return _configured


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return ``headers`` with credential values replaced by ``***redacted***``."""

    return {
        key: (REDACTED if key.lower() in _SENSITIVE_HEADERS else redact(value))
        for key, value in headers.items()
    }


def safe_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Public alias for the sensitive-key scrubber used by the model gateway."""

    return _sensitive(mapping)
