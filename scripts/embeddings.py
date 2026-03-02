#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.0.0",
#     "requests>=2.31.0",
#     "sqlite-vec>=0.1.6",
#     "tiktoken>=0.5",
# ]
# ///
# Note: LM Studio provider (primary) uses requests for API calls
# Gemini provider (fallback) uses google-genai
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

import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# Lazy imports for optional dependencies
_genai_client = None
_tokenizer = None


# ============================================================================
# Configuration
# ============================================================================

DEFAULT_EMBEDDING_CONFIG = {
    "enabled": True,
    "provider": "google",
    "model": "gemini-embedding-001",
    "dimensions": 3072,
    "api_key_env": "GEMINI_API_KEY",
}

DEFAULT_CHUNK_CONFIG = {
    "max_tokens": 400,
    "overlap_tokens": 80,
}


def get_embedding_config() -> dict:
    """Load embedding configuration."""
    config = DEFAULT_EMBEDDING_CONFIG.copy()

    # Check for config file
    config_path = Path("~/.memex/config.json").expanduser()
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text())
            if "embeddings" in full_config:
                config.update(full_config["embeddings"])
        except json.JSONDecodeError:
            pass

    return config


def get_chunk_config() -> dict:
    """Load chunking configuration."""
    config = DEFAULT_CHUNK_CONFIG.copy()

    config_path = Path("~/.memex/config.json").expanduser()
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text())
            if "search" in full_config:
                if "chunk_max_tokens" in full_config["search"]:
                    config["max_tokens"] = full_config["search"]["chunk_max_tokens"]
                if "chunk_overlap_tokens" in full_config["search"]:
                    config["overlap_tokens"] = full_config["search"]["chunk_overlap_tokens"]
        except json.JSONDecodeError:
            pass

    return config


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

    # Split body by turn headers
    parts = TURN_PATTERN.split(body)
    headers = TURN_PATTERN.findall(body)

    config = get_chunk_config()
    max_tokens = config.get("max_tokens", 400)

    # Process each turn
    for header, body_part in zip(headers, parts[1::2]):
        turn_content = header + body_part

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


# ============================================================================
# Gemini Provider
# ============================================================================

class GeminiProvider(EmbeddingProvider):
    """Gemini API embedding provider."""

    def __init__(self, config: dict):
        self._model = config.get("model", "gemini-embedding-001")
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
        """Lazy-load Gemini client."""
        if self._client is None and self._api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self._api_key)
            except ImportError:
                raise ValueError("google-genai not installed")
        return self._client

    def embed_texts(self, texts: list[str], task_type: str = "document") -> list[list[float] | None]:
        """Embed texts using Gemini API."""
        try:
            from google.genai import types

            client = self._get_client()
            gemini_task = "RETRIEVAL_QUERY" if task_type == "query" else "RETRIEVAL_DOCUMENT"

            response = client.models.embed_content(
                model=self._model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=gemini_task)
            )

            return [emb.values if emb else None for emb in response.embeddings]

        except Exception as e:
            print(f"Gemini embedding error: {e}", file=sys.stderr)
            return [None] * len(texts)


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

        results = self._provider_impl.embed_texts([text], task_type=normalized_task)
        return results[0] if results else None

    def embed_query(self, query: str) -> bytes | None:
        """
        Embed a search query.

        Uses RETRIEVAL_QUERY task type for better search results.
        Returns serialized bytes for sqlite-vec.
        """
        vector = self.embed_text(query, task_type="RETRIEVAL_QUERY")
        if vector:
            return serialize_f32(vector)
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

        # Batch embed uncached chunks
        # Provider-specific batching: Gemini has rate limits, local doesn't
        if to_embed and self._provider_impl:
            import time

            # Provider-specific batch settings
            if self.provider == "google":
                BATCH_SIZE = 100  # Gemini batch limit
                MAX_RETRIES = 4
                INTER_BATCH_DELAY = 0.5  # 500ms for rate limits
            else:  # local models
                BATCH_SIZE = 50  # Reasonable chunks for sequential processing
                MAX_RETRIES = 1  # No retries needed for local
                INTER_BATCH_DELAY = 0.0  # No rate limits

            total_batches = (len(to_embed) + BATCH_SIZE - 1) // BATCH_SIZE
            for batch_start in range(0, len(to_embed), BATCH_SIZE):
                batch = to_embed[batch_start:batch_start + BATCH_SIZE]
                batch_num = batch_start // BATCH_SIZE + 1

                for attempt in range(MAX_RETRIES):
                    try:
                        contents = [item[1] for item in batch]

                        # Delegate to provider
                        embeddings_result = self._provider_impl.embed_texts(contents, task_type="document")

                        for (idx, content, content_hash), vec in zip(batch, embeddings_result):
                            if vec is not None:
                                embedding_blob = serialize_f32(vec)

                                # Cache the embedding
                                conn.execute(
                                    """INSERT OR REPLACE INTO embedding_cache
                                       (provider, model, content_hash, embedding)
                                       VALUES (?, ?, ?, ?)""",
                                    (self.provider, self.model, content_hash, embedding_blob)
                                )

                                results.append((idx, embedding_blob))

                        conn.commit()

                        # Inter-batch delay (only for API providers)
                        if INTER_BATCH_DELAY > 0 and batch_num < total_batches:
                            time.sleep(INTER_BATCH_DELAY)
                        break  # Success, exit retry loop

                    except Exception as e:
                        error_str = str(e)
                        is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str

                        if is_rate_limit and attempt < MAX_RETRIES - 1:
                            # Exponential backoff for API rate limits
                            wait = 10 * (3 ** attempt)
                            print(f"Rate limited on batch {batch_num}, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...", file=sys.stderr)
                            time.sleep(wait)
                        else:
                            print(f"Batch {batch_num} failed: {e}", file=sys.stderr)
                            break  # Give up on this batch

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
    dimensions = config.get("dimensions", 3072)

    # Validate dimensions to prevent SQL injection from config
    try:
        dimensions = int(dimensions)
        if not 1 <= dimensions <= 10000:
            raise ValueError("out of range")
    except (TypeError, ValueError):
        print(f"Invalid embedding dimensions: {dimensions}, using default 3072", file=sys.stderr)
        dimensions = 3072

    # Check for dimension migration (3072→4096 when switching providers)
    try:
        row = conn.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
        if row:
            existing_dims = len(row[0]) // 4  # 4 bytes per float32
            if existing_dims != dimensions:
                print(f"Dimension migration detected: {existing_dims}d → {dimensions}d", file=sys.stderr)
                print(f"Dropping vec_chunks table and clearing chunks...", file=sys.stderr)
                conn.execute("DROP TABLE IF EXISTS vec_chunks")
                conn.execute("DELETE FROM chunks")
                conn.commit()
                print(f"Run full rebuild to re-embed with new model", file=sys.stderr)
    except sqlite3.OperationalError:
        pass  # vec_chunks doesn't exist yet

    # Vector embeddings table (sqlite-vec virtual table)
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
        USING vec0(embedding float[{dimensions}])
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(doc_path, chunk_index)
        )
    """)

    # Add chunk_type column if it doesn't exist (migration for existing DBs)
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN chunk_type TEXT NOT NULL DEFAULT 'content'")
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
) -> int:
    """
    Index a document with embeddings.

    Returns number of chunks indexed.
    """
    content = file_path.read_text()
    rel_path = str(file_path.relative_to(memex))

    # Check if changed
    if not document_changed(rel_path, content, conn):
        return 0  # No change, skip

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
        return 0

    # Get embeddings
    embeddings = []
    if pipeline and pipeline.enabled:
        embeddings = pipeline.embed_chunks(chunks, conn)

    # Create embedding lookup
    embedding_map = {idx: emb for idx, emb in embeddings}

    # Insert chunks
    for chunk in chunks:
        cursor = conn.execute(
            """INSERT INTO chunks (doc_path, chunk_index, content, content_hash, chunk_type, start_offset, end_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rel_path, chunk.index, chunk.content, chunk.content_hash, chunk.chunk_type,
             chunk.start_offset, chunk.end_offset)
        )
        chunk_id = cursor.lastrowid

        # Insert embedding if available
        if chunk.index in embedding_map:
            conn.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (chunk_id, embedding_map[chunk.index])
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

    conn.commit()
    return len(chunks)


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

        conn = sqlite3.connect(index_path)
        try:
            if not init_embedding_schema(conn):
                print("Failed to initialize embedding schema")
                sys.exit(1)

            pipeline = EmbeddingPipeline()
            count = index_document(conn, file_path, memex, pipeline)

            print(f"Indexed {count} chunks from {file_path.name}")
        finally:
            conn.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
