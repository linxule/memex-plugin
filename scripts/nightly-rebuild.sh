#!/usr/bin/env bash
# Nightly memex index rebuild + embed-missing retry.
#
# Generic wrapper: invoke from launchd (macOS), cron (Linux/macOS), or
# systemd timer (Linux). The public repo does not ship a launchd plist
# because the label is inherently per-user — see SETUP.md for a templated
# example you can adapt.
#
# Logs to ~/.memex/logs/nightly-rebuild{,-error}.log when the scheduler
# redirects stdio there (recommended).
#
# Exit codes:
#   0  — rebuild ran and any embed gaps backfilled
#   1  — rebuild failed
#   2  — rebuild ok, but some embeddings still missing after retry
#   3  — ~/.secrets exists but failed to source (syntax error, bad line, etc.)
set -euo pipefail

LOG_DIR="${HOME}/.memex/logs"
mkdir -p "${LOG_DIR}"

# Timestamp every run — launchd appends to the log file, so runs run together.
echo "===== nightly-rebuild @ $(date -Iseconds) ====="

# Gemini key (and anything else) lives in ~/.secrets. Launchd does not inherit
# shell env, so we source it explicitly. We toggle off `set -e` around the
# source so a malformed file surfaces as a clear log line rather than an
# invisible early abort under launchd.
if [[ -r "${HOME}/.secrets" ]]; then
  set +e
  # shellcheck disable=SC1091
  source "${HOME}/.secrets"
  src_rc=$?
  set -e
  if [[ ${src_rc} -ne 0 ]]; then
    echo "ERROR: sourcing ~/.secrets failed (rc=${src_rc}) — aborting" >&2
    exit 3
  fi
else
  echo "WARN: ${HOME}/.secrets not found — rebuild will fall back to FTS-only." >&2
fi

# PATH must include uv + memex. Launchd sets a minimal PATH.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# 1. Incremental rebuild. Exits 0 even if embeddings failed — the gap is
#    surfaced in the warning block; we catch it in step 2.
if ! memex index rebuild --incremental; then
  echo "ERROR: memex index rebuild --incremental failed" >&2
  exit 1
fi

# 2. Retry any chunks/observations missing from vec_* tables. Idempotent.
#    Exits non-zero if any remain unembedded (persistent API failure).
if ! memex index embed-missing; then
  echo "WARN: embed-missing reported persistent gaps — run manually when fixed" >&2
  exit 2
fi

echo "===== nightly-rebuild done @ $(date -Iseconds) ====="
