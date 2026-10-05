"""The registration flow: wait for the opening, poll the page, click "Anmelden", confirm."""
from __future__ import annotations

import enum
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from lxml.html import HtmlElement

from . import pages
from .config import Config
from .pages import VIENNA, PageError
from .session import NotLoggedIn, Page, TissSession
from .timing import ClockSync, measure_offset, sleep_until

log = logging.getLogger("tissqr")

TRANSIENT = (httpx.HTTPError, PageError)

LEAD_S = 1.5             # start polling this long before the opening
ARRIVE_MARGIN_S = 0.04   # one poll is timed to arrive at the server this long after the opening
KEEPALIVE_S = 300        # while waiting, reload the page this often (keeps the session alive)
RESYNC_S = 30            # re-measure the server clock this long before the opening
MAX_ATTEMPTS = 5         # register attempts once the button is visible


class Outcome(enum.Enum):
    REGISTERED = "registered"
    WAITLIST = "waitlist"
    DRY_RUN = "dry-run"
    FAILED = "failed"    # rejected (redirect), no confirmation page, or no success message


class Recorder:
    """Saves raw HTML responses (logs/<run>/NNN-label.html) for post-mortem debugging."""

    def __init__(self, directory: Path | None):
        self.dir = directory
        self.n = 0

    def save(self, label: str, page: Page | None) -> None:
        if self.dir is None or page is None:
            return
        self.n += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        head = f"<!-- {page.url} status={page.status} elapsed={page.elapsed_ms:.0f}ms -->\n".encode()
        (self.dir / f"{self.n:03d}-{label}.html").write_bytes(head + page.content)


@dataclass
class Status:
    page: Page
    doc: HtmlElement
    option: HtmlElement | None
    button: HtmlElement | None
    registered: bool
    start: datetime | None


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, VIENNA).strftime("%H:%M:%S.%f")[:-3]


def fmt_start(dt: datetime | None) -> str:
    return dt.astimezone(VIENNA).strftime("%d.%m.%Y %H:%M") if dt else "unknown"


def fmt_duration(s: float) -> str:
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m:02d}m {int(s) % 60:02d}s"


class Registrar:
    def __init__(self, cfg: Config, sess: TissSession, rec: Recorder, dry_run: bool = False):
        self.cfg = cfg
        self.sess = sess
        self.rec = rec
        self.dry_run = dry_run
        self.sync = ClockSync(0.0, 0.0, 0.1)

    # ------------------------------------------------------------------ page state

    def inspect(self) -> Status:
        page = self.sess.get(self.cfg.url)
        if page.status >= 400:
            raise PageError(f"HTTP {page.status} for {page.url}")
        doc = page.doc()
        opt = pages.find_option(doc, self.cfg.type, self.cfg.name)
        return Status(page, doc, opt,
                      button=pages.find_button(opt),
                      registered=pages.find_button(opt, pages.UNREGISTER) is not None,
                      start=pages.registration_start(opt) if opt is not None else None)

    def report(self, st: Status, list_all: bool = False) -> bool:
        """Log what we see on the page. Returns False if something is clearly wrong."""
        ok = True
        nr, sub = pages.course_number(st.doc), pages.sub_header(st.doc)
        log.info("page: %s %s | %s", nr, pages.course_title(st.doc), sub)
        if re.sub(r"\W", "", nr).upper() != self.cfg.course_nr:
            log.warning("course number on page (%s) != configured (%s)", nr, self.cfg.course)
            ok = False
        if self.cfg.semester not in sub:
            log.warning("semester %s not found in page header %r", self.cfg.semester, sub)
            ok = False
        if list_all or st.option is None:
            opts = pages.wrappers(st.doc)
            log.info("options on page (%d):", len(opts))
            for w in opts:
                btn = pages.find_button(w, pages.REGISTER + pages.UNREGISTER)
                log.info("  - %-60s start %s, button %s", pages.option_header(w)[:60],
                         fmt_start(pages.registration_start(w)), btn.get("value") if btn is not None else "-")
        if st.option is None:
            log.warning("target NOT found on page (%s)", self.cfg.describe())
            ok = False
        else:
            log.info("target: %s | start %s | register button %s%s", pages.option_header(st.option),
                     fmt_start(st.start), "VISIBLE" if st.button is not None else "not visible (yet)",
                     " | ALREADY REGISTERED" if st.registered else "")
        for m in pages.messages(st.doc):
            log.info("TISS message: %s", m)
        return ok

    # ------------------------------------------------------------------ clock

    def clock_sync(self) -> None:
        s = measure_offset(self.sess.head_root)
        if s is None:
            log.warning("could not determine the server clock offset - keeping %+.3fs", self.sync.offset)
            return
        if abs(s.offset) > 120:
            log.warning("server clock differs by %.1fs from the local clock - check NTP!", s.offset)
        log.info("server clock offset %+.3fs (+-%.3fs), best RTT %.0f ms", s.offset, s.error, s.rtt * 1000)
        self.sync = s

    def _local(self, start: datetime) -> float:
        """Local epoch time at which the server clock reaches `start`."""
        return start.timestamp() - self.sync.offset

    # ------------------------------------------------------------------ main flow

    def run(self, now: bool = False) -> int:
        log.info("target: %s", self.cfg.describe())
        log.info("url: %s", self.cfg.url)
        st = self.inspect()
        self.rec.save("start", st.page)
        self.report(st)
        if st.registered:
            log.info("already registered - nothing to do")
            return 0
        if now:
            return self._poll(None)

        start = self.cfg.start or st.start
        if start is None:
            log.error("no start time configured and none found on the page; set `start` or use --now")
            return 2
        if self.cfg.start and st.start and self.cfg.start != st.start:
            log.warning("configured start %s differs from the start shown on the page (%s)",
                        fmt_start(self.cfg.start), fmt_start(st.start))
        if start.timestamp() <= time.time():
            log.info("registration is open since %s - polling now", fmt_start(start))
            return self._poll(None)
        if start.timestamp() - time.time() > 10:   # the sync takes ~4 s
            self.clock_sync()
        log.info("registration opens at %s (server time), in %s", fmt_start(start),
                 fmt_duration(max(0.0, self._local(start) - time.time())))
        self._wait(start)
        return self._poll(start)

    def _wait(self, start: datetime) -> None:
        """Sleep until the polling starts. Reloads the page every few minutes to keep the session
        alive and re-measures the server clock shortly before the opening."""
        if self._local(start) - time.time() < 1.5 * RESYNC_S:
            return   # the clock sync in run() is recent enough
        while self._local(start) - time.time() > RESYNC_S + KEEPALIVE_S:
            time.sleep(KEEPALIVE_S)
            self._keepalive(start)
        sleep_until(self._local(start) - RESYNC_S)
        self.clock_sync()

    def _keepalive(self, start: datetime) -> None:
        try:
            st = self.inspect()
        except NotLoggedIn:
            raise
        except TRANSIENT as e:
            log.warning("page reload failed: %s", e)
            return
        log.info("%s until the opening - session ok (%.0f ms)%s",
                 fmt_duration(self._local(start) - time.time()), st.page.elapsed_ms,
                 " - register button is already visible" if st.button is not None else "")

    def _poll(self, start: datetime | None) -> int:
        """Reload the page every interval_ms (one request at a time) until the register button
        shows up, then register. With a start time, one poll is timed to arrive right after it."""
        interval = self.cfg.interval_ms / 1000
        if start is None:
            anchor = None
            next_send = time.time()
            deadline = next_send + self.cfg.window_s
        else:
            open_at = self._local(start)
            anchor = open_at + ARRIVE_MARGIN_S - self.sync.rtt / 2
            next_send = anchor - math.ceil(LEAD_S / interval) * interval
            deadline = open_at + self.cfg.window_s
        log.info("polling every %d ms until %s", self.cfg.interval_ms, clock(deadline))

        polls = attempts = 0
        rtt = self.sync.rtt
        st: Status | None = None
        while time.time() < deadline:
            if anchor is not None and next_send < anchor < next_send + rtt + 0.01:
                next_send = anchor   # that poll would still be running at the opening: wait for the opening one
            sleep_until(next_send)
            polls += 1
            sent = time.time()
            try:
                st = self.inspect()
            except NotLoggedIn:
                raise
            except TRANSIENT as e:
                log.warning("poll %d failed after %.0f ms: %s", polls, (time.time() - sent) * 1000, e)
                next_send = max(sent + interval, time.time())
                continue
            rtt = st.page.elapsed_ms / 1000
            log.debug("poll %d sent %s (server %s), %.0f ms, button=%s", polls, clock(sent),
                      clock(sent + self.sync.offset), st.page.elapsed_ms, st.button is not None)

            if st.registered:
                log.info("SUCCESS: the page shows you are registered")
                return 0
            if st.button is None:
                if polls == 1 or polls % 25 == 0:
                    log.info("poll %d: %s (%.0f ms)", polls,
                             "target not found" if st.option is None else "no register button yet",
                             st.page.elapsed_ms)
                next_send = max(next_send + interval, time.time())
                continue

            attempts += 1
            log.info("register button visible (poll %d, sent at server time %s) - attempt %d",
                     polls, clock(sent + self.sync.offset), attempts)
            try:
                out = self._attempt(st)
            except NotLoggedIn:
                raise
            except TRANSIENT as e:
                log.warning("attempt %d failed: %s", attempts, e)
                out = Outcome.FAILED
            log.info("attempt %d: %s", attempts, out.value)
            if out in (Outcome.REGISTERED, Outcome.DRY_RUN):
                return 0
            if out is Outcome.WAITLIST:
                log.warning("ended up on the WAITING LIST")
                return 3
            if attempts >= MAX_ATTEMPTS:
                log.error("giving up after %d attempts", attempts)
                break
            next_send = time.time()   # retry right away with a fresh page (new ViewState)

        if st is not None:
            self.rec.save("last-poll", st.page)
        log.error("FAILED: not registered (polls=%d, attempts=%d)", polls, attempts)
        return 1

    # ------------------------------------------------------------------ register + confirm

    def _attempt(self, st: Status) -> Outcome:
        """Click the register button, then confirm. HTML is saved afterwards, off the critical path."""
        seen = [("open-page", st.page)]
        try:
            t0 = time.perf_counter()
            res = self.sess.post(pages.build_submission(st.button))
            log.info("register click -> %s in %.0f ms",
                     "redirect " + res.redirect if res.redirect else f"HTTP {res.page.status}", res.elapsed_ms)
            if res.redirect:
                return Outcome.FAILED
            seen.append(("register-response", res.page))
            doc = res.page.doc()

            reg_form = pages.form_by_id(doc, "regForm")
            if reg_form is None:
                return self._evaluate(doc)   # no confirmation page: maybe the result is shown directly
            btn = pages.confirm_button(reg_form)
            if btn is None:
                log.error("confirmation page has no confirm button")
                return Outcome.FAILED
            fields = self._confirm_fields(reg_form)
            if self.dry_run:
                log.info("DRY RUN: confirmation page reached (button %r%s) - NOT confirming",
                         btn.get("value"), f", fields {fields}" if fields else "")
                return Outcome.DRY_RUN

            res = self.sess.post(pages.build_submission(btn, fields))
            log.info("confirm -> %s in %.0f ms (click + confirm: %.0f ms)",
                     "redirect " + res.redirect if res.redirect else f"HTTP {res.page.status}",
                     res.elapsed_ms, (time.perf_counter() - t0) * 1000)
            if res.redirect:
                return Outcome.FAILED
            seen.append(("confirm-response", res.page))
            return self._evaluate(res.page.doc())
        finally:
            for label, page in seen:
                self.rec.save(label, page)

    def _confirm_fields(self, reg_form: HtmlElement) -> dict[str, str]:
        """Study code and exam slot selection on the confirmation page (only present if needed)."""
        fields: dict[str, str] = {}
        sc = pages.select_options(reg_form, "studyCode")
        if sc:
            name, opts = sc
            if self.cfg.study_code:
                if any(v == self.cfg.study_code for v, _ in opts):
                    fields[name] = self.cfg.study_code
                else:
                    log.warning("study code %s not offered, keeping TISS default; options: %s",
                                self.cfg.study_code, opts)
            elif len(opts) > 1:
                log.warning("several study codes offered, keeping TISS default: %s", opts)

        sl = pages.select_options(reg_form, "subgrouplist")
        if sl:
            name, opts = sl
            choice = None
            if self.cfg.slot:
                tokens = [x for x in re.split(r"[\s,\-–]+", self.cfg.slot) if x]
                choice = next((v for v, txt in opts if all(tok in txt for tok in tokens)), None)
                if choice is None:
                    log.warning("slot %r not found, available: %s", self.cfg.slot, [t for _, t in opts])
            if choice is None and opts:
                choice = opts[0][0]
                log.info("using the first available slot: %s", opts[0][1])
            if choice is not None:
                fields[name] = choice
        return fields

    def _evaluate(self, doc: HtmlElement) -> Outcome:
        msg = pages.result_message(doc)
        kind = pages.classify_result(msg) if msg else "unknown"
        if msg:
            log.info("TISS result: %s", msg)
        else:
            for m in pages.messages(doc):
                log.info("TISS message: %s", m)
        if kind == "prereg":
            log.info("pre-registration recorded (the lecturer still has to confirm it)")
        if kind in ("success", "prereg", "already"):
            return Outcome.REGISTERED
        if kind == "waitlist":
            return Outcome.WAITLIST
        return Outcome.FAILED
