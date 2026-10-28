"""Kimi Code sessions as ``NormalizedSession`` (v0.22.0, plan item D3).

Observed format (Kimi Code app, wire protocol versions 1.0, 1.1, 1.3, 1.4 and
1.5, read 2026-10-28; the format is undocumented, so this records record types
and keys only)::

    ~/.kimi-code/
      session_index.jsonl   {sessionId, sessionDir, workDir} per session;
                            {sessionId, deleted: true} when a session is deleted
      workspaces.json       {version, workspaces: {<wd dir name>: {root, name,
                            created_at, last_opened_at}}, deleted_workspace_ids}
      sessions/wd_<cwd-slug>_<hash>/          one dir per workspace (cwd)
        session_<uuid>/                       a session made by Kimi Code
        ses_<uuid>/                           a Kimi CLI session imported into Kimi Code
          state.json        {createdAt, updatedAt (epoch ms or ISO 8601), title,
                            lastPrompt, isCustomTitle, custom, agents: {main:
                            {type: main, homedir}, agent-<n>: {type: sub,
                            parentAgentId, homedir, ...}}; newer files add id
                            (== the dir name), version, cwd, archived; some
                            older ones carry workDir instead of cwd.
                            Imported sessions: custom.imported_from_kimi_cli,
                            custom.kimi_cli_session_id (== the dir's uuid)}
          agents/main/wire.jsonl              the top-level conversation
          agents/agent-<n>/wire.jsonl         one per subagent (same record shapes)
          agents/*/blobs/, file-history/, tasks/, tool-results/, logs/, notify/

``wire.jsonl``, one JSON record per line, keyed by ``type``:

- ``metadata`` (always line 1): ``protocol_version``, ``created_at`` (epoch ms),
  sometimes ``app_version``.
- ``context.append_message``: ``message.{role, content[{type: text|think|
  image_url|video_url, text|think}], origin.kind, toolCalls}``, ``time`` (epoch
  ms), sometimes ``agentId``. ``origin.kind == "user"`` is a message the person
  typed (prompts and mid-turn steers alike). Every other kind is injected:
  ``injection`` (``origin.variant``: AGENTS.md, permission mode, todo and date
  reminders, ...), ``system_trigger`` (goal continuation, a subagent's task),
  ``skill_activation``, ``task``, ``cron_job``, ``background_task``.
- ``context.append_loop_event``: ``event.type`` ∈ ``step.begin``, ``step.end``,
  ``content.part`` (``event.part.{type: text|think}``, ``turnId``, ``step``),
  ``tool.call``, ``tool.result``. Assistant text = ``content.part`` with
  ``part.type == "text"``; parts are whole (not deltas), in file order.
- ``usage.record``: ``model``, ``usage``, ``usageScope``, ``time``. ``model`` is
  the configured model alias, present in every protocol version.
- From 1.4/1.5 also ``llm.request`` (``model``, ``modelAlias``, ``provider``),
  ``agent.message.appended`` (``message.message`` + ``message.meta.source``:
  a second copy of user/assistant/tool messages, ignored here so nothing is
  doubled), ``subagent.spawned|started|completed|failed|cancelled``
  (``subagentId``), ``turn.prompt``/``turn.steer`` (``input``, ``origin``),
  ``turn.ended``, ``prompt.completed``, ``config.update`` (``modelAlias``,
  ``profileName``, ``systemPrompt``, ``thinkingEffort``), ``profile.bind``,
  ``goal.*``, ``plan_mode.*``, ``full_compaction.*``, ``context.undo``,
  ``forked`` and others: none carry text this adapter renders.

A resumed session appends to the same ``wire.jsonl`` (session-start records
such as ``plugin.session_start`` and ``mcp.tools_discovered`` repeat; there is
no second ``metadata`` line and no new directory), so one directory is one
session. Subagents live beside ``main`` and are counted, not rendered.

Imported Kimi CLI sessions (``ses_<uuid>``) use an older record shape: only
``metadata``, ``config.update``, ``tools.set_active_tools`` and
``context.append_message`` with ``message.{role: user|assistant|tool, content,
toolCalls}``, no ``origin`` and no per-record ``time``. Injected text there sits
inside user messages as ``<system-reminder>`` blocks, which are removed (native
sessions mark injections by ``origin.kind`` instead, so their user text is kept
verbatim).

Session ids: memex stamps memos with a bare UUID (``session_memos``), so a
``session_<uuid>``/``ses_<uuid>`` directory yields ``session_id = <uuid>``; the
native id stays in ``metadata["kimi_session_id"]``. A directory whose name is
not that shape keeps its name as the id.

Only protocol versions in ``KNOWN_PROTOCOLS`` are parsed; any other version is
skipped and counted as ``protocol:<version>`` in ``adapter.skipped``, so a Kimi
Code upgrade shows up as a count rather than as silently wrong transcripts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from memex.harness import NormalizedSession, SessionRef, Turn

NAME = "kimi-code"
DEFAULT_ROOT = Path.home() / ".kimi-code" / "sessions"
# Versions whose record shapes were checked against real files (see docstring).
KNOWN_PROTOCOLS = frozenset({"1.0", "1.1", "1.3", "1.4", "1.5"})
FORMAT_NATIVE = "native"
FORMAT_KIMI_CLI = "kimi-cli-import"

_DIR_ID = re.compile(
    r"^(?:session|ses)_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE
)
# The one injected marker seen inside imported Kimi CLI user messages.
_INJECTED_BLOCK = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_MEDIA = {"image_url": "[image]", "video_url": "[video]"}


def _iso(value: object) -> str | None:
    """Epoch ms/s or an ISO 8601 string → ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 1e11 else value
            stamp = datetime.fromtimestamp(seconds, tz=UTC)
        elif isinstance(value, str) and value:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_record(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            line = fh.readline()
    except OSError:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _session_id(dir_name: str) -> str:
    match = _DIR_ID.match(dir_name)
    return match.group(1).lower() if match else dir_name


def _is_imported(session_dir: Path, state: dict) -> bool:
    custom = state.get("custom")
    if isinstance(custom, dict) and custom.get("imported_from_kimi_cli") is True:
        return True
    return session_dir.name.startswith("ses_")


def _text_of(content: object, *, strip_injected: bool) -> str:
    """User/assistant text parts joined; ``think`` parts never; media as a marker."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            text = part["text"]
            if strip_injected:
                text = _INJECTED_BLOCK.sub("", text)
            if text.strip():
                pieces.append(text.strip())
        elif kind in _MEDIA:
            pieces.append(_MEDIA[kind])
    return "\n\n".join(pieces)


def _assistant_part(record: dict) -> str | None:
    """The text of a ``content.part`` loop event whose part is ``text``."""
    event = record.get("event")
    if not isinstance(event, dict) or event.get("type") != "content.part":
        return None
    part = event.get("part")
    if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
        return None
    return part["text"].strip() or None


class _Builder:
    """Turns in file order; consecutive assistant parts merge into one turn,
    and assistant text before the first user turn is dropped."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def user(self, text: str, timestamp: str | None) -> None:
        self.turns.append(Turn("user", text, timestamp))

    def assistant(self, text: str, timestamp: str | None) -> bool:
        if not self.turns:
            return False
        last = self.turns[-1]
        if last.role == "assistant":
            last.text = f"{last.text}\n\n{text}"
        else:
            self.turns.append(Turn("assistant", text, timestamp))
        return True


class KimiCodeAdapter:
    """``HarnessAdapter`` for Kimi Code. Counters on the instance say what was
    skipped and why (``skipped``), what was left out of transcripts
    (``dropped``), which protocol versions were seen (``protocols``) and how
    many lines failed to parse (``parse_errors``). Make one per run: the
    counters and the ``session_index.jsonl``/``workspaces.json`` reads are
    cached on the instance."""

    name = NAME

    def __init__(self, *, include_imported: bool = True) -> None:
        self.include_imported = include_imported
        self.skipped: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()
        self.protocols: Counter[str] = Counter()
        self.parse_errors = 0
        self._indexes: dict[Path, tuple[dict[str, str], set[str]]] = {}
        self._workspaces: dict[Path, dict[str, str]] = {}

    # --- side files (kimi home = the parent of sessions/) --------------------

    def _index(self, kimi_home: Path) -> tuple[dict[str, str], set[str]]:
        """``session_index.jsonl`` → ({sessionId: workDir}, {deleted ids})."""
        if kimi_home not in self._indexes:
            work_dirs: dict[str, str] = {}
            deleted: set[str] = set()
            try:
                with (kimi_home / "session_index.jsonl").open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        sid = row.get("sessionId") if isinstance(row, dict) else None
                        if not isinstance(sid, str):
                            continue
                        if row.get("deleted") is True:
                            deleted.add(sid)
                            work_dirs.pop(sid, None)
                        elif isinstance(row.get("workDir"), str):
                            work_dirs[sid] = row["workDir"]
                            deleted.discard(sid)
            except OSError:
                pass
            self._indexes[kimi_home] = (work_dirs, deleted)
        return self._indexes[kimi_home]

    def _workspace_roots(self, kimi_home: Path) -> dict[str, str]:
        if kimi_home not in self._workspaces:
            spaces = _read_json(kimi_home / "workspaces.json").get("workspaces")
            roots = {}
            if isinstance(spaces, dict):
                for key, entry in spaces.items():
                    if isinstance(entry, dict) and isinstance(entry.get("root"), str):
                        roots[key] = entry["root"]
            self._workspaces[kimi_home] = roots
        return self._workspaces[kimi_home]

    # --- discover -------------------------------------------------------------

    def discover(self, *, root: Path | None = None) -> Iterator[SessionRef]:
        """One ``SessionRef`` per top-level session (its ``agents/main/wire.jsonl``).
        Reads only line 1 of each wire file and the small ``state.json``."""
        root = Path(root) if root is not None else DEFAULT_ROOT
        if not root.is_dir():
            return
        _, deleted = self._index(root.parent)
        for workspace in self._subdirs(root):
            for session_dir in self._subdirs(workspace):
                wire = session_dir / "agents" / "main" / "wire.jsonl"
                if not wire.is_file():
                    self.skipped["no_wire"] += 1
                    continue
                first = _first_record(wire)
                if not first or first.get("type") != "metadata":
                    self.skipped["no_metadata"] += 1
                    continue
                version = str(first.get("protocol_version"))
                self.protocols[version] += 1
                if version not in KNOWN_PROTOCOLS:
                    self.skipped[f"protocol:{version}"] += 1
                    continue
                if session_dir.name in deleted:
                    self.skipped["deleted"] += 1
                    continue
                if not self.include_imported and _is_imported(session_dir, _read_json(session_dir / "state.json")):
                    self.skipped["imported_kimi_cli"] += 1
                    continue
                yield SessionRef(harness=NAME, session_id=_session_id(session_dir.name), files=(wire,))

    def _subdirs(self, path: Path) -> list[Path]:
        try:
            return sorted(p for p in path.iterdir() if p.is_dir())
        except OSError:
            self.skipped["unreadable_dir"] += 1
            return []

    # --- load -----------------------------------------------------------------

    def load(self, ref: SessionRef) -> NormalizedSession | None:
        if ref.harness != NAME:
            raise ValueError(f"not a {NAME} session: {ref.harness!r}")
        wire = ref.files[0]
        session_dir = wire.parent.parent.parent
        kimi_home = session_dir.parent.parent.parent
        state = _read_json(session_dir / "state.json")
        imported = _is_imported(session_dir, state)

        builder = _Builder()
        models: list[str] = []
        aliases: list[str] = []
        first_time: int | float | None = None
        last_time: int | float | None = None
        version = app_version = None
        created_at: object = None
        errors = 0
        try:
            fh = wire.open(encoding="utf-8", errors="replace")
        except OSError:
            self.skipped["unreadable"] += 1
            return None
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    errors += 1
                    continue
                if not isinstance(record, dict):
                    errors += 1
                    continue
                kind = record.get("type")
                agent = record.get("agentId")
                if agent is not None and agent != "main":
                    self.dropped["other_agent"] += 1
                    continue
                when = record.get("time")
                if isinstance(when, int | float) and not isinstance(when, bool):
                    first_time = when if first_time is None else min(first_time, when)
                    last_time = when if last_time is None else max(last_time, when)
                if kind == "metadata":
                    version = record.get("protocol_version")
                    app_version = record.get("app_version")
                    created_at = record.get("created_at")
                elif kind == "context.append_message":
                    self._message(record, builder, imported=imported)
                elif kind == "context.append_loop_event" and not imported:
                    text = _assistant_part(record)
                    if text and not builder.assistant(text, _iso(when)):
                        self.dropped["assistant_before_user"] += 1
                elif kind == "usage.record":
                    model = record.get("model")
                    if isinstance(model, str) and model and model not in models:
                        models.append(model)
                elif kind == "config.update":
                    alias = record.get("modelAlias")
                    if isinstance(alias, str) and alias and alias not in aliases:
                        aliases.append(alias)
        self.parse_errors += errors

        if version is None:
            self.skipped["no_metadata"] += 1
            return None
        if str(version) not in KNOWN_PROTOCOLS:
            self.skipped[f"protocol:{version}"] += 1
            return None
        if not any(t.role == "user" for t in builder.turns):
            self.skipped["no_user_turns"] += 1
            return None

        started = _iso(created_at) or _iso(first_time) or _iso(state.get("createdAt"))
        ended = _iso(last_time) if not imported else None
        ended = ended or _iso(state.get("updatedAt"))
        if started and ended and ended < started:
            ended = started

        metadata: dict[str, object] = {
            "kimi_session_id": session_dir.name,
            "format": FORMAT_KIMI_CLI if imported else FORMAT_NATIVE,
        }
        if version is not None:
            metadata["protocol_version"] = str(version)
        if isinstance(app_version, str) and app_version:
            metadata["app_version"] = app_version
        if imported:
            metadata["imported_from"] = "kimi-cli"
        if errors:
            metadata["parse_errors"] = errors

        return NormalizedSession(
            harness=NAME,
            session_id=ref.session_id,
            cwd=self._cwd(session_dir, state, kimi_home),
            turns=builder.turns,
            source_files=[wire],
            started_at=started,
            ended_at=ended,
            models=models or aliases,
            subagent_count=self._subagents(session_dir),
            metadata=metadata,
        )

    def _message(self, record: dict, builder: _Builder, *, imported: bool) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        when = _iso(record.get("time"))
        if imported:
            if role == "user":
                text = _text_of(message.get("content"), strip_injected=True)
                if text:
                    builder.user(text, when)
                else:
                    self.dropped["injected"] += 1
            elif role == "assistant":
                text = _text_of(message.get("content"), strip_injected=False)
                if text and not builder.assistant(text, when):
                    self.dropped["assistant_before_user"] += 1
            return
        if role != "user":
            return
        origin = message.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        if origin_kind != "user":
            self.dropped[f"origin:{origin_kind}"] += 1
            return
        text = _text_of(message.get("content"), strip_injected=False)
        if text:
            builder.user(text, when)
        else:
            self.dropped["empty_user"] += 1

    def _cwd(self, session_dir: Path, state: dict, kimi_home: Path) -> str:
        for key in ("cwd", "workDir"):
            value = state.get(key)
            if isinstance(value, str) and value:
                return value
        work_dirs, _ = self._index(kimi_home)
        if session_dir.name in work_dirs:
            return work_dirs[session_dir.name]
        return self._workspace_roots(kimi_home).get(session_dir.parent.name, "")

    @staticmethod
    def _subagents(session_dir: Path) -> int:
        agents = session_dir / "agents"
        try:
            return sum(1 for p in agents.iterdir() if p.name != "main" and (p / "wire.jsonl").is_file())
        except OSError:
            return 0


__all__ = ["DEFAULT_ROOT", "KNOWN_PROTOCOLS", "NAME", "KimiCodeAdapter"]
