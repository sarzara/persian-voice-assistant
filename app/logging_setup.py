"""Structured logging: one JSON object per line, with correlation IDs from context variables.

- ``request_id``: one per Telegram update (``tg-<update_id>``), HTTP request or MCP call.
- ``run_id``: set as soon as an agent run exists, then carried through agent, tools and
  executor. ``asyncio`` tasks and ``to_thread`` copy context, so it follows the work.

Grep a single request end to end:  docker compose logs bot | grep '"run_id": 42'
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import sys
import uuid

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
run_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("run_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


def new_request_id(prefix: str = "req") -> str:
    rid = f"{prefix}-{uuid.uuid4().hex[:12]}"
    request_id_var.set(rid)
    return rid


def set_run_id(run_id: int | None) -> None:
    run_id_var.set(run_id)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.run_id = run_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "run_id": getattr(record, "run_id", None),
        }
        # Anything passed via logger.info(..., extra={...}).
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in data and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s [%(name)s] req=%(request_id)s run=%(run_id)s %(message)s")


def configure_logging(service: str, level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)  # stderr: stdout is the MCP stdio channel
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Telegram request URLs contain the bot token: never log them.
    for noisy in ("httpx", "httpcore", "httpx2", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Route uvicorn's loggers through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
