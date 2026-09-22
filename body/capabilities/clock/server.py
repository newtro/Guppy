"""Clock capability: what time is it here, and what time is it there.

Plain functions hold the logic (tested in test_clock.py); the MCP tools are thin wrappers.
"""
from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from mcp.server.mcpserver import MCPServer

server = MCPServer("clock")


def describe(moment: datetime) -> dict:
    """Break an aware datetime into the pieces Guppy says out loud."""
    offset = moment.utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    return {
        "iso": moment.isoformat(timespec="seconds"),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "time_12h": moment.strftime("%-I:%M %p"),
        "day_of_week": moment.strftime("%A"),
        "timezone": str(moment.tzinfo),
        "abbreviation": moment.tzname() or "",
        "utc_offset": f"{sign}{hours:02d}:{minutes:02d}",
        "spoken": moment.strftime("%A, %B %-d, %Y at %-I:%M %p"),
    }


def local_zone_name() -> str | None:
    """The machine's IANA timezone name, so the Mind can hand it back to time_in. None if unresolvable."""
    candidate = os.environ.get("TZ", "").strip()
    if candidate and candidate in available_timezones():
        return candidate
    link = Path("/etc/localtime")
    try:
        parts = link.resolve().parts
    except OSError:
        return None
    if "zoneinfo" in parts:
        name = "/".join(parts[len(parts) - 1 - parts[::-1].index("zoneinfo") + 1:])
        if name in available_timezones():
            return name
    return None


def local_now(clock=datetime.now) -> dict:
    """Current local date and time, with the day of the week."""
    moment = clock().astimezone()
    name = local_zone_name()
    if name:  # prefer "America/New_York" over the bare "EDT" abbreviation
        moment = moment.astimezone(ZoneInfo(name))
    return describe(moment)


def resolve_zone(name: str) -> tzinfo:
    """An IANA timezone name to a tzinfo, raising ValueError with a usable message."""
    name = (name or "").strip()
    if not name:
        raise ValueError("No timezone given. Use an IANA name like Europe/London.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        suggestions = search_timezones(name, limit=5)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise ValueError(f"Unknown timezone {name!r}. Use an IANA name like Europe/London.{hint}") from None


def zone_now(name: str, clock=datetime.now) -> dict:
    """Current date and time in the named IANA timezone."""
    zone = resolve_zone(name)
    return describe(clock().astimezone().astimezone(zone))


def search_timezones(query: str, limit: int = 20) -> list[str]:
    """IANA timezone names containing the query (case-insensitive), e.g. 'london' -> Europe/London."""
    needle = (query or "").strip().lower().replace(" ", "_")
    names = sorted(available_timezones())
    if not needle:
        return names[:limit]
    exact = [n for n in names if n.lower().rsplit("/", 1)[-1] == needle]
    rest = [n for n in names if needle in n.lower() and n not in exact]
    return (exact + rest)[:limit]


@server.tool()
def now() -> dict:
    """The current local date, time and day of the week where the Admiral is."""
    return local_now()


@server.tool()
def time_in(timezone: str) -> dict:
    """The current date, time and day of the week in an IANA timezone, e.g. Europe/London."""
    try:
        return zone_now(timezone)
    except ValueError as e:
        return {"error": str(e)}


@server.tool()
def list_timezones(query: str = "", limit: int = 20) -> dict:
    """Look up IANA timezone names by city or region, e.g. 'london' or 'america/'."""
    return {"query": query, "matches": search_timezones(query, limit=limit)}


if __name__ == "__main__":
    server.run()
