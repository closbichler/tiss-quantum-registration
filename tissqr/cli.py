"""Command line interface: `tissqr check|run`."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, config
from .pages import VIENNA
from .register import Recorder, Registrar, fmt_start
from .session import NotLoggedIn, TissSession, load_cookies

log = logging.getLogger("tissqr")


class ViennaFormatter(logging.Formatter):
    """Log timestamps in Vienna time (= TISS time), whatever the machine's timezone is."""

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, VIENNA).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def setup_logging(run_dir: Path, verbose: bool) -> None:
    fmt = ViennaFormatter("%(asctime)s %(levelname)-7s %(message)s")
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)
    run_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    log.info("logging to %s", run_dir)


def _setup(args) -> tuple[config.Config, TissSession, Recorder]:
    cfg = config.load(args.config)
    run_dir = cfg.log_dir / datetime.now(VIENNA).strftime("%Y%m%d-%H%M%S")
    setup_logging(run_dir, args.verbose)
    cookies = load_cookies(cfg.cookies)
    log.info("loaded %d cookies from %s: %s", len(cookies), cfg.cookies.name,
             ", ".join(sorted({c.name for c in cookies})))
    missing = {"TISS_AUTH", "JSESSIONID", "_tiss_session"} - {c.name for c in cookies}
    if missing:
        log.warning("cookie(s) usually needed but missing: %s", ", ".join(sorted(missing)))
    return cfg, TissSession(cookies), Recorder(run_dir)


def cmd_check(args) -> int:
    cfg, sess, rec = _setup(args)
    reg = Registrar(cfg, sess, rec)
    log.info("target: %s", cfg.describe())
    log.info("url: %s", cfg.url)
    st = reg.inspect()
    rec.save("check", st.page)
    ok = reg.report(st, list_all=True)
    if cfg.start:
        log.info("start: %s (configured)", fmt_start(cfg.start))
    elif st.start:
        log.info("start: %s (from the page)", fmt_start(st.start))
    else:
        log.warning("start: unknown - set `start` in the config")
        ok = False
    reg.clock_sync()
    log.info("check %s", "OK" if ok else "found problems (see warnings above)")
    return 0 if ok else 1


def cmd_run(args) -> int:
    cfg, sess, rec = _setup(args)
    if args.dry_run:
        log.info("DRY RUN: will stop before the final confirmation")
    try:
        return Registrar(cfg, sess, rec, dry_run=args.dry_run).run(now=args.now)
    finally:
        sess.close()


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.toml", help="config file (default: config.toml)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug output on the console")

    p = argparse.ArgumentParser(prog="tissqr", description="TISS Quantum Registration - fast headless TISS registration")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", parents=[common],
                       help="read-only: verify cookies, course page, target, start time and clock")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", parents=[common], help="wait for the opening and register")
    r.add_argument("--now", action="store_true", help="ignore the start time and poll immediately")
    r.add_argument("--dry-run", action="store_true",
                   help="click register but stop before the final (binding) confirmation")
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except NotLoggedIn as e:
        log.error("NOT LOGGED IN: %s - export fresh cookies from your browser", e)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except (FileNotFoundError, ValueError, KeyError) as e:
        log.error("configuration problem: %s", e)
        return 2
