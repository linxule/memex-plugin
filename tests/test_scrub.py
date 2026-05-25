"""Tests for src/memex/scrub.py — regex-based secret scrubber.

## Fixture construction

GitHub's push protection (and similar third-party secret scanners) pattern-
match the LITERAL bytes of source files. A test like

    text = "token=xoxb-0000000000-0000000000-AAAA...AAA"

trips the Slack-token detector even though the value is obviously fake,
because the scanner sees the full xoxb-NN-NN-XX shape in the source bytes.
The same issue applies to AIza* (Google), eyJ*.eyJ*.* (JWT), and others.

Workaround: build the fixtures at runtime via concatenation / multiplication.
The source bytes contain `"xox" + "b-"` (no match), while the runtime string
that exercises our regex contains the full literal shape. Helpers below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.scrub import (
    Match,
    PATTERNS,
    scan_path,
    scrub_file,
    scrub_text,
)


# ── fixture builders ────────────────────────────────────────────────────────
#
# Each builder splits the recognizable-prefix from the body so the source-file
# bytes never contain a full provider-shaped literal. The runtime return value
# DOES contain the full shape — that's what the scrubber regex is matched
# against.


def _anthropic_fixture() -> str:
    return "sk-ant" + "-api03-" + "A" * 56


def _openai_project_fixture() -> str:
    return "sk-proj" + "-" + "A" * 44


def _openai_service_fixture() -> str:
    return "sk-svcacct" + "-" + "A" * 44


def _generic_sk_fixture() -> str:
    # 32+ alphanumerics after sk- (length floor of our pattern)
    return "sk-" + "X" * 40


def _generic_sk_vendor_fixture() -> str:
    return "sk-" + "moonshot" + "-" + "Y" * 36


def _github_pat_fixture() -> str:
    return "ghp" + "_" + "A" * 36


def _github_oauth_fixture() -> str:
    return "gho" + "_" + "B" * 36


def _google_api_fixture() -> str:
    return "AI" + "za" + "A" * 35


def _aws_access_fixture() -> str:
    # AWS canonical test key (used in their own docs — well-known fake).
    return "AKIA" + "IOSFODNN7" + "EXAMPLE"


def _slack_fixture() -> str:
    return "xox" + "b-" + "0" * 10 + "-" + "0" * 10 + "-" + "A" * 24


def _jwt_fixture() -> str:
    # Three base64url-ish segments past the regex length floor.
    return "ey" + "JAAAAAAAAAAAAAAAAA" + "." + "ey" + "JAAAAAAAAAAAAAAAAA" + "." + "A" * 17


def _private_key_fixture() -> str:
    return (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + "A" * 60
        + "\n"
        + "B" * 60
        + "\n-----END RSA PRIVATE KEY-----"
    )


# ── pattern coverage ────────────────────────────────────────────────────────


class TestPatterns:
    def test_anthropic_key_detected(self):
        text = "key=" + _anthropic_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "anthropic"

    def test_openai_project_key_detected(self):
        text = "key=" + _openai_project_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "openai-project"

    def test_openai_service_key_detected(self):
        text = "key=" + _openai_service_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "openai-service"

    def test_generic_sk_detected(self):
        text = "key=" + _generic_sk_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "generic-sk"

    def test_generic_sk_vendor_detected(self):
        text = "key=" + _generic_sk_vendor_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "generic-sk-vendor"

    def test_github_pat_detected(self):
        text = "token=" + _github_pat_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "github-token"

    def test_github_oauth_detected(self):
        text = "token=" + _github_oauth_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "github-token"

    def test_google_api_key_detected(self):
        text = "key=" + _google_api_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "google-api"

    def test_aws_access_key_detected(self):
        text = _aws_access_fixture() + " in env"
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "aws-access-key"

    def test_slack_token_detected(self):
        text = "token=" + _slack_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "slack"

    def test_jwt_detected(self):
        text = "Bearer " + _jwt_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "jwt"

    def test_private_key_block_detected(self):
        _, matches = scrub_text(_private_key_fixture())
        assert len(matches) == 1
        assert matches[0].provider == "private-key"


# ── overlap resolution: specific patterns win ────────────────────────────────


class TestOverlap:
    def test_anthropic_wins_over_generic_sk(self):
        # sk-ant-... also matches generic-sk if listed later. Specific wins.
        text = "key=" + _anthropic_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "anthropic"

    def test_openai_project_wins_over_generic_sk(self):
        text = "key=" + _openai_project_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "openai-project"

    def test_specific_pattern_wins_when_generic_starts_earlier(self, monkeypatch):
        """Regression: the overlap algorithm must prefer the more-specific
        pattern even when a less-specific pattern starts at an earlier offset
        than the specific match. Earlier implementation sorted by (start,
        pattern_idx) and walked forward, which let the wide-but-generic
        pattern claim the span before the narrow-but-specific one was
        considered."""
        import re
        from memex import scrub as scrub_mod

        # Synthesize a catalog where 'narrow' is specific (index 0) and
        # 'wide' is generic (index 1) and would otherwise swallow narrow's
        # match by starting earlier.
        narrow = re.compile(r"NARROW_[A-Z0-9]{8}")
        wide = re.compile(r"PREFIX_[A-Z0-9_]{20,}")

        fake_catalog = [("narrow", narrow), ("wide", wide)]
        monkeypatch.setattr(scrub_mod, "PATTERNS", fake_catalog)

        # In "PREFIX_AAAA_NARROW_BBBBBBBB", wide matches starting at col 0
        # (spanning past the narrow match); narrow matches NARROW_BBBBBBBB
        # at col 12. Specific must win.
        text = "PREFIX_AAAA_NARROW_BBBBBBBB"
        _, matches = scrub_mod.scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "narrow"


# ── multi-match + position ──────────────────────────────────────────────────


class TestMultiMatch:
    def test_multiple_secrets_in_one_text(self):
        text = (
            "openai=" + _generic_sk_fixture() + "\n"
            "google=" + _google_api_fixture() + "\n"
            "github=" + _github_pat_fixture() + "\n"
        )
        _, matches = scrub_text(text)
        assert len(matches) == 3
        providers = {m.provider for m in matches}
        assert providers == {"generic-sk", "google-api", "github-token"}

    def test_line_and_col_reported_correctly(self):
        text = "line1 no secret\nline2 " + _github_pat_fixture() + " tail"
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].line == 2
        assert matches[0].col == 7

    def test_match_order_is_file_order(self):
        text = "first " + _github_pat_fixture() + "\nsecond " + _aws_access_fixture() + "\n"
        _, matches = scrub_text(text)
        assert [m.provider for m in matches] == ["github-token", "aws-access-key"]


# ── idempotency ─────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_already_redacted_text_is_noop(self):
        text = "user has <REDACTED:anthropic> and <REDACTED:google-api> set"
        new_text, matches = scrub_text(text, apply=True)
        assert matches == []
        assert new_text == text

    def test_apply_twice_produces_same_output(self):
        text = (
            "key1=" + _generic_sk_fixture() + "\n"
            "key2=" + _github_pat_fixture()
        )
        once, _ = scrub_text(text, apply=True)
        twice, _ = scrub_text(once, apply=True)
        assert once == twice


# ── apply behaviour ─────────────────────────────────────────────────────────


class TestApply:
    def test_apply_false_returns_unchanged_text(self):
        text = "key=" + _anthropic_fixture()
        new_text, matches = scrub_text(text, apply=False)
        assert new_text == text
        assert len(matches) == 1

    def test_apply_true_replaces_with_redacted_marker(self):
        text = "key=" + _anthropic_fixture()
        new_text, _ = scrub_text(text, apply=True)
        assert new_text == "key=<REDACTED:anthropic>"

    def test_apply_preserves_surrounding_text_exactly(self):
        text = "before " + _generic_sk_fixture() + " after"
        new_text, _ = scrub_text(text, apply=True)
        assert new_text == "before <REDACTED:generic-sk> after"


# ── no false positives on common memo prose ─────────────────────────────────


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "prose",
        [
            "The function is called scrub_text — it scans for secrets.",
            "She said 'skip the test' before lunch.",
            "Version v0.11.6 fixes the grep -Fxq -- bug.",
            "Use sk- as a prefix marker in your tests.",
            "AI" + "za is the Google prefix.",  # split to avoid scanner
            "The PR is at github.com/linxule/memex-plugin",
            "Markdown link [docs](https://example.com/docs?key=foo)",
            "JWT stands for JSON Web Token.",
            "I love eyes-on review.",
        ],
    )
    def test_prose_does_not_match(self, prose):
        _, matches = scrub_text(prose)
        assert matches == [], f"Unexpected match in: {prose!r}"


# ── file I/O ────────────────────────────────────────────────────────────────


class TestFileScrub:
    def test_scrub_file_dry_run_does_not_modify(self, tmp_path: Path):
        f = tmp_path / "memo.md"
        original = "Some prose. key=" + _github_pat_fixture() + " done."
        f.write_text(original)
        result = scrub_file(f, apply=False)
        assert len(result.matches) == 1
        assert result.applied is False
        assert f.read_text() == original

    def test_scrub_file_apply_rewrites_in_place(self, tmp_path: Path):
        f = tmp_path / "memo.md"
        f.write_text("key=" + _github_pat_fixture() + " done")
        result = scrub_file(f, apply=True)
        assert result.applied is True
        assert f.read_text() == "key=<REDACTED:github-token> done"

    def test_scrub_file_no_matches_returns_empty(self, tmp_path: Path):
        f = tmp_path / "clean.md"
        f.write_text("Pure prose. No secrets.")
        result = scrub_file(f, apply=True)
        assert result.matches == []
        assert result.applied is False

    def test_scan_path_walks_directory(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("key=" + _github_pat_fixture())
        (tmp_path / "b.md").write_text("clean")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.md").write_text("key=" + _google_api_fixture())
        results, errors = scan_path(tmp_path, apply=False)
        assert {Path(r.path).name for r in results} == {"a.md", "c.md"}
        assert errors == []

    def test_scan_path_skips_noise_dirs(self, tmp_path: Path):
        for dirname in (".venv", ".git", "__pycache__", "node_modules", "tests"):
            d = tmp_path / dirname
            d.mkdir()
            (d / "leaked.md").write_text("key=" + _github_pat_fixture())
        results, errors = scan_path(tmp_path, apply=False)
        # All skip-listed dirs ignored, including tests/ (so the scrubber
        # never rewrites its own fixtures when run from the repo root).
        assert results == []
        assert errors == []

    def test_scan_path_returns_errors_for_unwritable_files(self, tmp_path: Path):
        import os
        f = tmp_path / "readonly.md"
        f.write_text("key=" + _github_pat_fixture())
        try:
            os.chmod(tmp_path, 0o555)
            results, errors = scan_path(tmp_path, apply=True)
            assert errors, "Expected at least one error from unwritable directory"
            assert str(f) in errors[0][0]
        finally:
            os.chmod(tmp_path, 0o755)


class TestLineEndingPreservation:
    """Regression for codex finding (b): text-mode read+write would normalize
    line endings, silently mutating CRLF / CR / mixed transcripts even when no
    secrets were touched. Binary-mode I/O preserves bytes."""

    def test_crlf_file_preserved_through_apply(self, tmp_path: Path):
        f = tmp_path / "windows.md"
        secret = _github_pat_fixture().encode("ascii")
        content_bytes = (
            b"# Header\r\n"
            b"key=" + secret + b" done\r\n"
            b"end\r\n"
        )
        f.write_bytes(content_bytes)
        scrub_file(f, apply=True)
        new_bytes = f.read_bytes()
        assert b"\r\n" in new_bytes
        assert b"\n\n" not in new_bytes.replace(b"\r\n", b"")
        assert b"<REDACTED:github-token>" in new_bytes
        assert b"ghp" + b"_" not in new_bytes
        assert new_bytes.count(b"\r\n") == 3

    def test_clean_crlf_file_byte_identical_through_dry_run(self, tmp_path: Path):
        """A dry-run on a CRLF file with no matches must not touch the file."""
        import os
        f = tmp_path / "clean-windows.md"
        original = b"line1\r\nline2\r\nline3\r\n"
        f.write_bytes(original)
        mtime_before = os.stat(f).st_mtime
        scrub_file(f, apply=True)  # apply=True but no matches → no rewrite
        assert f.read_bytes() == original
        assert os.stat(f).st_mtime == mtime_before

    def test_lf_file_unchanged_through_apply(self, tmp_path: Path):
        """Sanity: the unix-default LF case still works."""
        f = tmp_path / "unix.md"
        secret = _github_pat_fixture().encode("ascii")
        f.write_bytes(b"key=" + secret + b"\n")
        scrub_file(f, apply=True)
        new = f.read_bytes()
        assert new == b"key=<REDACTED:github-token>\n"


class TestLongFilenameHandling:
    """Regression for codex finding (a): mkstemp prefix of `.{path.name}.` could
    exceed per-component filename limits on filesystems with NAME_MAX=255 when
    path.name was already long. Truncating the prefix to 200 chars leaves
    headroom for `.`, the 6-char mkstemp random suffix, and `.tmp`."""

    def test_apply_succeeds_on_long_filename(self, tmp_path: Path):
        long_base = "a" * 240
        f = tmp_path / f"{long_base}.md"
        f.write_text("key=" + _github_pat_fixture() + " done")
        scrub_file(f, apply=True)
        assert "<REDACTED:github-token>" in f.read_text()


class TestAtomicWrite:
    def test_apply_writes_via_temp_rename(self, tmp_path: Path, monkeypatch):
        """The atomic-write path should never leave a partial file.
        Monkeypatch os.replace to verify it's called (not direct write_text)."""
        import os
        from memex import scrub as scrub_mod

        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy_replace(src, dst):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(scrub_mod.os, "replace", spy_replace)

        f = tmp_path / "memo.md"
        f.write_text("key=" + _github_pat_fixture() + " done")
        scrub_file(f, apply=True)

        assert len(calls) == 1, "Expected exactly one os.replace call"
        src, dst = calls[0]
        assert str(f) == dst, "Replace must target the original file"
        assert src.endswith(".tmp"), f"Temp file should end with .tmp; got {src}"

    def test_apply_cleans_up_temp_on_write_failure(self, tmp_path: Path, monkeypatch):
        """If the write fails after the temp is created, no .tmp file should remain."""
        from memex import scrub as scrub_mod

        def boom_replace(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(scrub_mod.os, "replace", boom_replace)

        f = tmp_path / "memo.md"
        original = "key=" + _github_pat_fixture() + " done"
        f.write_text(original)
        with pytest.raises(OSError):
            scrub_file(f, apply=True)
        assert f.read_text() == original
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".memo.md.")]
        assert leftovers == [], f"Temp leftovers: {leftovers}"


class TestPatternCatalog:
    def test_specific_patterns_listed_before_generic(self):
        """Order matters for overlap resolution. The generic-sk pattern must
        come after every specific sk- pattern, otherwise it would steal the
        match before the specific provider gets a chance."""
        names = [name for name, _ in PATTERNS]
        generic_idx = names.index("generic-sk")
        for specific in ("anthropic", "openai-project", "openai-service"):
            assert names.index(specific) < generic_idx, (
                f"{specific} must appear before generic-sk in PATTERNS"
            )

    def test_each_provider_has_unique_name(self):
        names = [name for name, _ in PATTERNS]
        assert len(names) == len(set(names))
