"""Duty roster: who is on shift for emergency alerts right now.

The roster is a CSV, usually exported from the ED / nursing rota each month, with columns:

    day,start,end,tier,phone,name
    2026-10-01,08:00,20:00,1,919000000001,Sr. Nurse A      one dated shift
    2026-10-01,20:00,08:00,1,919000000002,Sr. Nurse B      ends next morning (end before start)
    mon,09:00,17:00,2,919000000010,Dr. On-call Mon       every Monday
    daily,00:00,24:00,3,919000000020,Nursing Superintendent   every day, all day

- `day` is a date (YYYY-MM-DD), a weekday (mon..sun) or `daily`.
- Times are IST, 24-hour. An end time at or before the start means the shift ends the next day.
- `tier` 1 is alerted first; higher tiers are escalated to if nobody takes the ticket.

Uploads go through the desk (/desk/roster) and are stored in the database, so every version,
who uploaded it and when, is kept. If the roster has nobody on a tier at the moment of an
alert, the fixed ALERT_TIERS numbers are used instead.
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, insert, select
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
COLUMNS = ["day", "start", "end", "tier", "phone", "name"]

metadata = MetaData()
duty_rosters = Table(
    "duty_rosters", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("uploaded_by", String(80), nullable=False),
    Column("rows", Integer, nullable=False),
    Column("content", Text, nullable=False),
)


@dataclass(frozen=True)
class Shift:
    day: str  # "2026-10-01", "mon".."sun" or "daily"
    start: time
    end: time
    ends_midnight: bool  # end written as 24:00
    tier: int
    phone: str
    name: str

    def applies_on(self, d: date) -> bool:
        if self.day == "daily":
            return True
        if self.day in WEEKDAYS:
            return WEEKDAYS[d.weekday()] == self.day
        return self.day == d.isoformat()

    def window(self, d: date) -> tuple[datetime, datetime]:
        """The shift's start and end (IST) if it starts on day d."""
        start = datetime.combine(d, self.start, IST)
        if self.ends_midnight:
            end = datetime.combine(d + timedelta(days=1), time(0), IST)
        else:
            end = datetime.combine(d, self.end, IST)
            if end <= start:
                end += timedelta(days=1)
        return start, end

    def covers(self, moment: datetime) -> bool:
        local = moment.astimezone(IST)
        for d in (local.date(), local.date() - timedelta(days=1)):  # started today, or overnight from yesterday
            if self.applies_on(d):
                start, end = self.window(d)
                if start <= local < end:
                    return True
        return False


@dataclass(frozen=True)
class OnDuty:
    phone: str
    name: str


class RosterError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _time(value: str) -> tuple[time, bool]:
    value = value.strip()
    if value == "24:00":
        return time(0), True
    return datetime.strptime(value, "%H:%M").time(), False


def parse_roster(text: str) -> list[Shift]:
    """Parse and validate. Raises RosterError listing every problem (with CSV line numbers)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))  # tolerate Excel's BOM
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise RosterError([f"Missing column(s): {', '.join(missing)}. Expected: {','.join(COLUMNS)}"])
    shifts, problems = [], []
    for line, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        try:
            day = row["day"].lower()
            if day not in WEEKDAYS and day != "daily":
                date.fromisoformat(day)  # validates
            start, start_24 = _time(row["start"])
            end, ends_midnight = _time(row["end"])
            if start_24:
                raise ValueError("start can't be 24:00")
            tier = int(row["tier"])
            if not 1 <= tier <= 9:
                raise ValueError("tier must be 1–9")
            phone = "".join(ch for ch in row["phone"] if ch.isdigit())
            if len(phone) == 10:
                phone = "91" + phone
            if len(phone) < 11:
                raise ValueError(f"phone '{row['phone']}' is too short")
            if not row["name"]:
                raise ValueError("name is empty")
            shifts.append(Shift(day, start, end, ends_midnight, tier, phone, row["name"]))
        except (ValueError, KeyError) as e:
            problems.append(f"Line {line}: {e}")
    if problems:
        raise RosterError(problems)
    if not shifts:
        raise RosterError(["The file has no shifts in it."])
    return shifts


class Roster:
    def __init__(self, shifts: list[Shift]):
        self.shifts = shifts

    def on_duty(self, moment: datetime) -> dict[int, list[OnDuty]]:
        """{tier: [people]} for everyone on shift at this moment, tiers in order."""
        tiers: dict[int, list[OnDuty]] = {}
        for s in self.shifts:
            if s.covers(moment):
                people = tiers.setdefault(s.tier, [])
                if all(p.phone != s.phone for p in people):
                    people.append(OnDuty(s.phone, s.name))
        return dict(sorted(tiers.items()))

    def gaps(self, start: datetime, hours: int = 24, tier: int = 1) -> list[tuple[datetime, datetime]]:
        """Exact periods in the next `hours` with nobody on `tier`."""
        stop = start + timedelta(hours=hours)
        edges = {start, stop}
        for begin, end, s in self.upcoming(start, hours):
            if s.tier == tier:
                edges.update(t for t in (begin, end) if start < t < stop)
        points = sorted(edges)
        gaps: list[tuple[datetime, datetime]] = []
        for a, b in zip(points, points[1:]):
            if not self.on_duty(a).get(tier):  # coverage only changes at shift edges
                if gaps and gaps[-1][1] == a:
                    gaps[-1] = (gaps[-1][0], b)
                else:
                    gaps.append((a, b))
        return gaps

    def upcoming(self, start: datetime, hours: int = 24) -> list[tuple[datetime, datetime, Shift]]:
        """Shift instances overlapping the next `hours`, in start order."""
        local = start.astimezone(IST)
        stop = start + timedelta(hours=hours)
        out = []
        d, last = local.date() - timedelta(days=1), (stop.astimezone(IST)).date()
        while d <= last:
            for s in self.shifts:
                if s.applies_on(d):
                    begin, end = s.window(d)
                    if begin < stop and end > start:
                        out.append((begin, end, s))
            d += timedelta(days=1)
        return sorted(out, key=lambda x: (x[0], x[2].tier))


@dataclass
class RosterVersion:
    id: int
    ts: datetime
    uploaded_by: str
    rows: int
    content: str


class RosterStore:
    """Keeps every uploaded roster; the newest one is live."""

    def __init__(self, engine: Engine, seed_csv=None):
        self.engine = engine
        metadata.create_all(engine)
        self._cache: tuple[int, Roster] | None = None
        if seed_csv and seed_csv.exists() and self.latest() is None:
            try:
                self.upload(seed_csv.read_text(encoding="utf-8"), f"file {seed_csv.name}")
            except RosterError as e:
                log.error("Duty roster %s not loaded: %s", seed_csv, e)

    def upload(self, content: str, uploaded_by: str) -> RosterVersion:
        shifts = parse_roster(content)  # raises RosterError; nothing is saved on error
        with self.engine.begin() as conn:
            conn.execute(insert(duty_rosters).values(
                ts=datetime.now(timezone.utc), uploaded_by=uploaded_by, rows=len(shifts), content=content))
        return self.latest()

    def latest(self) -> RosterVersion | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(duty_rosters).order_by(duty_rosters.c.id.desc()).limit(1)).first()
        if not row:
            return None
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        return RosterVersion(row.id, ts, row.uploaded_by, row.rows, row.content)

    def current(self) -> Roster | None:
        version = self.latest()
        if version is None:
            return None
        if self._cache is None or self._cache[0] != version.id:
            self._cache = (version.id, Roster(parse_roster(version.content)))
        return self._cache[1]
