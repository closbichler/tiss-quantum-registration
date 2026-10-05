# TISS Quantum Registration

Registers you for a [TISS](https://tiss.tuwien.ac.at) course, group or exam the moment registration opens. Plain HTTP, no browser.

## Quickstart

On Linux or macOS (e.g. a small VPS):

```bash
git clone https://github.com/closbichler/tiss-quantum-registration.git && cd tiss-quantum-registration
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.toml config.toml     # fill in course, semester, group
.venv/bin/tissqr check                 # checks everything, changes nothing
.venv/bin/tissqr run                   # waits for the opening, then registers
```

`check` and `run` also need `cookies.txt`. To create it:

1. Log in to TISS in your browser and open the registration page.
2. Open DevTools (F12) → Network and reload the page.
3. Click the `groupList.xhtml` request (`courseRegistration.xhtml` or `examDateList.xhtml` for courses and exams).
4. Under Request Headers, copy the value of `Cookie:` into `cookies.txt`.

Don't log out in the browser afterwards, or the cookies stop working. Closing the tab is fine.

## Config

```toml
type = "group"            # course | group | exam
course = "185.A91"
semester = "2026W"
name = "Gruppe 002"       # group: exact name | exam: part of the exam's line, e.g. its date
```

Optional settings:

| Setting | Default | |
|---------|---------|---|
| `start` | from TISS | opening time (Vienna), e.g. `2026-10-12T10:00:00` |
| `study_code` | TISS default | if you are enrolled in several curricula, e.g. `"033534"` |
| `slot` | first slot | exam time slot, e.g. `"14:15"` |
| `cookies` | `"cookies.txt"` | cookie file |
| `interval_ms` | `200` | time between page reloads around the opening |
| `window_s` | `90` | give up this long after the opening |

## Commands

| Command | |
|---------|---|
| `tissqr check` | checks cookies, course, group, start time and clock; changes nothing |
| `tissqr run` | waits for the opening, then registers |
| `tissqr run --dry-run` | the same, but stops before the binding confirmation |

To use a different config, add its file name, e.g. `tissqr run exam.toml`.

Exit codes:
* `0` registered (or dry run OK)
* `1` not registered
* `2` setup problem (config, cookies, login, network)
* `3` waiting list

## Good to know

* **Rehearse:** run `tissqr run --dry-run` on any registration that is already open. To also rehearse the waiting, set `start` a few minutes ahead.
* **Keep it running:** leave it running (e.g. in `tmux`) and the computer awake until the opening. It keeps the TISS session alive while it waits.
* **Clock:** your clock doesn't need to be exact, because the tool syncs to the TISS clock. It does need to be stable: WSL's clock isn't, and `check` warns about this.
* **Logs:** each run writes a log and the TISS pages it saw to `logs/<time>/`. The pages contain your name.
* **Cookie exports:** a Netscape `cookies.txt` or a Cookie-Editor JSON export work too.

## How it works

1. Starting 1.5 s before the opening, it reloads the registration page every 200 ms, one request at a time. One reload is timed to arrive right after the opening.
2. When the `Anmelden` button appears, it sends the same form the browser would and gets the confirmation page.
3. It confirms → *"Sie wurden erfolgreich … angemeldet."*

In the live test on 2026-10-05 the click took 30–130 ms and the confirmation 55–680 ms.

**Status:** tested live with course and group registrations that were already open. Not tested yet: an actual opening, exams (slots, study code), and the waiting list. Automated use may conflict with TU Wien's IT usage policies; use at your own risk.

**Tests:** `.venv/bin/python -m unittest discover -s tests` (runs against a mock TISS).
