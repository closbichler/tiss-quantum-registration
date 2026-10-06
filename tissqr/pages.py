"""HTML parsing of the TISS registration pages (no rendering, just lxml)."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import lxml.html
from lxml.html import HtmlElement

VIENNA = ZoneInfo("Europe/Vienna")
_local = threading.local()   # one parser per thread: a shared lxml parser parses one page at a time

REGISTER = ("Anmelden", "Register", "Voranmeldung", "Voranmelden", "Preregistration")
UNREGISTER = ("Abmelden", "Deregistration", "Unregister")
CANCEL = ("Abbrechen", "Cancel", "Zurück", "Back")


class PageError(Exception):
    """The page did not look like we expected."""


def norm(s: str | None) -> str:
    return " ".join((s or "").split())


def parse(content: bytes, url: str) -> HtmlElement:
    parser = getattr(_local, "parser", None)
    if parser is None:
        parser = _local.parser = lxml.html.HTMLParser(encoding="utf-8")
    return lxml.html.document_fromstring(content, parser=parser, base_url=url)


def _has_class(cls: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')"


def course_number(doc: HtmlElement) -> str:
    spans = doc.xpath("//*[@id='contentInner']//h1/span")
    return norm(spans[0].text_content()) if spans else ""


def course_title(doc: HtmlElement) -> str:
    """<h1><span>185.A62 </span>Präsentation und Moderation <small>...</small></h1>"""
    spans = doc.xpath("//*[@id='contentInner']//h1/span")
    return norm(spans[0].tail) if spans else ""


def sub_header(doc: HtmlElement) -> str:
    el = doc.xpath("//*[@id='subHeader']")
    return norm(el[0].text_content()) if el else ""


def wrappers(doc: HtmlElement) -> list[HtmlElement]:
    """Each option (course, group, exam) is a div.groupWrapper: header row, details, button."""
    inner = doc.xpath("//*[@id='contentInner']")
    root = inner[0] if inner else doc
    # a plain contains() first: half the time of matching the exact class in XPath (this runs on every reload)
    return [el for el in root.xpath(".//*[contains(@class, 'groupWrapper')]")
            if "groupWrapper" in el.get("class", "").split()]


def option_name(wrapper: HtmlElement) -> str:
    span = next(wrapper.iter("span"), None)
    return norm(span.text_content()) if span is not None else ""


def option_header(wrapper: HtmlElement) -> str:
    """Header row, e.g. 'Observers | Anmeldung möglich 16.09.26-02.03.27 | 0 / ∞'."""
    head = wrapper.xpath(f".//*[{_has_class('groupHeaderWrapper')}]")
    if not head:
        return option_name(wrapper)
    return " | ".join(t for t in (norm(el.text_content()) for el in head[0]) if t)


def registration_start(wrapper: HtmlElement) -> datetime | None:
    """'Beginn der Anmeldung'. The span id ends with :begin (course) or :appBeginn (group, exam)."""
    for span in wrapper.xpath(".//span[contains(@id, 'egin')]"):
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4}),?\s+(\d{1,2}):(\d{2})", span.text_content())
        if m:
            d, mo, y, h, mi = map(int, m.groups())
            return datetime(y, mo, d, h, mi, tzinfo=VIENNA)
    return None


def find_option(doc: HtmlElement, kind: str, name: str = "") -> HtmlElement | None:
    """course: the only option on the page; group: exact name; exam: regex anywhere in the header row."""
    ws = wrappers(doc)
    if kind == "course":
        return ws[0] if ws else None
    if kind == "group":
        want = norm(name).lower()
        return next((w for w in ws if option_name(w).lower() == want), None)
    rx = re.compile(name, re.I)
    return next((w for w in ws if rx.search(option_header(w))), None)


def find_button(scope: HtmlElement | None, labels=REGISTER) -> HtmlElement | None:
    if scope is None:
        return None
    wanted = {norm(l).lower() for l in labels}
    for el in scope.iter("input", "button"):
        if el.tag == "input" and (el.get("type") or "").lower() not in ("submit", "button"):
            continue
        # confirmOkBtn belongs to the hidden "really deregister?" dialog
        if el.get("disabled") is not None or "confirmOkBtn" in (el.get("id") or ""):
            continue
        label = el.get("value") if el.tag == "input" else (el.text_content() or el.get("value"))
        if norm(label).lower() in wanted:
            return el
    return None


def form_by_id(doc: HtmlElement, form_id: str) -> HtmlElement | None:
    for f in doc.forms:
        fid = f.get("id") or f.get("name") or ""
        if fid == form_id or fid.endswith(":" + form_id):
            return f
    return None


def confirm_button(reg_form: HtmlElement) -> HtmlElement | None:
    btn = find_button(reg_form)
    if btn is not None:
        return btn
    cancel = {c.lower() for c in CANCEL}
    for el in reg_form.xpath(".//input[@type='submit']"):
        if norm(el.get("value")).lower() not in cancel:
            return el
    return None


def select_options(form: HtmlElement, name_suffix: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Return (select name, [(value, text)]) for a select whose name ends with name_suffix."""
    for sel in form.xpath(".//select"):
        name = sel.get("name") or ""
        if name.endswith(name_suffix):
            return name, [(o.get("value", ""), norm(o.text_content())) for o in sel.xpath(".//option")]
    return None


def result_message(doc: HtmlElement) -> str:
    el = doc.xpath(f"//form[contains(@id, 'confirmForm')]//*[{_has_class('staticInfoMessage')}]")
    if el:
        return norm(el[0].text_content())
    el = doc.xpath(f"//*[{_has_class('staticInfoMessage')}]")
    return norm(el[0].text_content()) if el else ""


def messages(doc: HtmlElement) -> list[str]:
    """Collect info/error messages TISS shows on a page (for logging)."""
    out: list[str] = []
    xp = ("//*[@id='globalMessagesPanel']"
          f"|//*[{_has_class('staticInfoMessage')}]"
          f"|//*[{_has_class('ui-messages')}]"
          f"|//*[{_has_class('errorMessage')}]"
          f"|//*[{_has_class('infoMessage')}]")
    for el in doc.xpath(xp):
        if el.xpath("ancestor::noscript"):
            continue
        t = norm(el.text_content())
        if t and t not in out:
            out.append(t[:400])
    return out


SUCCESS_RE = re.compile(r"sie wurden erfolgreich zur.*angemeldet|you successfully registered for", re.I)
ALREADY_RE = re.compile(r"ist bereits gruppenmitglied|already group member|bereits angemeldet|already registered", re.I)
PREREG_RE = re.compile(r"anmeldewunsch.*wurde erfasst|registration request for.*has been recorded", re.I)
WAITLIST_RE = re.compile(r"warteliste|waiting list", re.I)


def classify_result(text: str) -> str:
    """Map the TISS result message to waitlist|prereg|success|already|unknown.

    The waiting list comes first: its message also says "Ihr Anmeldewunsch ... wurde erfasst"."""
    if WAITLIST_RE.search(text):
        return "waitlist"
    if PREREG_RE.search(text):
        return "prereg"
    if SUCCESS_RE.search(text):
        return "success"
    if ALREADY_RE.search(text):
        return "already"
    return "unknown"


@dataclass
class Submission:
    url: str
    data: list[tuple[str, str]]


def enclosing_form(el: HtmlElement) -> HtmlElement | None:
    p = el
    while p is not None and p.tag != "form":
        p = p.getparent()
    return p


def build_submission(button: HtmlElement, overrides: dict[str, str] | None = None) -> Submission:
    """What the browser sends when `button` is clicked, e.g. groupContentForm_SUBMIT=1,
    jakarta.faces.ViewState=..., jakarta.faces.ClientWindow=..., groupContentForm:...:j_id_a2=Anmelden"""
    form = enclosing_form(button)
    if form is None:
        raise PageError(f"button {button.get('id')} is not inside a form")
    overrides = dict(overrides or {})
    # lxml includes <input type="button"> (e.g. the dialog's confirmCancelBtn), a browser never sends it
    skip = {el.get("name") for el in form.xpath(".//input[@type='button']")}
    data: list[tuple[str, str]] = []
    for name, value in form.form_values():
        if name in skip:
            continue
        if name in overrides:
            value = overrides.pop(name)
        data.append((name, value))
    data.extend(overrides.items())
    name = button.get("name") or button.get("id")
    if name:
        data.append((name, button.get("value") or ""))
    return Submission(url=form.action, data=data)
