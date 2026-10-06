"""TISS server clock and precise sleeping."""
from __future__ import annotations

import math
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


AIM_AHEAD_S = 0.003   # aim at a moment at least this far away


def measure_offset(head_fn, samples: int = 16, budget_s: float = 6.0, clock=time.time, wait=None) -> ClockSync:
    """Estimate the server clock offset from the 1-second `Date` header.

    The server clock showed S..S+1 at some instant between our send (t0) and receive (t1),
    so the offset lies in [S - t1, S + 1 - t0]. After the first sample, each request is sent
    so that the server clock should tick to the next second while the request is under way:
    whether the answer shows the old or the new second halves the interval. If the tick is
    still ahead, the next request aims at it again, otherwise at the next second. Within a few
    seconds the interval is about as narrow as the round trip (± half the round trip).
    """
    wait = wait or sleep_until
    lo, hi, rtt = float("-inf"), float("inf"), float("inf")
    ok = tries = 0
    end = clock() + budget_s
    while ok < samples and tries < 2 * samples and clock() < end:
        if ok:
            mid, lead = (lo + hi) / 2, rtt / 2   # the server reads its clock about half a round trip after we send
            tick = math.floor(clock() + mid + lead + AIM_AHEAD_S) + 1
            send = tick - mid - lead
            if send > end:
                break
            wait(send)
        tries += 1
        try:
            resp, t0, t1 = head_fn()
            s = parsedate_to_datetime(resp.headers["date"]).timestamp()
        except Exception:
            continue
        lo, hi = max(lo, s - t1), min(hi, s + 1 - t0)
        rtt = min(rtt, t1 - t0)
        ok += 1
        if lo > hi:
            raise ClockError("your computer's clock is unstable (it jumped while measuring; common in WSL and VMs)")
    if ok < 3:
        raise ClockError("TISS did not answer")
    return ClockSync(offset=(lo + hi) / 2, error=(hi - lo) / 2, rtt=rtt)


def sleep_until(t: float) -> None:
    """Sleep until local epoch time t with sub-millisecond precision. The last 2 ms are spun,
    yielding to other threads in between."""
    while (rem := t - time.time()) > 0:
        time.sleep(rem - 0.002 if rem > 0.003 else 0)
