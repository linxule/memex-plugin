# Gemini credentials

Keyword search works without a Gemini key. For semantic search, obtain your own
key from [Google AI Studio](https://aistudio.google.com/apikey), then choose how
Memex receives it. See Google's [API key guide](https://ai.google.dev/gemini-api/docs/api-key)
for account setup.

## Use 1Password for one command

If your environment-reference file defines `GEMINI_API_KEY`, run:

```sh
op run --env-file ~/.secrets.op -- memex search "your query"
```

Use your own env-file path if different. The file contains `op://` references;
[`op run`](https://developer.1password.com/docs/cli/reference/commands/run/)
resolves them for the child command. 1Password handles authorization. Memex
does not invoke `op`, inspect the vault, or save the injected key automatically.

## Save a key for automatic loading

```sh
memex auth set-key
```

Enter the key at the hidden prompt. This explicitly saves an **unencrypted local
file**, readable and writable only by its owner (mode `0600`). The default path
is `~/.memex/credentials/gemini-api-key`, outside the knowledge vault. A custom
`state_dir` / `MEMEX_STATE_DIR` selects a different installation's state location.
Subsequent commands load the saved key without a shell export or 1Password prompt.
Run the same command again to replace it.

To explicitly save a key already injected into the process environment:

```sh
op run --env-file ~/.secrets.op -- memex auth set-key --from-env
```

This command deliberately keeps a local copy; ordinary `op run ... memex search`
does not. Setup never sends the key to Gemini to validate it.

```sh
memex auth status     # Source only; does not print the key or contact Gemini
memex auth clear-key  # Remove the saved local copy
```

Credential precedence is:

1. The variable selected by `embeddings.api_key_env` (default `GEMINI_API_KEY`).
2. `GOOGLE_API_KEY`, when the configured variable is the default name.
3. The locally saved key.

Memex passes the selected key explicitly to the SDK. Environment variables can
therefore override a saved key for one command. Clearing the saved copy does not
unset those variables. An unresolved `op://` reference produces guidance to use
`op run`; it is never sent as an API key.

Set `embeddings.enabled` to `false` in configuration to disable embeddings,
including credential/provider initialization, while retaining keyword search.

## Sending observation JSON

Shell quoting can corrupt JSON before Memex receives it. Prefer a file:

```sh
memex backfill obs --store-json /absolute/path/observations.json \
  --doc-path projects/example/memos/example.md
```

Or use a quoted heredoc, which preserves apostrophes and backslash escapes:

```sh
memex backfill obs --stdin --doc-path projects/example/memos/example.md <<'JSON'
[{"content":"Claude's observation","obs_type":"explicit","confidence":"high"}]
JSON
```

If valid JSON is already in a shell variable, use `printf '%s\n' "$json"` to pipe
it. Avoid `echo`: shell-specific escape handling can alter JSON. Malformed input
now reports its source and line/column before touching the index.
