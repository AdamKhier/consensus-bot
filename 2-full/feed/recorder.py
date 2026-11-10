"""Append-only JSONL recorder.

Raw ticks go to a day-rotated file; completed windows go to a separate
`rounds` file. Splitting them means the round labels stay readable and
incompressible small even when the tick firehose is gigabytes, and it keeps the
training set trivially loadable.

Writes are flushed per line. A process killed mid-window loses at most the
in-flight line, which matters because this thing is meant to survive being run
for days.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import IO

from .types import Tick
from .windows import WindowResult


@dataclass
class Recorder:
    directory: str = "data"
    record_ticks: bool = True
    _tick_file: IO[str] | None = None
    _tick_day: str = ""
    rounds_path: str = ""
    ticks_written: int = 0
    rounds_written: int = 0
    _closed: bool = False

    def __post_init__(self) -> None:
        os.makedirs(self.directory, exist_ok=True)
        self.rounds_path = os.path.join(self.directory, "rounds.jsonl")
        if self.record_ticks:
            self._open_tick_file()

    def _open_tick_file(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day == self._tick_day and self._tick_file is not None:
            return
        self._close_tick_file()
        self._tick_day = day
        self._tick_file = open(  # noqa: SIM115 - lifetime owned by this object
            os.path.join(self.directory, f"ticks-{day}.jsonl"),
            "a",
            buffering=1,
        )

    def _close_tick_file(self) -> None:
        if self._tick_file is not None:
            try:
                self._tick_file.close()
            except OSError:
                pass
            self._tick_file = None

    def write_tick(self, tick: Tick) -> None:
        if not self.record_ticks or self._tick_file is None or self._closed:
            return
        try:
            self._tick_file.write(json.dumps(tick.to_dict(), separators=(",", ":")) + "\n")
            self.ticks_written += 1
        except OSError:
            pass

    def write_round(self, result: WindowResult) -> None:
        if self._closed:
            return
        try:
            with open(self.rounds_path, "a", buffering=1) as handle:
                handle.write(json.dumps(result.to_dict(), separators=(",", ":")) + "\n")
            self.rounds_written += 1
        except OSError:
            pass

    def write_event(self, kind: str, **fields) -> None:
        """Record a non-tick event (errors, reconnects, startup)."""
        if self._closed:
            return
        payload = {"type": kind, "ts": time.time(), **fields}
        try:
            with open(os.path.join(self.directory, "events.jsonl"), "a", buffering=1) as handle:
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def close(self) -> None:
        self._close_tick_file()
        self._closed = True

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def load_rounds(path: str) -> list[dict]:
    """Load completed rounds, skipping any truncated final line."""
    out: list[dict] = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out
