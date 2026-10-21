#!/usr/bin/env python3
"""The `claude` that `crucible.py --demo` launches instead: a whole tournament, offline.

tests/fake_claude.py exists to make assertions possible; this one exists to
make CRUCIBLE watchable without an API key. It speaks the same headless JSON
envelope, honours the same argv contract — `--output-format json`, and
`--json-schema` as *inline* JSON — and recovers its round and candidate from
its cwd (…/candidates/round_N/cand_K), exactly like the test fake.

What it does with that is tell a plausible story about the expense tracker
from docs/ACCEPTANCE_RUN.md:

  round 1  c1 ships a summary() that overwrites instead of accumulating, and
           its own test catches it: the gates go genuinely RED. The rest go
           green but thin, and the critic says exactly why.
  round 2  every candidate validates input properly; the strongest also
           survives a corrupt store. Scores rise, must_fix shrinks.
  round 3  the strongest makes writes atomic. Every dimension clears the bar
           and nothing is left to fix: stop reason `pass`.

The builders write a small but real Python project, so the gates — real
pytest, real ruff — pass and fail on their own. Only the critic is scripted,
and even it reads the code on disk to decide what must be fixed rather than
going by the round number. Costs are simulated: small, plausible
total_cost_usd values, so the run report's cost chart has something to draw.
They are normalised by field size — a wide field costs what three candidates
would — so the default $5 budget never cuts the story short at a large -n.
Nothing leaves the machine.

The strongest candidate is the highest id in the field (the field size is
read from the sibling worktrees, which all exist before any builder starts),
so the story ends in `pass` at any -n, including 1.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

# Seconds of simulated thinking per invocation, so the terminal reads like a
# run rather than a dump. CRUCIBLE_DEMO_PACE=0 turns it off (the tests do).
PACE = float(os.environ.get("CRUCIBLE_DEMO_PACE", "1") or 0)

# ───────────────────────── the project the builders write ──────────────────
#
# One expense tracker, assembled from feature flags so each round's code is
# visibly better than the last:
#   bug       summary() overwrites per category instead of accumulating
#   positive  add() rejects amounts <= 0 (but NaN and inf still slip through)
#   finite    add() rejects anything not a finite number > 0, and blank categories
#   tolerant  a corrupt expenses.json is a domain error, not a traceback
#   atomic    writes go to a temp file and os.replace() it into place

APP_HEAD = '''"""Expense tracker: record expenses, list them, and total them per category.

Expenses live in a JSON file: expenses.json in the working directory, or the
path in $EXPENSES_FILE.
"""

import argparse
import json
{extra_imports}import os
import sys
{tempfile_import}from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("EXPENSES_FILE", "expenses.json"))


class ExpenseError(Exception):
    """A problem the user can fix: bad input or an unreadable store."""

'''

LOAD_PLAIN = '''
def load(path: Path) -> list[dict]:
    """Every recorded expense, oldest first. A missing store is an empty one."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
'''

LOAD_TOLERANT = '''
def load(path: Path) -> list[dict]:
    """Every recorded expense, oldest first. A missing store is an empty one."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExpenseError(f"{path} is not valid JSON ({exc.msg}); fix or remove it") from exc
    if not isinstance(data, list):
        raise ExpenseError(f"{path} does not contain a list of expenses")
    return data
'''

SAVE_PLAIN = '''

def save(path: Path, expenses: list[dict]) -> None:
    path.write_text(json.dumps(expenses, indent=2), encoding="utf-8")
'''

SAVE_ATOMIC = '''

def save(path: Path, expenses: list[dict]) -> None:
    """Write via a temp file and os.replace, so a crash never truncates the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".expenses-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(expenses, fh, indent=2, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
'''

ADD_HEAD = '''

def add(path: Path, amount: float, category: str) -> dict:
    """Record one expense and return it as stored."""
'''

CHECK_POSITIVE = '''    if amount <= 0:
        raise ExpenseError("amount must be greater than 0")
'''

CHECK_FINITE = '''    if not math.isfinite(amount) or amount <= 0:
        raise ExpenseError("amount must be a finite number greater than 0")
    if not category.strip():
        raise ExpenseError("category must not be empty")
'''

ADD_TAIL = '''    expense = {"amount": round(amount, 2), "category": category.strip().lower()}
    expenses = load(path)
    expenses.append(expense)
    save(path, expenses)
    return expense


def list_expenses(path: Path) -> list[dict]:
    return load(path)


def summary(path: Path) -> dict[str, float]:
    """Total spend per category, alphabetical."""
    totals: dict[str, float] = {}
    for expense in load(path):
'''

ACCUMULATE = '''        category = expense["category"]
        totals[category] = round(totals.get(category, 0.0) + expense["amount"], 2)
'''

OVERWRITE = '''        totals[expense["category"]] = expense["amount"]
'''

APP_TAIL = '''    return dict(sorted(totals.items()))


def main(argv: list[str] | None = None, path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="expenses", description="Track expenses.")
    sub = parser.add_subparsers(dest="command", required=True)
    add_cmd = sub.add_parser("add", help="record an expense")
    add_cmd.add_argument("amount", type=float)
    add_cmd.add_argument("category")
    sub.add_parser("list", help="list every expense")
    sub.add_parser("summary", help="total spend per category")
    args = parser.parse_args(argv)
    store = path or DEFAULT_PATH

    try:
        if args.command == "add":
            expense = add(store, args.amount, args.category)
            print(f"Added: {expense['category']}: {expense['amount']:.2f}")
        elif args.command == "list":
            rows = list_expenses(store)
            if not rows:
                print("No expenses yet.")
            for i, expense in enumerate(rows, 1):
                print(f"{i:>3}. {expense['category']}: {expense['amount']:.2f}")
        else:
            totals = summary(store)
            for category, total in totals.items():
                print(f"{category}: {total:.2f}")
            print(f"TOTAL: {sum(totals.values()):.2f}")
    except ExpenseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TESTS_BASE = '''

def test_add_then_list_round_trips(tmp_path):
    store = tmp_path / "expenses.json"
    add(store, 40, "Food")
    add(store, 12.5, "transport")
    assert list_expenses(store) == [
        {"amount": 40, "category": "food"},
        {"amount": 12.5, "category": "transport"},
    ]


def test_summary_totals_per_category(tmp_path):
    store = tmp_path / "expenses.json"
    add(store, 40, "food")
    add(store, 12.5, "food")
    add(store, 30, "transport")
    assert summary(store) == {"food": 52.5, "transport": 30.0}


def test_missing_store_is_empty(tmp_path):
    store = tmp_path / "nothing-here.json"
    assert list_expenses(store) == []
    assert summary(store) == {}


def test_cli_summary_prints_totals(tmp_path, capsys):
    store = tmp_path / "expenses.json"
    assert main(["add", "40", "food"], path=store) == 0
    assert main(["add", "2.5", "food"], path=store) == 0
    capsys.readouterr()
    assert main(["summary"], path=store) == 0
    assert capsys.readouterr().out.splitlines() == ["food: 42.50", "TOTAL: 42.50"]
'''

TESTS_POSITIVE = '''

@pytest.mark.parametrize("amount", [-5, 0])
def test_rejects_non_positive_amount(tmp_path, amount):
    store = tmp_path / "expenses.json"
    with pytest.raises(ExpenseError, match="greater than 0"):
        add(store, amount, "food")
    assert list_expenses(store) == []
'''

TESTS_FINITE = '''

@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_amount(tmp_path, amount):
    store = tmp_path / "expenses.json"
    with pytest.raises(ExpenseError, match="finite"):
        add(store, amount, "food")
    assert not store.exists()


@pytest.mark.parametrize("category", ["", "   "])
def test_rejects_blank_category(tmp_path, category):
    with pytest.raises(ExpenseError, match="category"):
        add(tmp_path / "expenses.json", 5, category)


def test_cli_reports_bad_input_and_exits_1(tmp_path, capsys):
    assert main(["add", "nan", "food"], path=tmp_path / "expenses.json") == 1
    assert capsys.readouterr().err == "Error: amount must be a finite number greater than 0\\n"
'''

TESTS_TOLERANT = '''

def test_corrupt_store_is_a_domain_error(tmp_path):
    store = tmp_path / "expenses.json"
    store.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExpenseError, match="not valid JSON"):
        list_expenses(store)


def test_cli_corrupt_store_exits_1_without_a_traceback(tmp_path, capsys):
    store = tmp_path / "expenses.json"
    store.write_text('{"not": "a list"}', encoding="utf-8")
    assert main(["summary"], path=store) == 1
    assert "does not contain a list" in capsys.readouterr().err
'''

TESTS_ATOMIC = '''

def test_save_leaves_no_temp_files_behind(tmp_path):
    store = tmp_path / "expenses.json"
    add(store, 1, "food")
    add(store, 2, "food")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["expenses.json"]


def test_failed_write_keeps_the_previous_data(tmp_path, monkeypatch):
    store = tmp_path / "expenses.json"
    add(store, 40, "food")

    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(expenses.json, "dump", disk_full)
    with pytest.raises(OSError):
        add(store, 12.5, "food")
    monkeypatch.undo()
    assert list_expenses(store) == [{"amount": 40, "category": "food"}]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["expenses.json"]
'''

# The project's own tool config, as a careful builder would ship it. It also
# makes the gates behave the same wherever the runs directory lives: without
# it, ruff and pytest walk up and adopt whichever config they find first —
# CRUCIBLE's own, when runs/ sits inside the checkout.
PYPROJECT = """[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.ruff.lint.isort]
known-first-party = ["expenses"]

[tool.pytest.ini_options]
testpaths = ["."]
"""


def write_project(cwd: pathlib.Path, feats: set[str], rnd: int, cid: int, angle: str) -> int:
    """Write the expense tracker for this feature set. Returns the test count."""
    validated = "positive" in feats or "finite" in feats
    app = APP_HEAD.format(
        extra_imports="import math\n" if "finite" in feats else "",
        tempfile_import="import tempfile\n" if "atomic" in feats else "",
    )
    app += LOAD_TOLERANT if "tolerant" in feats else LOAD_PLAIN
    app += SAVE_ATOMIC if "atomic" in feats else SAVE_PLAIN
    app += ADD_HEAD
    if "finite" in feats:
        app += CHECK_FINITE
    elif "positive" in feats:
        app += CHECK_POSITIVE
    app += ADD_TAIL + (OVERWRITE if "bug" in feats else ACCUMULATE) + APP_TAIL

    names = ["add", "list_expenses", "main", "summary"]
    if validated or "tolerant" in feats:
        names.insert(0, "ExpenseError")
    # isort layout matches PYPROJECT: pytest is third-party, expenses first-party.
    imports = ""
    if validated or "tolerant" in feats:
        imports += "import pytest\n\n"
    if "atomic" in feats:
        imports += "import expenses\n"
    imports += f"from expenses import {', '.join(names)}\n"
    tests = '"""Tests for the expense tracker."""\n\n' + imports + TESTS_BASE
    if "finite" in feats:
        tests += TESTS_POSITIVE.replace("[-5, 0]", "[-5, 0, -0.01]") + TESTS_FINITE
    elif "positive" in feats:
        tests += TESTS_POSITIVE
    if "tolerant" in feats:
        tests += TESTS_TOLERANT
    if "atomic" in feats:
        tests += TESTS_ATOMIC

    (cwd / "expenses.py").write_text(app, encoding="utf-8")
    (cwd / "test_expenses.py").write_text(tests, encoding="utf-8")
    (cwd / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (cwd / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    approach = angle or "Straight read of the idea."
    (cwd / "README.md").write_text(
        "# Expense tracker\n\n"
        "A small CLI that records expenses and totals them per category, stored as JSON.\n\n"
        "## Usage\n\n"
        "```bash\n"
        "python expenses.py add 40 food\n"
        "python expenses.py list\n"
        "python expenses.py summary\n"
        "```\n\n"
        "Set `EXPENSES_FILE` to keep the store somewhere other than `./expenses.json`.\n\n"
        "## Testing\n\n"
        "```bash\npip install -r requirements.txt\npython -m pytest -q\n```\n\n"
        f"## Decisions\n\n{approach}\n\n"
        f"_Candidate {cid}, round {rnd} — written by the CRUCIBLE demo's simulated builder._\n",
        encoding="utf-8",
    )
    return tests.count("\ndef test_")


# ─────────────────────────────── the story ─────────────────────────────────


def features(rnd: int, cid: int, champion: int) -> set[str]:
    """What this candidate builds this round. The champion is always a step ahead."""
    if rnd == 1:
        if cid == 1:
            return {"bug"}
        return {"positive"} if cid == champion else set()
    if rnd == 2:
        return {"finite", "tolerant"} if cid == champion else {"finite"}
    return {"finite", "tolerant", "atomic"} if cid == champion else {"finite", "tolerant"}


def inspect(cwd: pathlib.Path) -> set[str]:
    """What the code on disk actually does — the critic reviews files, not rounds."""
    try:
        src = (cwd / "expenses.py").read_text(encoding="utf-8")
    except OSError:
        return set()
    found = set()
    if "math.isfinite" in src:
        found.add("finite")
    elif "amount <= 0" in src:
        found.add("positive")
    if "JSONDecodeError" in src:
        found.add("tolerant")
    if "os.replace" in src:
        found.add("atomic")
    return found


def review(found: set[str], cid: int, champion: int) -> tuple[dict, list[str]]:
    """Scores and blocking findings for what is on disk."""
    val = 2 if "finite" in found else 1 if "positive" in found else 0
    tol = int("tolerant" in found)
    atom = int("atomic" in found)
    scores = {
        "correctness": min(10, 5 + val + tol + atom),
        "completeness": 7 + (val > 0) + tol,
        "robustness": 3 + val + tol + 3 * atom,
        "code_quality": 7 + tol + atom,
        "test_quality": 4 + val + tol + atom,
    }
    # Same code, different reviewer's eye: losing candidates read a touch rougher
    # on the dimensions that are not their weakest, so the cards are not clones.
    if cid != champion and cid % 2 == 0:
        scores["completeness"] -= 1

    must_fix: list[str] = []
    if val == 0:
        must_fix += [
            "expenses.py: add() accepts negative and zero amounts, so `add -5 food` silently "
            "lowers the food total — reject anything that is not > 0 with a clear error",
            "test_expenses.py: no test exercises bad input at all — add cases for -5, 0, nan "
            "and an empty category, asserting the specific error",
        ]
    elif val == 1:
        must_fix.append(
            "expenses.py: the `amount <= 0` guard does not reject NaN or Infinity (every NaN "
            "comparison is False and `inf <= 0` is False), so `add nan food` is accepted — "
            "use math.isfinite()"
        )
    if val < 2:
        must_fix.append(
            "expenses.py: add() stores a blank category ('   ' becomes ''), which summary() then "
            "reports as an unnamed line — validate it"
        )
    if not tol:
        must_fix.append(
            "expenses.py: load() lets json.JSONDecodeError escape on a corrupt expenses.json, "
            "so every command dies with a traceback instead of an actionable error"
        )
    if not atom:
        must_fix += [
            "expenses.py: save() truncates expenses.json before writing, so an interrupt or a "
            "full disk mid-write destroys every recorded expense — write a temp file and "
            "os.replace() it",
            "expenses.py: json.dumps() keeps the default allow_nan=True and can emit NaN, which "
            "no strict JSON parser will read back — pass allow_nan=False",
        ]
    return scores, must_fix


# ─────────────────────────────── the CLI ───────────────────────────────────


def emit(envelope: dict) -> None:
    print(json.dumps(envelope))
    sys.exit(0)


def fail(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def identity(cwd: pathlib.Path) -> tuple[int, int, int]:
    """(round, candidate, field size), recovered from the worktree path."""
    rnd = re.search(r"round_(\d+)", str(cwd))
    cid = re.search(r"cand_(\d+)", str(cwd))
    field = [p for p in cwd.parent.glob("cand_*") if re.fullmatch(r"cand_\d+", p.name)]
    return (
        int(rnd.group(1)) if rnd else 1,
        int(cid.group(1)) if cid else 1,
        max([int(p.name[5:]) for p in field], default=1),
    )


def check_contract(argv: list[str]) -> bool:
    """Enforce the real CLI's contract, as tests/fake_claude.py does. True = critic.

    --json-schema takes the schema *inline*; a path fails against the real CLI
    with "not valid JSON", so it fails here too. --output-format json is what
    the controller parses, so anything else is a controller bug worth surfacing.
    """
    if "-p" not in argv:
        fail("the demo fake only runs headless: -p <prompt> is required")
    fmt = argv[argv.index("--output-format") + 1] if "--output-format" in argv else None
    if fmt != "json":
        fail(f"--output-format must be json, got {fmt!r}")
    if "--json-schema" not in argv:
        return False
    value = argv[argv.index("--json-schema") + 1]
    try:
        json.loads(value)
    except json.JSONDecodeError:
        fail(f"--json-schema is not valid JSON: {value[:40]}")
    return True


def field_scale(champion: int) -> float:
    """Shrink per-call cost for fields wider than three, so a run's total stays flat."""
    return 3 / max(champion, 3)


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] in (["--version"], ["-v"]):
        print("0.0.0 (CRUCIBLE demo fake — no API calls)")
        return

    cwd = pathlib.Path.cwd()
    rnd, cid, champion = identity(cwd)
    is_critic = check_contract(argv)
    prompt = argv[argv.index("-p") + 1]

    if is_critic:
        time.sleep(PACE * (0.3 + 0.1 * cid))
        threshold = re.search(r"every score >= (\d+)", prompt)
        bar = int(threshold.group(1)) if threshold else 8
        found = inspect(cwd)
        scores, must_fix = review(found, cid, champion)
        weakest = min(scores, key=scores.get)
        verdict = "PASS" if not must_fix and min(scores.values()) >= bar else "REVISE"
        if verdict == "PASS":
            summary = (
                "Small, honest and finished: input is validated at the boundary, a corrupt or "
                "half-written store cannot lose data, and the tests pin real behaviour "
                "including every failure path. Nothing blocking."
            )
        else:
            summary = (
                f"Gates are green, but green is not done: {len(must_fix)} blocking issue(s). "
                f"Weakest dimension is {weakest} at {scores[weakest]}/10."
            )
        emit({
            "type": "result", "subtype": "success", "is_error": False,
            "duration_ms": 4100 + 300 * cid, "num_turns": 6 + cid,
            "session_id": f"demo-critic-r{rnd}-c{cid}",
            "total_cost_usd": round((0.08 + 0.011 * cid + 0.007 * rnd) * field_scale(champion), 4),
            "result": summary,
            "structured_output": {
                "scores": scores, "must_fix": must_fix, "summary": summary, "verdict": verdict,
            },
        })  # fmt: skip

    time.sleep(PACE * (0.5 + 0.15 * cid))
    angle = re.search(r"## Emphasis for this attempt\n(.+)", prompt)
    feats = features(rnd, cid, champion)
    n_tests = write_project(cwd, feats, rnd, cid, angle.group(1).strip() if angle else "")
    emit({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 21000 + 2500 * cid, "num_turns": 9 + 2 * cid + rnd,
        "session_id": f"demo-builder-r{rnd}-c{cid}",
        "total_cost_usd": round((0.17 + 0.023 * cid + 0.031 * rnd) * field_scale(champion), 4),
        "result": f"Round {rnd}: wrote expenses.py and {n_tests} tests "
                  f"({', '.join(sorted(feats)) or 'baseline'}).",
    })  # fmt: skip


if __name__ == "__main__":
    main()
