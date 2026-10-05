# tiss-quickreg

Headless, fast registration for [TISS](https://tiss.tuwien.ac.at) (TU Wien): **LVA**, **group** and **exam** registrations.
It is a browser-free take on the
[TISS Quick Registration Script](https://github.com/mangei/tissquickregistrationscript) and draws on the
TISS API notes from [TISS Lightning Registrator](https://github.com/The-breakbar/TISS-Lightning-Registrator).

It never renders a page. It talks to the JSF backend directly with plain HTTP and parses only the HTML it needs (lxml). It runs on a headless VPS close to TISS.

## How it works

1. **Session:** uses your browser's TISS session cookies (TU Wien SSO has 2FA, so the tool does not log in itself).
2. **DeltaSpike window handshake:** every GET sends `?dsrid=X&dswid=W` together with cookie `dsrwid-X=W`. This skips TISS's JS "Loading…" interstitial, so you get the real page in one round trip.
3. **Waiting:** while waiting it reloads the page every `keepalive_s` to keep the session alive. It syncs to the **TISS server clock** using the HTTP `Date` header (±tens of ms), and pre-warms the session and TLS connection about 12s before opening.
4. **Burst:** it polls the course page one request at a time, starting `lead_ms` before opening. One poll is timed so that it *arrives* at the server `arrive_margin_ms` after the opening.
5. As soon as the `Anmelden`/`Register` button for your option shows up, it replays the click:
   `POST groupList.xhtml` (or `courseRegistration.xhtml` / `examDateList.xhtml`) with the form's `ViewState`/`ClientWindow`. It then replays the confirmation: `POST register.xhtml`, with study code and exam slot if needed.
6. It reads the result message (`success` / `pre-registration` / `waiting list`) and checks the course page again.

A redirect in response to a POST means TISS rejected it (stale ViewState, not open yet, …). In that case the tool reloads and retries, up to `max_attempts` times.

## Setup (VPS)

```bash
git clone <your-repo> tiss-quickreg && cd tiss-quickreg
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.toml config.toml   # edit target / schedule
```

Make sure the VPS clock is NTP-synced (`timedatectl`). The tool also measures the TISS clock itself:
`.venv/bin/tissreg clock`.

## Cookies

Log in to TISS in your browser, open the course's registration page, then export the cookies into `cookies.txt` (git-ignored). Any of these formats works:

* **Raw header** (simplest): DevTools → Network → click the request for `groupList.xhtml` (or similar) → *Request Headers* → copy the whole `Cookie:` value into the file.
  It must come from a request under `/education/...`, so that it contains the right `JSESSIONID`.
* **Netscape cookies.txt** (e.g. the "Get cookies.txt" extension).
* **JSON** (e.g. the "Cookie-Editor" extension → Export → JSON).

The tool needs `TISS_AUTH`, `_tiss_session` and `JSESSIONID`. If you also export the cookies of `idp.zid.tuwien.ac.at`, the tool can silently refresh the TISS session through SSO while the IdP session is still valid. That path is untested.

**Do not log out in the browser** after exporting, because that invalidates the session. Closing the tab is fine.

## Usage

```bash
# 1. read-only check: cookies valid? course/semester right? option found? start time? clock offset?
.venv/bin/tissreg check -c config.toml

# 2. safe rehearsal on a registration that is already open (no POST at all):
.venv/bin/tissreg run -c config.toml --now --dry-run
#    goes one step further: clicks "Anmelden" but does NOT confirm (the confirmation page is not binding)
.venv/bin/tissreg run -c config.toml --now --dry-run=confirm

# 3. the real thing - start it any time before the opening, it waits by itself:
tmux new -s tiss '.venv/bin/tissreg run -c config.toml'
#    or: nohup .venv/bin/tissreg run -c config.toml > /dev/null 2>&1 &
```

If `schedule.start` is not set, the start time is read from the page ("Anmeldebeginn").

Exit codes: `0` registered / already registered / dry run OK, `1` failed, `2` not logged in or config error, `3` waiting list.

Logs go to `logs/tissreg-<timestamp>.log` (millisecond timestamps). With `save_html = true`, every relevant HTML response is stored in `logs/<timestamp>/` for debugging.

## Tests

`tests/` runs the full flow against a **mock** TISS (`httpx.MockTransport`). The mock is modelled on the documented page and request structure, not on live data:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Status / caveats

* Built **without access to an authenticated TISS session**. The unauthenticated parts are verified live: the window handshake, the login redirect detection and the clock sync. The registration pages themselves are implemented from the userscript and the Lightning Registrator API docs. Run `check` and `--dry-run` before relying on it.
* Requests are strictly sequential (no request floods). Be reasonable with `interval_ms`. Automated use may conflict with TU Wien's IT usage policies; use at your own risk.
