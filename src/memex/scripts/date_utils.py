"""
Natural-language date parsing utilities for memex temporal queries.

Shared by: temporal_scan.py, hybrid_search.py, and search.py

No external dependencies — stdlib only.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta


# Day name → weekday number (Monday=0)
_WEEKDAYS = {
    name.lower(): i
    for i, name in enumerate(calendar.day_name)
}

# Month name/abbreviation → month number
_MONTHS: dict[str, int] = {}
for i in range(1, 13):
    _MONTHS[calendar.month_name[i].lower()] = i
    _MONTHS[calendar.month_abbr[i].lower()] = i


def parse_temporal_expression(
    expr: str, reference: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """Parse natural-language date expression into (start_inclusive, end_exclusive) range.

    Returns (start, end) datetimes or None if unparsable.

    Supports:
      - "today", "yesterday"
      - "N days ago", "last N days"
      - "this week", "last week"
      - "last Monday" ... "last Sunday"
      - Duration strings: "7d", "2w", "3m"
      - "March 15", "Mar 15", "March 15 2026"
      - "2026-03-15", "20260315"
    """
    if not expr or not expr.strip():
        return None

    now = reference or datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    text = expr.strip().lower()

    # 1. "today"
    if text == "today":
        return (today_start, tomorrow_start)

    # 2. "yesterday"
    if text == "yesterday":
        return (today_start - timedelta(days=1), today_start)

    # 3. "N days ago" → single day
    m = re.match(r'^(\d+)\s+days?\s+ago$', text)
    if m:
        n = int(m.group(1))
        day_start = today_start - timedelta(days=n)
        return (day_start, day_start + timedelta(days=1))

    # 4. "last N days" → range from N days ago through today
    m = re.match(r'^last\s+(\d+)\s+days?$', text)
    if m:
        n = int(m.group(1))
        return (today_start - timedelta(days=n), tomorrow_start)

    # 5. "this week" → Monday of current week through today
    if text == "this week":
        monday = today_start - timedelta(days=today_start.weekday())
        return (monday, tomorrow_start)

    # 6. "last week" → Monday-to-Sunday of previous week
    if text == "last week":
        this_monday = today_start - timedelta(days=today_start.weekday())
        last_monday = this_monday - timedelta(days=7)
        return (last_monday, this_monday)

    # 7. "last Monday" ... "last Sunday"
    m = re.match(r'^last\s+(' + '|'.join(_WEEKDAYS.keys()) + r')$', text)
    if m:
        target_weekday = _WEEKDAYS[m.group(1)]
        current_weekday = today_start.weekday()
        days_back = (current_weekday - target_weekday) % 7
        if days_back == 0:
            days_back = 7  # "last Monday" on a Monday means 7 days ago
        day_start = today_start - timedelta(days=days_back)
        return (day_start, day_start + timedelta(days=1))

    # 8. Duration strings: "7d", "2w", "3m"
    m = re.match(r'^(\d+)([dwm])$', text)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        days = {"d": 1, "w": 7, "m": 30}[unit] * num
        return (now - timedelta(days=days), tomorrow_start)

    # 9. "March 15", "Mar 15", "March 15 2026"
    m = re.match(
        r'^(' + '|'.join(_MONTHS.keys()) + r')\s+(\d{1,2})(?:\s+(\d{4}))?$',
        text,
    )
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None
        # If the date is in the future and no year was specified, use last year
        if d > now and not m.group(3):
            d = d.replace(year=year - 1)
        return (d, d + timedelta(days=1))

    # 10a. ISO date: "2026-03-15"
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', text)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (d, d + timedelta(days=1))
        except ValueError:
            return None

    # 10b. Compact date: "20260315"
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', text)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (d, d + timedelta(days=1))
        except ValueError:
            return None

    return None


def parse_since_duration_extended(since: str) -> datetime | None:
    """Drop-in replacement for hybrid_search.parse_since_duration().

    Handles existing Nd/Nw/Nm format PLUS natural-language dates.
    Returns cutoff datetime (everything after this point).
    """
    if not since:
        return None

    result = parse_temporal_expression(since)
    if result is not None:
        return result[0]  # start of range = cutoff
    return None


def parse_before_expression(before: str) -> datetime | None:
    """Parse a --before/--until expression into a ceiling datetime.

    Returns datetime such that docs with date <= this are included.
    """
    if not before:
        return None

    result = parse_temporal_expression(before)
    if result is not None:
        return result[1]  # end of range = ceiling
    return None


def parse_date_range(
    since: str | None = None,
    before: str | None = None,
    between: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Unified parser for --since, --before, --between CLI args.

    --between expects "start end" format, e.g., "2026-03-01 2026-03-15"
    or "last week this week".

    Returns (start, end) where either can be None.
    """
    if between:
        # Try to split into two expressions
        # First try splitting on common separators
        parts = between.split(" to ")
        if len(parts) != 2:
            # Try splitting on space — but only if both halves parse
            # Try known boundary: date formats are either single-word or multi-word
            # Strategy: try splitting at each space position, return first that parses both halves
            words = between.split()
            for i in range(1, len(words)):
                left = " ".join(words[:i])
                right = " ".join(words[i:])
                r1 = parse_temporal_expression(left)
                r2 = parse_temporal_expression(right)
                if r1 is not None and r2 is not None:
                    return (r1[0], r2[1])
            return (None, None)
        else:
            r1 = parse_temporal_expression(parts[0].strip())
            r2 = parse_temporal_expression(parts[1].strip())
            start = r1[0] if r1 else None
            end = r2[1] if r2 else None
            return (start, end)

    start = parse_since_duration_extended(since) if since else None
    end = parse_before_expression(before) if before else None
    return (start, end)
