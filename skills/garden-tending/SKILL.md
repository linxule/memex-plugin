---
name: garden-tending
description: Tend the knowledge garden — diagnose vault health, condense project memos into `_project.md` overviews, create/merge cross-project topics, fix broken links, archive superseded notes, extend trails. Trigger on "tend the garden", "condense", "update project overview", "check vault health", "where are we with X?", "what does this project know?", "extend a trail", or when a `_project.md` is empty/stale, a project has 5+ unprocessed memos, or a concept appears in 2+ projects. For autonomous tending with judgment, prefer curator-practice.
argument-hint: "[diagnose|condense|connect|crystallize|grow|maintain]"
effort: xhigh
allowed-tools: Read, Write, Bash, Grep, Glob, Task
---

# Knowledge Garden Tending

## Current State

**Index:** !`sqlite3 "$(memex path --index 2>/dev/null)" "SELECT (SELECT COUNT(*) FROM fts_content) || ' docs, ' || (SELECT COUNT(*) FROM chunks) || ' chunks indexed'" 2>/dev/null || echo "(index unavailable)"`

**Projects (undigested / total / last condensed):**
!`for d in $(memex path 2>/dev/null)/projects/*/; do name=$(basename "$d"); total=$(ls "$d/memos/"*.md 2>/dev/null | wc -l | tr -d ' '); digested=$(grep -m1 'memos_digested:' "$d/_project.md" 2>/dev/null | cut -d: -f2 | tr -d ' '); condensed=$(grep -m1 'condensed:' "$d/_project.md" 2>/dev/null | cut -d: -f2 | tr -d ' '); lines=$(wc -l < "$d/_project.md" 2>/dev/null | tr -d ' '); if [ -z "$digested" ] && [ "${lines:-0}" -gt 40 ]; then echo "- $name: MAINTAINED (${total} memos, ${lines}-line overview, no memos_digested — add it)"; else undigested=$((total - ${digested:-0})); [ "$undigested" -lt 0 ] && undigested=0; echo "- $name: ${undigested} undigested (${total} total), condensed: ${condensed:-never}"; fi; done 2>/dev/null || echo "(no projects found)"`
<!-- NOTE: extraction above uses `cut -d: -f2`, NOT `awk '{print $2}'`. Inside a `!`command`` dynamic-context injection, the harness applies slash-command argument substitution ($1, $2, $ARGUMENTS) to the command body BEFORE execution — so `$2` in awk is replaced by the skill's (empty) 2nd arg, yielding `awk '{print }'`. Any `$<digit>` in a bang-command injection is unsafe; avoid positional awk fields here. -->

**Existing topics (use [[?name]] for anything NOT in this list):**
!`ls $(memex path 2>/dev/null)/topics/*.md 2>/dev/null | xargs -I{} basename {} .md | sort | tr '\n' ' ' || echo "(no topics found)"`

## Path Setup

The `memex` CLI handles vault path resolution automatically. For Obsidian CLI and dreamer commands not yet wrapped, prefix with `cd $(memex path 2>/dev/null) &&`.

## Core Principle

**Memos are ore. Project overviews are metal. Topics are the connective tissue. Trails are the narrative arc.** The vault is a living knowledge graph, not an archive. Garden tending is the practice of keeping it alive: distilling memos into overviews, connecting ideas across projects, pruning dead links, growing new topics where patterns emerge, and extending trails where understanding evolves.

The test: if someone starts a session in this project tomorrow, does the vault give them enough to pick up where things left off?

This is not about maintenance logs or summaries. It's about seeing the fuller picture — how does new knowledge relate to what's already here, how do we condense the whole thing and grow it into something more than the sum of its sessions?

---

## The Garden Tending Lifecycle

Knowledge work in the memex follows a cycle:

1. **Diagnose** — Assess vault health, find what needs work (undigested memos, broken links, merge candidates, stale topics/trails)
2. **Condense** — Distill project memos into living `_project.md` overviews
3. **Connect** — Create cross-project topic notes where patterns emerge, add wikilinks
4. **Crystallize** — Analyze graph topology: which ghost nodes (unresolved links) should materialize as real topics?
5. **Grow** — Merge overlapping topics, expand stubs into full topics, **extend trails**
6. **Maintain** — Fix broken links, archive stale files, add aliases, improve discoverability

These aren't strict phases — they interleave. An agent condensing a project notices a cross-project pattern and creates a topic. The lead fixing wikilinks discovers a concept worth growing. Garden tending is sensing what the vault needs and responding.

For vault-wide work, use a **team approach** — human + Claude lead + Sonnet agents working in parallel. The human brings vision and priorities. Claude designs the team and reviews quality. Agents read and write. The vault grows.

---

## Diagnosis: Mapping What Needs Work

When maintenance should act on the index itself, run the dreamer:

```bash
cd $(memex path 2>/dev/null) && uv run python -m memex.dreamer --scope=all --dry-run
cd $(memex path 2>/dev/null) && uv run python -m memex.dreamer --scope=project:<name> --dry-run
```

Use dry-run first (uses fast heuristics, no LLM calls). Without `--dry-run`, the dreamer uses Claude Sonnet via `claude` CLI for semantic deduction, contradiction detection, and pattern filtering — producing higher-quality insights suitable for quoting in project overviews. Falls back to heuristics if `claude` CLI is unavailable.

Before tending, build a map of what needs attention.

### Option A: Run Synthesis First (Recommended for Large Vaults)

Run cross-session synthesis in a **separate session** (even a cheap Sonnet one):

```bash
memex search --since=2w "<pattern query>"
```

Read the results, then synthesize across them: look for recurring themes, contradictions between memos, projects with undigested work, and overlapping topics. The synthesis report identifies:
- Patterns across projects (topic candidates)
- Contradictions in understanding
- Projects with undigested memos
- Topics to merge (overlapping content)
- Compression candidates (superseded memos)

Bring that report to the garden tending session as input.

### Option B: Quick Assessment

```bash
# Which projects have undigested memos? (run with bash; under zsh `setopt NULL_GLOB` first)
# undigested = folder_total - memos_digested (from _project.md frontmatter).
# A SUBSTANTIAL overview with NO memos_digested frontmatter is "maintained but
# unstamped", NOT "never condensed" — add the frontmatter, don't re-condense from
# scratch. Use `cut -d:`, not `awk '{print $2}'` (positional fields are unsafe in
# a `!`command`` skill injection — the harness substitutes $1/$2 as args).
for d in "$(memex path 2>/dev/null)"/projects/*/; do
  name=$(basename "$d")
  total=$(ls "$d/memos/"*.md 2>/dev/null | wc -l | tr -d ' ')
  digested=$(grep -m1 'memos_digested:' "$d/_project.md" 2>/dev/null | cut -d: -f2 | tr -d ' ')
  condensed=$(grep -m1 'condensed:' "$d/_project.md" 2>/dev/null | cut -d: -f2 | tr -d ' ')
  lines=$(wc -l < "$d/_project.md" 2>/dev/null | tr -d ' ')
  if [ -z "$digested" ] && [ "${lines:-0}" -gt 40 ]; then
    echo "$name: MAINTAINED — ${total} memos, ${lines}-line overview, but no memos_digested frontmatter (add it)"
  else
    undigested=$((total - ${digested:-0})); [ "$undigested" -lt 0 ] && undigested=0
    echo "$name: ${undigested} undigested (${total} total), last condensed: ${condensed:-never}"
  fi
done
```

### Vault Health Check

```bash
# Comprehensive health report (uses native vault + alias commands)
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py status

# Unresolved links (detailed)
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py unresolved --verbose

# Orphans and dead-ends
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py orphans --total
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py deadends --total

# Alias coverage (for link resolution quality)
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py aliases --total

# Crystallization readiness (alias-aware, with delta tracking)
memex check --tier ready

# Topic similarity (find merge candidates)
memex similarity                       # default threshold 0.85
memex similarity --threshold 0.90      # near-duplicates only
memex similarity --threshold 0.90 -v   # with file paths

# Auto-memory sync state (from Claude Code working memory)
memex sync --status
```

### Auto-Memory Sync Check

Run `memex sync --status` to check for new or stale auto-memory files from `~/.claude/projects/*/memory/`. Sync before tending to ensure current project knowledge is in the vault. After syncing new files, add wikilinks in the `## Vault Annotations` section to connect them to the knowledge graph. Files with `volatile: true` (MEMORY.md) change frequently — don't treat as stable references for condensation.

**Interpretation:**
- **5+ undigested memos** → needs condensation
- **`MAINTAINED` (substantial overview, no `memos_digested`)** → NOT never-condensed; the overview is current but predates the frontmatter convention (or was hand-written). Just add `condensed:`/`memos_digested:` frontmatter — don't re-condense from scratch. Distinguishing this from a true stub avoids wasted re-condensation work.
- **Empty `_project.md`** → condensation overdue
- **High unresolved link count** → needs link fixing (but clean frontmatter noise first)
- **Orphans** → isolated notes that need connecting
- **Dead-ends** → notes with no outbound links (should link to topics)
- **Similarity > 0.90** → likely duplicates, merge or alias one to the other
- **Similarity 0.85-0.90** → related but may be distinct, review manually

**Clean noise before measuring.** Frontmatter artifacts, transcript URLs, and template references inflate unresolved counts. Fix these first to see the real signal. In practice, 512 "unresolved" links became 356 after frontmatter cleanup — a 30% reduction before agents even started.

### Suggested Concept Frequency

**Preferred: Use crystallization check** (alias-aware, with maturation tiers and delta tracking):

```bash
memex check              # full report
memex check --tier ready  # actionable items only
memex check -v            # with source files
```

**Fallback: grep for `[[?concept]]` placeholders:**

```bash
grep -rh '\[\[?' $(memex path 2>/dev/null)/projects/ 2>/dev/null | \
  grep -o '\[\[?[^]]*\]\]' | \
  sed 's/\[\[?//;s/\]\]//' | \
  sort | uniq -c | sort -rn | head -20
```

Concepts mentioned 3+ times across projects are strong candidates for topic creation.

### Health Report Format

When presenting diagnosis results, use this tabular format:

| Aspect | Count | Status | Action |
|--------|-------|--------|--------|
| Unresolved links | 356 | Moderate | Fix high-frequency, clean frontmatter noise |
| Orphan notes | 12 | Low | Connect or archive |
| Dead-end notes | 28 | Low | Add outbound links to topics |
| Suggested topics (`[[?...]]`) | 18 (5 high-freq) | Good | Create 5 high-frequency topics |
| Projects needing condensation | 3 (5+ memos each) | Action needed | Assign to condensers |

---

## Condensation: Distilling Memos into Overviews

**Condensation compresses along the time axis** — many sessions become one overview. This is distinct from crystallization (which works the topology axis) and connection (which grows cross-project patterns).

### When to Condense

- **1-4 new memos**: Not worth re-condensing unless they change the architecture
- **5+ new memos**: Worth updating — read the new ones, revise the overview
- **Major architectural change**: Update immediately regardless of count
- **Never condensed**: First condensation is the biggest lift, but most valuable

For projects with 10+ memos, **spawn an Explore agent** to read them all and extract: dates, key decisions, what's superseded, what's still active, open threads. For smaller projects, read directly.

### The Process

#### 1. List Existing Topics First

This is the most common source of errors — linking to topics that don't exist without the `[[?...]]` prefix.

```bash
ls $(memex path 2>/dev/null)/topics/*.md | xargs -I{} basename {} .md | sort
```

**Store this list.** You'll need it for every wikilink you write. If assigning to an agent, include the topic list in their prompt.

#### 2. Read Current `_project.md`

Check what's there already. If it has a `condensed` date in frontmatter, focus on memos since that date. If empty, read all memos.

#### 3. Read Project Memos

```bash
ls -t $(memex path 2>/dev/null)/projects/<name>/memos/*.md
```

For team-based condensation, assign 2-4 projects per agent, grouped by domain affinity (philosophical projects together, tool projects together). An agent that condenses one project in a domain builds context that helps it condense the next.

#### 4. Write the Overview

Use this format:

```markdown
---
type: project
name: <project>
created: <original date>
condensed: <today>
memos_digested: <count>
---

# Project Name

[1-2 sentences: what is this project? Link to relevant [[topics]].]

Workspace: `<path>`

## Current Architecture

[How the system works right now. Link to [[topic-notes]] for detailed subsystems. This section is the most valuable — it's what memos can't give you because each memo only sees one session.]

## Key Decisions in Effect

| Decision | Why | Date |
|----------|-----|------|
| [Active decision] | [Rationale, not just "it's better"] | [When decided] |

[Only decisions that are STILL in effect. Superseded decisions are history — omit unless the supersession itself is a lesson.]

## What's Been Learned

[Hard-won lessons organized by area. The gotchas, the non-obvious, the things that bit you. Link to [[topics]] for the concepts behind the lessons.]

## What's Active

[Current work in progress. What shipped recently. What's being explored.]

## Open Threads

- [ ] [Specific, actionable next step]
- [ ] [Use [[?suggested-topic]] for concepts that deserve a topic]
```

### What to Include

- **Current state** — architecture, decisions, active work
- **Lessons** — gotchas and non-obvious things (the "What's Been Learned" section is often the most valuable)
- **Open threads** — specific enough to act on
- **Wikilinks** — connect to the knowledge graph

### What to Exclude

- **Session play-by-play** — "on Jan 28 we did X, then Y" (that's in the memos)
- **Superseded decisions** — unless the change itself is a lesson
- **Maintenance logs** — not project knowledge
- **Everything in the code** — don't repeat what's readable from source

### Wikilinks: Critical for Connection

A `_project.md` without wikilinks is an island — invisible to backlinks, disconnected from the knowledge graph.

**Rules:**
1. **Check what topics exist first** — don't link to things that don't exist (unless using `[[?...]]`)
2. **Link concepts, not every noun** — link where following the link teaches something this note doesn't
3. **Use `[[?new-concept]]` for suggested topics** — concepts that deserve a home but don't have one yet
4. **Use aliases for readability** — `[[claude-code-plugins|plugin]]` reads better than `[[claude-code-plugins]]`
5. **Project overviews are natural hubs** — they should link outward generously; they're the entry point for a project's knowledge

**Typical link density:** 5-10 wikilinks in a project overview. Fewer than 3 means you're writing in isolation. More than 15 means you're over-linking.

**Quality over completeness.** The best overviews from our first garden tending session shared these traits:
- Captured tensions and trade-offs, not just clean summaries
- Showed progressive understanding (how thinking evolved across sessions)
- Had substantial "What's Been Learned" sections (the hard-won, non-obvious stuff)
- Used wikilinks to connect to concepts where deeper understanding lives

---

## Connection: Growing Topic Notes

### When to Create a Topic

A concept earns its own topic note when:
- It appears in **2+ projects** (cross-project pattern)
- It's **substantial enough to teach** (not just a label — there's real understanding to capture)
- It **evolves over time** (understanding deepens, contradictions emerge, practices refine)
- Following a `[[?suggested-topic]]` link from a project overview

**Don't create topics for:**
- Single-project concepts (keep in `_project.md`)
- Trivial labels ("git", "python") unless there's vault-specific knowledge
- Things that are better as sections within an existing topic

**The cross-pollinator insight:** In team-based tending, the agent that creates topic notes should ALSO condense related projects. Seeing patterns while condensing is where the best cross-project insights come from. An agent that condenses philosophical projects (loom, orchestration) and then creates a topic on "knowledge-metabolism" has lived context that pure topic creation lacks.

### Topic Note Format

```markdown
---
type: concept
title: <Title>
projects: [project1, project2]
related_memos: []
created: <today>
tags: [category1, category2]
---

# Topic Title

[1-2 sentences: what is this? Why does it matter across projects?]

## Where It Appears

[Which projects, what form it takes in each. Be specific — "In alcor, this manifests as..." not "Used in alcor."]

## Current Understanding

[The state of the art in this vault. What we know now. Organize by sub-themes if the concept is rich enough.]

## Open Questions

- [ ] [What's unresolved?]
- [ ] [What contradictions exist between projects?]

## Related

- [[topic-a]] - How it connects
- [[topic-b]] - How it connects
```

### Obsidian Formatting in Topic Notes

Topic notes benefit from callouts and embeds more than memos do. Use them to make knowledge visually scannable:

- `> [!warning]` for gotchas discovered across sessions
- `> [!tip]` for hard-won best practices
- `> [!example]` for concrete project examples
- `> [!question]` for open questions needing resolution
- `![[project/_project.md#Architecture]]` to embed relevant sections from overviews

Full syntax reference: `references/obsidian-formatting.md`

### Quality Bar for Topics

The best topic notes (knowledge-metabolism, multi-agent-review, fork-maintenance) share these traits:

- **Real examples from memos, not abstractions** — "In alcor, the 5-agent vision assessment caught N+1 SSE connection leaks" not "multi-agent review finds more bugs"
- **Tensions and trade-offs** — "manual tracking beats automated merge for semantic conflicts, BUT doesn't scale past 100 files"
- **Progressive understanding** — show how the concept evolved across projects, not just its final form
- **Actionable open questions** — not "should we do X?" but "threshold: >50% files diverged? >6mo since last upstream merge?"
- **200+ lines for substantial concepts** — if it's worth a topic note, there's enough to say. A 30-line topic note is probably a section in another note.

**Link density:** 5-8 wikilinks. Topics are knowledge graph connectors — they should link to related topics AND back to project overviews where the concept lives.

---

## Crystallization: Materializing Ghost Nodes

Condensation compresses along the **time axis** (many sessions → one overview). Crystallization works the **topology axis**: concepts that exist implicitly across many memos become explicit nodes in the graph. The garden analogy: condensation is composting; crystallization is noticing which volunteer seedlings deserve their own bed.

### The Signal

Every unresolved wikilink `[[concept]]` is a vote. When the vault mentions `[[claude-code]]` in 8 places across 4 projects without a landing page, that's a concept ready to crystallize. The graph is already talking about it — it just doesn't have a home yet.

### The Process

**Quick check** (periodic, ~10 seconds):
```bash
memex check
```
This runs alias-aware analysis: gets unresolved links from Obsidian's metadata cache, filters out alias-resolved false positives and noise, classifies by maturation tier, and tracks delta since last run. Use `--tier ready` to see only actionable items, `-v` for source files, `--json` for programmatic use.

**Full crystallization pass** (monthly or when READY/OVERDUE items appear):
1. **Run the check**: `memex check -v`
2. **Review READY/OVERDUE tiers**: these are the candidates
3. **Triage each candidate**:
   - **Variant phrasing** of existing topic → add alias to that topic's frontmatter
   - **Novel concept**, 3+ cross-project references → create new topic stub
   - **Project-specific** or 1-2 references → leave as-is (may mature later)
4. **Materialize**: Create topic stubs for concepts that pass the bar, add aliases for variant phrasings
5. **Re-run check** to verify: resolved items should drop from the report

### Maturation Threshold

Not every unresolved link deserves a topic today. The frequency signal tells you what's *ripe*:

- **1 reference**: Leave as-is — might grow, might not
- **2-3 references, single project**: Probably project-specific. Note but don't act
- **3+ references, cross-project**: Ready to crystallize. Create the topic
- **5+ references**: Overdue. The vault is working around its absence

### Aliases vs New Topics

Most unresolved links are **variant phrasings**, not missing concepts. `[[mcp-architecture]]`, `[[mcp-protocol]]`, `[[mcp-servers]]` all point to the same thing — add them as aliases to the existing topic rather than creating 3 new files.

In practice: ~60% of actionable unresolved links resolve via aliases. ~25% warrant new topic stubs. ~15% are project-specific and should be left alone.

### Team Approach

For crystallization at scale, use two waves:

1. **Discovery wave** (3 Explore agents, parallel): investigate alias candidates, analyze high-frequency topics, classify the long tail. These are read-only — they produce recommendations
2. **Creation wave** (3 general-purpose agents, parallel): create new topic files, add aliases to existing files, create concept stubs. These write based on discovery findings

The lead does initial categorization (noise vs signal) before discovery agents start, then reviews and approves recommendations before creation agents execute.

### Prescription: subagents add return-links during writes

When dispatching a subagent for condensation, crystallization, or any write that references existing topics, **include the return-link prescription in the brief**. The prescription:

> For every existing topic file you reference via `[[topic-name]]` in the updated `_project.md` or new topic note, append a `## Recent signals` entry to that topic file with today's date and a brief note linking back to the relevant memo. Dedup with `grep -Fxq -- "$LINE" "$FILE"` before appending. If the topic has `redirect_to:` in frontmatter, resolve to canonical first via `memex topic resolve <slug>`.

Validated 2026-05-25 (a loom condensation subagent given this prescription appended 21 signals across 13 topics with dedup-discipline + redirect-awareness, no post-pass lead cleanup required). The prior pattern (lead does post-pass return-link cleanup after subagent writes) is now obsolete for any dispatch where the subagent reads the brief carefully. Concrete-brief format applies — list the topic files the subagent should expect to touch, include the dedup snippet inline.

The prescription IS the methodology test. If a subagent ignores or fumbles it, the issue is brief-shape (open-brief vs concrete-brief) or model selection, not the prescription. Audit by `git diff --stat topics/*.md` after the dispatch returns; count touched files against the subagent's self-report.

---

## Growth: Merging and Expanding

### When to Merge Topics

Topics should be merged when:
- Two notes cover the same concept with different names (`obsidian-integration` + `obsidian-vault-management`)
- One topic is a subtopic that's too thin to stand alone (`plugin-architecture` → section in `plugin-development`)
- A synthesis report identifies overlap

### Merge Process

1. **Read ALL source files** completely before writing anything
2. **Identify the target**: merge INTO the most comprehensive existing note, or create a NEW canonical note
3. **Check for duplicate content**: If the target already has a "Development Workflow" section and a source also has one, DON'T paste both — synthesize into one. This was the most common error in practice.
4. **Write the merged note**: Combine non-redundant content. Preserve the best version of each section.
5. **Archive sources** (don't delete):
   - Add `status: archived` to frontmatter
   - Add `**Note**: Merged into [[target-note]].` at top of body
6. **Update aliases**: Add old topic name as alias in target note's frontmatter (so existing wikilinks still resolve)
7. **Verify link resolution**: Confirm that aliases added in step 6 cover all inbound wikilinks to source files. Use `cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py backlinks <source-name>` to check. If any links don't resolve via aliases, update them manually or report for the lead to fix.

**Gotchas:**
- **Always diff before merge**: Read both the source and target. If the target already covers something, don't add a duplicate.
- **Aliases preserve links**: Adding `obsidian-integration` as alias on `obsidian-vault.md` means `[[obsidian-integration]]` still resolves in Obsidian. This reduces the urgency of updating all references.
- **Archive, never delete**: The old file with its `status: archived` marker serves as a redirect for anyone following old links.

---

## Trails: Extending Narrative Knowledge

Trails (`type: trail` in frontmatter) are narrative artifacts that trace how understanding of a concept evolved across projects and time. They live in `topics/` alongside encyclopedic topics but have different maintenance:

- **Topics** are **rewritten** — synthesize current understanding, replace old content
- **Trails** are **extended** — append new chapters, old content stays

### When to Suggest a Trail

During diagnosis or condensation, notice when:
- A concept spans **3+ projects** with significant temporal evolution
- The same idea appears across **5+ memos** over **2+ months**
- Understanding visibly **shifted** (not just accumulated — actually changed direction)
- A topic page can't capture the journey (it only captures current state)

Propose to the user: "Your understanding of X has evolved across N memos and M projects over K months. Should I create a trail?"

### Trail Format

```markdown
---
type: trail
title: "<Narrative title — not just the concept name>"
concept: <topic-slug>
projects: [project1, project2, ...]
started: <earliest-memo-date>
last_extended: <today>
status: active
aliases: [<concept> trail]
tags: [trail, ...]
---

# <Narrative Title>

<1-2 sentence hook: what's the arc?>

## <Phase 1 title> (<date range>)
<What happened, what was tried, what was learned>
Sources: [[memo-link|description]], [[memo-link|description]]

## <Phase 2 title> (<date range>)
...

## What the Journey Taught
<The thesis — the shape of the learning arc>

## Open Questions
<What's unresolved>
```

### Extending a Trail

When a trail has accumulated recent signals (check `## Recent signals` section), or when new memos clearly advance the narrative:

1. Read the trail's existing chapters
2. Read the new memos that touch this concept
3. Decide: does this extend the story or just repeat it?
4. If it extends: append a new chapter section before "What the Journey Taught"
5. Update `last_extended` in frontmatter
6. Clear incorporated signals from `## Recent signals`

**Don't rewrite existing chapters.** The temporal structure is the point.

### Staleness-Aware Tending

Topics and trails accumulate "Recent signals" — one-line pointers appended by `/memex:save` when a memo references them. During diagnosis, check which topics/trails have the most unincorporated signals:

```bash
# Find topics/trails with recent signals
for f in $(memex path 2>/dev/null)/topics/*.md; do
  count=$(grep -c "^- " <(sed -n '/## Recent signals/,$ p' "$f") 2>/dev/null)
  if [ "$count" -gt 0 ]; then
    name=$(basename "$f" .md)
    type=$(grep -m1 '^type:' "$f" | awk '{print $2}')
    echo "$count signals: $name ($type)"
  fi
done | sort -rn
```

Prioritize tending for topics/trails with 3+ accumulated signals. For trails, this means extending. For topics, this means rewriting/updating. After tending, remove the incorporated signals from the `## Recent signals` section.

---

## Maintenance: Keeping the Graph Healthy

Vault health work is fixing broken links, adding aliases, creating missing topics, archiving stale files, and maintaining base views. The same team approach applies — different roles, same coordination.

### Base Views (`_views/*.base`)

When creating or modifying `.base` files, consult `references/bases-functions.md` for the full formula function reference (date math, string ops, duration gotchas, list operations). Key pitfalls:
- Duration from date subtraction needs `.days`/`.hours` before `.round()`
- Formulas with double quotes must be wrapped in single quotes in YAML
- Guard empty properties with `if(prop.isEmpty(), fallback, prop)`

### Link-Safe File Operations (Obsidian CLI 1.12.5+)

When Obsidian is running, **always use CLI `move`/`rename` instead of manual file operations.** These commands tell Obsidian to update all backlinks automatically — the #1 source of broken links during garden-tending is manual renames.

```bash
# Rename a topic (Obsidian updates all [[old-name]] → [[new-name]])
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py rename old-topic-name new-topic-name

# Move a file to a different folder (backlinks updated)
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py move my-note topics/

# Delete non-knowledge files only (empty placeholders, config artifacts)
# For topic/memo files, always archive instead — see Archival section
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py delete stale-config-file

# Remove a property from frontmatter
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py property-remove old-property --file=my-note
```

**When Obsidian is unavailable** (cron, CI, headless): fall back to manual file operations + alias-based link preservation (add old name as alias in target frontmatter).

### Link Fixing

**Two types of fixes:**

1. **Alias-based resolution** (mechanical): Link exists, name doesn't match. Add alias to target topic's frontmatter. Example: `grounded-theory.md` with alias `gioia-methodology` resolves 6 `[[gioia-methodology]]` links without creating new files.

2. **Topic creation** (creative): High-frequency missing link represents a real concept. Research memos, create substantial topic note. Example: `[[organizational-intelligence]]` appeared 8 times across projects — created 172-line topic note.

**Triage what's fixable:**
- Many "broken links" are intentional (`[[?suggested]]`) or transcript noise (URLs, coordinates)
- Focus on: links where topic exists but isn't found (aliases needed), links in topic files (high-value connectors), high-frequency missing links (worth creating)

**For teams:** Assign alias additions and mechanical fixes to a "link-fixer" agent. Assign topic creation for high-value concepts to a "topic-creator" agent. The lead handles frontmatter cleanup (noise reduction) before agents start, then reviews after.

### Archival and Deduplication

**Archive candidates:**
- Superseded memos (decision changed, approach abandoned)
- Near-identical duplicate files (conversation-importer has two, keep better one)
- Empty placeholder files from templates
- Stale system files (old config schemas, replaced scripts)

**Process:**
- Add `status: archived` to frontmatter
- Add note at top explaining why archived and pointing to replacement if one exists
- DO NOT delete — archived files serve as breadcrumbs in the knowledge graph

**Human judgment is critical.** Claude can identify 4 topics that look stale. The human decides whether a 3-week-old vault is too young to prune. The vault is a garden, not a warehouse — some "stale" notes are dormant seeds.

---

## Team-Scale Operations

For vault-wide tending, use a **team approach** — human + Claude lead + Sonnet agents working in parallel. This is the big unlock: growing the vault faster than one session could, while maintaining quality through review.

### Designing the Team

This is a conversation between human and Claude. Based on the synthesis report or assessment:

1. **Which projects need condensation?** (5+ undigested memos, or never condensed)
2. **What cross-project topics should be created?** (patterns that appear in 2+ projects)
3. **What topics should be merged?** (overlapping notes, thin subtopics)
4. **What to archive or deduplicate?** (superseded memos, near-identical entries)

**The human decides priorities.** Maybe some projects can wait. Maybe a topic isn't worth creating yet. Claude proposes, human approves.

### Team Structure for Garden Tending

**For condensation + topic creation:**
- **2-3 condenser agents** (Sonnet): 2-4 projects each, grouped by domain affinity
- **1 cross-pollinator** (Sonnet): 1-2 projects + topic creation + topic merges
- **Claude (Opus) as lead**: designs tasks, reviews quality, fixes wikilinks, coordinates

**For vault health:**
- **1 link-fixer** (Sonnet): fix broken wikilinks, add aliases, remove dead references
- **1 topic-creator** (Sonnet): research and create substantial topic notes for high-frequency missing links
- **Claude (Opus) as lead**: frontmatter cleanup, archival, task assignment, review

**Domain grouping matters.** An agent that condenses a philosophical project (loom) builds context that helps it condense another philosophical project (orchestration). Group by:
- Philosophical / research projects together
- Tool / infrastructure projects together
- Platform / architecture projects together

**The cross-pollinator is the key role.** This agent does condensation AND topic creation. Seeing patterns while condensing is where the best cross-project insights come from. Give this agent the projects most relevant to the topics they'll create.

### Preparing Agent Prompts

Each agent needs:

1. **Vault path** and assigned project paths
2. **The condensation format** (from earlier in this skill)
3. **The current topic list** (so they can write correct wikilinks)
4. **Specific instructions** for each task (what to read, what to write, where)
5. **For the cross-pollinator**: guidance on what topics to create, what to merge, dedup targets

**Critical: include the topic list in every agent prompt.** This is the #1 source of errors. Agents that don't know what topics exist will link to non-existent topics.

Example topic list block for agent prompts:
```
## Existing topics for wikilinks:
agent-architecture, claude-code-hooks, claude-code-plugins, epistemic-responsibility,
fork-maintenance, knowledge-metabolism, knowledge-topology, memex-project,
multi-agent-review, multi-agent-systems, obsidian-vault, plugin-development,
qualitative-research, third-space, trust-calibration, ...
```

### Execution and Monitoring

Agents work in parallel. While they work, the lead can:

- **Do their own tasks**: update the memex overview, archive duplicates, fix wikilinks, merge small directories
- **Monitor completion**: check in on agents via Task tool, review as overviews come in
- **Quality-check early outputs**: don't wait until all agents finish — review the first completed overview while others are still working, catch issues early, send corrections to agents if needed

**The human watches too.** The team shows agent activity. If something looks off — an agent is summarizing instead of synthesizing, or producing thin overviews — the human or lead can redirect.

### Review: The Most Important Step

After all agents complete, **every overview, topic note, and merge gets reviewed.** Budget real time for this — it's half the work.

**Review checklist:**

1. **Wikilink validation**: Every `[[topic]]` must either exist OR use `[[?topic]]` prefix. This is the most common error. Use `cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py check-links --path=<file>` to validate all links in a file after writing.
2. **No duplicate sections**: Especially in merged files. Check for repeated headings.
3. **Frontmatter complete**: `condensed` date, `memos_digested` count, correct `type`
4. **Link density**: 5-10 wikilinks per overview. <3 means isolated, >15 means over-linked.
5. **Open threads are actionable**: "Reduce SSE reconnect leak" not "improve performance"
6. **Archived sources have markers**: `status: archived` + merge pointer
7. **Quality of understanding**: Does this read like wisdom or a summary? The best overviews capture tensions, trade-offs, and "why" — not just "what."

In practice, the review pass caught issues in a third of the outputs. This isn't a sign of agent failure — it's the design. Agents produce drafts at scale. The lead ensures quality. Garden tending is a two-pass operation.

### Cleanup

1. **Wait for agents to complete** (all spawned Task tools finish)
2. **Run incremental index rebuild** to pick up all changes:
   ```bash
   memex index rebuild --incremental
   ```
3. **Plugin reinstall** if skills changed:
   ```bash
   claude plugin uninstall memex@memex-local --scope user && claude plugin install memex@memex-local --scope user
   ```

### Practical Constraints

**Max 4-5 tasks per agent.** An agent that creates three 200+ line topic notes will hit context limits. If you need 8 tasks for the cross-pollinator, split into two agents.

**Task ordering matters.** Condensation first → topics second → merges last. Agents that condense first see patterns that produce better topic notes. Merges need all topics in place.

**Synthesis → garden tending should be separate sessions.** Synthesis is analytical (finding patterns, contradictions). Garden tending is productive (writing overviews, topics). Running both in one session exhausts context. The synthesis report is the handoff artifact.

**Weekly cadence for active vaults.** If 5+ projects are generating memos weekly, garden tend weekly (even if light — just the projects with new memos). Monthly for stable vaults. The first garden tending is the biggest (condensing everything). Subsequent ones are incremental.

### Roles

| Role | Who | Does | Decides |
|------|-----|------|---------|
| **Gardener** | Human | Brings synthesis report, sets priorities, watches, judges quality | What to condense, what topics matter, what to archive |
| **Lead** | Claude (Opus) | Designs team, creates tasks, reviews all output, fixes wikilinks | Team structure, task assignment, quality corrections |
| **Condensers** | Sonnet agents | Read memos, write overviews | How to structure each overview (within the format) |
| **Cross-pollinator** | Sonnet agent | Condense + create topics + merge | Which patterns are substantial enough for topics |
| **Link-fixer** | Sonnet agent | Fix broken wikilinks, add aliases | Which aliases to add, which references to remove |
| **Topic-creator** | Sonnet agent | Research and create topic notes | Which missing links are worth creating topics for |

**The human's judgment is irreplaceable.** Claude can identify that 4 topics look stale. The human decides whether a 3-week-old vault is too young to prune. Claude proposes archiving a memo. The human knows the duplicate in conversation-importer is the better version. The team executes; the human and Claude decide.

---

## Execution Patterns from Practice

These patterns emerged from multi-session vault work. They refine the lifecycle steps above — same phases, sharper execution.

### 1. 4-Wave Execution Architecture

After diagnosis, large garden-tending sessions benefit from a staged wave structure:

| Wave | Focus | Risk | Agents |
|------|-------|------|--------|
| **1 — Quick Wins** | Wiring orphaned topics into overviews, archiving confirmed stubs | Low | 2-3 Sonnet |
| **2 — Expansion** | Growing thin hub topics, creating topics for blind spots | Medium | 2-3 Sonnet |
| **3 — Consolidation** | Merging overlapping topics, expanding underweight ones | Medium | 2-3 Sonnet |
| **4 — Export/Bridging** | Creating bridging topics, materializing philosophical foundations | High | 2-3 Sonnet |

Run waves sequentially (each builds on the previous). Within each wave, agents work in parallel. Quick wins first — establishing stable ground before bolder moves.

**Why the order matters:** Quick wins (archival, wiring) are confidence-building and low-stakes. Expansion requires existing structure as anchor. Consolidation needs all pieces in place. Bridging is the riskiest move — it invents new topology — and should only happen after the vault is internally coherent.

### 2. Vault Cartography (Deep Diagnosis)

When "scratching the surface" isn't enough, deploy 4 specialized Explore agents in parallel before any editing starts:

- **Hub & Spoke Mapper**: backlink counts on all topics, identify clusters and isolates
- **Depth Auditor**: line counts + maturity tiers (Mature 200+, Developing 50-200, Stub < 50)
- **Blind Spot Hunter**: grep memos for recurring themes that have no topic yet
- **Ecosystem Mapper**: project-to-project adjacency via shared topic mentions

This produces a full map before execution: which topics are hubs, which are orphaned, which cross-project themes are unhoused. Design the execution waves against this map — not against intuition.

**Cost:** 4 Explore agents reading ~100 files = moderate token usage. **Return:** Execution that hits real targets instead of surface-visible ones.

```bash
# Quick topology from CLI before spawning agents
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py files --folder=topics --total
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py orphans --total
cd $(memex path 2>/dev/null) && uv run scripts/obsidian_cli.py deadends --total

# Then augment with grep-based blind spot hunting:
grep -rh '\[\[' $(memex path 2>/dev/null)/projects/*/memos/*.md 2>/dev/null | grep -o '\[\[[^]]*\]\]' | sort | uniq -c | sort -rn | head -40
```

### 3. Conservative Stub Analysis

Before archiving any stub topics, launch an Explore agent to read ALL candidates and classify each one. Do not archive based on names alone.

**Why:** Most stubs are legitimately distinct — in practice, ~10/20 apparent overlaps turned out to be genuinely different concepts. Only archive when content is genuinely subsumed by a parent topic.

**Classification checklist per stub:**
- Does the parent topic already cover this content?
- Is there unique content, examples, or angle worth preserving?
- Is the stub referenced from multiple projects (suggests real need)?
- Is the stub a variant phrasing (alias candidate) or a different concept?

**Decision tree:**
- Genuinely subsumed → archive with `status: archived` + pointer to parent
- Variant phrasing → add as alias to parent; delete stub
- Distinct but thin → expand rather than archive
- Cross-project + distinct → keep, mark for expansion in wave 2

### 4. Bridging Topic Strategy

When meta-knowledge is insular (topics that only link back to themselves, not to broader domains), create **bridging topics** that extract transferable principles and connect them outward.

**Signs you need bridging topics:**
- A cluster of topics all link to each other but not to the rest of the vault
- A project's `_project.md` has only self-referential wikilinks
- The same principle keeps appearing in unrelated domains without a shared vocabulary

**What bridging topics are NOT:** They're not cross-reference lists. They're not summaries. They extract the underlying insight and reframe it for transfer to other domains.

**Example:** `prompt-engineering` and `memex-architecture` both reference ideas about "progressive compression of knowledge." A bridging topic on [[?knowledge-compression]] extracts the principle in domain-neutral terms, linking both directions.

**Budget 1 wave for bridging** — it requires the most judgment and the most vault knowledge. Don't attempt it in wave 1 when the vault's current topology is still unclear.

### 5. Wiring Orphaned Topics

After creating new topics, immediately wire them into relevant project overviews. Don't batch this as a cleanup step.

**A topic with 0 backlinks is invisible to backlink traversal.** It exists in the vault but isn't part of the graph. Wiring happens by adding `[[new-topic]]` references in the 2-3 project overviews most relevant to that topic.

**Workflow:**
1. Create topic file
2. Identify 2-3 projects that reference this concept in their memos
3. Add wikilink in each `_project.md` under "Current Architecture" or "What's Been Learned"
4. Run `check-links` on the edited files

This can be done by the same agent that created the topic (cheaper, has context) or by the lead during review (catches agent-missed connections).

### 6. Budget Efficiency

Sonnet agents for vault-grounded edits (read → search → edit → validate) + Opus orchestration = very high throughput at low cost.

**Feb 26 benchmark:** 15 agents, two context windows, 7 topics created, 7 expanded, 10 archived, 3 consolidated, 17+ wikilinks wired, 10 projects condensed, ~76 memos digested — at ~3% of weekly agent budget.

**Why it's efficient:**
- Sonnet is strong enough for read-and-write vault work (full comprehension, coherent prose)
- Opus is used only for orchestration (wave design, task assignment, quality review) — maybe 20% of total calls
- Parallel agents multiply throughput without multiplying cost linearly
- Explore agents (read-only) are cheaper than full-capability agents; use them for cartography

**When to use Opus agents:** For bridging topic creation (highest judgment), for multi-project synthesis (needs cross-project pattern recognition), and for the lead review pass (quality is load-bearing).

### 7. Post-Execution Index Rebuild

After any major vault change (multiple topic creates/edits/archives), run the incremental index rebuild before synthesizing or searching for new content.

```bash
memex index rebuild --incremental
```

New topics are not keyword-searchable until indexed. New backlinks are not reflected in graph queries until indexed. Run this as a wave 4 cleanup step or at session end.

**Then optionally:** Run cross-session synthesis to surface patterns in the rewired topology:

```bash
memex search --since=7d "<pattern query>"
```

Search for recent memos and synthesize across them — the rewired topology often makes new patterns visible that weren't apparent before.

---

## Practice Notes

### Session 1: First Garden Tending (Feb 14)

**Setup:** 4 Sonnet agents + Opus lead, 13 tasks, synthesis report as input.

**Results:** 11 projects condensed (~209 memos digested), 3 cross-project topics created (200+ lines each), 3 topic merges completed, duplicates archived, empty directory merged.

**What produced the best results:**
- Agents that read ALL memos before writing (not skimming) produced richer overviews
- The cross-pollinator produced the best topic notes because it had already condensed related projects — condensation builds context for topic creation
- Overviews that captured tensions and trade-offs were more valuable than clean summaries
- Human judgment on what NOT to do was as valuable as execution (decided not to archive "stale" topics in a 3-week vault)

**What needed post-review fixes:**
- 4 of 11 overviews had broken wikilinks (agents linked to non-existent topics without `[[?...]]`)
- 1 merged file had duplicate sections (merge didn't check for existing content)
- 1 agent hit context limits (8 tasks including three 200+ line topics — too many)
- Stale wikilinks in older files needed updating after topic merges

**Key insight:** Garden tending is a two-pass operation. Pass 1 is the agents writing. Pass 2 is the lead reviewing. Budget time for both.

### Session 2: Vault Health (Feb 14)

**Setup:** 2 Sonnet agents (link-fixer, topic-creator) + Opus lead, 6 tasks.

**Results:** Unresolved links 512 → 356 (30% reduction). 64 files had frontmatter cleaned (348 topic references). 2 deep topic notes created (organizational-intelligence 172 lines, human-ai-collectives 243 lines). 6 stale system files archived. Aliases added to 30+ topic files. Broken links fixed in 5 topic files.

**What produced the best results:**
- **Prep before agents**: Frontmatter cleanup script eliminated ~350 false-positive unresolved links before agents even started. Cleaning noise first makes the real problems visible
- **Fewer agents, deeper work**: 2 agents instead of 4 meant each could focus deeply. Topic-creator spent significant time reading memos before writing — both topics were excellent
- **Alias-based resolution**: Adding aliases to existing topic frontmatter resolved many broken links without creating new files
- **Lead fills gaps during review**: Lead caught broken links the link-fixer missed and unwrapped `[[?topic]]` links where topics had just been created

**What needed post-review fixes:**
- Link-fixer didn't fix 2 skill-name links in `collaborative-writing.md` (these are skills, not topics, so the agent couldn't find a target)
- New topic creation meant some `[[?topic]]` links could now be unwrapped — lead caught 3 of these

**Key insight:** Vault health is best as a two-phase operation. Phase 1: noise reduction (frontmatter, archival). Phase 2: team handles real fixes. Without Phase 1, the signal-to-noise ratio makes triage difficult.

### Session 3: Graph Crystallization (Feb 14)

**Setup:** 3 Explore agents (Haiku + 2 Sonnet) for discovery, then 3 general-purpose agents for creation. Two-wave approach: discover first, create second.

**Results:** 365 unresolved links analyzed. 8 new topic files created (2 umbrella topics, 6 concept stubs). 35 aliases added across 15 existing files. CLI unresolved: 365 → 356, but ~50-60 additional resolved via Obsidian alias resolution (CLI only checks filenames, not frontmatter aliases).

**Discovery phase breakdown:**
- 55 pure noise (coordinates, @handles, URL fragments)
- 22 intentional `[[?concept]]` breadcrumbs — working as designed
- ~12 template placeholders
- ~39 variant phrasings resolved via aliases to existing topics
- 8 cross-project concepts materialized as new topic files
- ~220 single-reference project-specific terms — left as-is

**Key insight:** Crystallization is distinct from condensation. Condensation compresses along the time axis (sessions → overview). Crystallization works the topology axis (scattered references → formalized concept). The maturation signal is already in the graph — frequency of unresolved links tells you what's ripe. Periodic crystallization passes let the vault grow organically as concepts accumulate enough references to warrant formalization.

### Session 4: Mega Garden-Tending — Vault Cartography + 4-Wave Execution (Feb 26)

**Setup:** Full vault cartography diagnosis followed by 4 execution waves across two context windows. 15 agents total (Sonnet + Opus orchestration). Spanned two sessions due to context limit after wave 2.

**Results:** 10 projects condensed (alcor, loom, memex, atlas, predictive-ai, pi-proxy, linxule.com, duality, duality-paper, mcp-workspace). 7 new topic files created. 7 existing topics expanded. 10 stubs archived. 3 topics consolidated. 17+ wikilinks wired into project overviews. Vault grew 99 → 106 topics; mature topics 11 → 14. ~76 memos digested. All at ~3% of weekly agent budget.

**Wave structure:**
- **Wave 1 (Quick wins):** Stub archival + link wiring for newly created topics. Used Explore agent to read ALL stub candidates before any archival decisions — found ~10/20 were legitimately distinct. Wired orphaned topics into project overviews immediately after creation.
- **Wave 2 (Dedicated expansion):** Thin hub topics expanded; 3 new topics for recurring themes with no home. Each agent handled 2-3 topics with deep memo research.
- **Wave 3 (Consolidation — session 2):** Merged overlapping topics (e.g., parallel academic topics that diverged). Expanded underweight topics in adjacent domains.
- **Wave 4 (Export + bridging):** Created bridging topics to connect insular meta-knowledge clusters to broader domains. Philosophical foundations materialized where they'd been referenced but never written.

**Vault cartography (deep diagnosis):**
Before execution, deployed 4 specialized Explore agents in parallel:
- Hub & Spoke Mapper: backlink counts on all topics, identified clusters and isolates
- Depth Auditor: line counts + maturity tiers across all 99 topics
- Blind Spot Hunter: grep memos for recurring themes with no topic home
- Ecosystem Mapper: project-to-project adjacency via shared topics

This produced a full map: which topics were hubs (many backlinks), which were thin (< 50 lines), which had no backlinks (orphaned), and which cross-project themes had no topic at all. Execution waves were designed against this map.

**What produced the best results:**
- Vault cartography before execution made the work legible — agents had clear targets, not vague instructions
- Conservative stub analysis (Explore agent classifies ALL candidates before any archival) prevented destroying legitimately distinct topics
- Wiring topics into overviews immediately after creation (not as a cleanup step) ensured they weren't invisible
- 4-wave architecture let each wave build on the previous: quick wins first freed attention for expansion; consolidation after expansion had all pieces in place

**What needed post-review fixes:**
- Several topic expansions needed `check-links` validation — agents sometimes linked to `[[?concept]]` placeholders for concepts that already existed
- Bridging topics required human judgment: Claude proposed 5, human approved 3 (2 were too abstract to anchor in real vault content)
- Index rebuild required after session 2 to make new topics searchable

**Key insight:** The 4-wave architecture works because each wave has a different risk profile. Quick wins (archival, wiring) are low-risk, high-confidence. Expansion is medium-risk (judgment about what to add). Consolidation is medium-risk (judgment about what to merge). Bridging is highest-risk (judgment about what new topology to create). Running them in order lets you establish stable ground before making bolder moves.

---

## Quality Check

Before saving any condensation artifact (overview, topic, or merge):

1. Does someone starting a session tomorrow know enough to continue work?
2. Are there wikilinks to relevant topics? (Check they exist against the topic list!)
3. Are superseded decisions removed (not just marked)?
4. Are open threads specific enough to act on?
5. Does the "What's Been Learned" section capture things you can't learn from reading the code?
6. For topic notes: are there real examples from memos, not just abstractions?
7. For merges: is there any duplicated content between sections?
