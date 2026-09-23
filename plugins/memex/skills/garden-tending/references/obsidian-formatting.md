# Obsidian Formatting Reference

> Upstream source: [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) (absorbed 2026-04-03)
> Re-check upstream periodically for new callout types or syntax changes.

## Callouts

Callouts highlight information in notes. Use them in topic notes and project overviews for warnings, tips, and examples. Avoid overuse in memos — the memo format handles structure already.

### Syntax

```markdown
> [!type]
> Content here.

> [!type] Custom Title
> Content with a custom title.

> [!type]- Collapsed by default
> Hidden until expanded.

> [!type]+ Expanded by default
> Visible but collapsible.
```

### Nesting

```markdown
> [!question] Outer
> > [!note] Inner
> > Nested content
```

### Callout Types

| Type | Aliases | Color | Use for |
|------|---------|-------|---------|
| `note` | — | Blue | General annotations |
| `abstract` | `summary`, `tldr` | Teal | Section summaries |
| `info` | — | Blue | Context, background |
| `todo` | — | Blue | Action items |
| `tip` | `hint`, `important` | Cyan | Best practices, insights |
| `success` | `check`, `done` | Green | Confirmed outcomes |
| `question` | `help`, `faq` | Yellow | Open questions |
| `warning` | `caution`, `attention` | Orange | Gotchas, pitfalls |
| `failure` | `fail`, `missing` | Red | What didn't work |
| `danger` | `error` | Red | Critical issues |
| `bug` | — | Red | Known bugs |
| `example` | — | Purple | Concrete examples |
| `quote` | `cite` | Gray | Quotations |

### When to Use in Memex

**Topic notes and project overviews:**
- `> [!warning]` for gotchas and pitfalls discovered across sessions
- `> [!tip]` for hard-won best practices
- `> [!example]` for concrete examples from projects
- `> [!question]` for open questions that need resolution

**Memos:** Rarely needed — the memo format (Key Decisions, Surprises, Open Threads) already structures this. Use only when a gotcha is critical enough to visually stand out.

---

## Embeds

Embeds inline content from other notes, images, and PDFs directly in the current note.

### Notes

```markdown
![[Note Name]]                  # Full note
![[Note Name#Heading]]          # Specific heading section
![[Note Name#^block-id]]        # Specific block
```

### Images

```markdown
![[image.png]]                  # Full size
![[image.png|300]]              # 300px width
![[image.png|640x480]]          # Explicit dimensions
![Alt text](https://url)|300    # External image with width
```

### Audio and PDF

```markdown
![[recording.mp3]]              # Inline audio player
![[document.pdf]]               # Full PDF
![[document.pdf#page=3]]        # Specific page
![[document.pdf#height=400]]    # Custom height
```

### When to Use in Memex

- **Topic notes:** Embed key sections from project overviews to compose a concept view
- **Project overviews:** Embed a relevant base view or diagram
- **Avoid:** Embedding full memos (link instead — memos are referenced, not inlined)

---

## Tags

### Hierarchy

Tags support nesting with `/`:

```markdown
#category/subcategory
#project/memex
#status/active
```

### Rules

- Can contain: letters (any language), numbers (not first char), `_`, `-`, `/`
- Cannot start with a number
- Case-insensitive in search

### Memex Convention

Memex uses `topics: []` in frontmatter rather than inline tags. Use `#tags` sparingly — they're redundant with the `topics` array for most vault queries.

---

## Comments

Hide content from reading view:

```markdown
%%This is hidden%%

%%
Multi-line
hidden content
%%
```

## Highlights

```markdown
==highlighted text==
```
