from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional


LogSink = Callable[[str], None]


@dataclass
class Logger:
    sinks: List[LogSink]

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def _write(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        for s in self.sinks:
            s(line)


def make_logger(sink: Optional[LogSink] = None) -> Logger:
    sinks: list[LogSink] = []
    if sink is not None:
        sinks.append(sink)
    return Logger(sinks=sinks)

