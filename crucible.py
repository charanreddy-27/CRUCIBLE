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
    python crucible.py --demo             # a whole tournament offline: no API key, no cost
    python crucible.py --report runs/<id> # re-render a past run's RUN_REPORT.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

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

# By default the builder runs with --dangerously-skip-permissions, which is
# acceptable ONLY because cwd is a throwaway candidate worktree created by this
# run. Never point CRUCIBLE at an existing repo.
#
# Set BUILDER_ALLOWED_TOOLS — here or via CRUCIBLE_BUILDER_ALLOWED_TOOLS — to use
# scoped permissions instead. That is *required* when running as root: the CLI
# refuses --dangerously-skip-permissions under root/sudo, so containers and CI
# have to take this path. Preflight enforces it rather than letting every builder
# invocation fail with an unparseable envelope.
BUILDER_ALLOWED_TOOLS: str | None = os.environ.get("CRUCIBLE_BUILDER_ALLOWED_TOOLS") or None
BUILDER_ALLOWED_TOOLS_SUGGESTION = (
    "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(python *),Bash(pytest *),Bash(ruff *)"
)

CRITIC_ALLOWED_TOOLS = "Read,Glob,Grep,Bash(git diff *),Bash(git log *)"

# How every builder and critic is launched. `--demo` swaps in the bundled fake
# as an absolute interpreter + script argv — never a name to look up — so no
# PATH order, noexec temp dir or Windows .cmd/.exe resolution rule can ever
# route a "simulated" run to the real, billed CLI.
CLAUDE_CMD: list[str] = ["claude"]

__version__ = "2.1.0"

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
SCHEMA = ROOT / "schemas" / "critique.schema.json"
# Overridable so a test suite can run real loops without writing into the repo.
# Absolute from the start: candidate worktrees and every `claude` subprocess
# take paths under here as their cwd, and a relative path stops resolving the
# moment a subprocess runs from inside the workspace.
RUNS = Path(os.environ.get("CRUCIBLE_RUNS_DIR", ROOT / "runs")).resolve()

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


def die(msg: str) -> NoReturn:
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


# ──────────────────────────── terminal color ───────────────────────────────

# Color is for a human at a terminal, so it is opt-out (NO_COLOR, https://no-color.org)
# and opt-in only by request (FORCE_COLOR). With color off, paint() returns its
# input untouched, so log() output — which tests and pipes read — stays
# byte-identical to an uncolored build.
ANSI = {
    "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
    "accent": "38;5;208",  # molten orange, the report's accent
}  # fmt: skip


def use_color(stream=None) -> bool:
    """NO_COLOR wins, then FORCE_COLOR, then whether the stream is a TTY."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR", "").lower() not in ("", "0", "false"):
        return True
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # no isatty, or a closed stream
        return False


def paint(text: str, *styles: str) -> str:
    if not styles or not use_color():
        return text
    return f"\033[{';'.join(ANSI[s] for s in styles)}m{text}\033[0m"


def score_style(score: float | None) -> str:
    return "green" if score is not None and score >= PASS_THRESHOLD else "yellow"


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

    if CLAUDE_CMD == ["claude"] and shutil.which("claude") is None:
        missing.append("`claude` (Claude Code CLI) — https://code.claude.com/docs/en/headless")

    # The CLI refuses --dangerously-skip-permissions under root/sudo. Without
    # this check every builder invocation dies on a non-JSON error, burns its
    # retry, and reports "unparseable JSON envelope" — true, but useless.
    if not BUILDER_ALLOWED_TOOLS and hasattr(os, "geteuid") and os.geteuid() == 0:
        missing.append(
            "running as root, which the CLI forbids with "
            "--dangerously-skip-permissions.\n      Either run as a non-root user, "
            "or set BUILDER_ALLOWED_TOOLS in crucible.py to use scoped\n      "
            f'permissions instead, e.g.\n        BUILDER_ALLOWED_TOOLS = '
            f'"{BUILDER_ALLOWED_TOOLS_SUGGESTION}"'
        )
    if shutil.which("git") is None:
        missing.append("`git`")
    else:
        # Probe inside a scratch repo. `git worktree list -h` from an arbitrary
        # cwd exits 128 ("not a git repository") on modern git, which read as
        # "no worktree support" for anyone running crucible.py from outside one.
        with tempfile.TemporaryDirectory() as scratch:
            subprocess.run(["git", "init", "-q", scratch], capture_output=True)
            probe = subprocess.run(
                ["git", "worktree", "list"], cwd=scratch, capture_output=True, text=True
            )
        if probe.returncode != 0:
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
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                # Without this the CLI waits 3s for stdin on every invocation —
                # dead time multiplied by candidates and rounds.
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            log(f"  ! {label}: timed out after {timeout}s (attempt {attempt})")
            last_raw = f"<timeout after {timeout}s>"
            continue
        except FileNotFoundError:
            if not cwd.is_dir():
                die(f"{label}: working directory {cwd} does not exist")
            if argv[0] == "claude":
                die("`claude` CLI not found on PATH — https://code.claude.com/docs/en/headless")
            die(f"{label}: cannot launch {argv[0]}")

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
        *CLAUDE_CMD, "-p", prompt,
        "--output-format", "json",
        "--model", BUILDER_MODEL,
        "--max-turns", str(BUILDER_MAX_TURNS),
    ]  # fmt: skip
    if BUILDER_ALLOWED_TOOLS:
        argv += ["--allowedTools", BUILDER_ALLOWED_TOOLS]
    else:
        argv.append("--dangerously-skip-permissions")
    return invoke_claude(argv, cwd, BUILDER_TIMEOUT_S, label, out_dir / "builder.json")


def invoke_critic(prompt: str, cwd: Path, out_dir: Path, label: str):
    argv = [
        *CLAUDE_CMD, "-p", prompt,
        "--output-format", "json",
        # --json-schema takes the schema *inline*, not a path to it. Passing a
        # path fails with "not valid JSON: Unrecognized token '/'".
        "--json-schema", SCHEMA.read_text(encoding="utf-8"),
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
        f"{paint('green', 'green') if cand.gates_ok else paint('RED', 'red', 'bold')}"
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
    verdict = paint(critique["verdict"], "green" if critique["verdict"] == "PASS" else "yellow")
    weakest = paint(f"min {cand.overall}/10", score_style(cand.overall), "bold")
    log(f"  {cand.label}: {verdict}, {weakest} ({scores})")


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
    winner: Candidate | None = None

    # Whatever happens below — including a round where every candidate dies —
    # the forensics get written and the worktrees get reclaimed. An aborted
    # round is the one you most want a record of.
    try:
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

        return winner, cands, ""

    finally:
        (round_dir / "selection.json").write_text(
            json.dumps(
                {"round": n, "base": base[:8],
                 "winner": winner.cid if winner else None,
                 "candidates": [c.summary() for c in cands]},
                indent=2,
            ),
            encoding="utf-8",
        )  # fmt: skip
        # Branches persist for forensics; the working trees are just disk.
        for c in cands:
            git(["worktree", "remove", "--force", str(c.path)], workspace, check=False)


# ─────────────────────────── state and report ──────────────────────────────


def losing_branch(history: list) -> tuple[int, int | None] | None:
    """The last promoted round and one of its losers — a ref pair that exists.

    Only a round with a winner was promoted and tagged `round-N`; a final round
    where every builder died, or every critic failed, has no tag at all. And
    with -n 1, or when candidate 1 won, `cand-rN-1` is the winner itself, so
    diffing it against `round-N` would show nothing.
    """
    for h in reversed(history or []):
        if not isinstance(h, dict) or type(h.get("winner")) is not int:
            continue
        losers = [
            c for c in h.get("candidates") or []
            if isinstance(c, dict) and type(c.get("id")) is int and c["id"] != h["winner"]
        ]  # fmt: skip
        # Prefer a loser that actually built something over one that died.
        built = [c for c in losers if c.get("status") in ("green", "red")]
        pick = (built or losers or [None])[0]
        return h["round"], pick["id"] if pick else None
    return None


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
    pair = losing_branch(state["history"])
    diff_line = (
        f"\ngit diff round-{pair[0]} cand-r{pair[0]}-{pair[1]}"
        if pair and pair[1] is not None
        else ""
    )
    checkout = (
        f"""
```bash
cd {ws_display}
git checkout best
```

Every promoted round is tagged `round-N`. Every candidate that ever built —
winners and losers — is kept as a branch `cand-r<round>-<id>`, so a losing
attempt can still be inspected or salvaged:

```bash
git branch --list 'cand-*'{diff_line}
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
    write_html_report(run_dir)


# ────────────────────────────── html report ────────────────────────────────
#
# RUN_REPORT.html is RUN_REPORT.md's visual twin: the same run, laid out so a
# tournament can be read at a glance. It is rendered from disk alone — state.json,
# selection.json, critique.json, IDEA.md — never from live objects, so
# `--report` can re-render any past run and the page always shows what was
# recorded rather than what the controller happens to believe now. One file,
# no network: it has to open from an archive, offline, years later.
#
# Every interpolated value goes through _e(). The idea, the critic's prose and
# the angle text are all model- or user-written: hostile input, to a browser.

RUBRIC = ("correctness", "completeness", "robustness", "code_quality", "test_quality")

# stop_reason -> (badge, tone, one-line explanation). The explanation is
# formatted with the run's recorded caps, not today's defaults.
STOP_REASONS = {
    "pass": ("pass", "good",
             "The winner cleared every gate and scored at least {threshold}/10 on all "
             "five dimensions, with nothing left to fix."),
    "max_rounds": ("max rounds", "warn",
                   "Ran all {max_rounds} rounds without a candidate clearing the bar of "
                   "{threshold}/10 on every dimension."),
    "stalled": ("stalled", "warn",
                "The best score did not improve for {stall} critic-scored rounds in a row, "
                "so more rounds were not buying anything."),
    "budget": ("budget", "warn",
               "Cumulative spend crossed the ${budget:.2f} cap, so the run stopped rather "
               "than keep paying."),
    "builder_error": ("builder error", "bad",
                      "Every builder in the final round failed twice — there was nothing "
                      "left to carry forward."),
    "critic_error": ("critic error", "bad",
                     "Candidates went green, but the critic failed on every one of them, "
                     "so nothing could be scored."),
}  # fmt: skip
STOP_UNKNOWN = ("unfinished", "warn",
                "state.json records no stop reason — the run was interrupted, or is "
                "still going.")  # fmt: skip

REPORT_LIGHT = """
  --bg: #faf8f5; --surface: #ffffff; --surface-2: #f3f0eb; --border: #e5e1da;
  --border-strong: #cdc6bb; --text: #1c1917; --text-2: #44403c; --muted: #736d66;
  --accent: #c2410c; --accent-soft: rgba(194, 65, 12, .09); --accent-ink: #ffffff;
  --green: #15803d; --green-soft: rgba(21, 128, 61, .10);
  --red: #b91c1c; --red-soft: rgba(185, 28, 28, .08); --glow: rgba(194, 65, 12, .07);
  color-scheme: light;
"""

REPORT_CSS = """
:root {
  --bg: #0a0a0b; --surface: #121214; --surface-2: #19191c; --border: #26262a;
  --border-strong: #3a3a40; --text: #eceae7; --text-2: #c4c0ba; --muted: #96938d;
  --accent: #ff8a3d; --accent-soft: rgba(255, 138, 61, .13); --accent-ink: #1a0b02;
  --green: #4ade80; --green-soft: rgba(74, 222, 128, .12);
  --red: #f87171; --red-soft: rgba(248, 113, 113, .13); --glow: rgba(255, 138, 61, .11);
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
    sans-serif;
  color-scheme: dark;
}
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { LIGHT } }
:root[data-theme="light"] { LIGHT }

*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text-2);
  font: 15px/1.6 var(--sans); -webkit-font-smoothing: antialiased;
}
body::before {
  content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background: radial-gradient(900px 420px at 50% -12%, var(--glow), transparent 70%);
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
h1, h2, h3 { color: var(--text); margin: 0; line-height: 1.2; letter-spacing: -.01em; }
h2 {
  font: 700 12px var(--mono); letter-spacing: .16em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 12px;
}
code { font: .9em var(--mono); background: var(--surface-2); padding: 1px 5px; border-radius: 5px; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 20px 16px 56px; display: grid; gap: 24px; }
.wrap > * { min-width: 0; }
@media (min-width: 720px) { .wrap { padding: 28px 28px 72px; } }

.top { display: flex; align-items: center; gap: 12px; }
.brand { display: flex; align-items: center; gap: 9px; font: 800 14px var(--mono);
  letter-spacing: .14em; color: var(--text); }
.brand svg { width: 22px; height: 22px; color: var(--accent); }
.spacer { flex: 1; }
.toggle {
  display: inline-grid; place-items: center; width: 34px; height: 34px; border-radius: 9px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-2);
  cursor: pointer;
}
.toggle[hidden] { display: none; }  /* shown by the script; useless without it */
.toggle:hover { color: var(--text); border-color: var(--border-strong); }
.toggle svg { width: 16px; height: 16px; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; }
.hero { padding: 24px 20px 20px; position: relative; overflow: hidden; }
.hero::before {
  content: ""; position: absolute; inset: 0 0 auto 0; height: 3px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
}
.eyebrow { font: 700 11px var(--mono); letter-spacing: .2em; text-transform: uppercase;
  color: var(--accent); display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.sim { color: var(--text); background: var(--accent-soft); border: 1px solid var(--accent);
  border-radius: 99px; padding: 1px 9px; letter-spacing: .08em; }
.hero h1 { font-size: clamp(22px, 4.2vw, 34px); font-weight: 800; margin: 10px 0 12px;
  overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 4px 18px; font: 12.5px var(--mono);
  color: var(--muted); overflow-wrap: anywhere; }
.meta b { color: var(--text-2); font-weight: 600; }
.stop { margin-top: 18px; display: flex; gap: 12px 16px; align-items: center; flex-wrap: wrap;
  padding: 14px 16px; border-radius: 12px; background: var(--surface-2);
  border: 1px solid var(--border); }
.stop p { margin: 0; flex: 1 1 240px; color: var(--text-2); }
.badge { font: 800 14px var(--mono); letter-spacing: .1em; text-transform: uppercase;
  padding: 7px 13px; border-radius: 9px; white-space: nowrap; }
.badge.good { color: var(--green); background: var(--green-soft);
  box-shadow: inset 0 0 0 1px var(--green); }
.badge.warn { color: var(--accent); background: var(--accent-soft);
  box-shadow: inset 0 0 0 1px var(--accent); }
.badge.bad { color: var(--red); background: var(--red-soft);
  box-shadow: inset 0 0 0 1px var(--red); }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr));
  gap: 12px; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; min-width: 0; }
.kpi .l { font: 700 10.5px var(--mono); letter-spacing: .14em; text-transform: uppercase;
  color: var(--muted); }
.kpi .n { font: 800 26px var(--mono); color: var(--text); margin-top: 4px; letter-spacing: -.02em; }
.kpi .n small { font-size: 14px; font-weight: 600; color: var(--muted); margin-left: 3px; }
.kpi .d { font: 12px var(--mono); color: var(--muted); margin-top: 2px; }
.kpi.good .n { color: var(--green); } .kpi.hot .n { color: var(--accent); }
.meter { height: 6px; border-radius: 99px; background: var(--surface-2); margin-top: 9px;
  overflow: hidden; box-shadow: inset 0 0 0 1px var(--border); }
.meter i { display: block; height: 100%; background: var(--accent); border-radius: 99px; }
.meter.over i { background: var(--red); }

.charts { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); }
@media (min-width: 900px) { .charts { grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); } }
.chart-card { padding: 18px 18px 14px; }
.chart-card h2 { margin-bottom: 8px; }
svg.chart { display: block; width: 100%; height: auto; font-family: var(--mono); }
svg .grid { stroke: var(--border); stroke-dasharray: 2 4; }
svg .axis { stroke: var(--border-strong); }
svg .tick { fill: var(--muted); font-size: 11px; }
svg .thr { stroke: var(--green); stroke-width: 1.5; stroke-dasharray: 6 5; }
svg .thr-l { fill: var(--green); font-size: 11px; font-weight: 700; }
svg .cap { stroke: var(--red); stroke-width: 1.5; stroke-dasharray: 6 5; }
svg .cap-l { fill: var(--red); font-size: 11px; font-weight: 700; }
svg .dot { fill: var(--muted); fill-opacity: .7; }
svg .wline { fill: none; stroke: var(--accent); stroke-width: 2.5; stroke-linejoin: round; }
svg .wdot { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
svg .best { fill: none; stroke: var(--accent); stroke-width: 1.5; }
svg .best-l { fill: var(--accent); font-size: 11px; font-weight: 800; letter-spacing: .08em; }
svg .bar { fill: var(--accent); }
svg .bar-prev { fill: var(--accent); fill-opacity: .28; }
svg .val { fill: var(--text-2); font-size: 11px; font-weight: 600; }
svg .none { fill: var(--muted); font-size: 13px; }
/* Charts scale with their container, and so does SVG text. On a phone they
   shrink to ~0.7x, so labels are drawn larger in user units to land near 10px. */
@media (max-width: 600px) {
  svg .tick, svg .thr-l, svg .cap-l, svg .best-l, svg .val { font-size: 17px; }
  svg .none { font-size: 18px; }
}
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font: 12px var(--mono);
  color: var(--muted); margin-top: 8px; }
.legend i { display: inline-block; width: 14px; height: 3px; margin-right: 6px;
  vertical-align: middle; border-radius: 2px; }
.legend i.dotk { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.legend i.win { background: var(--accent); }
.legend i.thr { background: none; border-top: 2px dashed var(--green); height: 0; }
.legend i.cap { background: none; border-top: 2px dashed var(--red); height: 0; }
.legend i.prev { background: var(--accent); opacity: .28; height: 8px; }

.rounds { display: grid; gap: 26px; }
.round-h { display: flex; align-items: baseline; gap: 6px 14px; flex-wrap: wrap;
  margin-bottom: 12px; }
.round-h h3 { font: 800 18px var(--mono); }
.round-h .sub { font: 12.5px var(--mono); color: var(--muted); }
.round-h .sub b { color: var(--text-2); }
.round-h .fail { color: var(--red); font: 700 12.5px var(--mono); }
.cands { display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); }
.cand { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.cand.win { border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent), 0 14px 40px -24px var(--accent); }
.cand.dead { border-style: dashed; }
.cand-h { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.cid { font: 800 16px var(--mono); color: var(--text); }
.angle { font-size: 13px; color: var(--muted); flex: 1 1 120px; min-width: 0; }
.pill { font: 700 11px var(--mono); letter-spacing: .06em; text-transform: uppercase;
  padding: 2px 8px; border-radius: 99px; white-space: nowrap; }
.pill.green { color: var(--green); background: var(--green-soft); }
.pill.red { color: var(--red); background: var(--red-soft); }
.pill.dead { color: var(--muted); box-shadow: inset 0 0 0 1px var(--border-strong); }
.win-badge { font: 800 10.5px var(--mono); letter-spacing: .12em; text-transform: uppercase;
  color: var(--accent-ink); background: var(--accent); padding: 3px 8px; border-radius: 6px;
  white-space: nowrap; }
.facts { display: flex; flex-wrap: wrap; gap: 2px 14px; font: 12px var(--mono);
  color: var(--muted); }
.facts b { color: var(--text-2); font-weight: 600; }
.dims { display: grid; gap: 5px; }
.dim { display: grid; grid-template-columns: 8.4em minmax(0, 1fr) 1.8em; gap: 8px;
  align-items: center; font: 12px var(--mono); color: var(--muted); }
.dim .track { height: 6px; border-radius: 99px; background: var(--surface-2); overflow: hidden;
  box-shadow: inset 0 0 0 1px var(--border); }
.dim .track i { display: block; height: 100%; border-radius: 99px; background: var(--text-2);
  opacity: .5; }
.dim.ok .track i { background: var(--green); opacity: .85; }
.dim b { text-align: right; color: var(--text); }
.dim.weak, .dim.weak b { color: var(--accent); font-weight: 800; }
.dim.weak .track i { background: var(--accent); opacity: 1; }
.score { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.score .min { font: 800 28px var(--mono); color: var(--text); letter-spacing: -.02em; }
.score .min small { font-size: 13px; color: var(--muted); font-weight: 600; margin-left: 2px; }
.score .lbl { font: 12px var(--mono); color: var(--muted); }
.verdict { font: 700 11px var(--mono); letter-spacing: .08em; padding: 3px 8px;
  border-radius: 6px; }
.verdict.PASS { color: var(--green); background: var(--green-soft); }
.verdict.REVISE { color: var(--accent); background: var(--accent-soft); }
.why { margin: 0; font: 12.5px var(--mono); color: var(--muted); padding: 10px 12px;
  border: 1px dashed var(--border-strong); border-radius: 9px; }
details { border-top: 1px solid var(--border); padding-top: 10px; margin-top: auto; }
summary { cursor: pointer; font: 600 12px var(--mono); color: var(--text-2); }
details p { margin: 10px 0 0; font-size: 13.5px; overflow-wrap: anywhere; }
details ul { margin: 8px 0 0; padding-left: 18px; font-size: 13px; }
details li { margin: 5px 0; overflow-wrap: anywhere; }
.mf { font: 700 11px var(--mono); letter-spacing: .1em; text-transform: uppercase;
  color: var(--accent); margin-top: 10px; }

.section { padding: 20px; }
.section p { margin: 0 0 12px; }
pre { font: 13px/1.6 var(--mono); background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin: 0; color: var(--text); }
pre.idea { white-space: pre-wrap; overflow-wrap: anywhere; }
pre.cmd { overflow-x: auto; margin-top: 12px; }
.empty { padding: 26px 18px; text-align: center; color: var(--muted); font: 13px var(--mono); }
.empty b { display: block; color: var(--text); font-size: 15px; margin-bottom: 4px; }
footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 16px;
  font: 12px var(--mono); color: var(--muted); border-top: 1px solid var(--border);
  padding-top: 16px; }
@media print {
  .toggle, body::before { display: none !important; }
  .card, .cand { break-inside: avoid; }
}
""".replace("LIGHT", REPORT_LIGHT)

# Applied in <head>, before first paint, so a stored preference never flashes
# the wrong theme. Storage can throw (privacy modes, file:// in some browsers),
# hence the try/catch: without it the page is still correct, just unremembered.
REPORT_THEME_BOOT = (
    "(function(){try{var t=localStorage.getItem('crucible-report-theme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);"
    "}catch(e){}})();"
)
REPORT_THEME_TOGGLE = (
    "(function(){var b=document.getElementById('themeToggle');if(!b)return;"
    "b.hidden=false;b.addEventListener('click',function(){"
    "var r=document.documentElement,cur=r.getAttribute('data-theme');"
    "if(!cur)cur=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';"
    "var next=cur==='light'?'dark':'light';r.setAttribute('data-theme',next);"
    "try{localStorage.setItem('crucible-report-theme',next);}catch(e){}});})();"
)

# A crucible: a vessel over a flame.
REPORT_LOGO = (
    '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 11h20l-2.4 12.2a4 4 0 0 1-3.9 3.3h-7.4a4 4 0 0 1-3.9-3.3z"/>'
    '<path d="M16 3.5c2.6 2.7 3 4.6 1.4 6.2a2.3 2.3 0 0 1-3.6-2.6" fill="currentColor"/>'
    "</svg>"
)
REPORT_ICON_THEME = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z"/></svg>'
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _num(value: object) -> float | None:
    """A real number from JSON, else None. bool is an int subclass and is no score."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fmt_score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _read_json(path: Path) -> dict | None:
    """A JSON object from disk; missing, unreadable or malformed are all None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and bad UTF-8
        return None
    return data if isinstance(data, dict) else None


def _valid_critique(data: dict | None) -> dict | None:
    """Keep only what the report can trust: numeric scores, string findings."""
    if not data or not isinstance(data.get("scores"), dict):
        return None
    scores = {str(k): v for k, v in data["scores"].items() if _num(v) is not None}
    if not scores:
        return None
    must_fix = data.get("must_fix")
    verdict = data.get("verdict")
    return {
        "scores": scores,
        "must_fix": [str(i) for i in must_fix] if isinstance(must_fix, list) else [],
        "summary": str(data.get("summary") or ""),
        "verdict": verdict if verdict in ("PASS", "REVISE") else None,
    }


def _load_rounds(run_dir: Path, state: dict) -> list[dict]:
    """Every round on record, joined from state.json, selection.json and critique.json.

    selection.json is preferred: it is written in a `finally`, so it exists even
    for a round that aborted before state.json caught up. History fills in for a
    missing or corrupt one. A bad critique.json costs its card the dimension
    bars, never the report.
    """
    history = {
        h["round"]: h
        for h in state.get("history") or []
        if isinstance(h, dict) and isinstance(h.get("round"), int)
    }
    rounds_dir = run_dir / "rounds"
    numbers = set(history)
    if rounds_dir.is_dir():
        for p in rounds_dir.iterdir():
            m = re.fullmatch(r"round_(\d+)", p.name)
            if m and p.is_dir():
                numbers.add(int(m.group(1)))

    rounds = []
    for n in sorted(numbers):
        h = history.get(n, {})
        sel = _read_json(rounds_dir / f"round_{n}" / "selection.json") or {}
        raw = sel.get("candidates")
        if not isinstance(raw, list):
            raw = h.get("candidates") if isinstance(h.get("candidates"), list) else []
        cands = []
        for c in raw:
            # The id becomes a path component below, so it must be a real int.
            if not isinstance(c, dict) or type(c.get("id")) is not int:
                continue
            crit = _valid_critique(
                _read_json(rounds_dir / f"round_{n}" / f"cand_{c['id']}" / "critique.json")
            )
            score = _num(c.get("min_score"))
            if score is None and crit and c.get("status") == "green":
                score = float(min(_num(v) for v in crit["scores"].values()))
            cands.append({
                "id": c["id"], "angle": str(c.get("angle") or "straight read"),
                "status": str(c.get("status") or "unknown"),
                "gates": str(c.get("gates_passed") or "—"), "score": score,
                "verdict": c.get("verdict") or (crit or {}).get("verdict"),
                "cost": _num(c.get("cost_usd")) or 0.0,
                "commit": str(c.get("commit") or ""), "critique": crit,
            })  # fmt: skip
        winner = sel["winner"] if "winner" in sel else h.get("winner")
        cost = _num(h.get("cost_usd"))
        rounds.append({
            "n": n, "base": str(sel.get("base") or h.get("base") or ""),
            "winner": winner if type(winner) is int else None,
            "error": str(h.get("gates") or "") if h.get("winner") is None else "",
            # A round on disk that never reached history was cut short: the
            # process stopped mid-round, so silence there is not a failure.
            "recorded": n in history,
            "cands": cands,
            "cost": cost if cost is not None else sum(c["cost"] for c in cands),
        })  # fmt: skip
    return rounds


def _idea_title(idea: str, fallback: str) -> str:
    for raw in idea.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= 140 else line[:139] + "…"
    return fallback


def _inline_md(text: str) -> str:
    """Escape, then style `code` and **bold** — the only markdown notes and critics use.

    Styling after escaping means the markup can only ever come from these two
    substitutions, never from the text itself.
    """
    out = _e(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def _x_slots(count: int, left: float, width: float) -> list[float]:
    """Centres of `count` equal slots — one round still lands mid-chart, not on an edge."""
    slot = width / max(count, 1)
    return [left + slot * (i + 0.5) for i in range(count)]


def _score_chart(
    rounds: list[dict], threshold: float, best_round: int | None, empty_note: str
) -> str:
    """Every scored candidate's min score per round, the winners joined by a line."""
    # 520 wide, not wider: on a phone the chart shrinks to ~360px, and every
    # extra unit of width is a unit of label legibility lost.
    w, h, left, right, top, bottom = 520, 260, 34, 14, 16, 30
    plot_w, plot_h = w - left - right, h - top - bottom
    xs = _x_slots(len(rounds), left, plot_w)
    slot = plot_w / max(len(rounds), 1)

    def y(score: float) -> float:
        return top + plot_h * (1 - max(0.0, min(score, 10.0)) / 10)

    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Score progression: '
        f'min rubric score per candidate per round, winners connected">'
    ]
    for s in (0, 2, 4, 6, 8, 10):
        cls = "axis" if s == 0 else "grid"
        parts.append(
            f'<line class="{cls}" x1="{left}" x2="{w - right}" y1="{y(s):.1f}" y2="{y(s):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{y(s) + 4:.1f}" text-anchor="end">{s}</text>'
        )
    parts.append(
        f'<line class="thr" x1="{left}" x2="{w - right}" y1="{y(threshold):.1f}" '
        # Labelled at the left edge: a passing winner usually lands on the right,
        # right where its BEST marker would collide with this label.
        f'y2="{y(threshold):.1f}"/><text class="thr-l" x="{left + 6}" '
        f'y="{y(threshold) - 6:.1f}">PASS {_e(_fmt_score(threshold))}</text>'
    )
    step = max(1, len(rounds) // 12)  # keep round labels legible on long runs
    for i, (r, x) in enumerate(zip(rounds, xs, strict=True)):
        if i % step == 0 or i == len(rounds) - 1:
            parts.append(
                f'<text class="tick" x="{x:.1f}" y="{h - 8}" text-anchor="middle">R{r["n"]}</text>'
            )

    winners: list[tuple[int, float, float] | None] = []
    any_scored = False
    for r, x in zip(rounds, xs, strict=True):
        cands = r["cands"]
        spread = min(10.0, slot * 0.5 / max(len(cands), 1))
        point = None
        for k, c in enumerate(cands):
            if c["score"] is None:
                continue
            any_scored = True
            cx, cy = x + (k - (len(cands) - 1) / 2) * spread, y(c["score"])
            if c["id"] == r["winner"]:
                point = (r["n"], cx, cy)
            tip = f"Round {r['n']}, c{c['id']}: min {_fmt_score(c['score'])}/10"
            parts.append(
                f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="3.6">'
                f"<title>{_e(tip)}</title></circle>"
            )
        winners.append(point)

    # The winners' line breaks across a round with no scored winner rather than
    # drawing a slope through a round that has no score to stand on.
    run: list[tuple[int, float, float]] = []
    for point in [*winners, None]:
        if point is not None:
            run.append(point)
            continue
        if len(run) > 1:
            pts = " ".join(f"{px:.1f},{py:.1f}" for _, px, py in run)
            parts.append(f'<polyline class="wline" points="{pts}"/>')
        run = []
    for point in winners:
        if point is None:
            continue
        n, px, py = point
        parts.append(f'<circle class="wdot" cx="{px:.1f}" cy="{py:.1f}" r="5.5"/>')
        if n == best_round:
            parts.append(
                f'<circle class="best" cx="{px:.1f}" cy="{py:.1f}" r="11"/>'
                f'<text class="best-l" x="{px:.1f}" y="{max(py - 17, 11):.1f}" '
                'text-anchor="middle">BEST</text>'
            )
    if not any_scored:
        parts.append(
            f'<text class="none" x="{left + plot_w / 2:.1f}" y="{top + plot_h / 2:.1f}" '
            f'text-anchor="middle">{_e(empty_note)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _cost_chart(rounds: list[dict], budget: float) -> str:
    """Cumulative spend after each round, against the budget cap."""
    w, h, left, right, top, bottom = 420, 260, 60, 12, 18, 30
    plot_w, plot_h = w - left - right, h - top - bottom
    cumulative, total = [], 0.0
    for r in rounds:
        total += r["cost"]
        cumulative.append(total)
    peak = max(budget, total, 0.01) * 1.12
    xs = _x_slots(len(rounds), left, plot_w)
    bar_w = min(46.0, plot_w / max(len(rounds), 1) * 0.58)

    def y(cost: float) -> float:
        return top + plot_h * (1 - cost / peak)

    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Cumulative cost per round against the ${budget:.2f} budget cap">'
    ]
    for frac in (0, 0.5, 1):
        v = budget * frac
        cls = "axis" if frac == 0 else "grid"
        parts.append(
            f'<line class="{cls}" x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">'
            f"${v:.2f}</text>"
        )
    step = max(1, len(rounds) // 10)
    prev = 0.0
    for i, (r, x, cum) in enumerate(zip(rounds, xs, cumulative, strict=True)):
        bx = x - bar_w / 2
        if prev > 0:
            parts.append(
                f'<rect class="bar-prev" x="{bx:.1f}" y="{y(prev):.1f}" width="{bar_w:.1f}" '
                f'height="{y(0) - y(prev):.1f}" rx="2"/>'
            )
        tip = f"Round {r['n']}: ${r['cost']:.4f} this round, ${cum:.4f} so far"
        parts.append(
            f'<rect class="bar" x="{bx:.1f}" y="{y(cum):.1f}" width="{bar_w:.1f}" '
            f'height="{max(y(prev) - y(cum), 0.0):.1f}" rx="2"><title>{_e(tip)}</title></rect>'
        )
        if len(rounds) <= 8:
            parts.append(
                f'<text class="val" x="{x:.1f}" y="{y(cum) - 6:.1f}" '
                f'text-anchor="middle">${cum:.2f}</text>'
            )
        if i % step == 0 or i == len(rounds) - 1:
            parts.append(
                f'<text class="tick" x="{x:.1f}" y="{h - 8}" text-anchor="middle">R{r["n"]}</text>'
            )
        prev = cum
    parts.append(
        f'<line class="cap" x1="{left}" x2="{w - right}" y1="{y(budget):.1f}" '
        f'y2="{y(budget):.1f}"/><text class="cap-l" x="{w - right - 4}" '
        f'y="{y(budget) - 6:.1f}" text-anchor="end">CAP ${budget:.2f}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _candidate_card(r: dict, c: dict, threshold: float) -> str:
    """One competitor: status, gates, the five dimensions, and why if it went unscored."""
    status = c["status"]
    kind = status if status in ("green", "red") else "dead"
    pill = status if kind != "dead" else f"dead — {status}"
    is_winner = c["id"] == r["winner"]
    round_scored = any(o["score"] is not None for o in r["cands"])
    classes = " ".join(["cand", kind] + (["win"] if is_winner else []))

    head = (
        f'<div class="cand-h"><span class="cid">c{c["id"]}</span>'
        f'<span class="angle">{_e(c["angle"])}</span>'
        f'<span class="pill {kind}">{_e(pill)}</span>'
    )
    if is_winner:
        label = "winner" if round_scored else "carried forward"
        head += f'<span class="win-badge" title="promoted onto trunk">{label}</span>'
    head += "</div>"

    facts = f"<span>gates <b>{_e(c['gates'])}</b></span><span>cost <b>${c['cost']:.4f}</b></span>"
    if c["commit"]:
        facts += f"<span>commit <b>{_e(c['commit'][:8])}</b></span>"

    crit = c["critique"]
    body = ""
    if c["score"] is not None:
        if crit:
            values = {d: _num(crit["scores"].get(d)) for d in RUBRIC}
            known = [v for v in values.values() if v is not None]
            low = min(known) if known else None
            rows = []
            for d, v in values.items():
                if v is None:
                    continue
                cls = "dim weak" if v == low else "dim ok" if v >= threshold else "dim"
                rows.append(
                    f'<div class="{cls}"><span>{d.replace("_", " ")}</span>'
                    f'<span class="track"><i style="width:{max(0.0, min(v, 10.0)) * 10:.0f}%">'
                    f"</i></span><b>{_fmt_score(v)}</b></div>"
                )
            body += f'<div class="dims">{"".join(rows)}</div>'
        else:
            body += (
                '<p class="why">critique.json missing or unreadable — dimensions unavailable</p>'
            )
        verdict = c["verdict"] if c["verdict"] in ("PASS", "REVISE") else None
        body += (
            f'<div class="score"><span class="lbl">min</span><span class="min">'
            f'{_fmt_score(c["score"])}<small>/10</small></span>'
            + (f'<span class="verdict {verdict}">{verdict}</span>' if verdict else "")
            + "</div>"
        )
    elif kind == "dead":
        body += f'<p class="why">{_e(status)} — never reached the gates, not reviewed</p>'
    elif kind == "red":
        body += '<p class="why">gates red — not reviewed</p>'
    elif not r["recorded"]:
        body += '<p class="why">gates green — the run stopped before its critique</p>'
    else:
        body += '<p class="why">gates green, but the critic failed twice — unscored</p>'

    if crit and (crit["summary"] or crit["must_fix"]):
        detail = f"<p>{_inline_md(crit['summary'])}</p>" if crit["summary"] else ""
        if crit["must_fix"]:
            items = "".join(f"<li>{_inline_md(i)}</li>" for i in crit["must_fix"])
            detail += f'<div class="mf">must fix · {len(crit["must_fix"])}</div><ul>{items}</ul>'
        else:
            detail += '<div class="mf">must fix · none</div>'
        body += f"<details><summary>critique</summary>{detail}</details>"

    return (
        f'<article class="{classes}" id="r{r["n"]}-c{c["id"]}" '
        f'data-winner="{"true" if is_winner else "false"}">'
        f'{head}<div class="facts">{facts}</div>{body}</article>'
    )


def render_html_report(run_dir: Path) -> str:
    """RUN_REPORT.html for a run directory, read entirely from what is on disk."""
    run_dir = Path(run_dir)
    state = _read_json(run_dir / "state.json")
    if state is None:
        raise ValueError(f"{run_dir / 'state.json'} is missing or is not a JSON object")
    try:
        idea = (run_dir / "IDEA.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        idea = ""

    # Caps as recorded by the run; module defaults only for runs older than 2.1.
    threshold = _num(state.get("pass_threshold")) or PASS_THRESHOLD
    max_rounds = state.get("max_rounds") or MAX_ROUNDS
    budget = _num(state.get("budget_usd")) or MAX_COST_USD
    stall = state.get("stall_rounds") or STALL_ROUNDS
    total_cost = _num(state.get("total_cost_usd")) or 0.0
    run_id = str(state.get("run_id") or run_dir.name)
    best_round = state.get("best_round") if type(state.get("best_round")) is int else None
    best_overall = _num(state.get("best_overall"))
    demo = bool(state.get("demo"))
    rounds = _load_rounds(run_dir, state)
    finished = isinstance(state.get("stop_reason"), str)
    # state.json's total only advances when a round completes; a cut-short
    # round's candidates still spent money, and selection.json recorded it.
    total_cost = max(total_cost, sum(r["cost"] for r in rounds))

    # ── hero ──
    reason = state.get("stop_reason")
    badge, tone, why = STOP_REASONS.get(reason if isinstance(reason, str) else "", STOP_UNKNOWN)
    why = why.format(threshold=_fmt_score(threshold), max_rounds=max_rounds, stall=stall,
                     budget=budget)  # fmt: skip
    eyebrow = "CRUCIBLE run"
    if demo:
        eyebrow += '<span class="sim">simulated · demo mode, no API calls</span>'
    meta = [f"<span>run <b>{_e(run_id)}</b></span>"]
    if state.get("started_at"):
        meta.append(f"<span>started <b>{_e(state['started_at'])}</b></span>")
    if state.get("version"):
        meta.append(f"<span>crucible <b>{_e(state['version'])}</b></span>")
    hero = (
        f'<header class="card hero"><div class="eyebrow">{eyebrow}</div>'
        f"<h1>{_inline_md(_idea_title(idea, run_id))}</h1>"
        f'<div class="meta">{"".join(meta)}</div>'
        f'<div class="stop"><span class="badge {tone}" title="stop reason">{_e(badge)}</span>'
        f"<p>{_e(why)}</p></div></header>"
    )

    # ── KPI tiles ──
    all_cands = [c for r in rounds for c in r["cands"]]
    green = sum(1 for c in all_cands if c["status"] == "green")
    completed = state.get("round") if type(state.get("round")) is int else len(rounds)
    per_round = state.get("candidates_per_round") or "—"
    if best_round is not None and best_overall is not None:
        best_tile = (
            f'<div class="kpi hot"><div class="l">best round</div><div class="n">R{best_round}'
            f'</div><div class="d">min score {_fmt_score(best_overall)}/10</div></div>'
        )
    else:
        best_tile = (
            '<div class="kpi"><div class="l">best round</div><div class="n">—</div>'
            '<div class="d">no critic-scored round</div></div>'
        )
    used = total_cost / budget if budget else 0.0
    kpis = (
        '<section class="kpis" aria-label="Key figures">'
        f'<div class="kpi"><div class="l">rounds</div><div class="n">{completed}'
        f'<small>/ {_e(max_rounds)}</small></div><div class="d">completed / cap</div></div>'
        f'<div class="kpi"><div class="l">candidates</div><div class="n">{_e(per_round)}</div>'
        f'<div class="d">forked per round</div></div>'
        f'<div class="kpi{" good" if green else ""}"><div class="l">went green</div>'
        f'<div class="n">{green}<small>/ {len(all_cands)}</small></div>'
        f'<div class="d">passed every gate</div></div>'
        f"{best_tile}"
        f'<div class="kpi"><div class="l">total cost</div><div class="n">${total_cost:.2f}'
        f'</div><div class="d">of ${budget:.2f} cap · {used:.0%}'
        f'{" · simulated" if demo else ""}</div>'
        f'<div class="meter{" over" if used > 1 else ""}" role="img" '
        f'aria-label="{used:.0%} of budget"><i style="width:{min(used, 1.0) * 100:.1f}%"></i>'
        "</div></div></section>"
    )

    # ── charts ──
    # Why nothing was scored, by cause — the chart and the tournament both say it.
    if not finished:
        empty_note = "Nothing has been scored yet — the run was interrupted or is still going."
    elif not green:
        empty_note = (
            "No candidate was ever scored: none passed the gates, so none reached the critic."
        )
    else:
        empty_note = "No candidate was ever scored: the critic failed on every green candidate."
    if rounds:
        charts = (
            '<section class="charts">'
            '<div class="card chart-card"><h2>Score progression</h2>'
            f"{_score_chart(rounds, threshold, best_round, empty_note)}"
            '<div class="legend"><span><i class="dotk"></i>candidate min score</span>'
            '<span><i class="win"></i>round winner</span>'
            f'<span><i class="thr"></i>pass threshold ({_fmt_score(threshold)})</span></div>'
            "</div>"
            '<div class="card chart-card"><h2>Cumulative cost</h2>'
            f"{_cost_chart(rounds, budget)}"
            '<div class="legend"><span><i class="win"></i>this round</span>'
            '<span><i class="prev"></i>earlier rounds</span>'
            f'<span><i class="cap"></i>budget cap</span></div></div></section>'
        )
    else:
        charts = (
            '<section class="card empty"><b>No round completed</b>'
            "The run stopped before its first round produced a record, so there is "
            "nothing to chart.</section>"
        )

    # ── the tournament, round by round ──
    sections = []
    for r in rounds:
        n_green = sum(1 for c in r["cands"] if c["status"] == "green")
        sub = (
            f"base <b>{_e(r['base'] or '—')}</b> · {len(r['cands'])} candidate(s) · "
            f"<b>{n_green}</b> green · ${r['cost']:.4f}"
        )
        if r["winner"] is not None:
            sub += f" · winner <b>c{r['winner']}</b>"
        fail = ""
        if not r["recorded"] and not finished:
            fail = '<span class="fail">interrupted — the run stopped during this round</span>'
        elif r["winner"] is None and r["cands"]:
            cause = {
                "critic_error": "the critic failed on every green candidate",
                "builder_error": "every builder failed twice",
            }.get(r["error"], "every candidate failed")
            fail = f'<span class="fail">no winner — {_e(cause)}</span>'
        cards = "".join(_candidate_card(r, c, threshold) for c in r["cands"])
        if not cards:
            cards = '<div class="card empty">No candidate record for this round.</div>'
        if r["recorded"] and r["cands"] and not any(c["score"] is not None for c in r["cands"]):
            fail += (
                '<span class="sub">nothing scored this round — '
                + ("only gates-green candidates reach the critic"
                   if not n_green else "the critic failed on every green candidate")
                + "</span>"
            )  # fmt: skip
        sections.append(
            f'<section class="round" id="round-{r["n"]}"><div class="round-h">'
            f'<h3>Round {r["n"]}</h3><span class="sub">{sub}</span>{fail}</div>'
            f'<div class="cands">{cards}</div></section>'
        )
    if not any(c["score"] is not None for c in all_cands) and rounds:
        sections.insert(0, f'<p class="why">{_e(empty_note)}</p>')
    tournament = (
        '<section aria-label="Tournament"><h2>Tournament</h2><div class="rounds">'
        + ("".join(sections) or '<div class="card empty">No rounds on record.</div>')
        + "</div></section>"
    )

    # ── the idea, and how to get the result ──
    tag_note = state.get("tag_note")
    best_tag = state.get("best_tag") if "best_tag" in state else best_round
    if not tag_note:
        if not finished:
            tag_note = (
                "This run did not finish, so `best` was never tagged. Each round that "
                "completed is tagged `round-N` in the workspace."
            )
        elif "version" not in state:  # written before 2.1 recorded tag notes
            tag_note = (
                f"`best` points at round {best_tag}." if best_tag is not None
                else "This run's state.json predates tag notes; see RUN_REPORT.md."
            )  # fmt: skip
        else:
            tag_note = "Nothing was tagged."
    ws = (run_dir / "workspace").resolve()
    ws_display = ws.relative_to(ROOT) if ws.is_relative_to(ROOT) else ws
    checkout = ""
    if type(best_tag) is int:
        pair = losing_branch(state.get("history"))
        diff = (
            f"\ngit diff round-{pair[0]} cand-r{pair[0]}-{pair[1]}"
            "   # what a losing attempt did differently"
            if pair and pair[1] is not None
            else ""
        )
        checkout = (
            f'<pre class="cmd">cd {_e(shlex.quote(str(ws_display)))}\n'
            "git checkout best                   # the highest-scoring round, not the last\n"
            "git branch --list 'cand-*'          # every candidate, winners and losers"
            f"{diff}</pre>"
        )
    getting = (
        '<section class="card section"><h2>Getting the best version</h2>'
        f"<p>{_inline_md(str(tag_note))}</p>{checkout}</section>"
    )
    idea_block = (
        '<section class="card section"><h2>The idea</h2>'
        f'<pre class="idea">{_e(idea.strip() or "(IDEA.md is missing)")}</pre></section>'
    )

    footer_bits = [f"<span>generated by crucible {_e(__version__)}</span>"]
    if state.get("version") and state["version"] != __version__:
        footer_bits.append(f"<span>run recorded by crucible {_e(state['version'])}</span>")
    if demo:
        footer_bits.append("<span>simulated run — demo/fake_claude.py, no API calls</span>")
    footer_bits.append("<span>good enough is a rule here, not a feeling.</span>")

    title = f"CRUCIBLE · {_idea_title(idea, run_id)[:60]}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="crucible {_e(__version__)}">
<title>{_e(title)}</title>
<script>{REPORT_THEME_BOOT}</script>
<style>{REPORT_CSS}</style>
</head>
<body>
<div class="wrap">
<nav class="top"><span class="brand">{REPORT_LOGO}CRUCIBLE</span><span class="spacer"></span>
<button class="toggle" id="themeToggle" type="button" hidden
 aria-label="Toggle light or dark theme">{REPORT_ICON_THEME}</button></nav>
{hero}
{kpis}
{charts}
{tournament}
{idea_block}
{getting}
<footer>{"".join(footer_bits)}</footer>
</div>
<script>{REPORT_THEME_TOGGLE}</script>
</body>
</html>
"""


def write_html_report(run_dir: Path) -> Path | None:
    """Write RUN_REPORT.html beside RUN_REPORT.md. Never raises.

    The report describes the run; it is not part of it. A rendering bug must
    cost the page, never the outcome — the state, tags and markdown report are
    already on disk by the time this runs, and `--report` can retry it later.
    """
    try:
        path = run_dir / "RUN_REPORT.html"
        path.write_text(render_html_report(run_dir), encoding="utf-8")
        return path
    except Exception as exc:
        log(f"  ! RUN_REPORT.html not written ({type(exc).__name__}: {exc})")
        return None


def report_command(run_dir: Path) -> int:
    """`--report RUN_DIR`: re-render a past run's HTML report. Needs no claude, no git."""
    if not (run_dir / "state.json").is_file():
        die(f"{run_dir} is not a CRUCIBLE run directory — it has no state.json")
    try:
        page = render_html_report(run_dir)
    except Exception as exc:
        die(f"could not render {run_dir}: {type(exc).__name__}: {exc}")
    out = run_dir / "RUN_REPORT.html"
    out.write_text(page, encoding="utf-8")
    print(out)
    return 0


# ─────────────────────────────── demo mode ─────────────────────────────────
#
# `--demo` runs the real controller — real worktrees, real pytest and ruff
# gates, real promotion and stop rules — against a scripted `claude` that tells
# a plausible three-round story offline. Only the model is simulated, so the
# demo shows exactly what a live run does, minus the API key and the bill.

DEMO_FAKE = ROOT / "demo" / "fake_claude.py"
DEMO_IDEA = (
    "CLI expense tracker: `add <amount> <category>`, `list`, `summary` (totals per "
    "category). JSON file storage. Python stdlib + pytest."
)


def enable_demo() -> None:
    """Point every builder and critic invocation at the demo's fake `claude`.

    The fake is launched as [python, demo/fake_claude.py] — an absolute argv,
    not a shim on PATH. A shim can be skipped (a noexec temp dir; Windows,
    where CreateProcess never runs a .cmd) and the lookup then lands on the
    real CLI while every label still says "simulated". An absolute argv cannot
    fall through to anything.
    """
    global BUILDER_ALLOWED_TOOLS, CLAUDE_CMD

    if not DEMO_FAKE.is_file():
        die(
            f"--demo needs {DEMO_FAKE}, which ships with the repository, not with the "
            "pip-installed module.\n  Run it from a clone: "
            "git clone https://github.com/charanreddy-27/CRUCIBLE && cd CRUCIBLE && "
            "python crucible.py --demo"
        )
    CLAUDE_CMD = [sys.executable, str(DEMO_FAKE)]

    # The fake ignores permissions entirely, so the scoped list costs nothing —
    # and it keeps preflight's root check from blocking the demo in a container.
    if not BUILDER_ALLOWED_TOOLS:
        BUILDER_ALLOWED_TOOLS = BUILDER_ALLOWED_TOOLS_SUGGESTION


# ──────────────────────────────── the loop ─────────────────────────────────


def main() -> int:
    # --budget and --max-rounds rebind these, so every reference — loop, stop
    # rules, report — agrees with what the caller actually asked for.
    global MAX_ROUNDS, MAX_COST_USD

    parser = argparse.ArgumentParser(
        prog="crucible", description="Build → critique → rebuild loop agent."
    )
    parser.add_argument(
        "idea", nargs="?",
        help="path to an IDEA.md file, or the idea itself as a one-line string",
    )  # fmt: skip
    parser.add_argument(
        "-n", "--candidates", type=int, default=CANDIDATES,
        help=f"builders forked per round (default {CANDIDATES}; 1 = sequential v1 loop)",
    )  # fmt: skip
    parser.add_argument(
        "--budget", type=float, default=MAX_COST_USD, metavar="USD",
        help=f"cumulative spend cap (default {MAX_COST_USD:.2f})",
    )  # fmt: skip
    parser.add_argument(
        "--max-rounds", type=int, default=MAX_ROUNDS, metavar="N",
        help=f"round cap (default {MAX_ROUNDS})",
    )  # fmt: skip
    parser.add_argument(
        "--demo", action="store_true",
        help="run a whole tournament offline against a simulated claude: no API key, no cost. "
             "The simulated builders always build the same expense tracker, so --demo takes "
             "no idea (needs the repository checkout)",
    )  # fmt: skip
    parser.add_argument(
        "--report", metavar="RUN_DIR", type=Path,
        help="re-render RUN_REPORT.html for an existing run directory, then exit",
    )  # fmt: skip
    parser.add_argument("--version", action="version", version=f"crucible {__version__}")
    args = parser.parse_args()

    # --report reads files and writes one; it needs no claude, git or gate tools,
    # so it returns before preflight can demand them.
    if args.report is not None:
        if args.idea is not None or args.demo:
            parser.error("--report re-renders an existing run; it takes no idea and no --demo")
        return report_command(args.report)
    if args.demo and args.idea is not None:
        # The fake builds one scripted project whatever it is asked; accepting an
        # idea would title the report with something the workspace doesn't hold.
        parser.error("--demo always builds the scripted expense tracker; drop the idea")
    if args.idea is None and not args.demo:
        parser.error("an idea is required: a path to IDEA.md or the idea as a string "
                     "(or --demo to watch a simulated run, or --report RUN_DIR)")  # fmt: skip

    if args.candidates < 1:
        die("--candidates must be at least 1")
    if args.max_rounds < 1:
        die("--max-rounds must be at least 1")
    if args.budget <= 0:
        die("--budget must be greater than 0")

    # Rebind the caps so every reference — loop, stop rules, report — agrees.
    MAX_ROUNDS, MAX_COST_USD = args.max_rounds, args.budget

    # Idea: a readable file path, else literal inline text. Checked before
    # preflight so a bad input is reported as such on any machine.
    if args.idea is None:
        idea = DEMO_IDEA
    else:
        idea_path = Path(args.idea)
        try:
            is_file = idea_path.is_file()
        except OSError:  # e.g. inline text longer than the filename limit
            is_file = False
        idea = idea_path.read_text(encoding="utf-8") if is_file else args.idea
    if not idea.strip():
        die("the idea is empty")

    if args.demo:
        enable_demo()
    preflight()

    started = dt.datetime.now()
    stamp = started.strftime("%Y-%m-%d_%H%M")
    run_id = f"{stamp}_{'demo_' if args.demo else ''}{slugify(idea)}"
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
    if args.demo:
        log(paint("  demo: simulated builders and critic (demo/fake_claude.py) — "
                  "no API calls, no cost; the gates are real", "accent"))  # fmt: skip
    log(
        f"  caps: {MAX_ROUNDS} rounds x {args.candidates} candidates, "
        f"${MAX_COST_USD:.2f}, pass at {PASS_THRESHOLD}/10\n"
    )

    state = {
        "run_id": run_id, "round": 0, "candidates_per_round": args.candidates,
        "best_round": None, "best_overall": None, "total_cost_usd": 0.0,
        "history": [], "stop_reason": None,
        # Recorded so RUN_REPORT.html re-renders faithfully from disk even after
        # the module defaults change, and says plainly when a run was simulated.
        "version": __version__, "demo": args.demo,
        "max_rounds": MAX_ROUNDS, "budget_usd": MAX_COST_USD,
        "pass_threshold": PASS_THRESHOLD, "stall_rounds": STALL_ROUNDS,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": None,
        "best_tag": None, "tag_note": None,
    }  # fmt: skip
    write_state(run_dir, state)

    feedback, source = "", ""
    best_overall, best_round, stall_count = -1, None, 0
    stop_reason: str | None = None

    for n in range(1, MAX_ROUNDS + 1):
        log(paint(f"── round {n}/{MAX_ROUNDS} ", "accent", "bold") + paint("─" * 44, "accent"))
        base = git(["rev-parse", "HEAD"], workspace).stdout.strip()[:8]

        winner, cands, error = run_round(
            n, run_dir, workspace, idea, feedback, source, args.candidates
        )
        round_cost = sum(c.cost for c in cands)
        state["total_cost_usd"] += round_cost

        if error or winner is None:
            log(paint(f"  every candidate failed ({error}) — terminating, best-so-far preserved",
                      "red"))  # fmt: skip
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
            + paint(f"winner {winner.label}", "accent", "bold")
            + (paint(f" at min {winner.overall}/10", score_style(winner.overall))
               if winner.overall is not None else "")
            + f", ${round_cost:.4f}"
        )  # fmt: skip

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

    # The HTML report reads state.json rather than these locals, so it can
    # re-render the run faithfully later — record what the markdown report knows.
    state["best_tag"] = tag_target
    state["tag_note"] = tag_note
    state["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_state(run_dir, state)
    write_report(
        run_dir, state, idea, tag_note, tag_target is not None, args.candidates
    )

    reason = state["stop_reason"]
    tone = "green" if reason == "pass" else "red" if reason.endswith("_error") else "yellow"
    log("")
    log(paint("── done ", "accent", "bold") + paint("─" * 52, "accent"))
    log(f"  stop reason: {paint(reason, tone, 'bold')}")
    log(f"  best: {paint('round ' + str(tag_target), 'bold') if tag_target else 'none'}")
    log(
        f"  cost: ${state['total_cost_usd']:.4f}"
        + (paint(" (simulated — demo mode, no API calls)", "accent") if args.demo else "")
    )
    log(f"  report: {run_dir / 'RUN_REPORT.md'}")
    if (run_dir / "RUN_REPORT.html").is_file():
        log(f"  html:   {run_dir / 'RUN_REPORT.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
