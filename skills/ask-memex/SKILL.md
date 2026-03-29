---
name: ask-memex
effort: max
description: |
  Ask a complex question about the memex vault — search, cross-reference, and synthesize from memos and observations. Use for "why did we...", "what was the reasoning behind...", cross-project pattern questions, or when simple keyword search isn't enough.

  <example>
  Context: User asks about a past architectural decision
  User: "Why did we reject the Honcho data model?"
  Assistant: Runs `memex ask` with thorough depth, synthesizes from observations and memos.
  <commentary>
  Complex "why" question spanning multiple sessions — ask retrieves observations (atomic facts) plus document context, enabling cross-referencing.
  </commentary>
  </example>

  <example>
  Context: User asks about cross-project patterns
  User: "What patterns do we use for config management across projects?"
  Assistant: Runs `memex ask`, finds observations from memex, my-app, and research-project projects.
  <commentary>
  Cross-project pattern question — ask searches observations across all projects and merges results via RRF scoring.
  </commentary>
  </example>
allowed-tools: Read, Bash
---

Run deep retrieval via CLI:

```bash
memex ask "<question>"
```

For thorough (semantic + keyword) retrieval:
```bash
memex ask "<question>" --depth=thorough
```

After receiving results:
1. Read the `content` field of each result directly.
2. Check `observations` for atomic facts that answer the question.
3. Synthesize across results for agreements, contradictions, and evolution over time.
4. Cite source memo paths when making claims.
5. Note anything missing from `query_info.gaps`.
6. Offer `/memex:load <path>` when a source memo deserves deeper reading.
