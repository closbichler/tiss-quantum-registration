"""Offline tests against a mock TISS.

The mock mirrors the structure of the real pages as recorded on 2026-10-05 (course and group
registration): button ids, the hidden "really deregister?" dialog button, regForm/confirmForm.

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import logging
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from tissqr import config, pages, register
from tissqr.pages import VIENNA
from tissqr.register import Recorder, Registrar
from tissqr.session import BASE, NotLoggedIn, TissSession, load_cookies
from tissqr.timing import ClockSync

STUB = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html><head><title>Loading...</title></head><body>
<script type="text/javascript">
    var newWindowId = 'uninitializedWindowId';
    var redirectUrl = '\\/education\\/course\\/groupList.xhtml?semester=2026W\\x26courseNr=185A91';
    window.onload = function() { handleWindowId(newWindowId, redirectUrl) }
</script></body></html>"""

GROUP_ID = "groupContentForm:j_id_55:0:j_id_5g:j_id_5j"


def page(form: str, action: str, body: str, vs: str, wid: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html lang="de"><head><title>185.A91 Einführung in die Programmierung 1 | TU Wien</title></head><body>
<div id="contentInner">
<h1><span class="light">185.A91 </span>Einführung in die Programmierung 1
  <small class="rightAlign dropdown"><form id="semesterForm" name="semesterForm" method="post" action="{action}">
  <select name="semesterForm:j_id_26"><option value="2026W" selected="selected">2026W</option></select>
  <input type="hidden" name="semesterForm_SUBMIT" value="1"></form></small></h1>
<div id="subHeader" class="clearfix">2026W, VU, 4.0h, 5.5EC</div>
<form id="{form}" name="{form}" method="post" action="{action}" enctype="application/x-www-form-urlencoded">
<div class="ui-confirm-dialog"><span class="ui-confirm-dialog-message">Wollen Sie sich wirklich abmelden?</span>
  <input id="{form}:confirmOkBtn" name="{form}:confirmOkBtn" type="submit" value="Abmelden">
  <input id="{form}:confirmCancelBtn" name="{form}:confirmCancelBtn" type="button" value="Abbrechen"></div>
{body}
<input type="hidden" name="{form}_SUBMIT" value="1">
<input type="hidden" name="jakarta.faces.ViewState" id="j_id__v_0:jakarta.faces.ViewState:3" value="{vs}" autocomplete="off">
<input type="hidden" id="j_id__v_0:jakarta.faces.ClientWindow:3" name="jakarta.faces.ClientWindow" value="{wid}">
</form></div></body></html>"""


def option(name: str, status: str, begin_id: str, button: str) -> str:
    return f"""
<div class="groupWrapper">
  <div class="groupHeaderWrapper clearfix">
    <div class="header_element titleCol titleColStudent groupHeadertrigger"><span class="bold">{name} </span></div>
    <div class="header_element"><span class="bold">{status} </span><span class="italic">12.10.26-20.10.26</span></div>
    <div class="rightLink">12 / 20</div>
  </div>
  <div class="toggleAll"><fieldset><ol>
    <li><label>Teilnehmende</label><span id="{begin_id.rsplit(':', 1)[0]}:members">12 / </span>20</li>
    <li><label>Beginn der Anmeldung</label><span id="{begin_id}">01.09.2026, 10:00</span></li>
    {f'<li>{button}</li>' if button else ''}
  </ol></fieldset></div>
</div>"""


def button(btn_id: str, value: str) -> str:
    return f'<input id="{btn_id}" name="{btn_id}" type="submit" value="{value}">'


def group_page(vs: str, wid: str, state: str) -> str:
    """state: closed | open | registered (refers to "Gruppe 002"; "Gruppe 001" is open whenever 002 is)"""
    b1 = {"open": button(f"{GROUP_ID}:1:j_id_a2", "Anmelden"),
          "registered": button(f"{GROUP_ID}:1:j_id_a6", "Abmelden")}.get(state, "")
    b0 = button(f"{GROUP_ID}:0:j_id_a2", "Anmelden") if state != "closed" else ""
    status = {"closed": "Anmeldung ab 01.09.26 10:00", "open": "Anmeldung möglich", "registered": "angemeldet"}[state]
    body = (option("Gruppe 001", "Anmeldung möglich", f"{GROUP_ID}:0:appBeginn", b0)
            + option("Gruppe   002", status, f"{GROUP_ID}:1:appBeginn", b1))
    return page("groupContentForm", "/education/course/groupList.xhtml", body, vs, wid)


def course_page(vs: str, wid: str, state: str) -> str:
    btn = {"open": button("registrationForm:j_id_72", "Anmelden"),
           "registered": button("registrationForm:j_id_76", "Abmelden")}.get(state, "")
    body = option("LVA-Anmeldung", "Anmeldung möglich", "registrationForm:begin", btn)
    return page("registrationForm", "/education/course/courseRegistration.xhtml", body, vs, wid)


def confirm_page(vs: str, wid: str, selects: bool) -> str:
    sel = """
<select id="regForm:studyCode" name="regForm:studyCode" size="1">
  <option value="033521" selected="selected">033 521 Informatik</option>
  <option value="033534">033 534 Software Engineering</option>
</select>
<select id="regForm:subgrouplist" name="regForm:subgrouplist" size="3">
  <option value="138806">12.10.2026 14:00 - 14:15</option>
  <option value="138807">12.10.2026 14:15 - 14:30</option>
</select>""" if selects else ""
    return f"""<!DOCTYPE html><html><head><title>Anmeldung | TU Wien</title></head><body><div id="contentInner">
<h1>Anmeldung für Gruppe 002</h1>
<form id="regForm" name="regForm" method="post" action="/education/course/register.xhtml" enctype="application/x-www-form-urlencoded">
<p><span class="bold">Wollen Sie sich wirklich anmelden? Die Anmeldung ist verbindlich, ...</span></p>
<fieldset class="nonStyledFieldset"><div class="leftBlock"><ol>{sel}</ol></div></fieldset>
<ul class="styledCommandBox leftBlock">
  <li><input id="regForm:j_id_34" name="regForm:j_id_34" type="submit" value="Anmelden"></li>
  <li><input id="regForm:j_id_36" name="regForm:j_id_36" type="submit" value="Abbrechen"></li>
</ul>
<input type="hidden" name="regForm_SUBMIT" value="1">
<input type="hidden" name="jakarta.faces.ViewState" id="j_id__v_0:jakarta.faces.ViewState:2" value="{vs}" autocomplete="off">
<input type="hidden" id="j_id__v_0:jakarta.faces.ClientWindow:2" name="jakarta.faces.ClientWindow" value="{wid}">
</form></div></body></html>"""


def result_page(what: str) -> str:
    return f"""<!DOCTYPE html><html><body><div id="contentInner">
<form id="confirmForm" name="confirmForm" method="post" action="/education/course/register.xhtml">
<div class="staticInfoMessage">Sie wurden erfolgreich zur {what} angemeldet.
</div>
<input id="confirmForm:j_id_3c" name="confirmForm:j_id_3c" type="submit" value="Ok" class="primaryButton">
</form></div></body></html>"""


class MockTiss:
    PAGES = {"/education/course/groupList.xhtml": (group_page, f"{GROUP_ID}:1:j_id_a2", 'Gruppe "Gruppe 002"'),
             "/education/course/courseRegistration.xhtml": (course_page, "registrationForm:j_id_72",
                                                            'Lehrveranstaltung "Einführung in die Programmierung 1"')}

    def __init__(self, open_after_polls: int = 2, opens_at: float = 0.0, logged_in: bool = True,
                 selects: bool = False, reject_clicks: int = 0):
        self.polls = 0
        self.open_after = open_after_polls
        self.opens_at = opens_at
        self.logged_in = logged_in
        self.selects = selects
        self.reject_clicks = reject_clicks
        self.registered = False
        self.vs_n = 0
        self.valid_vs: set[str] = set()
        self.confirm_body: dict | None = None
        self.stubs_served = 0
        self.log: list[tuple[float, str, str, bool]] = []   # (time, method, path, open?)

    def _vs(self) -> str:
        self.vs_n += 1
        v = f"VS{self.vs_n:04d}"
        self.valid_vs.add(v)
        return v

    def is_open(self) -> bool:
        return self.polls > self.open_after and time.time() >= self.opens_at

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.log.append((time.time(), req.method, path, self.is_open()))
        if req.method == "HEAD":
            return httpx.Response(200, headers={"date": format_datetime(datetime.now(timezone.utc), usegmt=True)})
        if not self.logged_in and path.startswith("/education"):
            return httpx.Response(302, headers={"location": "/admin/authentifizierung?instant_login=1"})
        if path.startswith("/admin/authentifizierung"):
            return httpx.Response(302, headers={
                "location": "https://idp.zid.tuwien.ac.at/simplesaml/module.php/core/loginuserpass?x=1"})
        if req.url.host == "idp.zid.tuwien.ac.at":
            return httpx.Response(200, text="<html><title>TU Wien Login</title></html>")

        if req.method == "GET" and path in self.PAGES:
            q = req.url.params
            rid, wid = q.get("dsrid"), q.get("dswid")
            if not rid or f"dsrwid-{rid}={wid}" not in req.headers.get("cookie", ""):
                self.stubs_served += 1
                return httpx.Response(200, text=STUB)
            self.polls += 1
            state = "registered" if self.registered else ("open" if self.is_open() else "closed")
            return httpx.Response(200, text=self.PAGES[path][0](self._vs(), wid, state))

        body = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        vs = body.get("jakarta.faces.ViewState")
        if vs not in self.valid_vs:
            return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=invalidViewState"})
        self.valid_vs.discard(vs)  # one-time use

        if path in self.PAGES:
            form = "groupContentForm" if "group" in path else "registrationForm"
            if self.reject_clicks:
                self.reject_clicks -= 1
                return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=viewExpired"})
            if body.get(self.PAGES[path][1]) != "Anmelden" or body.get(f"{form}_SUBMIT") != "1" \
                    or f"{form}:confirmOkBtn" in body:
                return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=badRequest"})
            self.what = self.PAGES[path][2]
            return httpx.Response(200, text=confirm_page(self._vs(), body["jakarta.faces.ClientWindow"], self.selects))
        if path == "/education/course/register.xhtml":
            if body.get("regForm:j_id_34") != "Anmelden" or "regForm:j_id_36" in body:
                return httpx.Response(302, headers={"location": "/education/error.xhtml?errorCode=badRequest"})
            self.confirm_body = body
            self.registered = True
            return httpx.Response(200, text=result_page(self.what))
        return httpx.Response(404)


def make_cfg(tmp: Path, **settings) -> config.Config:
    s = dict(type="group", course="185.A91", semester="2026W", name="Gruppe 002", interval_ms=20, window_s=5)
    s.update(settings)
    lines = [f"{k} = {v}" if isinstance(v, int) else f'{k} = "{v}"' for k, v in s.items() if v is not None]
    (tmp / "config.toml").write_text("\n".join(lines))
    (tmp / "cookies.txt").write_text("Cookie: JSESSIONID=d8~ABC; TISS_AUTH=x; _tiss_session=y")
    return config.load(tmp / "config.toml")


class FlowTest(unittest.TestCase):
    def setUp(self):
        logging.getLogger("tissqr").setLevel(logging.CRITICAL)
        self.tmp = Path(tempfile.mkdtemp())

    def registrar(self, mock, dry_run=False, **settings):
        cfg = make_cfg(self.tmp, **settings)
        sess = TissSession(load_cookies(cfg.cookies), transport=httpx.MockTransport(mock))
        return Registrar(cfg, sess, Recorder(None), dry_run=dry_run)

    def test_group_registration(self):
        mock = MockTiss(open_after_polls=3)
        self.assertEqual(self.registrar(mock).run(), 0)
        self.assertTrue(mock.registered)
        self.assertEqual(mock.stubs_served, 0, "window handshake should avoid the loading stub")
        self.assertEqual(mock.confirm_body["regForm_SUBMIT"], "1")
        # once the button is visible: exactly click + confirm, no further requests
        self.assertEqual([(m, p) for _, m, p, _ in mock.log[-3:]],
                         [("GET", "/education/course/groupList.xhtml"),
                          ("POST", "/education/course/groupList.xhtml"),
                          ("POST", "/education/course/register.xhtml")])

    def test_course_registration(self):
        mock = MockTiss(open_after_polls=1)
        self.assertEqual(self.registrar(mock, type="course", name=None).run(), 0)
        self.assertTrue(mock.registered)
        self.assertEqual(mock.log[-2][2], "/education/course/courseRegistration.xhtml")

    def test_confirm_fields(self):
        mock = MockTiss(open_after_polls=0, selects=True)
        reg = self.registrar(mock, study_code="033534", slot="14:15 - 14:30")
        self.assertEqual(reg.run(), 0)
        self.assertEqual(mock.confirm_body["regForm:studyCode"], "033534")
        self.assertEqual(mock.confirm_body["regForm:subgrouplist"], "138807")

    def test_rejected_click_is_retried(self):
        mock = MockTiss(open_after_polls=0, reject_clicks=2)
        self.assertEqual(self.registrar(mock).run(), 0)
        self.assertTrue(mock.registered)

    def test_dry_run_stops_before_confirm(self):
        mock = MockTiss(open_after_polls=1)
        self.assertEqual(self.registrar(mock, dry_run=True).run(), 0)
        self.assertFalse(mock.registered)
        self.assertIsNone(mock.confirm_body)
        self.assertEqual(mock.log[-1][1:3], ("POST", "/education/course/groupList.xhtml"))

    def test_already_registered(self):
        mock = MockTiss()
        mock.registered = True
        self.assertEqual(self.registrar(mock).run(), 0)
        self.assertEqual(len(mock.log), 1)

    def test_not_logged_in(self):
        mock = MockTiss(logged_in=False)
        with self.assertRaises(NotLoggedIn):
            self.registrar(mock).run()

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

    def test_poll_arrives_right_after_opening(self):
        opens_at = time.time() + 2.0
        mock = MockTiss(open_after_polls=0, opens_at=opens_at)
        reg = self.registrar(mock, start=datetime.fromtimestamp(opens_at, VIENNA).isoformat())
        reg.sync = ClockSync(0.0, 0.0, 0.002)   # mock: same clock, ~no latency
        self.assertEqual(reg.run(), 0)
        gets = [(t, is_open) for t, m, _, is_open in mock.log if m == "GET"]
        self.assertGreater(sum(1 for _, o in gets if not o), 10, "should poll before the opening")
        first_open = next(t for t, o in gets if o)
        self.assertLess(first_open - opens_at, 0.06, "one poll should arrive just after the opening")
        self.assertTrue(mock.registered)

    def test_wait_keeps_session_alive_and_resyncs(self):
        opens_at = time.time() + 1.5
        mock = MockTiss(open_after_polls=0, opens_at=opens_at)
        reg = self.registrar(mock, start=datetime.fromtimestamp(opens_at, VIENNA).isoformat())
        syncs = []
        reg.clock_sync = lambda: syncs.append(time.time())
        old = register.KEEPALIVE_S, register.RESYNC_S
        register.KEEPALIVE_S, register.RESYNC_S = 0.3, 0.5
        try:
            self.assertEqual(reg._wait(reg.cfg.start), None)
        finally:
            register.KEEPALIVE_S, register.RESYNC_S = old
        self.assertGreaterEqual(len([1 for _, m, _, _ in mock.log if m == "GET"]), 2)
        self.assertEqual(len(syncs), 1)
        self.assertAlmostEqual(syncs[0], opens_at - 0.5, delta=0.1)


class ParseTest(unittest.TestCase):
    def doc(self, html: str):
        return pages.parse(html.encode(), BASE + "/education/course/groupList.xhtml")

    def test_course_header(self):
        doc = self.doc(group_page("V", "1", "open"))
        self.assertEqual(pages.course_number(doc), "185.A91")
        self.assertEqual(pages.course_title(doc), "Einführung in die Programmierung 1")
        self.assertEqual(pages.sub_header(doc), "2026W, VU, 4.0h, 5.5EC")

    def test_find_group(self):
        doc = self.doc(group_page("V", "1", "open"))
        w = pages.find_option(doc, "group", "gruppe 002")
        self.assertEqual(pages.option_header(w), "Gruppe 002 | Anmeldung möglich 12.10.26-20.10.26 | 12 / 20")
        self.assertEqual(pages.registration_start(w), datetime(2026, 9, 1, 10, 0, tzinfo=VIENNA))
        self.assertIsNone(pages.find_option(doc, "group", "Gruppe 00"))

    def test_find_exam_by_header_regex(self):
        doc = self.doc(group_page("V", "1", "open"))
        self.assertEqual(pages.option_name(pages.find_option(doc, "exam", r"002.*möglich")), "Gruppe 002")

    def test_buttons(self):
        doc = self.doc(group_page("V", "1", "registered"))
        w = pages.find_option(doc, "group", "Gruppe 002")
        self.assertIsNone(pages.find_button(w))
        self.assertEqual(pages.find_button(w, pages.UNREGISTER).get("id"), f"{GROUP_ID}:1:j_id_a6")
        # the deregistration dialog button (form level) never counts as "registered"
        self.assertIsNone(pages.find_button(pages.find_option(doc, "group", "Gruppe 001"), pages.UNREGISTER))

    def test_course_page(self):
        doc = self.doc(course_page("V", "1", "open"))
        w = pages.find_option(doc, "course")
        self.assertEqual(pages.option_name(w), "LVA-Anmeldung")
        self.assertEqual(pages.registration_start(w).hour, 10)
        self.assertEqual(pages.find_button(w).get("id"), "registrationForm:j_id_72")

    def test_submission_contains_jsf_fields(self):
        doc = self.doc(group_page("VSX", "4321", "open"))
        sub = pages.build_submission(pages.find_button(pages.find_option(doc, "group", "Gruppe 002")))
        self.assertEqual(sub.url, BASE + "/education/course/groupList.xhtml")
        self.assertEqual(dict(sub.data), {"groupContentForm_SUBMIT": "1", "jakarta.faces.ViewState": "VSX",
                                          "jakarta.faces.ClientWindow": "4321",
                                          f"{GROUP_ID}:1:j_id_a2": "Anmelden"})

    def test_classify(self):
        self.assertEqual(pages.classify_result('Sie wurden erfolgreich zur Gruppe "Observers" angemeldet.'), "success")
        self.assertEqual(pages.classify_result(
            'Sie wurden erfolgreich zur Lehrveranstaltung "Präsentation und Moderation" angemeldet.'), "success")
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


class ConfigTest(unittest.TestCase):
    def load(self, text: str) -> config.Config:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.toml").write_text(text, encoding="utf-8")
        return config.load(tmp / "config.toml")

    def test_minimal(self):
        cfg = self.load('type = "course"\ncourse = "185.a62"\nsemester = "2026w"\nstart = 2026-10-12T10:00:00\n')
        self.assertEqual(cfg.url, BASE + "/education/course/courseRegistration.xhtml?courseNr=185A62&semester=2026W")
        self.assertEqual(cfg.start, datetime(2026, 10, 12, 10, 0, tzinfo=VIENNA))
        self.assertEqual((cfg.interval_ms, cfg.window_s), (200, 90))

    def test_errors(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            config.load(Path(tempfile.mkdtemp()) / "config.toml")
        with self.assertRaisesRegex(ValueError, "not valid TOML"):
            self.load('type = group\n')
        with self.assertRaisesRegex(ValueError, "unknown setting"):
            self.load('[target]\ntype = "group"\n')
        with self.assertRaisesRegex(ValueError, "name is required"):
            self.load('type = "group"\ncourse = "185.A91"\nsemester = "2026W"\n')
        with self.assertRaisesRegex(ValueError, "type must be"):
            self.load('type = "lva"\ncourse = "185.A91"\nsemester = "2026W"\n')
        with self.assertRaisesRegex(ValueError, "regular expression"):
            self.load('type = "exam"\ncourse = "185.A91"\nsemester = "2026W"\nname = "VO ("\n')

    def test_durations(self):
        self.assertEqual(register.fmt_duration(65), "1m 05s")
        self.assertEqual(register.fmt_duration(3 * 3600 + 5), "3h 00m 05s")
        self.assertEqual(register.fmt_duration(2 * 86400 + 3600 + 120), "2d 1h 02m")


if __name__ == "__main__":
    unittest.main()
