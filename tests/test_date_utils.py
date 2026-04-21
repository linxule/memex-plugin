"""Tests for scripts/date_utils.py — natural-language date parsing."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from memex.scripts.date_utils import (
    parse_temporal_expression,
    parse_since_duration_extended,
    parse_before_expression,
    parse_date_range,
)

# Fixed reference date for deterministic tests: Wednesday 2026-03-25 14:30:00
REF = datetime(2026, 3, 25, 14, 30, 0)
REF_MIDNIGHT = REF.replace(hour=0, minute=0, second=0, microsecond=0)


class TestParseTemporalExpression:
    def test_today(self):
        start, end = parse_temporal_expression("today", REF)
        assert start == REF_MIDNIGHT
        assert end == REF_MIDNIGHT + timedelta(days=1)

    def test_yesterday(self):
        start, end = parse_temporal_expression("yesterday", REF)
        assert start == REF_MIDNIGHT - timedelta(days=1)
        assert end == REF_MIDNIGHT

    def test_n_days_ago(self):
        start, end = parse_temporal_expression("3 days ago", REF)
        expected = REF_MIDNIGHT - timedelta(days=3)
        assert start == expected
        assert end == expected + timedelta(days=1)

    def test_1_day_ago(self):
        start, end = parse_temporal_expression("1 day ago", REF)
        expected = REF_MIDNIGHT - timedelta(days=1)
        assert start == expected
        assert end == expected + timedelta(days=1)

    def test_last_n_days(self):
        start, end = parse_temporal_expression("last 5 days", REF)
        assert start == REF_MIDNIGHT - timedelta(days=5)
        assert end == REF_MIDNIGHT + timedelta(days=1)

    def test_last_1_day(self):
        start, end = parse_temporal_expression("last 1 day", REF)
        assert start == REF_MIDNIGHT - timedelta(days=1)
        assert end == REF_MIDNIGHT + timedelta(days=1)

    def test_this_week(self):
        # REF is Wednesday → Monday is 2 days earlier
        start, end = parse_temporal_expression("this week", REF)
        monday = REF_MIDNIGHT - timedelta(days=2)  # Wednesday - 2 = Monday
        assert start == monday
        assert end == REF_MIDNIGHT + timedelta(days=1)

    def test_last_week(self):
        start, end = parse_temporal_expression("last week", REF)
        this_monday = REF_MIDNIGHT - timedelta(days=2)
        last_monday = this_monday - timedelta(days=7)
        assert start == last_monday
        assert end == this_monday

    def test_last_monday(self):
        # REF is Wednesday → last Monday is 2 days ago
        start, end = parse_temporal_expression("last monday", REF)
        expected = REF_MIDNIGHT - timedelta(days=2)
        assert start == expected
        assert end == expected + timedelta(days=1)

    def test_last_wednesday_on_wednesday(self):
        # "last wednesday" on a Wednesday means 7 days ago
        start, end = parse_temporal_expression("last wednesday", REF)
        expected = REF_MIDNIGHT - timedelta(days=7)
        assert start == expected
        assert end == expected + timedelta(days=1)

    def test_last_friday(self):
        # REF is Wednesday → last Friday is 5 days ago
        start, end = parse_temporal_expression("last friday", REF)
        expected = REF_MIDNIGHT - timedelta(days=5)
        assert start == expected
        assert end == expected + timedelta(days=1)

    def test_duration_7d(self):
        start, end = parse_temporal_expression("7d", REF)
        assert start == REF - timedelta(days=7)
        assert end == REF_MIDNIGHT + timedelta(days=1)

    def test_duration_2w(self):
        start, end = parse_temporal_expression("2w", REF)
        assert start == REF - timedelta(days=14)

    def test_duration_3m(self):
        start, end = parse_temporal_expression("3m", REF)
        assert start == REF - timedelta(days=90)

    def test_month_day(self):
        start, end = parse_temporal_expression("march 15", REF)
        assert start == datetime(2026, 3, 15)
        assert end == datetime(2026, 3, 16)

    def test_month_abbrev(self):
        start, end = parse_temporal_expression("mar 15", REF)
        assert start == datetime(2026, 3, 15)

    def test_month_day_year(self):
        start, end = parse_temporal_expression("january 1 2025", REF)
        assert start == datetime(2025, 1, 1)
        assert end == datetime(2025, 1, 2)

    def test_future_month_wraps_to_last_year(self):
        # "december 25" on March 25 2026 → Dec 25 2025 (past, not future)
        start, end = parse_temporal_expression("december 25", REF)
        assert start == datetime(2025, 12, 25)

    def test_iso_date(self):
        start, end = parse_temporal_expression("2026-03-15", REF)
        assert start == datetime(2026, 3, 15)
        assert end == datetime(2026, 3, 16)

    def test_compact_date(self):
        start, end = parse_temporal_expression("20260315", REF)
        assert start == datetime(2026, 3, 15)
        assert end == datetime(2026, 3, 16)

    def test_invalid_returns_none(self):
        assert parse_temporal_expression("nonsense", REF) is None
        assert parse_temporal_expression("", REF) is None
        assert parse_temporal_expression("   ", REF) is None

    def test_invalid_date_returns_none(self):
        assert parse_temporal_expression("2026-13-45", REF) is None
        assert parse_temporal_expression("20261345", REF) is None

    def test_case_insensitive(self):
        start, end = parse_temporal_expression("Yesterday", REF)
        assert start == REF_MIDNIGHT - timedelta(days=1)

        start, end = parse_temporal_expression("LAST WEEK", REF)
        this_monday = REF_MIDNIGHT - timedelta(days=2)
        assert start == this_monday - timedelta(days=7)


class TestParseSinceDurationExtended:
    def test_duration_format(self):
        cutoff = parse_since_duration_extended("7d")
        assert cutoff is not None

    def test_natural_date(self):
        cutoff = parse_since_duration_extended("yesterday")
        assert cutoff is not None

    def test_empty_returns_none(self):
        assert parse_since_duration_extended("") is None
        assert parse_since_duration_extended(None) is None

    def test_invalid_returns_none(self):
        assert parse_since_duration_extended("not a date") is None


class TestParseBeforeExpression:
    def test_returns_end_of_range(self):
        result = parse_before_expression("yesterday")
        assert result is not None
        # "yesterday" end = midnight today
        assert result.hour == 0

    def test_empty(self):
        assert parse_before_expression("") is None
        assert parse_before_expression(None) is None


class TestParseDateRange:
    def test_since_only(self):
        start, end = parse_date_range(since="7d")
        assert start is not None
        assert end is None

    def test_before_only(self):
        start, end = parse_date_range(before="yesterday")
        assert start is None
        assert end is not None

    def test_both(self):
        start, end = parse_date_range(since="30d", before="7d")
        assert start is not None
        assert end is not None

    def test_between_with_to(self):
        start, end = parse_date_range(between="2026-03-01 to 2026-03-15")
        assert start == datetime(2026, 3, 1)
        assert end == datetime(2026, 3, 16)  # end_exclusive of 2026-03-15

    def test_between_space_separated(self):
        start, end = parse_date_range(between="2026-03-01 2026-03-15")
        assert start == datetime(2026, 3, 1)
        assert end == datetime(2026, 3, 16)

    def test_none_inputs(self):
        start, end = parse_date_range()
        assert start is None
        assert end is None
