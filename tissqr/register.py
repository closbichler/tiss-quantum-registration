"""The registration flow: wait for the opening, reload the page, click "Anmelden", confirm."""
from __future__ import annotations

import enum
import logging
import math
import re
import sys
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
from .timing import ClockError, ClockSync, measure_offset, sleep_until

log = logging.getLogger("tissqr")

TRANSIENT = (httpx.HTTPError, PageError)

LEAD_S = 1.5             # start reloading this long before the opening
ARRIVE_MARGIN_S = 0.02   # one reload arrives this long after the opening (plus the clock uncertainty)
KEEPALIVE_S = 300        # while waiting, reload the page this often to keep the session alive
RESYNC_S = 30            # measure the TISS clock again this long before the opening
MAX_ATTEMPTS = 5

NOT_FOUND_HINT = {
    "course": " - this course has no registration in TISS (yet)",
    "group": " - set `name` to one of the groups listed above",
    "exam": " - `name` must match a part of one of the lines above",
}


class Outcome(enum.Enum):
    REGISTERED = "registered"
    WAITLIST = "waitlist"
    DRY_RUN = "dry run"
    FAILED = "failed"


class Recorder:
    """Saves TISS pages into the run's log folder."""

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


def hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, VIENNA).strftime("%H:%M:%S.%f")[:-3]


def fmt_start(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(VIENNA).strftime("%d.%m.%Y %H:%M:%S" if dt.second else "%d.%m.%Y %H:%M")


def fmt_duration(seconds: float) -> str:
    s = int(max(0.0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


class Registrar:
    def __init__(self, cfg: Config, sess: TissSession, rec: Recorder, dry_run: bool = False):
        self.cfg = cfg
        self.sess = sess
        self.rec = rec
        self.dry_run = dry_run
        self.sync = ClockSync(0.0, 0.0, 0.1)

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
        """Tell the user what the page shows. Returns False if something is wrong."""
        ok = True
        nr, sub = pages.course_number(st.doc), pages.sub_header(st.doc)
        log.info("✔ logged in")
        log.info("✔ %s %s (%s)", nr, pages.course_title(st.doc), sub)
        if re.sub(r"\W", "", nr).upper() != self.cfg.course_nr:
            log.error("✘ TISS shows course %s instead of %s", nr, self.cfg.course)
            ok = False
        if self.cfg.semester not in sub:
            log.error("✘ TISS shows %s instead of semester %s", sub.split(",")[0], self.cfg.semester)
            ok = False
        if list_all or st.option is None:
            self._list_options(st.doc)
        for m in pages.messages(st.doc):
            log.info("  TISS: %s", m)

        what = self.cfg.describe()
        if st.option is None:
            log.error("✘ %s is not on the page%s", what, NOT_FOUND_HINT[self.cfg.type])
            return False
        if st.registered:
            log.info("✔ you are already registered for %s", what)
        elif st.button is not None:
            log.info("✔ found %s - registration is open", what)
        else:
            log.info("✔ found %s - registration is not open yet", what)
        return ok

    def _list_options(self, doc: HtmlElement) -> None:
        opts = pages.wrappers(doc)
        log.info("  on this page:" if opts else "  no registrations on this page")
        for w in opts:
            btn = pages.find_button(w, pages.REGISTER + pages.UNREGISTER)
            log.info("    %s (start %s%s)", pages.option_header(w), fmt_start(pages.registration_start(w)),
                     f', button "{btn.get("value")}"' if btn is not None else "")

    def clock_sync(self) -> None:
        try:
            s = measure_offset(self.sess.head_root)
        except ClockError as e:
            log.warning("! could not measure the TISS clock: %s - the timing may be off", e)
            return
        self.sync = s
        if abs(s.offset) < 0.05:
            log.info("✔ your clock matches the TISS clock (±%.2f s)", s.error)
        else:
            log.info("✔ your clock is %.2f s %s the TISS clock - this is compensated (±%.2f s)",
                     abs(s.offset), "ahead of" if s.offset < 0 else "behind", s.error)

    def _local(self, start: datetime) -> float:
        """Local time at which the TISS clock reaches `start`."""
        return start.timestamp() - self.sync.offset

    def run(self) -> int:
        st = self.inspect()
        self.rec.save("start", st.page)
        self.report(st)
        if st.registered:
            return 0

        start = self.cfg.start or st.start
        if self.cfg.start and st.start and self.cfg.start != st.start:
            log.warning("! start in the config (%s) differs from TISS (%s) - using the config",
                        fmt_start(self.cfg.start), fmt_start(st.start))
        if start is None and st.button is None:
            log.error("✘ TISS shows no start time - set `start` in the config")
            return 2
        if start is None or start.timestamp() <= time.time():
            return self._poll(None)
        if start.timestamp() - time.time() > 10:   # measuring takes ~4 s
            self.clock_sync()
        log.info("  registration opens %s, in %s - keep this running",
                 fmt_start(start), fmt_duration(self._local(start) - time.time()))
        self._wait(start)
        return self._poll(start)

    def _wait(self, start: datetime) -> None:
        """Sleep until shortly before the opening, keeping the session alive and the clock in sync."""
        if self._local(start) - time.time() > 1.5 * RESYNC_S:
            while self._local(start) - time.time() > RESYNC_S + KEEPALIVE_S:
                self._sleep(time.time() + KEEPALIVE_S, start)
                self._keepalive(start)
            self._sleep(self._local(start) - RESYNC_S, start)
            self.clock_sync()
        self._sleep(self._local(start) - LEAD_S - 0.5, start)

    def _sleep(self, until: float, start: datetime) -> None:
        """Sleep until `until`. On a terminal, show a live countdown to the opening."""
        tty = sys.stdout.isatty()
        while (rem := until - time.time()) > 0:
            if tty:
                print(f"\r  opens in {fmt_duration(self._local(start) - time.time())}   ", end="", flush=True)
            time.sleep(min(1.0, rem))
        if tty:
            print("\r" + " " * 40 + "\r", end="", flush=True)

    def _keepalive(self, start: datetime) -> None:
        try:
            st = self.inspect()
        except NotLoggedIn:
            raise
        except TRANSIENT as e:
            log.warning("! could not reach TISS (%s) - trying again in %d min", e, KEEPALIVE_S // 60)
            return
        log.info("  %s left - still logged in%s", fmt_duration(self._local(start) - time.time()),
                 ", the register button is already there" if st.button is not None else "")

    def _poll(self, start: datetime | None) -> int:
        """Reload the page every interval_ms (one request at a time) until the register button
        appears, then register. With a start time, one reload arrives right after the opening."""
        interval = self.cfg.interval_ms / 1000
        if start is None:
            anchor = None
            next_send = time.time()
            deadline = next_send + self.cfg.window_s
        else:
            open_at = self._local(start)
            anchor = open_at + ARRIVE_MARGIN_S + self.sync.error - self.sync.rtt / 2
            next_send = anchor - math.ceil(LEAD_S / interval) * interval
            deadline = open_at + self.cfg.window_s
        log.info("  reloading the page every %d ms until the register button appears", self.cfg.interval_ms)

        polls = attempts = 0
        rtt = self.sync.rtt
        st: Status | None = None
        while time.time() < deadline:
            if anchor is not None and next_send < anchor < next_send + rtt + 0.01:
                next_send = anchor   # that reload would still be running at the opening
            sleep_until(next_send)
            polls += 1
            sent = time.time()
            try:
                st = self.inspect()
            except NotLoggedIn:
                raise
            except TRANSIENT as e:
                log.warning("! reload %d failed: %s", polls, e)
                next_send = max(sent + interval, time.time())
                continue
            rtt = st.page.elapsed_ms / 1000
            log.debug("reload %d sent %s (TISS time %s), %.0f ms, button=%s", polls, hms(sent),
                      hms(sent + self.sync.offset), st.page.elapsed_ms, st.button is not None)

            if st.registered:
                log.info("✔ REGISTERED - the page shows you as registered")
                return 0
            if st.button is None:
                if polls % 25 == 0:
                    log.info("  no register button yet (%d reloads)", polls)
                next_send = max(next_send + interval, time.time())
                continue

            attempts += 1
            log.info("✔ the register button is there (TISS time %s) - registering", hms(sent + self.sync.offset))
            try:
                out = self._attempt(st)
            except NotLoggedIn:
                raise
            except TRANSIENT as e:
                log.warning("! request failed: %s", e)
                out = Outcome.FAILED
            if out is Outcome.REGISTERED:
                after = f" - {time.time() + self.sync.offset - start.timestamp():.2f} s after the opening" if start else ""
                log.info("✔ REGISTERED%s", after)
                return 0
            if out is Outcome.DRY_RUN:
                log.info("✔ DRY RUN finished at the confirmation page - you are NOT registered")
                return 0
            if out is Outcome.WAITLIST:
                log.warning("! you are on the WAITING LIST")
                return 3
            if attempts >= MAX_ATTEMPTS:
                break
            log.warning("! attempt %d did not work - trying again with a fresh page", attempts)
            next_send = time.time()

        if st is not None:
            self.rec.save("last-poll", st.page)
        if attempts:
            log.error("✘ NOT registered - %d attempt(s) failed", attempts)
        else:
            log.error("✘ NOT registered - no register button until %s (closed or full?)", hms(deadline))
        return 1

    def _attempt(self, st: Status) -> Outcome:
        """Click the register button and confirm. The pages are saved afterwards, off the critical path."""
        seen = [("open-page", st.page)]
        try:
            t0 = time.perf_counter()
            res = self.sess.post(pages.build_submission(st.button))
            if res.redirect:
                log.warning("! TISS rejected the click (%.0f ms)", res.elapsed_ms)
                log.debug("redirected to %s", res.redirect)
                return Outcome.FAILED
            seen.append(("register-response", res.page))
            doc = res.page.doc()

            reg_form = pages.form_by_id(doc, "regForm")
            if reg_form is None:
                return self._evaluate(doc)
            btn = pages.confirm_button(reg_form)
            if btn is None:
                log.warning("! the confirmation page has no confirm button")
                return Outcome.FAILED
            log.info("  confirmation page (%.0f ms)", res.elapsed_ms)
            fields = self._confirm_fields(reg_form)
            if self.dry_run:
                return Outcome.DRY_RUN

            res = self.sess.post(pages.build_submission(btn, fields))
            if res.redirect:
                log.warning("! TISS rejected the confirmation (%.0f ms)", res.elapsed_ms)
                log.debug("redirected to %s", res.redirect)
                return Outcome.FAILED
            seen.append(("confirm-response", res.page))
            log.info("  confirmed (%.0f ms, %.0f ms in total)", res.elapsed_ms, (time.perf_counter() - t0) * 1000)
            return self._evaluate(res.page.doc())
        finally:
            for label, page in seen:
                self.rec.save(label, page)

    def _confirm_fields(self, reg_form: HtmlElement) -> dict[str, str]:
        """Study code and exam slot on the confirmation page (only there if needed)."""
        fields: dict[str, str] = {}
        sc = pages.select_options(reg_form, "studyCode")
        if sc:
            name, opts = sc
            codes = [v for v, _ in opts]
            if not self.cfg.study_code:
                if len(codes) > 1:
                    log.warning("! you have several study codes, TISS uses its default - set `study_code` to choose")
            elif self.cfg.study_code in codes:
                fields[name] = self.cfg.study_code
            else:
                log.warning("! study code %s is not offered, TISS uses its default (offered: %s)",
                            self.cfg.study_code, ", ".join(codes))

        sl = pages.select_options(reg_form, "subgrouplist")
        if sl and sl[1]:
            name, opts = sl
            tokens = [x for x in re.split(r"[\s,\-–]+", self.cfg.slot) if x]
            choice = next(((v, t) for v, t in opts if tokens and all(tok in t for tok in tokens)), None)
            if choice is None:
                if self.cfg.slot:
                    log.warning("! slot %r not found", self.cfg.slot)
                choice = opts[0]
            log.info("  exam slot: %s", choice[1])
            fields[name] = choice[0]
        return fields

    def _evaluate(self, doc: HtmlElement) -> Outcome:
        msg = pages.result_message(doc)
        for m in [msg] if msg else pages.messages(doc):
            log.info("  TISS: %s", m)
        kind = pages.classify_result(msg) if msg else "unknown"
        if kind == "prereg":
            log.info("  this is a pre-registration: the lecturer still has to accept it")
        if kind in ("success", "prereg", "already"):
            return Outcome.REGISTERED
        if kind == "waitlist":
            return Outcome.WAITLIST
        return Outcome.FAILED
