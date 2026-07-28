#!/usr/bin/env python3
"""CRUCIBLE — build → critique → rebuild loop agent.

One idea in, working repo out. Claude Code (headless) builds the project,
deterministic gates and a fresh-context critic score it, and this controller
decides whether to iterate or stop. "Satisfied" is a set of rules, never the
model's feeling.

Every round is a tournament: N builders fork from the same base commit into
isolated git worktrees, build in parallel under different emphases, and only
the winner is promoted onto trunk.

Usage:
    python crucible.py IDEA.md            # start a run from an idea file
    python crucible.py "one-line idea"    # or inline text
    python crucible.py IDEA.md -n 5       # 5 candidates per round
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
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────────── config ────────────────────────────────────

MAX_ROUNDS = 5  # hard cap, non-negotiable
CANDIDATES = 3  # builders forked per round; 1 restores the sequential v1 loop
MAX_PARALLEL = 3  # concurrent claude sessions
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

# Independent samples of the same prompt cluster around the same solution, so
# best-of-N buys little. Each candidate gets a different emphasis instead:
# decorrelated attempts give the tournament something real to choose between.
# Index 0 is deliberately empty — one candidate always reads the idea straight.
CANDIDATE_ANGLES = [
    "",
    "Bias toward the simplest design that fully satisfies the idea: fewer "
    "moving parts, fewer abstractions, fewer files. Resist speculative "
    "generality.",
    "Bias toward robustness: bad input, empty state, and boundary conditions "
    "are where this will be judged hardest. Handle them explicitly and cover "
    "them in tests.",
    "Bias toward structure and testability: small, well-named units, each with "
    "focused tests that pin down real behavior.",
    "Bias toward completeness: re-read the idea line by line and make sure no "
    "stated requirement is quietly dropped or half-implemented.",
]

# The builder runs with --dangerously-skip-permissions, which is acceptable
# ONLY because cwd is a throwaway candidate worktree created by this run. Never
# point CRUCIBLE at an existing repo. Stricter alternative — swap the flag for:
# BUILDER_ALLOWED_TOOLS = "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(python *),Bash(pytest *),Bash(ruff *)"

CRITIC_ALLOWED_TOOLS = "Read,Glob,Grep,Bash(git diff *),Bash(git log *)"

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
SCHEMA = ROOT / "schemas" / "critique.schema.json"
RUNS = ROOT / "runs"

TRUNK = "trunk"  # the promoted line of history inside workspace/
GATE_FEEDBACK_LINES = 120  # tail of failure output handed to the next builder
GATES_SUMMARY_LINES = 60  # tail of gate output handed to the critic

WORKSPACE_GITIGNORE = """__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
"""

# Candidates commit concurrently into one object store. Separate worktrees keep
# separate indexes, but ref updates are shared — so controller-side git is
# serialized. Builds stay parallel; these calls are milliseconds.
GIT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


# ─────────────────────────────── helpers ───────────────────────────────────


def log(msg: str = "") -> None:
    with LOG_LOCK:
        print(msg, flush=True)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"\ncrucible: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def slugify(text: str) -> str:
    """First few meaningful words of the idea, as a filesystem-safe slug."""
    first = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            first = line
            break
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return "-".join(slug.split("-")[:4])[:40].strip("-") or "run"


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
    with GIT_LOCK:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc


# ────────────────────────────── candidates ─────────────────────────────────


@dataclass
class Candidate:
    """One forked attempt at a round, living in its own git worktree."""

    cid: int
    angle: str
    branch: str
    path: Path  # worktree directory (outside workspace/)
    dir: Path  # rounds/round_N/cand_K — artifacts, never seen by the builder
    cost: float = 0.0
    dead: bool = False
    dead_reason: str = ""
    gates_ok: bool = False
    gates_passed: int = 0
    gates_full: str = ""
    failure_output: str = ""
    critique: dict | None = None
    overall: int | None = None
    commit: str = ""

    @property
    def label(self) -> str:
        return f"c{self.cid}"

    def summary(self) -> dict:
        return {
            "id": self.cid,
            "angle": self.angle.split(":")[0] if self.angle else "straight read",
            "status": self.dead_reason if self.dead else ("green" if self.gates_ok else "red"),
            "gates_passed": f"{self.gates_passed}/{len(GATES)}",
            "min_score": self.overall,
            "verdict": self.critique["verdict"] if self.critique else None,
            "cost_usd": round(self.cost, 4),
            "commit": self.commit[:8],
        }


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
    else:
        probe = subprocess.run(
            ["git", "worktree", "list", "-h"], capture_output=True, text=True
        )
        if probe.returncode not in (0, 129):  # 129 = usage text, the norm for -h
            missing.append("`git worktree` support (git >= 2.5) — required to fork candidates")

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
                    f"`{module}` module — required by the `{name}` gate (pip install {module})"
                )
        # pytest --timeout= needs the pytest-timeout plugin.
        if any("pytest" in part for part in cmd) and any(
            a.startswith("--timeout") for a in cmd
        ):
            probe = subprocess.run(
                [sys.executable, "-c", "import pytest_timeout"], capture_output=True, text=True
            )
            if probe.returncode != 0:
                missing.append(
                    f"`pytest-timeout` plugin — the `{name}` gate passes --timeout "
                    "(pip install pytest-timeout)"
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


def invoke_builder(prompt: str, cwd: Path, out_dir: Path, label: str):
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", BUILDER_MODEL,
        "--max-turns", str(BUILDER_MAX_TURNS),
        "--dangerously-skip-permissions",
    ]  # fmt: skip
    return invoke_claude(argv, cwd, BUILDER_TIMEOUT_S, label, out_dir / "builder.json")


def invoke_critic(prompt: str, cwd: Path, out_dir: Path, label: str):
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--json-schema", str(SCHEMA),  # absolute: cwd has no schemas/ dir
        "--model", CRITIC_MODEL,
        "--max-turns", str(CRITIC_MAX_TURNS),
        "--allowedTools", CRITIC_ALLOWED_TOOLS,
    ]  # fmt: skip
    return invoke_claude(argv, cwd, CRITIC_TIMEOUT_S, label, out_dir / "critic.json")


# ──────────────────────────────── gates ────────────────────────────────────


def run_gates(cwd: Path, out_dir: Path) -> tuple[bool, int, str, str]:
    """Run every gate command. Returns (all_passed, n_passed, failures, full).

    Deterministic: no model is involved in deciding pass/fail.
    """
    chunks: list[str] = []
    failures: list[str] = []
    n_passed = 0

    # Gates are the ground truth of this whole design, so never let a stale
    # bytecode cache answer for the working tree: pytest keys its
    # assertion-rewrite cache on (mtime, size), and a builder can rewrite a
    # same-size file within one second.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    for name, cmd in GATES:
        header = f"$ {' '.join(cmd)}"
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=GATE_TIMEOUT_S, env=env,
            )  # fmt: skip
        except FileNotFoundError:
            die(
                f"the `{name}` gate needs `{cmd[0]}`, which is not on PATH. "
                "Install it and re-run — gates are never skipped."
            )
        except subprocess.TimeoutExpired:
            chunks.append(f"{header}\n[TIMEOUT after {GATE_TIMEOUT_S}s]")
            failures.append(f"### gate `{name}` timed out after {GATE_TIMEOUT_S}s")
            continue

        body = (proc.stdout or "") + (proc.stderr or "")
        chunks.append(f"{header}\n[exit {proc.returncode}]\n{body}")
        if proc.returncode == 0:
            n_passed += 1
        else:
            failures.append(f"### gate `{name}` failed (exit {proc.returncode})\n{body}")

    full = "\n\n".join(chunks)
    (out_dir / "gates.txt").write_text(full, encoding="utf-8")
    return n_passed == len(GATES), n_passed, "\n".join(failures), full


# ────────────────────────── the round tournament ───────────────────────────


def build_candidate(
    cand: Candidate, n: int, idea: str, feedback: str, source: str
) -> None:
    """Build one candidate in its worktree, then gate it. Runs in a thread."""
    angle_block = f"\n## Emphasis for this attempt\n{cand.angle}\n" if cand.angle else ""

    if n == 1:
        prompt = render(PROMPTS / "builder_init.md", {"IDEA": idea, "ANGLE": angle_block})
    else:
        prompt = render(
            PROMPTS / "builder_revise.md",
            {"IDEA": idea, "SOURCE": source, "FEEDBACK": feedback,
             "N": str(n), "ANGLE": angle_block},
        )  # fmt: skip

    envelope, cost = invoke_builder(prompt, cand.path, cand.dir, f"builder {cand.label}")
    cand.cost += cost
    if envelope is None:
        cand.dead, cand.dead_reason = True, "builder error"
        log(f"  {cand.label}: builder failed twice — candidate dropped")
        return

    # Safety commit — the builder is told to commit; we make sure of it.
    git(["add", "-A"], cand.path)
    git(["commit", "-m", f"round {n} ({cand.label})", "--allow-empty"], cand.path, check=False)
    cand.commit = git(["rev-parse", "HEAD"], cand.path, check=False).stdout.strip()

    produced = [
        p for p in cand.path.iterdir() if p.name not in (".git", ".gitignore")
    ]
    if not produced:
        cand.dead, cand.dead_reason = True, "produced nothing"
        log(f"  {cand.label}: produced nothing — candidate dropped")
        return

    cand.gates_ok, cand.gates_passed, cand.failure_output, cand.gates_full = run_gates(
        cand.path, cand.dir
    )
    log(
        f"  {cand.label}: built ${cand.cost:.4f}, "
        f"gates {cand.gates_passed}/{len(GATES)} "
        f"{'green' if cand.gates_ok else 'RED'}"
    )


def critique_candidate(cand: Candidate, idea: str) -> None:
    """Score one gates-green candidate. Runs in a thread."""
    prompt = render(
        PROMPTS / "critic.md",
        {
            "IDEA": idea,
            "GATES_SUMMARY": tail(cand.gates_full, GATES_SUMMARY_LINES),
            "PASS_THRESHOLD": str(PASS_THRESHOLD),
        },
    )
    envelope, cost = invoke_critic(prompt, cand.path, cand.dir, f"critic {cand.label}")
    cand.cost += cost
    if envelope is None:
        log(f"  {cand.label}: critic failed twice — unscored")
        return

    critique = envelope.get("structured_output")
    if not isinstance(critique, dict) or "scores" not in critique:
        log(f"  {cand.label}: critic returned no structured_output — unscored")
        return

    cand.critique = critique
    cand.overall = min(critique["scores"].values())  # weakest dimension carries it
    (cand.dir / "critique.json").write_text(json.dumps(critique, indent=2), encoding="utf-8")
    scores = ", ".join(f"{k}={v}" for k, v in critique["scores"].items())
    log(f"  {cand.label}: {critique['verdict']}, min {cand.overall}/10 ({scores})")


def run_round(
    n: int, run_dir: Path, workspace: Path, idea: str, feedback: str,
    source: str, n_candidates: int,
) -> tuple[Candidate | None, list[Candidate], str]:  # fmt: skip
    """Run one tournament round. Returns (winner, all candidates, error)."""
    round_dir = run_dir / "rounds" / f"round_{n}"
    round_dir.mkdir(parents=True, exist_ok=True)
    wt_root = run_dir / "candidates" / f"round_{n}"

    base = git(["rev-parse", "HEAD"], workspace).stdout.strip()

    cands: list[Candidate] = []
    for k in range(1, n_candidates + 1):
        cand = Candidate(
            cid=k,
            angle=CANDIDATE_ANGLES[(k - 1) % len(CANDIDATE_ANGLES)],
            branch=f"cand-r{n}-{k}",
            path=wt_root / f"cand_{k}",
            dir=round_dir / f"cand_{k}",
        )
        cand.dir.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "--quiet", "-b", cand.branch, str(cand.path), base], workspace)
        cands.append(cand)

    log(f"  forking {n_candidates} candidate(s) from {base[:8]}…")
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        list(pool.map(lambda c: build_candidate(c, n, idea, feedback, source), cands))

    alive = [c for c in cands if not c.dead]
    if not alive:
        return None, cands, "builder_error"

    survivors = [c for c in alive if c.gates_ok]
    if survivors:
        log(f"  critiquing {len(survivors)} survivor(s)…")
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            list(pool.map(lambda c: critique_candidate(c, idea), survivors))
        scored = [c for c in survivors if c.critique]
        if not scored:
            return None, cands, "critic_error"
        # Highest weakest-dimension score; ties go to the lower candidate id.
        winner = max(scored, key=lambda c: (c.overall, -c.cid))
    else:
        # Nobody is green. Carry forward the least-broken attempt so the next
        # round revises the most promising line rather than a random one.
        winner = max(alive, key=lambda c: (c.gates_passed, -c.cid))
        log(f"  no candidate passed the gates — carrying {winner.label} forward")

    # Promote: trunk has not moved since the fork, so this fast-forwards.
    ff = git(["merge", "--ff-only", winner.branch], workspace, check=False)
    if ff.returncode != 0:
        log("  ! fast-forward failed — resetting trunk to the winner")
        git(["reset", "--hard", winner.branch], workspace)
    git(["tag", "-f", f"round-{n}"], workspace, check=False)

    (round_dir / "selection.json").write_text(
        json.dumps(
            {"round": n, "base": base[:8], "winner": winner.cid,
             "candidates": [c.summary() for c in cands]},
            indent=2,
        ),
        encoding="utf-8",
    )  # fmt: skip

    # Branches persist for forensics; the working trees are just disk.
    for c in cands:
        git(["worktree", "remove", "--force", str(c.path)], workspace, check=False)

    return winner, cands, ""


# ─────────────────────────── state and report ──────────────────────────────


def write_state(run_dir: Path, state: dict) -> None:
    (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_report(
    run_dir: Path, state: dict, idea: str, tag_note: str, tagged: bool, n_cand: int
) -> None:
    rows = []
    for h in state["history"]:
        score = "—" if h["min_score"] is None else str(h["min_score"])
        green = sum(1 for c in h["candidates"] if c["status"] == "green")
        rows.append(
            f"| {h['round']} | {len(h['candidates'])} | {green} | "
            f"#{h['winner']} | {h['gates']} | {score} | {h['verdict'] or '—'} | "
            f"${h['cost_usd']:.4f} |"
        )
    table = "\n".join(rows) if rows else "| — | — | — | — | — | — | — | — |"

    detail = []
    for h in state["history"]:
        detail.append(f"\n### Round {h['round']} — base `{h['base']}`\n")
        detail.append("| Cand | Emphasis | Status | Gates | Min score | Verdict | Cost |")
        detail.append("|---|---|---|---|---|---|---|")
        for c in h["candidates"]:
            mark = " **←**" if c["id"] == h["winner"] else ""
            detail.append(
                f"| #{c['id']}{mark} | {c['angle']} | {c['status']} | "
                f"{c['gates_passed']} | {c['min_score'] or '—'} | "
                f"{c['verdict'] or '—'} | ${c['cost_usd']:.4f} |"
            )

    ws = run_dir / "workspace"
    ws_display = ws.relative_to(ROOT) if ws.is_relative_to(ROOT) else ws
    checkout = (
        f"""
```bash
cd {ws_display}
git checkout best
```

Every promoted round is tagged `round-1` … `round-{state['round']}`. Every
candidate that ever built — winners and losers — is kept as a branch
`cand-r<round>-<id>`, so a losing attempt can still be inspected or salvaged:

```bash
git branch --list 'cand-*'
git diff round-{state['round']} cand-r{state['round']}-1
```
"""
        if tagged
        else ""
    )

    best = state["best_round"]
    best_line = (
        f"round {best} (min score {state['best_overall']})"
        if best is not None
        else "no critic-scored round"
    )

    report = f"""# CRUCIBLE run report

**Run:** `{state['run_id']}`
**Stop reason:** `{state['stop_reason']}`
**Rounds completed:** {state['round']} / {MAX_ROUNDS}
**Candidates per round:** {n_cand}
**Best:** {best_line}
**Total cost:** ${state['total_cost_usd']:.4f} (cap ${MAX_COST_USD:.2f})

## Idea

{idea.strip()}

## Rounds

| Round | Cands | Green | Won | Gates | Min score | Verdict | Cost |
|---|---|---|---|---|---|---|---|
{table}

Min score is the lowest of the five rubric dimensions — a chain is as strong
as its weakest link. Only gates-green candidates are sent to the critic; if a
round produces none, the least-broken attempt is carried forward instead.

## Candidate detail
{chr(10).join(detail)}

## Getting the best version

{tag_note}
{checkout}"""
    (run_dir / "RUN_REPORT.md").write_text(report, encoding="utf-8")


# ──────────────────────────────── the loop ─────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="crucible", description="Build → critique → rebuild loop agent."
    )
    parser.add_argument(
        "idea", help="path to an IDEA.md file, or the idea itself as a one-line string"
    )
    parser.add_argument(
        "-n", "--candidates", type=int, default=CANDIDATES,
        help=f"builders forked per round (default {CANDIDATES}; 1 = sequential v1 loop)",
    )  # fmt: skip
    args = parser.parse_args()

    if args.candidates < 1:
        die("--candidates must be at least 1")

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
    git(["symbolic-ref", "HEAD", f"refs/heads/{TRUNK}"], workspace)
    git(["config", "user.email", "crucible@localhost"], workspace)
    git(["config", "user.name", "CRUCIBLE"], workspace)
    # Running the gates litters each worktree with caches. Without this seed the
    # safety commit would sweep them into the delivered repo.
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8")
    git(["add", "-A"], workspace)
    # Candidates fork from a commit, so trunk needs one before round 1.
    git(["commit", "-q", "-m", "crucible: base", "--allow-empty"], workspace)

    log(f"crucible: run {run_id}")
    log(f"  workspace: {workspace}")
    log(
        f"  caps: {MAX_ROUNDS} rounds x {args.candidates} candidates, "
        f"${MAX_COST_USD:.2f}, pass at {PASS_THRESHOLD}/10\n"
    )

    state = {
        "run_id": run_id, "round": 0, "candidates_per_round": args.candidates,
        "best_round": None, "best_overall": None, "total_cost_usd": 0.0,
        "history": [], "stop_reason": None,
    }  # fmt: skip
    write_state(run_dir, state)

    feedback, source = "", ""
    best_overall, best_round, stall_count = -1, None, 0
    stop_reason: str | None = None

    for n in range(1, MAX_ROUNDS + 1):
        log(f"── round {n}/{MAX_ROUNDS} " + "─" * 44)
        base = git(["rev-parse", "HEAD"], workspace).stdout.strip()[:8]

        winner, cands, error = run_round(
            n, run_dir, workspace, idea, feedback, source, args.candidates
        )
        round_cost = sum(c.cost for c in cands)
        state["total_cost_usd"] += round_cost

        if error or winner is None:
            log(f"  every candidate failed ({error}) — terminating, best-so-far preserved")
            state["history"].append({
                "round": n, "base": base, "winner": None, "gates": error,
                "min_score": None, "verdict": None, "cost_usd": round_cost,
                "candidates": [c.summary() for c in cands],
            })  # fmt: skip
            state["round"] = n
            stop_reason = error or "builder_error"
            break

        # Track best — the winner's score stands for the round.
        if winner.overall is not None:
            if winner.overall > best_overall:
                best_overall, best_round, stall_count = winner.overall, n, 0
            else:
                stall_count += 1

        if winner.critique:
            feedback = "\n".join(f"- {i}" for i in winner.critique["must_fix"])
            if winner.critique.get("summary"):
                feedback += f"\n\nReviewer summary: {winner.critique['summary']}"
            source = "code review"
        else:
            feedback = tail(winner.failure_output, GATE_FEEDBACK_LINES)
            source = "failing gates"

        green = sum(1 for c in cands if c.gates_ok)
        log(
            f"  round {n}: {green}/{len(cands)} green, "
            f"winner {winner.label}"
            + (f" at min {winner.overall}/10" if winner.overall is not None else "")
            + f", ${round_cost:.4f}"
        )

        state["history"].append({
            "round": n, "base": base, "winner": winner.cid,
            "gates": "pass" if winner.gates_ok else "fail",
            "min_score": winner.overall,
            "verdict": winner.critique["verdict"] if winner.critique else None,
            "cost_usd": round_cost,
            "candidates": [c.summary() for c in cands],
        })  # fmt: skip
        state["round"] = n
        state["best_round"] = best_round
        state["best_overall"] = best_overall if best_round is not None else None
        write_state(run_dir, state)

        # Stop rules — first match wins.
        c = winner.critique
        if (
            c and winner.gates_ok and c["verdict"] == "PASS"
            and all(v >= PASS_THRESHOLD for v in c["scores"].values())
            and not c["must_fix"]
        ):  # fmt: skip
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

    # A round where every candidate died never promoted a commit, so its
    # round-N tag does not exist. Never point `best` at a missing ref, and
    # never tell the report's reader to check out something that isn't there.
    if tag_target is not None:
        exists = git(
            ["rev-parse", "--verify", "--quiet", f"round-{tag_target}"], workspace, check=False
        )
        if exists.returncode == 0:
            git(["tag", "-f", "best", f"round-{tag_target}"], workspace, check=False)
        else:
            tag_note = (
                "**Nothing was tagged.** The run ended before any round promoted "
                "a commit, so there is no `best` ref and no work to check out. "
                f"See `rounds/round_{tag_target}/` for what the builders returned."
            )
            tag_target = None

    write_state(run_dir, state)
    write_report(
        run_dir, state, idea, tag_note, tag_target is not None, args.candidates
    )

    log("")
    log("── done " + "─" * 52)
    log(f"  stop reason: {state['stop_reason']}")
    log(f"  best: {'round ' + str(tag_target) if tag_target else 'none'}")
    log(f"  cost: ${state['total_cost_usd']:.4f}")
    log(f"  report: {run_dir / 'RUN_REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
