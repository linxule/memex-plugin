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

import json
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


def _huggingface_fixture() -> str:
    # hf_ + 34 alphanumerics. Defang via runtime concat so source bytes don't
    # carry the full provider shape (push-protection hygiene).
    return "hf" + "_" + "A" * 34


def _stripe_live_secret_fixture() -> str:
    return "sk" + "_live_" + "A" * 24


def _stripe_live_publishable_fixture() -> str:
    return "pk" + "_live_" + "B" * 24


def _stripe_test_secret_fixture() -> str:
    return "sk" + "_test_" + "C" * 24


def _stripe_restricted_fixture() -> str:
    return "rk" + "_live_" + "D" * 24


def _notion_fixture() -> str:
    # secret_ + exactly 43 alphanumerics
    return "secret" + "_" + "A" * 43


def _sentry_dsn_fixture() -> str:
    # https://<32 hex>@<host with `sentry`>/<id>
    public_key = "0" * 32
    return "https://" + public_key + "@" + "o0.ingest." + "sentry" + ".io/1"


def _sentry_dsn_with_secret_fixture() -> str:
    # Legacy form with both public + secret
    public_key = "0" * 32
    secret_key = "f" * 32
    return "https://" + public_key + ":" + secret_key + "@" + "o0.ingest." + "sentry" + ".io/42"



def _der_b64(n_bytes: int) -> str:
    """Base64 of a DER-shaped blob (SEQUENCE header with the exact length, then
    filler bytes): shaped like a key body, built at runtime, never a real key."""
    import base64

    filler = b"\x02\x01\x00" + bytes((i * 37 + 11) % 256 for i in range(n_bytes - 3))  # version INTEGER 0 first
    header = b"\x30" + (bytes([n_bytes]) if n_bytes < 0x80 else b"\x82" + n_bytes.to_bytes(2, "big"))
    return base64.b64encode(header + filler).decode()


def _wrap(text: str, width: int = 64) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]

def _private_key_fixture() -> str:
    return (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + "\n".join(_wrap(_der_b64(300)))
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

    def test_huggingface_token_detected(self):
        text = "HF_TOKEN=" + _huggingface_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "huggingface"

    def test_stripe_live_secret_detected(self):
        text = "STRIPE_SECRET=" + _stripe_live_secret_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "stripe"

    def test_stripe_live_publishable_detected(self):
        text = "STRIPE_PUBLISHABLE=" + _stripe_live_publishable_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "stripe"

    def test_stripe_test_secret_detected(self):
        text = "STRIPE_TEST_SECRET=" + _stripe_test_secret_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "stripe"

    def test_stripe_restricted_detected(self):
        text = "STRIPE_RESTRICTED=" + _stripe_restricted_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "stripe"

    def test_notion_secret_detected(self):
        text = "NOTION_TOKEN=" + _notion_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "notion"

    def test_sentry_dsn_detected(self):
        text = "SENTRY_DSN=" + _sentry_dsn_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "sentry-dsn"

    def test_sentry_dsn_with_secret_detected(self):
        text = "SENTRY_DSN=" + _sentry_dsn_with_secret_fixture()
        _, matches = scrub_text(text)
        assert len(matches) == 1
        assert matches[0].provider == "sentry-dsn"


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
            "secret_password is a bad variable name",
            "secret_key = os.environ['KEY']  # short, not 43 chars",
            "hf_dataset = load_dataset('imdb')  # short HF symbol, not a token",
            "sk_live_demo would be too short to be a real Stripe key",
            "Visit https://docs.sentry.io/quickstart for setup.",
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


class TestSafeWriteText:
    """The shared helper for "Python writes prose to disk without going through
    the PostToolUse hook." Refactored sync_auto_memory + extract.py both use it."""

    def test_writes_clean_content_unchanged(self, tmp_path):
        from memex.scrub import safe_write_text
        target = tmp_path / "clean.md"
        n = safe_write_text(target, "no secrets in this file\n")
        assert n == 0
        assert target.read_text() == "no secrets in this file\n"

    def test_redacts_secret_before_write(self, tmp_path):
        from memex.scrub import safe_write_text
        secret = _anthropic_fixture()
        target = tmp_path / "leaky.md"
        n = safe_write_text(target, f"key={secret}\n")
        assert n == 1
        on_disk = target.read_text()
        assert secret not in on_disk
        assert "<REDACTED:anthropic>" in on_disk

    def test_redacts_multiple_secrets(self, tmp_path):
        from memex.scrub import safe_write_text
        a = _anthropic_fixture()
        s = _slack_fixture()
        target = tmp_path / "multi.md"
        n = safe_write_text(target, f"a={a}\ns={s}\n")
        assert n == 2
        on_disk = target.read_text()
        assert a not in on_disk
        assert s not in on_disk

    def test_raises_oserror_on_write_failure(self, tmp_path):
        from memex.scrub import safe_write_text
        # Writing into a path whose parent doesn't exist raises OSError —
        # caller decides whether to swallow or propagate.
        target = tmp_path / "nope" / "still-nope" / "out.md"
        with pytest.raises(OSError):
            safe_write_text(target, "anything")


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


# ── private keys: real bodies only (v0.22.0 gauntlet round 1) ───────────────


def _pem(label: str = "RSA PRIVATE KEY", sep: str = "\n") -> str:
    body = sep.join(_wrap(_der_b64(200)))
    return f"-----BEGIN {label}-----{sep}{body}{sep}-----END {label}-----"


class TestPrivateKeyBodies:
    def test_real_key_shapes_are_redacted(self):
        shapes = {
            "multi-line": _pem(),
            "indented": "    " + _pem().replace("\n", "\n    "),
            "json-escaped": json.dumps({"content": "leaked " + _pem()}),
            "json-escaped crlf": json.dumps({"content": _pem(sep="\r\n")}),
            "one-line paste": _pem(sep=" "),
            "openssh": "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            + "\n".join(_wrap(__import__("base64").b64encode(b"openssh-key-v1\x00" + bytes(range(200))).decode(), 70))
            + "\n-----END OPENSSH PRIVATE KEY-----",
            "encrypted, armor headers": "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
            "DEK-Info: AES-128-CBC,ABCDEF0123456789\n\n"
            + "\n".join(_wrap(__import__("base64").b64encode(bytes((i * 73 + 5) % 256 for i in range(640))).decode()))
            + "\n-----END RSA PRIVATE KEY-----",
        }
        for name, text in shapes.items():
            out, matches = scrub_text(text, apply=True)
            assert [m.provider for m in matches] == ["private-key"], name
            assert "B" * 20 not in out, name

    def test_prose_quoting_both_markers_is_left_alone(self):
        prose = (
            "## SSH setup\nThe file starts with -----BEGIN OPENSSH PRIVATE KEY----- and we never paste it.\n"
            "Run ssh-keygen, then check the permissions.\n"
            "It ends with -----END OPENSSH PRIVATE KEY----- as usual.\n"
        )
        out, matches = scrub_text(prose, apply=True)
        assert matches == [] and out == prose

    def test_markers_in_separate_jsonl_records_never_merge_them(self):
        lines = [json.dumps({"content": c}) for c in (
            "we saw -----BEGIN RSA PRIVATE KEY----- in the log", "an ordinary observation",
            "and -----END RSA PRIVATE KEY----- later")]
        text = "\n".join(lines) + "\n"
        out, matches = scrub_text(text, apply=True)
        assert matches == [] and out == text

    def test_end_label_must_repeat_the_begin_label(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n-----END EC PRIVATE KEY-----"
        assert scrub_text(text)[1] == []

    def test_a_long_single_line_body_is_caught(self):
        """OpenSSL accepts a 184-char single-line EC body (gauntlet round 3)."""
        text = "-----BEGIN EC PRIVATE KEY-----\n" + _der_b64(135) + "\n-----END EC PRIVATE KEY-----"
        assert [m.provider for m in scrub_text(text)[1]] == ["private-key"]

    def test_a_sea_of_whitespace_after_begin_scans_in_linear_time(self):
        """Overlapping blank rules backtracked quadratically: 7 s on 20,000
        spaces (gauntlet round 3). Possessive quantifiers make it linear."""
        import time

        begin = "-----BEGIN RSA PRIVATE KEY-----"
        for tail in (" " * 100_000, " \t" * 50_000, "  \n  " * 20_000):
            start = time.perf_counter()
            scrub_text(begin + tail)
            assert time.perf_counter() - start < 0.5

    def test_json_slash_escapes_and_short_groups_are_caught(self):
        """gauntlet round 3b: JSON may escape '/' as '\\/', and a key split into
        4- or 8-char groups is still valid for OpenSSL."""
        body = _der_b64(150)
        head, tail = "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"
        shapes = [
            json.dumps({"c": f"{head}\n{body}\n{tail}"}).replace("/", "\\/"),
            head + "\n" + " ".join(body[i:i + 4] for i in range(0, len(body), 4)) + "\n" + tail,
            head + "\n" + " ".join(body[i:i + 8] for i in range(0, len(body), 8)) + "\n" + tail,
        ]
        for text in shapes:
            out, matches = scrub_text(text, apply=True)
            assert [m.provider for m in matches] == ["private-key"], text[:60]

    def test_a_placeholder_sentence_between_the_markers_is_kept(self):
        """gauntlet round 4: letters-and-spaces prose is not a key body."""
        for text in (
            "-----BEGIN RSA PRIVATE KEY-----\nPaste the private key contents from your local machine here\n-----END RSA PRIVATE KEY-----",
            json.dumps({"c": "-----BEGIN OPENSSH PRIVATE KEY----- Paste the private key contents from your local machine here -----END OPENSSH PRIVATE KEY-----"}),
        ):
            out, matches = scrub_text(text, apply=True)
            assert matches == [] and out == text

    def test_json_tab_and_unicode_escapes_are_caught(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(_wrap(_der_b64(150))) + "\n-----END RSA PRIVATE KEY-----"
        for text in (json.dumps({"c": pem.replace("\n", "\t")}),
                     json.dumps({"c": pem}).replace("\\n", "\\u000a").replace("/", "\\u002f")):
            assert [m.provider for m in scrub_text(text)[1]] == ["private-key"], text[:50]

    def test_a_short_key_in_groups_is_caught(self):
        """Measured: 1 in 4 Ed25519 keys split into 4/8-char groups has no '+'/'/'
        and no long run; grouped base64 still switches class inside tokens."""
        body = _der_b64(46)  # PKCS#8 Ed25519-sized: 48 bytes, 64 base64 chars
        for n in (4, 8):
            text = ("-----BEGIN PRIVATE KEY-----\n" + " ".join(body[i:i + n] for i in range(0, len(body), n))
                    + "\n-----END PRIVATE KEY-----")
            assert [m.provider for m in scrub_text(text)[1]] == ["private-key"], n

    def test_prose_with_digits_and_camel_case_between_markers_is_kept(self):
        for prose in ("Use the 2 keys from the 3 machines here and then rotate them every 90 days",
                      "The macOS and iPhone keys live in the Keychain on each device"):
            text = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{prose}\n-----END OPENSSH PRIVATE KEY-----"
            assert scrub_text(text)[1] == [], prose


    def test_prose_that_fooled_the_token_heuristics_is_kept(self):
        """gauntlet round 5: digits, camelCase and slashes in prose between the
        markers never decode to a key."""
        for prose in ("Use iPhone12 macOS14 OpenSSL3 and LibreSSL4 for testing",
                      "Rotate keys 1/2 and 2/2 on 10/28 then restart the svc+db pair",
                      "MIIE is how a key usually starts and MIIBIjAN is a public key"):
            text = f"-----BEGIN RSA PRIVATE KEY-----\n{prose}\n-----END RSA PRIVATE KEY-----"
            assert scrub_text(text)[1] == [], prose

    def test_pgp_and_legacy_encrypted_placeholders_are_kept(self):
        """gauntlet round 6: a PGP block must open with a secret-key packet, and a
        Proc-Type header alone does not make prose a key."""
        for text in (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nplease paste your private key contents from your local machine here\n-----END PGP PRIVATE KEY BLOCK-----",
            "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n\nPaste the private key contents from your local machine here\n-----END RSA PRIVATE KEY-----",
        ):
            assert scrub_text(text)[1] == [], text[:40]

    def test_a_legacy_encrypted_pem_is_caught(self):
        import base64

        ciphertext = base64.b64encode(bytes((i * 73 + 5) % 256 for i in range(640))).decode()
        text = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n"
                + "\n".join(_wrap(ciphertext)) + "\n-----END RSA PRIVATE KEY-----")
        assert [m.provider for m in scrub_text(text)[1]] == ["private-key"]
