"""Tests for the CRUCIBLE controller.

The unit tests cover the pure logic. The end-to-end tests run real loops —
real subprocesses, real git worktrees, real pytest and ruff gates — against
the fake `claude` in tests/fake_claude.py, so the tournament's decisions are
asserted rather than assumed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crucible  # noqa: E402  # isort: skip  (needs the sys.path line above)


# ────────────────────────────── unit tests ─────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("CLI expense tracker: add, list", "cli-expense-tracker-add"),
        ("# Idea\n\nA thing that does stuff", "idea"),
        ("   \n\n  Trailing and leading  ", "trailing-and-leading"),
        ("!!!", "run"),
        ("", "run"),
    ],
)
def test_slugify(text, expected):
    assert crucible.slugify(text) == expected


def test_slugify_is_filesystem_safe():
    slug = crucible.slugify("a/b\\c:d*e?f\"g<h>i|j")
    assert not set(slug) & set('/\\:*?"<>|')


def test_render_substitutes_placeholders(tmp_path):
    tpl = tmp_path / "t.md"
    tpl.write_text("Hello {NAME}, round {N}")
    assert crucible.render(tpl, {"NAME": "world", "N": "2"}) == "Hello world, round 2"


def test_render_leaves_unrelated_braces_alone(tmp_path):
    """Prompts contain JSON and shell. str.format would choke on these."""
    tpl = tmp_path / "t.md"
    tpl.write_text('{"schema": {"type": "object"}} and ${SHELL_VAR} and {IDEA}')
    out = crucible.render(tpl, {"IDEA": "x"})
    assert '{"schema": {"type": "object"}}' in out
    assert "${SHELL_VAR}" in out
    assert out.endswith("x")


def test_tail_returns_last_n_lines():
    assert crucible.tail("\n".join(str(i) for i in range(100)), 3) == "97\n98\n99"
    assert crucible.tail("one line", 5) == "one line"
    assert crucible.tail("", 5) == ""


def test_candidate_summary_reports_status():
    c = crucible.Candidate(
        cid=2, angle="Bias toward robustness: details", branch="b",
        path=Path("/tmp/x"), dir=Path("/tmp/y"),
    )  # fmt: skip
    c.dead, c.dead_reason = True, "builder error"
    assert c.summary()["status"] == "builder error"
    assert c.summary()["angle"] == "Bias toward robustness"

    c.dead = False
    c.gates_ok = True
    assert c.summary()["status"] == "green"
    c.gates_ok = False
    assert c.summary()["status"] == "red"


def test_first_candidate_reads_the_idea_straight():
    """Decorrelation is the point of best-of-N, but one attempt stays neutral."""
    assert crucible.CANDIDATE_ANGLES[0] == ""
    assert all(a for a in crucible.CANDIDATE_ANGLES[1:])


def test_angles_cover_the_default_candidate_count():
    assert len(crucible.CANDIDATE_ANGLES) >= crucible.CANDIDATES


# ─────────────────────────── end-to-end harness ────────────────────────────


def has_tools() -> bool:
    return all(shutil.which(t) for t in ("git", "ruff")) and not subprocess.run(
        [sys.executable, "-c", "import pytest_timeout"], capture_output=True
    ).returncode


requires_tools = pytest.mark.skipif(
    not has_tools(), reason="needs git, ruff and pytest-timeout on PATH"
)


@pytest.fixture
def fake_claude(tmp_path):
    """Put the fake claude on PATH as `claude`, ahead of any real install."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(
        f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_claude.py'} \"$@\"\n"
    )
    shim.chmod(0o755)
    return bindir


def run_crucible(fake_claude, tmp_path, scenario, idea, *extra):
    env = {
        **os.environ,
        "PATH": f"{fake_claude}{os.pathsep}{os.environ['PATH']}",
        "CRUCIBLE_FAKE_SCENARIO": scenario,
        "CRUCIBLE_RUNS_DIR": str(tmp_path / "runs"),
    }
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), idea, *extra],
        capture_output=True, text=True, env=env, timeout=600,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / "state.json").read_text())
    return state, run_dir, proc.stdout


def git_out(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


# ───────────────────────────── end-to-end tests ────────────────────────────


@requires_tools
def test_tournament_promotes_the_highest_scorer(fake_claude, tmp_path):
    """c1 fails gates, c2 scores 7, c3 scores 9 — trunk must become c3."""
    state, run_dir, _ = run_crucible(fake_claude, tmp_path, "tourney", "tournament test")
    ws = run_dir / "workspace"

    assert state["stop_reason"] == "pass"
    round1 = state["history"][0]
    assert round1["winner"] == 3
    assert round1["min_score"] == 9

    # The promoted commit is exactly the winner's, not merely equivalent to it.
    assert git_out(ws, "rev-parse", "trunk") == git_out(ws, "rev-parse", "cand-r1-3")
    assert "Candidate 3" in git_out(ws, "show", "trunk:README.md")

    # Losers survive for salvage.
    branches = git_out(ws, "branch", "--list", "cand-*")
    assert "cand-r1-1" in branches and "cand-r1-2" in branches


@requires_tools
def test_losing_candidates_are_not_merged_into_trunk(fake_claude, tmp_path):
    """A tournament that silently merged everyone would still look green."""
    _, run_dir, _ = run_crucible(fake_claude, tmp_path, "tourney", "isolation test")
    ws = run_dir / "workspace"
    # Trunk is a straight line: base -> winner. No merge commits, no loser parents.
    log = git_out(ws, "log", "--pretty=%P", "trunk")
    assert all(len(line.split()) <= 1 for line in log.splitlines())


@requires_tools
def test_gate_artifacts_written_per_candidate(fake_claude, tmp_path):
    _, run_dir, _ = run_crucible(fake_claude, tmp_path, "tourney", "artifact test")
    r1 = run_dir / "rounds" / "round_1"
    assert (r1 / "selection.json").is_file()
    for k in (1, 2, 3):
        assert (r1 / f"cand_{k}" / "builder.json").is_file()
        assert (r1 / f"cand_{k}" / "gates.txt").is_file()
    # c1 failed its gates, so it must never have been sent to the critic.
    assert not (r1 / "cand_1" / "critique.json").exists()
    assert (r1 / "cand_3" / "critique.json").is_file()


@requires_tools
def test_one_dead_builder_does_not_kill_the_round(fake_claude, tmp_path):
    """The core robustness win of N>1: v1 would have terminated here."""
    state, _, _ = run_crucible(fake_claude, tmp_path, "onedies", "survivor test")
    assert state["stop_reason"] == "pass"
    cands = state["history"][0]["candidates"]
    assert cands[0]["status"] == "builder error"
    assert state["history"][0]["winner"] in (2, 3)


@requires_tools
def test_all_builders_dead_terminates_cleanly(fake_claude, tmp_path):
    state, run_dir, _ = run_crucible(fake_claude, tmp_path, "alldie", "total failure test")
    assert state["stop_reason"] == "builder_error"
    assert state["best_round"] is None
    # Nothing was promoted, so nothing may be tagged — and the report must not
    # tell the reader to check out a ref that does not exist.
    assert git_out(run_dir / "workspace", "tag") == ""
    assert "git checkout best" not in (run_dir / "RUN_REPORT.md").read_text()


@requires_tools
def test_no_green_candidate_carries_least_broken_forward(fake_claude, tmp_path):
    state, run_dir, _ = run_crucible(fake_claude, tmp_path, "allred", "all red test")
    assert state["stop_reason"] == "max_rounds"
    assert state["round"] == crucible.MAX_ROUNDS
    assert state["best_round"] is None  # the critic never ran
    assert all(h["gates"] == "fail" for h in state["history"])
    # Every round still promoted something, so the run stays inspectable.
    assert git_out(run_dir / "workspace", "tag", "--list", "round-*")
    assert "No round ever passed the gates" in (run_dir / "RUN_REPORT.md").read_text()


@requires_tools
def test_best_is_not_necessarily_last(fake_claude, tmp_path):
    """slowpass scores 9 from round 3 on; best must pin the first 9, not the last."""
    state, run_dir, _ = run_crucible(fake_claude, tmp_path, "slowpass", "best not last")
    assert state["best_round"] == 3
    ws = run_dir / "workspace"
    assert git_out(ws, "rev-parse", "best") == git_out(ws, "rev-parse", "round-3")


@requires_tools
def test_single_candidate_runs_the_sequential_loop(fake_claude, tmp_path):
    state, _, out = run_crucible(fake_claude, tmp_path, "tourney", "solo test", "-n", "1")
    assert state["candidates_per_round"] == 1
    assert all(len(h["candidates"]) == 1 for h in state["history"])
    assert "forking 1 candidate" in out


@requires_tools
def test_state_json_tracks_cost_and_history(fake_claude, tmp_path):
    state, _, _ = run_crucible(fake_claude, tmp_path, "tourney", "state test")
    assert state["total_cost_usd"] == pytest.approx(0.95)  # 3 builders + 2 critics
    assert state["run_id"] and state["candidates_per_round"] == 3


# ──────────────────────────── argument handling ────────────────────────────


def test_rejects_zero_candidates():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "x", "-n", "0"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 1
    assert "at least 1" in proc.stderr


def test_reports_version():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--version"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert crucible.__version__ in proc.stdout


def test_empty_idea_is_rejected(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), str(empty)],
        capture_output=True, text=True, env={**os.environ, "CRUCIBLE_RUNS_DIR": str(tmp_path)},
    )  # fmt: skip
    assert proc.returncode == 1
    assert "empty" in proc.stderr
