#!/usr/bin/env python3
"""CRUCIBLE — build → critique → rebuild loop agent.

One idea in, working repo out. Claude Code (headless) builds the project,
deterministic gates and a fresh-context critic score it, and this controller
decides whether to iterate or stop. "Satisfied" is a set of rules, never the
model's feeling.

Usage:
    python crucible.py IDEA.md            # start a run from an idea file
    python crucible.py "one-line idea"    # or inline text
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────── config ────────────────────────────────────

MAX_ROUNDS = 5  # hard cap, non-negotiable
PASS_THRESHOLD = 8  # every rubric dimension must be >= this
STALL_ROUNDS = 2  # consecutive critic rounds with no best-score gain
MAX_COST_USD = 5.00  # cumulative, summed from total_cost_usd
BUILDER_MODEL = "sonnet"
CRITIC_MODEL = "opus"  # different, stronger model = less self-flattery
BUILDER_MAX_TURNS = 80
CRITIC_MAX_TURNS = 40
BUILDER_TIMEOUT_S = 1800  # subprocess timeout per builder round
CRITIC_TIMEOUT_S = 900
GATE_TIMEOUT_S = 300  # per gate command
GATES = [
    ("tests", ["python", "-m", "pytest", "-q", "--timeout=60"]),
    ("lint", ["ruff", "check", "."]),
]

# The builder runs with --dangerously-skip-permissions, which is acceptable
# ONLY because cwd is a throwaway workspace/ created by this run. Never point
# CRUCIBLE at an existing repo. Stricter alternative — swap the flag for:
# BUILDER_ALLOWED_TOOLS = "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(python *),Bash(pytest *),Bash(ruff *)"

CRITIC_ALLOWED_TOOLS = "Read,Glob,Grep,Bash(git diff *),Bash(git log *)"

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
SCHEMA = ROOT / "schemas" / "critique.schema.json"
RUNS = ROOT / "runs"

GATE_FEEDBACK_LINES = 120  # tail of failure output handed to the next builder
GATES_SUMMARY_LINES = 60  # tail of gate output handed to the critic

WORKSPACE_GITIGNORE = """__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
"""


# ─────────────────────────────── helpers ───────────────────────────────────


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"\ncrucible: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def slugify(text: str) -> str:
    """First few meaningful words of the idea, as a filesystem-safe slug."""
    first = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            first = line
            break
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    slug = "-".join(slug.split("-")[:4])[:40].strip("-")
    return slug or "run"


def render(template: Path, mapping: dict[str, str]) -> str:
    """Substitute {PLACEHOLDER} tokens.

    Uses plain replacement rather than str.format so that literal braces in the
    prompt bodies (JSON snippets, shell) survive untouched.
    """
    text = template.read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", value)
    return text


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc


# ────────────────────────────── preflight ──────────────────────────────────


def preflight() -> None:
    """Verify every external tool exists before burning a builder call.

    A missing gate tool terminates the run — a gate is never silently skipped.
    """
    missing: list[str] = []

    if shutil.which("claude") is None:
        missing.append("`claude` (Claude Code CLI) — https://code.claude.com/docs/en/headless")
    if shutil.which("git") is None:
        missing.append("`git`")

    for name, cmd in GATES:
        if shutil.which(cmd[0]) is None:
            missing.append(f"`{cmd[0]}` — required by the `{name}` gate")
            continue
        # `python -m <module>` form: confirm the module is importable.
        if len(cmd) >= 3 and cmd[1] == "-m":
            module = cmd[2]
            probe = subprocess.run(
                [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
            )
            if probe.returncode != 0:
                missing.append(
                    f"`{module}` module — required by the `{name}` gate "
                    f"(pip install {module})"
                )
        # pytest --timeout= needs the pytest-timeout plugin.
        if any("pytest" in part for part in cmd) and any(
            a.startswith("--timeout") for a in cmd
        ):
            probe = subprocess.run(
                [sys.executable, "-c", "import pytest_timeout"],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                missing.append(
                    f"`pytest-timeout` plugin — the `{name}` gate passes --timeout "
                    f"(pip install pytest-timeout)"
                )

    if not SCHEMA.exists():
        missing.append(f"critic schema missing at {SCHEMA}")
    for template in ("builder_init.md", "builder_revise.md", "critic.md"):
        if not (PROMPTS / template).exists():
            missing.append(f"prompt template missing at {PROMPTS / template}")

    if missing:
        die("cannot start — missing prerequisites:\n  - " + "\n  - ".join(missing))


# ────────────────────────── claude invocations ─────────────────────────────


def invoke_claude(
    argv: list[str], cwd: Path, timeout: int, label: str, raw_path: Path
) -> tuple[dict | None, float]:
    """Run a `claude -p` invocation with the retry-once rule.

    Returns (envelope, cost). envelope is None if both attempts failed; cost is
    accumulated across every attempt that reported one, so a failed-then-retried
    round still bills honestly.
    """
    cost = 0.0
    last_raw = ""

    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            log(f"  ! {label}: timed out after {timeout}s (attempt {attempt})")
            last_raw = f"<timeout after {timeout}s>"
            continue
        except FileNotFoundError:
            die("`claude` CLI not found on PATH — https://code.claude.com/docs/en/headless")

        last_raw = proc.stdout or ""
        envelope: dict | None = None
        try:
            envelope = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            envelope = None

        if isinstance(envelope, dict):
            reported = envelope.get("total_cost_usd")
            if isinstance(reported, (int, float)):
                cost += float(reported)

        if proc.returncode != 0:
            log(f"  ! {label}: exit {proc.returncode} (attempt {attempt})")
            log("    " + tail(proc.stderr or proc.stdout or "", 5).replace("\n", "\n    "))
            continue
        if envelope is None:
            log(f"  ! {label}: unparseable JSON envelope (attempt {attempt})")
            continue
        if envelope.get("is_error"):
            log(f"  ! {label}: is_error=true (attempt {attempt})")
            continue

        raw_path.write_text(proc.stdout, encoding="utf-8")
        return envelope, cost

    raw_path.write_text(last_raw, encoding="utf-8")
    return None, cost


def invoke_builder(
    prompt: str, workspace: Path, round_dir: Path
) -> tuple[dict | None, float]:
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        BUILDER_MODEL,
        "--max-turns",
        str(BUILDER_MAX_TURNS),
        "--dangerously-skip-permissions",
    ]
    return invoke_claude(
        argv, workspace, BUILDER_TIMEOUT_S, "builder", round_dir / "builder.json"
    )


def invoke_critic(
    prompt: str, workspace: Path, round_dir: Path
) -> tuple[dict | None, float]:
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        str(SCHEMA),  # absolute: cwd is workspace/, which has no schemas/ dir
        "--model",
        CRITIC_MODEL,
        "--max-turns",
        str(CRITIC_MAX_TURNS),
        "--allowedTools",
        CRITIC_ALLOWED_TOOLS,
    ]
    return invoke_claude(
        argv, workspace, CRITIC_TIMEOUT_S, "critic", round_dir / "critic.json"
    )


# ──────────────────────────────── gates ────────────────────────────────────


def run_gates(workspace: Path, round_dir: Path) -> tuple[bool, str, str]:
    """Run every gate command in the workspace.

    Returns (all_passed, failure_output, full_output). Deterministic: no model
    is involved in deciding pass/fail.
    """
    chunks: list[str] = []
    failures: list[str] = []
    passed = True

    # Gates are the ground truth of this whole design, so never let a stale
    # bytecode cache answer for the working tree: pytest keys its
    # assertion-rewrite cache on (mtime, size), and a builder can rewrite a
    # same-size file within one second.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    for name, cmd in GATES:
        header = f"$ {' '.join(cmd)}"
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT_S,
                env=env,
            )
        except FileNotFoundError:
            die(
                f"the `{name}` gate needs `{cmd[0]}`, which is not on PATH. "
                "Install it and re-run — gates are never skipped."
            )
        except subprocess.TimeoutExpired:
            body = f"[TIMEOUT after {GATE_TIMEOUT_S}s]"
            chunks.append(f"{header}\n{body}")
            failures.append(f"### gate `{name}` timed out after {GATE_TIMEOUT_S}s")
            passed = False
            continue

        body = (proc.stdout or "") + (proc.stderr or "")
        chunks.append(f"{header}\n[exit {proc.returncode}]\n{body}")
        status = "pass" if proc.returncode == 0 else "FAIL"
        log(f"  gate {name}: {status}")
        if proc.returncode != 0:
            passed = False
            failures.append(f"### gate `{name}` failed (exit {proc.returncode})\n{body}")

    full = "\n\n".join(chunks)
    (round_dir / "gates.txt").write_text(full, encoding="utf-8")
    return passed, "\n".join(failures), full


# ─────────────────────────── state and report ──────────────────────────────


def write_state(run_dir: Path, state: dict) -> None:
    (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_report(
    run_dir: Path, state: dict, idea: str, tag_note: str, tagged: bool
) -> None:
    rows = []
    for h in state["history"]:
        score = "—" if h["min_score"] is None else str(h["min_score"])
        verdict = h["verdict"] or "—"
        rows.append(
            f"| {h['round']} | {h['gates']} | {score} | {verdict} | ${h['cost_usd']:.4f} |"
        )
    table = "\n".join(rows) if rows else "| — | — | — | — | — |"

    best = state["best_round"]
    best_line = (
        f"round {best} (min score {state['best_overall']})"
        if best is not None
        else "no critic-scored round"
    )

    ws = run_dir / "workspace"
    ws_display = ws.relative_to(ROOT) if ws.is_relative_to(ROOT) else ws
    checkout = (
        f"""
```bash
cd {ws_display}
git checkout best
```

Every round that committed is also tagged `round-1` … `round-{state['round']}`.
"""
        if tagged
        else ""
    )

    report = f"""# CRUCIBLE run report

**Run:** `{state['run_id']}`
**Stop reason:** `{state['stop_reason']}`
**Rounds completed:** {state['round']} / {MAX_ROUNDS}
**Best:** {best_line}
**Total cost:** ${state['total_cost_usd']:.4f} (cap ${MAX_COST_USD:.2f})

## Idea

{idea.strip()}

## Rounds

| Round | Gates | Min score | Verdict | Cost |
|---|---|---|---|---|
{table}

Min score is the lowest of the five rubric dimensions — a chain is as strong
as its weakest link. Rounds whose gates failed are not sent to the critic.

## Getting the best version

{tag_note}
{checkout}"""
    (run_dir / "RUN_REPORT.md").write_text(report, encoding="utf-8")


# ──────────────────────────────── the loop ─────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="crucible",
        description="Build → critique → rebuild loop agent.",
    )
    parser.add_argument(
        "idea",
        help="path to an IDEA.md file, or the idea itself as a one-line string",
    )
    args = parser.parse_args()

    preflight()

    # Idea: a readable file path, else literal inline text.
    idea_path = Path(args.idea)
    try:
        is_file = idea_path.is_file()
    except OSError:  # e.g. inline text longer than the filename limit
        is_file = False
    idea = idea_path.read_text(encoding="utf-8") if is_file else args.idea
    if not idea.strip():
        die("the idea is empty")

    # Run directory.
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    run_id = f"{stamp}_{slugify(idea)}"
    run_dir = RUNS / run_id
    if run_dir.exists():
        run_id = f"{run_id}-{dt.datetime.now().strftime('%S')}"
        run_dir = RUNS / run_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    (run_dir / "rounds").mkdir()
    (run_dir / "IDEA.md").write_text(idea, encoding="utf-8")

    git(["init", "-q"], workspace)
    git(["config", "user.email", "crucible@localhost"], workspace)
    git(["config", "user.name", "CRUCIBLE"], workspace)
    # Running the gates litters the workspace with caches. Without this seed the
    # next round's `git add -A` would commit them into the delivered repo.
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8")

    log(f"crucible: run {run_id}")
    log(f"  workspace: {workspace}")
    log(f"  caps: {MAX_ROUNDS} rounds, ${MAX_COST_USD:.2f}, pass at {PASS_THRESHOLD}/10\n")

    state = {
        "run_id": run_id,
        "round": 0,
        "best_round": None,
        "best_overall": None,
        "total_cost_usd": 0.0,
        "history": [],
        "stop_reason": None,
    }
    write_state(run_dir, state)

    feedback = ""
    feedback_source = ""
    best_overall = -1
    best_round: int | None = None
    stall_count = 0
    stop_reason: str | None = None

    for n in range(1, MAX_ROUNDS + 1):
        log(f"── round {n}/{MAX_ROUNDS} " + "─" * 44)
        round_dir = run_dir / "rounds" / f"round_{n}"
        round_dir.mkdir(parents=True, exist_ok=True)
        round_cost = 0.0

        # 1. Build.
        if n == 1:
            prompt = render(PROMPTS / "builder_init.md", {"IDEA": idea})
        else:
            prompt = render(
                PROMPTS / "builder_revise.md",
                {
                    "IDEA": idea,
                    "SOURCE": feedback_source,
                    "FEEDBACK": feedback,
                    "N": str(n),
                },
            )

        log("  builder: running…")
        envelope, cost = invoke_builder(prompt, workspace, round_dir)
        round_cost += cost
        state["total_cost_usd"] += cost

        if envelope is None:
            log("  builder failed twice — terminating, best-so-far preserved")
            state["history"].append(
                {
                    "round": n,
                    "gates": "builder error",
                    "min_score": None,
                    "verdict": None,
                    "cost_usd": round_cost,
                }
            )
            state["round"] = n
            stop_reason = "builder_error"
            break

        log(f"  builder: {envelope.get('num_turns', '?')} turns, ${cost:.4f}")

        # 2. Safety commit — the builder is told to commit; we make sure of it.
        git(["add", "-A"], workspace)
        git(["commit", "-m", f"round {n}", "--allow-empty"], workspace, check=False)
        git(["tag", "-f", f"round-{n}"], workspace, check=False)

        # Empty workspace after round 1 is a builder failure.
        if n == 1:
            produced = [p for p in workspace.iterdir() if p.name != ".git"]
            if not produced:
                log("  workspace is empty after round 1 — terminating")
                state["history"].append(
                    {
                        "round": n,
                        "gates": "builder error",
                        "min_score": None,
                        "verdict": None,
                        "cost_usd": round_cost,
                    }
                )
                state["round"] = n
                stop_reason = "builder_error"
                break

        # 3. Gates.
        gates_ok, failure_output, gates_full = run_gates(workspace, round_dir)

        critique: dict | None = None
        overall: int | None = None

        if not gates_ok:
            # No point reviewing broken code — skip the critic, save an opus call.
            feedback = tail(failure_output, GATE_FEEDBACK_LINES)
            feedback_source = "failing gates"
            log("  critic: skipped (gates red)")
        else:
            # 4. Critique.
            log("  critic: running…")
            critic_prompt = render(
                PROMPTS / "critic.md",
                {
                    "IDEA": idea,
                    "GATES_SUMMARY": tail(gates_full, GATES_SUMMARY_LINES),
                    "PASS_THRESHOLD": str(PASS_THRESHOLD),
                },
            )
            critic_env, cost = invoke_critic(critic_prompt, workspace, round_dir)
            round_cost += cost
            state["total_cost_usd"] += cost

            if critic_env is None:
                log("  critic failed twice — terminating, best-so-far preserved")
                state["history"].append(
                    {
                        "round": n,
                        "gates": "pass",
                        "min_score": None,
                        "verdict": None,
                        "cost_usd": round_cost,
                    }
                )
                state["round"] = n
                stop_reason = "critic_error"
                break

            critique = critic_env.get("structured_output")
            if not isinstance(critique, dict) or "scores" not in critique:
                log("  critic returned no structured_output — terminating")
                state["history"].append(
                    {
                        "round": n,
                        "gates": "pass",
                        "min_score": None,
                        "verdict": None,
                        "cost_usd": round_cost,
                    }
                )
                state["round"] = n
                stop_reason = "critic_error"
                break

            (round_dir / "critique.json").write_text(
                json.dumps(critique, indent=2), encoding="utf-8"
            )

            scores = critique["scores"]
            overall = min(scores.values())  # weakest dimension carries the round
            log(
                f"  critic: {critique['verdict']}, min {overall}/10 "
                f"({', '.join(f'{k}={v}' for k, v in scores.items())}), ${cost:.4f}"
            )

            # 5. Track best.
            if overall > best_overall:
                best_overall = overall
                best_round = n
                stall_count = 0
            else:
                stall_count += 1

            feedback = "\n".join(f"- {item}" for item in critique["must_fix"])
            if critique.get("summary"):
                feedback += f"\n\nReviewer summary: {critique['summary']}"
            feedback_source = "code review"

        state["history"].append(
            {
                "round": n,
                "gates": "pass" if gates_ok else "fail",
                "min_score": overall,
                "verdict": critique["verdict"] if critique else None,
                "cost_usd": round_cost,
            }
        )
        state["round"] = n
        state["best_round"] = best_round
        state["best_overall"] = best_overall if best_round is not None else None
        write_state(run_dir, state)

        # 6. Stop rules — first match wins.
        if (
            critique is not None
            and gates_ok
            and critique["verdict"] == "PASS"
            and all(v >= PASS_THRESHOLD for v in critique["scores"].values())
            and not critique["must_fix"]
        ):
            stop_reason = "pass"
        elif n == MAX_ROUNDS:
            stop_reason = "max_rounds"
        elif stall_count >= STALL_ROUNDS:
            stop_reason = "stalled"
        elif state["total_cost_usd"] > MAX_COST_USD:
            stop_reason = "budget"

        if stop_reason:
            break

        log("")

    # ── termination ────────────────────────────────────────────────────────
    state["stop_reason"] = stop_reason or "max_rounds"
    state["best_round"] = best_round
    state["best_overall"] = best_overall if best_round is not None else None

    tag_target = best_round
    if tag_target is not None:
        tag_note = (
            f"`best` points at round {tag_target}, the highest-scoring round "
            f"(min score {best_overall}/10) — not necessarily the last one."
        )
    else:
        green = [h["round"] for h in state["history"] if h["gates"] == "pass"]
        if green:
            tag_target = green[-1]
            tag_note = (
                f"No round was scored by the critic, so `best` points at round "
                f"{tag_target}, the last round with green gates."
            )
        elif state["history"]:
            tag_target = state["history"][-1]["round"]
            tag_note = (
                f"**No round ever passed the gates.** `best` points at round "
                f"{tag_target} (the final round) so there is something to inspect, "
                "but it is not known-good."
            )
        else:
            tag_note = "No round completed, so nothing was tagged."

    # A round that died inside the builder never reached its safety commit, so
    # its round-N tag does not exist. Never point `best` at a missing ref, and
    # never tell the report's reader to check out something that isn't there.
    if tag_target is not None:
        exists = git(
            ["rev-parse", "--verify", "--quiet", f"round-{tag_target}"],
            workspace,
            check=False,
        )
        if exists.returncode == 0:
            git(["tag", "-f", "best", f"round-{tag_target}"], workspace, check=False)
        else:
            tag_note = (
                "**Nothing was tagged.** The run ended before any round produced "
                "a commit, so there is no `best` ref and no work to check out. "
                f"See `rounds/round_{tag_target}/` for what the builder returned."
            )
            tag_target = None

    write_state(run_dir, state)
    write_report(run_dir, state, idea, tag_note, tagged=tag_target is not None)

    log("")
    log("── done " + "─" * 52)
    log(f"  stop reason: {state['stop_reason']}")
    log(f"  best: {'round ' + str(tag_target) if tag_target else 'none'}")
    log(f"  cost: ${state['total_cost_usd']:.4f}")
    log(f"  report: {run_dir / 'RUN_REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
