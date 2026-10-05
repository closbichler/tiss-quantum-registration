"""Configuration file (TOML) loading."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .pages import VIENNA
from .session import BASE

PAGES = {"course": "courseRegistration", "group": "groupList", "exam": "examDateList"}
KEYS = {"type", "course", "semester", "name", "start", "slot", "study_code", "cookies", "interval_ms", "window_s"}


@dataclass
class Config:
    type: str                       # course | group | exam
    course: str                     # "185.A91"
    semester: str                   # "2026W"
    name: str = ""                  # group name / exam text
    start: datetime | None = None   # None -> start time shown on the page
    slot: str = ""                  # exam time slot
    study_code: str = ""            # curriculum, if enrolled in several
    cookies: Path = Path("cookies.txt")
    interval_ms: int = 200          # time between poll requests
    window_s: int = 90              # keep polling this long after the opening
    log_dir: Path = Path("logs")

    @property
    def course_nr(self) -> str:
        return self.course.replace(".", "").strip().upper()

    @property
    def url(self) -> str:
        return (f"{BASE}/education/course/{PAGES[self.type]}.xhtml"
                f"?courseNr={self.course_nr}&semester={self.semester}")

    def describe(self) -> str:
        what = "course registration" if self.type == "course" else f"{self.type} {self.name!r}"
        return f"{self.course} {self.semester}: {what}"


def _to_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.strip().replace(" ", "T"))
    if not isinstance(v, datetime):
        raise ValueError(f"start must be a date and time like 2026-10-12T10:00:00, got {v!r}")
    return v.replace(tzinfo=VIENNA) if v.tzinfo is None else v


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise ValueError(f"unknown setting(s) in {path.name}: {', '.join(unknown)} - see config.example.toml")
    kind = raw.get("type")
    if kind not in PAGES:
        raise ValueError(f"type must be one of {', '.join(PAGES)}, got {kind!r}")
    for key in ("course", "semester") + (("name",) if kind != "course" else ()):
        if not str(raw.get(key, "")).strip():
            raise ValueError(f"{key} is required for type = {kind!r}")
    return Config(
        type=kind,
        course=str(raw["course"]).strip(),
        semester=str(raw["semester"]).strip().upper(),
        name=str(raw.get("name", "")).strip(),
        start=_to_dt(raw.get("start")),
        slot=str(raw.get("slot", "")).strip(),
        study_code=str(raw.get("study_code", "")).strip(),
        cookies=(path.parent / raw.get("cookies", "cookies.txt")).resolve(),
        interval_ms=int(raw.get("interval_ms", 200)),
        window_s=int(raw.get("window_s", 90)),
        log_dir=(path.parent / "logs").resolve(),
    )
