"""
Structured logging setup with optional JSON output and redaction.
- Console handler + rotating file handler
- JSON formatter by default; pretty text if LOG_JSON=false
- Redacts emails and bearer-like tokens if enabled in settings

Example:
    from hermes.logging_utils import configure_logging, get_logger
    configure_logging()
    log = get_logger(__name__)
    log.info("service started", extra={"component": "triage"})
"""
from __future__ import annotations

import json
import logging
import logging.handlers as handlers
import re
import sys
from datetime import datetime, timezone
from typing import Any

from hermes.config import settings


EMAIL_RE = re.compile(r"(?i)([a-z0-9._%+-]+)@([a-z0-9.-]+\.[a-z]{2,})")
TOKEN_RE = re.compile(r"(?i)\b(eyJ[\w-]+\.[\w-]+\.[\w-]+|sk-[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_-]{20,})\b")


class RedactingFilter(logging.Filter):
    """Redact emails and token-like strings in log messages and extras."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        """Mutate the record in-place, replacing sensitive string patterns."""
        if not settings.redact_emails_in_logs:
            return True
        # Message
        if isinstance(record.msg, str):
            record.msg = EMAIL_RE.sub("***@***", record.msg)
            record.msg = TOKEN_RE.sub("***TOKEN***", record.msg)
        # Args (positional)
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                TOKEN_RE.sub("***TOKEN***", EMAIL_RE.sub("***@***", a)) if isinstance(a, str) else a for a in record.args
            )
        # Extra dicts (common in structured logging)
        for attr in ("extra", "__dict__"):
            payload = getattr(record, attr, None)
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, str):
                        payload[k] = TOKEN_RE.sub("***TOKEN***", EMAIL_RE.sub("***@***", v))
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter for logs."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        """Render a log record as a JSON object string."""
        base: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include extras if present
        # Python logging tucks extras into record.__dict__ beyond standard attrs
        std = set(vars(logging.LogRecord("x", 0, "x", 0, "", (), None)).keys())
        extras = {k: v for k, v in record.__dict__.items() if k not in std}
        if extras:
            # Avoid unserializable objects
            safe_extras = {k: _safe(v) for k, v in extras.items()}
            base.update(safe_extras)
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable single-line formatter for console output."""

    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        """Render timestamp, level, logger name, and message as plain text."""
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        msg = record.getMessage()
        return f"{ts} | {record.levelname:<7} | {record.name} | {msg}"


def _safe(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except Exception:
        return repr(v)


_configured = False


def configure_logging(force: bool = False) -> None:
    """Set up console + rotating file handlers, JSON or text based on settings.

    Call once at app startup. Safe to call multiple times with force=True.
    """
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)

    filt = RedactingFilter()

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.addFilter(filt)
    ch.setLevel(level)
    ch.setFormatter(JsonFormatter() if settings.log_json else TextFormatter())
    root.addHandler(ch)

    # File handler (rotating)
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = handlers.RotatingFileHandler(
            settings.log_path, maxBytes=settings.log_max_bytes, backupCount=settings.log_backup_count
        )
        fh.addFilter(filt)
        fh.setLevel(level)
        fh.setFormatter(JsonFormatter())  # Always JSON in file for easier parsing
        root.addHandler(fh)
    except Exception:
        # If filesystem not writable, keep going with console only
        root.warning("file handler disabled (log_path not writable)")

    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger, configuring the global logging system on first use."""
    if not _configured:
        configure_logging()
    return logging.getLogger(name if name else __name__)


# --- tiny smoke test when run directly ---
if __name__ == "__main__":
    configure_logging(force=True)
    log = get_logger("hermes.smoke")
    log.info("hello from logging", extra={"component": "smoke", "user": "jane.doe@example.com"})
    log.error("token leak? sk-abcdef1234567890", extra={"note": "should be redacted"})
