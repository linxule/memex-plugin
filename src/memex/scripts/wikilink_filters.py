"""Shared wikilink-noise filters for the indexer and the crystallization checker.

Two surfaces extract ``[[wikilinks]]`` and must agree on *which files cast votes*
and *how code spans are neutralized*, or they drift:

- ``memex.scripts.embeddings.extract_wikilinks`` populates the ``wikilinks``
  graph table (backlinks + ``graph stats`` total/broken counts).
- ``memex.scripts.crystallization_check`` scans for unresolved links to surface
  ghost nodes ready to crystallize.

Before this module the two paths diverged: v0.15.3 taught the checker to skip
transcripts/auto-memory as link SOURCES and to strip code spans, but the indexer
still scanned them — so ``graph stats`` over-counted broken links from raw
``[[$MEMO_PATH]]``-style transcript fragments and fenced-code examples. Keeping
the rules here, dependency-light (stdlib ``re`` only), lets the offline checker
and the core indexer both import them without pulling heavy deps and without a
core→CLI import cycle.

The transcript/auto-memory exclusion mirrors ``graph_queries``' existing
``NOT LIKE '%/transcripts/%'`` task filter and the ``status: archived``
source-skip: these files remain valid wikilink TARGETS, they just don't cast
votes as SOURCES.
"""

from __future__ import annotations

import re

# Code-span / escape patterns. Wikilink-shaped fragments inside code blocks
# (TOML ``[[section]]`` headers, bash ``if [[ -f x ]]``) and terminal escape
# dumps are not real links; Obsidian's metadataCache parser ignores code spans
# and this mirrors that so neither surface over-reports phantom nodes.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_code_spans(text: str) -> str:
    """Remove fenced/inline code and ANSI escapes before wikilink scanning.

    Line numbers are preserved: a fenced block is replaced with the same number
    of blank lines (not deleted), so a caller that records ``line_number`` per
    link (the indexer) stays accurate for links that follow a code block. Inline
    code and ANSI sequences never span newlines, so removing them outright leaves
    line counts unchanged.
    """
    text = _ANSI_CSI_RE.sub("", text)
    text = _FENCED_CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def is_noncurated_source(rel: str) -> bool:
    """True if a vault-relative path is a raw transcript or synced auto-memory.

    Transcripts (``projects/*/transcripts/``) are conversation/terminal dumps and
    auto-memory (``projects/*/auto-memory/``) is synced from Claude Code — neither
    is curated knowledge, so neither may cast a wikilink vote (graph backlink /
    broken-link count, or crystallization ghost-node). They remain valid wikilink
    TARGETS (their stems stay resolvable); they are only excluded as link
    SOURCES. Same principle as the ``status: archived`` source-skip and
    ``graph_queries``' ``NOT LIKE '%/transcripts/%'`` task exclusion.

    Matching on path *segments* (``/transcripts/``, ``/auto-memory/``) — not bare
    stems — covers both resolution paths and avoids excluding a legitimately
    named ``topics/transcripts.md``. Backslashes are normalized for Windows
    paths.
    """
    rel = rel.replace("\\", "/")
    return "/transcripts/" in rel or "/auto-memory/" in rel
