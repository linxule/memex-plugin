"""PostCompact hook (v0.22.0 I3): keep the compaction summary, nothing else.

The hook runs as a subprocess against a throwaway HOME (``hook_env``), the way
Claude Code runs it. PostCompact cannot inject context or show a message, so
the contract is: a file in the state dir (never the vault), silence on stdout,
and exit 0 on every input.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from hook_helpers import HOOKS_DIR

SID = "11111111-2222-4333-8444-555555555555"
REPO = HOOKS_DIR.parent


def payload(**overrides) -> dict:
    base = {
        "session_id": SID,
        "transcript_path": "/nonexistent/transcript.jsonl",
        "cwd": "/synthetic/project",
        "hook_event_name": "PostCompact",
        "trigger": "auto",
        "compact_summary": "Synthetic summary one: the fixture was set up.",
    }
    base.update(overrides)
    return base


def summaries_dir(env, sid: str = SID) -> Path:
    return env.state_dir / "compact-summaries" / sid


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", text, re.DOTALL)
    assert match, f"no frontmatter block in {text[:120]!r}"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(": ")
        fields[key] = json.loads(value)
    return fields, match.group(2)


def test_writes_summary_with_frontmatter(hook_env):
    result = hook_env.run("post-compact", payload(trigger="manual"))

    assert result.returncode == 0
    assert result.stdout == "", "PostCompact output is discarded by Claude Code; the hook must print nothing"
    files = sorted(summaries_dir(hook_env).iterdir())
    assert [f.name for f in files] == ["1.md"]
    fields, body = split_frontmatter(files[0].read_text())
    assert fields["session_id"] == SID
    assert fields["trigger"] == "manual"
    assert fields["cwd"] == "/synthetic/project"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", fields["timestamp"])
    assert body == "Synthetic summary one: the fixture was set up.\n"
    # nothing but the finished summary: no staging file left behind, content readable by the owner only
    assert sorted(p.name for p in summaries_dir(hook_env).glob("*")) == ["1.md"]
    assert sorted(p.name for p in summaries_dir(hook_env).glob(".*")) == []
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


def test_each_compaction_gets_the_next_number_and_keeps_the_earlier_one(hook_env):
    hook_env.run("post-compact", payload(compact_summary="first compaction digest"))
    hook_env.run("post-compact", payload(compact_summary="second compaction digest", trigger="manual"))

    files = {f.name: f.read_text() for f in summaries_dir(hook_env).iterdir()}
    assert sorted(files) == ["1.md", "2.md"]
    assert "first compaction digest" in files["1.md"] and "second" not in files["1.md"]
    assert "second compaction digest" in files["2.md"]
    assert split_frontmatter(files["2.md"])[0]["trigger"] == "manual"


def test_numbering_continues_after_the_highest_and_ignores_other_files(hook_env):
    d = summaries_dir(hook_env)
    d.mkdir(parents=True)
    (d / "5.md").write_text("older")
    (d / "notes.md").write_text("not a summary")
    (d / "9.txt").write_text("not a summary")

    hook_env.run("post-compact", payload())

    assert sorted(f.name for f in d.iterdir()) == ["5.md", "6.md", "9.txt", "notes.md"]
    assert (d / "5.md").read_text() == "older"
    assert "Synthetic summary one" in (d / "6.md").read_text()


def test_sessions_are_kept_apart(hook_env):
    other = "99999999-8888-4777-8666-555555555555"
    hook_env.run("post-compact", payload(compact_summary="belongs to the first session"))
    hook_env.run("post-compact", payload(session_id=other, compact_summary="belongs to the other session"))

    assert "first session" in (summaries_dir(hook_env) / "1.md").read_text()
    assert "other session" in (summaries_dir(hook_env, other) / "1.md").read_text()


def test_concurrent_compactions_never_overwrite_each_other(hook_env):
    texts = [f"digest number {i}" for i in range(6)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda t: hook_env.run("post-compact", payload(compact_summary=t)), texts))

    assert all(r.returncode == 0 for r in results)
    bodies = sorted(split_frontmatter(f.read_text())[1].strip() for f in summaries_dir(hook_env).iterdir())
    assert bodies == sorted(texts)


def test_helper_seats_leave_nothing(hook_env):
    result = hook_env.run("post-compact", payload(), env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})

    assert result.returncode == 0
    assert not (hook_env.state_dir / "compact-summaries").exists()
    # the same payload from an interactive session is kept: the env var alone made the difference
    hook_env.run("post-compact", payload(), env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert (summaries_dir(hook_env) / "1.md").is_file()


@pytest.mark.parametrize(
    "summary",
    [None, "", "   \n\t ", 42, ["a list"], {"text": "an object"}],
    ids=["null", "empty", "whitespace", "number", "list", "object"],
)
def test_empty_or_invalid_summary_writes_nothing(hook_env, summary):
    result = hook_env.run("post-compact", payload(compact_summary=summary))

    assert result.returncode == 0
    assert not (hook_env.state_dir / "compact-summaries").exists()


def test_missing_summary_key_writes_nothing(hook_env):
    data = payload()
    del data["compact_summary"]

    result = hook_env.run("post-compact", data)

    assert result.returncode == 0
    assert not (hook_env.state_dir / "compact-summaries").exists()


@pytest.mark.parametrize("raw", ["", "not json at all", "{", "[]", "null", '"a string"', "42"])
def test_malformed_stdin_exits_zero_and_writes_nothing(hook_env, raw):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "post-compact.py")],
        input=raw,
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if not k.startswith(("MEMEX_", "CLAUDE_"))},
             "HOME": str(hook_env.home)},
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (hook_env.state_dir / "compact-summaries").exists()


@pytest.mark.parametrize(
    "sid",
    ["", "../escape", "a/b", "..", ".hidden", "x" * 200, "has space", "nul\x00byte"],
    ids=["empty", "traversal", "slash", "dotdot", "dotfile", "too-long", "space", "nul"],
)
def test_unsafe_session_id_is_refused(hook_env, sid):
    result = hook_env.run("post-compact", payload(session_id=sid))

    assert result.returncode == 0
    assert not (hook_env.state_dir / "compact-summaries").exists()
    # nothing escaped above the state dir either
    assert sorted(p.name for p in hook_env.home.iterdir()) == [".memex"]
    assert not any(p.name == "escape" for p in hook_env.state_dir.rglob("*"))


def test_missing_session_id_writes_nothing(hook_env):
    data = payload()
    del data["session_id"]

    result = hook_env.run("post-compact", data)

    assert result.returncode == 0
    assert not (hook_env.state_dir / "compact-summaries").exists()


def test_frontmatter_round_trips_through_yaml_for_awkward_cwds(hook_env):
    awkward = "/w\u00f6rk/\u9879\u76ee/a \"b\" c: d\u2028e\u0085f\u2029g\\h"
    hook_env.run("post-compact", payload(cwd=awkward, trigger="manual"))

    text = (summaries_dir(hook_env) / "1.md").read_text()
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["cwd"] == awkward
    assert front["session_id"] == SID and front["trigger"] == "manual"
    assert len(text.split("---\n")[1].splitlines()) == 4, "a line-break character must not split the scalar"


def test_a_large_summary_is_kept_whole(hook_env):
    big = "\n".join(f"line {i}: synthetic filler for the pipe buffer" for i in range(8000))

    result = hook_env.run("post-compact", payload(compact_summary=big))

    assert result.returncode == 0
    assert split_frontmatter((summaries_dir(hook_env) / "1.md").read_text())[1] == big + "\n"


def _load_hook():
    spec = importlib.util.spec_from_file_location("post_compact_hook", HOOKS_DIR / "post-compact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_never_replaces_an_existing_summary_even_with_a_stale_listing(tmp_path, monkeypatch):
    """Two hooks that both counted `highest = 0` before either linked: the loser moves to 2."""
    hook = _load_hook()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first, second = tmp_path / "a.tmp", tmp_path / "b.tmp"
    first.write_text("first")
    second.write_text("second")

    assert hook._publish(first, session_dir) == session_dir / "1.md"
    with monkeypatch.context() as m:
        m.setattr(Path, "iterdir", lambda self: iter(()))  # the racer's directory scan predates the first link
        assert hook._publish(second, session_dir) == session_dir / "2.md"

    assert (session_dir / "1.md").read_text() == "first"
    assert (session_dir / "2.md").read_text() == "second"


def test_secrets_in_the_summary_are_redacted_before_they_hit_disk(hook_env):
    token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
    hook_env.run("post-compact", payload(compact_summary=f"We pasted the token {token} into the shell history."))

    text = (summaries_dir(hook_env) / "1.md").read_text()
    assert token not in text
    assert "<REDACTED:github-token>" in text
    assert "We pasted the token" in text, "only the secret goes, not the prose around it"


def test_summary_goes_to_the_configured_state_dir_and_never_the_vault(hook_env, tmp_path):
    custom_state = tmp_path / "elsewhere" / "state"
    hook_env.write_config(state_dir=str(custom_state))

    hook_env.run("post-compact", payload())

    assert (custom_state / "compact-summaries" / SID / "1.md").is_file()
    assert not (hook_env.state_dir / "compact-summaries").exists()
    assert not list(hook_env.vault.rglob("*compact*")), "summaries must never be written into the vault"
    assert list(hook_env.vault.rglob("*.md")) == []


def test_unwritable_state_dir_still_exits_zero(hook_env, tmp_path):
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("not a directory")
    hook_env.write_config(state_dir=str(blocker))

    result = hook_env.run("post-compact", payload())

    assert result.returncode == 0
    assert "Traceback" not in result.stderr


def test_summary_survives_the_real_interpreter(hook_env):
    """Under `uv run --script` only the inline dependencies exist. The hook swallows runtime
    errors (it must never fail a session), so asserting the file exists is what catches a
    missing inline dependency that only bites inside main()."""
    result = hook_env.run("post-compact", payload(), real=True, timeout=120)

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout == ""
    assert "Synthetic summary one" in (summaries_dir(hook_env) / "1.md").read_text()


def test_hooks_json_registers_post_compact_like_the_others():
    hooks = json.loads((HOOKS_DIR / "hooks.json").read_text())["hooks"]

    entry = hooks["PostCompact"]
    assert len(entry) == 1 and entry[0]["matcher"] == "*"
    (command,) = entry[0]["hooks"]
    assert command == {
        "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/post-compact.py",
        "timeout": 5,
    }
    assert (HOOKS_DIR / "post-compact.py").stat().st_mode & stat.S_IXUSR, "hook script must be executable"
    # every other event is still registered
    assert {"SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact", "PostToolUse"} <= set(hooks)


def test_post_compact_declares_the_dependencies_its_imports_need():
    header = (HOOKS_DIR / "post-compact.py").read_text().split("# ///")[1]
    # utils imports filelock at module level; scrub and paths are stdlib-only
    assert "filelock" in header
    assert "pyyaml" not in header.lower(), "the hook must not pull in session_memos (YAML) for a file write"


RECOVERY = REPO / "skills" / "memo-writing" / "recovery.md"


def test_recovery_recipe_is_one_self_contained_file_with_the_contract_the_hooks_point_at():
    text = RECOVERY.read_text()

    # inputs the spawning message fills in
    for placeholder in ("<TRANSCRIPT_PATH>", "<SUMMARIES_DIR>", "<PROJECT>", "<SESSION_ID>", "<VAULT_PATH>"):
        assert placeholder in text
    # summaries first, transcript second
    assert text.index("compaction summaries first") < text.index("transcript second")
    # the pieces of the memo contract
    assert "compact-summaries" in text
    assert "memo-default.md" in text
    assert "session_id: <SESSION_ID>" in text
    assert 'memex scrub "<VAULT_PATH>/projects/<PROJECT>/memos/<filename>.md" --apply' in text
    assert 'memex backfill obs --stdin --replace --doc-path "projects/<PROJECT>/memos/<filename>.md"' in text
    assert "ONE call" in text and "5-15" in text
    for secret_pattern in ("sk-*", "ghp_*", "AIza*", "AKIA*", "xox*", "PEM"):
        assert secret_pattern in text
    # Agent tool, sonnet; the retired Task(...) form must not come back
    assert "Agent(" in text and 'model: "sonnet"' in text
    assert "Task(" not in text


def test_recovery_recipe_is_in_the_portable_skill_copy():
    portable = REPO / "plugins" / "memex" / "skills" / "memo-writing" / "recovery.md"
    assert portable.read_bytes() == RECOVERY.read_bytes()
