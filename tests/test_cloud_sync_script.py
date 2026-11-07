"""scripts/cloud-sync.sh records each outcome for SessionStart's cloud-sync notice."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cloud-sync.sh"

FAKE_MEMEX = """#!/bin/bash
case "$1 $2" in
  "cloud pull") exit "${FAKE_PULL_RC:-0}" ;;
  "index rebuild") exit "${FAKE_REBUILD_RC:-0}" ;;
  "cloud push") exit "${FAKE_PUSH_RC:-0}" ;;
  "path "*|"path") echo "$FAKE_VAULT" ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".memex").mkdir()
    (tmp_path / "vault" / "projects").mkdir(parents=True)
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "memex"
    fake.write_text(FAKE_MEMEX)
    fake.chmod(0o755)
    return tmp_path


def _run(home, **env):
    full = {"HOME": str(home), "FAKE_VAULT": str(home / "vault"), "TMPDIR": str(home), **env}
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True).returncode


def test_failed_runs_append_and_a_completed_run_clears(home):
    (home / ".memex" / "cloud.json").write_text("{}")
    failed, ok = home / ".memex" / "cloud-sync.failed", home / ".memex" / "cloud-sync.ok"

    assert _run(home, FAKE_PULL_RC="1") == 1
    assert _run(home, FAKE_PULL_RC="1") == 1
    lines = failed.read_text().splitlines()
    assert len(lines) == 2 and all(line.endswith("exit=1") for line in lines)
    assert not ok.exists()

    assert _run(home) == 0
    assert not failed.exists() and ok.exists()


def test_not_configured_clears_a_stale_failure_record(home):
    failed = home / ".memex" / "cloud-sync.failed"
    failed.write_text("2026-11-07T09:00:00+01:00 exit=1\n2026-11-07T10:00:00+01:00 exit=1\n")

    assert _run(home) == 0
    assert not failed.exists()
    assert not (home / ".memex" / "cloud-sync.ok").exists(), "no cloud, no completed run"


@pytest.mark.parametrize("step, code", [("FAKE_REBUILD_RC", 2), ("FAKE_PUSH_RC", 3)])
def test_rebuild_and_push_failures_record_their_exit_code(home, step, code):
    (home / ".memex" / "cloud.json").write_text("{}")
    (home / ".memex" / "cloud-sync.ok").touch()

    assert _run(home, **{step: "1"}) == code
    assert (home / ".memex" / "cloud-sync.failed").read_text().strip().endswith(f"exit={code}")
