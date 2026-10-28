"""Codex Desktop rollouts -> ``NormalizedSession`` (v0.22.0, plan D2).

Codex Desktop writes one JSONL rollout per thread segment under
``$CODEX_HOME/sessions/YYYY/MM/DD/`` (``CODEX_HOME`` defaults to ``~/.codex``).
Codex later compresses older rollouts to ``rollout-*.jsonl.zst`` and deletes the
plain file; both are read (Python 3.14 ``compression.zstd``, else the ``zstd``
CLI). Record shapes this adapter relies on (learned from real rollouts, 2026-10-28):

- line 1 is ``{"type": "session_meta", "payload": {...}}`` with the FULL thread
  ``id``, ``originator`` (``"Codex Desktop"``), ``source`` (``"vscode"``,
  ``"exec"``, or ``{"subagent": {"thread_spawn": {"parent_thread_id": ...}}}``),
  ``thread_source`` (``user``, ``automation``, ``subagent``,
  ``agent_created_thread``, ...) and ``cwd``;
- one thread resumes across several files that share that id; the resume files
  seen so far reference earlier history (``history_base``) instead of replaying
  it, but replayed records are de-duplicated anyway (message id, then
  timestamp + role + text);
- ``event_msg/task_started`` opens a turn; ``turn_attribution.turn_trigger``
  says who started it (``composer``, ``queue``, ``remote_*``, ``edit_*``, ...,
  ``automation_*`` for scheduled automations); a ``<heartbeat>`` user message is
  an automation waking a person's thread, often in a turn with no attribution;
- conversation text is ``response_item/message`` with role ``user`` or
  ``assistant`` (``developer`` is harness instructions); user messages carry
  injected context blocks (``# AGENTS.md instructions``,
  ``<environment_context>``, ``<skill>``, ``<subagent_notification>``, ...)
  that are dropped here; reasoning is encrypted and tool I/O is not rendered;
- models come from ``turn_context.model`` and
  ``event_msg/thread_settings_applied.thread_settings.model``.

Rollouts reach hundreds of MB, almost all of it tool output and compaction
records. ``discover`` reads only each file's first record. ``load`` streams
line by line, sniffs each line's record type from its first bytes and parses
JSON only for the records it needs. A line it does not need is drained in
READ_CHUNK pieces and never held. Pasted screenshots ride inside message
records as base64 data URLs (up to ~25 MB a record); their payload is dropped
while the line streams in, so a needed record costs at most MAX_RECORD_BYTES of
what remains.

Known limits: an edited prompt that had already been answered stays in the
transcript beside its edit (rollouts record no rollback); a person's message
sent into a running ``automation_*`` turn is indistinguishable from the
automation's own prompt and is dropped with it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from memex.harness import NormalizedSession, SessionRef, Turn
from memex.scripts.transcript_to_md import MAX_USER_MESSAGE_LENGTH, SYSTEM_TAG_PATTERNS, smart_truncate

NAME = "codex-desktop"
ORIGINATOR = "Codex Desktop"
ROLLOUT_GLOB = "rollout-*.jsonl*"
ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")

HEAD_LINE_LIMIT = 4 << 20  # discover never reads past this much of a first record
HEAD_MAX_LINES = 4  # ...or past this many leading lines looking for session_meta
READ_CHUNK = 1 << 20
MAX_RECORD_BYTES = 32 << 20  # a needed record still longer than this (images dropped) is counted and skipped
MAX_USER_CHARS = MAX_USER_MESSAGE_LENGTH  # the cap Claude Code transcripts use
MAX_ASSISTANT_CHARS = 20000
PARALLEL_HEADS_ABOVE = 32  # thread-pool the head reads (one zstd process each) past this many files

# Why scan() skipped a rollout FILE.
SKIP_REASONS = (
    "empty",
    "unreadable",
    "corrupt",
    "parse-error",
    "legacy-format",
    "no-session-meta",
    "zstd-unavailable",
    "duplicate-compressed",
    "subagent",
    "exec",
    "originator",
)
# Why load() returned None for a discovered thread. `unreadable` wins whenever a
# file could not be read, so an importer never skips a thread for good on a partial read.
LOAD_SKIP_REASONS = ("unreadable", "automation-only", "no-user-turns", "no-text")

# User-role blocks Codex injects around (or instead of) what the person typed.
INJECTED_USER_TAGS = frozenset(
    {
        "environment_context",
        "user_instructions",
        "permissions",
        "turn_aborted",
        "subagent_notification",
        "recommended_plugins",
        "in-app-browser-context",
        "skill",
        "heartbeat",
        "goal_context",
        "realtime_delegation",
        "user_shell_command",
        "app-context",
        "collaboration_mode",
        "skills_instructions",
        "model_switch",
        "image_resize_notice",
        "multi_agent_mode",
        "multi_agent_role",
    }
)
INJECTED_USER_PREFIXES = ("codex_", "external_codex_")  # codex_internal_context, external_codex_apps_*
AGENTS_MD_PREFIX = "# AGENTS.md instructions"
AGENTS_MD_END = "</INSTRUCTIONS>"
QUESTION_REPLY_TAG = "send_user_message_question_reply"  # the person's answers, as JSON
IMAGE_TAG = "image"  # `<image name=[Image #1]>` labels an attached image

_MEM_CITATION_RE = re.compile(r"<oai-mem-citation\b[^>]*>.*?</oai-mem-citation>", re.DOTALL)
_HEARTBEAT_RE = re.compile(r"<heartbeat\b[^>]*>.*?</heartbeat>", re.DOTALL)
_PROPOSED_PLAN_TAG_RE = re.compile(r"</?proposed_plan\b[^>]*>")
_LEAD_TAG_RE = re.compile(r"<([A-Za-z_][\w\-]*)(?:\s[^>]*)?>")
_LEADING_IMAGE_CLOSE_RE = re.compile(r"^\s*</image\s*>")
# `{"timestamp":"...","ordinal":N,"type":"...","payload":{"type":"..."` - the fixed key
# order of modern rollout lines. A line that does not match is parsed in full.
_HEAD_RE = re.compile(
    rb'^\{"timestamp":"([^"]*)",(?:"ordinal":\d+,)?"type":"([a-z_]+)"(?:,"payload":\{"type":"([a-z_]+)")?'
)


def default_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "sessions"


# --- reading ---------------------------------------------------------------


class ZstdUnavailable(RuntimeError):
    """A .jsonl.zst rollout and no decoder (Python 3.14 compression.zstd or the zstd CLI)."""


class CorruptRollout(RuntimeError):
    """A compressed rollout that did not decode to its end (truncated or damaged)."""


def _stdlib_zstd() -> Any:
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
    except ImportError:
        return None
    return zstd


def _zstd_cli() -> str | None:
    return shutil.which("zstd")


def zstd_available() -> bool:
    return _stdlib_zstd() is not None or _zstd_cli() is not None


class _Stream:
    """``readline`` over a decoded rollout: notes EOF, turns decoder errors into CorruptRollout."""

    def __init__(self, fh: IO[bytes], errors: tuple[type[BaseException], ...] = ()) -> None:
        self._fh = fh
        self._errors = errors
        self.eof = False

    def readline(self, limit: int = -1) -> bytes:
        try:
            line = self._fh.readline(limit)
        except self._errors as exc:
            raise CorruptRollout(type(exc).__name__) from exc
        if not line:
            self.eof = True
        return line


@contextlib.contextmanager
def _open_binary(path: Path) -> Iterator[Any]:
    """A binary line stream of the rollout's JSONL, decompressing ``.zst`` on the fly.

    Raises ZstdUnavailable (no decoder) or CorruptRollout (a ``.zst`` that ends
    early or does not decode; for the CLI this is checked when the stream was
    read to its end).
    """
    if not path.name.endswith(".zst"):
        with path.open("rb") as fh:
            yield fh
        return
    zstd = _stdlib_zstd()
    if zstd is not None:
        errors = (EOFError, getattr(zstd, "ZstdError", EOFError))
        with zstd.open(path, "rb") as fh:
            yield _Stream(fh, errors)
        return
    exe = _zstd_cli()
    if exe is None:
        raise ZstdUnavailable(path.name)
    proc = subprocess.Popen(
        [exe, "-dcq", "--", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert proc.stdout is not None
    stream = _Stream(proc.stdout)
    try:
        yield stream
    finally:
        if not stream.eof and proc.poll() is None:
            proc.kill()  # read what we needed (discover reads one line)
        proc.stdout.close()
        returncode = proc.wait()
    if stream.eof and returncode != 0:
        raise CorruptRollout(f"zstd exited {returncode}")


def _needs_parse(rtype: bytes, ptype: bytes | None) -> bool:
    if rtype in (b"session_meta", b"turn_context"):
        return True
    if rtype == b"response_item":
        return ptype is None or ptype == b"message"
    if rtype == b"event_msg":
        return ptype is None or ptype in (b"task_started", b"thread_settings_applied")
    return False  # token counts, world_state, compacted, inter-agent metadata, ...


class _Base64Dropper:
    """Drops the payload of ``"image_url":"data:<mime>;base64,<payload>"`` from a JSON
    line as it streams in, chunk by chunk.

    Only a run of base64-alphabet bytes right after a complete header is dropped;
    such a run holds no quote or backslash, so removing it can never break the
    JSON. The last few bytes of a chunk are held back in case a header straddles
    the boundary.
    """

    HEADER = re.compile(rb'"image_url":"data:[\w.+/-]{1,100};base64,')
    RUN = re.compile(rb"[A-Za-z0-9+/=]*")
    KEEP_BACK = 160  # > the longest header HEADER can match

    def __init__(self) -> None:
        self.inside = False
        self.pending = b""

    def feed(self, chunk: bytes, *, final: bool) -> bytes:
        data = self.pending + chunk if self.pending else chunk
        self.pending = b""
        out: list[bytes] = []
        pos = 0
        while True:
            if self.inside:
                pos = self.RUN.match(data, pos).end()  # type: ignore[union-attr]
                if pos == len(data) and not final:
                    return b"".join(out)  # the payload continues in the next chunk
                self.inside = False
            m = self.HEADER.search(data, pos)
            if m is not None:
                out.append(data[pos : m.end()])
                pos = m.end()
                self.inside = True
                continue
            if final:
                out.append(data[pos:])
                return b"".join(out)
            cut = max(pos, len(data) - self.KEEP_BACK)
            out.append(data[pos:cut])
            self.pending = data[cut:]
            return b"".join(out)


def _iter_records(fh: Any, counters: Counter[str]) -> Iterator[tuple[str | None, dict | None]]:
    """``(timestamp, record)`` per line; ``record`` is None for lines load() does not need.

    Reads at most READ_CHUNK bytes at a time. An unneeded line is drained, never
    held; a needed one is assembled with its base64 images dropped, up to
    MAX_RECORD_BYTES.
    """
    while True:
        first = fh.readline(READ_CHUNK)
        if not first:
            return
        head = _HEAD_RE.match(first)
        ts = head.group(1).decode("utf-8", "replace") if head else None
        complete = first.endswith(b"\n")
        if head is not None and not _needs_parse(head.group(2), head.group(3)):
            while not complete:
                more = fh.readline(READ_CHUNK)
                complete = not more or more.endswith(b"\n")
            yield ts, None
            continue
        dropper = _Base64Dropper()
        buf = bytearray(dropper.feed(first, final=complete))
        oversized = len(buf) > MAX_RECORD_BYTES
        while not complete:
            more = fh.readline(READ_CHUNK)
            complete = not more or more.endswith(b"\n")
            if oversized:
                continue
            buf += dropper.feed(more, final=complete)
            if len(buf) > MAX_RECORD_BYTES:
                oversized = True
                buf = bytearray()
        if oversized:
            counters["oversized_records"] += 1
            yield ts, None
            continue
        if not buf.strip():
            continue
        try:
            rec = json.loads(buf)
        except ValueError:
            counters["parse_errors"] += 1
            continue
        if not isinstance(rec, dict):
            counters["parse_errors"] += 1
            continue
        stamp = rec.get("timestamp")
        yield (stamp if isinstance(stamp, str) else ts), rec


# --- discover --------------------------------------------------------------


@dataclass(frozen=True)
class CodexSessionRef(SessionRef):
    """A top-level Codex Desktop thread, with what discover learned from the file heads."""

    cwd: str = ""
    started_at: str | None = None
    thread_source: str | None = None
    subagent_count: int = 0


@dataclass
class CodexScan:
    refs: list[CodexSessionRef] = field(default_factory=list)
    files_seen: int = 0
    compressed_files: int = 0
    largest_file_bytes: int = 0
    skipped: Counter[str] = field(default_factory=Counter)  # rollout files, by reason
    skipped_originators: Counter[str] = field(default_factory=Counter)  # the "originator" files, by value
    subagent_threads: int = 0  # distinct subagent thread ids seen
    subagents_folded: int = 0  # ...of which counted toward a discovered thread
    subagent_counts: dict[str, int] = field(default_factory=dict)
    # (reason, start time from the file name or None) for every file whose head
    # could not be read, so a caller can judge only the ones in its date window.
    unread: list[tuple[str, str | None]] = field(default_factory=list)


_HEAD_KEYS = ("id", "session_id", "originator", "source", "thread_source", "cwd", "parent_thread_id")


@dataclass
class _Head:
    path: Path
    reason: str | None = None  # a skip reason when the head is unusable
    meta: dict | None = None
    timestamp: str | None = None


def _read_head(path: Path) -> _Head:
    """The session_meta record from the first lines of one rollout; never the whole file."""
    try:
        with _open_binary(path) as fh:
            for i in range(HEAD_MAX_LINES):
                line = fh.readline(HEAD_LINE_LIMIT)
                if not line:
                    return _Head(path, reason="empty" if i == 0 else "no-session-meta")
                if not line.endswith(b"\n") and len(line) >= HEAD_LINE_LIMIT:
                    return _Head(path, reason="parse-error")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    return _Head(path, reason="parse-error")
                if not isinstance(rec, dict):
                    return _Head(path, reason="parse-error")
                if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
                    payload = rec["payload"]
                    stamp = rec.get("timestamp") or payload.get("timestamp")
                    # Keep only the routing keys: session_meta also carries the full
                    # base instructions (~20 KB a file, thousands of files).
                    meta = {k: payload[k] for k in _HEAD_KEYS if k in payload}
                    return _Head(path, meta=meta, timestamp=stamp if isinstance(stamp, str) else None)
                if "type" not in rec:
                    return _Head(path, reason="legacy-format")  # pre-session_meta CLI rollouts
            return _Head(path, reason="no-session-meta")
    except ZstdUnavailable:
        return _Head(path, reason="zstd-unavailable")
    except CorruptRollout:
        return _Head(path, reason="corrupt")
    except OSError:
        return _Head(path, reason="unreadable")


def _thread_id(meta: dict) -> str | None:
    tid = meta.get("id") or meta.get("session_id")
    return tid if isinstance(tid, str) and tid else None


def _subagent_parent(meta: dict) -> str | None:
    source = meta.get("source")
    spawn = {}
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict) and isinstance(sub.get("thread_spawn"), dict):
            spawn = sub["thread_spawn"]
    parent = spawn.get("parent_thread_id") or meta.get("parent_thread_id")
    return parent if isinstance(parent, str) and parent else None


def _classify(meta: dict) -> str:
    """'top', or the skip reason for a parsed session_meta."""
    source = meta.get("source")
    if (isinstance(source, dict) and "subagent" in source) or meta.get("thread_source") == "subagent":
        return "subagent"
    if meta.get("originator") != ORIGINATOR:
        return "originator"
    if source == "exec":
        return "exec"  # `codex exec` probes launched from the desktop app
    return "top"


def _rollout_files(root: Path) -> tuple[list[Path], int]:
    """Every rollout under root; a ``.zst`` whose plain twin exists is dropped (counted)."""
    plain: set[Path] = set()
    compressed: set[Path] = set()
    for p in root.rglob(ROLLOUT_GLOB):
        if p.name.endswith(ROLLOUT_SUFFIXES) and p.is_file():
            (compressed if p.name.endswith(".zst") else plain).add(p)
    twins = {p for p in compressed if p.with_suffix("") in plain}
    return sorted(plain) + sorted(compressed - twins), len(twins)


def _root_of(tid: str, parents: dict[str, str], hints: dict[str, str], tops: set[str]) -> str | None:
    """Follow parent_thread_id up to a top-level thread (subagents nest); session_id as fallback."""
    seen: set[str] = set()
    cur: str | None = tid
    while cur is not None and cur not in seen:
        if cur in tops:
            return cur
        seen.add(cur)
        cur = parents.get(cur)
    hint = hints.get(tid)
    return hint if hint in tops else None


_FILENAME_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.jsonl(?:\.zst)?$"
)


def _name_facts(path: Path) -> tuple[str | None, str | None]:
    """(thread id, ISO start) from a rollout's file name, for a file whose head cannot be read."""
    m = _FILENAME_RE.match(path.name)
    if m is None:
        return None, None
    return m.group(5), f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}"


def _sessions_root_of(path: Path) -> Path:
    """The sessions root a rollout lives in: above YYYY/MM/DD when present, else its folder."""
    day = path.parent.parts[-3:]
    if len(day) == 3 and all(part.isdigit() for part in day):
        return path.parents[3]
    return path.parent


def _existing(path: Path) -> Path:
    """The rollout as it is on disk now: Codex compresses a plain rollout and deletes it."""
    if path.exists():
        return path
    twin = path.with_name(path.name + ".zst")
    return twin if twin.exists() else path


# --- load ------------------------------------------------------------------


def _close_tag_re(tag: str) -> re.Pattern[str]:
    return re.compile(rf"</{re.escape(tag)}(?:\s[^>]*)?>")


def _is_injected(tag: str) -> bool:
    return tag in INJECTED_USER_TAGS or tag.startswith(INJECTED_USER_PREFIXES)


def _strip_system_tags(text: str) -> str:
    for pattern in SYSTEM_TAG_PATTERNS:  # <system-reminder> and Claude Code command scaffolding
        text = pattern.sub("", text)
    return text


def _render_question_reply(body: str) -> str:
    """``[{"question", "answer", ...}]`` -> readable Q/A lines (the person's answers)."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()
    if not isinstance(data, list):
        return body.strip()
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question, answer = item.get("question"), item.get("answer")
        if isinstance(answer, list):
            answer = ", ".join(str(a) for a in answer)
        elif answer is not None and not isinstance(answer, str):
            answer = json.dumps(answer, ensure_ascii=False)
        out.append(f"Q: {question}\nA: {answer or ''}" if question else f"A: {answer or ''}")
    return "\n\n".join(out)


def clean_user_text(text: str) -> str:
    """What the person typed: injected leading blocks dropped, the rest kept.

    Blocks are peeled off the front one at a time; an unknown leading tag stops
    the peeling (the person may type XML). An injected block with no closing tag
    swallows the rest of the item.
    """
    text = _strip_system_tags(text)
    kept: list[str] = []
    while True:
        s = text.lstrip()
        if not s:
            break
        if s.startswith(AGENTS_MD_PREFIX):
            end = s.find(AGENTS_MD_END)
            if end < 0:
                break
            text = s[end + len(AGENTS_MD_END) :]
            continue
        m = _LEAD_TAG_RE.match(s)
        if m is None:
            kept.append(s)
            break
        tag = m.group(1)
        if tag == QUESTION_REPLY_TAG:
            close = _close_tag_re(tag).search(s, m.end())
            reply = _render_question_reply(s[m.end() : close.start() if close else len(s)])
            if reply:
                kept.append(reply)
            if close is None:
                break
            text = s[close.end() :]
            continue
        if tag == IMAGE_TAG:
            text = _LEADING_IMAGE_CLOSE_RE.sub("", s[m.end() :])
            continue
        if not _is_injected(tag):
            kept.append(s)
            break
        if m.group(0).endswith("/>"):
            text = s[m.end() :]
            continue
        close = _close_tag_re(tag).search(s, m.end())
        if close is None:
            break
        text = s[close.end() :]
    return "\n\n".join(k.strip() for k in kept if k.strip())


def clean_assistant_text(text: str) -> str:
    text = _strip_system_tags(text)
    text = _MEM_CITATION_RE.sub("", text)
    text = _HEARTBEAT_RE.sub("", text)  # an automation's decision markup
    if text.lstrip().startswith("<heartbeat"):
        return ""  # ...unclosed: the whole item is the decision
    return _PROPOSED_PLAN_TAG_RE.sub("", text).strip()


def _text_items(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _is_heartbeat(content: object) -> bool:
    """A user message that is an automation waking the thread up, not a person."""
    return any(isinstance(c.get("text"), str) and c["text"].lstrip().startswith("<heartbeat") for c in _text_items(content))


def _message_text(role: str, content: object, counters: Counter[str]) -> str:
    parts: list[str] = []
    images = 0
    for item in _text_items(content):
        if item.get("type") in ("input_image", "image"):
            images += 1
            continue
        raw = item.get("text")
        if not isinstance(raw, str) or not raw.strip():
            continue
        if role == "user":
            cleaned = smart_truncate(clean_user_text(raw), MAX_USER_CHARS)
        else:
            cleaned = smart_truncate(clean_assistant_text(raw), MAX_ASSISTANT_CHARS)
        if cleaned:
            parts.append(cleaned)
        else:
            counters["injected_blocks_dropped"] += 1
    if role == "user" and images:
        parts.append("[image]" if images == 1 else f"[{images} images]")
    return "\n\n".join(parts)


def _is_automation_turn(trigger: object, thread_source: object) -> bool:
    if isinstance(trigger, str) and trigger.startswith("automation"):
        return True
    # In a thread an automation created, only turns a person started (composer,
    # queue, remote_*, ...) belong to the person; untagged turns are the automation's.
    return thread_source == "automation" and not isinstance(trigger, str)


@dataclass
class _LoadState:
    cwd: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    models: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    task_seq: int = 0  # bumps on every task_started
    last_assistant_task: int = -1
    seen_ids: set[str] = field(default_factory=set)
    seen_keys: set[tuple[str, str, bytes]] = field(default_factory=set)
    meta: dict[str, object] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)

    def see(self, ts: str | None) -> None:
        if not ts:
            return
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts

    def model(self, name: object) -> None:
        if isinstance(name, str) and name and name not in self.models:
            self.models.append(name)

    def add(self, role: str, text: str, ts: str | None, *, edit: bool = False) -> None:
        last = self.turns[-1] if self.turns else None
        if role == "assistant":
            if last is not None and last.role == "assistant" and self.last_assistant_task == self.task_seq:
                last.text += "\n\n" + text  # commentary + final answer of one task = one reply
            else:
                self.turns.append(Turn("assistant", text, ts))
            self.last_assistant_task = self.task_seq
            return
        if last is not None and last.role == "user":
            if last.text == text:
                self.counters["resent_dropped"] += 1  # the same prompt sent again before any reply
                return
            if edit:
                self.counters["edits_replaced"] += 1  # an unanswered prompt, edited and re-sent
                self.turns[-1] = Turn("user", text, ts)
                return
        self.turns.append(Turn("user", text, ts))

    @property
    def user_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")


def _load_file(fh: Any, st: _LoadState) -> None:
    trigger: object = None
    heartbeat_turn = False  # a heartbeat woke the thread: replies belong to the automation
    edit_pending = False  # this task re-sends an edited prompt
    for ts, rec in _iter_records(fh, st.counters):
        st.see(ts)
        if rec is None:
            continue
        rtype = rec.get("type")
        pl = rec.get("payload")
        if not isinstance(pl, dict):
            continue
        if rtype == "session_meta":
            if st.cwd is None and isinstance(pl.get("cwd"), str):
                st.cwd = pl["cwd"]
            for key in ("originator", "thread_source", "cli_version"):
                if key not in st.meta and isinstance(pl.get(key), str):
                    st.meta[key] = pl[key]
            continue
        if rtype == "turn_context":
            st.model(pl.get("model"))
            continue
        if rtype == "event_msg":
            if pl.get("type") == "task_started":
                attribution = pl.get("turn_attribution")
                trigger = attribution.get("turn_trigger") if isinstance(attribution, dict) else None
                heartbeat_turn = False
                edit_pending = isinstance(trigger, str) and trigger.startswith("edit")
                st.task_seq += 1
            elif pl.get("type") == "thread_settings_applied" and isinstance(pl.get("thread_settings"), dict):
                st.model(pl["thread_settings"].get("model"))
            continue
        if rtype != "response_item" or pl.get("type") != "message":
            continue
        role = pl.get("role")
        if role not in ("user", "assistant"):
            continue  # developer = harness instructions
        content = pl.get("content")
        wake_up = role == "user" and _is_heartbeat(content)
        heartbeat_turn = heartbeat_turn or wake_up
        automation = (
            wake_up
            or (heartbeat_turn and role == "assistant")
            or _is_automation_turn(trigger, st.meta.get("thread_source"))
        )
        # De-duplicate BEFORE dropping automation messages, so a replay of a dropped
        # message (in a resume file, outside its automation turn) is still recognised.
        mid = pl.get("id")
        if isinstance(mid, str) and mid:
            if mid in st.seen_ids:
                st.counters["duplicates_dropped"] += 1
                continue
            st.seen_ids.add(mid)
        text = _message_text(role, content, Counter() if automation else st.counters)
        if ts and text:
            key = (ts, role, hashlib.sha1(text.encode("utf-8", "replace")).digest())
            if key in st.seen_keys:
                st.counters["duplicates_dropped"] += 1
                continue
            st.seen_keys.add(key)
        if automation:
            st.counters["automation_messages_dropped"] += 1
            continue
        if not text:
            continue
        if role == "user":
            heartbeat_turn = False  # a person spoke: the automation's turn is over
            st.add("user", text, ts, edit=edit_pending)
            edit_pending = False
        else:
            st.add("assistant", text, ts)


# --- adapter ---------------------------------------------------------------


class CodexDesktopAdapter:
    """``HarnessAdapter`` for Codex Desktop. ``scan`` also reports what it skipped and why.

    An adapter instance is a snapshot: file heads are cached by (size, mtime) and
    subagent counts per sessions root are kept until a new ``scan`` of that root.
    """

    name = NAME

    def __init__(self, root: Path | None = None, *, workers: int = 8) -> None:
        self.root = root
        self.workers = max(1, workers)
        self._counts: dict[Path, dict[str, int]] = {}
        self._heads: dict[Path, tuple[tuple[int, int], _Head]] = {}

    def _root(self, root: Path | None) -> Path:
        return Path(root or self.root or default_root())

    def _head(self, path: Path) -> _Head:
        try:
            st = path.stat()
        except OSError:
            return _Head(path, reason="unreadable")
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._heads.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        head = _read_head(path)
        self._heads[path] = (stamp, head)
        return head

    def scan(self, *, root: Path | None = None) -> CodexScan:
        base = self._root(root)
        scan = CodexScan()
        if not base.is_dir():
            self._counts[base] = {}
            return scan
        files, twins = _rollout_files(base)
        scan.files_seen = len(files) + twins
        if twins:
            scan.skipped["duplicate-compressed"] = twins
        for p in files:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            scan.largest_file_bytes = max(scan.largest_file_bytes, size)
            scan.compressed_files += p.name.endswith(".zst")
        if self.workers > 1 and len(files) > PARALLEL_HEADS_ABOVE:
            with ThreadPoolExecutor(self.workers) as pool:
                heads = list(pool.map(self._head, files))
        else:
            heads = [self._head(p) for p in files]

        threads: dict[str, list[_Head]] = defaultdict(list)
        parents: dict[str, str] = {}
        hints: dict[str, str] = {}
        subagents: set[str] = set()
        damaged: list[tuple[str, _Head]] = []  # unreadable parts, by the thread id in their name
        for head in heads:
            if head.meta is None:
                scan.skipped[head.reason or "parse-error"] += 1
                tid, started = _name_facts(head.path)
                scan.unread.append((head.reason or "parse-error", started))
                if tid and head.reason in ("corrupt", "unreadable", "zstd-unavailable"):
                    damaged.append((tid, _Head(head.path, reason=head.reason, timestamp=started)))
                continue
            tid = _thread_id(head.meta)
            if tid is None:
                scan.skipped["parse-error"] += 1
                scan.unread.append(("parse-error", _name_facts(head.path)[1]))
                continue
            kind = _classify(head.meta)
            if kind == "subagent":
                scan.skipped["subagent"] += 1
                subagents.add(tid)
                parent = _subagent_parent(head.meta)
                if parent and parent != tid:
                    parents[tid] = parent
                hint = head.meta.get("session_id")
                if isinstance(hint, str) and hint != tid:
                    hints[tid] = hint
                continue
            if kind != "top":
                scan.skipped[kind] += 1
                if kind == "originator":
                    scan.skipped_originators[str(head.meta.get("originator"))] += 1
                continue
            threads[tid].append(head)

        tops = set(threads)
        # A damaged part of a known thread stays in its ref, so load() reports it
        # (unreadable_files / corrupt_files) instead of the history silently shrinking.
        for tid, head in damaged:
            if tid in tops:
                threads[tid].append(head)
        counts: Counter[str] = Counter()
        for sub in subagents:
            owner = _root_of(sub, parents, hints, tops)
            if owner is not None:
                counts[owner] += 1
        scan.subagent_threads = len(subagents)
        scan.subagents_folded = sum(counts.values())
        scan.subagent_counts = dict(counts)
        self._counts[base] = scan.subagent_counts

        for tid, group in threads.items():
            group.sort(key=lambda h: (h.timestamp or "", h.path.name))
            first = next(h.meta for h in group if h.meta is not None)
            cwd = first.get("cwd")
            thread_source = first.get("thread_source")
            scan.refs.append(
                CodexSessionRef(
                    harness=NAME,
                    session_id=tid,
                    files=tuple(h.path for h in group),
                    cwd=cwd if isinstance(cwd, str) else "",
                    started_at=group[0].timestamp,
                    thread_source=thread_source if isinstance(thread_source, str) else None,
                    subagent_count=counts.get(tid, 0),
                )
            )
        scan.refs.sort(key=lambda r: (r.started_at or "", r.session_id))
        return scan

    def discover(self, *, root: Path | None = None) -> list[CodexSessionRef]:
        return self.scan(root=root).refs

    def _subagent_count(self, ref: SessionRef) -> int:
        count = getattr(ref, "subagent_count", None)
        if isinstance(count, int):
            return count
        if not ref.files:
            return 0
        base = _sessions_root_of(ref.files[0])  # the tree the ref lives in, not the adapter's default
        if base not in self._counts:
            self.scan(root=base)
        return self._counts.get(base, {}).get(ref.session_id, 0)

    def _ordered(self, files: Sequence[Path]) -> list[Path]:
        heads = [self._head(_existing(p)) for p in files]
        return [h.path for h in sorted(heads, key=lambda h: (h.timestamp or "", h.path.name))]

    def load(self, ref: SessionRef) -> NormalizedSession | None:
        return self.load_explained(ref)[0]

    def load_explained(self, ref: SessionRef) -> tuple[NormalizedSession | None, str | None]:
        """``(session, None)``, or ``(None, reason)``; reason is one of LOAD_SKIP_REASONS."""
        if isinstance(ref, CodexSessionRef):
            files = [_existing(p) for p in ref.files]
        else:
            files = self._ordered(ref.files)
        st = _LoadState()
        for path in files:
            try:
                with _open_binary(path) as fh:
                    _load_file(fh, st)
            except CorruptRollout:
                st.counters["corrupt_files"] += 1  # what decoded before the damage is kept
            except (ZstdUnavailable, OSError):
                st.counters["unreadable_files"] += 1
        if st.user_turns == 0:
            if st.counters["unreadable_files"] or st.counters["corrupt_files"]:
                return None, "unreadable"
            if st.counters["automation_messages_dropped"]:
                return None, "automation-only"
            return None, ("no-user-turns" if st.turns else "no-text")
        metadata: dict[str, object] = dict(st.meta)
        metadata["rollout_files"] = len(files)
        metadata["compressed_files"] = sum(1 for p in files if p.name.endswith(".zst"))
        for key in (
            "parse_errors",
            "oversized_records",
            "duplicates_dropped",
            "resent_dropped",
            "edits_replaced",
            "injected_blocks_dropped",
            "automation_messages_dropped",
            "unreadable_files",
            "corrupt_files",
        ):
            metadata[key] = st.counters[key]
        session = NormalizedSession(
            harness=NAME,
            session_id=ref.session_id,
            cwd=st.cwd or getattr(ref, "cwd", "") or "",
            turns=st.turns,
            source_files=list(files),
            started_at=st.first_ts,
            ended_at=st.last_ts,
            models=st.models,
            subagent_count=self._subagent_count(ref),
            metadata=metadata,
        )
        return session, None
