"""Clock synchronisation against the TISS server and precise sleeping."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

log = logging.getLogger("tissreg")


@dataclass
class ClockSync:
    offset: float      # server_time - local_time, seconds
    error: float       # +- seconds (half width of the bound interval)
    rtt: float         # best observed round trip, seconds


def measure_offset(head_fn, samples: int = 24, spacing: float = 0.07) -> ClockSync | None:
    """Estimate the server clock offset from the 1s-resolution `Date` header.

    Each response tells us: the server clock read S..S+1 at some instant between our
    send (t0) and receive (t1). So offset is in [S - t1, S + 1 - t0]. Intersecting these
    intervals over samples taken at different sub-second phases narrows it down to
    roughly the one-way latency.
    """
    lo, hi, rtt = float("-inf"), float("inf"), float("inf")
    ok = 0
    for _ in range(samples):
        try:
            resp, t0, t1 = head_fn()
        except Exception as e:  # noqa: BLE001 - best effort
            log.debug("clock sample failed: %s", e)
            continue
        d = resp.headers.get("date")
        if not d:
            continue
        s = parsedate_to_datetime(d).timestamp()
        lo, hi = max(lo, s - t1), min(hi, s + 1 - t0)
        rtt = min(rtt, t1 - t0)
        ok += 1
        time.sleep(spacing + random.uniform(0, spacing))
    if ok < 3:
        return None
    if lo > hi:  # inconsistent (several frontends with different clocks?)
        log.warning("server clock samples inconsistent (lo=%.3f hi=%.3f), ignoring", lo, hi)
        return None
    return ClockSync(offset=(lo + hi) / 2, error=(hi - lo) / 2, rtt=rtt)


def sleep_until(t: float) -> None:
    """Sleep until local epoch time t with ~1ms precision."""
    while True:
        rem = t - time.time()
        if rem <= 0:
            return
        if rem > 0.05:
            time.sleep(rem - 0.02)
        elif rem > 0.002:
            time.sleep(rem / 2)
        # else: spin for the last couple of ms
