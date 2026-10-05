"""Offline tests against a mock TISS (structure modelled on the real pages, not live data).

Run: .venv/bin/python -m unittest -v
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from tissqr import config, pages
from tissqr.register import Recorder, Registrar
from tissqr.session import BASE, NotLoggedIn, TissSession, load_cookies

STUB = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html><head><title>Loading...</title></head><body>
<script type="text/javascript">
    var newWindowId = 'uninitializedWindowId';
    var redirectUrl = '\\/education\\/course\\/groupList.xhtml?semester=2026W\\x26courseNr=185A91';
    window.onload = function() { handleWindowId(newWindowId, redirectUrl) }
</script></body></html>"""


def group_page(vs: str, wid: str, state: str) -> str:
    """state: closed | open | registered"""
    def option(idx: int, name: str, btn: str) -> str:
        return f"""
<div class="groupWrapper">
  <div class="groupHeaderWrapper"><div class="header_element"><span class="bold">{name}</span> Mo 10:00</div></div>
  <ol>
    <li><label>Teilnehmer*innen</label><span id="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:{idx}:members">12</span> / 20</li>
    <li><label>Anmeldebeginn</label><span id="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:{idx}:appBeginn">12.10.2026, 10:00</span></li>
  </ol>
  {btn}
</div>"""

    if state == "open":
        b1 = '<input id="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a1" type="submit" ' \
             'name="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a1" value="Anmelden" class="button" />'
    elif state == "registered":
        b1 = '<input id="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a2" type="submit" ' \
             'name="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a2" value="Abmelden" class="button" />'
    else:
        b1 = ""
    b0 = '<input id="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:0:j_id_a1" type="submit" ' \
         'name="groupContentForm:j_id_52:0:j_id_5d:j_id_5g:0:j_id_a1" value="Anmelden" />' if state != "closed" else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html><head><title>185.A91 Test</title></head><body>
<div id="contentInner">
<h1><span class="light">185.A91 </span> Einführung in die Programmierung 1 </h1>
<div id="subHeader" class="clearfix">2026W, VU, 4.0h, 5.5EC</div>
<form id="groupContentForm" name="groupContentForm" method="post" action="/education/course/groupList.xhtml" enctype="application/x-www-form-urlencoded">
<input type="hidden" name="groupContentForm_SUBMIT" value="1" />
<h2>Übungsgruppen</h2>
{option(0, "Gruppe 001", b0)}
{option(1, "Gruppe   002", b1)}
<input type="hidden" name="jakarta.faces.ViewState" id="j_id__v_0:jakarta.faces.ViewState:1" value="{vs}" autocomplete="off" />
<input type="hidden" id="j_id__v_0:jakarta.faces.ClientWindow:1" name="jakarta.faces.ClientWindow" value="{wid}" />
</form></div></body></html>"""


def confirm_page(vs: str, wid: str) -> str:
    return f"""<!DOCTYPE html><html><body><div id="contentInner">
<form id="regForm" name="regForm" method="post" action="/education/course/register.xhtml" enctype="application/x-www-form-urlencoded">
<input type="hidden" name="regForm_SUBMIT" value="1" />
<select id="regForm:studyCode" name="regForm:studyCode" size="1">
  <option value="033521" selected="selected">033 521 Informatik</option>
  <option value="033534">033 534 Software Engineering</option>
</select>
<select id="regForm:subgrouplist" name="regForm:subgrouplist" size="3">
  <option value="138806">12.10.2026 14:00 - 14:15</option>
  <option value="138807">12.10.2026 14:15 - 14:30</option>
</select>
<input id="regForm:j_id_30" type="submit" name="regForm:j_id_30" value="Anmelden" />
<input id="regForm:j_id_31" type="submit" name="regForm:j_id_31" value="Abbrechen" />
<input type="hidden" name="jakarta.faces.ViewState" value="{vs}" />
<input type="hidden" name="jakarta.faces.ClientWindow" value="{wid}" />
</form></div></body></html>"""


RESULT = """<!DOCTYPE html><html><body><div id="contentInner">
<form id="confirmForm" name="confirmForm" method="post" action="/education/course/register.xhtml">
<span class="staticInfoMessage">Sie wurden erfolgreich zur Gruppe Gruppe 002 angemeldet.</span>
<input type="submit" name="confirmForm:ok" value="Ok" />
</form></div></body></html>"""


class MockTiss:
    def __init__(self, open_after_polls: int = 2, logged_in: bool = True):
        self.polls = 0
        self.open_after = open_after_polls
        self.logged_in = logged_in
        self.registered = False
        self.vs_n = 0
        self.valid_vs: set[str] = set()
        self.confirm_body: dict | None = None
        self.stubs_served = 0

    def _vs(self) -> str:
        self.vs_n += 1
        v = f"VS{self.vs_n:04d}"
        self.valid_vs.add(v)
        return v

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "HEAD":
            return httpx.Response(200, headers={"date": format_datetime(datetime.now(timezone.utc), usegmt=True)})
        if not self.logged_in and path.startswith("/education"):
            return httpx.Response(302, headers={"location": "/admin/authentifizierung?instant_login=1"})
        if path.startswith("/admin/authentifizierung"):
            return httpx.Response(302, headers={
                "location": "https://idp.zid.tuwien.ac.at/simplesaml/module.php/core/loginuserpass?x=1"})
        if req.url.host == "idp.zid.tuwien.ac.at":
            return httpx.Response(200, text="<html><title>TU Wien Login</title></html>")

        if req.method == "GET" and path == "/education/course/groupList.xhtml":
            q = req.url.params
            rid, wid = q.get("dsrid"), q.get("dswid")
            cookies = req.headers.get("cookie", "")
            if not rid or f"dsrwid-{rid}={wid}" not in cookies:
                self.stubs_served += 1
                return httpx.Response(200, text=STUB)
            self.polls += 1
            state = "registered" if self.registered else (
                "open" if self.polls > self.open_after else "closed")
            return httpx.Response(200, text=group_page(self._vs(), wid, state))

        body = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        vs = body.get("jakarta.faces.ViewState")
        if vs not in self.valid_vs:
            return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=invalidViewState"})
        self.valid_vs.discard(vs)  # one-time use

        if path == "/education/course/groupList.xhtml":
            key = "groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a1"
            if body.get(key) != "Anmelden" or body.get("groupContentForm_SUBMIT") != "1":
                return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=badRequest"})
            return httpx.Response(200, text=confirm_page(self._vs(), body["jakarta.faces.ClientWindow"]))
        if path == "/education/course/register.xhtml":
            if body.get("regForm:j_id_30") != "Anmelden" or "regForm:j_id_31" in body:
                return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=badRequest"})
            self.confirm_body = body
            self.registered = True
            return httpx.Response(200, text=RESULT)
        return httpx.Response(404)


def make_cfg(tmp: Path, **target) -> config.Config:
    t = dict(type="group", course="185.A91", semester="2026W", name="Gruppe 002")
    t.update(target)
    lines = ["[target]"] + [f'{k} = "{v}"' for k, v in t.items()]
    lines += ["[schedule]", "interval_ms = 20", "window_s = 5", "use_server_clock = false",
              "[log]", 'dir = "logs"', "save_html = false"]
    (tmp / "config.toml").write_text("\n".join(lines))
    (tmp / "cookies.txt").write_text("Cookie: JSESSIONID=d8~ABC; TISS_AUTH=x; _tiss_session=y")
    return config.load(tmp / "config.toml")


class FlowTest(unittest.TestCase):
    def setUp(self):
        logging.getLogger("tissqr").setLevel(logging.CRITICAL)
        self.tmp = Path(tempfile.mkdtemp())

    def registrar(self, mock, dry_run=None, **target):
        cfg = make_cfg(self.tmp, **target)
        sess = TissSession(load_cookies(cfg.cookies_file), "test", transport=httpx.MockTransport(mock))
        return Registrar(cfg, sess, Recorder(None), dry_run=dry_run)

    def test_full_registration(self):
        mock = MockTiss(open_after_polls=3)
        reg = self.registrar(mock, study_code="033534", slot="14:15 - 14:30")
        self.assertEqual(reg.run(now=True), 0)
        self.assertTrue(mock.registered)
        self.assertEqual(mock.stubs_served, 0, "window handshake should avoid the loading stub")
        self.assertEqual(mock.confirm_body["regForm:studyCode"], "033534")
        self.assertEqual(mock.confirm_body["regForm:subgrouplist"], "138807")
        self.assertEqual(mock.confirm_body["regForm_SUBMIT"], "1")

    def test_dry_run_detect_does_not_post(self):
        mock = MockTiss(open_after_polls=1)
        self.assertEqual(self.registrar(mock, dry_run="detect").run(now=True), 0)
        self.assertFalse(mock.registered)
        self.assertEqual(mock.vs_n, mock.polls)  # no confirm page was ever requested

    def test_dry_run_confirm_stops_before_confirm(self):
        mock = MockTiss(open_after_polls=1)
        self.assertEqual(self.registrar(mock, dry_run="confirm").run(now=True), 0)
        self.assertFalse(mock.registered)
        self.assertIsNone(mock.confirm_body)

    def test_already_registered(self):
        mock = MockTiss()
        mock.registered = True
        self.assertEqual(self.registrar(mock).run(now=True), 0)

    def test_not_logged_in(self):
        mock = MockTiss(logged_in=False)
        with self.assertRaises(NotLoggedIn):
            self.registrar(mock).run(now=True)

    def test_window_stub_is_resolved(self):
        """If the first GET yields the loading stub, the redirect target is fetched with a handshake."""
        mock = MockTiss()
        reg = self.registrar(mock)
        real, calls = reg.sess._tokenize, []

        def flaky(url):
            calls.append(url)
            return url if len(calls) == 1 else real(url)
        reg.sess._tokenize = flaky
        st = reg.inspect()
        self.assertEqual(mock.stubs_served, 1)
        self.assertIsNotNone(st.option)

    def test_scheduled_start_waits(self):
        mock = MockTiss(open_after_polls=0)
        reg = self.registrar(mock)
        reg.cfg.schedule.start = datetime.now().astimezone() + timedelta(seconds=1.2)
        reg.cfg.schedule.lead_ms = 200
        self.assertEqual(reg.run(), 0)
        self.assertGreaterEqual(datetime.now().astimezone(), reg.cfg.schedule.start - timedelta(milliseconds=250))
        self.assertTrue(mock.registered)


class ParseTest(unittest.TestCase):
    def test_find_group_with_whitespace(self):
        doc = pages.parse(group_page("V", "1", "open").encode(), BASE + "/x")
        w = pages.find_option(doc, "group", "Gruppe 002")
        self.assertIsNotNone(w)
        self.assertEqual(pages.option_id(w), "j_id_52:0:j_id_5d:j_id_5g:1")
        self.assertEqual(pages.registration_start(w).hour, 10)
        self.assertEqual(pages.course_number(doc), "185.A91")

    def test_find_by_option_id(self):
        doc = pages.parse(group_page("V", "1", "open").encode(), BASE + "/x")
        w = pages.find_option(doc, "group", opt_id="j_id_52:0:j_id_5d:j_id_5g:0")
        self.assertEqual(pages.option_name(w), "Gruppe 001")

    def test_submission_contains_jsf_fields(self):
        doc = pages.parse(group_page("VSX", "4321", "open").encode(), BASE + "/education/course/groupList.xhtml")
        w = pages.find_option(doc, "group", "Gruppe 002")
        sub = pages.build_submission(pages.find_button(w, ["Anmelden"]))
        d = dict(sub.data)
        self.assertEqual(sub.url, BASE + "/education/course/groupList.xhtml")
        self.assertEqual(d["jakarta.faces.ViewState"], "VSX")
        self.assertEqual(d["jakarta.faces.ClientWindow"], "4321")
        self.assertEqual(d["groupContentForm_SUBMIT"], "1")
        self.assertEqual(d["groupContentForm:j_id_52:0:j_id_5d:j_id_5g:1:j_id_a1"], "Anmelden")
        self.assertNotIn("groupContentForm:j_id_52:0:j_id_5d:j_id_5g:0:j_id_a1", d)

    def test_classify(self):
        self.assertEqual(pages.classify_result("Sie wurden erfolgreich zur Gruppe X angemeldet."), "success")
        self.assertEqual(pages.classify_result("You are on the waiting list"), "waitlist")
        self.assertEqual(pages.classify_result(
            "Ihr Anmeldewunsch für X wurde erfasst. Erst nach Bestätigung durch ..."), "prereg")

    def test_cookie_formats(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.txt").write_text("# Netscape HTTP Cookie File\n#HttpOnly_tiss.tuwien.ac.at\tFALSE\t/education\tTRUE\t0\tJSESSIONID\tabc\n")
        (tmp / "b.json").write_text('[{"name":"TISS_AUTH","value":"z","domain":"tiss.tuwien.ac.at","path":"/"}]')
        (tmp / "c.txt").write_text("JSESSIONID=d8~1; TISS_AUTH=2")
        a, = load_cookies(tmp / "a.txt")
        self.assertEqual((a.name, a.value, a.path), ("JSESSIONID", "abc", "/education"))
        self.assertEqual(load_cookies(tmp / "b.json")[0].value, "z")
        c = load_cookies(tmp / "c.txt")
        self.assertEqual([(x.name, x.path) for x in c], [("JSESSIONID", "/education"), ("TISS_AUTH", "/")])


if __name__ == "__main__":
    unittest.main()
