"""Tests for scripts/temporal_scan.py — filesystem date scanner."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from memex.scripts.temporal_scan import extract_date_from_filename, scan_temporal


class TestExtractDateFromFilename:
    def test_compact_datetime(self):
        assert extract_date_from_filename("20260128-135914-73c12fe2.md") == date(2026, 1, 28)

    def test_compact_datetime_with_slug(self):
        assert extract_date_from_filename("20260128-origins-fork-to-production.md") == date(2026, 1, 28)

    def test_iso_date_slug(self):
        assert extract_date_from_filename("2026-02-21-memex-synthesis-and-compression.md") == date(2026, 2, 21)

    def test_iso_date_only(self):
        assert extract_date_from_filename("2026-03-15.md") == date(2026, 3, 15)

    def test_uuid_only_returns_none(self):
        assert extract_date_from_filename("af7379a6-4675-48ff-a3fb-c9e1b7503e6d.md") is None

    def test_no_date_returns_none(self):
        assert extract_date_from_filename("random-notes.md") is None

    def test_invalid_date_returns_none(self):
        assert extract_date_from_filename("20261345-invalid.md") is None


class TestScanTemporal:
    def test_scan_with_mock_files(self, tmp_path):
        """Create a mock vault structure and scan it."""
        # Create project structure
        proj = tmp_path / "projects" / "testproj"
        memos = proj / "memos"
        transcripts = proj / "transcripts"
        memos.mkdir(parents=True)
        transcripts.mkdir(parents=True)

        # Create memo files
        (memos / "2026-03-15-test-memo.md").write_text(
            "---\ntype: memo\ntitle: Test Memo\ndate: 2026-03-15\ntopics: [testing]\nstatus: active\n---\nContent"
        )
        (memos / "2026-03-10-old-memo.md").write_text(
            "---\ntype: memo\ntitle: Old Memo\ndate: 2026-03-10\n---\nOld content"
        )

        # Create transcript files
        (transcripts / "20260315-140000-abc12345.md").write_text(
            "---\ntype: transcript\ntitle: Session ABC\ndate: 2026-03-15\nduration_minutes: 45\nturns: 20\nhas_memo: true\n---\nTranscript"
        )

        # Scan for March 15 only
        results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 15),
            end=datetime(2026, 3, 16),
            mode="detail",
        )

        assert len(results) == 2
        types = {r["type"] for r in results}
        assert types == {"memo", "transcript"}

        # Check memo detail
        memo = next(r for r in results if r["type"] == "memo")
        assert memo["title"] == "Test Memo"
        assert memo["project"] == "testproj"

        # Check transcript detail
        transcript = next(r for r in results if r["type"] == "transcript")
        assert transcript["duration_minutes"] == "45"
        assert transcript["turns"] == "20"

    def test_scan_project_filter(self, tmp_path):
        """Test --project filter works."""
        for name in ("alpha", "beta"):
            memos = tmp_path / "projects" / name / "memos"
            memos.mkdir(parents=True)
            (memos / "2026-03-15-test.md").write_text("---\ntitle: Test\ndate: 2026-03-15\n---\n")

        # Only alpha
        results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 15),
            end=datetime(2026, 3, 16),
            project="alpha",
        )
        assert len(results) == 1
        assert results[0]["project"] == "alpha"

    def test_scan_type_filter(self, tmp_path):
        """Test --type filter works."""
        proj = tmp_path / "projects" / "test"
        (proj / "memos").mkdir(parents=True)
        (proj / "transcripts").mkdir(parents=True)
        (proj / "memos" / "2026-03-15-memo.md").write_text("---\ntitle: M\n---\n")
        (proj / "transcripts" / "20260315-120000-abc.md").write_text("---\ntitle: T\n---\n")

        memo_results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 15),
            end=datetime(2026, 3, 16),
            doc_type="memo",
        )
        assert all(r["type"] == "memo" for r in memo_results)

        transcript_results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 15),
            end=datetime(2026, 3, 16),
            doc_type="transcript",
        )
        assert all(r["type"] == "transcript" for r in transcript_results)

    def test_scan_empty_range(self, tmp_path):
        """Scanning a date range with no files returns empty list."""
        (tmp_path / "projects" / "test" / "memos").mkdir(parents=True)
        results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2),
        )
        assert results == []

    def test_scan_limit(self, tmp_path):
        """Test --limit caps results."""
        memos = tmp_path / "projects" / "test" / "memos"
        memos.mkdir(parents=True)
        for i in range(10):
            (memos / f"2026-03-{15+i:02d}-memo-{i}.md").write_text(f"---\ntitle: Memo {i}\n---\n")

        results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 1),
            end=datetime(2026, 4, 1),
            limit=3,
        )
        assert len(results) == 3

    def test_list_mode_skips_frontmatter(self, tmp_path):
        """List mode should not include title from frontmatter."""
        memos = tmp_path / "projects" / "test" / "memos"
        memos.mkdir(parents=True)
        (memos / "2026-03-15-test.md").write_text("---\ntitle: Should Not Appear\n---\n")

        results = scan_temporal(
            memex=tmp_path,
            start=datetime(2026, 3, 15),
            end=datetime(2026, 3, 16),
            mode="list",
        )
        assert len(results) == 1
        # In list mode, title is the filename stem
        assert results[0]["title"] == "2026-03-15-test"
