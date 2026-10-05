"""HTML parsing of the TISS registration pages (no rendering, just lxml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import lxml.html
from lxml.html import HtmlElement

VIENNA = ZoneInfo("Europe/Vienna")
_PARSER = lxml.html.HTMLParser(encoding="utf-8")


class PageError(Exception):
    """The page did not look like we expected."""


def norm(s: str | None) -> str:
    return " ".join((s or "").split())


def parse(content: bytes, url: str) -> HtmlElement:
    return lxml.html.document_fromstring(content, parser=_PARSER, base_url=url)


def _has_class(cls: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')"


def _closest(el: HtmlElement, cls: str) -> HtmlElement | None:
    while el is not None:
        if cls in (el.get("class") or "").split():
            return el
        el = el.getparent()
    return None


# --------------------------------------------------------------------------- course header

def course_number(doc: HtmlElement) -> str:
    spans = doc.xpath("//*[@id='contentInner']//h1/span")
    return norm(spans[0].text_content()) if spans else ""


def course_title(doc: HtmlElement) -> str:
    h1 = doc.xpath("//*[@id='contentInner']//h1")
    return norm(h1[0].text) if h1 else ""


def sub_header(doc: HtmlElement) -> str:
    el = doc.xpath("//*[@id='subHeader']")
    return norm(el[0].text_content()) if el else ""


# --------------------------------------------------------------------------- options (groups/exams/lva)

def wrappers(doc: HtmlElement) -> list[HtmlElement]:
    inner = doc.xpath("//*[@id='contentInner']")
    root = inner[0] if inner else doc
    return root.xpath(f".//*[{_has_class('groupWrapper')}]")


def option_name(wrapper: HtmlElement) -> str:
    span = wrapper.xpath(".//span")
    return norm(span[0].text_content()) if span else ""


def option_header(wrapper: HtmlElement) -> str:
    """Full header text, e.g. exam name + date."""
    h = wrapper.xpath(f".//*[{_has_class('header_element')}]")
    if h:
        return norm(h[0].text_content())
    h = wrapper.xpath(f".//*[{_has_class('groupHeaderWrapper')}]")
    return norm(h[0].text_content()) if h else option_name(wrapper)


def option_id(wrapper: HtmlElement) -> str:
    """Stable-ish JSF id of an option, taken from its `...:members` span."""
    for span in wrapper.xpath(".//span[contains(@id, 'members')]"):
        m = re.search(r"Form:(.*):members", span.get("id"))
        if m:
            return m.group(1)
    return ""


def registration_start(wrapper: HtmlElement) -> datetime | None:
    """Registration start shown on the option ("dd.mm.yyyy, HH:MM", Vienna time)."""
    for span in wrapper.xpath(".//span[contains(@id, 'egin')]"):
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4}),?\s+(\d{1,2}):(\d{2})", span.text_content())
        if m:
            d, mo, y, h, mi = map(int, m.groups())
            return datetime(y, mo, d, h, mi, tzinfo=VIENNA)
    return None


def find_option(doc: HtmlElement, kind: str, name: str = "", date: str = "",
                opt_id: str = "") -> HtmlElement | None:
    ws = wrappers(doc)
    if opt_id:
        for w in ws:
            if option_id(w) == opt_id:
                return w
        return None
    if kind == "lva":
        # the LVA registration page has exactly one option
        return ws[0] if ws else None
    if kind == "group":
        want = norm(name).lower()
        for w in ws:
            if option_name(w).lower() == want:
                return w
        # fallback like the original userscript: any span in the header equals the name
        for w in ws:
            for span in w.xpath(f".//*[{_has_class('header_element')}]//span"):
                if norm(span.text_content()).lower() == want:
                    return _closest(span, "groupWrapper")
        return None
    if kind == "exam":
        rx = re.compile(name, re.I) if name else None
        for w in ws:
            if rx and not rx.search(option_name(w)):
                continue
            if date and not (date in option_header(w) or re.search(date, option_header(w))):
                continue
            return w
        return None
    raise ValueError(f"unknown registration type {kind!r}")


def find_button(scope: HtmlElement | None, labels: list[str]) -> HtmlElement | None:
    if scope is None:
        return None
    wanted = {norm(l).lower() for l in labels}
    for el in scope.iter("input", "button"):
        if el.tag == "input" and (el.get("type") or "").lower() not in ("submit", "button"):
            continue
        # skip disabled buttons and the hidden "confirm deregistration" dialog button
        if el.get("disabled") is not None or "confirmOkBtn" in (el.get("id") or ""):
            continue
        label = el.get("value") if el.tag == "input" else (el.text_content() or el.get("value"))
        if norm(label).lower() in wanted:
            return el
    return None


# --------------------------------------------------------------------------- confirm / result pages

def form_by_id(doc: HtmlElement, form_id: str) -> HtmlElement | None:
    for f in doc.forms:
        fid = f.get("id") or f.get("name") or ""
        if fid == form_id or fid.endswith(":" + form_id):
            return f
    return None


def confirm_button(reg_form: HtmlElement, labels: list[str]) -> HtmlElement | None:
    btn = find_button(reg_form, labels)
    if btn is not None:
        return btn
    # fallback: the first submit input that is not a cancel button
    for el in reg_form.xpath(".//input[@type='submit']"):
        if norm(el.get("value")).lower() not in ("abbrechen", "cancel", "zurück", "back"):
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
    """Map the TISS result message to success|prereg|already|waitlist|unknown."""
    if PREREG_RE.search(text):
        return "prereg"
    if SUCCESS_RE.search(text):
        return "success"
    if ALREADY_RE.search(text):
        return "already"
    if WAITLIST_RE.search(text):
        return "waitlist"
    return "unknown"


# --------------------------------------------------------------------------- form submission

@dataclass
class Submission:
    url: str
    data: list[tuple[str, str]]
    button: str


def enclosing_form(el: HtmlElement) -> HtmlElement | None:
    p = el
    while p is not None and p.tag != "form":
        p = p.getparent()
    return p


def build_submission(button: HtmlElement, overrides: dict[str, str] | None = None) -> Submission:
    """Replicate a click on `button`: all successful form controls + the button itself.

    This yields exactly what the browser sends, e.g. for a group:
      groupContentForm_SUBMIT=1, jakarta.faces.ViewState=..., jakarta.faces.ClientWindow=...,
      groupContentForm:j_id_..:j_id_a1=Anmelden
    """
    form = enclosing_form(button)
    if form is None:
        raise PageError(f"button {button.get('id')} is not inside a form")
    overrides = dict(overrides or {})
    data: list[tuple[str, str]] = []
    for name, value in form.form_values():
        if name in overrides:
            value = overrides.pop(name)
        data.append((name, value))
    data.extend(overrides.items())  # e.g. a select that had no default selection
    name = button.get("name") or button.get("id")
    if name:
        data.append((name, button.get("value") or ""))
    return Submission(url=form.action, data=data, button=name or "")
