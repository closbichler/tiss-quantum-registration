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

1. **Clock:** it measures the TISS clock to within about half the network round trip (a few ms), from the `Date` header of a static TISS file. Each request is timed so that the TISS clock ticks to the next second while the request is under way. `check` also shows your round trip to TISS and warns if your clock is far off.
2. **Before the opening:** from 1.5 s before, it reloads the registration page every 200 ms, one request at a time.
3. **At the opening:** 1–4 reloads, a few ms apart, spread over the remaining clock uncertainty and sent without waiting for each other. Their connections are opened beforehand, so no handshake slows them down. One of them reaches TISS 20–40 ms after the real opening. If TISS turns out to answer parallel reloads one after the other (tested at startup), only one is sent.
4. **Register:** the first page with the `Anmelden` button sends the same form the browser would. The confirmation follows immediately; logging and saving pages wait until it is sent. If TISS rejects the click, another page from the opening is used, without a new reload.
5. **Result:** *"Sie wurden erfolgreich … angemeldet."* → exit code `0`; the waiting list → exit code `3`.

On courses with few participants the whole registration takes 80-800ms. On courses with many participants (tested with ~200 participant course, all registering at the same time) the registration took 2-3s, because TISS slows down during the rush.

**Status:** tested with course and group registrations. Not tested yet: exams (slots, study code). 
Automated use may conflict with TU Wien's IT usage policies; use at your own risk.
