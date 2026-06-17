# Note: Gemini provider (primary) uses google-genai
# LM Studio provider (local fallback) uses requests for API calls
"""
Claude Memory Plugin - Embedding Pipeline

Provides:
- Section-aware markdown chunking with overlap
- Gemini API integration for embeddings
- Content-hash based caching (no duplicate API calls)
- Vector serialization for sqlite-vec

Usage:
    embeddings.py --test  # Test embedding a sample text
    embeddings.py --index <file>  # Index a specific file
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import random
import re
import sqlite3
import struct
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

try:
    from google.genai import errors as genai_errors
except ImportError:  # pragma: no cover - optional dependency for Gemini only
    genai_errors = None

from memex.config import get_settings
from memex.observations import init_observation_schema

# Lazy imports for optional dependencies
_genai_client = None
_tokenizer = None

GEMINI_TOKEN_BUDGET_PER_BATCH = 8000
GEMINI_MAX_ATTEMPTS = 4
GEMINI_BACKOFF_SCHEDULE = (10.0, 30.0, 90.0)
# ±20% jitter on backoff sleep to prevent thundering-herd retries when N
# concurrent batches all hit 429 simultaneously and would otherwise wake
# at the same instant. Tests seed `random` for determinism.
GEMINI_BACKOFF_JITTER = 0.2
# Concurrent in-flight batches against the Gemini embedding API.
# Math: Tier 2 paid = 5K RPM / 5M TPM. With 8K-token batches, TPM caps
# at ~625 RPM. 5 concurrent × ~1s/batch ≈ 300 RPM ≈ 48% TPM utilization,
# leaving ample headroom — a conservative default for unknown quota tiers
# (free tier is only 30K TPM). Bump to 8-10 after observing one full rebuild
# with no 429s in ~/.memex/logs/nightly-rebuild.log. The token-aware batching
# + retry/backoff path tolerates occasional 429s regardless.
GEMINI_CONCURRENCY = 5

# Serializes the env-var stash dance in `GeminiProvider._get_client` across
# threads. The dance temporarily pops `GOOGLE_API_KEY` to suppress the SDK's
# "both keys set" warning, then restores it. Concurrent provider instances
# on different threads could otherwise observe a half-mutated environment.
_GET_CLIENT_LOCK = threading.Lock()


def _is_retryable_api_error(exc: Exception) -> bool:
    if genai_errors is None:
        return False
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.APIError):
        if exc.code in (429, 500, 503):
            return True
        if (exc.status or "").upper() == "RESOURCE_EXHAUSTED":
            return True
    return False


# ============================================================================
# Configuration
# ============================================================================

def get_embedding_config() -> dict:
    """Load embedding configuration."""
    config = get_settings().embeddings
    return {
        "enabled": config.enabled,
        "provider": config.provider,
        "model": config.model,
        "dimensions": config.dimensions,
        "index_dimensions": config.effective_index_dimensions,
        "api_key_env": config.api_key_env,
    }


def get_chunk_config() -> dict:
    """Load chunking configuration."""
    config = get_settings().search
    return {
        "max_tokens": config.chunk_max_tokens,
        "overlap_tokens": config.chunk_overlap_tokens,
    }


# ============================================================================
# Token Counting
# ============================================================================

def get_tokenizer():
    """Get or create tokenizer (lazy loading)."""
    global _tokenizer
    if _tokenizer is None:
        import tiktoken
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count tokens in text."""
    return len(get_tokenizer().encode(text))


def get_last_n_tokens(text: str, n: int) -> str:
    """Get the last n tokens of text as a string."""
    enc = get_tokenizer()
    tokens = enc.encode(text)
    if len(tokens) <= n:
        return text
    return enc.decode(tokens[-n:])


def get_first_n_tokens(text: str, n: int) -> str:
    """Get the first n tokens of text as a string."""
    enc = get_tokenizer()
    tokens = enc.encode(text)
    if len(tokens) <= n:
        return text
    return enc.decode(tokens[:n])


# ============================================================================
# Chunking
# ============================================================================

@dataclass
class Chunk:
    """A chunk of document content."""
    index: int
    content: str
    content_hash: str
    chunk_type: str = "content"  # frontmatter, turn, memo, concept, content
    is_frontmatter: bool = False
    start_offset: int = 0
    end_offset: int = 0

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()
        # Sync is_frontmatter with chunk_type for backwards compatibility
        if self.is_frontmatter:
            self.chunk_type = "frontmatter"


def extract_frontmatter(content: str) -> tuple[str | None, str]:
    """
    Extract YAML frontmatter from markdown content.

    Returns: (frontmatter_str, body_str)
    """
    if not content.startswith("---"):
        return None, content

    try:
        # Find closing ---
        end_idx = content.index("---", 3)
        frontmatter = content[:end_idx + 3]
        body = content[end_idx + 3:].strip()
        return frontmatter, body
    except ValueError:
        return None, content


def split_by_headers(text: str) -> list[tuple[str, str]]:
    """
    Split text by markdown headers.

    Returns: List of (header, content) tuples
    """
    # Match ## or ### headers
    pattern = r'^(#{2,3}\s+.+)$'
    parts = re.split(pattern, text, flags=re.MULTILINE)

    sections = []
    current_header = ""
    current_content = []

    for part in parts:
        if re.match(r'^#{2,3}\s+', part):
            # This is a header
            if current_content or current_header:
                sections.append((current_header, "\n".join(current_content)))
            current_header = part
            current_content = []
        else:
            current_content.append(part)

    # Don't forget the last section
    if current_content or current_header:
        sections.append((current_header, "\n".join(current_content)))

    return sections


def chunk_markdown(
    content: str,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None
) -> list[Chunk]:
    """
    Chunk markdown content with section awareness.

    Strategy:
    1. Extract frontmatter as chunk[0] if present
    2. Split remaining content by ## headers
    3. For each section:
       - If <= max_tokens, keep as single chunk
       - If > max_tokens, split at paragraph boundaries
       - Add overlap from previous chunk
    """
    config = get_chunk_config()
    max_tokens = max_tokens or config["max_tokens"]
    overlap_tokens = overlap_tokens or config["overlap_tokens"]

    chunks = []
    offset = 0

    # 1. Handle frontmatter
    frontmatter, body = extract_frontmatter(content)
    if frontmatter:
        chunks.append(Chunk(
            index=0,
            content=frontmatter,
            content_hash=hashlib.sha256(frontmatter.encode()).hexdigest(),
            is_frontmatter=True,
            start_offset=0,
            end_offset=len(frontmatter)
        ))
        offset = len(frontmatter) + 1  # +1 for newline

    if not body.strip():
        return chunks

    # 2. Split by headers
    sections = split_by_headers(body)

    # 3. Process sections with overlap
    prev_tail = ""

    for header, section_content in sections:
        section_text = f"{header}\n{section_content}".strip() if header else section_content.strip()

        if not section_text:
            continue

        section_tokens = count_tokens(section_text)

        if section_tokens <= max_tokens:
            # Section fits in one chunk
            chunk_content = prev_tail + section_text if prev_tail else section_text

            chunks.append(Chunk(
                index=len(chunks),
                content=chunk_content,
                content_hash=hashlib.sha256(chunk_content.encode()).hexdigest(),
                start_offset=offset,
                end_offset=offset + len(section_text)
            ))

            prev_tail = get_last_n_tokens(section_text, overlap_tokens)
            offset += len(section_text) + 1

        else:
            # Section too large - split at paragraphs
            paragraphs = section_text.split("\n\n")
            current_chunk = prev_tail
            chunk_start = offset

            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue

                potential = current_chunk + "\n\n" + para if current_chunk else para

                if count_tokens(potential) > max_tokens and current_chunk:
                    # Save current chunk and start new one
                    chunks.append(Chunk(
                        index=len(chunks),
                        content=current_chunk,
                        content_hash=hashlib.sha256(current_chunk.encode()).hexdigest(),
                        start_offset=chunk_start,
                        end_offset=offset
                    ))

                    prev_tail = get_last_n_tokens(current_chunk, overlap_tokens)
                    current_chunk = prev_tail + "\n\n" + para
                    chunk_start = offset
                else:
                    current_chunk = potential

                offset += len(para) + 2  # +2 for \n\n

            # Save remaining content
            if current_chunk:
                chunks.append(Chunk(
                    index=len(chunks),
                    content=current_chunk,
                    content_hash=hashlib.sha256(current_chunk.encode()).hexdigest(),
                    start_offset=chunk_start,
                    end_offset=offset
                ))
                prev_tail = get_last_n_tokens(current_chunk, overlap_tokens)

    return chunks


# ============================================================================
# Content-Type Detection & Specialized Chunkers
# ============================================================================

# Patterns for transcript parsing
TURN_PATTERN = re.compile(r'^## Turn \d+', re.MULTILINE)
TOOL_RESULT_PATTERN = re.compile(r'(\*\*Result:\*\*\s*\n\s*```)([\s\S]*?)(```)')


def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter as dict."""
    if not content.startswith("---"):
        return {}

    try:
        end = content.index("---", 3)
        yaml_content = content[3:end].strip()

        result = {}
        for line in yaml_content.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]
                result[key] = value
        return result
    except ValueError:
        return {}


# ============================================================================
# Graph Extraction Functions
# ============================================================================

WIKILINK_PATTERN = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
TASK_PATTERN = re.compile(r'^(\s*)-\s*\[([ xX])\]\s*(.+)$')
HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$')


def resolve_wikilink(link: str, vault_root: Path, conn: sqlite3.Connection | None = None) -> dict:
    """
    Resolve [[link]] to actual file path.

    Checks common locations in priority order, then aliases if DB available.
    """
    # Handle section links like [[page#section]]
    link_base = link.split('#')[0] if '#' in link else link

    if not link_base:
        return {'path': None, 'is_broken': True}

    # Normalize: remove .md if present for comparison
    link_normalized = link_base.removesuffix('.md')

    # Check common locations in priority order
    candidates = [
        vault_root / f"{link_normalized}.md",
        vault_root / "topics" / f"{link_normalized}.md",
    ]

    # Check for project path pattern
    if '/' in link_normalized:
        candidates.insert(0, vault_root / f"{link_normalized}.md")
        candidates.append(vault_root / link_normalized / "_project.md")
    else:
        # Could be a project name
        candidates.append(vault_root / "projects" / link_normalized / "_project.md")

    for candidate in candidates:
        if candidate.exists():
            try:
                return {'path': str(candidate.relative_to(vault_root)), 'is_broken': False}
            except ValueError:
                continue

    # Check aliases in database if available
    if conn:
        result = conn.execute(
            "SELECT doc_path FROM doc_aliases WHERE alias = ?",
            (link_normalized,)
        ).fetchone()
        if result:
            return {'path': result[0], 'is_broken': False}

    return {'path': None, 'is_broken': True}


def extract_wikilinks(content: str, source_path: str, vault_root: Path,
                      conn: sqlite3.Connection | None = None) -> list[dict]:
    """Extract [[wikilinks]] from markdown content."""
    links = []
    lines = content.split('\n')

    for line_num, line in enumerate(lines, 1):
        for match in WIKILINK_PATTERN.finditer(line):
            link_target = match.group(1).strip()
            display_text = match.group(2).strip() if match.group(2) else None

            # Resolve to actual path
            resolved = resolve_wikilink(link_target, vault_root, conn)

            links.append({
                'source_path': source_path,
                'target_path': resolved['path'],
                'link_text': link_target,
                'display_text': display_text,
                'is_broken': 1 if resolved['is_broken'] else 0,
                'line_number': line_num
            })

    return links


def extract_tasks(content: str, doc_path: str) -> list[dict]:
    """Extract - [ ] and - [x] task items."""
    tasks = []
    lines = content.split('\n')
    current_section = None

    for line_num, line in enumerate(lines, 1):
        # Track current section
        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            current_section = heading_match.group(2).strip()

        # Check for task
        task_match = TASK_PATTERN.match(line)
        if task_match:
            completed = task_match.group(2).lower() == 'x'
            task_text = task_match.group(3).strip()

            tasks.append({
                'doc_path': doc_path,
                'task_text': task_text,
                'completed': 1 if completed else 0,
                'line_number': line_num,
                'section': current_section
            })

    return tasks


def extract_sections(content: str, doc_path: str) -> list[dict]:
    """Extract markdown headings for document structure."""
    sections = []
    lines = content.split('\n')

    for line_num, line in enumerate(lines, 1):
        match = HEADING_PATTERN.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()

            sections.append({
                'doc_path': doc_path,
                'heading': heading,
                'level': level,
                'line_number': line_num
            })

    return sections


def extract_tags_and_aliases(meta: dict, doc_path: str) -> tuple[list[dict], list[dict]]:
    """Extract tags and aliases from frontmatter."""
    tags = []
    aliases = []

    # Tags can be string or list
    raw_tags = meta.get('tags', [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    for tag in raw_tags:
        if tag:  # Skip empty tags
            tags.append({'doc_path': doc_path, 'tag': tag.strip()})

    # Aliases
    raw_aliases = meta.get('aliases', [])
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    for alias in raw_aliases:
        if alias:  # Skip empty aliases
            aliases.append({'doc_path': doc_path, 'alias': alias.strip()})

    return tags, aliases


def get_content_type(rel_path: str, content: str) -> str:
    """Detect content type from path and frontmatter."""
    if "/transcripts/" in rel_path:
        return "transcript"
    if "/auto-memory/" in rel_path:
        return "auto-memory"

    meta = parse_frontmatter(content)
    doc_type = meta.get("type", "")

    if doc_type in ("memo", "concept", "transcript", "project", "auto-memory"):
        return doc_type

    return "markdown"


def truncate_tool_outputs(content: str, max_chars: int = 500) -> str:
    """Truncate tool result blocks while preserving tool name and input."""
    def truncate_match(match):
        prefix = match.group(1)  # **Result:**\n```
        result = match.group(2)
        suffix = match.group(3)  # ```

        if len(result) <= max_chars:
            return match.group(0)

        # Keep 70% from start, 20% from end (10% for truncation message)
        keep_start = int(max_chars * 0.7)
        keep_end = int(max_chars * 0.2)
        truncated_len = len(result) - (keep_start + keep_end)
        return (prefix +
                result[:keep_start] +
                f"\n\n[...truncated {truncated_len} chars...]\n\n" +
                result[-keep_end:] +
                suffix)

    return TOOL_RESULT_PATTERN.sub(truncate_match, content)


def chunk_transcript_turns(content: str, meta: dict) -> list[Chunk]:
    """
    Chunk transcript by conversation turns.

    Each turn (User + Assistant) becomes one chunk.
    Frontmatter + Summary becomes Chunk 0.
    """
    chunks = []

    # Split header (frontmatter + summary) from turns
    first_turn = TURN_PATTERN.search(content)
    if not first_turn:
        return chunk_markdown(content)  # Fallback for non-standard transcripts

    header = content[:first_turn.start()].strip()
    body = content[first_turn.start():]

    # Chunk 0: Frontmatter + Summary
    if header:
        chunks.append(Chunk(
            index=0,
            content=header,
            content_hash=hashlib.sha256(header.encode()).hexdigest(),
            chunk_type="frontmatter"
        ))

    # Context prefix for turn chunks
    project = meta.get("project", "unknown")
    date = meta.get("date", "unknown")
    context_prefix = f"[Project: {project} | Date: {date}]\n\n"

    # Split body by turn headers.
    # TURN_PATTERN has no capturing group, so re.split returns
    # [pre_match, after_turn_1, after_turn_2, ..., after_turn_N].
    # Since `body` starts at the first match, parts[0] == "" and the
    # real turn bodies live at parts[1:], one per header.
    parts = TURN_PATTERN.split(body)
    turn_headers = TURN_PATTERN.findall(body)

    config = get_chunk_config()
    max_tokens = config.get("max_tokens", 400)

    # Process each turn (parts[1:] — one body per header, in order).
    # Previously this used parts[1::2], which assumed a capturing group
    # and silently dropped every other turn + misaligned headers with
    # bodies. Fixed 2026-04-21. A full rebuild is needed to re-chunk
    # every existing transcript.
    for turn_header, body_part in zip(turn_headers, parts[1:]):
        turn_content = turn_header + body_part

        # Truncate verbose tool outputs
        turn_content = truncate_tool_outputs(turn_content)

        # Prefix thinking blocks for better semantic understanding
        turn_content = turn_content.replace(
            '<summary>Thinking</summary>',
            '<summary>Assistant reasoning</summary>'
        )

        # Add context prefix
        final_content = context_prefix + turn_content

        # Check if turn exceeds token limit
        if count_tokens(final_content) > max_tokens * 3:
            # Fall back to sliding window for very long turns
            sub_chunks = chunk_markdown(final_content)
            for sub in sub_chunks:
                sub.chunk_type = "turn"
                sub.index = len(chunks)
                chunks.append(sub)
        else:
            chunks.append(Chunk(
                index=len(chunks),
                content=final_content,
                content_hash=hashlib.sha256(final_content.encode()).hexdigest(),
                chunk_type="turn"
            ))

    return chunks


def chunk_whole_doc(content: str, meta: dict) -> list[Chunk]:
    """
    Embed entire document as single chunk.

    For short docs like memos (500-2000 tokens) that shouldn't be split.
    Falls back to chunk_markdown if document is too long.
    """
    # Check if within token limit (leave buffer for 2048 limit)
    if count_tokens(content) > 1800:
        return chunk_markdown(content)

    # Build context prefix
    project = meta.get("project", "unknown")
    title = meta.get("title", "untitled")
    context_prefix = f"[Project: {project} | Title: {title}]\n\n"

    final_content = context_prefix + content
    doc_type = meta.get("type", "memo")

    return [Chunk(
        index=0,
        content=final_content,
        content_hash=hashlib.sha256(final_content.encode()).hexdigest(),
        chunk_type=doc_type
    )]


# ============================================================================
# Vector Serialization
# ============================================================================

def serialize_f32(vector: list[float]) -> bytes:
    """Serialize float list to compact bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


def deserialize_f32(blob: bytes) -> list[float]:
    """Deserialize bytes back to float list."""
    count = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{count}f", blob))


# ============================================================================
# Matryoshka truncation + vec-table dimension/metadata helpers (v0.15.0)
# ============================================================================

def get_vector_dimensions(config: dict | None = None) -> int:
    """Dimension stored in the vec0 tables and used for KNN queries.

    Matryoshka-truncated from the native model `dimensions` when
    `index_dimensions` is configured below it; otherwise equals `dimensions`.
    The native dimension still governs the API call and `embedding_cache`
    fidelity — only storage/search are truncated.
    """
    cfg = config or get_embedding_config()
    native = cfg.get("dimensions", 3072)
    idx = cfg.get("index_dimensions") or native
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = int(native)
    return idx


def truncate_unit_vector(blob: bytes, target_dim: int) -> bytes:
    """Truncate a serialized float32 embedding to ``target_dim`` and
    L2-renormalize.

    Valid for Matryoshka (MRL) models like Gemini Embedding 2: the first N
    dims are an independently-useful embedding after renormalization. A unit
    3072d vector truncated to its first 768 dims is NOT unit-norm, so the
    renormalize step is required (sqlite-vec L2 distance is only monotonic
    with cosine on unit vectors). No-op when ``target_dim`` >= current dim.
    """
    n = len(blob) // 4
    if target_dim <= 0 or target_dim >= n:
        return blob
    floats = struct.unpack(f"{n}f", blob)[:target_dim]
    norm = sum(x * x for x in floats) ** 0.5
    if norm > 0:
        floats = tuple(x / norm for x in floats)
    return struct.pack(f"{target_dim}f", *floats)


def match_query_dim(conn: sqlite3.Connection, table: str, query_blob: bytes) -> bytes:
    """Truncate ``query_blob`` to whatever dimension ``table`` actually stores.

    Defensive: keeps query/stored dimensions aligned regardless of which code
    path produced the query vector (embed_query already truncates, but
    provider-direct callers may pass full-dim vectors). No-op when the table
    is empty or the query already matches.
    """
    try:
        row = conn.execute(f"SELECT embedding FROM {table} LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return query_blob
    if not row or not row[0]:
        return query_blob
    stored_dim = len(row[0]) // 4
    if len(query_blob) // 4 > stored_dim:
        return truncate_unit_vector(query_blob, stored_dim)
    return query_blob


def date_to_yyyymmdd(value) -> int:
    """Encode an ISO-ish date ('YYYY-MM-DD'...) as an integer YYYYMMDD for
    sqlite-vec INTEGER range filters. 0 when unknown/unparseable (so it is
    excluded by `>= since` filters, matching the prior 'unknown date →
    excluded' post-filter behavior)."""
    if not value:
        return 0
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    return int(m.group(1) + m.group(2) + m.group(3)) if m else 0


def date_int_from_path(path: str) -> int:
    """Best-effort YYYYMMDD from a dated memo filename
    (projects/x/memos/YYYY-MM-DD-title.md). 0 when absent."""
    return date_to_yyyymmdd(path)


def project_from_path(path: str) -> str:
    """Extract the project slug from a vault-relative path
    (projects/<slug>/...). Empty string when not under projects/."""
    if path and path.startswith("projects/"):
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[1]
    return ""


def vec_stored_dim(conn: sqlite3.Connection, table: str) -> int | None:
    """The float dimension currently stored in a vec0 ``table`` (None when the
    table is absent or empty).

    Insert paths align to THIS, not the configured index dim, so that a vec
    write never dimension-mismatches the live table during the window after
    `index_dimensions` is changed in config but before `memex index migrate-vec`
    runs (e.g. a nightly incremental rebuild firing mid-transition). Once
    migrate-vec completes, stored dim == configured index dim and the two agree.
    """
    try:
        row = conn.execute(f"SELECT embedding FROM {table} LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    return len(row[0]) // 4


def table_has_metadata(conn: sqlite3.Connection, table: str) -> bool:
    """True when the vec0 ``table`` carries the v0.15.0 metadata columns
    (doc_project/doc_type/doc_date). Used to stay backward-compatible with
    indexes created before the metadata migration."""
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return False
    return {"doc_project", "doc_type", "doc_date"}.issubset(cols)


# ============================================================================
# Embedding Provider Abstraction
# ============================================================================

class EmbeddingProvider(ABC):
    """Abstract embedding provider interface."""

    @abstractmethod
    def embed_texts(self, texts: list[str], task_type: str = "document") -> list[list[float] | None]:
        """
        Embed multiple texts.

        Args:
            texts: List of text strings to embed
            task_type: "document" for indexing, "query" for search

        Returns:
            List of embedding vectors (or None for failures)
        """
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Embedding vector dimensionality."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier for caching (e.g., 'google', 'lmstudio')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier for caching."""
        ...


class PartialEmbeddingFailure(Exception):
    """Raised when a batch embedding request exhausts retries."""

    def __init__(self, results: list[list[float] | None], last_error: Exception):
        self.results = results
        self.last_error = last_error
        super().__init__(
            f"Embedding batch failed after retries: "
            f"{sum(1 for r in results if r is None)}/{len(results)} items missing. "
            f"Last error: {last_error}"
        )


# Unit-norm invariant: sqlite-vec uses L2 distance by default, which is only
# monotonic with cosine similarity when inputs are unit-norm. If a provider
# starts returning non-unit vectors, ranking silently degrades. We check
# a few sampled vectors per batch (cheap) and raise loudly so the drift
# is caught at write time rather than at query time.
#
# Tolerance is expressed on ||v||^2, not ||v||. A 1% slack on ||v|| maps to
# roughly 2% slack on ||v||^2 since (1 ± 0.01)^2 ≈ 1 ± 0.02.
_UNIT_NORM_TOLERANCE = 0.02  # on ||v||^2 — equivalent to ~1% on ||v||


def _assert_unit_norm(batch: list[list[float] | None], provider: str) -> None:
    """Raise ValueError if any sampled non-None vector in `batch` is not
    unit-norm (within tolerance). Samples head, mid, and tail — enough to
    catch provider-level drift (which is uniform across a batch) without
    paying O(N·D) per batch at D=3072.

    This is not a defense against per-vector corruption — for that,
    normalize at insert time. It IS a canary for model/config drift
    (e.g., accidentally switching to a non-normalized model variant).
    """
    n = len(batch)
    if n == 0:
        return
    # Head + mid + tail, deduplicated so tiny batches don't double-check.
    indices = {0, n // 2, n - 1}
    for i in sorted(indices):
        vec = batch[i]
        if vec is None:
            continue
        norm_sq = sum(x * x for x in vec)
        if not (1.0 - _UNIT_NORM_TOLERANCE <= norm_sq <= 1.0 + _UNIT_NORM_TOLERANCE):
            raise ValueError(
                f"{provider} provider returned non-unit vector "
                f"(||v||^2={norm_sq:.4f}, tolerance=±{_UNIT_NORM_TOLERANCE}). "
                f"sqlite-vec assumes unit-norm inputs for cosine-correct ranking. "
                f"Check model config or add explicit normalization."
            )


# ============================================================================
# Gemini Provider
# ============================================================================

class GeminiProvider(EmbeddingProvider):
    """Gemini API embedding provider."""

    def __init__(self, config: dict):
        self._model = config.get("model", "gemini-embedding-2")
        self._dimensions_val = config.get("dimensions", 3072)
        api_key_env = config.get("api_key_env", "GEMINI_API_KEY")
        self._api_key = os.environ.get(api_key_env)
        self._client = None

        if not self._api_key:
            raise ValueError(f"Gemini API key not found: set ${api_key_env}")

    @property
    def dimensions(self) -> int:
        return self._dimensions_val

    @property
    def provider_name(self) -> str:
        return "google"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self):
        """Lazy-load Gemini client.

        The env-var stash dance below is process-global state — concurrent
        provider instances on different threads could otherwise observe a
        half-mutated environment. `_GET_CLIENT_LOCK` serializes the critical
        section. The lock is contended at most once per provider instance
        (subsequent calls early-return on `self._client is not None`).
        """
        if self._client is None and self._api_key:
            with _GET_CLIENT_LOCK:
                # Re-check after acquiring the lock — another thread may have
                # initialized the client while we were waiting.
                if self._client is None:
                    try:
                        # Temporarily hide GOOGLE_API_KEY to suppress SDK warning
                        # "Both GOOGLE_API_KEY and GEMINI_API_KEY are set"
                        # We pass the key explicitly, so env var sniffing is unnecessary.
                        stashed = os.environ.pop("GOOGLE_API_KEY", None)
                        try:
                            from google import genai
                            self._client = genai.Client(api_key=self._api_key)
                        finally:
                            if stashed is not None:
                                os.environ["GOOGLE_API_KEY"] = stashed
                    except ImportError:
                        raise ValueError("google-genai not installed")
        return self._client

    def _batch_texts_by_token_budget(self, texts: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_tokens = 0

        for text in texts:
            text_tokens = count_tokens(text)

            if current_batch and current_tokens + text_tokens > GEMINI_TOKEN_BUDGET_PER_BATCH:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            if text_tokens > GEMINI_TOKEN_BUDGET_PER_BATCH:
                batches.append([text])
                continue

            current_batch.append(text)
            current_tokens += text_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    def _build_embed_config(self, task_type: str):
        from google.genai import types

        if self._model.startswith("gemini-embedding-2"):
            return types.EmbedContentConfig()

        gemini_task = "RETRIEVAL_QUERY" if task_type == "query" else "RETRIEVAL_DOCUMENT"
        return types.EmbedContentConfig(task_type=gemini_task)

    async def _aembed_one_batch(
        self,
        sub_batch: list[str],
        config,
    ) -> list[list[float] | None]:
        """Embed one sub-batch with retry, awaiting the sync SDK call via
        `asyncio.to_thread` for concurrency.

        Why not `client.aio.models.embed_content`? The SDK's aio HTTP client
        binds to whichever event loop first touches it. In the running-loop
        fallback path (`embed_texts` punts to a worker thread that spins up
        a fresh loop), that binding goes stale across calls and raises
        "Event loop is closed". `asyncio.to_thread` over the sync API is
        loop-agnostic — each call runs on the loop's default executor with
        no shared SDK state across loops.

        Concurrency is preserved because `asyncio.gather` schedules these
        coroutines and `to_thread` releases the GIL during the HTTP call.
        """
        client = self._get_client()
        from google.genai import types
        # CRITICAL: pass one Content per text. A list of bare strings is
        # interpreted by the SDK as the *parts of a single Content*, so the API
        # returns ONE embedding for the whole list (the rest silently None) —
        # batch under-population. Wrapping each text in its own Content yields
        # one embedding per text. Verified against gemini-embedding-2 on
        # google-genai 1.75 and 2.8 (a bare-string list returns 1; a Content
        # list returns N). Regression: tests/test_embedding_batch_contents.py.
        contents = [types.Content(parts=[types.Part(text=t)]) for t in sub_batch]
        last_exc: Exception | None = None
        for attempt in range(GEMINI_MAX_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    client.models.embed_content,
                    model=self._model,
                    contents=contents,
                    config=config,
                )
                embeddings = list(getattr(response, "embeddings", []) or [])
                batch_results = [emb.values if emb else None for emb in embeddings]
                if len(batch_results) < len(sub_batch):
                    batch_results.extend([None] * (len(sub_batch) - len(batch_results)))
                elif len(batch_results) > len(sub_batch):
                    batch_results = batch_results[:len(sub_batch)]
                # Sanity check: Gemini Embedding 2 returns unit-norm vectors.
                # sqlite-vec uses L2 distance; rankings are only monotonic with
                # cosine when inputs are normalized. If a provider starts
                # returning non-unit vectors (model change, API drift), hybrid
                # search + detect_contradictions silently degrade. Fail loud.
                _assert_unit_norm(batch_results, provider="gemini")
                return batch_results
            except Exception as exc:
                last_exc = exc
                is_non_retryable_client_error = (
                    genai_errors is not None
                    and isinstance(exc, genai_errors.ClientError)
                    and not _is_retryable_api_error(exc)
                )
                if is_non_retryable_client_error:
                    raise
                if _is_retryable_api_error(exc) and attempt < GEMINI_MAX_ATTEMPTS - 1:
                    base = GEMINI_BACKOFF_SCHEDULE[attempt]
                    # Jitter prevents N concurrent batches that all hit 429 at
                    # the same instant from waking up simultaneously and
                    # repeating the burst. Range: base ± GEMINI_BACKOFF_JITTER.
                    jitter = random.uniform(-GEMINI_BACKOFF_JITTER, GEMINI_BACKOFF_JITTER)
                    await asyncio.sleep(base * (1.0 + jitter))
                    continue
                raise
        # Defensive: loop should always either return or raise above.
        assert last_exc is not None
        raise last_exc

    async def _aembed_texts(
        self,
        texts: list[str],
        task_type: str = "document",
    ) -> list[list[float] | None]:
        """Async core: token-batch + concurrent dispatch + ordered reassembly.

        Concurrency is gated by `GEMINI_CONCURRENCY` via `asyncio.Semaphore`.
        Per-batch retry/backoff lives in `_aembed_one_batch`. Failures from
        any batch surface as `PartialEmbeddingFailure`, with that batch's
        slot filled with Nones — successful batches are preserved so a
        single bad batch doesn't blow away the whole call.
        """
        if not texts:
            return []

        # Pre-init client in the calling thread so the env-var stash dance
        # in `_get_client` doesn't race across coroutines on first use.
        self._get_client()
        config = self._build_embed_config(task_type)
        batches = self._batch_texts_by_token_budget(texts)
        # Per-call (not per-process) Semaphore: each `embed_texts` invocation
        # gets its own GEMINI_CONCURRENCY-slot window. Acceptable because the
        # current call pattern is serial within a pipeline instance — no two
        # `_aembed_texts` coroutines on the same event loop overlap. If the
        # call pattern ever becomes concurrent (e.g., parallel rebuild + query
        # path on one process), promote this to a class-level semaphore.
        sem = asyncio.Semaphore(GEMINI_CONCURRENCY)

        async def run_batch(batch: list[str]) -> list[list[float] | None]:
            async with sem:
                return await self._aembed_one_batch(batch, config)

        outcomes = await asyncio.gather(
            *(run_batch(b) for b in batches),
            return_exceptions=True,
        )

        flat_results: list[list[float] | None] = []
        first_exc: Exception | None = None
        for batch, outcome in zip(batches, outcomes):
            if isinstance(outcome, Exception):
                if first_exc is None:
                    first_exc = outcome
                flat_results.extend([None] * len(batch))
            else:
                flat_results.extend(outcome)

        if first_exc is not None:
            raise PartialEmbeddingFailure(flat_results, first_exc)
        return flat_results

    def embed_texts(self, texts: list[str], task_type: str = "document") -> list[list[float] | None]:
        """Sync entry point. Drives `_aembed_texts` via `asyncio.run`.

        Callers stay synchronous (`memex index rebuild`, `memex backfill obs`,
        single-text `embed_query`). Concurrency is internal: multiple sub-batches
        run in parallel under `GEMINI_CONCURRENCY`, ordered results re-flatten
        in submission order to preserve the `PartialEmbeddingFailure.results`
        contract that `index_rebuild._embed_and_insert` relies on.

        If invoked from inside a running event loop (e.g., `scripts/mcp_server.py`
        async tool handlers), `asyncio.run` would raise `RuntimeError`. We
        detect that case and run the coroutine on a fresh loop in a worker
        thread instead. Keeps the sync API contract for both contexts at the
        cost of a one-shot ThreadPoolExecutor per inside-loop call (rare path).
        """
        if not texts:
            return []
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._aembed_texts(texts, task_type=task_type))
        # Already inside an event loop — punt to a worker thread with its
        # own loop. Don't use asyncio.run_coroutine_threadsafe — the target
        # loop here is the *caller's* loop and we can't block it.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(self._aembed_texts(texts, task_type=task_type))
            ).result()


# ============================================================================
# LM Studio Provider
# ============================================================================

class LMStudioProvider(EmbeddingProvider):
    """LM Studio API embedding provider (OpenAI-compatible v1 API)."""

    def __init__(self, config: dict):
        self._base_url = config.get("base_url", "http://localhost:1234/v1")
        self._model = config.get("model", "qwen3-embedding-0.6b")
        self._dimensions_val = config.get("dimensions", 1024)
        self._api_key = config.get("api_key", "lm-studio")

    @property
    def dimensions(self) -> int:
        return self._dimensions_val

    @property
    def provider_name(self) -> str:
        return "lmstudio"

    @property
    def model_name(self) -> str:
        return self._model

    def embed_texts(self, texts: list[str], task_type: str = "document") -> list[list[float] | None]:
        """Embed texts using LM Studio API."""
        if not texts:
            return []

        try:
            import requests

            # LM Studio v1 API (OpenAI-compatible)
            response = requests.post(
                f"{self._base_url}/embeddings",
                json={
                    "input": texts,
                    "model": self._model
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=300
            )
            response.raise_for_status()

            data = response.json()

            # Extract embeddings from OpenAI-format response
            results = []
            for item in data["data"]:
                embedding = item["embedding"]
                if len(embedding) != self._dimensions_val:
                    embedding = embedding[:self._dimensions_val]
                results.append(embedding)

            # Sanity check: the vec_* tables assume unit-norm inputs. Qwen3
            # returns normalized vectors on OpenAI-compat endpoints, but this
            # is not contractual — fail loud if a local model drifts.
            _assert_unit_norm(results, provider="lmstudio")
            return results

        except Exception as e:
            print(f"LM Studio API error: {e}", file=sys.stderr)
            return [None] * len(texts)


# ============================================================================
# Embedding Pipeline (Provider-Agnostic)
# ============================================================================

class EmbeddingPipeline:
    """
    Embedding pipeline with provider abstraction and caching.

    Features:
    - Provider abstraction (Gemini API or LM Studio)
    - Content-hash based caching (no duplicate API calls)
    - Batch embedding support
    - Graceful fallback when provider unavailable
    """

    def __init__(self, config: dict | None = None):
        self.config = config or get_embedding_config()
        self._provider_impl: EmbeddingProvider | None = None
        self.enabled = False

        # Try to create provider
        provider_type = self.config.get("provider", "google")

        try:
            if provider_type == "lmstudio":
                self._provider_impl = LMStudioProvider(self.config)
            else:  # default to google
                self._provider_impl = GeminiProvider(self.config)

            # Sync provider properties to pipeline
            self.enabled = True
            self.provider = self._provider_impl.provider_name
            self.model = self._provider_impl.model_name
            self.dimensions = self._provider_impl.dimensions

        except (ValueError, FileNotFoundError, ImportError) as e:
            print(f"Embedding provider unavailable: {e}", file=sys.stderr)
            self.enabled = False
            # Set defaults for backward compat
            self.provider = provider_type
            self.model = self.config.get("model", "unknown")
            self.dimensions = self.config.get("dimensions", 0)

    def embed_text(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float] | None:
        """
        Embed a single text string.

        Args:
            text: Text to embed
            task_type: RETRIEVAL_DOCUMENT (indexing) or RETRIEVAL_QUERY (search)

        Returns:
            Embedding vector or None if embedding fails
        """
        if not self.enabled or not self._provider_impl:
            return None

        # Normalize task_type for provider interface
        normalized_task = "query" if "QUERY" in task_type else "document"

        try:
            results = self._provider_impl.embed_texts([text], task_type=normalized_task)
        except PartialEmbeddingFailure as exc:
            results = exc.results
        return results[0] if results else None

    def embed_query(self, query: str) -> bytes | None:
        """
        Embed a search query.

        Uses RETRIEVAL_QUERY task type for better search results.
        Returns serialized bytes for sqlite-vec, Matryoshka-truncated to the
        configured index dimension so the query matches the stored vectors.
        """
        vector = self.embed_text(query, task_type="RETRIEVAL_QUERY")
        if vector:
            return truncate_unit_vector(serialize_f32(vector), get_vector_dimensions())
        return None

    def embed_chunks(
        self,
        chunks: list[Chunk],
        conn: sqlite3.Connection
    ) -> list[tuple[int, bytes]]:
        """
        Embed chunks with caching.

        Checks embedding_cache table first, only calls API for uncached chunks.
        Returns list of (chunk_index, embedding_bytes) tuples.
        """
        if not self.enabled:
            return []

        results = []
        to_embed = []  # (index, content, hash)

        # Check cache for each chunk
        for chunk in chunks:
            cached = conn.execute(
                """SELECT embedding FROM embedding_cache
                   WHERE provider = ? AND model = ? AND content_hash = ?""",
                (self.provider, self.model, chunk.content_hash)
            ).fetchone()

            if cached:
                results.append((chunk.index, cached[0]))
            else:
                to_embed.append((chunk.index, chunk.content, chunk.content_hash))

        if to_embed and self._provider_impl:
            embeddings_result: list[list[float] | None]
            try:
                embeddings_result = self._provider_impl.embed_texts(
                    [item[1] for item in to_embed],
                    task_type="document",
                )
            except PartialEmbeddingFailure as exc:
                embeddings_result = exc.results
                failures = sum(1 for vec in embeddings_result if vec is None)
                print(
                    f"Embedding batch partially failed: {failures}/{len(to_embed)} items missing.",
                    file=sys.stderr,
                )

            if len(embeddings_result) < len(to_embed):
                embeddings_result = embeddings_result + [None] * (len(to_embed) - len(embeddings_result))
            elif len(embeddings_result) > len(to_embed):
                embeddings_result = embeddings_result[:len(to_embed)]

            for (idx, _content, content_hash), vec in zip(to_embed, embeddings_result):
                if vec is not None:
                    embedding_blob = serialize_f32(vec)
                    conn.execute(
                        """INSERT OR REPLACE INTO embedding_cache
                           (provider, model, content_hash, embedding)
                           VALUES (?, ?, ?, ?)""",
                        (self.provider, self.model, content_hash, embedding_blob)
                    )
                    results.append((idx, embedding_blob))

            # NOTE: no `conn.commit()` here. Commits are the caller's
            # responsibility so per-doc SAVEPOINTs in the rebuild loops
            # actually contain writes. See `index_document` and
            # `index_rebuild.rebuild_*` for the per-doc atomicity model.

        # Sort by original index
        results.sort(key=lambda x: x[0])
        return results


# ============================================================================
# Database Schema
# ============================================================================

def init_embedding_schema(conn: sqlite3.Connection):
    """Initialize embedding-related tables."""

    # Try to load sqlite-vec extension
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        print(f"Warning: Could not load sqlite-vec: {e}", file=sys.stderr)
        return False

    config = get_embedding_config()
    native_dim = config.get("dimensions", 3072)
    dimensions = get_vector_dimensions(config)

    # Validate dimensions to prevent SQL injection from config
    try:
        dimensions = int(dimensions)
        if not 1 <= dimensions <= 10000:
            raise ValueError("out of range")
    except (TypeError, ValueError):
        print(f"Invalid embedding dimensions: {dimensions}, using default 3072", file=sys.stderr)
        dimensions = 3072
    try:
        native_dim = int(native_dim)
    except (TypeError, ValueError):
        native_dim = 3072

    # Check for dimension migration (e.g. provider switch 1024↔3072).
    try:
        row = conn.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
        if row:
            existing_dims = len(row[0]) // 4  # 4 bytes per float32
            if existing_dims != dimensions:
                # Matryoshka truncation (native→index dim) must NOT drop +
                # re-embed: the embedding_cache holds full-fidelity vectors and
                # `memex index migrate-vec` truncates in place for free. Only a
                # genuine provider/model dimension change (existing dim is not
                # the native dim) warrants the destructive auto-migration.
                if existing_dims == native_dim and dimensions < native_dim:
                    print(
                        f"vec_chunks is {existing_dims}d but index_dimensions="
                        f"{dimensions}. Run `memex index migrate-vec` to truncate "
                        f"in place (no re-embed). Skipping auto-migration.",
                        file=sys.stderr,
                    )
                else:
                    print(f"Dimension migration detected: {existing_dims}d → {dimensions}d", file=sys.stderr)
                    print("Dropping vec_chunks table and clearing chunks...", file=sys.stderr)
                    conn.execute("DROP TABLE IF EXISTS vec_chunks")
                    conn.execute("DELETE FROM chunks")
                    conn.commit()
                    print("Run full rebuild to re-embed with new model", file=sys.stderr)
    except sqlite3.OperationalError:
        pass  # vec_chunks doesn't exist yet

    # Vector embeddings table (sqlite-vec virtual table). Metadata columns
    # (v0.15.0) enable filter-pushdown inside the KNN — see vector_search().
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
        USING vec0(
            embedding float[{dimensions}],
            doc_project text,
            doc_type text,
            doc_date integer
        )
    """)

    # Chunk metadata table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            chunk_type TEXT NOT NULL DEFAULT 'content',
            start_offset INTEGER,
            end_offset INTEGER,
            doc_date TEXT NOT NULL DEFAULT '',
            doc_project TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(doc_path, chunk_index)
        )
    """)

    # Add chunk_type column if it doesn't exist (migration for existing DBs)
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN chunk_type TEXT NOT NULL DEFAULT 'content'")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add doc_date and doc_project columns (migration for existing DBs)
    for col, default in [("doc_date", "''"), ("doc_project", "''")]:
        try:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Embedding cache table (for deduplication across documents)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (provider, model, content_hash)
        )
    """)

    # Document hash tracking for incremental updates
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_hashes (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            last_indexed TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    init_observation_schema(conn, dimensions)

    # Index metadata
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # =========================================================================
    # Graph Indexing Tables (Phase 8)
    # =========================================================================

    # Wikilinks: Track [[links]] between documents
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wikilinks (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            target_path TEXT,
            link_text TEXT NOT NULL,
            display_text TEXT,
            is_broken BOOLEAN DEFAULT 0,
            line_number INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_path, link_text, line_number)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wikilinks_source ON wikilinks(source_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wikilinks_target ON wikilinks(target_path)")

    # Tasks: Extract - [ ] and - [x] items
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            task_text TEXT NOT NULL,
            completed BOOLEAN DEFAULT 0,
            line_number INTEGER,
            section TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(doc_path, line_number)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_doc ON tasks(doc_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed)")

    # Tags: Frontmatter tags array
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_tags (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            tag TEXT NOT NULL,
            UNIQUE(doc_path, tag)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag)")

    # Aliases: Frontmatter aliases for link resolution
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_aliases (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            alias TEXT NOT NULL,
            UNIQUE(doc_path, alias)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_aliases_alias ON doc_aliases(alias)")

    # Sections: Document structure (headings)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            heading TEXT NOT NULL,
            level INTEGER NOT NULL,
            line_number INTEGER,
            UNIQUE(doc_path, line_number)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_doc ON sections(doc_path)")

    conn.commit()
    return True


# ============================================================================
# Document Indexing
# ============================================================================

def content_hash(content: str) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def document_changed(doc_path: str, content: str, conn: sqlite3.Connection) -> bool:
    """Check if document content has changed since last indexing."""
    current_hash = content_hash(content)

    stored = conn.execute(
        "SELECT content_hash FROM doc_hashes WHERE path = ?",
        (doc_path,)
    ).fetchone()

    if stored is None:
        return True  # New document

    return stored[0] != current_hash


def index_document(
    conn: sqlite3.Connection,
    file_path: Path,
    memex: Path,
    pipeline: EmbeddingPipeline | None = None
) -> dict:
    """
    Index a document with embeddings.

    Returns {"chunks": int, "embedded": int} — chunk count total vs. chunks
    that actually got a vec_chunks entry. When an embedding API call fails,
    `chunks > embedded` and the caller can surface the gap to the user.
    The gap is also detectable post-hoc via `count_embedding_gaps()`.

    TRANSACTION: this function does NOT commit. Callers own the transaction
    boundary — rebuild_full / rebuild_incremental wrap each doc in a
    SAVEPOINT so a mid-doc failure rolls the doc back without polluting
    the batch; single-file CLI callers commit at the end themselves.
    """
    content = file_path.read_text()
    rel_path = str(file_path.relative_to(memex))

    # Check if changed
    if not document_changed(rel_path, content, conn):
        return {"chunks": 0, "embedded": 0}  # No change, skip

    # Remove old chunks for this document
    old_chunk_ids = [row[0] for row in conn.execute(
        "SELECT id FROM chunks WHERE doc_path = ?", (rel_path,)
    )]

    if old_chunk_ids:
        # Safe: only interpolating '?' placeholders, actual values passed as params
        placeholders = ','.join('?' * len(old_chunk_ids))
        try:
            conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", old_chunk_ids)
        except sqlite3.OperationalError:
            pass  # vec_chunks may not exist or sqlite-vec not loaded
        conn.execute("DELETE FROM chunks WHERE doc_path = ?", (rel_path,))

    # Clear old graph metadata for this document
    conn.execute("DELETE FROM wikilinks WHERE source_path = ?", (rel_path,))
    conn.execute("DELETE FROM tasks WHERE doc_path = ?", (rel_path,))
    conn.execute("DELETE FROM sections WHERE doc_path = ?", (rel_path,))
    conn.execute("DELETE FROM doc_tags WHERE doc_path = ?", (rel_path,))
    conn.execute("DELETE FROM doc_aliases WHERE doc_path = ?", (rel_path,))

    # Detect content type and parse metadata
    content_type = get_content_type(rel_path, content)
    meta = parse_frontmatter(content)

    # Route to appropriate chunking strategy
    if content_type == "transcript":
        chunks = chunk_transcript_turns(content, meta)
    elif content_type in ("memo", "concept", "project"):
        chunks = chunk_whole_doc(content, meta)
    else:
        chunks = chunk_markdown(content)

    if not chunks:
        return {"chunks": 0, "embedded": 0}

    # Get embeddings
    embeddings = []
    if pipeline and pipeline.enabled:
        embeddings = pipeline.embed_chunks(chunks, conn)

    # Create embedding lookup
    embedding_map = {idx: emb for idx, emb in embeddings}

    # Extract date and project from frontmatter / path for chunk metadata
    doc_date = str(meta.get("date", "")) if meta else ""
    doc_project = str(meta.get("project", "")) if meta else ""
    if not doc_project and rel_path.startswith("projects/"):
        parts = rel_path.split("/")
        if len(parts) >= 2:
            doc_project = parts[1]

    # Vec-table dimension + metadata-capability (computed once per doc).
    # Align to the table's CURRENT stored dim (fallback to config when empty) so
    # an insert never mismatches a not-yet-migrated table — see vec_stored_dim.
    _idx_dim = vec_stored_dim(conn, "vec_chunks") or get_vector_dimensions()
    _vec_has_meta = table_has_metadata(conn, "vec_chunks")
    _doc_date_int = date_to_yyyymmdd(doc_date)

    # Insert chunks
    for chunk in chunks:
        cursor = conn.execute(
            """INSERT INTO chunks (doc_path, chunk_index, content, content_hash, chunk_type, start_offset, end_offset, doc_date, doc_project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rel_path, chunk.index, chunk.content, chunk.content_hash, chunk.chunk_type,
             chunk.start_offset, chunk.end_offset, doc_date, doc_project)
        )
        chunk_id = cursor.lastrowid

        # Insert embedding if available.
        # NOTE: chunks may land in `chunks` without a corresponding
        # `vec_chunks` row when the embedding API fails (expired key,
        # rate-limit exhaustion). This is by design — the chunk is still
        # keyword-searchable via FTS. The gap is surfaced post-hoc by
        # `count_embedding_gaps()` and remediated by
        # `memex index embed-missing`. Do NOT turn this into an error path.
        if chunk.index in embedding_map:
            _vec_blob = truncate_unit_vector(embedding_map[chunk.index], _idx_dim)
            if _vec_has_meta:
                conn.execute(
                    "INSERT INTO vec_chunks (rowid, embedding, doc_project, doc_type, doc_date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, _vec_blob, doc_project, content_type, _doc_date_int),
                )
            else:
                conn.execute(
                    "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                    (chunk_id, _vec_blob),
                )

    # =========================================================================
    # Extract and Index Graph Metadata
    # =========================================================================

    # Extract wikilinks
    wikilinks = extract_wikilinks(content, rel_path, memex, conn)
    if wikilinks:
        conn.executemany("""
            INSERT OR IGNORE INTO wikilinks
            (source_path, target_path, link_text, display_text, is_broken, line_number)
            VALUES (:source_path, :target_path, :link_text, :display_text, :is_broken, :line_number)
        """, wikilinks)

    # Extract tasks
    tasks = extract_tasks(content, rel_path)
    if tasks:
        conn.executemany("""
            INSERT OR IGNORE INTO tasks (doc_path, task_text, completed, line_number, section)
            VALUES (:doc_path, :task_text, :completed, :line_number, :section)
        """, tasks)

    # Extract sections
    sections = extract_sections(content, rel_path)
    if sections:
        conn.executemany("""
            INSERT OR IGNORE INTO sections (doc_path, heading, level, line_number)
            VALUES (:doc_path, :heading, :level, :line_number)
        """, sections)

    # Extract tags and aliases from frontmatter
    tags, aliases = extract_tags_and_aliases(meta, rel_path)
    if tags:
        conn.executemany("""
            INSERT OR IGNORE INTO doc_tags (doc_path, tag)
            VALUES (:doc_path, :tag)
        """, tags)
    if aliases:
        conn.executemany("""
            INSERT OR IGNORE INTO doc_aliases (doc_path, alias)
            VALUES (:doc_path, :alias)
        """, aliases)

    # Update document hash
    conn.execute(
        """INSERT OR REPLACE INTO doc_hashes (path, content_hash, last_indexed)
           VALUES (?, ?, datetime('now'))""",
        (rel_path, content_hash(content))
    )

    # NOTE: intentionally NOT committing. See docstring — the caller
    # (rebuild loop or CLI) owns the transaction boundary.
    return {"chunks": len(chunks), "embedded": len(embedding_map)}


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Embedding pipeline utilities")
    parser.add_argument("--test", action="store_true", help="Test embedding a sample text")
    parser.add_argument("--index", type=str, help="Index a specific file")
    parser.add_argument("--chunk", type=str, help="Show chunks for a file (no embedding)")

    args = parser.parse_args()

    if args.test:
        print("Testing Gemini embedding...")
        pipeline = EmbeddingPipeline()

        if not pipeline.enabled:
            print(f"Embeddings disabled. Set ${pipeline.config.get('api_key_env', 'GEMINI_API_KEY')} to enable.")
            sys.exit(1)

        test_text = "Claude Code is a powerful AI assistant for software development."
        embedding = pipeline.embed_text(test_text)

        if embedding:
            print(f"Success! Embedding dimensions: {len(embedding)}")
            print(f"First 5 values: {embedding[:5]}")
        else:
            print("Failed to generate embedding")
            sys.exit(1)

    elif args.chunk:
        file_path = Path(args.chunk)
        if not file_path.exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = file_path.read_text()
        chunks = chunk_markdown(content)

        print(f"Generated {len(chunks)} chunks:\n")
        for chunk in chunks:
            token_count = count_tokens(chunk.content)
            preview = chunk.content[:100].replace("\n", "\\n")
            fm_marker = " [FRONTMATTER]" if chunk.is_frontmatter else ""
            print(f"Chunk {chunk.index}{fm_marker}: {token_count} tokens")
            print(f"  Hash: {chunk.content_hash[:16]}...")
            print(f"  Preview: {preview}...")
            print()

    elif args.index:
        file_path = Path(args.index).resolve()
        if not file_path.exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        # Find memex root (check config first, then env var, then fallback)
        config_path = Path.home() / ".memex" / "config.json"
        memex = None
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                if "memex_path" in config:
                    memex = Path(config["memex_path"]).expanduser()
            except (json.JSONDecodeError, KeyError):
                pass
        if not memex:
            memex = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", file_path.parent.parent))
        index_path = memex / "_index.sqlite"

        # WAL + busy_timeout via shared helper — matches the rebuild-path
        # connection so a single-file index call doesn't lock out a
        # concurrent `memex backfill obs --stdin` (and vice versa).
        from memex.db_utils import connect_index

        conn = connect_index(index_path)
        try:
            if not init_embedding_schema(conn):
                print("Failed to initialize embedding schema")
                sys.exit(1)

            pipeline = EmbeddingPipeline()
            try:
                result = index_document(conn, file_path, memex, pipeline)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            chunk_count = result["chunks"]
            embedded_count = result["embedded"]
            gap = chunk_count - embedded_count
            msg = f"Indexed {chunk_count} chunks from {file_path.name}"
            if gap:
                msg += f" ({embedded_count} embedded, {gap} missing — run `memex index embed-missing` after fixing cause)"
            print(msg)
        finally:
            conn.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
