"""Live log stream for dashboard debug tests."""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Optional

_lock = threading.Lock()
_buffers: dict[str, deque[str]] = {}
_handlers: dict[str, logging.Handler] = {}
_broadcast: Optional[Callable[[str, str], None]] = None


class _DebugLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(("GramAddict", "dashboard"))


class DebugLogHandler(logging.Handler):
    def __init__(self, serial: str) -> None:
        super().__init__()
        self.serial = serial
        self.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname).1s | %(message)s",
                datefmt="%I:%M:%S %p",
            )
        )
        self.addFilter(_DebugLogFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append(self.serial, self.format(record))
        except Exception:
            self.handleError(record)


def set_broadcast(fn: Optional[Callable[[str, str], None]]) -> None:
    global _broadcast
    _broadcast = fn


def append(serial: str, line: str) -> None:
    with _lock:
        buf = _buffers.setdefault(serial, deque(maxlen=800))
        buf.append(line)
    if _broadcast:
        try:
            _broadcast(serial, line)
        except Exception:
            pass


def start_session(serial: str) -> None:
    stop_session(serial)
    with _lock:
        _buffers[serial] = deque(maxlen=800)
    handler = DebugLogHandler(serial)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    logging.getLogger("GramAddict").setLevel(logging.DEBUG)
    _handlers[serial] = handler
    append(serial, f"— debug log started ({serial}) —")


def stop_session(serial: str) -> None:
    handler = _handlers.pop(serial, None)
    if handler is None:
        return
    root = logging.getLogger()
    root.removeHandler(handler)
    handler.close()
    append(serial, "— debug log ended —")


def get_lines(serial: str, since: int = 0) -> tuple[list[str], int]:
    with _lock:
        lines = list(_buffers.get(serial, []))
    if since < 0:
        since = 0
    if since >= len(lines):
        return [], len(lines)
    chunk = lines[since:]
    return chunk, len(lines)


def trace(serial: Optional[str], message: str) -> None:
    if not serial:
        return
    append(serial, message)
