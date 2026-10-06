"""HTTP session against TISS: cookie import, DeltaSpike window handling, redirects."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from . import pages

BASE = "https://tiss.tuwien.ac.at"
HOST = "tiss.tuwien.ac.at"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")
TIMEOUT_S = 20.0
CLOCK_URL = BASE + "/robots.txt"   # a static file: TISS answers it faster than any page
IDLE_S = 4.0         # TISS closes connections idle for ~5 s; never reuse one that old
# a kept-alive connection that turned out to be closed: the request never reached TISS's application
STALE = (httpx.RemoteProtocolError, httpx.WriteError, httpx.ConnectError)

_STUB_RE = re.compile(r"var redirectUrl\s*=\s*'([^']*)'")


class NotLoggedIn(Exception):
    """The session cookies are missing or expired."""


@dataclass
class Page:
    url: str
    status: int
    content: bytes
    elapsed_ms: float

    def doc(self):
        return pages.parse(self.content, self.url)


@dataclass
class PostResult:
    page: Page | None
    redirect: str | None
    elapsed_ms: float


@dataclass
class CookieSpec:
    name: str
    value: str
    domain: str = HOST
    path: str = "/"


def load_cookies(path: Path) -> list[CookieSpec]:
    """Accepts a raw Cookie header, a Netscape cookies.txt or a JSON export (e.g. Cookie-Editor)."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path.name} is empty - paste your TISS cookies into it (see README)")

    if text.startswith("[") or text.startswith("{"):
        items = json.loads(text)
        if isinstance(items, dict):
            items = items.get("cookies", [])
        return [CookieSpec(c["name"], c["value"], c.get("domain") or HOST, c.get("path") or "/")
                for c in items]

    lines = [l for l in text.splitlines() if l.strip()]
    if any(l.count("\t") >= 6 for l in lines):
        out = []
        for line in lines:
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out.append(CookieSpec(parts[5], parts[6], parts[0], parts[2] or "/"))
        return out

    header = " ".join(lines)
    if header.lower().startswith("cookie:"):
        header = header[7:]
    out = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        # JSESSIONID is path-scoped per TISS app; the registration pages live under /education
        out.append(CookieSpec(name, value, HOST, "/education" if name == "JSESSIONID" else "/"))
    return out


def set_query(url: str, **params: str) -> str:
    s = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k not in params]
    q.extend(params.items())
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), s.fragment))


def _js_unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


class TissSession:
    """Thread-safe: around the opening several requests run at the same time."""
    MAX_HOPS = 8

    def __init__(self, cookies: list[CookieSpec], transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(TIMEOUT_S, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=IDLE_S),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-AT,de;q=0.9,en;q=0.6",
            },
        )
        for c in cookies:
            if not c.name.startswith("dsrwid-"):   # the browser's window cookies; each request brings its own
                self.client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        self.window_id = str(random.randint(1000, 9999))

    def close(self) -> None:
        self.client.close()

    # DeltaSpike: a GET needs ?dsrid=X&dswid=W plus cookie dsrwid-X=W, otherwise TISS
    # answers with its JavaScript "Loading..." page instead of the real page.
    def _tokenize(self, url: str) -> str:
        s = urlsplit(url)
        if s.hostname != HOST or not s.path.endswith(".xhtml"):
            return url
        return set_query(url, dsrid=str(random.randint(0, 998)), dswid=self.window_id)

    def _send(self, req: httpx.Request) -> httpx.Response:
        try:
            return self.client.send(req)
        except STALE:
            return self.client.send(req)   # once more, on a fresh connection

    def _get(self, url: str) -> httpx.Response:
        url = self._tokenize(url)
        req = self.client.build_request("GET", url)
        q = dict(parse_qsl(urlsplit(url).query))
        if "dsrid" in q:
            # the dsrwid cookie goes into this request only, not into the shared cookie jar
            c = f"dsrwid-{q['dsrid']}={q.get('dswid', '')}"
            req.headers["Cookie"] = f"{req.headers['Cookie']}; {c}" if "Cookie" in req.headers else c
        return self._send(req)

    def get(self, url: str) -> Page:
        """GET a TISS page, following redirects and window-handler stubs (normally: one round trip)."""
        t0 = time.perf_counter()
        resp = self._get(url)
        for _ in range(self.MAX_HOPS):
            url = str(resp.url)
            if resp.is_redirect:
                resp = self._get(urljoin(url, resp.headers["location"]))
                continue
            if urlsplit(url).hostname != HOST:
                raise NotLoggedIn(f"redirected to {url.split('?')[0]}")
            if urlsplit(url).path.startswith("/admin/authentifizierung"):
                raise NotLoggedIn("redirected to the TISS login")
            if b"handleWindowId" in resp.content[:20000] or b"handleWindowId" in resp.content[-2000:]:
                m = _STUB_RE.search(resp.text)
                if m:
                    resp = self._get(urljoin(BASE, _js_unescape(m.group(1))))
                    continue
            return Page(url, resp.status_code, resp.content, (time.perf_counter() - t0) * 1000)
        raise pages.PageError("too many redirects")

    def post(self, sub: pages.Submission) -> PostResult:
        """POST a form. TISS answers 200 on success; any redirect means the request was rejected."""
        t0 = time.perf_counter()
        resp = self._send(self.client.build_request(
            "POST", sub.url, content=urlencode(sub.data),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE, "Referer": sub.url}))
        ms = (time.perf_counter() - t0) * 1000
        if resp.is_redirect:
            return PostResult(None, urljoin(str(resp.url), resp.headers.get("location", "")), ms)
        return PostResult(Page(str(resp.url), resp.status_code, resp.content, ms), None, ms)

    def head_clock(self) -> tuple[httpx.Response, float, float]:
        t0 = time.time()
        r = self.client.head(CLOCK_URL)
        return r, t0, time.time()

    def warm(self, n: int) -> None:
        """Open n connections at once, so that n parallel requests right after don't wait for a
        TCP and TLS handshake (about three round trips)."""
        def head():
            try:
                self.client.head(CLOCK_URL)
            except httpx.HTTPError:
                pass
        threads = [threading.Thread(target=head, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
