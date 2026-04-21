"""Regression tests for chunking helpers in scripts/embeddings.py.

History: `chunk_transcript_turns` shipped with a slice bug (`parts[1::2]`)
that silently dropped half of every transcript's turns AND misaligned
the remaining headers with the wrong bodies. Fixed 2026-04-21.

These tests pin the boundary behaviour so the regression cannot recur.
"""
from __future__ import annotations

import textwrap

import pytest

from memex.scripts.embeddings import (
    Chunk,
    chunk_transcript_turns,
)


def _build_transcript(num_turns: int) -> str:
    """Build a fake transcript with N turns of recognizable content."""
    header = textwrap.dedent(
        """\
        ---
        type: transcript
        project: test
        ---

        ## Summary

        A short summary block.
        """
    )
    turns = "\n".join(
        f"## Turn {i}\n\n**User**: user-{i}\n\n**Assistant**: assistant-{i}\n"
        for i in range(1, num_turns + 1)
    )
    return header + "\n" + turns


def _turn_chunks(chunks: list[Chunk]) -> list[Chunk]:
    return [c for c in chunks if c.chunk_type == "turn"]


def test_chunk_transcript_turns_preserves_every_turn():
    """Every turn should produce its own chunk; none should be dropped."""
    content = _build_transcript(num_turns=6)
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    turns = _turn_chunks(chunks)
    assert len(turns) == 6, f"expected 6 turn chunks, got {len(turns)}"


def test_chunk_transcript_turns_pairs_header_with_matching_body():
    """Header for Turn N must pair with body containing user-N / assistant-N."""
    content = _build_transcript(num_turns=4)
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    turns = _turn_chunks(chunks)
    assert len(turns) == 4
    for idx, chunk in enumerate(turns, start=1):
        assert f"Turn {idx}" in chunk.content, (
            f"chunk {idx} should contain 'Turn {idx}' header, got: {chunk.content[:120]!r}"
        )
        assert f"user-{idx}" in chunk.content, (
            f"chunk {idx} should contain user-{idx}, got: {chunk.content[:120]!r}"
        )
        assert f"assistant-{idx}" in chunk.content, (
            f"chunk {idx} should contain assistant-{idx}, got: {chunk.content[:120]!r}"
        )


def test_chunk_transcript_turns_handles_odd_turn_count():
    """Odd turn counts used to be the worst case for the [1::2] slice bug —
    the last turn was always dropped. Pin that it's included now."""
    content = _build_transcript(num_turns=5)
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    turns = _turn_chunks(chunks)
    assert len(turns) == 5
    # The 5th turn specifically must be present — this is the one the buggy
    # slice silently dropped.
    assert any("Turn 5" in c.content and "user-5" in c.content for c in turns), (
        "Turn 5 must be chunked; the [1::2] bug used to drop it entirely"
    )


def test_chunk_transcript_turns_single_turn_is_included():
    """1-turn transcripts must produce exactly one turn chunk."""
    content = _build_transcript(num_turns=1)
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    turns = _turn_chunks(chunks)
    assert len(turns) == 1
    assert "Turn 1" in turns[0].content
    assert "user-1" in turns[0].content


def test_chunk_transcript_turns_emits_frontmatter_chunk():
    """First chunk should be the frontmatter+summary block."""
    content = _build_transcript(num_turns=3)
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    assert chunks, "expected at least one chunk"
    assert chunks[0].chunk_type == "frontmatter"
    assert "Summary" in chunks[0].content or "summary" in chunks[0].content.lower()


def test_chunk_transcript_turns_falls_back_when_no_turns():
    """Non-transcript content (no `## Turn N` headers) should fall back
    to `chunk_markdown` — i.e. still produce at least one chunk."""
    content = "# Plain document\n\nSome prose without any turn markers.\n"
    chunks = chunk_transcript_turns(content, meta={"project": "test", "date": "2026-04-21"})
    assert chunks, "fallback should still produce chunks"
    # No `turn` chunks since there are no turns
    assert not any(c.chunk_type == "turn" for c in chunks)
