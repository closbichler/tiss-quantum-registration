"""Command line interface: `tissqr check|run|clock`."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, config
from .register import Recorder, Registrar
from .session import NotLoggedIn, TissSession, load_cookies
from .timing import measure_offset

log = logging.getLogger("tissqr")


def setup_logging(log_dir: Path | None, verbose: bool) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"tissqr-{stamp}.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.info("logging to %s", fh.baseFilename)
    return stamp


def _session(cfg: config.Config) -> TissSession:
    cookies = load_cookies(cfg.cookies_file)
    log.info("loaded %d cookies from %s: %s", len(cookies), cfg.cookies_file.name,
             ", ".join(sorted({c.name for c in cookies})))
    missing = {"TISS_AUTH", "JSESSIONID", "_tiss_session"} - {c.name for c in cookies}
    if missing:
        log.warning("cookie(s) usually needed but missing: %s", ", ".join(sorted(missing)))
    return TissSession(cookies, cfg.user_agent, cfg.timeout_s)


def cmd_check(args) -> int:
    cfg = config.load(args.config)
    stamp = setup_logging(cfg.log_dir, args.verbose)
    sess = _session(cfg)
    reg = Registrar(cfg, sess, Recorder(cfg.log_dir / stamp if cfg.save_html else None))
    log.info("target: %s", cfg.target.describe())
    st = reg.inspect(save_as="check")
    ok = reg.report(st, list_all=True)
    if cfg.schedule.start:
        log.info("configured start: %s", cfg.schedule.start.isoformat())
    reg.clock_sync()
    log.info("check %s", "OK" if ok else "found problems (see warnings above)")
    return 0 if ok else 1


def cmd_run(args) -> int:
    cfg = config.load(args.config)
    stamp = setup_logging(cfg.log_dir, args.verbose)
    sess = _session(cfg)
    reg = Registrar(cfg, sess, Recorder(cfg.log_dir / stamp if cfg.save_html else None),
                    dry_run=args.dry_run)
    if args.dry_run:
        log.info("DRY RUN mode: %s", args.dry_run)
    try:
        return reg.run(now=args.now)
    finally:
        sess.close()


def cmd_clock(args) -> int:
    setup_logging(None, args.verbose)
    sess = TissSession([], config.DEFAULT_UA)
    s = measure_offset(sess.head_root, samples=args.samples)
    if s is None:
        log.error("could not measure")
        return 1
    log.info("server - local = %+.3f s (+-%.3f s), best RTT %.1f ms", s.offset, s.error, s.rtt * 1000)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tissqr", description="TISS Quantum Registration - fast headless TISS registration")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug output on the console")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="read-only: verify cookies, course page and target option")
    c.add_argument("-c", "--config", default="config.toml")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="wait for the opening and register")
    r.add_argument("-c", "--config", default="config.toml")
    r.add_argument("--now", action="store_true", help="ignore the start time and poll immediately")
    r.add_argument("--dry-run", nargs="?", const="detect", choices=["detect", "confirm"],
                   help="detect: stop when the register button appears (no POST at all); "
                        "confirm: click register but stop before the final confirmation")
    r.set_defaults(func=cmd_run)

    k = sub.add_parser("clock", help="measure the TISS server clock offset (no login needed)")
    k.add_argument("-n", "--samples", type=int, default=24)
    k.set_defaults(func=cmd_clock)

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
