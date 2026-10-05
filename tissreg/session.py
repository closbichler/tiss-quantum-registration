"""HTTP session against TISS: cookie import, DeltaSpike window handling, redirects."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from . import pages

BASE = "https://tiss.tuwien.ac.at"
HOST = "tiss.tuwien.ac.at"
log = logging.getLogger("tissreg")

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
    page: Page | None          # 200 response
    redirect: str | None       # a redirect after POST means TISS rejected the request
    elapsed_ms: float


# --------------------------------------------------------------------------- cookies

@dataclass
class CookieSpec:
    name: str
    value: str
    domain: str = HOST
    path: str = "/"


def load_cookies(path: Path) -> list[CookieSpec]:
    """Accepts a Netscape cookies.txt, a JSON export (e.g. Cookie-Editor) or a raw Cookie header."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"cookie file {path} is empty")

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

    # raw header: "Cookie: a=b; c=d" (copied from a request to /education/...)
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


# --------------------------------------------------------------------------- session

def set_query(url: str, **params: str) -> str:
    s = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k not in params]
    q.extend(params.items())
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), s.fragment))


def _js_unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


class TissSession:
    MAX_HOPS = 12

    def __init__(self, cookies: list[CookieSpec], user_agent: str, timeout_s: float = 20.0,
                 transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            # TISS' Apache keeps idle connections for 200s, so a warmed-up TLS connection is reused
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=150),
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-AT,de;q=0.9,en;q=0.6",
            },
        )
        for c in cookies:
            self.client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        # DeltaSpike window id (the browser generates 1000..9999 as well)
        self.window_id = str(random.randint(1000, 9999))

    def close(self) -> None:
        self.client.close()

    # -- DeltaSpike: a GET needs ?dsrid=X&dswid=W plus cookie dsrwid-X=W, otherwise TISS
    #    answers with the JS "Loading..." window-handler page instead of the real page.
    def _tokenize(self, url: str) -> str:
        s = urlsplit(url)
        if s.hostname != HOST or not s.path.endswith(".xhtml"):
            return url
        rid = str(random.randint(0, 998))
        jar = self.client.cookies.jar
        for c in list(jar):
            if c.name.startswith("dsrwid-"):
                jar.clear(c.domain, c.path, c.name)
        self.client.cookies.set(f"dsrwid-{rid}", self.window_id, domain=HOST, path="/")
        return set_query(url, dsrid=rid, dswid=self.window_id)

    def get(self, url: str) -> Page:
        t0 = time.perf_counter()
        resp = self.client.get(self._tokenize(url))
        return self._settle(resp, t0)

    def _settle(self, resp: httpx.Response, t0: float) -> Page:
        """Follow redirects / window-handler stubs / SAML hops until we have a real TISS page."""
        for _ in range(self.MAX_HOPS):
            url = str(resp.url)
            host = urlsplit(url).hostname or ""
            if resp.is_redirect:
                resp = self.client.get(self._tokenize(urljoin(url, resp.headers["location"])))
                continue
            if host != HOST:
                nxt = self._saml_continue(resp)
                if nxt is None:
                    raise NotLoggedIn(f"redirected to login page {url.split('?')[0]}")
                resp = nxt
                continue
            if urlsplit(url).path.startswith("/admin/authentifizierung"):
                raise NotLoggedIn("TISS asks for authentication")
            if b"handleWindowId" in resp.content[:20000] or b"handleWindowId" in resp.content[-2000:]:
                m = _STUB_RE.search(resp.text)
                if m:
                    log.debug("window-handler stub received, retrying with fresh dsrid")
                    resp = self.client.get(self._tokenize(urljoin(BASE, _js_unescape(m.group(1)))))
                    continue
            return Page(url, resp.status_code, resp.content, (time.perf_counter() - t0) * 1000)
        raise pages.PageError("too many redirects")

    def _saml_continue(self, resp: httpx.Response) -> httpx.Response | None:
        """If the IdP still has a valid session (idp cookies supplied), auto-submit the SAML form."""
        if b"SAMLResponse" not in resp.content:
            return None
        doc = pages.parse(resp.content, str(resp.url))
        for form in doc.forms:
            values = form.form_values()
            if any(n == "SAMLResponse" for n, _ in values):
                log.info("IdP session still valid - completing SAML login automatically")
                return self.client.post(form.action, content=urlencode(values),
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        return None

    def post(self, sub: pages.Submission) -> PostResult:
        """POST a form. TISS answers 200 on success; any redirect means the request was rejected."""
        t0 = time.perf_counter()
        resp = self.client.post(sub.url, content=urlencode(sub.data),
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": BASE, "Referer": sub.url})
        ms = (time.perf_counter() - t0) * 1000
        if resp.is_redirect:
            return PostResult(None, urljoin(str(resp.url), resp.headers.get("location", "")), ms)
        if urlsplit(str(resp.url)).hostname != HOST:
            raise NotLoggedIn("POST answered from outside TISS")
        return PostResult(Page(str(resp.url), resp.status_code, resp.content, ms), None, ms)

    def head_root(self) -> tuple[httpx.Response, float, float]:
        t0 = time.time()
        r = self.client.head(BASE + "/")
        return r, t0, time.time()
