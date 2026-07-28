#!/usr/bin/env python3
"""A fake `claude` CLI, so a non-deterministic system can be tested determinstically.

CRUCIBLE's whole job is orchestrating a model whose output nobody can predict.
That makes the interesting logic — winner selection, promotion, stop rules,
failure handling — untestable against the real CLI: you cannot assert that
candidate 3 wins if you cannot control what candidate 3 builds.

So the tests put this script on PATH as `claude`. It speaks the same headless
JSON envelope, recovers which candidate and round it is from its own cwd
(…/candidates/round_N/cand_K), and produces deliberately different quality per
candidate. That turns "did the tournament pick the best one?" into an assertion.

Scenarios, via CRUCIBLE_FAKE_SCENARIO:
  tourney   c1 red gates, c2 scores 7, c3 scores 9 -> c3 must win
  allred    every candidate fails gates, every round
  onedies   c1's builder errors, the rest are fine
  alldie    every builder errors
  slowpass  nobody passes until round 3, then everyone does
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

SCENARIO = os.environ.get("CRUCIBLE_FAKE_SCENARIO", "tourney")

APP = '''"""Demo app."""


def add(a, b):
    """Return the sum of a and b."""
    return a + b
'''

TEST_PASSING = '''from app import add


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0
'''

TEST_FAILING = '''from app import add


def test_add():
    assert add(2, 3) == 99
'''


def emit(envelope: dict) -> None:
    print(json.dumps(envelope))
    sys.exit(0)


def identity(cwd: pathlib.Path) -> tuple[int, int]:
    """Recover (round, candidate) from the worktree path."""
    rnd = re.search(r"round_(\d+)", str(cwd))
    cid = re.search(r"cand_(\d+)", str(cwd))
    return (int(rnd.group(1)) if rnd else 1, int(cid.group(1)) if cid else 1)


def check_contract(argv: list[str]) -> bool:
    """Enforce the real CLI's contract, so the fake can't give false confidence.

    --json-schema takes the schema *inline*, not a path to it. A path fails
    against the real CLI with "not valid JSON: Unrecognized token '/'", which a
    fake that merely looks for the flag would happily accept — and every real
    critic call would then fail in production. So the fake parses it too.
    """
    if "--json-schema" not in argv:
        return False
    value = argv[argv.index("--json-schema") + 1]
    try:
        json.loads(value)
    except json.JSONDecodeError:
        print(
            f"Error: --json-schema is not valid JSON: {value[:40]}", file=sys.stderr
        )
        sys.exit(1)
    return True


def main() -> None:
    cwd = pathlib.Path.cwd()
    rnd, cid = identity(cwd)
    is_critic = check_contract(sys.argv[1:])

    if is_critic:
        # Higher candidate id = better work, so c3 should win a fair tournament.
        base = 9 if rnd >= 2 else {1: 5, 2: 7, 3: 9}.get(cid, 6)
        scores = {
            "correctness": base,
            "completeness": base,
            "robustness": base,
            "code_quality": min(10, base + 1),
            "test_quality": base,
        }
        clean = base >= 8
        emit({
            "type": "result", "subtype": "success", "is_error": False,
            "num_turns": 5, "session_id": f"fake-critic-r{rnd}-c{cid}",
            "total_cost_usd": 0.10, "result": "reviewed",
            "structured_output": {
                "scores": scores,
                "must_fix": [] if clean else [f"app.py: candidate {cid} needs input validation"],
                "summary": f"fake critique of candidate {cid}, round {rnd}",
                "verdict": "PASS" if clean else "REVISE",
            },
        })  # fmt: skip

    if SCENARIO == "alldie" or (SCENARIO == "onedies" and cid == 1):
        emit({
            "type": "result", "is_error": True, "num_turns": 1,
            "session_id": f"fake-builder-r{rnd}-c{cid}",
            "total_cost_usd": 0.05, "result": "boom",
        })  # fmt: skip

    (cwd / "app.py").write_text(APP)
    (cwd / "requirements.txt").write_text("pytest\n")
    (cwd / "README.md").write_text(f"# Demo\n\nCandidate {cid}, round {rnd}.\n")

    if SCENARIO == "allred":
        failing = True
    elif SCENARIO == "slowpass":
        failing = rnd < 3
    elif SCENARIO == "tourney":
        failing = rnd == 1 and cid == 1
    else:
        failing = False
    (cwd / "test_app.py").write_text(TEST_FAILING if failing else TEST_PASSING)

    emit({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 11, "session_id": f"fake-builder-r{rnd}-c{cid}",
        "total_cost_usd": 0.25, "result": f"built r{rnd} c{cid}",
    })  # fmt: skip


if __name__ == "__main__":
    main()
