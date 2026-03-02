# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Claude Memory Plugin - Transcript to Markdown Converter

Parses Claude Code JSONL transcripts and converts to readable markdown
with proper filtering, turn-based structure, and clean formatting.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Import from utils (when used as module)
try:
    from .utils import (
        log_warning, log_error, log_info,
        format_frontmatter, sanitize_project_name
    )
except ImportError:
    try:
        from utils import (
            log_warning, log_error, log_info,
            format_frontmatter, sanitize_project_name
        )
    except ImportError:
        # Standalone mode - define minimal versions
        def log_warning(msg): print(f"WARNING: {msg}", file=__import__('sys').stderr)
        def log_error(msg): print(f"ERROR: {msg}", file=__import__('sys').stderr)
        def log_info(msg): print(f"INFO: {msg}", file=__import__('sys').stderr)

    def format_frontmatter(data: dict) -> str:
        lines = ["---"]
        for key, value in data.items():
            if isinstance(value, list):
                lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
            elif isinstance(value, bool):
                lines.append(f"{key}: {str(value).lower()}")
            else:
                lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    def sanitize_project_name(name: str) -> str:
        name = re.sub(r'[^\w\-]', '_', name)
        name = re.sub(r'_+', '_', name).strip('_')[:50]
        return name or '_uncategorized'


# ============================================================================
# Configuration
# ============================================================================

# Message types to skip entirely
SKIP_MESSAGE_TYPES = {
    "file-history-snapshot",
    "progress",
    "summary",
    "queue-operation",
    "system",
}

# Maximum lengths for various content types
MAX_TOOL_RESULT_LENGTH = 1500
MAX_THINKING_LENGTH = 3000
MAX_USER_MESSAGE_LENGTH = 5000

# Tool result patterns to collapse (regex -> replacement)
COLLAPSE_PATTERNS = [
    # Long file listings
    (r'(total \d+\n(?:.*\n){20,})', lambda m: f"[...{len(m.group(1).splitlines())} lines of file listing...]"),
    # Long JSON arrays — use atomic-style matching to avoid catastrophic backtracking
    # Match [ ... ] blocks containing 10+ lines that look like JSON objects
    (r'(\[(?:\n\s*\{[^\n]*\n){10,}[^\]]*\])', "[...large JSON array...]"),
    # Repeated similar lines
    (r'((?:^.{50,}$\n){10,})', lambda m: f"[...{len(m.group(1).splitlines())} similar lines...]"),
]

# System tags to strip from user messages (Claude Code internal scaffolding)
SYSTEM_TAG_PATTERNS = [
    # Local command noise
    re.compile(r'<local-command-caveat>.*?</local-command-caveat>', re.DOTALL),
    re.compile(r'<local-command-stdout>.*?</local-command-stdout>', re.DOTALL),
    re.compile(r'<command-name>.*?</command-name>', re.DOTALL),
    re.compile(r'<command-message>.*?</command-message>', re.DOTALL),
    re.compile(r'<command-args>.*?</command-args>', re.DOTALL),
    # System reminders (hook injections, plugin context)
    re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL),
]

# Skill expansion detection — when a user "message" is actually a skill prompt injection
# Use \A anchor (start of string) to avoid false positives on headings mid-message
# Two tiers: exact match for known memex skills, generic match with secondary confirmation
SKILL_EXPANSION_EXACT_RE = re.compile(
    r'\A#\s+(?:Save Memo|Search|Synthesize|Load|Status|Open|Retry)\s+Command\b'
)
# Generic: heading ending with Command/Workflow/Skill + secondary markers near the top
# Excludes "Guide" (too common in user content like "# Deployment Guide")
SKILL_EXPANSION_GENERIC_RE = re.compile(
    r'\A#\s+.+\s(?:Command|Workflow|Skill)\s*$'
)
SKILL_SECONDARY_MARKER_RE = re.compile(
    r'^(?:##\s+Instructions|ARGUMENTS:|##\s+Path Resolution|##\s+When to)',
    re.MULTILINE
)
SKILL_ARGS_RE = re.compile(r'^ARGUMENTS:\s*(.+)$', re.MULTILINE)

# Git commit detection in Bash tool results (from simonw/claude-code-transcripts)
GIT_COMMIT_RE = re.compile(r'\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)')


# ============================================================================
# JSONL Parsing
# ============================================================================

def parse_transcript_jsonl(path: Path) -> tuple[list[dict], int]:
    """
    Parse JSONL transcript file.

    Returns:
        Tuple of (messages list, error count)
    """
    messages = []
    errors = 0

    if not path.exists():
        log_error(f"Transcript file not found: {path}")
        return [], 0

    # Stream file to handle large transcripts without memory issues
    with open(path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError as e:
                log_warning(f"Line {i} parse error: {e}")
                errors += 1

    return messages, errors


def extract_session_id(path: Path) -> str:
    """Extract session ID from transcript path."""
    return path.stem


def _normalize_timestamp(value) -> str | None:
    """Normalize timestamp from ISO string or epoch milliseconds.

    Claude Code logs timestamps as ISO strings, but some edge cases
    (older sessions, restored backups) may use epoch ms.
    Adapted from peteromallet/dataclaw parser.py.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).isoformat()
    return None


def extract_session_metadata(messages: list[dict]) -> dict:
    """Extract session-level metadata from raw message stream.

    Single pass: collects timestamps, models, message counts, token usage.
    Called before filtering/dedup so counts reflect raw input.
    """
    if not messages:
        return {}

    timestamps = []
    models = set()
    agent_ids = set()
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0

    for msg in messages:
        if ts := _normalize_timestamp(msg.get("timestamp")):
            timestamps.append(ts)

        if msg.get("type") == "assistant":
            msg_inner = msg.get("message", {})
            if isinstance(msg_inner, dict):
                if model := msg_inner.get("model"):
                    models.add(model)
                # Token usage tracking (adapted from dataclaw)
                usage = msg_inner.get("usage", {})
                if isinstance(usage, dict):
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)
                    cache_read_tokens += usage.get("cache_read_input_tokens", 0)

        if agent_id := msg.get("agentId"):
            agent_ids.add(agent_id)

    metadata = {"total_messages": len(messages)}

    if timestamps:
        try:
            parsed = sorted(
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                for ts in timestamps
                if ts
            )
            if len(parsed) >= 2:
                metadata["start_time"] = parsed[0].strftime("%Y-%m-%dT%H:%M:%S")
                metadata["end_time"] = parsed[-1].strftime("%Y-%m-%dT%H:%M:%S")
                metadata["duration_minutes"] = max(
                    1, int((parsed[-1] - parsed[0]).total_seconds() / 60)
                )
        except (ValueError, TypeError):
            pass

    if models:
        metadata["models"] = sorted(models)

    if agent_ids:
        metadata["subagents"] = len(agent_ids)

    # Token usage — only include if we found any
    if input_tokens or output_tokens:
        metadata["input_tokens"] = input_tokens + cache_read_tokens
        metadata["output_tokens"] = output_tokens
        metadata["cache_read_tokens"] = cache_read_tokens

    return metadata


def deduplicate_messages(messages: list[dict]) -> list[dict]:
    """Remove duplicate messages caused by version stutter.

    Claude Code occasionally logs the same message twice (upgrade artifacts).
    Dedup key: (type, timestamp, content_key).
    Adapted from daaain/claude-code-log converter.py.
    """
    seen: dict[tuple, int] = {}
    deduped: list[dict] = []

    for msg in messages:
        msg_type = msg.get("type", "")
        timestamp = msg.get("timestamp", "")
        if not timestamp:
            deduped.append(msg)
            continue

        # Compute content_key to distinguish concurrent messages
        content_key = None
        is_user_text = False

        if msg_type == "assistant":
            msg_inner = msg.get("message", {})
            if isinstance(msg_inner, dict):
                content_key = msg_inner.get("id", "")
        elif msg_type == "user":
            content = msg.get("message", msg.get("content", ""))
            if isinstance(content, dict):
                content = content.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        content_key = item.get("tool_use_id", "")
                        break
                else:
                    is_user_text = True

        dedup_key = (msg_type, timestamp, content_key)

        if dedup_key in seen:
            # For user text, keep version with more content items
            if is_user_text:
                idx = seen[dedup_key]
                existing_content = deduped[idx].get("message", deduped[idx].get("content", ""))
                new_content = msg.get("message", msg.get("content", ""))
                if isinstance(new_content, (list, dict)) and not isinstance(existing_content, (list, dict)):
                    deduped[idx] = msg
                elif isinstance(new_content, list) and isinstance(existing_content, list) and len(new_content) > len(existing_content):
                    deduped[idx] = msg
        else:
            seen[dedup_key] = len(deduped)
            deduped.append(msg)

    dup_count = len(messages) - len(deduped)
    if dup_count > 0:
        log_info(f"Deduplicated {dup_count} messages")

    return deduped


def clean_system_tags(text: str) -> str:
    """Strip Claude Code system tags from message content."""
    for pattern in SYSTEM_TAG_PATTERNS:
        text = pattern.sub('', text)
    # Collapse multiple blank lines left by tag removal
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def compress_skill_expansion(text: str, force: bool = False) -> str:
    """Detect and compress skill prompt injections to one-liner.

    When Claude invokes a skill, the full skill prompt (50+ lines)
    gets captured as a user message. Compress to a readable summary.

    Detection (any of):
    - force=True (isMeta flag from JSONL — reliable signal)
    - Exact match for known memex skills (Save Memo, Search, etc.)
    - Generic heading + secondary marker (## Instructions, ARGUMENTS:) for other plugins
    """
    is_skill = force
    if not is_skill:
        is_skill = bool(SKILL_EXPANSION_EXACT_RE.search(text))
    if not is_skill:
        # Generic detection requires both heading match AND secondary marker
        # Secondary marker must appear within first 500 chars (skill prompts are structured)
        if SKILL_EXPANSION_GENERIC_RE.search(text) and SKILL_SECONDARY_MARKER_RE.search(text[:500]):
            is_skill = True
    if not is_skill:
        return text

    # Extract skill name from the heading
    heading_match = re.search(r'\A#\s+(.+?)(?:\s+Command|\s+Workflow|\s+Skill)?\s*$', text, re.MULTILINE)
    skill_name = heading_match.group(1).strip() if heading_match else "Unknown"

    # Extract arguments if present
    args_match = SKILL_ARGS_RE.search(text)
    args = args_match.group(1).strip() if args_match else ""

    if args:
        return f"[Skill invoked: {skill_name} — {args}]"
    return f"[Skill invoked: {skill_name}]"


# ============================================================================
# Content Extraction (Clean)
# ============================================================================

def extract_thinking(content_item: dict) -> str | None:
    """Extract thinking content, removing signature."""
    if content_item.get("type") != "thinking":
        return None

    thinking = content_item.get("thinking", "")
    # Signature is noise - don't include it
    return thinking if thinking else None


def extract_text(content_item: dict) -> str | None:
    """Extract text content."""
    if content_item.get("type") != "text":
        return None
    return content_item.get("text", "")


def extract_tool_use(content_item: dict) -> dict | None:
    """Extract tool use details."""
    if content_item.get("type") != "tool_use":
        return None

    return {
        "id": content_item.get("id", ""),
        "name": content_item.get("name", "unknown"),
        "input": content_item.get("input", {}),
    }


def extract_tool_result(content_item: dict) -> dict | None:
    """Extract tool result details."""
    if content_item.get("type") != "tool_result":
        return None

    content = content_item.get("content", "")
    if isinstance(content, list):
        # Handle complex content (e.g., images)
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)

    # Strip system tags from tool results (Claude Code injects <system-reminder> etc.)
    content = clean_system_tags(str(content))

    return {
        "tool_use_id": content_item.get("tool_use_id", ""),
        "content": content,
        "is_error": content_item.get("is_error", False),
    }


def smart_truncate(text: str, max_length: int, context: str = "") -> str:
    """Truncate text intelligently, preserving useful parts."""
    if len(text) <= max_length:
        return text

    # For tool results, try to keep beginning and end
    if context == "tool_result":
        keep_start = int(max_length * 0.7)
        keep_end = int(max_length * 0.25)
        truncated = len(text) - keep_start - keep_end
        return f"{text[:keep_start]}\n\n[...truncated {truncated} chars...]\n\n{text[-keep_end:]}"

    # Default: just truncate with note
    return f"{text[:max_length]}\n\n[...truncated...]"


def collapse_verbose_output(text: str) -> str:
    """Collapse repetitive or verbose patterns in output."""
    for pattern, replacement in COLLAPSE_PATTERNS:
        if callable(replacement):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
    return text


# ============================================================================
# Message Processing
# ============================================================================

def get_message_role(msg: dict) -> str:
    """Determine message role from various formats."""
    if "role" in msg:
        return msg["role"]

    if "type" in msg:
        type_map = {
            "human": "user",
            "assistant": "assistant",
            "system": "system",
            "user": "user",
        }
        return type_map.get(msg["type"], msg["type"])

    return "unknown"


def should_skip_message(msg: dict) -> bool:
    """Check if message should be skipped entirely."""
    msg_type = msg.get("type", "")

    # Skip known noise types
    if msg_type in SKIP_MESSAGE_TYPES:
        return True

    # Skip empty messages
    content = msg.get("content", msg.get("message", ""))
    if not content:
        return True

    return False


def parse_message_content(msg: dict) -> dict:
    """
    Parse message into structured components.

    Returns dict with keys: thinking, text, tool_uses, tool_results
    """
    is_meta = msg.get("isMeta", False)
    result = {
        "thinking": [],
        "text": [],
        "tool_uses": [],
        "tool_results": [],
        "tool_use_result_meta": msg.get("toolUseResult"),
        "role": get_message_role(msg),
        "is_meta": is_meta,
    }

    # Handle different content formats
    content = msg.get("content", msg.get("message", ""))

    # If content is in nested message dict
    if isinstance(content, dict) and "content" in content:
        content = content["content"]

    # String content (simple user message)
    if isinstance(content, str):
        # Clean system tags and compress skill expansions for user messages
        if result["role"] in ("user", "human"):
            content = clean_system_tags(content)
            content = compress_skill_expansion(content, force=is_meta)
        if content.strip():
            result["text"].append(content)
        return result

    # Dict content (legacy format)
    is_user = result["role"] in ("user", "human")
    if isinstance(content, dict):
        if "role" in content and "content" in content:
            # Nested message format
            inner = content["content"]
            if isinstance(inner, str):
                if is_user:
                    inner = clean_system_tags(inner)
                    inner = compress_skill_expansion(inner, force=is_meta)
                if inner.strip():
                    result["text"].append(inner)
                return result
            elif isinstance(inner, list):
                content = inner  # Fall through to list processing below
        elif content.get("type") == "text":
            text_val = content.get("text", "")
            if is_user:
                text_val = clean_system_tags(text_val)
                text_val = compress_skill_expansion(text_val, force=is_meta)
            if text_val.strip():
                result["text"].append(text_val)
            return result

    # List content (standard format)
    if isinstance(content, list):
        raw_texts = []  # Collect user texts for post-pass cleaning (handles split tags)
        for item in content:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    if is_user:
                        raw_texts.append(item)
                    elif item.strip():
                        result["text"].append(item)
                continue

            # Extract by type
            if thinking := extract_thinking(item):
                result["thinking"].append(thinking)
            elif text := extract_text(item):
                if is_user:
                    raw_texts.append(text)
                elif text.strip():
                    result["text"].append(text)
            elif tool_use := extract_tool_use(item):
                result["tool_uses"].append(tool_use)
            elif tool_result := extract_tool_result(item):
                # Attach toolUseResult metadata if available
                if result["tool_use_result_meta"]:
                    tool_result["metadata"] = result["tool_use_result_meta"]
                result["tool_results"].append(tool_result)

        # Post-pass: join user texts, clean tags that may span blocks, then compress
        if is_user and raw_texts:
            joined = "\n".join(raw_texts)
            joined = clean_system_tags(joined)
            joined = compress_skill_expansion(joined, force=is_meta)
            if joined.strip():
                result["text"].append(joined)

    return result


# ============================================================================
# Turn Building
# ============================================================================

class Turn:
    """Represents a conversation turn (user request + assistant response)."""

    def __init__(self, number: int):
        self.number = number
        self.user_content: list[str] = []
        self.thinking: list[str] = []
        self.responses: list[str] = []
        self.tool_calls: list[dict] = []  # {name, input, result, is_error}

    def add_user_message(self, text: str):
        if text and text.strip():
            self.user_content.append(text.strip())

    def add_thinking(self, text: str):
        if text and text.strip():
            self.thinking.append(text.strip())

    def add_response(self, text: str):
        if text and text.strip():
            self.responses.append(text.strip())

    def add_tool_use(self, tool_use: dict):
        self.tool_calls.append({
            "id": tool_use["id"],
            "name": tool_use["name"],
            "input": tool_use["input"],
            "result": None,
            "is_error": False,
        })

    def add_tool_result(self, tool_result: dict):
        # Match result to tool use by ID
        metadata = tool_result.get("metadata", {})
        for tc in self.tool_calls:
            if tc["id"] == tool_result["tool_use_id"]:
                tc["result"] = tool_result["content"]
                tc["is_error"] = tool_result["is_error"]
                if metadata:
                    tc["metadata"] = metadata
                return

        # Orphan result - add as standalone
        self.tool_calls.append({
            "id": tool_result["tool_use_id"],
            "name": "unknown",
            "input": {},
            "result": tool_result["content"],
            "is_error": tool_result["is_error"],
            **({"metadata": metadata} if metadata else {}),
        })

    def is_empty(self) -> bool:
        return not (self.user_content or self.thinking or
                   self.responses or self.tool_calls)

    def to_markdown(self) -> str:
        """Convert turn to markdown."""
        parts = []

        # Turn header
        parts.append(f"## Turn {self.number}")
        parts.append("")

        # User message
        if self.user_content:
            parts.append("### User")
            parts.append("")
            for text in self.user_content:
                truncated = smart_truncate(text, MAX_USER_MESSAGE_LENGTH)
                parts.append(truncated)
                parts.append("")

        # Assistant section
        has_assistant_content = self.thinking or self.responses or self.tool_calls
        if has_assistant_content:
            parts.append("### Assistant")
            parts.append("")

            # Thinking (collapsible in some markdown renderers)
            if self.thinking:
                parts.append("<details>")
                parts.append("<summary>Thinking</summary>")
                parts.append("")
                for thinking in self.thinking:
                    truncated = smart_truncate(thinking, MAX_THINKING_LENGTH)
                    parts.append(truncated)
                    parts.append("")
                parts.append("</details>")
                parts.append("")

            # Text responses
            for response in self.responses:
                parts.append(response)
                parts.append("")

            # Tool calls
            for tc in self.tool_calls:
                parts.append(self._format_tool_call(tc))
                parts.append("")

        return "\n".join(parts)

    def _format_tool_call(self, tc: dict) -> str:
        """Format a single tool call with input and result."""
        lines = []
        name = tc["name"]
        inp = tc["input"]
        result = tc["result"]
        is_error = tc["is_error"]
        metadata = tc.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # Tool header — include duration from toolUseResult if available
        duration_ms = metadata.get("durationMs") or metadata.get("totalDurationMs")
        if duration_ms and isinstance(duration_ms, (int, float)):
            duration_sec = duration_ms / 1000
            if duration_sec >= 60:
                lines.append(f"#### Tool: {name} ({duration_sec / 60:.1f}m)")
            else:
                lines.append(f"#### Tool: {name} ({duration_sec:.1f}s)")
        else:
            lines.append(f"#### Tool: {name}")
        lines.append("")

        # Format input based on tool type
        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            if desc:
                lines.append(f"*{desc}*")
                lines.append("")
            lines.append("```bash")
            lines.append(cmd)
            lines.append("```")

        elif name == "Read":
            path = inp.get("file_path", "")
            lines.append(f"Reading: `{path}`")

        elif name == "Write":
            path = inp.get("file_path", "")
            content = inp.get("content", "")
            lines.append(f"Writing to: `{path}`")
            if content:
                preview = content[:500] + "..." if len(content) > 500 else content
                lines.append("")
                lines.append("```")
                lines.append(preview)
                lines.append("```")

        elif name == "Edit":
            path = inp.get("file_path", "")
            old = inp.get("old_string", "")[:200]
            new = inp.get("new_string", "")[:200]
            lines.append(f"Editing: `{path}`")
            lines.append("")
            lines.append("```diff")
            lines.append(f"- {old}{'...' if len(inp.get('old_string', '')) > 200 else ''}")
            lines.append(f"+ {new}{'...' if len(inp.get('new_string', '')) > 200 else ''}")
            lines.append("```")

        elif name == "Grep":
            pattern = inp.get("pattern", "")
            path = inp.get("path", ".")
            lines.append(f"Searching for `{pattern}` in `{path}`")

        elif name == "Glob":
            pattern = inp.get("pattern", "")
            path = inp.get("path", ".")
            lines.append(f"Finding files: `{pattern}` in `{path}`")

        elif name == "Task":
            desc = inp.get("description", "")
            agent = inp.get("subagent_type", "")
            lines.append(f"Launching {agent} agent: {desc}")

        else:
            # Generic tool formatting
            if inp:
                lines.append("```json")
                lines.append(json.dumps(inp, indent=2)[:500])
                lines.append("```")

        # Result
        if result is not None:
            lines.append("")
            if is_error:
                lines.append("**Error:**")
            else:
                lines.append("**Result:**")
            lines.append("")

            # Clean and truncate result
            result_text = collapse_verbose_output(str(result))
            result_text = smart_truncate(result_text, MAX_TOOL_RESULT_LENGTH, "tool_result")

            lines.append("```")
            lines.append(result_text)
            lines.append("```")

        return "\n".join(lines)


# Compaction boundary detection (from simonw/claude-code-transcripts)
_COMPACT_SUMMARY_PREFIX = "This session is being continued from a previous conversation"


def build_turns(messages: list[dict]) -> list[Turn]:
    """
    Build conversation turns from messages.

    Groups messages into turns where each turn starts with a user message.
    Detects compaction boundaries (isCompactSummary) and subagent switches.
    """
    turns = []
    current_turn = None
    pending_tool_uses = {}  # id -> tool_use dict
    current_agent = None  # Track subagent switches

    for msg in messages:
        if should_skip_message(msg):
            continue

        # Detect compaction boundary (isCompactSummary flag or content prefix)
        is_compact = msg.get("isCompactSummary", False)
        if not is_compact:
            raw = msg.get("message", msg.get("content", ""))
            if isinstance(raw, str) and raw.startswith(_COMPACT_SUMMARY_PREFIX):
                is_compact = True
            elif isinstance(raw, dict):
                inner = raw.get("content", "")
                if isinstance(inner, str) and inner.startswith(_COMPACT_SUMMARY_PREFIX):
                    is_compact = True

        if is_compact:
            if current_turn and not current_turn.is_empty():
                turns.append(current_turn)
            boundary = Turn(len(turns) + 1)
            boundary.add_user_message("[Session compacted — continuing from previous conversation]")
            turns.append(boundary)
            current_turn = None
            continue

        # Detect subagent switch
        agent_id = msg.get("agentId")
        if agent_id and agent_id != current_agent:
            current_agent = agent_id
            if current_turn and not current_turn.is_empty():
                turns.append(current_turn)
            marker = Turn(len(turns) + 1)
            marker.add_user_message(f"[Subagent: {agent_id[:7]}]")
            turns.append(marker)
            current_turn = None

        parsed = parse_message_content(msg)
        role = parsed["role"]

        # User message starts a new turn
        if role in ("user", "human"):
            # Check if this is just tool results
            if parsed["tool_results"] and not parsed["text"]:
                # Tool results belong to current turn
                if current_turn:
                    for tr in parsed["tool_results"]:
                        current_turn.add_tool_result(tr)
                continue

            # New turn with actual user content
            if current_turn and not current_turn.is_empty():
                turns.append(current_turn)

            current_turn = Turn(len(turns) + 1)
            for text in parsed["text"]:
                current_turn.add_user_message(text)

        elif role == "assistant":
            if current_turn is None:
                current_turn = Turn(1)

            # Add thinking
            for thinking in parsed["thinking"]:
                current_turn.add_thinking(thinking)

            # Add text responses
            for text in parsed["text"]:
                current_turn.add_response(text)

            # Add tool uses
            for tool_use in parsed["tool_uses"]:
                current_turn.add_tool_use(tool_use)
                pending_tool_uses[tool_use["id"]] = tool_use

    # Don't forget the last turn
    if current_turn and not current_turn.is_empty():
        turns.append(current_turn)

    return turns


# ============================================================================
# Statistics
# ============================================================================

def calculate_stats(turns: list[Turn]) -> dict:
    """Calculate transcript statistics from turns."""
    stats = {
        "total_turns": len(turns),
        "user_messages": sum(len(t.user_content) for t in turns),
        "assistant_responses": sum(len(t.responses) for t in turns),
        "tool_uses": sum(len(t.tool_calls) for t in turns),
        "thinking_blocks": sum(len(t.thinking) for t in turns),
        "errors": sum(1 for t in turns for tc in t.tool_calls if tc["is_error"]),
    }

    # Tool usage breakdown + git commit detection
    tool_counts = {}
    commits = []
    for turn in turns:
        for tc in turn.tool_calls:
            name = tc["name"]
            tool_counts[name] = tool_counts.get(name, 0) + 1

            # Detect git commits in Bash results
            if name == "Bash" and tc.get("result") and not tc["is_error"]:
                for match in GIT_COMMIT_RE.finditer(str(tc["result"])):
                    commits.append(match.group(1))

    stats["tool_breakdown"] = tool_counts

    # Deduplicate commits
    if commits:
        seen = set()
        unique = []
        for c in commits:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        stats["commits"] = unique

    return stats


# ============================================================================
# Markdown Conversion
# ============================================================================

def convert_to_markdown(
    jsonl_path: Path,
    session_id: str | None = None,
    project: str | None = None,
    include_stats: bool = True,
    has_memo: bool = False,
) -> tuple[str, dict]:
    """
    Convert JSONL transcript to markdown.

    Returns:
        Tuple of (markdown content, metadata dict)
    """
    messages, error_count = parse_transcript_jsonl(jsonl_path)

    if not messages:
        return "", {"error": "No messages found", "parse_errors": error_count}

    # Extract session metadata before filtering
    session_meta = extract_session_metadata(messages)

    # Deduplicate version stutter
    messages = deduplicate_messages(messages)

    # Extract session ID if not provided
    if not session_id:
        session_id = extract_session_id(jsonl_path)

    # Build turns
    turns = build_turns(messages)

    if not turns:
        return "", {"error": "No conversation turns found", "parse_errors": error_count}

    # Calculate stats
    stats = calculate_stats(turns)

    # Build frontmatter — merge session metadata + stats
    frontmatter_data = {
        "type": "transcript",
        "session_id": session_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat(),
        "turns": stats["total_turns"],
        "tool_uses": stats["tool_uses"],
        "has_memo": has_memo,
    }

    # Add session metadata (timing, models, subagents, tokens)
    for key in ("start_time", "end_time", "duration_minutes", "models",
                "total_messages", "subagents",
                "input_tokens", "output_tokens", "cache_read_tokens"):
        if key in session_meta:
            frontmatter_data[key] = session_meta[key]

    # Add commits if detected
    if stats.get("commits"):
        frontmatter_data["commits"] = stats["commits"]

    if project:
        frontmatter_data["project"] = project

    if error_count > 0:
        frontmatter_data["parse_errors"] = error_count

    # Build markdown content
    parts = [format_frontmatter(frontmatter_data), ""]

    # Title
    parts.append(f"# Session: {session_id[:8]}...")
    parts.append("")

    # Stats section
    if include_stats:
        parts.append("## Summary")
        parts.append("")
        parts.append(f"- **Turns**: {stats['total_turns']}")
        parts.append(f"- **Tool uses**: {stats['tool_uses']}")
        if stats["errors"] > 0:
            parts.append(f"- **Errors**: {stats['errors']}")
        parts.append(f"- **Date**: {frontmatter_data['date']}")

        # Tool breakdown
        if stats["tool_breakdown"]:
            breakdown = ", ".join(f"{k}: {v}" for k, v in
                                 sorted(stats["tool_breakdown"].items(),
                                       key=lambda x: -x[1])[:5])
            parts.append(f"- **Top tools**: {breakdown}")

        # Token usage
        if session_meta.get("input_tokens"):
            total_tokens = session_meta["input_tokens"] + session_meta.get("output_tokens", 0)
            cache_pct = ""
            if session_meta.get("cache_read_tokens") and session_meta["input_tokens"] > 0:
                pct = session_meta["cache_read_tokens"] / session_meta["input_tokens"] * 100
                cache_pct = f" ({pct:.0f}% cached)"
            parts.append(f"- **Tokens**: {total_tokens:,} total ({session_meta['input_tokens']:,} in, {session_meta.get('output_tokens', 0):,} out){cache_pct}")

        if error_count > 0:
            parts.append(f"- **Parse errors**: {error_count}")
        parts.append("")

    # Conversation turns
    parts.append("---")
    parts.append("")

    for turn in turns:
        parts.append(turn.to_markdown())
        parts.append("---")
        parts.append("")

    markdown = "\n".join(parts)

    metadata = {
        **frontmatter_data,
        **stats,
        "parse_errors": error_count,
    }

    return markdown, metadata


# ============================================================================
# For Memo Generation (Curated)
# ============================================================================

def extract_for_memo(
    jsonl_path: Path,
    max_chars: int = 50000,
) -> str:
    """
    Extract curated content for memo generation.

    Returns a condensed, focused view of the transcript optimized
    for LLM summarization.
    """
    messages, _ = parse_transcript_jsonl(jsonl_path)
    turns = build_turns(messages)

    parts = []
    total_chars = 0

    for turn in turns:
        turn_parts = []

        # User message (always include)
        if turn.user_content:
            user_text = "\n".join(turn.user_content)
            # Truncate long user messages more aggressively for memo
            if len(user_text) > 1000:
                user_text = user_text[:1000] + "..."
            turn_parts.append(f"**User:** {user_text}")

        # Thinking (include but truncate)
        if turn.thinking:
            thinking = turn.thinking[0]  # Usually just need first
            if len(thinking) > 500:
                thinking = thinking[:500] + "..."
            turn_parts.append(f"**Thinking:** {thinking}")

        # Response (always include)
        if turn.responses:
            response = "\n".join(turn.responses)
            if len(response) > 1500:
                response = response[:1500] + "..."
            turn_parts.append(f"**Response:** {response}")

        # Tool summary (condensed)
        if turn.tool_calls:
            tool_summary = []
            for tc in turn.tool_calls:
                name = tc["name"]
                if name == "Bash":
                    cmd = tc["input"].get("command", "")[:100]
                    tool_summary.append(f"Bash: `{cmd}`")
                elif name == "Read":
                    path = tc["input"].get("file_path", "")
                    tool_summary.append(f"Read: {path}")
                elif name in ("Write", "Edit"):
                    path = tc["input"].get("file_path", "")
                    tool_summary.append(f"{name}: {path}")
                else:
                    tool_summary.append(f"{name}")

                # Note errors
                if tc["is_error"]:
                    tool_summary[-1] += " (ERROR)"

            turn_parts.append(f"**Tools:** {', '.join(tool_summary)}")

        turn_text = "\n".join(turn_parts)

        # Check if we'd exceed limit
        if total_chars + len(turn_text) > max_chars:
            parts.append(f"\n[...{len(turns) - turn.number + 1} more turns truncated...]")
            break

        parts.append(turn_text)
        parts.append("")
        total_chars += len(turn_text)

    return "\n".join(parts)


# ============================================================================
# Main Entry Point
# ============================================================================

def convert_transcript_file(
    jsonl_path: Path | str,
    output_path: Path | str | None = None,
    session_id: str | None = None,
    project: str | None = None,
    has_memo: bool = False,
) -> Path | None:
    """
    Convert a JSONL transcript file to markdown.
    """
    jsonl_path = Path(jsonl_path)

    if not jsonl_path.exists():
        log_error(f"Input file not found: {jsonl_path}")
        return None

    # Default output path
    if output_path is None:
        output_path = jsonl_path.with_suffix('.md')
    else:
        output_path = Path(output_path)

    # Convert
    markdown, metadata = convert_to_markdown(
        jsonl_path,
        session_id=session_id,
        project=project,
        has_memo=has_memo,
    )

    if not markdown:
        log_error(f"Failed to convert transcript: {metadata.get('error', 'unknown error')}")
        return None

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown)

    log_info(f"Converted transcript to {output_path}")
    return output_path


# ============================================================================
# CLI Interface
# ============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: transcript_to_md.py <input.jsonl> [output.md] [--project=name]")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = None
    project = None

    for arg in sys.argv[2:]:
        if arg.startswith("--project="):
            project = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            output_path = Path(arg)

    result = convert_transcript_file(input_path, output_path, project=project)

    if result:
        print(f"Created: {result}")
        sys.exit(0)
    else:
        sys.exit(1)
