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


def _stdout_sink(line: str) -> None:
    print(line)


def make_logger(sink: Optional[LogSink] = None) -> Logger:
    """默认 sink 为 stdout——修复此前无 sink 时 INFO/WARN 日志全部被吞掉的问题。

    测试如需静默 logger，请显式传入一个收集型 sink。
    """
    sinks: list[LogSink] = [sink] if sink is not None else [_stdout_sink]
    return Logger(sinks=sinks)

