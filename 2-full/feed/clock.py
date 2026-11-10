"""Clock synchronisation against an exchange server.

Window boundaries are defined in exchange-server time, not local time. If the
local clock is even 200ms fast, every 5-minute open price we freeze is sampled
from the wrong instant. So we measure the offset and use corrected timestamps
everywhere downstream.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from statistics import median


@dataclass
class ClockOffset:
    """Estimated (server_time - local_time) in milliseconds."""

    offset_ms: float = 0.0
    samples: int = 0
    rtt_ms: float = 0.0
    last_sync: float = 0.0
    history: list[float] = field(default_factory=list)

    def now(self) -> float:
        """Current time in server epoch seconds."""
        return time.time() + self.offset_ms / 1000.0

    def now_ms(self) -> int:
        return int(self.now() * 1000)


def sync_offset(
    url: str,
    *,
    samples: int = 5,
    timeout: float = 10.0,
) -> ClockOffset:
    """Estimate clock offset using N round trips and the median of estimates.

    The median discards samples distorted by a slow round trip, which matters
    more than it sounds: a single 2-second stall would otherwise skew the offset
    by a full second and misalign every window.
    """
    clock = ClockOffset()
    for _ in range(samples):
        before = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read()
        except OSError:
            continue
        after = time.time()
        try:
            server_ms = int(json.loads(payload)["serverTime"])
        except (ValueError, KeyError, TypeError):
            continue
        offset = server_ms - (before + after) / 2 * 1000
        clock.history.append(offset)
        clock.rtt_ms = (after - before) * 1000
        time.sleep(0.2)

    if clock.history:
        clock.offset_ms = median(clock.history)
        clock.samples = len(clock.history)
    clock.last_sync = time.time()
    return clock
