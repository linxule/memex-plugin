"""The CLI accepts every flag the docs promise (v0.22.0 plan item A3).

Three layers:

1. ``session``/``backfill`` wrappers expose, in ``--help``, every flag the script
   behind them defines (they used to hide ``--import``, ``--exclude``,
   ``--apply``, ``-v``, ``--all-projects`` behind a pass-through).
2. Those typed options are forwarded to the script.
3. Every ``memex <subcommand> ... --flag`` written in ``.claude/rules/*.md``,
   ``CLAUDE.md`` and ``README.md`` (when present) is accepted by the CLI. A stale
   flag fails with the doc ``file:line``.

"Accepted" means: listed by that subcommand's ``--help``, or the subcommand is a
pass-through wrapper (``ignore_unknown_options``) whose delegate script defines
the flag (works, but is hidden from ``--help``). Anything else is stale.
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from memex import cli

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "memex"
DOC_FILES = sorted((ROOT / ".claude" / "rules").glob("*.md")) + [ROOT / "CLAUDE.md", ROOT / "README.md"]

runner = CliRunner()


# ── command tree ────────────────────────────────────────────────────

def _root():
    return typer.main.get_command(cli.app)


def _is_group(cmd) -> bool:
    return hasattr(cmd, "commands")


def _option_names(cmd) -> set[str]:
    names: set[str] = set()
    for param in cmd.params:
        if hasattr(param, "opts"):  # an option, not an argument
            names.update(param.opts)
            names.update(param.secondary_opts)
    return names


def _passes_through(cmd) -> bool:
    return bool(getattr(cmd, "context_settings", {}).get("ignore_unknown_options"))


def _resolve(tokens: list[str]):
    """Walk ``tokens`` down the command tree: ``(command, path, index of first unused token)``."""
    cmd, path, i = _root(), [], 0
    while i < len(tokens) and _is_group(cmd):
        token = tokens[i]
        if token.startswith("-"):
            break
        sub = cmd.commands.get(token)
        if sub is None:
            break
        cmd, i = sub, i + 1
        path.append(token)
    return cmd, tuple(path), i


# ── delegate scripts, read from cli.py without running it ───────────

def _delegate_map() -> dict[tuple[str, ...], list[str]]:
    """``("session", "discover") -> ["discover_sessions.py"]`` from cli.py's AST."""
    tree = ast.parse((SRC / "cli.py").read_text())
    groups: dict[str, str] = {"app": ""}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_typer"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    groups[node.args[0].id] = kw.value.value
    mapping: dict[tuple[str, ...], list[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for deco in fn.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "command"
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id in groups
            ):
                continue
            name = fn.name.replace("_", "-")
            for kw in deco.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
            group = groups[deco.func.value.id]
            key = (group, name) if group else (name,)
            scripts = [
                call.args[0].value
                for call in ast.walk(fn)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_delegate"
                and call.args
                and isinstance(call.args[0], ast.Constant)
            ]
            if scripts:
                mapping[key] = scripts
    return mapping


def _argparse_flags(path: Path, seen: set[Path] | None = None) -> set[str]:
    """Flags a module (and a ``from memex.x import main`` it forwards to) registers."""
    seen = seen if seen is not None else set()
    if path in seen or not path.exists():
        return set()
    seen.add(path)
    tree = ast.parse(path.read_text())
    flags: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-"):
                    flags.add(arg.value)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("memex.") and any(
            alias.name == "main" for alias in node.names
        ):
            target = SRC / (node.module.removeprefix("memex.").replace(".", "/") + ".py")
            flags |= _argparse_flags(target, seen)
    return flags - {"-h", "--help"}


def _script_flags(path: tuple[str, ...]) -> set[str]:
    flags: set[str] = set()
    for script in _delegate_map().get(path, []):
        flags |= _argparse_flags(SRC / "scripts" / script)
    return flags


# ── doc scanner ─────────────────────────────────────────────────────

_MEMEX = re.compile(r"""(?:^|[\s(|;&"'`])(?:\.?/?bin/)?memex(?=\s|$|[)"'`])""")
_SEPARATORS = {"|", "||", "&&", ";", "&", ">", ">>", "<"}
_LONG = re.compile(r"^(--[A-Za-z][\w-]*)")
_COMMENT_LONG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


@dataclass(frozen=True)
class Mention:
    file: str
    line: int
    path: tuple[str, ...]
    flag: str
    text: str

    def where(self) -> str:
        return f"{self.file}:{self.line}"


def _segments(text: str):
    """``(line number, segment)`` for each fenced-code line and inline code span."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            yield number, line
        else:
            for span in re.finditer(r"`([^`\n]+)`", line):
                yield number, span.group(1)


def _tokens(fragment: str) -> list[str]:
    try:
        return shlex.split(fragment)
    except ValueError:  # prose apostrophes inside a fenced block
        return fragment.split()


def _flags_in(tokens: list[str]) -> list[str]:
    found = []
    for token in tokens:
        if match := _LONG.match(token):
            found.append(match.group(1))
        elif re.fullmatch(r"-[A-Za-z]", token):
            found.append(token)
    return found


def find_flag_mentions(text: str, file: str) -> list[Mention]:
    """Every flag written after a ``memex`` command, with its resolved subcommand path.

    Flags in a trailing ``# comment`` belong to the command they annotate, and a
    code span made only of flags continues the previous ``memex`` span on the
    same line ("... with `--min-score=9 --import --apply`").
    """
    mentions: list[Mention] = []
    last_line, last_path = 0, None
    for number, segment in _segments(text):
        if number != last_line:
            last_line, last_path = number, None
        found_chain = False
        for match in _MEMEX.finditer(segment):
            rest = segment[match.end():]
            command_part, _, comment = rest.partition(" #")
            tokens: list[str] = []
            for token in _tokens(command_part):
                if token in _SEPARATORS or token.startswith(("<<", ">")):
                    break
                tokens.append(token)
            cmd, path, used = _resolve(tokens)
            if _is_group(cmd) and used < len(tokens) and tokens[used].startswith(("<", "[")):
                continue  # `memex <command> ...` placeholder, not a real invocation
            flags = _flags_in(tokens[used:]) + _COMMENT_LONG.findall(comment)
            found_chain = True
            last_path = path
            mentions += [Mention(file, number, path, flag, segment.strip()) for flag in flags]
        if not found_chain and last_path is not None:
            tokens = _tokens(segment)
            if tokens and all(t.startswith("-") for t in tokens):
                mentions += [
                    Mention(file, number, last_path, flag, segment.strip()) for flag in _flags_in(tokens)
                ]
    return mentions


def _classify(mention: Mention) -> str:
    """``listed`` (in --help), ``hidden`` (pass-through to a script that defines it) or ``stale``."""
    if mention.flag in ("--help", "-h"):
        return "listed"
    cmd, path, _ = _resolve(list(mention.path))
    if mention.flag in _option_names(cmd):
        return "listed"
    if _passes_through(cmd) and mention.flag in _script_flags(path):
        return "hidden"  # works through the pass-through, just not listed by --help
    return "stale"


def stale_mentions(mentions: list[Mention]) -> list[str]:
    """One message per mention the CLI would not accept."""
    return [
        f"{m.where()}: `memex {' '.join(m.path)}` does not accept {m.flag}  (written as: {m.text})"
        for m in mentions
        if _classify(m) == "stale"
    ]


def hidden_flags(mentions: list[Mention]) -> set[tuple[str, ...]]:
    """``(*subcommand path, flag)`` for documented flags that only work through a pass-through."""
    return {(*m.path, m.flag) for m in mentions if _classify(m) == "hidden"}


def _doc_mentions() -> list[Mention]:
    mentions: list[Mention] = []
    scanned = 0
    for doc in DOC_FILES:
        if not doc.exists():  # README.md lives in the public plugin repo only
            continue
        scanned += 1
        mentions += find_flag_mentions(doc.read_text(), str(doc.relative_to(ROOT)))
    assert scanned >= 2 and len(mentions) > 20, "doc scanner found nothing: the test would be vacuous"
    return mentions


# ── 3. the docs ─────────────────────────────────────────────────────

def test_every_documented_flag_is_accepted_by_the_cli():
    problems = stale_mentions(_doc_mentions())
    assert not problems, "stale flags in the docs (fix the doc or the CLI):\n  " + "\n  ".join(problems)


# Documented flags that work only through a pass-through wrapper (the script
# defines them, `--help` does not list them). All sit in commands outside the
# cli-discover unit's cli.py hunk: expose each with a typer option, then delete
# its entry here. The test fails both ways, so the list can only shrink.
KNOWN_HIDDEN = {
    ("index", "rebuild", "--incremental"),
    ("sync", "--discover"),
    ("sync", "--sync"),
    ("sync", "--status"),
    ("sync", "--apply"),
}


def test_documented_flags_hidden_from_help_are_a_known_shrinking_list():
    hidden = hidden_flags(_doc_mentions())
    new = hidden - KNOWN_HIDDEN
    fixed = KNOWN_HIDDEN - hidden
    assert not new, (
        f"documented but missing from `--help`: {sorted(new)}. Add a typer option that forwards it "
        "(or, for a command outside your unit, add it to KNOWN_HIDDEN)."
    )
    assert not fixed, f"now listed by --help (or no longer documented); remove from KNOWN_HIDDEN: {sorted(fixed)}"


def test_scanner_reports_a_stale_flag_with_file_and_line():
    doc = (
        "Intro line.\n"
        "```bash\n"
        "memex session discover --triage --min-score=9     # fine\n"
        "memex session discover --frobnicate               # stale\n"
        "memex session reconcile-orphans --apply --trust-window\n"
        "memex sync --status                               # hidden but accepted: pass-through\n"
        "```\n"
        "Prose: `memex backfill memos --apply -v` and `memex session import --no-such-flag`.\n"
    )
    mentions = find_flag_mentions(doc, "docs/fake.md")
    problems = stale_mentions(mentions)
    assert len(problems) == 2, problems
    assert problems[0].startswith("docs/fake.md:4: ") and "--frobnicate" in problems[0]
    assert problems[1].startswith("docs/fake.md:8: ") and "--no-such-flag" in problems[1]
    assert "`memex session discover`" in problems[0]
    assert "`memex session import`" in problems[1]
    # the valid mentions were really scanned, not skipped
    assert {m.flag for m in mentions} >= {"--triage", "--min-score", "--trust-window", "-v", "--status"}


def test_scanner_follows_trailing_comments_and_flag_only_spans():
    doc = (
        "```\n"
        "memex session import   # skip a session (--apply to run, --bogus-comment-flag)\n"
        "```\n"
        "Import with `memex session discover --triage`, then `--import --exclude` and `--apply`.\n"
    )
    mentions = find_flag_mentions(doc, "docs/fake.md")
    by_line = {(m.line, m.flag): m.path for m in mentions}
    assert by_line[(2, "--apply")] == ("session", "import")
    assert by_line[(2, "--bogus-comment-flag")] == ("session", "import")
    assert by_line[(4, "--import")] == ("session", "discover")
    assert by_line[(4, "--apply")] == ("session", "discover")
    stale = stale_mentions(mentions)
    assert len(stale) == 1 and "--bogus-comment-flag" in stale[0]


def test_scanner_ignores_placeholders_paths_and_other_tools():
    doc = (
        "Use `memex <command> --help`, `uv run pytest --maxfail=1`, `~/.memex/config.json`,\n"
        "`projects/memex/_project.md`, `memex.paths.get_index_path` and `cat x | memex read --nope`.\n"
    )
    flags = {(m.line, m.flag) for m in find_flag_mentions(doc, "docs/fake.md")}
    assert flags == {(2, "--nope")}


# ── 1. wrappers expose what the scripts define ──────────────────────

# `session import` is `discover --import`: the flag is implied, and
# --all-projects is a listing mode that never imports.
EXEMPT = {("session", "import"): {"--import", "--all-projects"}}

TYPED_COMMANDS = [
    ("session", "discover"),
    ("session", "import"),
    ("session", "reconcile-orphans"),
    ("backfill", "tokens"),
    ("backfill", "memos"),
    ("backfill", "obs"),
    ("backfill", "topic-tags"),
]


@pytest.mark.parametrize("path", TYPED_COMMANDS, ids=lambda p: " ".join(p))
def test_wrapper_lists_every_flag_its_script_defines(path):
    cmd, resolved, _ = _resolve(list(path))
    assert resolved == path
    script_flags = _script_flags(path)
    assert script_flags, f"no argparse flags found behind `memex {' '.join(path)}`: mapping broke"
    missing = script_flags - _option_names(cmd) - EXEMPT.get(path, set())
    assert not missing, (
        f"`memex {' '.join(path)} --help` hides {sorted(missing)}: the script defines them. "
        "Add a typer option that forwards it (src/memex/cli.py)."
    )


# Help is rendered by rich: on a narrow CI terminal it wraps and colours option
# names (public CI, 80 columns, failed while a wide local terminal passed).
# Render wide and plain, and strip any escape codes before matching.
_PLAIN_HELP_ENV = {"COLUMNS": "250", "NO_COLOR": "1", "TERM": "dumb", "_TYPER_FORCE_DISABLE_TERMINAL": "1"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _help(*args: str) -> str:
    result = runner.invoke(cli.app, [*args, "--help"], env=_PLAIN_HELP_ENV)
    assert result.exit_code == 0, result.output
    return _ANSI.sub("", result.output)


def test_help_shows_the_flags_the_docs_promise():
    discover = _help("session", "discover")
    for flag in ("--import", "--exclude", "--apply", "--verbose", "--all-projects", "--min-score", "--triage"):
        assert flag in discover, f"{flag} missing from `session discover --help`"
    memos = _help("backfill", "memos")
    assert "--apply" in memos and "--verbose" in memos


# ── 2. the typed options reach the script ───────────────────────────

@pytest.fixture
def delegated(monkeypatch):
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "_delegate", lambda script, args: calls.append((script, list(args))))
    return calls


def test_discover_forwards_every_typed_option(delegated):
    result = runner.invoke(cli.app, [
        "session", "discover", "--triage", "--min-score=9", "-v", "--project", "arena",
        "--since", "7d", "--json", "--import", "--apply", "--exclude", "1a2b3c4d",
        "--exclude", "9f8e7d6c", "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    (script, args), = delegated
    assert script == "discover_sessions.py"
    assert args == [
        "--import", "--apply", "--dry-run", "--triage", "--min-score", "9", "--verbose",
        "--project", "arena", "--since", "7d", "--json",
        "--exclude", "1a2b3c4d", "--exclude", "9f8e7d6c",
    ]


def test_discover_all_projects_and_unknown_flags_still_pass_through(delegated):
    result = runner.invoke(cli.app, ["session", "discover", "--all-projects", "--future-flag", "x"])
    assert result.exit_code == 0, result.output
    assert delegated == [("discover_sessions.py", ["--all-projects", "--future-flag", "x"])]


def test_import_implies_import_and_keeps_exclude_repeatable(delegated):
    result = runner.invoke(cli.app, [
        "session", "import", "--apply", "--exclude", "aaaa1111", "--exclude", "bbbb2222",
        "--min-score", "4",
    ])
    assert result.exit_code == 0, result.output
    (script, args), = delegated
    assert script == "discover_sessions.py"
    assert args[0] == "--import"
    assert "--apply" in args and args.count("--exclude") == 2
    assert args[args.index("--min-score") + 1] == "4"
    # no --apply: a dry run must not become an import
    delegated.clear()
    runner.invoke(cli.app, ["session", "import"])
    assert delegated == [("discover_sessions.py", ["--import"])]


def test_backfill_flags_forward(delegated):
    for command, script in (
        ("tokens", "backfill_tokens.py"),
        ("memos", "backfill_has_memo.py"),
        ("topic-tags", "backfill_topic_tags.py"),
    ):
        delegated.clear()
        result = runner.invoke(cli.app, ["backfill", command, "--apply", "-v"])
        assert result.exit_code == 0, (command, result.output)
        assert delegated == [(script, ["--apply", "--verbose"])]
    delegated.clear()
    runner.invoke(cli.app, ["backfill", "memos"])
    assert delegated == [("backfill_has_memo.py", [])]  # dry-run stays the default


def test_backfill_obs_forwards_its_input_and_mode_flags(delegated):
    result = runner.invoke(cli.app, [
        "backfill", "obs", "--stdin", "--doc-path", "projects/p/memos/a.md", "--replace", "--no-embed",
    ])
    assert result.exit_code == 0, result.output
    (script, args), = delegated
    assert script == "extract_observations.py"
    assert args == ["--doc-path", "projects/p/memos/a.md", "--stdin", "--replace", "--no-embed"]


def test_reconcile_orphans_forwards_window_and_json(delegated):
    result = runner.invoke(cli.app, [
        "session", "reconcile-orphans", "--apply", "--trust-window", "--window", "5", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert delegated == [("reconcile_orphans.py", ["--apply", "--trust-window", "--window", "5", "--json"])]
