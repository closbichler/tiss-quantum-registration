"""TISS server clock and precise sleeping."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class ClockError(Exception):
    pass


@dataclass
class ClockSync:
    offset: float   # server time - local time, seconds
    error: float    # +- seconds
    rtt: float      # best round trip, seconds


def measure_offset(head_fn, samples: int = 24, spacing: float = 0.07) -> ClockSync:
    """Estimate the server clock offset from the 1-second `Date` header.

    The server clock showed S..S+1 at some instant between our send (t0) and receive (t1),
    so the offset lies in [S - t1, S + 1 - t0]. Intersecting these intervals over samples
    at different sub-second phases narrows it down to about the one-way latency.
    """
    lo, hi, rtt = float("-inf"), float("inf"), float("inf")
    ok = 0
    for _ in range(samples):
        try:
            resp, t0, t1 = head_fn()
            s = parsedate_to_datetime(resp.headers["date"]).timestamp()
        except Exception:
            continue
        lo, hi = max(lo, s - t1), min(hi, s + 1 - t0)
        rtt = min(rtt, t1 - t0)
        ok += 1
        time.sleep(spacing + random.uniform(0, spacing))
    if ok < 3:
        raise ClockError("TISS did not answer")
    if lo > hi:
        raise ClockError("your computer's clock is unstable (it jumped while measuring; common in WSL and VMs)")
    return ClockSync(offset=(lo + hi) / 2, error=(hi - lo) / 2, rtt=rtt)


def sleep_until(t: float) -> None:
    """Sleep until local epoch time t with ~1 ms precision."""
    while (rem := t - time.time()) > 0:
        if rem > 0.05:
            time.sleep(rem - 0.02)
        elif rem > 0.002:
            time.sleep(rem / 2)
