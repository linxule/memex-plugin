"""Project-folder drift audit — detect the class of drift v0.15.5/v0.15.6 fixed.

Read-only. Flags, per ``projects/<name>/`` folder:
  - **fragment** names (cwd-path fragments like ``Apps-arena`` that didn't
    collapse to their leaf), via ``utils.looks_like_cwd_fragment``;
  - **mismatch** — folder name != ``detect_project(cwd)`` where ``cwd`` is the
    true working directory recovered from a memo's ``source_cwd`` frontmatter or
    an archived transcript JSONL;
  - **subset / duplicate** folders — one folder's content (memo filenames +
    transcript session ids) is a subset of another's (the ``personal-website ⊂
    linxule_com`` case), or two folders canonicalize to the same project.

For each drift folder it prints the exact ``memex obs reassign`` commands to
consolidate — with the bright-line reminder to confirm duplication first. Never
mutates vault content (memos/observations); it only reads the index for obs
counts, and only when ``_index.sqlite`` already exists. Exposed as
``memex check --folders`` (``--json`` for agents; exits non-zero when
high-confidence drift is found, for CI/launchd).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from memex.paths import get_memex_path
from memex.scripts.utils import detect_project, looks_like_cwd_fragment

_SOURCE_CWD_RE = re.compile(r"^source_cwd:\s*(.+?)\s*$", re.MULTILINE)
_DATE_PREFIX_RE = re.compile(r"^\d{8}-\d{6}-")


def _read_source_cwd(md: Path) -> str | None:
    """Pull ``source_cwd`` from a memo's YAML frontmatter (cheap, head only)."""
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
            try:
                with jf.open(encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh):
                        if i >= 200:
                            break
                        if '"cwd"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if isinstance(obj, dict):
                            cwd = obj.get("cwd")
                            if isinstance(cwd, str) and cwd:
                                return cwd
            except OSError:
                continue
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


def audit_folders(vault: Path) -> dict:
    """Build the drift report over ``projects/<name>/`` (no mutation)."""
    proj_root = vault / "projects"
    dirs = sorted(p for p in proj_root.iterdir() if p.is_dir()) if proj_root.is_dir() else []

    # Open the index only if it already exists — connect_index sets WAL pragmas
    # (a write), so we must not create one from a read-only audit.
    conn = None
    idx = vault / "_index.sqlite"
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
    for pd in dirs:
        name = pd.name
        cwd = _cwd_for_project(pd)
        canonical = detect_project(cwd) if cwd else None
        records[name] = {
            "name": name,
            "cwd": cwd,
            "canonical": canonical,
            "is_fragment": looks_like_cwd_fragment(name),
            "mismatch": bool(canonical and canonical != name),
            "obs": obs_count(name),
            "keys": _content_keys(pd),
            "subset_of": [],
        }

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


def _drift_folders(report: dict) -> dict:
    """High-confidence drift: cwd-fragment-shaped names, or content that is a
    strict subset of another folder (a true duplicate like personal-website ⊂
    linxule_com). These are safe-to-act-on signals."""
    return {
        n: r for n, r in report["records"].items()
        if r["is_fragment"] or r["subset_of"]
    }


def _review_folders(report: dict, drift: dict) -> dict:
    """Lower-confidence: name≠canonical (git remote / recovered cwd can legitimately
    differ from a deliberately-named folder) or canonical collisions. Surface for
    HUMAN review, not auto-action."""
    coll = {n for ns in report["collisions"].values() for n in ns}
    return {
        n: r for n, r in report["records"].items()
        if n not in drift and (r["mismatch"] or n in coll)
    }


def run_folder_audit(json_out: bool = False) -> int:
    """Print the folder-drift report. Returns 1 iff high-confidence drift found."""
    vault = get_memex_path()
    report = audit_folders(vault)
    drift = _drift_folders(report)
    review = _review_folders(report, drift)

    if json_out:
        print(json.dumps({
            "drift_count": len(drift),
            "drift": drift,
            "review": review,
            "collisions": report["collisions"],
        }, indent=2))
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
            obs = r["obs"] if r["obs"] is not None else "?"
            print(f"  • {name}  [{'; '.join(issues)}]  (obs={obs}, items={r['content_count']})")
            target = r["subset_of"][0] if r["subset_of"] else None
            if target:
                print(f"      → CONFIRM it duplicates '{target}' (diff memos AND transcripts), "
                      f"then reassign by SUBPREFIX:")
                for sub in ("memos", "transcripts"):
                    print(f"          memex obs reassign --from-prefix \"projects/{name}/{sub}/\" "
                          f"--to-prefix \"projects/{target}/{sub}/\"   # dry-run; add --apply")
                print(f"        # WARNING: do NOT reassign \"projects/{name}/\" wholesale "
                      f"(collides on _project.md); verify the obs-count invariant after.")
            elif r["is_fragment"]:
                print(f"      → fragment with no clear in-vault duplicate; find its canonical "
                      f"home (git remote / project_mappings) before reassigning")

    if review:
        print(f"\n  {len(review)} folder(s) to REVIEW (lower confidence — verify manually, "
              f"git-remote/cwd can legitimately differ from a chosen folder name):")
        for name, r in sorted(review.items()):
            why = f"name≠canonical ({r['canonical']})" if r["mismatch"] else "canonical collision"
            obs = r["obs"] if r["obs"] is not None else "?"
            print(f"    • {name}  [{why}]  (obs={obs})")

    print("\n  SOP: confirm duplication (filenames AND content) before any reassign; "
          "verify the obs-count invariant after. See the project-consolidation skill.")
    return 1 if drift else 0


def main():
    parser = argparse.ArgumentParser(description="Audit vault project folders for detection drift")
    parser.add_argument("--json", action="store_true", help="JSON output for agents")
    args = parser.parse_args()
    sys.exit(run_folder_audit(json_out=args.json))


if __name__ == "__main__":
    main()
