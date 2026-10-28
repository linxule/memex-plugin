#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
#     "pyyaml>=6.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Claude Memory Plugin - SessionStart Hook (v0.22.0)

Decides from the vault, not from state.json: a session has a memo when a memo
in the vault is stamped with its id (``memex.session_memos``).

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/project",
    "hook_event_name": "SessionStart",
    "source": "startup" | "resume" | "clear" | "compact" | "fork",
    "agent_type": "..."            // only when started with --agent
}

By source:
- compact → the truth about this session's memo: its path (extend it if work
  followed), or one short recovery instruction pointing at
  skills/memo-writing/recovery.md.
- startup / clear / resume → pending-memo signals for this cwd (or every
  signal, in the vault) that no stamped memo covers; covered and sdk-cli
  signals are cleared, never announced (positive evidence only).
- startup / clear / fork → the sidecar-rejection notice, and for a project
  session a one-line pointer to the project's overview (I5). fork: no memo
  nags — the parent session owns its pending work.
- fleet seats (sdk-cli) → nothing at all (I7).
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_index_path, get_memex_path, get_pending_dir, get_state_dir
from memex.routing import GUESS_RULES, read_filing_receipts
from memex.scripts.reconcile_orphans import (
    _UUID_RE,
    _date_from_name,
    _frontmatter,
    _redirect_target,
    is_helper_session,
    signal_is_helper,
    transcript_facts,
)
from memex.scripts.utils import (
    detect_project,
    get_config,
    is_session_processed,
    log_info,
    log_warning,
    output_context,
    read_hook_input,
)
from memex.session_memos import (
    _PREFILTER_CHARS,
    memo_for_session,
    memo_stamps,
    normalize_session_id,
)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RECOVERY_RECIPE = PLUGIN_ROOT / "skills" / "memo-writing" / "recovery.md"
POINTER_MAX_CHARS = 300
# The stub SessionEnd writes into a new project folder (create_project_meta).
_TEMPLATE_MARKER = "Transcripts and memos will accumulate in this project folder"
_MAINTAINED_LINES = 40  # garden-tending's "maintained overview" threshold
_MAX_REDIRECT_HOPS = 4
# Reading other sessions' transcripts to spot seats is bounded so a large
# backlog can't eat SessionStart's 7 s; what isn't checked stays announced.
HELPER_CHECK_BUDGET_S = 2.0


def _resolve(path_str: str) -> Path | None:
    """Best-effort Path.resolve(); returns None on empty/invalid input."""
    if not path_str:
        return None
    try:
        return Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _vault() -> Path | None:
    try:
        return get_memex_path()
    except Exception:
        return None


def _in_vault(cwd: str) -> bool:
    current, vault = _resolve(cwd), _vault()
    return bool(current and vault and (current == vault or vault in current.parents))


# Mirrors memex.sidecars.REJECTIONS_META_KEY (a test pins the two together);
# importing memex.sidecars here would pull the whole index stack into a hook
# with a 7 s budget.
_REJECTIONS_META_KEY = "sidecar_rejections"


def sidecar_rejection_notice(index_path: Path | None = None) -> str | None:
    """One line naming the sidecars the last ingest rejected, or None.

    A rejected sidecar keeps every observation in it out of the index, and
    the ingest pass only logs it — which once hid 8 observations for two
    weeks. Read-only, best-effort: any failure means no notice, never a
    failed session start.
    """
    try:
        index = index_path or get_index_path(get_memex_path())
        if not index.exists():
            return None
        conn = sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = ?", (_REJECTIONS_META_KEY,)
            ).fetchone()
        finally:
            conn.close()
        rejected = json.loads(row[0]) if row else []
        if not isinstance(rejected, list) or not rejected:
            return None
        first = rejected[0] if isinstance(rejected[0], dict) else {}
        more = f" and {len(rejected) - 1} more" if len(rejected) > 1 else ""
        return (
            f"⚠️ memex: {len(rejected)} observation sidecar(s) rejected by the last index "
            f"update, so changes to their observations are not reaching search: "
            f"{first.get('doc_path', '?')} "
            f"({str(first.get('error', '')).strip()}){more}. Run `memex obs sidecars` for "
            f"details, fix the file, then `memex obs ingest-sidecars`."
        )
    except Exception:
        return None


# ── pending-memo signals ─────────────────────────────────────────────────

def load_pending_signals() -> list[dict]:
    """Every readable signal, each carrying its file as ``_file``."""
    out: list[dict] = []
    try:
        files = sorted(get_pending_dir().glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            sig = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            log_warning(f"Invalid or unreadable pending memo file: {f}")
            continue
        if isinstance(sig, dict):
            sig["_file"] = str(f)
            out.append(sig)
    return out


def filter_pending_for_cwd(pending: list[dict], cwd: str) -> tuple[list[dict], bool]:
    """
    Filter pending memos to those relevant to the current working directory.

    Returns (visible_memos, is_vault_view):
    - If cwd exactly matches a pending memo's cwd: return matching memos, False
    - Else if cwd is inside the memex vault: return ALL pending memos, True
      (vault is the meta/triage location for orphan memos)
    - Otherwise: return [], False
    """
    current = _resolve(cwd)
    if current is None:
        return ([], False)

    matching = []
    for memo in pending:
        memo_cwd = _resolve(memo.get("cwd", ""))
        if memo_cwd is not None and memo_cwd == current:
            matching.append(memo)

    if matching:
        return (matching, False)

    # No project match — fall back to vault-only triage view
    try:
        memex_path = get_memex_path()
    except Exception:
        return ([], False)

    if current == memex_path or memex_path in current.parents:
        return (pending, True)

    return ([], False)


def stamped_sessions(vault: Path, session_ids: set[str]) -> dict[str, Path]:
    """session id → a memo stamped with it, for the ids asked about.

    One pass over the vault: the full UUIDs in each memo's head are matched
    against the ids first, and only a hit is parsed (``memo_stamps`` of the
    strict frontmatter reader), so N signals cost one scan, not N.
    """
    wanted = {sid for sid in map(normalize_session_id, session_ids) if sid}
    found: dict[str, Path] = {}
    if not wanted:
        return found
    for memo in sorted(vault.glob("projects/*/memos/**/*.md")):
        try:
            with open(memo, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_PREFILTER_CHARS)
        except OSError:
            continue
        hits = {m.lower() for m in _UUID_RE.findall(head)} & (wanted - found.keys())
        if not hits:
            continue
        stamps = memo_stamps(_frontmatter(memo))
        for sid in hits & stamps:
            found[sid] = memo
        if found.keys() >= wanted:
            break
    return found


def settle_signals(signals: list[dict], vault: Path | None) -> list[dict]:
    """The signals still owed a memo. A signal whose session has a stamped memo
    (the reconcile-orphans exact rule) or that belongs to an sdk-cli seat is
    deleted, never announced; everything else is kept as is."""
    if vault is None:
        return list(signals)
    covered = stamped_sessions(vault, {str(s.get("session_id", "") or "") for s in signals})
    deadline = time.monotonic() + HELPER_CHECK_BUDGET_S
    owed: list[dict] = []
    for sig in signals:
        sid = normalize_session_id(sig.get("session_id"))
        reason = None
        if sid and sid in covered:
            reason = f"memo {covered[sid].name}"
        elif time.monotonic() < deadline and signal_is_helper(vault, sig):
            reason = "sdk-cli seat"
        if reason is None:
            owed.append(sig)
            continue
        file = sig.get("_file")
        if file:
            try:
                Path(file).unlink()
                log_info(f"Cleared pending-memo signal {Path(file).name}: {reason}")
            except OSError as e:
                log_warning(f"Could not clear settled signal {file}: {e}")
    return owed


def pending_notice(cwd: str, *, resume: bool) -> str | None:
    """The pending-memo line for this cwd, or None when nothing is owed."""
    pending = load_pending_signals()
    visible, is_vault_view = filter_pending_for_cwd(pending, cwd)
    if not visible:
        if pending:
            log_info(f"Suppressed: {len(pending)} pending memo(s) exist but none match cwd={cwd}")
        return None
    owed = settle_signals(visible, _vault())
    if not owed:
        return None
    lead = "📝 Note:" if resume else "⚠️"
    where = ("orphan memo(s) pending retry (no matching project cwd)" if is_vault_view
             else "memo(s) pending retry for this project")
    log_info(f"{'Resume' if resume else 'Startup'} injection: {len(owed)} pending memo(s)"
             f"{' (vault triage)' if is_vault_view else ''}")
    return (
        f"{lead} {len(owed)} {where} — no memo is stamped with their session id. "
        f"First run `memex session reconcile-orphans` (lists likely matches and genuine retries). "
        f"For each genuine retry spawn one background subagent with the Agent tool, model 'sonnet' "
        f"(memo writing is a sonnet-tier task), that follows `{RECOVERY_RECIPE}`."
    )


# ── post-compaction ──────────────────────────────────────────────────────

def recorded_memo(session_id: str) -> str | None:
    """The memo path PreCompact or UserPromptSubmit recorded for this session."""
    state = get_state_dir() / "session-state"
    for name, check_id in ((f"{session_id[:16]}.memo", True), (f"{session_id[:16]}.json", False)):
        try:
            data = json.loads((state / name).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or (check_id and data.get("session_id") != session_id):
            continue
        if isinstance(data.get("memo_path"), str) and data["memo_path"]:
            return data["memo_path"]
    return None


def _own_signal(session_id: str) -> dict:
    try:
        f = get_pending_dir() / f"{session_id[:16]}.json"
        sig = json.loads(f.read_text()) if f.exists() else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(sig, dict) or str(sig.get("session_id", "")) != session_id:
        return {}
    return sig


def session_memo(vault: Path | None, session_id: str, *, project: str | None = None,
                 hint: str | None = None) -> Path | None:
    """This session's stamped memo: the path PreCompact/UserPromptSubmit
    recorded (re-verified — it may have moved or been re-stamped), else a scan."""
    if vault is None:
        return None
    sid = normalize_session_id(session_id)
    if sid and isinstance(hint, str) and hint:
        p = Path(hint)
        if p.is_file() and sid in memo_stamps(_frontmatter(p)):
            return p
    return memo_for_session(vault, session_id, project=project)


def _saved_at(path: Path) -> str:
    try:
        when = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return "time unknown"
    return when.strftime("%H:%M") if when.date() == date.today() else when.strftime("%Y-%m-%d %H:%M")


def _vault_rel(path: Path, vault: Path) -> str:
    try:
        return str(path.resolve().relative_to(vault.resolve()))
    except (OSError, ValueError):
        return str(path)


def memo_exists_message(memo: Path, vault: Path) -> str:
    return (
        f"📚 Session compacted. This session's memo is `{memo}` (saved {_saved_at(memo)}). "
        f"If substantial work followed, extend it: add a dated section, then re-send the "
        f"COMPLETE observation set (earlier + new) in one "
        f"`memex backfill obs --stdin --replace --doc-path \"{_vault_rel(memo, vault)}\"` call."
    )


def marked_saved_message(session_id: str) -> str:
    return (
        "📚 Session compacted. `memex mark-saved` recorded a memo for this session, but no memo "
        f"is stamped with its id. If substantial work followed, extend that memo (dated section) "
        f"and add `session_id: {session_id}` to its frontmatter."
    )


def recovery_message(session_id: str, transcript_path: str, project: str,
                     forked_from: str | None = None) -> str:
    summaries = get_state_dir() / "compact-summaries" / session_id
    fork = (f" This session was forked from {forked_from}: cover only the work after the fork."
            if forked_from else "")
    return (
        f"📚 Session compacted. ⚠️ **Memo needed**: no memo is stamped with this session's id. "
        f"Spawn ONE background subagent with the Agent tool (subagent_type 'general-purpose', "
        f"model 'sonnet', run_in_background true) whose prompt says: follow `{RECOVERY_RECIPE}` "
        f"for session `{session_id}`, project `{project}`; read the compaction summaries in "
        f"`{summaries}/` first, the transcript `{transcript_path}` second.{fork}"
    )


def handle_post_compact(session_id: str, transcript_path: str, cwd: str, *,
                        forked_from: str | None = None) -> None:
    signal = _own_signal(session_id)
    project = str(signal.get("project") or (detect_project(cwd) if cwd else "_uncategorized"))
    vault = _vault()
    memo = session_memo(vault, session_id, project=project, hint=recorded_memo(session_id))
    if memo is not None and vault is not None:
        context = memo_exists_message(memo, vault)
    elif is_session_processed(session_id, "memo_generated"):
        context = marked_saved_message(session_id)
    else:
        transcript = transcript_path or str(signal.get("transcript_path", "") or "")
        parent = forked_from or signal.get("forked_from")
        context = recovery_message(session_id, transcript, project,
                                   parent if isinstance(parent, str) else None)
    output_context(context)
    log_info(f"Post-compact injection ({len(context)} chars, memo={'yes' if memo else 'no'})")


# ── session-start pointer (I5) ───────────────────────────────────────────

def _live_overview(vault: Path, project: str) -> tuple[str, Path, dict, str] | None:
    """(project, path, frontmatter, text) of the project's overview, following
    ``redirect_to`` from a consolidated folder; None for archived or missing."""
    where = project
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        path = vault / "projects" / where / "_project.md"
        if not path.is_file():
            return None
        fm = _frontmatter(path)
        if not isinstance(fm, dict):
            return None
        if fm.get("redirect_to") is not None:
            target = _redirect_target(vault, where)  # the reconcile-orphans reader
            if not target or target.startswith(".") or target == where:
                return None
            where = target
            continue
        if str(fm.get("status", "")).strip().lower() == "archived":
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        return where, path, fm, text
    return None


def _stamp_date(value: object) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str) and value.strip():
        return value.strip()[:10]
    return None


def is_real_overview(fm: dict, text: str) -> bool:
    """Condensed (stamped) or maintained by hand; not SessionEnd's template stub."""
    if any(_stamp_date(fm.get(k)) for k in ("condensed", "updated")) or fm.get("memos_digested"):
        return True
    return _TEMPLATE_MARKER not in text and text.count("\n") > _MAINTAINED_LINES


def open_thread_count(text: str) -> int:
    """Unchecked top-level bullets under the overview's ``## Open Threads``
    (its ``###`` sub-groups included; fenced code ignored)."""
    count, inside, fenced = 0, False, False
    for line in text.split("\n"):
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r"(#{1,2}) (.*)", line)
        if heading:
            inside = len(heading.group(1)) == 2 and heading.group(2).strip().lower().startswith("open threads")
            continue
        if inside and re.match(r"[-*+] (?!\[[xX]\])\S", line):
            count += 1
    return count


def last_memo(vault: Path, project: str) -> tuple[str, str] | None:
    """(date, harness) of the project's newest memo by filename date."""
    memo_dir = vault / "projects" / project / "memos"
    dated = [(d, m.name, m) for m in memo_dir.glob("*.md") if (d := _date_from_name(m.name))]
    if not dated:
        return None
    day, _, newest = max(dated)
    fm = _frontmatter(newest)
    harness = fm.get("harness") if isinstance(fm, dict) else None
    harness = harness.strip() if isinstance(harness, str) and harness.strip() else "claude-code"
    return day.isoformat(), harness


def session_pointer(cwd: str, input_data: dict) -> str | None:
    """One line (≤ 300 chars) pointing a project session at its overview."""
    if not cwd or input_data.get("agent_type") or _in_vault(cwd):
        return None
    if get_config().get("session_start_pointer", True) is False:
        return None
    vault = _vault()
    if vault is None:
        return None
    project = detect_project(cwd)
    if not project or project.startswith("_"):
        return None
    found = _live_overview(vault, project)
    if found is None:
        return None
    project, _, fm, text = found
    if not is_real_overview(fm, text):
        return None
    condensed, updated = _stamp_date(fm.get("condensed")), _stamp_date(fm.get("updated"))
    overview = (f"overview condensed {condensed}" if condensed
                else f"overview updated {updated}" if updated else "overview unstamped")
    threads = open_thread_count(text)
    parts = [overview, f"{threads} open thread{'' if threads == 1 else 's'}"]
    newest = last_memo(vault, project)
    if newest:
        parts.append(f"last memo {newest[0]} ({newest[1]})")
    read = f"`memex read projects/{project}/_project.md`"
    line = f"memex: {project} — {', '.join(parts)}. {read}"
    while len(line) > POINTER_MAX_CHARS and len(parts) > 1:
        parts.pop()
        line = f"memex: {project} — {', '.join(parts)}. {read}"
    return line if len(line) <= POINTER_MAX_CHARS else None


# ── filing receipts (I6) ─────────────────────────────────────────────────

RECEIPTS_CURSOR = "receipts-shown.json"
_RECEIPT_RULE_WORDS = {"leaf": "folder name", "fallback": "no rule matched"}


def _receipt_reason(rec: dict) -> str | None:
    chan = rec.get("unmapped_channel")
    if isinstance(chan, str) and chan:
        return f"unmapped relay channel {chan}"
    rule = rec.get("rule")
    if rule in GUESS_RULES:
        return _RECEIPT_RULE_WORDS.get(rule, str(rule))
    return None


def receipts_notice(state_dir: Path, *, advance: bool = True) -> str | None:
    """One line naming sessions SessionEnd filed by a guess since the last notice.

    SessionEnd can't show anything (Claude Code discards its output), so it
    leaves receipts in ``last-archives.jsonl`` and the next vault session says
    where guesses landed — the 2026-10-28 case: a relay from a mapped-later
    channel re-formed a catch-all folder within an hour of a cleanup.
    """
    cursor_path = state_dir / RECEIPTS_CURSOR
    try:
        seen_until = json.loads(cursor_path.read_text()).get("until", "")
    except (OSError, ValueError, AttributeError):
        seen_until = ""
    receipts = [r for r in read_filing_receipts(state_dir, limit=500)
                if str(r.get("ts", "")) > seen_until]
    if not receipts:
        return None
    groups: dict[tuple[str, str], int] = {}
    for rec in receipts:
        reason = _receipt_reason(rec)
        if reason:
            key = (str(rec.get("project", "?")), reason)
            groups[key] = groups.get(key, 0) + 1
    if advance:
        try:
            tmp = cursor_path.with_name(cursor_path.name + ".tmp")
            tmp.write_text(json.dumps({"until": max(str(r.get("ts", "")) for r in receipts)}))
            os.replace(tmp, cursor_path)
        except OSError:
            pass
    if not groups:
        return None
    parts = [f"projects/{proj}/ ({reason}){f' ×{n}' if n > 1 else ''}"
             for (proj, reason), n in sorted(groups.items(), key=lambda kv: -kv[1])[:5]]
    more = len(groups) - len(parts)
    return ("memex: since the last vault session, SessionEnd filed by a guess: " + "; ".join(parts)
            + (f"; and {more} more" if more > 0 else "")
            + ". Map relay channels in ~/.memex/config.json `channel_mappings`; `memex check --channels` / `--folders` list them.")


# ── entry point ──────────────────────────────────────────────────────────

def main():
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    source = input_data.get("source", "startup")
    cwd = input_data.get("cwd", "")
    transcript_path = input_data.get("transcript_path", "")

    log_info(f"SessionStart hook triggered: session={session_id[:8] if session_id else 'unknown'}..., source={source}")

    facts = transcript_facts(transcript_path)
    if is_helper_session(facts):
        log_info("Helper session (sdk-cli): no memex context")
        sys.exit(0)

    if source == "compact":
        if session_id:
            log_info("Post-compaction, checking for memo")
            handle_post_compact(session_id, transcript_path, cwd, forked_from=facts["forked_from"])
        sys.exit(0)

    messages: list[str] = []
    if source != "resume":
        try:
            pointer = session_pointer(cwd, input_data)
        except Exception as e:  # a pointer is never worth a failed session start
            log_warning(f"Session pointer skipped: {e}")
            pointer = None
        if pointer:
            messages.append(pointer)

    # A fork (source, or a transcript of records copied from a parent): the
    # parent session owns whatever memo work is pending.
    if source != "fork" and not facts["forked_from"]:
        notice = pending_notice(cwd, resume=source == "resume")
        if notice:
            messages.append(notice)

    if source != "resume":
        rejection = sidecar_rejection_notice()
        if rejection:
            messages.append(rejection)
            log_warning("Startup injection: rejected observation sidecars")

    user_notice = None
    if source != "resume" and _in_vault(cwd):
        try:
            user_notice = receipts_notice(get_state_dir())
        except Exception as e:  # a receipt notice is never worth a failed session start
            log_warning(f"Receipt notice skipped: {e}")
        if user_notice:
            messages.append(user_notice)  # the curator can act on it too

    if messages or user_notice:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": "\n\n".join(messages)}}
        if user_notice:
            out["systemMessage"] = user_notice
        print(json.dumps(out))

    sys.exit(0)


if __name__ == "__main__":
    main()
