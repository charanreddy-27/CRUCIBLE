"""Tests for the CRUCIBLE controller.

The unit tests cover the pure logic. The end-to-end tests run real loops —
real subprocesses, real git worktrees, real pytest and ruff gates — against
the fake `claude` in tests/fake_claude.py, so the tournament's decisions are
asserted rather than assumed.
"""

from __future__ import annotations

import io
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


def test_critic_schema_is_sent_inline_not_as_a_path():
    """The real CLI takes --json-schema inline; a path fails every critic call.

    This shipped as a live bug: the schema was passed as a filesystem path, the
    stub only checked the flag was present, and the tests were green while the
    critic could never have worked against the real CLI.
    """
    schema_text = crucible.SCHEMA.read_text(encoding="utf-8")
    parsed = json.loads(schema_text)  # must itself be valid JSON
    assert parsed["type"] == "object"
    assert set(parsed["required"]) == {"scores", "must_fix", "summary", "verdict"}

    src = (ROOT / "crucible.py").read_text(encoding="utf-8")
    start = src.index("def invoke_critic")
    invocation = src[start : src.index("# ─", start)]
    assert "SCHEMA.read_text" in invocation, "schema must be inlined, not passed as a path"
    assert "str(SCHEMA)" not in invocation, "passing the schema path breaks every critic call"


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
        # CI and containers often run as root, where the CLI forbids
        # --dangerously-skip-permissions. Take the scoped path instead.
        "CRUCIBLE_BUILDER_ALLOWED_TOOLS": "Edit,Write,Read,Glob,Grep,Bash(git *)",
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
def test_aborted_round_still_cleans_up_and_records(fake_claude, tmp_path):
    """A round where every candidate dies is the one you most want a record of.

    It is also the easiest path to leak worktrees on, because it returns early.
    """
    _, run_dir, _ = run_crucible(fake_claude, tmp_path, "alldie", "cleanup test")

    # No worktree directories left on disk...
    assert not list((run_dir / "candidates").rglob("cand_*/.git"))
    # ...and none still registered with git (only the main workspace remains).
    assert len(git_out(run_dir / "workspace", "worktree", "list").splitlines()) == 1

    # Forensics for the failed round exist despite the early return.
    sel = json.loads((run_dir / "rounds" / "round_1" / "selection.json").read_text())
    assert sel["winner"] is None
    assert len(sel["candidates"]) == 3
    assert all(c["status"] == "builder error" for c in sel["candidates"])


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


@requires_tools
def test_budget_and_round_caps_are_overridable(fake_claude, tmp_path):
    """The caps must be settable per run — trying a real idea cheaply is the
    single most common thing you want to do, and editing source for it is silly."""
    state, _, out = run_crucible(
        fake_claude, tmp_path, "allred", "cap override", "--max-rounds", "2", "--budget", "9"
    )
    assert state["stop_reason"] == "max_rounds"
    assert state["round"] == 2, "the --max-rounds override must beat the module default"
    assert "2 rounds" in out and "$9.00" in out


@pytest.mark.parametrize("flag,value", [("--max-rounds", "0"), ("--budget", "0"), ("-n", "0")])
def test_rejects_nonsense_caps(flag, value):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "x", flag, value],
        capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 1
    assert "at least 1" in proc.stderr or "greater than 0" in proc.stderr


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
        capture_output=True, text=True,
        env={**os.environ, "CRUCIBLE_RUNS_DIR": str(tmp_path),
             "CRUCIBLE_BUILDER_ALLOWED_TOOLS": "Read"},
    )  # fmt: skip
    assert proc.returncode == 1
    assert "empty" in proc.stderr


def test_root_without_a_whitelist_is_refused_up_front(tmp_path):
    """The CLI forbids --dangerously-skip-permissions as root.

    Without this guard every builder invocation dies on a non-JSON error, burns
    its retry, and reports "unparseable JSON envelope" — true, but useless.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("only meaningful when running as root")
    env = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_BUILDER_ALLOWED_TOOLS"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "x"],
        capture_output=True, text=True,
        env={**env, "CRUCIBLE_RUNS_DIR": str(tmp_path)},
    )  # fmt: skip
    assert proc.returncode == 1
    assert "root" in proc.stderr and "BUILDER_ALLOWED_TOOLS" in proc.stderr


# ───────────────────────────── html report ─────────────────────────────────

HOSTILE_IDEA = "<script>alert('pwned')</script> & a tracker"


@requires_tools
def test_html_report_shows_the_tournament_escaped(fake_claude, tmp_path):
    """Every candidate, the winner, the stop reason — and no raw user markup."""
    state, run_dir, out = run_crucible(fake_claude, tmp_path, "tourney", HOSTILE_IDEA)
    page = (run_dir / "RUN_REPORT.html").read_text()

    # The idea is user input; it must never reach the page as markup.
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt; &amp; a tracker" in page

    assert '<span class="badge good" title="stop reason">pass</span>' in page
    for k in (1, 2, 3):
        assert f'id="r1-c{k}"' in page
    # Exactly one winner, and it is the candidate state.json says won.
    assert page.count('data-winner="true"') == 1
    assert 'id="r1-c3" data-winner="true"' in page
    assert "gates red — not reviewed" in page  # c1 never reached the critic
    assert "fake critique of candidate 3, round 1" in page  # read from critique.json
    assert "app.py: candidate 2 needs input validation" in page  # c2's must_fix
    assert "RUN_REPORT.html" in out

    # What the page needs to re-render faithfully later is recorded, not recomputed.
    assert state["best_tag"] == 1 and "`best` points at round 1" in state["tag_note"]
    assert state["version"] == crucible.__version__
    assert state["max_rounds"] == crucible.MAX_ROUNDS and state["demo"] is False


@requires_tools
@pytest.mark.parametrize(
    "scenario,extra,why",
    [
        ("alldie", (), "builder error — never reached the gates, not reviewed"),
        ("allred", ("--max-rounds", "2"), "gates red — not reviewed"),
    ],
)
def test_html_report_survives_degenerate_runs(fake_claude, tmp_path, scenario, extra, why):
    """A run where nothing was scored is exactly when the report must explain itself."""
    _, run_dir, _ = run_crucible(fake_claude, tmp_path, scenario, "degenerate", *extra)
    page = (run_dir / "RUN_REPORT.html").read_text()
    assert why in page
    # The cause, not just the outcome: nothing reached the critic, so it never ran.
    if scenario == "allred":
        assert "none passed the gates, so none reached the critic" in page
        assert "critic failed" not in page
    assert "No candidate was ever scored" in page
    assert "no critic-scored round" in page
    assert page.count('data-winner="true"') == (0 if scenario == "alldie" else 2)


def _write_run(run_dir: Path, history: list, **extra) -> None:
    run_dir.mkdir(parents=True)
    state = {
        "run_id": run_dir.name, "round": len(history), "candidates_per_round": 1,
        "best_round": None, "best_overall": None, "total_cost_usd": 0.0,
        "history": history, "stop_reason": None, **extra,
    }  # fmt: skip
    (run_dir / "state.json").write_text(json.dumps(state))


def _cand(cid, status="green", cost=0.25, min_score=None, verdict=None):
    return {"id": cid, "angle": "straight read", "status": status, "gates_passed": "2/2",
            "min_score": min_score, "verdict": verdict, "cost_usd": cost, "commit": "abc12345"}


def test_html_report_says_an_interrupted_round_was_interrupted(tmp_path):
    """A round on disk that never reached history was cut short — the critic was
    never asked, so the page must not blame it, and its spend still counts."""
    run_dir = tmp_path / "cut-short"
    _write_run(run_dir, [], version=crucible.__version__, tag_note=None)
    sel = run_dir / "rounds" / "round_1" / "selection.json"
    sel.parent.mkdir(parents=True)
    sel.write_text(json.dumps({"round": 1, "base": "deadbeef", "winner": None,
                               "candidates": [_cand(1), _cand(2), _cand(3)]}))
    page = crucible.render_html_report(run_dir)
    assert "interrupted — the run stopped during this round" in page
    assert "the run stopped before its critique" in page
    assert "critic failed" not in page and "every candidate failed" not in page
    assert "the run was interrupted or is still going" in page
    assert "predates tag notes" not in page and "did not finish" in page
    assert "$0.75" in page  # the cut-short round's spend, not state.json's $0.00


def test_html_report_blames_the_critic_only_when_it_failed(tmp_path):
    run_dir = tmp_path / "critic-down"
    history = [{"round": 1, "base": "deadbeef", "winner": None, "gates": "critic_error",
                "min_score": None, "verdict": None, "cost_usd": 0.9,
                "candidates": [_cand(1), _cand(2)]}]
    _write_run(run_dir, history, stop_reason="critic_error", version=crucible.__version__)
    page = crucible.render_html_report(run_dir)
    assert "no winner — the critic failed on every green candidate" in page
    assert "only gates-green candidates reach the critic" not in page
    assert "gates green, but the critic failed twice" in page


def test_losing_branch_names_refs_that_exist():
    field = [_cand(1, "dead"), _cand(2), _cand(3, "red")]
    promoted = {"round": 1, "winner": 2, "candidates": field}
    died = {"round": 2, "winner": None, "gates": "builder_error", "candidates": [_cand(1, "dead")]}
    # The final round never promoted, so its round-2 tag does not exist: use round 1,
    # and prefer a loser that built something over one that died.
    assert crucible.losing_branch([promoted, died]) == (1, 3)
    solo = {"round": 1, "winner": 1, "candidates": [_cand(1)]}
    assert crucible.losing_branch([solo]) == (1, None)  # -n 1: nothing lost
    assert crucible.losing_branch([died]) is None


def test_html_report_renders_a_run_with_zero_rounds(tmp_path):
    run_dir = tmp_path / "empty-run"
    _write_run(run_dir, [])
    page = crucible.render_html_report(run_dir)
    assert "No round completed" in page
    assert "unfinished" in page  # no stop_reason recorded
    assert "(IDEA.md is missing)" in page


@pytest.mark.parametrize(
    "critique", ["{not json", '["a", "list"]', '{"scores": {"correctness": "high"}}', "\xff"]
)
def test_html_report_skips_a_corrupt_critique(tmp_path, critique):
    """One bad critique.json costs its card the dimension bars — never the report."""
    run_dir = tmp_path / "solo"
    cand = {"id": 1, "angle": "straight read", "status": "green", "gates_passed": "2/2",
            "min_score": 6, "verdict": "REVISE", "cost_usd": 0.3, "commit": "abc"}  # fmt: skip
    _write_run(
        run_dir,
        [{"round": 1, "base": "b", "winner": 1, "gates": "pass", "min_score": 6,
          "verdict": "REVISE", "cost_usd": 0.3, "candidates": [cand]}],  # fmt: skip
        stop_reason="max_rounds", best_round=1, best_overall=6,
    )
    (run_dir / "IDEA.md").write_text("# A solo run\n\nwith -n 1")
    cdir = run_dir / "rounds" / "round_1" / "cand_1"
    cdir.mkdir(parents=True)
    (cdir / "critique.json").write_bytes(critique.encode("latin-1"))
    page = crucible.render_html_report(run_dir)
    assert "critique.json missing or unreadable" in page
    assert 'id="r1-c1" data-winner="true"' in page
    assert "<h1>A solo run</h1>" in page
    assert ">R1</text>" in page  # a single round still charts


def test_html_rendering_bug_never_fails_a_run(tmp_path, monkeypatch, capsys):
    def broken(run_dir):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(crucible, "render_html_report", broken)
    assert crucible.write_html_report(tmp_path) is None
    assert "RUN_REPORT.html not written" in capsys.readouterr().out
    assert not (tmp_path / "RUN_REPORT.html").exists()


@requires_tools
def test_report_flag_rerenders_an_existing_run(fake_claude, tmp_path):
    _, run_dir, _ = run_crucible(fake_claude, tmp_path, "tourney", "rerender test")
    page = run_dir / "RUN_REPORT.html"
    original = page.read_text()
    page.unlink()

    # No claude, no git, no gate tools on PATH, and (as root) no allow-list:
    # preflight would refuse all of that, so this proves --report never runs it.
    env = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_BUILDER_ALLOWED_TOOLS"}
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--report", str(run_dir)],
        capture_output=True, text=True, env={**env, "PATH": str(empty)},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(page)
    # Rendered from disk alone, so a re-render is the same page.
    assert page.read_text() == original


def test_report_flag_without_state_json_fails_clearly(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--report", str(tmp_path)],
        capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 1
    assert "no state.json" in proc.stderr


def test_no_idea_needs_demo_or_report():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py")], capture_output=True, text=True
    )
    assert proc.returncode != 0
    assert "an idea is required" in proc.stderr


# ─────────────────────────────── demo mode ─────────────────────────────────


@requires_tools
@pytest.mark.parametrize("extra", [(), ("-n", "1")])
def test_demo_runs_a_whole_tournament_offline(tmp_path, extra):
    """No fake on PATH and no allow-list: --demo must supply both itself."""
    env = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_BUILDER_ALLOWED_TOOLS"}
    env.update(CRUCIBLE_RUNS_DIR=str(tmp_path / "runs"), CRUCIBLE_DEMO_PACE="0", NO_COLOR="1")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--demo", *extra],
        capture_output=True, text=True, env=env, timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / "state.json").read_text())

    assert "demo" in state["run_id"] and state["demo"] is True
    assert state["stop_reason"] == "pass"
    assert state["best_round"] == 3 and state["best_overall"] >= crucible.PASS_THRESHOLD
    assert 0 < state["total_cost_usd"] < state["budget_usd"]
    # Round 1's c1 is red because its own test really fails, not because we say so.
    c1 = state["history"][0]["candidates"][0]
    assert c1["status"] == "red" and c1["gates_passed"] == "1/2"
    assert "failed" in (run_dir / "rounds" / "round_1" / "cand_1" / "gates.txt").read_text()

    assert "(simulated — demo mode, no API calls)" in proc.stdout
    page = (run_dir / "RUN_REPORT.html").read_text()
    assert "simulated · demo mode, no API calls" in page
    assert '<span class="badge good" title="stop reason">pass</span>' in page

    # The delivered artifact is real: its tests pass at `best`.
    ws = run_dir / "workspace"
    assert git_out(ws, "rev-parse", "best") == git_out(ws, "rev-parse", "round-3")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ws, capture_output=True, text=True,
    )  # fmt: skip
    assert tests.returncode == 0, tests.stdout


@requires_tools
def test_demo_never_resolves_claude_by_name(tmp_path):
    """A `claude` first on PATH must never run in a demo — it could be the real,
    billed CLI. The demo launches its fake by absolute argv, so nothing can fall
    through to a name lookup (a noexec temp dir once let exactly that happen)."""
    sentinel_dir = tmp_path / "bin"
    sentinel_dir.mkdir()
    called = tmp_path / "REAL_CLAUDE_WAS_CALLED"
    sentinel = sentinel_dir / "claude"
    sentinel.write_text(f"#!/bin/sh\ntouch {called}\nexit 1\n")
    sentinel.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_BUILDER_ALLOWED_TOOLS"}
    env.update(
        PATH=f"{sentinel_dir}{os.pathsep}{os.environ['PATH']}",
        CRUCIBLE_RUNS_DIR=str(tmp_path / "runs"), CRUCIBLE_DEMO_PACE="0", NO_COLOR="1",
    )  # fmt: skip
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--demo", "-n", "1", "--max-rounds", "1"],
        capture_output=True, text=True, env=env, timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not called.exists(), "--demo invoked the `claude` on PATH"
    builder = next((tmp_path / "runs").glob("*/rounds/round_1/cand_1/builder.json"))
    assert json.loads(builder.read_text())["session_id"].startswith("demo-")


def test_demo_takes_no_idea():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crucible.py"), "--demo", "a static site generator"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode != 0
    assert "drop the idea" in proc.stderr


@requires_tools
def test_preflight_git_probe_works_outside_a_repo(tmp_path, monkeypatch):
    """`git worktree list -h` exits 128 outside a repository on modern git; the
    probe must not mistake that for missing worktree support."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(crucible, "CLAUDE_CMD", [sys.executable, str(crucible.DEMO_FAKE)])
    monkeypatch.setattr(crucible, "BUILDER_ALLOWED_TOOLS", "Read")
    crucible.preflight()  # dies (SystemExit) if anything is reported missing


def test_demo_without_the_checkout_dies_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(crucible, "DEMO_FAKE", tmp_path / "missing.py")
    with pytest.raises(SystemExit) as exc:
        crucible.enable_demo()
    assert exc.value.code == 1
    assert "ships with the repository" in capsys.readouterr().err


# ──────────────────────────── terminal color ───────────────────────────────


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_no_color_wins(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert not crucible.use_color(FakeTTY())


def test_force_color_colors_a_pipe(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert crucible.use_color(io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert crucible.paint("RED", "red", "bold") == "\033[31;1mRED\033[0m"


def test_color_follows_the_tty_and_is_plain_otherwise(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert crucible.use_color(FakeTTY())
    assert not crucible.use_color(io.StringIO())
    # Off means byte-identical: tests and pipes read log() output verbatim.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert crucible.paint("green", "green") == "green"
