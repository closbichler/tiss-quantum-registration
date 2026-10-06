"""The registration flow: wait for the opening, reload the page, click "Anmelden", confirm."""
from __future__ import annotations

import enum
import gc
import logging
import math
import re
import sys
import threading
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
ARRIVE_MARGIN_S = 0.02   # the reloads around the opening arrive at least this long after it
BURST_STEP_S = 0.02      # ... at most about this far apart
BURST_MAX = 4            # ... and there are at most this many
WARM_S = 0.8             # open their connections this long before the first one
KEEPALIVE_S = 300        # while waiting, reload the page this often to keep the session alive
RESYNC_S = 30            # measure the TISS clock again this long before the opening
MAX_ATTEMPTS = 5
CLOCK_WARN_S = 2.0       # warn about a clock that is further off than this
SLOW_RTT_S = 0.025       # suggest a server closer to Vienna above this round trip

NOT_FOUND_HINT = {
    "course": " - this course has no registration in TISS (yet)",
    "group": " - set `name` to one of the groups listed above",
    "exam": " - `name` must match a part of one of the lines above",
}

TIME_SYNC_HINT = {
    "linux": "`sudo timedatectl set-ntp true`",
    "win32": "Settings → Time & language → Date & time → Set time automatically",
    "darwin": "System Settings → General → Date & Time → Set time and date automatically",
}.get(sys.platform, "your system's automatic time setting")


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
        self.lock = threading.Lock()

    def save(self, label: str, page: Page | None) -> None:
        if self.dir is None or page is None:
            return
        with self.lock:
            self.n += 1
            n = self.n
        self.dir.mkdir(parents=True, exist_ok=True)
        head = f"<!-- {page.url} status={page.status} elapsed={page.elapsed_ms:.0f}ms -->\n".encode()
        (self.dir / f"{n:03d}-{label}.html").write_bytes(head + page.content)


@dataclass
class Status:
    page: Page
    doc: HtmlElement
    option: HtmlElement | None
    button: HtmlElement | None
    registered: bool
    start: datetime | None
    sent: float = 0.0   # local time the reload was sent
    n: int = 0          # number of the reload in the race


class Race:
    """What the reloads of one registration share. Around the opening several of them run at
    the same time; the first that shows the register button registers, the others stay spare."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reloads = 0
        self.attempts = 0
        self.busy = False                         # a reload is registering right now
        self.outcome: Outcome | None = None       # set once there is a final outcome (not FAILED)
        self.done_at = 0.0                        # local time of the final outcome
        self.shown = False                        # the page showed us as registered
        self.spare: Status | None = None          # another page with the register button
        self.error: Exception | None = None       # e.g. NotLoggedIn in a parallel reload

    def next_number(self) -> int:
        with self.lock:
            self.reloads += 1
            return self.reloads

    def decided(self) -> bool:
        with self.lock:
            return self.busy or self.outcome is not None


def burst_offsets(error: float, parallel: bool = True) -> list[float]:
    """When the reloads around the opening should reach TISS, relative to the opening as
    measured. The real opening lies within ±error of that. The reloads are spread over that
    range so that, wherever it is, one of them arrives between ARRIVE_MARGIN_S and about
    ARRIVE_MARGIN_S + BURST_STEP_S after it. A single reload would need to arrive at
    ARRIVE_MARGIN_S + error, up to 2 * error later than necessary."""
    n = min(BURST_MAX, max(1, math.ceil(2 * error / BURST_STEP_S - 1e-9))) if parallel else 1
    step = 2 * error / n
    return sorted(ARRIVE_MARGIN_S + error - i * step for i in range(n))


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
        self.parallel = True   # TISS works on parallel reloads at the same time (see probe_parallel)
        self._clock_warned = False

    def inspect(self) -> Status:
        sent = time.time()
        page = self.sess.get(self.cfg.url)
        if page.status >= 400:
            raise PageError(f"HTTP {page.status} for {page.url}")
        doc = page.doc()
        opt = pages.find_option(doc, self.cfg.type, self.cfg.name)
        return Status(page, doc, opt,
                      button=pages.find_button(opt),
                      registered=pages.find_button(opt, pages.UNREGISTER) is not None,
                      start=pages.registration_start(opt) if opt is not None else None,
                      sent=sent)

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
            s = measure_offset(self.sess.head_clock)
        except ClockError as e:
            log.warning("! could not measure the TISS clock: %s - the timing may be off", e)
            return
        self.sync = s
        log.debug("clock: offset %+.4f s ± %.4f s, round trip %.1f ms", s.offset, s.error, s.rtt * 1000)
        err = f"±{s.error * 1000:.0f} ms"
        if abs(s.offset) < 0.05:
            log.info("✔ your clock matches the TISS clock (%s)", err)
        else:
            log.info("✔ your clock is %.2f s %s the TISS clock - this is compensated (%s)",
                     abs(s.offset), "ahead of" if s.offset < 0 else "behind", err)
        if s.rtt > SLOW_RTT_S:
            log.warning("! network round trip to TISS: %.0f ms - a server in or near Vienna "
                        "(e.g. Vienna or Frankfurt) would be faster", s.rtt * 1000)
        else:
            log.info("✔ network round trip to TISS: %.0f ms", s.rtt * 1000)
        if abs(s.offset) > CLOCK_WARN_S and not self._clock_warned:
            self._clock_warned = True
            log.warning("! your clock is %.1f s off - it's compensated, but turn on time sync (%s)",
                        abs(s.offset), TIME_SYNC_HINT)

    def probe_parallel(self) -> None:
        """Does TISS work on two reloads of the page at the same time? If it took them one after
        the other, parallel reloads around the opening would delay each other; then only one is sent."""
        self.sess.warm(2)
        try:
            single = self.inspect().page.elapsed_ms
            pair: list[float | Exception] = [0.0, 0.0]

            def reload(i: int) -> None:
                try:
                    pair[i] = self.inspect().page.elapsed_ms
                except Exception as e:
                    pair[i] = e
            threads = [threading.Thread(target=reload, args=(i,), daemon=True) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for x in pair:
                if isinstance(x, Exception):
                    raise x
        except NotLoggedIn:
            raise
        except TRANSIENT as e:
            log.warning("! could not test parallel reloads (%s) - assuming they work", e)
            return
        log.debug("one reload %.0f ms, two parallel reloads %.0f / %.0f ms", single, *pair)
        slow, fast = max(pair), min(pair)
        self.parallel = not (slow > 1.6 * single + 30 and slow - fast > 0.5 * single)
        if self.parallel:
            log.info("✔ a reload of the page takes %.0f ms, TISS answers parallel reloads at the same time", single)
        else:
            log.warning("! TISS answers parallel reloads one after the other (one: %.0f ms, two: %.0f ms) "
                        "- only one reload will be sent at the opening", single, slow)

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
        if start.timestamp() - time.time() > 12:   # measuring takes ~5 s, the parallel test ~1 s
            self.clock_sync()
            self.probe_parallel()
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
        """Reload the page until the register button appears, then register.

        Without a start time: one reload at a time, every interval_ms. With one: the same from
        LEAD_S before the opening, but around the opening a burst of reloads spread over the clock
        uncertainty (see burst_offsets), each in its own thread on an already open connection."""
        race = Race()
        burst: list[threading.Thread] = []
        if start is None:
            next_send = time.time()
            deadline = next_send + self.cfg.window_s
            burst_at = math.inf
        else:
            open_at = self._local(start)
            sends = [open_at + o - self.sync.rtt / 2 for o in burst_offsets(self.sync.error, self.parallel)]
            burst_at = sends[0]
            next_send = open_at - LEAD_S
            deadline = open_at + self.cfg.window_s
            burst = [threading.Thread(target=self._burst_reload, args=(race, t), daemon=True) for t in sends]
            if len(sends) > 1:
                burst.append(threading.Thread(target=self._warm, args=(burst_at - WARM_S, len(sends) + 1),
                                              daemon=True))
            log.debug("reloads at the opening arrive at %s (TISS time)",
                      ", ".join(hms(t + self.sync.rtt / 2 + self.sync.offset) for t in sends))
        log.info("  reloading the page every %d ms until the register button appears", self.cfg.interval_ms)
        for t in burst:
            t.start()

        gc.collect()
        gc.disable()   # no garbage collection pauses during the race
        try:
            return self._race(race, burst, burst_at, next_send, deadline, start)
        finally:
            gc.enable()

    def _race(self, race: Race, burst: list[threading.Thread], burst_at: float, next_send: float,
              deadline: float, start: datetime | None) -> int:
        interval = self.cfg.interval_ms / 1000
        rtt = self.sync.rtt   # how long a reload takes, updated from each reload
        reported = 0
        last: Status | None = None
        while True:
            if race.error is not None:
                raise race.error
            if race.outcome is not None:
                return self._finish(race, start)
            if race.attempts > reported:
                reported = race.attempts
                if race.attempts < MAX_ATTEMPTS:
                    log.warning("! attempt %d did not work - trying again with %s page", race.attempts,
                                "another" if race.spare is not None else "a fresh")
            if race.attempts >= MAX_ATTEMPTS or time.time() >= deadline:
                break
            if burst and next_send + rtt + 0.01 > burst_at:
                # that reload would still be running at the opening; the burst threads take over
                for t in burst:
                    t.join()
                burst = []
                next_send = max(next_send, time.time())
                continue
            if race.spare is not None and race.attempts:
                with race.lock:
                    st, race.spare = race.spare, None
                self._handle(race, st)
                continue

            sleep_until(next_send)
            st = self._reload(race)
            if st is None:
                next_send = max(next_send + interval, time.time())
                continue
            last = st
            rtt = st.page.elapsed_ms / 1000
            if self._handle(race, st):
                next_send = time.time()
            else:
                if st.n % 25 == 0:
                    log.info("  no register button yet (%d reloads)", st.n)
                next_send = max(next_send + interval, time.time())

        if last is not None:
            self.rec.save("last-poll", last.page)
        if race.attempts:
            log.error("✘ NOT registered - %d attempt(s) failed", race.attempts)
        else:
            log.error("✘ NOT registered - no register button until %s (closed or full?)", hms(deadline))
        return 1

    def _warm(self, at: float, n: int) -> None:
        sleep_until(at)
        t0 = time.perf_counter()
        self.sess.warm(n)
        log.debug("opened %d connections for the reloads at the opening (%.0f ms)", n, (time.perf_counter() - t0) * 1000)

    def _burst_reload(self, race: Race, at: float) -> None:
        try:
            sleep_until(at)
            if race.decided():
                return   # an earlier reload is already registering
            st = self._reload(race)
            if st is not None:
                self._handle(race, st)
        except Exception as e:   # the main thread raises it
            race.error = e

    def _reload(self, race: Race) -> Status | None:
        n = race.next_number()
        try:
            st = self.inspect()
        except NotLoggedIn as e:
            race.error = e
            return None
        except TRANSIENT as e:
            log.warning("! reload %d failed: %s", n, e)
            return None
        st.n = n
        return st

    def _handle(self, race: Race, st: Status) -> bool:
        """Register if the page shows the register button, unless another reload already does.
        Returns False if there is nothing to do yet."""
        if not st.registered and st.button is None:
            log.debug("reload %d sent %s (TISS time %s), %.0f ms, no button", st.n, hms(st.sent),
                      hms(st.sent + self.sync.offset), st.page.elapsed_ms)
            return False
        with race.lock:
            if race.busy or race.outcome is not None:
                if st.button is not None:
                    race.spare = st
                taken = False
            else:
                race.busy = taken = True
        if not taken:
            log.debug("reload %d sent at TISS time %s, %.0f ms: the button is there too (spare)",
                      st.n, hms(st.sent + self.sync.offset), st.page.elapsed_ms)
            return True
        out = Outcome.FAILED
        try:
            if st.registered:
                race.shown = True
                out = Outcome.REGISTERED
            else:
                out = self._attempt(st)
        except NotLoggedIn:
            raise
        except TRANSIENT as e:
            log.warning("! request failed: %s", e)
        finally:
            with race.lock:
                race.busy = False
                if not st.registered:
                    race.attempts += 1
                if out is not Outcome.FAILED:
                    race.outcome, race.done_at = out, time.time()
        return True

    def _finish(self, race: Race, start: datetime | None) -> int:
        after = f" - {race.done_at + self.sync.offset - start.timestamp():.2f} s after the opening" if start else ""
        if race.outcome is Outcome.REGISTERED:
            log.info("✔ REGISTERED - the page shows you as registered" if race.shown else f"✔ REGISTERED{after}")
            return 0
        if race.outcome is Outcome.DRY_RUN:
            log.info("✔ DRY RUN finished at the confirmation page - you are NOT registered")
            return 0
        log.warning("! you are on the WAITING LIST%s", after)
        return 3

    def _attempt(self, st: Status) -> Outcome:
        """Click the register button and confirm. Logging and saving the pages wait until the
        requests are sent."""
        seen = [("open-page", st.page)]
        held: list[tuple[int, str]] = []   # log lines held back until the confirmation is sent
        try:
            t0 = time.perf_counter()
            res = self.sess.post(pages.build_submission(st.button))
            log.info("✔ the register button is there (reload %d, sent at TISS time %s, %.0f ms) - registering",
                     st.n, hms(st.sent + self.sync.offset), st.page.elapsed_ms)
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
            fields = self._confirm_fields(reg_form, held)
            if self.dry_run:
                log.info("  confirmation page (%.0f ms)", res.elapsed_ms)
                for level, msg in held:
                    log.log(level, msg)
                return Outcome.DRY_RUN

            confirm = self.sess.post(pages.build_submission(btn, fields))
            log.info("  confirmation page (%.0f ms)", res.elapsed_ms)
            for level, msg in held:
                log.log(level, msg)
            if confirm.redirect:
                log.warning("! TISS rejected the confirmation (%.0f ms)", confirm.elapsed_ms)
                log.debug("redirected to %s", confirm.redirect)
                return Outcome.FAILED
            seen.append(("confirm-response", confirm.page))
            log.info("  confirmed (%.0f ms, %.0f ms in total)", confirm.elapsed_ms, (time.perf_counter() - t0) * 1000)
            return self._evaluate(confirm.page.doc())
        finally:
            for label, page in seen:
                self.rec.save(label, page)

    def _confirm_fields(self, reg_form: HtmlElement, held: list[tuple[int, str]]) -> dict[str, str]:
        """Study code and exam slot on the confirmation page (only there if needed).
        Messages go to `held`, to be logged after the confirmation is sent."""
        fields: dict[str, str] = {}
        sc = pages.select_options(reg_form, "studyCode")
        if sc:
            name, opts = sc
            codes = [v for v, _ in opts]
            if not self.cfg.study_code:
                if len(codes) > 1:
                    held.append((logging.WARNING, "! you have several study codes, TISS uses its default "
                                                  "- set `study_code` to choose"))
            elif self.cfg.study_code in codes:
                fields[name] = self.cfg.study_code
            else:
                held.append((logging.WARNING, f"! study code {self.cfg.study_code} is not offered, TISS uses "
                                              f"its default (offered: {', '.join(codes)})"))

        sl = pages.select_options(reg_form, "subgrouplist")
        if sl and sl[1]:
            name, opts = sl
            tokens = [x for x in re.split(r"[\s,\-–]+", self.cfg.slot) if x]
            choice = next(((v, t) for v, t in opts if tokens and all(tok in t for tok in tokens)), None)
            if choice is None:
                if self.cfg.slot:
                    held.append((logging.WARNING, f"! slot {self.cfg.slot!r} not found"))
                choice = opts[0]
            held.append((logging.INFO, f"  exam slot: {choice[1]}"))
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
