import asyncio
from datetime import datetime

import pytest

from kernel import scheduler as sch

NOW = datetime(2026, 9, 22, 20, 15)  # a Tuesday


@pytest.mark.parametrize("spec,expected", [
    ("0 8 * * 1", datetime(2026, 9, 28, 8, 0)),       # next Monday 08:00
    ("*/15 * * * *", datetime(2026, 9, 22, 20, 30)),
    ("30 9 1 * *", datetime(2026, 10, 1, 9, 30)),     # first of the month
    ("0 17 * * 1-5", datetime(2026, 9, 23, 17, 0)),   # weekdays
    ("0 10 * * 0", datetime(2026, 9, 27, 10, 0)),     # Sunday as 0
    ("0 10 * * 7", datetime(2026, 9, 27, 10, 0)),     # Sunday as 7
    ("0 9 15 * 5", datetime(2026, 9, 25, 9, 0)),      # dom OR dow when both restricted
])
def test_cron_next(spec, expected):
    assert sch.cron_next(spec, NOW) == expected


@pytest.mark.parametrize("when,first,once,norm", [
    ("every 30m", datetime(2026, 9, 22, 20, 45), False, "every 30m"),
    ("in 2h", datetime(2026, 9, 22, 22, 15), True, "in 2h"),
    ("at 21:00", datetime(2026, 9, 22, 21, 0), True, "at 21:00"),
    ("at 8:05 am", datetime(2026, 9, 23, 8, 5), True, "at 08:05"),
    ("at 7:30 pm", datetime(2026, 9, 23, 19, 30), True, "at 19:30"),   # already passed today
    ("once at 9am", datetime(2026, 9, 23, 9, 0), True, "at 09:00"),
    ("every monday at 8:00", datetime(2026, 9, 28, 8, 0), False, "0 8 * * 1"),
    ("every weekday at 9am", datetime(2026, 9, 23, 9, 0), False, "0 9 * * 1-5"),
    ("every day at 17:30", datetime(2026, 9, 23, 17, 30), False, "30 17 * * *"),
    ("every mon and thu at 7:15 pm", datetime(2026, 9, 24, 19, 15), False, "15 19 * * 1,4"),
    ("every month on the 1st at 9:00", datetime(2026, 10, 1, 9, 0), False, "0 9 1 * *"),
    ("every weekend at 10", datetime(2026, 9, 26, 10, 0), False, "0 10 * * 0,6"),
])
def test_parse_when(when, first, once, norm):
    assert sch.parse_when(when, NOW) == (first, once, norm)


@pytest.mark.parametrize("bad", ["every blursday at 9", "at 25:00", "every 10s", "0 25 * * *", "61 * * * *", "whenever", "* * * *", "0 0 31 2 *"])
def test_parse_when_rejects(bad):
    with pytest.raises(ValueError):
        sch.parse_when(bad, NOW)


class FakeTasks:
    def __init__(self):
        self.submitted = []

    async def submit(self, goal, role="general", provenance="admiral"):
        self.submitted.append((goal, role, provenance))
        return {"id": len(self.submitted)}


def test_tick_runs_due_once_and_collapses_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "DB_PATH", tmp_path / "t.db")
    s = sch.Scheduler(FakeTasks())
    rec = s.create("digest", "every 1h")
    one = s.create("remind", "in 5m")
    far_future = rec["next_run"] + 10 * 3600  # ten hourly slots missed
    asyncio.run(s.tick(now=far_future))
    assert sorted(g for g, *_ in s.tasks.submitted) == ["digest", "remind"]  # each once, not ten times
    assert all(p == "admiral" for *_, p in s.tasks.submitted)
    assert s.get(one["id"])["enabled"] == 0                              # one-shot retired
    assert s.get(rec["id"])["next_run"] > far_future                     # rescheduled after now
    asyncio.run(s.tick(now=far_future + 1))
    assert len(s.tasks.submitted) == 2                                   # nothing due again yet
