#!/usr/bin/env bash
# memex cloud sync: pull memos saved by remote agents, scrub, rebuild, push.
#
# Run it hourly on ONE machine that has done `memex cloud login` (~/.memex is
# not synced, so that machine is the one of record; two pushers would make the
# mirror flip between them). Needs only `memex` on PATH. Copy it somewhere
# stable first, since a plugin cache path changes with every version:
#
#   cp scripts/cloud-sync.sh ~/.local/bin/memex-cloud-sync
#   mkdir -p ~/.memex/logs
#   crontab -e   # then add:
#   0 * * * * $HOME/.local/bin/memex-cloud-sync >> $HOME/.memex/logs/cloud-sync.log 2>> $HOME/.memex/logs/cloud-sync-error.log
#
# (or a launchd agent with StartInterval 3600 and the same two log paths).
#
# The push publishes the working tree by default, so claude.ai sees what local
# agents see. For an urgent redaction don't wait for this job:
# `memex cloud forget <path> --yes` deletes the doc from the cloud at once and
# keeps that version out. With `cloud.publish = "committed"` only what git HEAD
# holds goes out; a doc goes out only when its indexed text is HEAD's, so the
# rebuild below still matters.
#
# Exit codes: 0 ok or not configured · 1 pull failed · 2 rebuild failed · 3 push failed
#
# Every outcome is recorded where memex's SessionStart hook reads it: a failed
# run appends "<time> exit=<n>" to ~/.memex/cloud-sync.failed, a completed run
# deletes that file and touches ~/.memex/cloud-sync.ok, and the hook tells the
# next session after two failures in a row. cron and launchd discard the exit
# code; before this, 9 failed runs in a row went unnoticed (2026-11-06/07: iCloud
# had evicted a vault file, which a launchd job cannot download).
set -uo pipefail
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

echo "===== cloud-sync @ $(date -Iseconds) ====="
if [[ ! -f "${HOME}/.memex/cloud.json" ]]; then
  echo "no memex cloud configured on this machine (memex cloud login); skipping"
  rm -f "${HOME}/.memex/cloud-sync.failed"  # a logged-out machine has nothing to announce
  exit 0
fi

record_outcome() {
  local rc=$?
  if [[ ${rc} -eq 0 ]]; then
    rm -f "${HOME}/.memex/cloud-sync.failed"
    touch "${HOME}/.memex/cloud-sync.ok"
  else
    echo "$(date -Iseconds) exit=${rc}" >> "${HOME}/.memex/cloud-sync.failed"
  fi
}
trap record_outcome EXIT

memex cloud pull || { echo "ERROR: memex cloud pull failed" >&2; exit 1; }

# Memos written outside Claude Code (Codex, Kimi Code, Obsidian, git pulls) skip
# memex's write-time scrub hook. Redact any memo or auto-memory file that
# changed since the last sync, in place, before it is indexed, pushed or committed.
# (`memex cloud push` also scrubs everything it sends.) Sidecars (.obs.jsonl) are not
# rewritten here: changing an observation's text changes its content hash, so ingest
# would replace the observation and its citations would go pending. The push scrubs
# observation text on the way out, and memos are scrubbed before extraction.
vault="$(memex path)" || { echo "ERROR: memex path failed" >&2; exit 1; }
marker="${HOME}/.memex/cloud-sync.scrubbed"
touch "${marker}.next"
if [[ -f "${marker}" ]]; then newer=(-newer "${marker}"); else newer=(-mmin -1440); fi
scrubbed=0
while IFS= read -r -d '' f; do
  if memex scrub "${f}" --apply >/dev/null 2>&1; then scrubbed=$((scrubbed + 1)); else echo "WARN: scrub failed on ${f}" >&2; fi
done < <(find "${vault}/projects" -type f \( -path '*/memos/*.md' -o -path '*/auto-memory/*.md' \) "${newer[@]}" -print0)
mv -f "${marker}.next" "${marker}"
echo "scrub: ${scrubbed} changed file(s) checked"

rebuild_log="$(mktemp "${TMPDIR:-/tmp}/memex-cloud-sync.XXXXXX")"
if ! memex index rebuild --incremental >"${rebuild_log}" 2>&1; then
  cat "${rebuild_log}" >&2; rm -f "${rebuild_log}"
  echo "ERROR: memex index rebuild --incremental failed" >&2
  exit 2
fi
grep -E '^(New|Updated|Deleted): ' "${rebuild_log}" | paste -sd' ' -
rm -f "${rebuild_log}"

memex cloud push || { echo "ERROR: memex cloud push failed" >&2; exit 3; }
echo "===== cloud-sync done @ $(date -Iseconds) ====="
