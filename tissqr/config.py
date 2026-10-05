"""Configuration file (TOML) loading."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .session import BASE

PAGES = {"lva": "courseRegistration", "group": "groupList", "exam": "examDateList"}

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")


@dataclass
class Target:
    type: str
    course: str
    semester: str
    name: str = ""
    exam_date: str = ""
    option_id: str = ""
    slot: str = ""
    study_code: str = ""

    @property
    def course_nr(self) -> str:
        return self.course.replace(".", "").strip().upper()

    @property
    def url(self) -> str:
        return (f"{BASE}/education/course/{PAGES[self.type]}.xhtml"
                f"?courseNr={self.course_nr}&semester={self.semester}")

    def describe(self) -> str:
        what = {"lva": "LVA registration", "group": f"group {self.name!r}",
                "exam": f"exam {self.name!r} {self.exam_date}".strip()}[self.type]
        return f"{self.course} {self.semester}: {what}"


@dataclass
class Schedule:
    start: datetime | None = None   # None -> auto-detect from the page
    lead_ms: int = 1500             # start polling this long before the opening
    interval_ms: int = 200          # time between poll request starts
    window_s: int = 90              # keep polling this long after the opening
    arrive_margin_ms: int = 40      # aim for a poll to *arrive* this long after the opening
    keepalive_s: int = 600          # refresh the page this often while waiting
    use_server_clock: bool = True


@dataclass
class Labels:
    register: list[str] = field(default_factory=lambda: [
        "Anmelden", "Register", "Voranmeldung", "Voranmelden", "Preregistration"])
    unregister: list[str] = field(default_factory=lambda: ["Abmelden", "Deregistration"])


@dataclass
class Config:
    target: Target
    schedule: Schedule
    cookies_file: Path
    labels: Labels
    log_dir: Path
    save_html: bool = True
    user_agent: str = DEFAULT_UA
    timeout_s: float = 20.0
    max_attempts: int = 5


def _to_dt(v, tz: ZoneInfo) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.strip().replace(" ", "T"))
    if not isinstance(v, datetime):
        raise ValueError(f"schedule.start must be a date-time, got {v!r}")
    return v.replace(tzinfo=tz) if v.tzinfo is None else v


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    t = raw.get("target", {})
    kind = t.get("type", "group")
    if kind not in PAGES:
        raise ValueError(f"target.type must be one of {sorted(PAGES)}, got {kind!r}")
    target = Target(
        type=kind,
        course=str(t["course"]),
        semester=str(t["semester"]).upper(),
        name=t.get("name", ""),
        exam_date=t.get("exam_date", ""),
        option_id=t.get("option_id", ""),
        slot=t.get("slot", ""),
        study_code=str(t.get("study_code", "")),
    )
    if kind in ("group", "exam") and not (target.name or target.option_id):
        raise ValueError("target.name (or target.option_id) is required for group/exam")

    s = raw.get("schedule", {})
    tz = ZoneInfo(s.get("timezone", "Europe/Vienna"))
    sched = Schedule(start=_to_dt(s.get("start"), tz))
    for k in ("lead_ms", "interval_ms", "window_s", "arrive_margin_ms", "keepalive_s", "use_server_clock"):
        if k in s:
            setattr(sched, k, type(getattr(sched, k))(s[k]))

    lab = raw.get("labels", {})
    labels = Labels()
    for k in ("register", "unregister"):
        if k in lab:
            setattr(labels, k, list(lab[k]))

    a = raw.get("auth", {})
    lg = raw.get("log", {})
    http = raw.get("http", {})
    return Config(
        target=target,
        schedule=sched,
        cookies_file=(base / a.get("cookies_file", "cookies.txt")).resolve(),
        labels=labels,
        log_dir=(base / lg.get("dir", "logs")).resolve(),
        save_html=bool(lg.get("save_html", True)),
        user_agent=http.get("user_agent", DEFAULT_UA),
        timeout_s=float(http.get("timeout_s", 20.0)),
        max_attempts=int(raw.get("register", {}).get("max_attempts", 5)),
    )
