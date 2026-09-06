from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from memex.db_utils import writer_lock
from memex.extract import Observation, store_observations
from memex.llm import call_claude_json, claude_available
from memex.observations import fetch_observations, init_observation_schema
from memex.paths import get_index_path, get_memex_path

STOPWORDS = {
    "project", "decision", "summary", "fact", "learning", "open", "question",
    "constraint", "observation", "with", "from", "that", "this", "into", "over",
    "because", "after", "before", "have", "has", "had", "were", "was", "been",
}
POSITIVE_TOKENS = {"enable", "enabled", "use", "used", "keep", "kept", "include", "included"}
NEGATIVE_TOKENS = {"disable", "disabled", "avoid", "avoided", "remove", "removed", "reject", "rejected", "exclude", "excluded"}

# -- JSON schemas for claude --json-schema flag --

PROJECT_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deductions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["content", "source_ids", "confidence"],
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "observation_a_id": {"type": "integer"},
                    "observation_b_id": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["content", "observation_a_id", "observation_b_id", "confidence"],
            },
        },
    },
    "required": ["deductions", "contradictions"],
}

PATTERN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "observation_ids": {"type": "array", "items": {"type": "integer"}},
                    "projects": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "description", "observation_ids", "projects"],
            },
        },
    },
    "required": ["patterns"],
}

# -- Prompts --

ANALYSIS_PROMPT = """You are analyzing observations from project "{project}" to find new deductions and contradictions.

## Observations
{observations}

## Task 1: Deductions
Identify NEW conclusions that logically follow from combining 2+ observations.
A valid deduction must:
- Combine information from multiple observations into something not already stated
- Follow necessarily from the source observations, not just probably
- Be a single, atomic, independently understandable fact
- Use absolute dates where relevant
Maximum 5 deductions. If no valid deductions exist, return an empty array.

## Task 2: Contradictions
Identify pairs of observations that genuinely conflict — where one being true makes the other false or problematic. Do NOT flag:
- Observations about different contexts or time periods (unless explicitly contradictory)
- Observations that merely discuss different aspects of the same topic
- Superseded decisions that acknowledge the change
Maximum 10 contradictions. If no genuine contradictions exist, return an empty array."""

PATTERN_PROMPT = """These observation clusters share terms across different projects in a knowledge vault. For each cluster, determine if there is a genuine cross-project PATTERN worth documenting as a topic note.

## Clusters
{clusters}

## Rules
- A genuine pattern appears in 3+ observations from 2+ projects
- Surface recurring preferences, architectural decisions, principles, or tensions
- Reject coincidental similarity (e.g., all projects using "git" or "python")
- Reject patterns that are just "multiple projects exist" or "work was done"
- For real patterns, provide a clear title (lowercase-hyphenated, suitable for a wiki page) and 2-3 sentence description
- Include the observation IDs and project names that support the pattern
If no genuine patterns exist, return an empty array."""


@dataclass(slots=True)
class DreamerResult:
    deductions_created: int
    patterns_found: int
    contradictions_detected: int
    duplicates_merged: int
    links_fixed: int
    archives_suggested: list[str]


async def run_dreamer(
    vault_path: Path,
    index_path: Path,
    scope: str = "all",
    dry_run: bool = False,
) -> DreamerResult:
    return await asyncio.to_thread(
        _run_dreamer_sync,
        vault_path,
        index_path,
        scope,
        dry_run,
    )


def _try_get_settings():
    try:
        from memex.config import get_settings
        return get_settings()
    except ImportError:
        return None


def _run_dreamer_sync(
    vault_path: Path,
    index_path: Path,
    scope: str,
    dry_run: bool,
) -> DreamerResult:
    settings = _try_get_settings()
    llm_enabled = (
        not dry_run
        and settings is not None
        and settings.dreamer.llm_enabled
        and claude_available()
    )
    model = settings.dreamer.model if settings else "sonnet"
    max_obs = settings.dreamer.max_observations_per_call if settings else 200

    # Hold the shared writer lock across the READ as well as the write: a
    # rebuild that finishes while we wait would otherwise leave us deriving
    # deductions from observations it just removed. (The inner writer_lock
    # below is a second shared lock on its own fd — compatible, harmless.)
    lock = nullcontext() if dry_run else writer_lock()
    with lock:
        return _dream_locked(vault_path, index_path, scope, dry_run, llm_enabled, model, max_obs)


def _dream_locked(
    vault_path: Path,
    index_path: Path,
    scope: str,
    dry_run: bool,
    llm_enabled: bool,
    model: str,
    max_obs: int,
) -> DreamerResult:
    conn = sqlite3.connect(index_path)
    try:
        init_observation_schema(conn)
        project_filter = _project_from_scope(scope)
        observations_by_project = _load_observations_by_project(conn, project_filter=project_filter)

        deductions_created = 0
        contradictions_detected = 0

        # All observation/obs-topic writes go through the shared writer_lock
        # (LOCK_SH on `~/.memex/locks/full-rebuild.lock`) so a concurrent
        # `memex index rebuild --full` (LOCK_EX) can't atomically swap mid-
        # write and silently drop our writes. dry-run skips writes entirely
        # so the lock is unnecessary on that path.
        if dry_run:
            for project, observations in observations_by_project.items():
                target_doc = _project_target_doc(vault_path, project, observations)

                if llm_enabled:
                    result = _analyze_project_llm(project, observations, model, max_obs)
                    if result is not None:
                        deductions, contradictions = result
                    else:
                        deductions = _derive_deductions_heuristic(project, observations)
                        contradictions = _detect_contradictions_heuristic(observations)
                else:
                    deductions = _derive_deductions_heuristic(project, observations)
                    contradictions = _detect_contradictions_heuristic(observations)

                deductions_created += len(deductions)
                contradictions_detected += len(contradictions)
            duplicates_merged = _merge_duplicate_observations(conn, dry_run=True)
        else:
            with writer_lock():
                for project, observations in observations_by_project.items():
                    target_doc = _project_target_doc(vault_path, project, observations)

                    if llm_enabled:
                        result = _analyze_project_llm(project, observations, model, max_obs)
                        if result is not None:
                            deductions, contradictions = result
                        else:
                            deductions = _derive_deductions_heuristic(project, observations)
                            contradictions = _detect_contradictions_heuristic(observations)
                    else:
                        deductions = _derive_deductions_heuristic(project, observations)
                        contradictions = _detect_contradictions_heuristic(observations)

                    if target_doc is not None:
                        all_derived = deductions + contradictions
                        store_observations(
                            index_path,
                            target_doc,
                            all_derived,
                            pipeline=None,
                            # Explicit: derived obs are recomputed as a complete
                            # set each pass, so replacing the doc's prior derived
                            # rows is intended.
                            mode="replace",
                        )
                    deductions_created += len(deductions)
                    contradictions_detected += len(contradictions)

                duplicates_merged = _merge_duplicate_observations(conn, dry_run=False)

        if llm_enabled:
            patterns_found = _materialize_patterns_llm(
                vault_path, observations_by_project, model, max_obs,
            )
        else:
            patterns_found = _materialize_patterns_heuristic(
                vault_path, observations_by_project, dry_run=dry_run,
            )
        links_fixed = _fix_broken_links(vault_path, conn, dry_run=dry_run)
        archives_suggested = _archive_candidates(conn, project_filter=project_filter)

        if not dry_run:
            _append_project_updates(vault_path, observations_by_project)

        return DreamerResult(
            deductions_created=deductions_created,
            patterns_found=patterns_found,
            contradictions_detected=contradictions_detected,
            duplicates_merged=duplicates_merged,
            links_fixed=links_fixed,
            archives_suggested=archives_suggested,
        )
    finally:
        conn.close()


# -- LLM-powered analysis --

def _format_observations(observations: list, max_count: int) -> str:
    explicit = [o for o in observations if o.obs_type == "explicit"]
    recent = sorted(explicit, key=lambda o: o.created_at or "", reverse=True)[:max_count]
    return "\n".join(f"[id={o.id}] {o.content}" for o in recent)


def _analyze_project_llm(
    project: str,
    observations: list,
    model: str,
    max_obs: int,
) -> tuple[list[Observation], list[Observation]] | None:
    formatted = _format_observations(observations, max_obs)
    if not formatted:
        return [], []

    prompt = ANALYSIS_PROMPT.format(project=project, observations=formatted)
    response = call_claude_json(prompt, PROJECT_ANALYSIS_SCHEMA, model=model)
    if response is None:
        return None
    if not isinstance(response.get("deductions"), list) or not isinstance(response.get("contradictions"), list):
        return None

    deductions: list[Observation] = []
    for item in response.get("deductions", [])[:5]:
        deductions.append(Observation(
            content=item["content"],
            obs_type="deductive",
            confidence=item.get("confidence", "medium"),
            source_obs_ids=item.get("source_ids", []),
        ))

    contradictions: list[Observation] = []
    for item in response.get("contradictions", [])[:10]:
        contradictions.append(Observation(
            content=item["content"],
            obs_type="contradiction",
            confidence=item.get("confidence", "medium"),
            source_obs_ids=[item["observation_a_id"], item["observation_b_id"]],
        ))

    return deductions, contradictions


def _build_clusters(observations_by_project: dict[str, list], max_obs: int) -> list[dict]:
    term_projects: dict[str, set[str]] = {}
    term_observations: dict[str, list] = {}

    for project, observations in observations_by_project.items():
        for observation in observations:
            for token in _meaningful_tokens(observation.content):
                term_projects.setdefault(token, set()).add(project)
                term_observations.setdefault(token, []).append(observation)

    clusters: list[dict] = []
    for token, projects in sorted(term_projects.items()):
        if len(projects) < 2:
            continue
        obs_list = term_observations[token]
        if len(obs_list) < 3:
            continue
        clusters.append({
            "term": token,
            "projects": sorted(projects),
            "observations": [
                {"id": o.id, "content": o.content, "project": p}
                for p, obs in observations_by_project.items()
                for o in obs
                if token in _meaningful_tokens(o.content)
            ][:max_obs],
        })
    return clusters


def _format_clusters(clusters: list[dict]) -> str:
    parts: list[str] = []
    for cluster in clusters:
        obs_lines = "\n".join(
            f"  [id={o['id']}] ({o['project']}) {o['content']}"
            for o in cluster["observations"]
        )
        parts.append(f"### Cluster: {cluster['term']}\nProjects: {', '.join(cluster['projects'])}\n{obs_lines}")
    return "\n\n".join(parts)


def _materialize_patterns_llm(
    vault_path: Path,
    observations_by_project: dict[str, list],
    model: str,
    max_obs: int,
) -> int:
    clusters = _build_clusters(observations_by_project, max_obs)
    if not clusters:
        return 0

    prompt = PATTERN_PROMPT.format(clusters=_format_clusters(clusters))
    response = call_claude_json(prompt, PATTERN_SCHEMA, model=model)
    if response is None or not isinstance(response.get("patterns"), list):
        # dry_run=False is safe here — this function is only called when llm_enabled (which requires not dry_run)
        return _materialize_patterns_heuristic(vault_path, observations_by_project, dry_run=False)

    created = 0
    for pattern in response.get("patterns", []):
        title = pattern["title"]
        slug = re.sub(r"[^a-z0-9-]", "-", title.lower()).strip("-")
        if not slug:
            continue
        description = pattern["description"]
        topic_path = vault_path / "topics" / f"{slug}.md"
        display_title = title.replace("-", " ").title()

        if topic_path.exists():
            content = topic_path.read_text()
            if description not in content:
                topic_path.write_text(
                    content.rstrip()
                    + f"\n\n## Dreamer Pattern Update {datetime.now():%Y-%m-%d}\n\n"
                    + description
                    + "\n"
                )
                created += 1
        else:
            topic_path.write_text(
                "---\n"
                "type: topic\n"
                f"title: {display_title}\n"
                f"date: {datetime.now():%Y-%m-%d}\n"
                "---\n\n"
                f"# {display_title}\n\n"
                f"{description}\n"
            )
            created += 1
    return created


# -- Heuristic fallbacks --

def _derive_deductions_heuristic(project: str, observations: list) -> list[Observation]:
    grouped: dict[str, list[int]] = {}
    for observation in observations:
        if observation.obs_type != "explicit":
            continue
        for token in _meaningful_tokens(observation.content):
            grouped.setdefault(token, []).append(observation.id)

    deductions: list[Observation] = []
    for token, obs_ids in grouped.items():
        unique_ids = list(dict.fromkeys(obs_ids))
        if len(unique_ids) < 2 or len(token) < 4:
            continue
        deductions.append(
            Observation(
                content=f"Deduction: project {project} repeatedly centers work on '{token}'.",
                obs_type="deductive",
                confidence="medium",
                source_obs_ids=unique_ids[:4],
            )
        )
        if len(deductions) >= 5:
            break
    return deductions


def _detect_contradictions_heuristic(observations: list) -> list[Observation]:
    contradictions: list[Observation] = []
    explicit = [observation for observation in observations if observation.obs_type == "explicit"]
    for index, left in enumerate(explicit):
        for right in explicit[index + 1 :]:
            if _token_overlap(left.content, right.content) < 0.45:
                continue
            if not _is_contradiction(left.content, right.content):
                continue
            contradictions.append(
                Observation(
                    content=(
                        f"Contradiction: '{left.content}' conflicts with '{right.content}'."
                    ),
                    obs_type="contradiction",
                    confidence="medium",
                    source_obs_ids=[left.id, right.id],
                )
            )
            if len(contradictions) >= 10:
                return contradictions
    return contradictions


def _materialize_patterns_heuristic(
    vault_path: Path,
    observations_by_project: dict[str, list],
    *,
    dry_run: bool,
) -> int:
    term_projects: dict[str, set[str]] = {}
    term_observations: dict[str, list] = {}

    for project, observations in observations_by_project.items():
        for observation in observations:
            for token in _meaningful_tokens(observation.content):
                term_projects.setdefault(token, set()).add(project)
                term_observations.setdefault(token, []).append(observation)

    created = 0
    for token, projects in sorted(term_projects.items()):
        if len(projects) < 2:
            continue
        observations = term_observations[token]
        if len(observations) < 3:
            continue
        created += 1
        if dry_run:
            continue

        topic_path = vault_path / "topics" / f"{token}.md"
        title = token.replace("-", " ").title()
        description = (
            f"Cross-project pattern around {token} observed in "
            f"{', '.join(sorted(projects))}."
        )
        if topic_path.exists():
            content = topic_path.read_text()
            if description not in content:
                topic_path.write_text(
                    content.rstrip()
                    + f"\n\n## Dreamer Pattern Update {datetime.now():%Y-%m-%d}\n\n"
                    + description
                    + "\n"
                )
        else:
            topic_path.write_text(
                "---\n"
                "type: topic\n"
                f"title: {title}\n"
                f"date: {datetime.now():%Y-%m-%d}\n"
                "---\n\n"
                f"# {title}\n\n"
                f"{description}\n"
            )
    return created


# -- Shared utilities (unchanged) --

def _project_from_scope(scope: str) -> str | None:
    if scope.startswith("project:"):
        return scope.split(":", 1)[1]
    return None


def _load_observations_by_project(
    conn: sqlite3.Connection,
    *,
    project_filter: str | None,
) -> dict[str, list]:
    sql = """
        SELECT docs.project, o.id, o.doc_path, o.content, o.content_hash, o.obs_type,
               o.confidence, o.source_obs_ids, o.created_at
        FROM observations o
        LEFT JOIN fts_content docs ON docs.path = o.doc_path
    """
    params: list[object] = []
    if project_filter:
        sql += " WHERE docs.project = ?"
        params.append(project_filter)
    sql += " ORDER BY docs.project, o.id"
    rows = conn.execute(sql, params).fetchall()

    grouped: dict[str, list] = {}
    for row in rows:
        project = row[0] or "_unknown"
        grouped.setdefault(project, []).append(_row_to_observation(row[1:]))
    return grouped


def _row_to_observation(row: tuple) -> object:
    from memex.observations import StoredObservation, decode_source_ids

    return StoredObservation(
        id=row[0],
        doc_path=row[1],
        content=row[2],
        content_hash=row[3],
        obs_type=row[4],
        confidence=row[5],
        source_obs_ids=decode_source_ids(row[6]),
        created_at=row[7],
    )


def _project_target_doc(vault_path: Path, project: str, observations: list) -> str | None:
    project_file = vault_path / "projects" / project / "_project.md"
    if project_file.exists():
        return str(project_file.relative_to(vault_path))
    if observations:
        return observations[0].doc_path
    return None


def _merge_duplicate_observations(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    rows = conn.execute(
        """
        SELECT lower(trim(content)) AS normalized, group_concat(id), count(*)
        FROM observations
        GROUP BY normalized
        HAVING count(*) > 1
        """
    ).fetchall()
    merged = 0
    for _, raw_ids, count in rows:
        obs_ids = [int(item) for item in str(raw_ids).split(",")]
        keep_id = obs_ids[0]
        drop_ids = obs_ids[1:]
        merged += len(drop_ids)
        if dry_run or not drop_ids:
            continue
        # Route through the shared deleter — this path used to hand-maintain
        # its own mirror-table list and omitted `observation_topics`, leaving
        # tag rows pointing at deleted observations.
        from memex.observations import delete_observation_ids

        delete_observation_ids(conn, drop_ids)
        conn.commit()
    return merged


def _fix_broken_links(vault_path: Path, conn: sqlite3.Connection, *, dry_run: bool) -> int:
    fixed = 0
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT source_path, link_text
            FROM wikilinks
            WHERE is_broken = 1
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    try:
        aliases = {
            row[0]: row[1]
            for row in conn.execute("SELECT alias, doc_path FROM doc_aliases").fetchall()
        }
    except sqlite3.OperationalError:
        aliases = {}
    stems = _stem_candidates(vault_path)

    for source_path, link_text in rows:
        candidates = set()
        if link_text in aliases:
            candidates.add(Path(aliases[link_text]).stem)
        if link_text in stems:
            candidates.add(stems[link_text])
        if len(candidates) != 1:
            continue

        replacement = next(iter(candidates))
        source_file = vault_path / source_path
        if not source_file.exists():
            continue
        original = source_file.read_text()
        updated = original.replace(f"[[{link_text}]]", f"[[{replacement}]]")
        if updated == original:
            continue
        fixed += 1
        if not dry_run:
            source_file.write_text(updated)
    return fixed


def _stem_candidates(vault_path: Path) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for path in list(vault_path.glob("topics/*.md")) + list(vault_path.glob("projects/*/_project.md")):
        candidates[path.stem] = path.stem
    return candidates


def _archive_candidates(conn: sqlite3.Connection, *, project_filter: str | None) -> list[str]:
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    sql = """
        SELECT f.path
        FROM fts_content f
        WHERE f.type = 'memo'
          AND f.date != ''
          AND f.date < ?
    """
    params: list[object] = [cutoff]
    if project_filter:
        sql += " AND f.project = ?"
        params.append(project_filter)
    rows = conn.execute(sql, params).fetchall()

    candidates: list[str] = []
    for row in rows:
        path = row[0]
        try:
            backlinks = conn.execute(
                "SELECT count(*) FROM wikilinks WHERE target_path = ?",
                (path,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            backlinks = 0
        try:
            open_tasks = conn.execute(
                "SELECT count(*) FROM tasks WHERE doc_path = ? AND completed = 0",
                (path,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            open_tasks = 0
        if backlinks == 0 and open_tasks == 0:
            candidates.append(path)
    return candidates


def _append_project_updates(vault_path: Path, observations_by_project: dict[str, list]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    for project, observations in observations_by_project.items():
        project_file = vault_path / "projects" / project / "_project.md"
        if not project_file.exists():
            continue
        content = project_file.read_text()
        marker = f"## Dreamer Update {today}"
        if marker in content:
            continue

        recent_titles = sorted({Path(observation.doc_path).stem for observation in observations})[:5]
        addition = (
            f"\n\n{marker}\n\n"
            f"- Observations indexed: {len(observations)}\n"
            f"- Recent sources: {', '.join(recent_titles) if recent_titles else 'none'}\n"
        )
        project_file.write_text(content.rstrip() + addition + "\n")


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"\b[a-z0-9][a-z0-9_-]+\b", text.lower())
        if len(token) > 3 and token not in STOPWORDS
    }


def _token_overlap(left: str, right: str) -> float:
    left_tokens = _meaningful_tokens(left)
    right_tokens = _meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)


def _polarity(text: str) -> int:
    tokens = _meaningful_tokens(text)
    positive = len(tokens & POSITIVE_TOKENS)
    negative = len(tokens & NEGATIVE_TOKENS)
    if positive > negative:
        return 1
    if negative > positive:
        return -1
    return 0


def _is_contradiction(left: str, right: str) -> bool:
    left_polarity = _polarity(left)
    right_polarity = _polarity(right)
    return left_polarity != 0 and right_polarity != 0 and left_polarity != right_polarity


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the maintenance dreamer")
    parser.add_argument("--scope", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vault", type=Path, default=get_memex_path())
    parser.add_argument("--index", type=Path, default=get_index_path())
    args = parser.parse_args()

    result = asyncio.run(
        run_dreamer(
            vault_path=args.vault,
            index_path=args.index,
            scope=args.scope,
            dry_run=args.dry_run,
        )
    )
    print(asdict(result))


if __name__ == "__main__":
    main()
