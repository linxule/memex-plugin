"""Tests for the shared wikilink-noise filters and their use in the indexer.

``wikilink_filters`` is the single source of truth shared by
``embeddings.extract_wikilinks`` (graph table) and ``crystallization_check``
(ghost-node detection). These tests pin the two rules that previously drifted
between them: transcript/auto-memory sources cast no votes, and code spans are
neutralized before scanning — with line numbers preserved so the indexer's
``line_number`` stays accurate.
"""

from __future__ import annotations

from pathlib import Path

from memex.scripts.embeddings import extract_wikilinks
from memex.scripts.wikilink_filters import is_noncurated_source, strip_code_spans


# --- strip_code_spans ------------------------------------------------------

def test_strip_code_spans_removes_fence_inline_and_ansi() -> None:
    text = (
        "real [[keep-me]] before\n"
        "```toml\n[[d1_databases]]\n[[r2_buckets]]\n```\n"
        "inline `if [[ $x ]]; then` mid\n"
        "ansi \x1b[1m bold \x1b[0m tail\n"
    )
    out = strip_code_spans(text)
    assert "[[keep-me]]" in out      # real link outside code survives
    assert "d1_databases" not in out  # fenced TOML header removed
    assert "r2_buckets" not in out
    assert "$x" not in out            # bash conditional in inline code removed
    assert "\x1b[" not in out         # ANSI CSI sequences stripped


def test_strip_code_spans_preserves_line_numbers() -> None:
    # A fenced block becomes the same number of blank lines, not deleted, so a
    # marker after the block keeps its original 0-based line index.
    text = "a\n```\nx\n```\nb\n"
    lines = strip_code_spans(text).split("\n")
    assert lines.index("b") == 4          # unchanged from the original layout
    assert all("x" != ln for ln in lines)  # code content gone


def test_strip_code_spans_unterminated_fence_is_not_stripped() -> None:
    # Documented limitation (unchanged from the pre-v0.15.4 local checker): the
    # fenced regex requires a CLOSING delimiter, so an unterminated fence is NOT
    # stripped and a link inside it survives. Raw transcripts — where unterminated
    # fences are common in terminal dumps — are covered separately by
    # is_noncurated_source (the indexer early-returns before stripping), not by
    # code-span stripping. A future line-anchored fence matcher may tighten this;
    # this test pins the current contract so that change is deliberate.
    text = "intro\n```bash\necho [[leaks-through]]\n"  # no closing fence
    assert "[[leaks-through]]" in strip_code_spans(text)


# --- is_noncurated_source --------------------------------------------------

def test_is_noncurated_source_matches_transcripts_and_auto_memory() -> None:
    assert is_noncurated_source("projects/foo/transcripts/2026-06-22-session.md")
    assert is_noncurated_source("projects/bar/auto-memory/MEMORY.md")
    assert is_noncurated_source("projects\\foo\\transcripts\\x.md")  # windows sep
    assert not is_noncurated_source("projects/foo/memos/2026-06-22-real.md")
    assert not is_noncurated_source("projects/foo/_project.md")
    assert not is_noncurated_source("topics/real-concept.md")
    # "transcripts" as a bare filename stem (no path segment) is NOT a match
    assert not is_noncurated_source("topics/transcripts.md")


# --- extract_wikilinks integration -----------------------------------------

def test_extract_wikilinks_skips_transcript_source(tmp_path: Path) -> None:
    links = extract_wikilinks(
        "junk dump emits [[transcript-only]] and [[$MEMO_PATH]] frags\n",
        "projects/foo/transcripts/2026-06-22-session.md",
        tmp_path,
    )
    assert links == []


def test_extract_wikilinks_skips_auto_memory_source(tmp_path: Path) -> None:
    links = extract_wikilinks(
        "[[some-link]] in synced memory\n",
        "projects/foo/auto-memory/MEMORY.md",
        tmp_path,
    )
    assert links == []


def test_extract_wikilinks_strips_code_block_phantoms(tmp_path: Path) -> None:
    content = (
        "Body references [[real-concept]] in prose.\n"
        "```toml\n[[d1_databases]]\n[[migrations]]\n```\n"
        "And `bash [[ -f x ]]` inline.\n"
    )
    links = extract_wikilinks(content, "projects/foo/memos/note.md", tmp_path)
    texts = {link["link_text"] for link in links}
    assert "real-concept" in texts        # curated prose link kept
    assert "d1_databases" not in texts     # fenced TOML header ignored
    assert "migrations" not in texts
    assert not any("-f x" in t for t in texts)  # inline bash fragment ignored


def test_extract_wikilinks_preserves_line_number_after_code_block(tmp_path: Path) -> None:
    content = (
        "line1 [[before]]\n"   # line 1
        "```bash\n"            # line 2
        "echo [[fake]]\n"      # line 3 (inside fence)
        "```\n"                # line 4
        "line5 [[after]]\n"    # line 5
    )
    links = extract_wikilinks(content, "topics/real.md", tmp_path)
    by_text = {link["link_text"]: link["line_number"] for link in links}
    assert "fake" not in by_text           # code-block link dropped
    assert by_text["before"] == 1
    assert by_text["after"] == 5           # line number survives the fence collapse


def test_extract_wikilinks_curated_source_records_source_path(tmp_path: Path) -> None:
    links = extract_wikilinks("[[x]]\n", "topics/real.md", tmp_path)
    assert links and all(link["source_path"] == "topics/real.md" for link in links)
