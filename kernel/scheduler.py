"""Scheduler: recurring and one-shot Mind tasks.

A schedule is a goal plus a `when`:
    "0 8 * * 1"        5-field cron (minute hour day-of-month month day-of-week; 0 or 7 = Sunday), local time
    "every 30m"        fixed interval (m, h, d)
    "in 90m"           once, relative to now
    "at 14:30"         once, at the next 14:30 local time (also "once at 2:30 pm")
    "every monday at 8:00" / "every weekday at 9am" / "every day at 17:30" / "every mon and thu at 7:15 pm"
                       recurring, converted to cron
Due schedules run as ordinary Mind tasks with the Admiral's provenance (the Admiral created them), so the gate,
taint rules, and spoken reports all apply unchanged. A run missed while Guppy was off happens once at startup.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from loguru import logger

from kernel.mind.tasks import DB_PATH, TaskManager

SCHEMA = """
create table if not exists schedules (
  id integer primary key, goal text not null, spec text not null, role text, enabled integer default 1,
  once integer default 0, next_run real, last_run real, last_task_id integer, runs integer default 0, created real);
"""
TICK_SECONDS = 15
Listener = Callable[[dict, str], Awaitable[None]]


# ---------------- time specs ----------------
def _field(expr: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-", 1))
        else:
            a = b = int(part)
            if step > 1:  # "5/15" means from 5 to the end, every 15
                b = hi
        if a < lo or b > hi or a > b or step < 1:
            raise ValueError(f"'{expr}' out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


def cron_next(spec: str, after: datetime) -> datetime:
    parts = spec.split()
    if len(parts) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
    minute, hour, dom, month = (_field(parts[0], 0, 59), _field(parts[1], 0, 23),
                                _field(parts[2], 1, 31), _field(parts[3], 1, 12))
    dow = {d % 7 for d in _field(parts[4], 0, 7)}  # 0 and 7 are Sunday
    dom_any, dow_any = parts[2] == "*", parts[4] == "*"
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):  # at most a year of minutes
        cron_dow = (t.weekday() + 1) % 7  # Python Monday=0 -> cron Monday=1
        if dom_any and dow_any:
            day_ok = True
        elif dom_any:
            day_ok = cron_dow in dow
        elif dow_any:
            day_ok = t.day in dom
        else:  # cron semantics: when both are restricted, either may match
            day_ok = t.day in dom or cron_dow in dow
        if t.month in month and day_ok and t.hour in hour and t.minute in minute:
            return t
        t += timedelta(minutes=1)
    raise ValueError("cron expression never fires")


UNIT = {"m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "day": 86400}
DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"


def _hm(h: str, mi: str | None, ampm: str | None) -> tuple[int, int]:
    hour, minute = int(h), int(mi or 0)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time {h}:{mi or '00'}")
    return hour, minute


def _days(words: str) -> str:
    words = words.strip()
    if words in ("day", "days", "morning", "evening", "night"):
        return "*"
    if words in ("weekday", "weekdays"):
        return "1-5"
    if words in ("weekend", "weekends"):
        return "0,6"
    out = []
    for w in re.split(r"\s*(?:,|and|&)\s*", words):
        key = w.strip()[:3]
        if key not in DAYS:
            raise ValueError(f"unknown day '{w}'")
        out.append(str(DAYS[key]))
    return ",".join(sorted(set(out), key=int))


def parse_when(spec: str, now: datetime | None = None) -> tuple[datetime, bool, str]:
    """Returns (first run, is one-shot, normalized spec). Raises ValueError with a helpful message."""
    now = now or datetime.now()
    s = spec.strip().lower()
    if m := re.fullmatch(r"(every|in)\s+(\d+)\s*(m|min|h|hr|d|day)s?", s):
        secs = int(m[2]) * UNIT[m[3]]
        if secs < 60:
            raise ValueError("minimum interval is 1 minute")
        return now + timedelta(seconds=secs), m[1] == "in", f"{m[1]} {m[2]}{m[3][0]}"
    if m := re.fullmatch(r"every\s+month\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+" + TIME, s):
        h, mi = _hm(m[2], m[3], m[4])
        spec = f"{mi} {h} {int(m[1])} * *"
        return cron_next(spec, now), False, spec
    if m := re.fullmatch(r"every\s+([a-z ,&]+?)\s+at\s+" + TIME, s):  # "every monday at 8:00" -> cron
        h, mi = _hm(m[2], m[3], m[4])
        spec = f"{mi} {h} * * {_days(m[1])}"
        return cron_next(spec, now), False, spec
    if m := re.fullmatch(r"(?:once\s+)?at\s+" + TIME, s):
        h, mi = _hm(m[1], m[2], m[3])
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return (t if t > now else t + timedelta(days=1)), True, f"at {h:02d}:{mi:02d}"
    return cron_next(s, now), False, s


def next_after(spec: str, last: datetime) -> datetime:
    if spec.startswith("every "):
        return parse_when(spec, last)[0]
    return cron_next(spec, last)


# ---------------- scheduler ----------------
class Scheduler:
    def __init__(self, tasks: TaskManager):
        self.tasks = tasks
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.listeners: list[Listener] = []
        self._loop: asyncio.Task | None = None

    def get(self, sid: int) -> dict | None:
        r = self.db.execute("select * from schedules where id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def list(self, include_disabled: bool = False) -> list[dict]:
        q = "select * from schedules" + ("" if include_disabled else " where enabled=1") + " order by next_run"
        return [dict(r) for r in self.db.execute(q)]

    def _update(self, sid: int, **f):
        self.db.execute(f"update schedules set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), sid))
        self.db.commit()

    async def _notify(self, s: dict, event: str):
        for fn in list(self.listeners):
            try:
                await fn(s, event)
            except Exception as e:
                logger.warning(f"scheduler listener failed: {e}")

    def create(self, goal: str, when: str, role: str = "general") -> dict:
        first, once, spec = parse_when(when)
        cur = self.db.execute(
            "insert into schedules (goal, spec, role, once, next_run, created) values (?,?,?,?,?,?)",
            (goal, spec, role, int(once), first.timestamp(), time.time()))
        self.db.commit()
        s = self.get(cur.lastrowid)
        logger.info(f"Schedule #{s['id']} created ({spec}, next {first:%a %Y-%m-%d %H:%M}): {goal[:80]}")
        return s

    def cancel(self, sid: int) -> bool:
        if not self.get(sid):
            return False
        self._update(sid, enabled=0)
        return True

    def start(self):
        self._loop = self._loop or asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: float | None = None):
        now = now or time.time()
        due = [dict(r) for r in self.db.execute(
            "select * from schedules where enabled=1 and next_run <= ? order by next_run", (now,))]
        for s in due:
            task = await self.tasks.submit(s["goal"], role=s["role"] or "general", provenance="admiral")
            fields = {"last_run": now, "last_task_id": task["id"], "runs": s["runs"] + 1}
            if s["once"]:
                fields["enabled"] = 0
            else:  # next slot after *now*: a backlog of missed runs collapses into this one run
                fields["next_run"] = next_after(s["spec"], datetime.fromtimestamp(now)).timestamp()
            self._update(s["id"], **fields)
            logger.info(f"Schedule #{s['id']} fired -> task #{task['id']}")
            await self._notify(self.get(s["id"]), "fired")


def describe(s: dict) -> str:
    nxt = datetime.fromtimestamp(s["next_run"]).strftime("%A %B %-d at %-I:%M %p") if s.get("next_run") else "never"
    return f"#{s['id']} {s['spec']}: {s['goal'][:80]} (next {nxt if s['enabled'] else 'done'})"
