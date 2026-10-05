"""The registration flow: warm up, wait, poll around the opening, click + confirm."""
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
from .pages import PageError
from .session import NotLoggedIn, Page, TissSession
from .timing import ClockSync, measure_offset, sleep_until

log = logging.getLogger("tissreg")

TRANSIENT = (httpx.HTTPError, PageError)


class Outcome(enum.Enum):
    REGISTERED = "registered"
    WAITLIST = "waitlist"
    DRY_RUN = "dry-run"
    REJECTED = "rejected"   # TISS answered with a redirect (stale ViewState, closed, full, ...)
    UNKNOWN = "unknown"


class Recorder:
    """Saves raw HTML responses for post-mortem debugging."""

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


class Registrar:
    def __init__(self, cfg: Config, sess: TissSession, rec: Recorder, dry_run: str | None = None):
        self.cfg = cfg
        self.t = cfg.target
        self.sess = sess
        self.rec = rec
        self.dry_run = dry_run          # None | "detect" | "confirm"
        self.sync = ClockSync(0.0, 0.0, 0.1)

    # ------------------------------------------------------------------ helpers

    def inspect(self, save_as: str | None = None) -> Status:
        page = self.sess.get(self.t.url)
        if save_as:
            self.rec.save(save_as, page)
        if page.status >= 400:
            raise PageError(f"HTTP {page.status} for {page.url}")
        doc = page.doc()
        opt = pages.find_option(doc, self.t.type, self.t.name, self.t.exam_date, self.t.option_id)
        btn = pages.find_button(opt, self.cfg.labels.register)
        registered = pages.find_button(opt, self.cfg.labels.unregister) is not None
        start = pages.registration_start(opt) if opt is not None else None
        return Status(page, doc, opt, btn, registered, start)

    def _safe_inspect(self, label: str) -> Status | None:
        try:
            st = self.inspect(save_as=label if label != "keepalive" else None)
            log.info("%s ok (%.0f ms)%s", label, st.page.elapsed_ms,
                     " - button already visible!" if st.button is not None else "")
            return st
        except NotLoggedIn:
            raise
        except TRANSIENT as e:
            log.warning("%s failed: %s", label, e)
            return None

    def clock_sync(self) -> None:
        if not self.cfg.schedule.use_server_clock:
            return
        s = measure_offset(self.sess.head_root)
        if s is None:
            log.warning("could not determine server clock offset - using local clock")
            return
        if abs(s.offset) > 120:
            log.warning("server clock differs by %.1fs from local clock - check NTP! Using server clock.",
                        s.offset)
        log.info("server clock offset %+.3fs (+-%.3fs), best RTT %.0f ms", s.offset, s.error, s.rtt * 1000)
        self.sync = s

    def server_now(self) -> float:
        return time.time() + self.sync.offset

    def _fmt(self, ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]

    def report(self, st: Status, list_all: bool = False) -> bool:
        """Log what we see on the page. Returns False if something is clearly wrong."""
        ok = True
        nr = pages.course_number(st.doc)
        sub = pages.sub_header(st.doc)
        log.info("page: %s %s | %s", nr, pages.course_title(st.doc), sub)
        if re.sub(r"\W", "", nr).upper() != self.t.course_nr:
            log.warning("course number on page (%s) != configured (%s)", nr, self.t.course)
            ok = False
        if self.t.semester not in sub:
            log.warning("semester %s not found in page header %r", self.t.semester, sub)
            ok = False
        opts = pages.wrappers(st.doc)
        if list_all or st.option is None:
            log.info("options on page (%d):", len(opts))
            for w in opts:
                btn = pages.find_button(w, self.cfg.labels.register + self.cfg.labels.unregister)
                start = pages.registration_start(w)
                log.info("  - %-40s id=%s start=%s button=%s", pages.option_header(w)[:80],
                         pages.option_id(w) or "-", start.strftime("%d.%m.%Y %H:%M") if start else "-",
                         btn.get("value") if btn is not None else "-")
        if st.option is None:
            log.warning("target option NOT found on page (%s)", self.t.describe())
            ok = False
        else:
            log.info("target: %s | id=%s | start=%s | register button: %s | registered: %s",
                     pages.option_header(st.option)[:100], pages.option_id(st.option) or "-",
                     st.start.isoformat() if st.start else "unknown",
                     "VISIBLE" if st.button is not None else "not yet", st.registered)
        for m in pages.messages(st.doc):
            log.info("TISS message: %s", m)
        return ok

    # ------------------------------------------------------------------ registration steps

    def _confirm_overrides(self, reg_form: HtmlElement) -> dict[str, str]:
        ov: dict[str, str] = {}
        sc = pages.select_options(reg_form, "studyCode")
        if sc:
            name, opts = sc
            if self.t.study_code:
                if any(v == self.t.study_code for v, _ in opts):
                    ov[name] = self.t.study_code
                else:
                    log.warning("study code %s not offered, options: %s", self.t.study_code, opts)
            elif len(opts) > 1:
                log.warning("several study codes offered, keeping TISS default: %s", opts)
        elif self.t.study_code:
            ov["regForm:studyCode"] = self.t.study_code

        sl = pages.select_options(reg_form, "subgrouplist")
        if sl:
            name, opts = sl
            choice = None
            if self.t.slot:
                tokens = [x for x in re.split(r"[\s,\-–]+", self.t.slot) if x]
                choice = next((v for v, txt in opts if all(tok in txt for tok in tokens)), None)
                if choice is None:
                    log.warning("slot %r not found, available: %s", self.t.slot, [t for _, t in opts])
            if choice is None and opts:
                choice = opts[0][0]
                log.warning("using first available slot: %s", opts[0][1])
            if choice is not None:
                ov[name] = choice
        return ov

    def _evaluate(self, page: Page) -> Outcome:
        doc = page.doc()
        msg = pages.result_message(doc)
        kind = pages.classify_result(msg) if msg else "unknown"
        if msg:
            log.info("TISS result: %s", msg)
        else:
            for m in pages.messages(doc):
                log.info("TISS message: %s", m)
        if kind in ("success", "prereg", "already"):
            if kind == "prereg":
                log.info("pre-registration recorded (needs confirmation by the lecturer)")
            return Outcome.REGISTERED
        if kind == "waitlist":
            return Outcome.WAITLIST
        return Outcome.UNKNOWN

    def attempt(self, st: Status) -> Outcome:
        assert st.button is not None
        if self.dry_run == "detect":
            log.info("DRY RUN: register button %r (%s) is clickable - not clicking",
                     st.button.get("value"), st.button.get("name"))
            return Outcome.DRY_RUN

        t0 = time.perf_counter()
        sub = pages.build_submission(st.button)
        res = self.sess.post(sub)
        log.info("register click -> %s in %.0f ms",
                 "redirect " + res.redirect if res.redirect else f"HTTP {res.page.status}", res.elapsed_ms)
        if res.redirect:
            return Outcome.REJECTED
        self.rec.save("register-response", res.page)
        doc = res.page.doc()

        reg_form = pages.form_by_id(doc, "regForm")
        if reg_form is None:
            # no confirmation page: maybe the result is shown directly
            return self._evaluate(res.page)
        btn = pages.confirm_button(reg_form, self.cfg.labels.register)
        if btn is None:
            log.error("confirmation page has no confirm button")
            return Outcome.UNKNOWN
        ov = self._confirm_overrides(reg_form)
        if self.dry_run == "confirm":
            log.info("DRY RUN: confirmation page reached (button %r, extra fields %s) - NOT confirming",
                     btn.get("value"), ov)
            return Outcome.DRY_RUN

        sub = pages.build_submission(btn, ov)
        res = self.sess.post(sub)
        log.info("confirm -> %s in %.0f ms (total %.0f ms)",
                 "redirect " + res.redirect if res.redirect else f"HTTP {res.page.status}",
                 res.elapsed_ms, (time.perf_counter() - t0) * 1000)
        if res.redirect:
            return Outcome.REJECTED
        self.rec.save("confirm-response", res.page)
        return self._evaluate(res.page)

    # ------------------------------------------------------------------ main flow

    def run(self, now: bool = False) -> int:
        log.info("target: %s", self.t.describe())
        log.info("url: %s", self.t.url)
        st = self.inspect(save_as="warmup")
        self.report(st)
        if st.registered:
            log.info("already registered - nothing to do")
            return 0

        start = None if now else (self.cfg.schedule.start or st.start)
        if not now and start is None:
            log.error("no start time configured and none found on the page; set schedule.start or use --now")
            return 2
        if self.cfg.schedule.start and st.start and self.cfg.schedule.start != st.start:
            log.warning("configured start %s differs from start shown on page %s",
                        self.cfg.schedule.start.isoformat(), st.start.isoformat())

        self.clock_sync()
        if start is not None:
            log.info("registration opens at %s (server time)", start.isoformat())
            if start.timestamp() + self.cfg.schedule.window_s < self.server_now():
                log.warning("start time is already past the polling window - starting now")
                start = None
            else:
                self._wait(start)
        return self._burst(start)

    def _open_local(self, start: datetime) -> float:
        return start.timestamp() - self.sync.offset

    def _wait(self, start: datetime) -> None:
        sch = self.cfg.schedule
        next_ka = time.time() + sch.keepalive_s
        did_sync = self._open_local(start) - time.time() <= 90
        did_warm = False
        while True:
            rem = self._open_local(start) - time.time()
            if rem <= sch.lead_ms / 1000 + 2.0:
                return
            if not did_sync and rem <= 90:
                self.clock_sync()
                did_sync = True
                continue
            if not did_warm and rem <= 12:
                self._safe_inspect("prewarm")   # fresh session + warm TLS connection
                did_warm = True
                continue
            if time.time() >= next_ka and rem > 30:
                h, m = divmod(int(rem) // 60, 60)
                log.info("waiting: %dh %02dm %02ds until opening", h, m, int(rem) % 60)
                self._safe_inspect("keepalive")
                next_ka = time.time() + sch.keepalive_s
                continue
            checkpoints = [rem - sch.lead_ms / 1000 - 2.0, next_ka - time.time()]
            if not did_sync:
                checkpoints.append(rem - 90)
            if not did_warm:
                checkpoints.append(rem - 12)
            upcoming = [c for c in checkpoints if c > 0]
            time.sleep(min(60.0, max(0.05, min(upcoming) if upcoming else 0.05)))

    def _burst(self, start: datetime | None) -> int:
        sch = self.cfg.schedule
        interval = sch.interval_ms / 1000
        if start is not None:
            open_l = self._open_local(start)
            # send one poll so that it *arrives* at the server just after the opening
            anchor = open_l + sch.arrive_margin_ms / 1000 - self.sync.rtt / 2
            next_send = anchor - math.ceil(sch.lead_ms / sch.interval_ms) * interval
            deadline = open_l + sch.window_s
        else:
            anchor = None
            next_send = time.time()
            deadline = time.time() + sch.window_s
        log.info("polling every %d ms until %s", sch.interval_ms, self._fmt(deadline))

        polls = attempts = 0
        last_elapsed = 0.15
        st: Status | None = None
        while time.time() < deadline:
            now = time.time()
            if anchor is not None and now < anchor and next_send < anchor \
                    and next_send + last_elapsed > anchor - 0.01:
                next_send = anchor   # keep the slot right at the opening free
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
            last_elapsed = st.page.elapsed_ms / 1000
            log.debug("poll %d sent %s (server %s) %.0f ms option=%s button=%s", polls, self._fmt(sent),
                      self._fmt(sent + self.sync.offset), st.page.elapsed_ms,
                      st.option is not None, st.button is not None)

            if st.registered:
                log.info("SUCCESS: page shows you are registered")
                return 0
            if st.button is None:
                if polls == 1 or polls % 25 == 0:
                    log.info("poll %d: %s (%.0f ms)", polls,
                             "option not found" if st.option is None else "no register button yet",
                             st.page.elapsed_ms)
                next_send = max(next_send + interval, time.time())
                continue

            attempts += 1
            log.info("register button visible (poll %d, sent at server time %s) - attempt %d",
                     polls, self._fmt(sent + self.sync.offset), attempts)
            self.rec.save("open-page", st.page)
            try:
                out = self.attempt(st)
            except NotLoggedIn:
                raise
            except TRANSIENT as e:
                log.warning("attempt %d failed: %s", attempts, e)
                out = Outcome.UNKNOWN
            log.info("attempt %d outcome: %s", attempts, out.value)
            if out is Outcome.DRY_RUN:
                return 0
            if out is Outcome.REGISTERED:
                self._verify()
                return 0
            if out is Outcome.WAITLIST:
                log.warning("ended up on the WAITING LIST")
                return 3
            if attempts >= self.cfg.max_attempts:
                log.error("giving up after %d attempts", attempts)
                break
            next_send = time.time()   # retry immediately with a fresh page/ViewState

        if st is not None:
            self.rec.save("last-poll", st.page)
        log.error("FAILED: not registered (polls=%d, attempts=%d)", polls, attempts)
        return 1

    def _verify(self) -> None:
        try:
            st = self.inspect(save_as="verify")
            log.info("verification: registered=%s", st.registered)
        except Exception as e:  # noqa: BLE001
            log.warning("verification request failed: %s", e)
