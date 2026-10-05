# TISS Quantum Registration

Headless, fast registration for [TISS](https://tiss.tuwien.ac.at) (TU Wien): **course** (LVA), **group** and **exam** registrations.
It is a browser-free take on the
[TISS Quick Registration Script](https://github.com/mangei/tissquickregistrationscript) and draws on the
TISS API notes from [TISS Lightning Registrator](https://github.com/The-breakbar/TISS-Lightning-Registrator).

It never renders a page. It talks to the JSF backend directly with plain HTTP and parses only the HTML it needs (lxml). It runs on a headless VPS close to TISS.

## How it works

A registration takes three sequential requests. Below are the measured times from the live tests on 2026-10-05:

| # | Request | Purpose | Measured |
|---|---------|---------|----------|
| 1 | `GET groupList.xhtml?courseNr=…&semester=…` | poll until the `Anmelden` button of your option appears | 60–220 ms |
| 2 | `POST groupList.xhtml` | "click" the button → confirmation page | 30–130 ms |
| 3 | `POST register.xhtml` | binding confirmation → *"Sie wurden erfolgreich … angemeldet."* | 55–680 ms |

Courses use `courseRegistration.xhtml` and exams use `examDateList.xhtml` instead of `groupList.xhtml`.

* **Session:** the tool uses your browser's TISS cookies. TU Wien SSO has 2FA, so the tool does not log in itself.
* **One round trip per poll:** every GET carries the DeltaSpike window handshake (`?dsrid=X&dswid=W` plus cookie `dsrwid-X=W`). This skips TISS's JavaScript "Loading…" page.
* **POSTs match the browser byte for byte:** `<form>_SUBMIT=1`, `jakarta.faces.ViewState`, `jakarta.faces.ClientWindow` and the clicked button. The ViewState changes on every page load, so each attempt starts from a freshly polled page.
* **Retries:** a redirect in response to a POST means TISS rejected it (stale ViewState, not open yet, …). The tool then reloads and retries, up to 5 times.
* **Timing:** the tool reads the TISS server clock from the HTTP `Date` header (±tens of ms). It syncs once at the start and again 30 s before the opening. While waiting, it reloads the page every 5 minutes to keep the session alive. Polling starts 1.5 s before the opening with one request at a time every `interval_ms`. One poll is timed to *arrive* at TISS 40 ms after the opening.
* **Result:** the success, pre-registration or waiting-list message on the result page decides the exit code. The saved HTML is written to disk after the confirmation, so disk writes never delay the requests.

Step 2 alone is not binding. `--dry-run` stops right there, and the page still showed "not registered" afterwards.

## Setup (VPS)

```bash
git clone https://github.com/closbichler/tiss-quantum-registration.git && cd tiss-quantum-registration
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.toml config.toml   # edit it, see below
```

## Cookies

Log in to TISS in your browser and open the registration page. Then save the cookies to `cookies.txt` next to the config (git-ignored). Any of these formats works:

* **Raw header** (simplest): DevTools → Network → click the request for `groupList.xhtml` (or `courseRegistration.xhtml`, …) → *Request Headers* → copy the whole `Cookie:` value into the file.
  It must come from a request under `/education/...`, so that it contains the right `JSESSIONID`.
* **Netscape cookies.txt** (e.g. the "Get cookies.txt" extension).
* **JSON** (e.g. the "Cookie-Editor" extension → Export → JSON).

The tool needs `TISS_AUTH`, `_tiss_session` and `JSESSIONID`. **Do not log out in the browser** after exporting, because that invalidates the session. Closing the tab is fine.

## Configuration

`config.toml` is a handful of lines:

```toml
type = "group"            # course | group | exam
course = "185.A91"        # course number, with or without dot
semester = "2026W"
name = "Gruppe 002"       # group: exact group name | exam: text (regex) in the exam's header
# start = 2026-10-12T10:00:00   # Vienna time; leave out to use "Beginn der Anmeldung" from the page
```

`type = "course"` needs no `name`. Only add the following settings if you need them:

| Setting | Default | Purpose |
|---------|---------|---------|
| `study_code` | TISS default | curriculum to register with, if you are enrolled in several (e.g. `"033534"`) |
| `slot` | first offered | exam with time slots: text of the slot, e.g. `"14:15"` |
| `cookies` | `"cookies.txt"` | cookie file, relative to the config |
| `interval_ms` | `200` | time between page reloads around the opening (never more than one request at a time) |
| `window_s` | `90` | give up this long after the opening |

## Usage

```bash
# 1. read-only check: cookies valid? course, semester and option right? start time? clock offset?
.venv/bin/tissqr check

# 2. rehearsal on a registration that is already open: clicks "Anmelden", does NOT confirm
.venv/bin/tissqr run --now --dry-run
#    rehearse the timing too: set `start` a few minutes ahead, then
.venv/bin/tissqr run --dry-run

# 3. the real thing: start it any time before the opening, it waits by itself
tmux new -s tiss '.venv/bin/tissqr run'
```

Use `-c other.toml` for another config, and `-v` for debug output (one line per poll).

Exit codes: `0` registered / already registered / dry run OK, `1` failed, `2` not logged in or config error, `3` waiting list.

## Clock

All timing is relative to the TISS clock, so the local clock has to be *stable*. Its absolute value does not matter. `tissqr check` prints the measured offset. Run it twice: the offset should be the same both times, with no "inconsistent" warning.

Measured on 2026-10-05:
* **Windows host:** steady at −1.16 s ±0.03 s.
* **WSL2 on the same machine:** the clock ran about 5 % fast and was stepped back by about 2 s every 40 s. `timedatectl` still reported "synchronized". The tool can't time the opening on such a clock, so don't do the real run in WSL.

On a Linux VPS with NTP (chrony or systemd-timesyncd) this is not an issue.

## Logs

Each run writes `logs/<timestamp>/run.log`, with millisecond timestamps in Vienna time. Every relevant HTML response is saved in the same folder: start page, the page with the button, confirmation page, result. These pages contain your name, so don't share them publicly.

## Tests

`tests/` runs the full flow against a **mock** TISS (`httpx.MockTransport`). The mock is modelled on the pages recorded in the live tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Status

* **Tested live** (2026-10-05, authenticated): course registration (185.A62) and group registration (064.013), including the confirmation, `--dry-run`, waiting for a configured start, already-registered detection, and the redirect to the login page.
* **Not tested live yet:**
  * the actual opening moment (both tests ran on registrations that were already open)
  * exam registration, exam slots and study code selection
  * waiting-list and pre-registration results
* Requests are strictly sequential (no request floods). Be reasonable with `interval_ms`. Automated use may conflict with TU Wien's IT usage policies; use at your own risk.
