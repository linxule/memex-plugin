"""Tests for the crystallization-check filesystem fallback.

When Obsidian is not running, ``memex check`` falls back to
``scan_unresolved_via_markdown`` instead of hard-erroring (the pre-fix
behaviour exited 1 on a 15s Obsidian timeout — unusable headless/cron).
These tests pin the fallback's resolution rules: a ``[[link]]`` resolves
against any markdown filename stem OR any frontmatter alias; everything else
is reported unresolved with its source files.
"""

from __future__ import annotations

from pathlib import Path

from memex.scripts.crystallization_check import (
    CURATOR_STALE_DAYS,
    _filter_noncurated_sources,
    _is_archived,
    _is_noncurated_source,
    _log_last_entry_date,
    _newest_topic_date,
    _project_folder_slugs,
    _read_frontmatter_aliases,
    _strip_code_spans,
    curator_artifact_status,
    is_noise,
    scan_unresolved_via_markdown,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_resolves_by_filename_stem_and_alias_reports_ghosts(tmp_path: Path) -> None:
    # Existing topic with an inline alias list
    _write(
        tmp_path / "topics" / "existing-topic.md",
        "---\naliases: [the-alias, second-alias]\n---\n\n# Existing\n",
    )
    # A project overview that references one resolvable filename, one alias,
    # and two genuine ghost nodes (one appearing twice across files).
    _write(
        tmp_path / "projects" / "foo" / "_project.md",
        "Links [[existing-topic]], [[the-alias|display]], "
        "[[ghost-concept]] and [[ghost-concept#heading]] and [[another-ghost]].\n",
    )
    _write(
        tmp_path / "projects" / "bar" / "_project.md",
        "Also references [[ghost-concept]] here.\n",
    )

    unresolved, alias_map = scan_unresolved_via_markdown(tmp_path)

    # Resolvable links must NOT appear
    assert "existing-topic" not in unresolved
    assert "the-alias" not in unresolved
    # Ghost nodes appear; heading/display suffixes are stripped before matching
    assert "ghost-concept" in unresolved
    assert "another-ghost" in unresolved
    # Cross-file aggregation: ghost-concept seen in both project files (deduped)
    assert sorted(unresolved["ghost-concept"]) == [
        "projects/bar/_project.md",
        "projects/foo/_project.md",
    ]
    # Alias map is built from frontmatter, lowercased
    assert alias_map.get("the-alias", "").endswith("existing-topic.md")
    assert "second-alias" in alias_map


def test_skips_plumbing_dirs(tmp_path: Path) -> None:
    _write(tmp_path / ".obsidian" / "x.md", "[[should-be-ignored]]\n")
    _write(tmp_path / ".git" / "y.md", "[[also-ignored]]\n")
    _write(tmp_path / "topics" / "real.md", "[[a-real-ghost]]\n")

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    assert "should-be-ignored" not in unresolved
    assert "also-ignored" not in unresolved
    assert "a-real-ghost" in unresolved


def test_frontmatter_alias_block_form(tmp_path: Path) -> None:
    f = tmp_path / "t.md"
    _write(
        f,
        "---\naliases:\n  - alpha\n  - beta\ntags: [x]\n---\n\nbody\n",
    )
    aliases = _read_frontmatter_aliases(f)
    assert aliases == ["alpha", "beta"]


def test_frontmatter_no_aliases(tmp_path: Path) -> None:
    f = tmp_path / "t.md"
    _write(f, "---\ntitle: Foo\n---\n\nbody [[x]]\n")
    assert _read_frontmatter_aliases(f) == []


def test_frontmatter_aliases_as_last_block_terminated_by_fence(tmp_path: Path) -> None:
    # Bare `aliases:` list with no following key — closing `---` must terminate it
    f = tmp_path / "t.md"
    _write(f, "---\naliases:\n  - alpha\n  - beta\n---\n\nbody\n")
    assert _read_frontmatter_aliases(f) == ["alpha", "beta"]


def test_frontmatter_quoted_aliases(tmp_path: Path) -> None:
    inline = tmp_path / "i.md"
    _write(inline, '---\naliases: ["foo", \'bar\']\n---\n')
    assert _read_frontmatter_aliases(inline) == ["foo", "bar"]
    block = tmp_path / "b.md"
    _write(block, "---\naliases:\n  - \"alpha\"\n  - 'beta'\n---\n")
    assert _read_frontmatter_aliases(block) == ["alpha", "beta"]


def test_malformed_and_empty_frontmatter_do_not_crash(tmp_path: Path) -> None:
    # Unterminated frontmatter (opening fence, no closing fence)
    unterminated = tmp_path / "u.md"
    _write(unterminated, "---\naliases:\n  - x\nbody with no closing fence\n")
    assert _read_frontmatter_aliases(unterminated) == ["x"]
    # File that is only an opening fence
    bare = tmp_path / "bare.md"
    _write(bare, "---\n")
    assert _read_frontmatter_aliases(bare) == []
    # Truly empty file
    empty = tmp_path / "e.md"
    _write(empty, "")
    assert _read_frontmatter_aliases(empty) == []
    # No frontmatter at all
    plain = tmp_path / "p.md"
    _write(plain, "# Heading\n[[ghost]]\n")
    assert _read_frontmatter_aliases(plain) == []


def test_non_utf8_file_is_tolerated(tmp_path: Path) -> None:
    # Latin-1 / invalid-UTF8 bytes must not crash the scan (errors="ignore")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"---\naliases: [caf\xe9]\n---\n[[real-ghost]]\n")
    unresolved, _ = scan_unresolved_via_markdown(tmp_path)
    assert "real-ghost" in unresolved


def test_strip_code_spans_removes_fences_inline_and_ansi() -> None:
    # Wikilink-shaped fragments inside code spans / ANSI must be removed so the
    # fallback doesn't mis-parse TOML [[section]] headers, bash `if [[ ]]`, and
    # terminal escape dumps as ghost nodes. Real links outside code survive.
    text = (
        "real [[keep-me]] before\n"
        "```toml\n[[d1_databases]]\n[[r2_buckets]]\n```\n"
        "inline `if [[ $x ]]; then` mid\n"
        "ansi \x1b[1m bold \x1b[0m tail\n"
    )
    out = _strip_code_spans(text)
    assert "[[keep-me]]" in out          # real link outside code survives
    assert "d1_databases" not in out     # fenced TOML header removed
    assert "r2_buckets" not in out
    assert "$x" not in out               # bash conditional in inline code removed
    assert "\x1b[" not in out            # ANSI CSI sequences stripped


def test_code_fence_wikilinks_not_reported_as_ghosts(tmp_path: Path) -> None:
    _write(
        tmp_path / "topics" / "real.md",
        "Body [[genuine-ghost]] in prose.\n"
        "```toml\n[[d1_databases]]\n[[migrations]]\n```\n"
        "And `bash [[ -f x ]]` inline.\n",
    )
    unresolved, _ = scan_unresolved_via_markdown(tmp_path)
    assert "genuine-ghost" in unresolved        # prose link still detected
    assert "d1_databases" not in unresolved      # fenced TOML header ignored
    assert "migrations" not in unresolved
    assert not any("-f x" in k for k in unresolved)  # bash inline fragment ignored


def test_is_noise_filters_residual_artifacts() -> None:
    # v0.14.2 noise additions
    assert is_noise("2026-02-16-feedback-loops")      # ISO-date-prefixed memo link
    assert is_noise("2026-05-17-v034-extraction.md")  # ISO date + .md
    assert is_noise("some-memo.md")                   # explicit .md file link
    assert is_noise("@xiaoxxchan")                    # social handle
    assert is_noise("X")                              # single uppercase
    assert is_noise("MCP")                            # short all-caps acronym
    assert is_noise("SSRN")
    # pre-existing noise still caught
    assert is_noise("?suggested-concept")
    assert is_noise("projects/foo/bar")
    # real kebab-case concepts must NOT be filtered
    assert not is_noise("mcp-distribution")
    assert not is_noise("garden-tending")
    assert not is_noise("post-agi-organizations")
    assert not is_noise("g-trust-coding-validity")


def test_is_archived_detects_frontmatter_status() -> None:
    assert _is_archived("---\ntype: memo\nstatus: archived\n---\nbody\n")
    assert _is_archived("---\nstatus: archived\nredirect_to: x\n---\n")
    assert not _is_archived("---\nstatus: active\n---\n")
    # status: archived only in the BODY (not frontmatter) must not count
    assert not _is_archived("---\ntype: memo\n---\nprose says status: archived here\n")
    # no frontmatter at all
    assert not _is_archived("# Heading\nstatus: archived in prose\n")


def test_is_noncurated_source_matches_transcripts_and_auto_memory() -> None:
    # Transcripts and auto-memory are raw dumps, not curated knowledge → not sources
    assert _is_noncurated_source("projects/foo/transcripts/2026-06-22-session.md")
    assert _is_noncurated_source("projects/bar/auto-memory/MEMORY.md")
    # Windows-style separators normalize too
    assert _is_noncurated_source("projects\\foo\\transcripts\\x.md")
    # Curated surfaces are NOT excluded
    assert not _is_noncurated_source("projects/foo/memos/2026-06-22-real.md")
    assert not _is_noncurated_source("projects/foo/_project.md")
    assert not _is_noncurated_source("topics/real-concept.md")
    # "transcripts"/"auto-memory" as a bare filename stem (no path segment) is NOT a match
    assert not _is_noncurated_source("topics/transcripts.md")


def test_transcript_ghost_excluded_but_memo_ghost_reported(tmp_path: Path) -> None:
    # The diagnosed bug: a curated memo references a genuine ghost, while a raw
    # transcript emits a wikilink-shaped fragment (the kind _strip_code_spans
    # misses on fenced-block edge cases). The transcript junk must NOT crystallize;
    # the memo's real ghost must still be reported.
    _write(
        tmp_path / "projects" / "foo" / "memos" / "2026-06-22-note.md",
        "Curated memo references [[real-concept]] which should crystallize.\n",
    )
    _write(
        tmp_path / "projects" / "foo" / "transcripts" / "2026-06-22-session.md",
        "terminal dump emits [[transcript-only-junk]] and [[$MEMO_PATH]] frags.\n",
    )
    # Also pin auto-memory exclusion in the same scan
    _write(
        tmp_path / "projects" / "foo" / "auto-memory" / "MEMORY.md",
        "synced auto-memory mentions [[auto-memory-junk]].\n",
    )

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    # Real ghost from a curated memo IS reported
    assert "real-concept" in unresolved
    # Transcript/auto-memory fragments are NOT reported (no curated vote)
    assert "transcript-only-junk" not in unresolved
    assert "$MEMO_PATH" not in unresolved
    assert "auto-memory-junk" not in unresolved


def test_filter_noncurated_sources_drops_sourceless_links() -> None:
    # Mirrors the Obsidian-native path: a link whose every source is a transcript
    # disappears; a link with at least one curated source keeps only the curated.
    raw = {
        "real-concept": [
            "projects/foo/memos/m.md",
            "projects/foo/transcripts/t.md",
        ],
        "transcript-only-junk": ["projects/foo/transcripts/t.md"],
        "$MEMO_PATH": [
            "projects/a/transcripts/x.md",
            "projects/b/auto-memory/MEMORY.md",
        ],
    }
    filtered = _filter_noncurated_sources(raw)
    # Real concept survives, but only with its curated source
    assert filtered["real-concept"] == ["projects/foo/memos/m.md"]
    # All-transcript / all-auto-memory links vanish entirely
    assert "transcript-only-junk" not in filtered
    assert "$MEMO_PATH" not in filtered


def test_project_folder_slugs_maps_only_dirs_with_overview(tmp_path: Path) -> None:
    # A real project (has _project.md) is included, lowercased, mapped to its
    # overview file's relpath.
    _write(
        tmp_path / "projects" / "llm-org-cognition" / "_project.md",
        "---\ntype: project\nname: llm-org-cognition\n---\n\n# llm-org-cognition\n",
    )
    # A cwd-fragment/drift folder with no overview file yet is excluded — it's
    # not a canonical project reference, so a bare link to it should still
    # surface as a ghost (nothing legitimate to route it to yet).
    _write(
        tmp_path / "projects" / "linxule-Papers-ai-org-design" / "memos" / "x.md",
        "some memo, no _project.md sibling\n",
    )
    # A non-directory stray file directly under projects/ must not crash the scan.
    _write(tmp_path / "projects" / "README.md", "not a project dir\n")

    slugs = _project_folder_slugs(tmp_path)

    assert slugs.get("llm-org-cognition") == "projects/llm-org-cognition/_project.md"
    assert "linxule-papers-ai-org-design" not in slugs
    assert "readme" not in slugs


def test_project_folder_slugs_empty_when_no_projects_dir(tmp_path: Path) -> None:
    assert _project_folder_slugs(tmp_path) == {}


def test_bare_project_name_wikilink_excluded_from_ghost_nodes(tmp_path: Path) -> None:
    # The diagnosed bug: a sibling project's memo references another project
    # by bare name (e.g. [[llm-org-cognition]], [[duality-paper]]) — this has
    # no markdown file literally named <slug>.md (the overview's stem is
    # _project), so it must NOT be treated as an unresolved ghost-node
    # candidate. A genuine ghost concept referenced alongside it must still be
    # detected — the fix must not over-suppress.
    _write(
        tmp_path / "projects" / "llm-org-cognition" / "_project.md",
        "---\ntype: project\nname: llm-org-cognition\n---\n\n# llm-org-cognition\n",
    )
    _write(
        tmp_path / "projects" / "linxule_com" / "memos" / "note.md",
        "Related to [[llm-org-cognition]] — one of the named orientations, "
        "and to the still-unwritten [[emergent-concept]].\n",
    )

    unresolved, alias_map = scan_unresolved_via_markdown(tmp_path)

    # Bare project-name reference is excluded — it's a valid project, not a
    # missing concept.
    assert "llm-org-cognition" not in unresolved
    # A real ghost-node concept referenced in the same file is still detected.
    assert "emergent-concept" in unresolved
    # The project slug is exposed via alias_map (used by main() to also filter
    # the Obsidian-native path, which has no notion of project folders).
    assert alias_map.get("llm-org-cognition") == "projects/llm-org-cognition/_project.md"


def test_project_name_without_overview_still_reported_as_ghost(tmp_path: Path) -> None:
    # A not-yet-consolidated fragment folder (no _project.md) is NOT treated
    # as a valid project reference — a bare link to its slug still surfaces,
    # since there's nothing canonical to route it to.
    _write(
        tmp_path / "projects" / "linxule-Papers-ai-org-design" / "memos" / "x.md",
        "drift fragment, never got an overview file\n",
    )
    _write(
        tmp_path / "projects" / "foo" / "_project.md",
        "See [[linxule-Papers-ai-org-design]] for the old fragment.\n",
    )

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    assert "linxule-Papers-ai-org-design" in unresolved


def test_project_slug_colliding_with_topic_stem_still_resolves(tmp_path: Path) -> None:
    # Real vault case: projects/zen-mcp-server/ (has _project.md) coexists
    # with topics/zen-mcp-server.md. The topic's filename stem already
    # resolves the link on its own; merging the project slug into alias_map
    # on top of that must not break resolution (dict values differ, but only
    # .keys() feed the resolvable set) or double-report anything.
    _write(tmp_path / "topics" / "zen-mcp-server.md", "# Zen MCP Server\n")
    _write(
        tmp_path / "projects" / "zen-mcp-server" / "_project.md",
        "---\ntype: project\nstatus: archived\n---\n\nSee [[zen-mcp-server]] topic.\n",
    )
    _write(
        tmp_path / "projects" / "other" / "_project.md",
        "Uses [[zen-mcp-server]] under the hood.\n",
    )

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    assert "zen-mcp-server" not in unresolved


def test_archived_file_is_not_a_source_but_remains_a_target(tmp_path: Path) -> None:
    # An archived dup memo links a ghost concept + a real topic; an active note
    # links TO the archived file by stem.
    _write(tmp_path / "topics" / "real-topic.md", "# Real\n")
    _write(
        tmp_path / "projects" / "frag" / "memos" / "dup.md",
        "---\ntype: memo\nstatus: archived\n---\n"
        "Links [[archived-only-ghost]] and [[real-topic]].\n",
    )
    _write(
        tmp_path / "projects" / "live" / "_project.md",
        "See [[dup]] and [[live-ghost]].\n",  # links TO the archived file by stem
    )

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    # Archived file's outbound link does NOT cast a ghost-node vote
    assert "archived-only-ghost" not in unresolved
    # Archived file still resolves as a TARGET (its stem stays in resolvable)
    assert "dup" not in unresolved
    # Active-file ghosts are still reported
    assert "live-ghost" in unresolved


# ---------------------------------------------------------------------------
# Curator-artifact freshness (dashboard/log staleness nudge)
# ---------------------------------------------------------------------------

def _seed_curator_vault(
    root: Path,
    *,
    dashboard_updated: str | None,
    log_dates: list[str],
    topic_updated: dict[str, str],
) -> None:
    """Write a minimal curator vault: dashboard + log + topics with dates."""
    if dashboard_updated is not None:
        _write(
            root / "_meta" / "curator-dashboard.md",
            f"---\ntitle: Curator Dashboard\nupdated: {dashboard_updated}\n"
            f"type: meta\n---\n\n# Dashboard\n",
        )
    if log_dates:
        body = "".join(f"## {d} — refresh | topic-{i}\n\n" for i, d in enumerate(log_dates))
        _write(root / "_meta" / "curator-log.md", body)
    for name, updated in topic_updated.items():
        _write(
            root / "topics" / f"{name}.md",
            f"---\ntitle: {name}\nupdated: {updated}\n---\n\nbody\n",
        )


def test_curator_status_fresh_when_dashboard_matches_newest_topic(tmp_path: Path) -> None:
    _seed_curator_vault(
        tmp_path,
        dashboard_updated="2026-07-15",
        log_dates=["2026-05-14", "2026-07-15"],  # not chronological on purpose
        topic_updated={"a": "2026-07-10", "b": "2026-07-15"},
    )
    status = curator_artifact_status(tmp_path)
    assert status is not None
    assert status["newest_topic"] == "2026-07-15"
    assert status["log_last_entry"] == "2026-07-15"  # max(), not last-in-file
    assert status["dashboard_behind_days"] == 0
    assert status["log_behind_days"] == 0
    assert status["dashboard_stale"] is False
    assert status["log_stale"] is False


def test_curator_status_flags_stale_dashboard_and_log(tmp_path: Path) -> None:
    _seed_curator_vault(
        tmp_path,
        dashboard_updated="2026-05-25",
        log_dates=["2026-05-25"],
        topic_updated={"fresh": "2026-07-15"},
    )
    status = curator_artifact_status(tmp_path)
    assert status is not None
    assert status["dashboard_behind_days"] == 51
    assert status["log_behind_days"] == 51
    assert status["dashboard_stale"] is True
    assert status["log_stale"] is True
    # 51 days genuinely exceeds the threshold (guards against a bad constant edit)
    assert 51 > CURATOR_STALE_DAYS


def test_curator_status_none_without_dashboard(tmp_path: Path) -> None:
    # No dashboard → section stays hidden (public-repo / non-curator vaults).
    _seed_curator_vault(
        tmp_path,
        dashboard_updated=None,
        log_dates=["2026-07-15"],
        topic_updated={"a": "2026-07-15"},
    )
    assert curator_artifact_status(tmp_path) is None


def test_newest_topic_date_excludes_archived_topics(tmp_path: Path) -> None:
    # A recently-archived topic must NOT count as the freshness baseline —
    # archiving bumps updated:, which would otherwise fake a fresh dashboard.
    _write(
        tmp_path / "topics" / "live.md",
        "---\ntitle: live\nupdated: 2026-07-10\n---\nbody\n",
    )
    _write(
        tmp_path / "topics" / "just-archived.md",
        "---\ntitle: arch\nupdated: 2026-07-20\nstatus: archived\n---\nbody\n",
    )
    assert _newest_topic_date(tmp_path).isoformat() == "2026-07-10"


def test_log_last_entry_uses_max_not_file_order(tmp_path: Path) -> None:
    _write(
        tmp_path / "_meta" / "curator-log.md",
        "## 2026-07-15 (batch 2) — refresh | tail\n\n"
        "## 2026-05-13 19:10 — refresh | older-with-time\n\n"
        "## 2026-05-14 — normalize | sweep\n",
    )
    assert _log_last_entry_date(tmp_path).isoformat() == "2026-07-15"


def test_curator_status_dashboard_newer_than_topics_clamps_to_zero(tmp_path: Path) -> None:
    # Dashboard updated AFTER the newest topic → behind clamps to 0, not negative.
    _seed_curator_vault(
        tmp_path,
        dashboard_updated="2026-07-20",
        log_dates=["2026-07-20"],
        topic_updated={"a": "2026-07-15"},
    )
    status = curator_artifact_status(tmp_path)
    assert status["dashboard_behind_days"] == 0
    assert status["dashboard_stale"] is False
