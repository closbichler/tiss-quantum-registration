"""Command line: `tissqr check` and `tissqr run`."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import config
from .pages import VIENNA, PageError
from .register import Recorder, Registrar, fmt_duration, fmt_start
from .session import NotLoggedIn, TissSession, load_cookies

log = logging.getLogger("tissqr")


class ViennaFormatter(logging.Formatter):
    """Timestamps in Vienna time (= TISS time), whatever the machine's timezone is."""

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, VIENNA).strftime(datefmt)[:-3]


def setup_console(timestamps: bool) -> None:
    sys.stdout.reconfigure(errors="replace")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ViennaFormatter("%(asctime)s %(message)s", "%H:%M:%S.%f") if timestamps
                         else logging.Formatter("%(message)s"))
    log.setLevel(logging.DEBUG)
    log.addHandler(console)


def add_log_file(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    file = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file.setFormatter(ViennaFormatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S.%f"))
    log.addHandler(file)


def setup(args) -> tuple[config.Config, TissSession, Recorder]:
    cfg = config.load(args.config)
    run_dir = cfg.log_dir / datetime.now(VIENNA).strftime("%Y%m%d-%H%M%S")
    add_log_file(run_dir)
    if not cfg.cookies.exists():
        raise ValueError(f"{cfg.cookies.name} not found - save your TISS cookies there (see README)")
    cookies = load_cookies(cfg.cookies)
    log.debug("cookies: %s", ", ".join(sorted(c.name for c in cookies)))
    missing = {"TISS_AUTH", "JSESSIONID", "_tiss_session"} - {c.name for c in cookies}
    if missing:
        log.warning("! %s has no %s - copy the Cookie header of a request to tiss.tuwien.ac.at/education/...",
                    cfg.cookies.name, ", ".join(sorted(missing)))
    return cfg, TissSession(cookies), Recorder(run_dir)


def check(args) -> int:
    cfg, sess, rec = setup(args)
    reg = Registrar(cfg, sess, rec)
    st = reg.inspect()
    rec.save("check", st.page)
    ok = reg.report(st, list_all=True)
    if st.option is not None and not st.registered:
        start = cfg.start or st.start
        if start is None and st.button is None:
            log.error("✘ TISS shows no start time - set `start` in the config")
            ok = False
        elif start and start.timestamp() > time.time():
            log.info("✔ registration opens %s (%s), in %s", fmt_start(start),
                     "from the config" if cfg.start else "from TISS", fmt_duration(start.timestamp() - time.time()))
    reg.clock_sync()

    run = "tissqr run" + ("" if args.config == "config.toml" else f" {args.config}")
    if ok:
        log.info("\nAll good. Start it with `%s` (or rehearse with `%s --dry-run`).", run, run)
    else:
        log.info("\nFix the problems marked ✘ and run the check again.")
    return 0 if ok else 1


def run(args) -> int:
    cfg, sess, rec = setup(args)
    if args.dry_run:
        log.info("  dry run: stops before the binding confirmation")
    try:
        code = Registrar(cfg, sess, rec, dry_run=args.dry_run).run()
    finally:
        sess.close()
    log.info("  log and saved pages: %s", rec.dir)
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tissqr", description="Registers you for a TISS course, group or exam "
                                                           "the moment registration opens.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check config, cookies, page and clock (changes nothing)")
    c.set_defaults(func=check)
    r = sub.add_parser("run", help="wait for the opening, then register")
    r.add_argument("--dry-run", action="store_true", help="stop before the binding confirmation")
    r.set_defaults(func=run)
    for s in (c, r):
        s.add_argument("config", nargs="?", default="config.toml", help="config file (default: config.toml)")
    args = p.parse_args(argv)

    setup_console(timestamps=args.cmd == "run")
    try:
        return args.func(args)
    except NotLoggedIn as e:
        log.debug("not logged in: %s", e)
        log.error("✘ not logged in - your cookies are missing or expired. Log in to TISS in your browser, "
                  "copy the cookies again (see README) and don't log out afterwards.")
    except ValueError as e:
        log.error("✘ %s", e)
    except httpx.HTTPError as e:
        log.error("✘ cannot reach TISS (%s) - check your internet connection", e)
    except PageError as e:
        log.error("✘ unexpected answer from TISS: %s", e)
    except KeyboardInterrupt:
        log.warning("\nstopped")
        return 130
    return 2
