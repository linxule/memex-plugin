---
description: Ask a complex question of the memex vault with deep retrieval
allowed-tools: Read, Bash
argument-hint: "<question>"
effort: max
---

# Ask Memex Command

Use deep retrieval for "why", "how did we", or cross-session pattern questions.

## Instructions

1. Run deep retrieval:
```bash
memex ask "<question>"
```

2. For thorough (semantic + keyword) retrieval:
```bash
memex ask "<question>" --depth=thorough
```

3. Synthesize across `results` and `observations`.
4. Cite memo paths when making claims.
