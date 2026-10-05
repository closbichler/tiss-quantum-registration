# TISS Quantum Registration

Registers you for a [TISS](https://tiss.tuwien.ac.at) course, group or exam the moment registration opens. Plain HTTP, no browser.

Faster evolution of [TISS Quick Registration Script](https://github.com/mangei/tissquickregistrationscript) and [TISS Lightning Registrator](https://github.com/The-breakbar/TISS-Lightning-Registrator)

## Quickstart

```bash
git clone https://github.com/closbichler/tiss-quantum-registration.git && cd tiss-quantum-registration
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.toml config.toml     # fill in course, semester, group
.venv/bin/tissqr check                 # checks everything, changes nothing
.venv/bin/tissqr run                   # waits for the opening, then registers
```

`check` and `run` also need `cookies.txt`. To create it either use "Get Cookies.txt" Extension or:

1. Log in to TISS in your browser and open the registration page.
2. Open DevTools (F12) → Network and reload the page.
3. Click the `groupList.xhtml` request (`courseRegistration.xhtml` or `examDateList.xhtml` for courses and exams).
4. Under Request Headers, copy the value of `Cookie:` into `cookies.txt`.

Don't log out in the browser afterwards, or the cookies stop working. Closing the tab is fine.

## Commands

| Command | |
|---------|---|
| `tissqr check` | checks cookies, course, group, start time and clock; changes nothing |
| `tissqr run` | waits for the opening, then registers |
| `tissqr run --dry-run` | the same, but stops before the binding confirmation |

Exit codes:
* `0` registered (or dry run OK)
* `1` not registered
* `2` setup problem (config, cookies, login, network)
* `3` waiting list

## How it works

1. Starting 1.5 s before the opening, it reloads the registration page every 200 ms, one request at a time. One reload is timed to arrive right after the opening.
2. When the `Anmelden` button appears, it sends the same form the browser would and gets the confirmation page.
3. It confirms → *"Sie wurden erfolgreich … angemeldet."*

In the live test on 2026-10-05 the click took 30–130 ms and the confirmation 55–680 ms.

**Status:** tested with course and group registrations that were already open. Not tested yet: exams (slots, study code), and waiting list. 
Automated use may conflict with TU Wien's IT usage policies; use at your own risk.
