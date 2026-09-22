"""Tests for the clock capability's logic (the MCP tools are thin wrappers over these)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    """Import this capability's server.py under a unique name (every capability has a server.py)."""
    spec = importlib.util.spec_from_file_location("guppy_capability_clock", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clock_server = _load()
describe = clock_server.describe
local_now = clock_server.local_now
resolve_zone = clock_server.resolve_zone
search_timezones = clock_server.search_timezones
zone_now = clock_server.zone_now


def fixed(moment: datetime):
    """A clock function that always returns the same moment."""
    return lambda: moment


def test_manifest_declares_every_exposed_tool():
    """The kernel refuses a capability whose tools are not all classified in capability.json."""
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["name"] == "clock"
    assert manifest["enabled"] is True
    exposed = {t.name for t in asyncio.run(clock_server.server.list_tools())}
    assert exposed == {"now", "time_in", "list_timezones"}
    assert set(manifest["effects"]) == exposed
    assert set(manifest["effects"].values()) == {"read"}  # a clock only ever reads


def test_every_tool_has_a_description_for_the_mind():
    for tool in asyncio.run(clock_server.server.list_tools()):
        assert (tool.description or "").strip()


def test_describe_breaks_out_the_spoken_pieces():
    moment = datetime(2026, 9, 22, 14, 5, 0, tzinfo=ZoneInfo("Europe/London"))
    d = describe(moment)
    assert d["date"] == "2026-09-22"
    assert d["time"] == "14:05:00"
    assert d["time_12h"] == "2:05 PM"
    assert d["day_of_week"] == "Tuesday"
    assert d["timezone"] == "Europe/London"
    assert d["abbreviation"] == "BST"
    assert d["utc_offset"] == "+01:00"
    assert d["spoken"] == "Tuesday, September 22, 2026 at 2:05 PM"


def test_describe_handles_negative_and_half_hour_offsets():
    ny = describe(datetime(2026, 1, 15, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    assert ny["utc_offset"] == "-05:00"
    kolkata = describe(datetime(2026, 1, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    assert kolkata["utc_offset"] == "+05:30"
    chatham = describe(datetime(2026, 1, 15, 9, 0, tzinfo=ZoneInfo("Pacific/Chatham")))
    assert chatham["utc_offset"] == "+13:45"


def test_local_now_is_aware_and_complete():
    d = local_now()
    assert set(d) == {"iso", "date", "time", "time_12h", "day_of_week", "timezone",
                      "abbreviation", "utc_offset", "spoken"}
    parsed = datetime.fromisoformat(d["iso"])
    assert parsed.tzinfo is not None
    assert d["day_of_week"] == parsed.strftime("%A")


def test_local_now_uses_the_injected_clock():
    d = local_now(clock=fixed(datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc)))
    assert d["date"] == datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")


def test_zone_now_converts_the_same_instant():
    instant = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    london = zone_now("Europe/London", clock=fixed(instant))
    tokyo = zone_now("Asia/Tokyo", clock=fixed(instant))
    assert london["time"] == "13:00:00"  # BST in September
    assert london["day_of_week"] == "Tuesday"
    assert tokyo["time"] == "21:00:00"
    assert tokyo["utc_offset"] == "+09:00"
    assert datetime.fromisoformat(london["iso"]) == datetime.fromisoformat(tokyo["iso"])


def test_zone_now_crosses_the_date_line():
    instant = datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc)
    auckland = zone_now("Pacific/Auckland", clock=fixed(instant))
    assert auckland["date"] == "2026-09-23"
    assert auckland["day_of_week"] == "Wednesday"


def test_zone_now_accepts_utc_and_surrounding_whitespace():
    instant = datetime(2026, 3, 1, 6, 15, tzinfo=timezone.utc)
    assert zone_now("  UTC  ", clock=fixed(instant))["time"] == "06:15:00"


def test_resolve_zone_rejects_empty_and_unknown_names():
    with pytest.raises(ValueError, match="No timezone given"):
        resolve_zone("")
    with pytest.raises(ValueError, match="Unknown timezone"):
        resolve_zone("Middle/Earth")


def test_unknown_zone_suggests_a_real_one():
    with pytest.raises(ValueError, match="Europe/London"):
        resolve_zone("London")


def test_search_timezones_prefers_the_exact_city():
    assert search_timezones("london")[0] == "Europe/London"
    assert search_timezones("Los Angeles")[0] == "America/Los_Angeles"
    assert search_timezones("nowhere-at-all") == []
    assert len(search_timezones("america", limit=3)) == 3


def test_search_timezones_with_no_query_returns_a_sample():
    assert len(search_timezones("", limit=5)) == 5


def test_time_in_tool_reports_a_bad_zone_instead_of_raising():
    result = clock_server.time_in("Middle/Earth")
    assert "error" in result
    assert "Middle/Earth" in result["error"]


def test_time_in_tool_returns_the_zone_on_success():
    assert clock_server.time_in("Asia/Tokyo")["timezone"] == "Asia/Tokyo"


def test_list_timezones_tool_shape():
    result = clock_server.list_timezones("london")
    assert result["query"] == "london"
    assert "Europe/London" in result["matches"]


def test_local_zone_name_is_a_real_zone_or_none():
    name = clock_server.local_zone_name()
    assert name is None or name in clock_server.available_timezones()


def test_local_zone_name_honours_the_tz_environment(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    assert clock_server.local_zone_name() == "Asia/Kolkata"
    monkeypatch.setenv("TZ", "Middle/Earth")
    assert clock_server.local_zone_name() != "Middle/Earth"


def test_local_now_reports_the_iana_zone_when_it_can(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    d = local_now(clock=fixed(datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)))
    assert d["timezone"] == "Europe/London"
    assert d["time"] == "13:00:00"
    assert d["day_of_week"] == "Tuesday"
