"""Project-folder drift audit — detect the class of drift v0.15.5/v0.15.6 fixed.

Read-only. Flags, per ``projects/<name>/`` folder:
  - **fragment** names (cwd-path fragments like ``Apps-arena`` that didn't
    collapse to their leaf), via ``utils.looks_like_cwd_fragment``;
  - **mismatch** — folder name != ``detect_project(cwd)`` where ``cwd`` is the
    true working directory recovered from a memo's ``source_cwd`` frontmatter or
    an archived transcript JSONL;
  - **subset / duplicate** folders — one folder's content (memo filenames +
    transcript session ids) is a subset of another's (the ``personal-website ⊂
    example_com`` case), or two folders canonicalize to the same project.

v0.22.0 adds the classes the 2026-10-28 pass found by hand (``memex check
--folders`` flagged none of the 19 folders it had to move). Each rests on
transcript evidence — the entrypoint and cwd of the folder's own sessions — and
the report says WHY a folder is flagged:
  - **fleet-seat folder** — EVERY transcript is an ``sdk-cli`` session from a
    temp cwd (``/private/var/folders/*/T/…``, ``/tmp/…``): a mkdtemp dir minted
    a project folder. HIGH when the name extends an existing project or each
    seat had its own temp dir; REVIEW when one stable temp checkout hosts them
    all (a real project run from /tmp looks like that). Sessions that are only
    sdk-cli, or only from a temp dir, prove nothing and are never flagged.
  - **name extends a project** — ``<existing-project>-<suffix>`` on a fleet-seat
    folder. The name alone is never flagged: ``memex`` / ``memex-cloud`` is a
    sibling project, not drift.
  - **catch-all cwd** — a folder whose cwd is the parent of two or more other
    projects' cwds (``~/Documents/GitHub``, a Dropbox root). HIGH when relay
    (claude.ai worker) sessions are filed there by cwd instead of by their
    channel mapping, REVIEW otherwise.
  - **shared cwd** — two folders holding sessions from one cwd (``alpha`` /
    ``alpha-internal``). HIGH when one side is only ``sdk-cli`` seats and the
    other has interactive sessions; REVIEW otherwise. Relay, temp and catch-all
    cwds are excluded (sharing them is expected).

``memex check --channels`` (C3) lists relay sessions whose claude.ai channel is
missing from config ``channel_mappings`` — one decision per claude.ai project.
The channel comes from transcript frontmatter ``relay_channel`` or, for earlier
archives, the ``<project id="chan_…">`` tag in the opening turns of the .md.

For each drift folder it prints the exact ``memex obs reassign`` commands to
consolidate — with the bright-line reminder to confirm duplication first. Never
mutates vault content (memos/observations); it only reads the index for obs
counts, and only when ``_index.sqlite`` already exists. Exposed as
``memex check --folders`` / ``--channels`` (``--json`` for agents; exits
non-zero when high-confidence drift / an unmapped channel is found, for
CI/launchd).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from memex.paths import get_index_path, get_memex_path
from memex.scripts.utils import (
    detect_project,
    get_config,
    looks_like_cwd_fragment,
    sanitize_project_name,
)

_SOURCE_CWD_RE = re.compile(r"^source_cwd:\s*(.+?)\s*$", re.MULTILINE)
_DATE_PREFIX_RE = re.compile(r"^\d{8}-\d{6}-")


def _read_source_cwd(md: Path) -> str | None:
    """Pull ``source_cwd`` from a memo's YAML frontmatter (cheap, head only)."""
    if not _readable(md):
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    head = text if end == -1 else text[:end]
    m = _SOURCE_CWD_RE.search(head)
    if m:
        return (m.group(1).strip().strip("\"'")) or None
    return None


def _cwd_for_project(project_dir: Path) -> str | None:
    """Recover the true cwd that produced a vault project folder.

    Prefers a memo's ``source_cwd`` frontmatter; falls back to a transcript
    JSONL's ``cwd`` field. (New writes persist ``source_cwd`` so this stays
    index-native; older content needs the JSONL read.)
    """
    memos = project_dir / "memos"
    if memos.is_dir():
        for md in sorted(memos.glob("*.md")):
            cwd = _read_source_cwd(md)
            if cwd:
                return cwd
    tr = project_dir / "transcripts"
    if tr.is_dir():
        for jf in sorted(tr.glob("*.jsonl")):
            cwd = _jsonl_head_facts(jf)[1]
            if cwd:
                return cwd
    return None


def _content_keys(project_dir: Path) -> set[str]:
    """Identity set for subset/dup detection: memo filenames + transcript
    session ids (date prefix stripped so memex's renamed transcripts match)."""
    keys: set[str] = set()
    memos = project_dir / "memos"
    if memos.is_dir():
        keys |= {f.name for f in memos.glob("*.md")}
    tr = project_dir / "transcripts"
    if tr.is_dir():
        for f in tr.iterdir():
            if f.suffix not in (".md", ".jsonl"):
                continue
            sid = _DATE_PREFIX_RE.sub("", f.stem)
            if sid:
                keys.add("session:" + sid)
    return keys


# --- v0.22.0: transcript evidence (entrypoint + cwd per session) -------------

# mkdtemp / scratch roots: macOS per-user temp (/private/var/folders/xx/yyyy/T/),
# /tmp and /var/tmp (all optionally under /private).
_TEMP_CWD_RE = re.compile(
    r"^(?:/private)?(?:/var/folders/[^/]+/[^/]+/[A-Za-z]|/tmp|/var/tmp)/"
)
# Claude Code's per-session scratchpad; its temp root can be moved by
# CLAUDE_CODE_TMPDIR, so the ``claude-<uid>/<enc>/<uuid>/scratchpad`` tail decides.
_SCRATCHPAD_TAIL_RE = re.compile(r"/claude-\d+/-[^/]+/[0-9a-f-]{36}/scratchpad(?:/|$)")
_SF_DATALESS = 0x40000000  # macOS st_flags: iCloud evicted the bytes
_FM_LINE_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$")
_NAME_SEPARATORS = ("-", "_")
_MIN_BASE_LEN = 3  # shortest folder name a ``<base>-<suffix>`` match may hang off


def is_temp_cwd(cwd: str | None) -> bool:
    """True for a cwd under a temp root (mkdtemp fleet dirs, /tmp scratch)."""
    if not cwd:
        return False
    cwd = cwd.rstrip("/") + "/"
    return bool(_TEMP_CWD_RE.match(cwd) or _SCRATCHPAD_TAIL_RE.search(cwd))


def _readable(path: Path) -> bool:
    """False for files that cannot be read without blocking: missing, or an
    iCloud dataless placeholder (reading one waits on ``fileproviderd``)."""
    try:
        return not (getattr(path.stat(), "st_flags", 0) & _SF_DATALESS)
    except OSError:
        return False


def _frontmatter_head(path: Path) -> dict[str, str]:
    """Top-level ``key: value`` lines of a markdown file's frontmatter (head only)."""
    return _frontmatter_head_checked(path) or {}


def _frontmatter_head_checked(path: Path) -> dict[str, str] | None:
    """Like ``_frontmatter_head``, but ``None`` when the file can't be read, so a
    caller that must not mistake "unreadable" for "no frontmatter" can tell
    (gauntlet round 1: a read failure after the stat check read as all-clear)."""
    if not _readable(path):
        return None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            text = fh.read(4000)
    except OSError:
        return None
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    head = text if end == -1 else text[:end]
    out: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        m = _FM_LINE_RE.match(line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip("\"'"))
    return out


def _parse_entrypoints(raw: str | None) -> frozenset[str]:
    """``sdk-cli`` or ``[cli, sdk-cli]`` (the stamped scalar or sorted list)."""
    if not raw:
        return frozenset()
    return frozenset(p.strip().strip("\"'") for p in raw.strip("[]").split(",") if p.strip())


def _jsonl_head_facts(path: Path) -> tuple[frozenset[str], str | None]:
    """``(entrypoints, cwd)`` from the first lines of an archived session JSONL."""
    if not _readable(path):
        return frozenset(), None
    eps: set[str] = set()
    cwd = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i >= 200:
                    break
                if '"cwd"' not in line and '"entrypoint"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                c = obj.get("cwd")
                if cwd is None and isinstance(c, str) and c:
                    cwd = c
                ep = obj.get("entrypoint")
                if isinstance(ep, str) and ep:
                    eps.add(ep)
                if cwd and eps:
                    break
    except OSError:
        pass
    return frozenset(eps), cwd


@dataclass(frozen=True)
class _Session:
    """What a project folder knows about one session: how it ran and where."""

    key: str
    entrypoints: frozenset[str]  # empty = unknown
    cwd: str | None
    relay: bool = False  # claude.ai relay worker (entrypoint claude-desktop / relay_channel)
    channel: str | None = None  # its claude.ai channel (``chan_…``) when one could be read

    @property
    def sdk_only(self) -> bool:
        return self.entrypoints == frozenset({"sdk-cli"})

    @property
    def temp(self) -> bool:
        return is_temp_cwd(self.cwd)

    @property
    def strong_seat(self) -> bool:
        return self.sdk_only and self.temp

    @property
    def seat_like(self) -> bool:
        return self.sdk_only or self.temp


def _scan_sessions(project_dir: Path) -> list[_Session]:
    """One ``_Session`` per archived transcript (md and jsonl are one session).

    The .md frontmatter supplies ``entrypoint`` (and ``relay_channel``); the cwd
    is not in it, so it is read from the JSONL head. Files are never read when
    iCloud has evicted them."""
    tr = project_dir / "transcripts"
    if not tr.is_dir():
        return []
    by_key: dict[str, dict[str, Path]] = {}
    for f in sorted(tr.iterdir()):
        if f.suffix in (".md", ".jsonl"):
            by_key.setdefault(_DATE_PREFIX_RE.sub("", f.stem), {})[f.suffix] = f
    sessions: list[_Session] = []
    for key, files in by_key.items():
        eps: frozenset[str] = frozenset()
        cwd = None
        channel = None
        md = files.get(".md")
        if md:
            fm = _frontmatter_head(md)
            eps = _parse_entrypoints(fm.get("entrypoint"))
            cwd = fm.get("source_cwd") or fm.get("cwd") or None
            channel = fm.get("relay_channel") or None
            if channel and not _CHANNEL_ID_RE.match(channel):
                channel = None
        js = files.get(".jsonl")
        if js and (not eps or not cwd):
            j_eps, j_cwd = _jsonl_head_facts(js)
            eps = eps or j_eps
            cwd = cwd or j_cwd
        relay = bool(channel) or "claude-desktop" in eps
        if relay and not channel and md:
            channel = _channel_from_opening_turns(md)  # earlier archives: raw first turns
        sessions.append(_Session(key, eps, cwd, relay=relay, channel=channel))
    return sessions


def _memo_cwds(project_dir: Path) -> list[str]:
    memos = project_dir / "memos"
    if not memos.is_dir():
        return []
    out = []
    for md in sorted(memos.glob("*.md")):
        if _readable(md):
            cwd = _read_source_cwd(md)
            if cwd:
                out.append(cwd)
    return out


def _norm(cwd: str) -> str:
    return cwd.rstrip("/") or "/"


def _under(child: str, parent: str) -> bool:
    return child.startswith(parent.rstrip("/") + "/")


def _finding(kind: str, confidence: str, reason: str, target: str | None = None) -> dict:
    return {"kind": kind, "confidence": confidence, "reason": reason, "target": target}


def _classify_evidence(
    facts: dict[str, dict], channel_mappings: dict | None = None
) -> dict[str, list[dict]]:
    """Findings per folder from the sessions' entrypoints and cwds.

    ``facts[name] = {"sessions": [_Session], "memo_cwds": [str]}``. A folder with
    no transcripts falls back to its memos' ``source_cwd`` as (entrypoint-less)
    sessions, so memo-only folders still count for the cwd classes.

    ``channel_mappings`` (config ``channel_mappings``) says where a relay worker
    BELONGS: one filed in the project its channel maps to is placed correctly and
    is not evidence of anything; its cwd is only a place to stand."""
    mapped = {
        k: sanitize_project_name(v)
        for k, v in (channel_mappings or {}).items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }

    def placed(s: _Session, folder: str) -> bool:
        return s.relay and s.channel is not None and mapped.get(s.channel) == folder

    eff: dict[str, list[_Session]] = {}
    for name, f in facts.items():
        sess = f["sessions"]
        if not sess:
            sess = [_Session(f"memo:{i}", frozenset(), c) for i, c in enumerate(f["memo_cwds"])]
        eff[name] = sess
    findings: dict[str, list[dict]] = {n: [] for n in facts}

    # (a) fleet-seat folders: EVERY session is an sdk-cli session from a temp cwd.
    # Sessions that are only sdk-cli (a headless pipeline) or only from a temp dir
    # (a scratch experiment) are not evidence of a minted folder.
    all_strong: set[str] = set()
    for name, sess in eff.items():
        if not sess or not all(s.strong_seat for s in sess):
            continue
        # A memo recorded from a real cwd is positive evidence of real work.
        if facts[name]["sessions"] and any(not is_temp_cwd(c) for c in facts[name]["memo_cwds"]):
            continue
        all_strong.add(name)

    # (b) the existing project a seat folder's name extends: <project>-<suffix>.
    def base_for(name: str) -> str | None:
        bases = [
            b for b in facts
            if b != name and b not in all_strong and len(b) >= _MIN_BASE_LEN
            and not b.startswith("_")
            and any(name.startswith(b + sep) and len(name) > len(b) + 1 for sep in _NAME_SEPARATORS)
        ]
        return max(bases, key=len) if bases else None

    seat_only: set[str] = set()  # fleet-seat folders (any confidence): not "projects"
    for name in sorted(all_strong):
        sess = eff[name]
        n = len(sess)
        temp_dirs = sorted({_norm(s.cwd) for s in sess})
        base = base_for(name)
        if base:
            conf, why = "high", f"its name extends existing project '{base}'"
        elif len(temp_dirs) == n:
            conf, why = "high", "each session had its own temp dir (the mkdtemp signature)"
        else:
            conf, why = "review", (f"{n} sessions share {len(temp_dirs)} temp dir(s) — a stable temp "
                                   f"checkout hosts them, so this may be a real project run from /tmp")
        seat_only.add(name)
        findings[name].append(_finding(
            "fleet-seat", conf,
            f"all {n} session(s) are sdk-cli fleet seats that ran from temp-dir cwds "
            f"(e.g. {temp_dirs[0]}); {why}",
        ))
        if base:
            findings[name].append(_finding(
                "name-extends", conf,
                f"name extends existing project '{base}' — the seats likely belong in '{base}'",
                target=base,
            ))

    # Project cwds: where non-seat folders' own (non-relay) sessions actually ran.
    project_cwds: dict[str, set[str]] = {
        name: {_norm(s.cwd) for s in sess if s.cwd and not s.temp and not s.relay}
        for name, sess in eff.items() if name not in seat_only
    }

    # (c) catch-all cwd: the folder's cwd is the parent of >= 2 other projects' cwds.
    catch_all_cwds: set[str] = set()
    for name, sess in eff.items():
        at_cwd: dict[str, list[_Session]] = {}
        for s in sess:
            if s.cwd and not s.temp and len(_norm(s.cwd)) > 1 and not placed(s, name):
                at_cwd.setdefault(_norm(s.cwd), []).append(s)
        for cwd, ss in sorted(at_cwd.items()):
            below_cwds = {
                (g, c) for g, cs in project_cwds.items() if g != name
                for c in cs if _under(c, cwd)
            }
            below = sorted({g for g, _ in below_cwds})
            # Two folders on ONE sub-directory are a shared cwd (d), not a parent.
            if len(below) < 2 or len({c for _, c in below_cwds}) < 2:
                continue
            catch_all_cwds.add(cwd)
            relay_n = sum(s.relay for s in ss)
            shown = ", ".join(f"'{g}'" for g in below[:4]) + (", …" if len(below) > 4 else "")
            reason = (f"{len(ss)} session(s) ran in {cwd}, the parent directory of {len(below)} other "
                      f"projects' cwds ({shown}) — sessions started here can't be attributed to one project")
            if relay_n:
                reason += (f"; {relay_n} of them claude.ai relay worker(s) filed by cwd, not by "
                           f"channel mapping (see `memex check --channels`)")
            findings[name].append(_finding("catch-all", "high" if relay_n else "review", reason))

    # (d) two folders, one cwd. Relay, temp and catch-all cwds are expected to be
    # shared (relay workers "stand" anywhere; sharing says nothing about identity).
    by_cwd: dict[str, dict[str, list[_Session]]] = {}
    for name, sess in eff.items():
        for s in sess:
            if not s.cwd or s.temp or s.relay:
                continue
            by_cwd.setdefault(_norm(s.cwd), {}).setdefault(name, []).append(s)
    for cwd, folders in sorted(by_cwd.items()):
        if len(folders) < 2 or cwd in catch_all_cwds:
            continue
        sdk_only_side = {n for n, ss in folders.items() if all(s.sdk_only for s in ss)}
        home_side = {n for n in folders if n not in sdk_only_side}
        if sdk_only_side and home_side:
            # sorted() first: ties on session count must not depend on set order
            home = max(sorted(home_side), key=lambda n: len(folders[n]))
            for n in sorted(sdk_only_side):
                findings[n].append(_finding(
                    "shared-cwd", "high",
                    f"{len(folders[n])} sdk-cli seat session(s) here ran in {cwd}, the same cwd as "
                    f"{len(folders[home])} non-seat session(s) in '{home}' — the seats were "
                    f"filed under a second name for one directory",
                    target=home,
                ))
            continue
        for n in sorted(folders):
            others = ", ".join(f"'{o}' ({len(folders[o])})" for o in sorted(folders) if o != n)
            findings[n].append(_finding(
                "shared-cwd", "review",
                f"{len(folders[n])} session(s) ran in {cwd}, which also holds sessions filed in {others} "
                f"— one directory, two folders (rename, split or remote change?)",
            ))
    return findings


def audit_folders(vault: Path, channel_mappings: dict | None = None) -> dict:
    """Build the drift report over ``projects/<name>/`` (no mutation).

    ``channel_mappings`` is config ``channel_mappings``; without it every relay
    worker counts as filed by cwd."""
    proj_root = vault / "projects"
    dirs = sorted(p for p in proj_root.iterdir() if p.is_dir()) if proj_root.is_dir() else []

    # Open the index only if it already exists — connect_index sets WAL pragmas
    # (a write), so we must not create one from a read-only audit.
    conn = None
    idx = get_index_path(vault)
    if idx.exists():
        try:
            from memex.db_utils import connect_index
            conn = connect_index(idx)
        except Exception:
            conn = None

    def obs_count(name: str):
        if not conn:
            return None
        # Prefix-range, NOT LIKE: '_' and '%' are LIKE wildcards and project
        # names contain underscores (sanitize_project_name), so LIKE would
        # mis-count sibling folders.
        prefix = f"projects/{name}/"
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM observations WHERE doc_path >= ? AND doc_path < ?",
                (prefix, prefix + "￿"),
            ).fetchone()[0]
        except Exception:
            return None

    records: dict[str, dict] = {}
    facts: dict[str, dict] = {}
    for pd in dirs:
        name = pd.name
        cwd = _cwd_for_project(pd)
        sessions = _scan_sessions(pd)
        # A relay worker's cwd is "a place to stand", not the project's identity:
        # once a channel mapping files it under its project, that cwd must not
        # make the folder read as name≠canonical.
        if cwd and sessions:
            native = {_norm(s.cwd) for s in sessions if s.cwd and not s.relay}
            relay = {_norm(s.cwd) for s in sessions if s.cwd and s.relay}
            if _norm(cwd) in relay and _norm(cwd) not in native:
                cwd = next((s.cwd for s in sessions if s.cwd and not s.relay), None)
        canonical = detect_project(cwd) if cwd else None
        facts[name] = {"sessions": sessions, "memo_cwds": _memo_cwds(pd)}
        records[name] = {
            "name": name,
            "cwd": cwd,
            "canonical": canonical,
            "is_fragment": looks_like_cwd_fragment(name),
            "mismatch": bool(canonical and canonical != name),
            "obs": obs_count(name),
            "keys": _content_keys(pd),
            "subset_of": [],
            "sessions": len(sessions),
            "relay_sessions": sum(s.relay for s in sessions),
            "findings": [],
        }

    for name, found in _classify_evidence(facts, channel_mappings).items():
        records[name]["findings"] = found

    # subset/duplicate: A ⊆ B. Use STRICT subset so identical folders don't each
    # claim the other (which would print circular A→B and B→A reassigns); for
    # exactly-equal content, flag only one side deterministically (a > b).
    names = list(records)
    for a in names:
        ka = records[a]["keys"]
        if not ka:
            continue
        for b in names:
            if a == b:
                continue
            kb = records[b]["keys"]
            if ka < kb:
                records[a]["subset_of"].append(b)
            elif ka == kb and a > b:
                records[a]["subset_of"].append(b)

    # canonical collisions: >1 folder resolving to the same project
    by_canon: dict[str, list[str]] = {}
    for n, r in records.items():
        if r["canonical"]:
            by_canon.setdefault(r["canonical"], []).append(n)
    collisions = {c: ns for c, ns in by_canon.items() if len(ns) > 1}

    if conn:
        conn.close()

    # strip the big key sets from the serializable record
    for r in records.values():
        r["content_count"] = len(r.pop("keys"))
    return {"records": records, "collisions": collisions}


def _has_high_finding(r: dict) -> bool:
    return any(f["confidence"] == "high" for f in r.get("findings", ()))


def _drift_folders(report: dict) -> dict:
    """High-confidence drift: cwd-fragment-shaped names, content that is a strict
    subset of another folder (a true duplicate like personal-website ⊂
    example_com), or transcript evidence that cannot be a real project (v0.22.0:
    fleet-seat folders, ``<project>-<suffix>`` seat folders, relay workers filed
    in a catch-all cwd, seats filed beside the interactive folder of one cwd).
    These are safe-to-act-on signals."""
    return {
        n: r for n, r in report["records"].items()
        if r["is_fragment"] or r["subset_of"] or _has_high_finding(r)
    }


def _review_folders(report: dict, drift: dict) -> dict:
    """Lower-confidence: name≠canonical (git remote / recovered cwd can legitimately
    differ from a deliberately-named folder), canonical collisions, or review-level
    transcript findings (mixed seat evidence, a catch-all cwd, two folders on one
    cwd). Surface for HUMAN review, not auto-action."""
    coll = {n for ns in report["collisions"].values() for n in ns}
    return {
        n: r for n, r in report["records"].items()
        if n not in drift and (r["mismatch"] or n in coll or r.get("findings"))
    }


_KIND_LABEL = {
    "fleet-seat": "fleet-seat folder",
    "name-extends": "name extends a project",
    "catch-all": "catch-all cwd",
    "shared-cwd": "shares a cwd with another folder",
}


def _finding_labels(r: dict) -> list[str]:
    labels = []
    for f in r.get("findings", ()):
        label = _KIND_LABEL[f["kind"]]
        if f["kind"] == "name-extends" and f.get("target"):
            label = f"name extends '{f['target']}'"
        if label not in labels:
            labels.append(label)
    return labels


def _print_findings(r: dict, indent: str) -> None:
    for f in r.get("findings", ()):
        tag = "" if f["confidence"] == "high" else " (review-level)"
        print(f"{indent}why: {f['reason']}{tag}")


def _folder_payload(report: dict) -> dict:
    drift = _drift_folders(report)
    return {
        "drift_count": len(drift),
        "drift": drift,
        "review": _review_folders(report, drift),
        "collisions": report["collisions"],
    }


def run_folder_audit(json_out: bool = False) -> int:
    """Print the folder-drift report. Returns 1 iff high-confidence drift found."""
    vault = get_memex_path()
    report = audit_folders(vault, get_config().get("channel_mappings") or {})
    drift = _drift_folders(report)
    review = _review_folders(report, drift)

    if json_out:
        print(json.dumps(_folder_payload(report), indent=2))
        return 1 if drift else 0

    total = len(report["records"])
    if not drift and not review:
        print(f"✓ No project-folder drift across {total} folders.")
        return 0

    if drift:
        print(f"⚠ {len(drift)} folder(s) with high-confidence drift:\n")
        for name, r in sorted(drift.items()):
            issues = []
            if r["is_fragment"]:
                issues.append("cwd-fragment name")
            if r["subset_of"]:
                issues.append(f"content ⊆ {', '.join(r['subset_of'])}")
            issues += _finding_labels(r)
            obs = r["obs"] if r["obs"] is not None else "?"
            print(f"  • {name}  [{'; '.join(issues)}]  (obs={obs}, items={r['content_count']})")
            _print_findings(r, "      ")
            high = [f for f in r.get("findings", ()) if f["confidence"] == "high"]
            kinds = {f["kind"] for f in high}
            target = r["subset_of"][0] if r["subset_of"] else next(
                (f["target"] for f in high if f.get("target")), None)
            if target:
                print(f"      → CONFIRM it duplicates '{target}' (diff memos AND transcripts), "
                      f"then reassign by SUBPREFIX:")
                for sub in ("memos", "transcripts"):
                    print(f"          memex obs reassign --from-prefix \"projects/{name}/{sub}/\" "
                          f"--to-prefix \"projects/{target}/{sub}/\"   # dry-run; add --apply")
                print(f"        # WARNING: do NOT reassign \"projects/{name}/\" wholesale "
                      f"(collides on _project.md); verify the obs-count invariant after.")
            elif "catch-all" in kinds:
                print("      → map the claude.ai channels first (`memex check --channels`), then move "
                      "each relay session to its project by exact path (project-consolidation skill)")
            elif "fleet-seat" in kinds:
                print("      → fleet seats with no parent project in the vault: find the project that "
                      "launched them (the seat prompt, the temp dir's leaf name), reassign by "
                      "SUBPREFIX, and add a project_mappings pin if the cwd will recur")
            elif r["is_fragment"]:
                print(f"      → fragment with no clear in-vault duplicate; find its canonical "
                      f"home (git remote / project_mappings) before reassigning")

    if review:
        print(f"\n  {len(review)} folder(s) to REVIEW (lower confidence — verify manually, "
              f"git-remote/cwd can legitimately differ from a chosen folder name):")
        coll = {n for ns in report["collisions"].values() for n in ns}
        for name, r in sorted(review.items()):
            if r["mismatch"]:
                why = [f"name≠canonical ({r['canonical']})"]
            elif name in coll:
                why = ["canonical collision"]
            else:
                why = []
            why += _finding_labels(r)
            obs = r["obs"] if r["obs"] is not None else "?"
            print(f"    • {name}  [{'; '.join(why)}]  (obs={obs})")
            _print_findings(r, "        ")

    print("\n  SOP: confirm duplication (filenames AND content) before any reassign; "
          "verify the obs-count invariant after. See the project-consolidation skill.")
    return 1 if drift else 0


# --- v0.22.0 C3: relay sessions whose claude.ai channel is not mapped ----------

_CHANNEL_ID_RE = re.compile(r"^chan_[A-Za-z0-9]+$")
# ``<project id="chan_…">`` as archived: raw, HTML-escaped (``&lt;`` / ``&quot;``)
# or JSON-escaped (``\"``).
_PROJECT_TAG_RE = re.compile(
    r"(?:<|&lt;)project\s+id\s*=\s*(?:\\?[\"']|&quot;|&#0*34;|&#0*39;|&apos;)(chan_[A-Za-z0-9]+)"
)
_TURN_HEAD_RE = re.compile(r"^## Turn (\d+)\b")
_OPENING_TURNS = 3  # the relay's wake message sits in the first user turns of the .md
_BODY_SCAN_BYTES = 4_000_000


def _channel_from_opening_turns(md: Path) -> str | None:
    """The ``<project id="chan_…">`` tag in a transcript's opening turns.

    Bounded: stops at turn 4 or ``_BODY_SCAN_BYTES``, so a long session that
    merely quotes a channel id later on cannot be mis-attributed."""
    if not _readable(md):
        return None
    seen = 0
    try:
        with md.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                seen += len(line)
                if seen > _BODY_SCAN_BYTES:
                    return None
                turn = _TURN_HEAD_RE.match(line)
                if turn and int(turn.group(1)) > _OPENING_TURNS:
                    return None
                m = _PROJECT_TAG_RE.search(line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def _session_date(fm: dict[str, str]) -> str | None:
    # start_time only: frontmatter ``date`` is the ARCHIVE day, which would
    # misstate a channel's span for a session with a single timestamp.
    return (fm.get("start_time") or "").split("T")[0] or None


def audit_channels(vault: Path, mappings: dict | None = None) -> dict:
    """Group the vault's claude.ai relay sessions by channel (no mutation).

    A relay session is a transcript with frontmatter ``relay_channel`` or
    ``entrypoint: claude-desktop``. Its channel is the frontmatter value, else
    the ``<project id>`` tag in the opening turns of the .md (earlier archives).
    A claude-desktop transcript with no channel anywhere is reported separately
    — there is nothing to decide about it."""
    mapped = {
        k: sanitize_project_name(v)
        for k, v in (mappings or {}).items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }
    proj_root = vault / "projects"
    dirs = sorted(p for p in proj_root.iterdir() if p.is_dir()) if proj_root.is_dir() else []
    channels: dict[str, dict] = {}
    unresolved: list[str] = []
    unreadable: list[str] = []
    relay_total = 0
    for pd in dirs:
        tr = pd / "transcripts"
        if not tr.is_dir():
            continue
        for md in sorted(tr.glob("*.md")):
            if not _readable(md):
                # An evicted transcript might be a relay session: say the audit
                # is incomplete rather than let the rest read as an all-clear.
                unreadable.append(md.relative_to(vault).as_posix())
                continue
            fm = _frontmatter_head_checked(md)
            if fm is None:
                unreadable.append(md.relative_to(vault).as_posix())
                continue
            fm_channel = fm.get("relay_channel") or ""
            if fm_channel and not _CHANNEL_ID_RE.match(fm_channel):
                fm_channel = ""
            if not fm_channel and "claude-desktop" not in _parse_entrypoints(fm.get("entrypoint")):
                continue
            relay_total += 1
            rel = md.relative_to(vault).as_posix()
            channel = fm_channel or _channel_from_opening_turns(md)
            if not channel:
                unresolved.append(rel)
                continue
            entry = channels.setdefault(channel, {
                "channel": channel,
                "mapped_to": mapped.get(channel),
                "sessions": 0,
                "folders": Counter(),
                "sources": Counter(),
                "dates": [],
                "paths": [],
            })
            entry["sessions"] += 1
            entry["folders"][pd.name] += 1
            entry["sources"]["frontmatter" if fm_channel else "opening-turns"] += 1
            if date := _session_date(fm):
                entry["dates"].append(date)
            entry["paths"].append(rel)

    for entry in channels.values():
        entry["folders"] = dict(sorted(entry["folders"].items(), key=lambda kv: (-kv[1], kv[0])))
        entry["sources"] = dict(entry["sources"])
        dates = sorted(entry.pop("dates"))
        entry["first"], entry["last"] = (dates[0], dates[-1]) if dates else (None, None)
        if entry["mapped_to"]:
            entry["misfiled"] = sum(n for f, n in entry["folders"].items() if f != entry["mapped_to"])
    ordered = sorted(channels.values(), key=lambda e: (-e["sessions"], e["channel"]))
    return {
        "relay_sessions": relay_total,
        "unmapped": [e for e in ordered if not e["mapped_to"]],
        "mapped": [e for e in ordered if e["mapped_to"]],
        "unresolved": unresolved,
        "unreadable": unreadable,
    }


def _channel_payload(report: dict) -> dict:
    return {
        "unmapped_channel_count": len(report["unmapped"]),
        "unmapped_session_count": sum(e["sessions"] for e in report["unmapped"]),
        "relay_sessions": report["relay_sessions"],
        "unmapped": report["unmapped"],
        "mapped": report["mapped"],
        "unresolved": report["unresolved"],
        "unreadable": report["unreadable"],
    }


def _channel_rc(report: dict) -> int:
    return 1 if report["unmapped"] or report["unreadable"] else 0


def run_channel_audit(json_out: bool = False) -> int:
    """Print the unmapped-relay-channel report. Returns 1 iff a channel needs a
    decision or the audit could not read every transcript (no false all-clear)."""
    vault = get_memex_path()
    report = audit_channels(vault, get_config().get("channel_mappings") or {})
    unmapped = report["unmapped"]
    rc = _channel_rc(report)
    if json_out:
        print(json.dumps(_channel_payload(report), indent=2))
        return rc

    mapped = report["mapped"]
    misfiled = sum(e.get("misfiled", 0) for e in mapped)
    outside = f"{misfiled} filed outside their mapped project (archived before the mapping)"
    if not unmapped:
        if mapped:
            print(f"✓ All {len(mapped)} relay channel(s) are in channel_mappings "
                  f"({sum(e['sessions'] for e in mapped)} session(s))"
                  + (f"; {outside}." if misfiled else "."))
        elif not report["relay_sessions"] and not report["unreadable"]:
            print("✓ No claude.ai relay sessions in the vault.")
        # else: only unresolved/unreadable sessions, described below
    else:
        n_sessions = sum(e["sessions"] for e in unmapped)
        print(f"⚠ {len(unmapped)} claude.ai channel(s) not in channel_mappings — "
              f"{n_sessions} relay session(s) filed by cwd:\n")
        for e in unmapped:
            span = ""
            if e["first"]:
                span = f", {e['first']}" if e["first"] == e["last"] else f", {e['first']} → {e['last']}"
            print(f"  • {e['channel']}  ({e['sessions']} session(s){span})")
            print("      filed in: " + ", ".join(f"{f} ({n})" for f, n in e["folders"].items()))
            src = ", ".join(f"{n} from {k}" for k, n in sorted(e["sources"].items()))
            print(f"      channel read: {src}")
            print(f"      → decide once; add to channel_mappings in ~/.memex/config.json:  "
                  f"\"{e['channel']}\": \"<project>\"")
    if mapped and unmapped:
        print(f"\n  Mapped: {len(mapped)} channel(s), {sum(e['sessions'] for e in mapped)} session(s); "
              f"{outside}.")
    if report["unresolved"]:
        print(f"\n  {len(report['unresolved'])} claude-desktop session(s) with no channel in the "
              f"frontmatter or opening turns (nothing to map): " + ", ".join(report["unresolved"][:3])
              + (", …" if len(report["unresolved"]) > 3 else ""))
    if report["unreadable"]:
        print(f"\n  ⚠ {len(report['unreadable'])} transcript(s) not read (iCloud placeholders or "
              f"unreadable) — this audit is INCOMPLETE: " + ", ".join(report["unreadable"][:3])
              + (", …" if len(report["unreadable"]) > 3 else ""))
    return rc


def main():
    parser = argparse.ArgumentParser(description="Audit vault project folders for detection drift")
    parser.add_argument("--json", action="store_true", help="JSON output for agents")
    parser.add_argument("--folders", action="store_true",
                        help="folder-drift audit (the default when no mode is given)")
    parser.add_argument("--channels", action="store_true",
                        help="relay sessions whose claude.ai channel is not in channel_mappings")
    args = parser.parse_args()
    if not args.channels:
        args.folders = True
    if args.folders and args.channels and args.json:
        vault = get_memex_path()
        mappings = get_config().get("channel_mappings") or {}
        folders = _folder_payload(audit_folders(vault, mappings))
        channels = _channel_payload(audit_channels(vault, mappings))
        print(json.dumps({"folders": folders, "channels": channels}, indent=2))
        sys.exit(1 if folders["drift_count"] or channels["unmapped_channel_count"]
                 or channels["unreadable"] else 0)
    rc = 0
    if args.folders:
        rc = max(rc, run_folder_audit(json_out=args.json))
    if args.channels:
        if args.folders:
            print()
        rc = max(rc, run_channel_audit(json_out=args.json))
    sys.exit(rc)


if __name__ == "__main__":
    main()
