# CRUCIBLE — Build → Critique → Rebuild Loop Agent

> One idea in, working repo out. Claude Code (headless) builds the project, deterministic gates and a fresh-context critic score it, a plain-Python controller decides whether to iterate or stop. "Satisfied" is a set of rules, never the model's feeling.

**Handoff note (for the implementing Claude Code session):** Build exactly what is specified here, in the phase order in §10. Do not add a web UI, parallel candidates, or resumable runs — those are explicitly out of scope (§11). The deliverable is a single-file controller plus prompt/schema assets.

> **Status:** this document is the v1 design record and is kept as written.
> Best-of-N parallel candidates — §11's "strong v2 idea" — has since been built,
> so the parallel-candidates non-goal no longer holds. The web UI and resumable
> runs remain out of scope. See [`README.md`](README.md) for the tournament
> round, which supersedes §4.2 and §5 where they differ.

---

## 1. How it works

```
IDEA.md
   │
   ▼
┌─────────────────────────── round N ───────────────────────────┐
│                                                                │
│  BUILDER  ── claude -p (fresh session, full tools, cwd=repo)   │
│     │                                                          │
│     ▼                                                          │
│  GATES ──── controller runs pytest + ruff (deterministic)      │
│     │                                                          │
│     ├── FAIL ──► feedback = failure output ─► next round       │
│     │            (critic is SKIPPED — no point reviewing       │
│     │             broken code; saves an opus call)             │
│     ▼                                                          │
│  CRITIC ─── claude -p (fresh session, READ-ONLY tools,         │
│             --json-schema → structured verdict)                │
│     │                                                          │
│     ▼                                                          │
│  CONTROLLER (plain Python) applies stop rules                  │
│     ├── PASS / stall / round cap / budget cap ──► STOP,        │
│     │        tag best version, write RUN_REPORT.md             │
│     └── otherwise: feedback = must_fix ─► next round           │
└────────────────────────────────────────────────────────────────┘
```

Core invariants:

1. **Fresh sessions every round.** No `--resume`. The repo on disk *is* the state; the prompt carries the idea + latest feedback. This prevents context rot and keeps every round independently debuggable.
2. **The controller is code, not an LLM.** All stop decisions are rule-based.
3. **Critic never shares context with the builder.** Separate invocation, read-only tools, adversarial rubric.
4. **Best ≠ last.** Every round is a git commit + tag; the run returns the highest-scoring round, not the final one.

---

## 2. Layout

```
crucible/
├── crucible.py               # the controller — the only thing Charan runs
├── prompts/
│   ├── builder_init.md       # round 1 template
│   ├── builder_revise.md     # round ≥2 template
│   └── critic.md             # rubric + instructions
├── schemas/
│   └── critique.schema.json  # enforced via --json-schema
└── runs/
    └── 2026-07-12_1430_expense-tracker/
        ├── IDEA.md                    # copied in at run start
        ├── workspace/                 # the repo Claude Code builds (git repo)
        ├── rounds/
        │   ├── round_1/
        │   │   ├── builder.json       # raw claude -p JSON envelope
        │   │   ├── gates.txt          # pytest/ruff output
        │   │   └── critique.json      # structured_output from critic
        │   └── round_2/ ...
        ├── state.json                 # loop state, updated every round
        └── RUN_REPORT.md              # written at termination
```

Loop metadata lives **outside** `workspace/` so the builder never sees or edits it.

---

## 3. Config (top of `crucible.py`)

```python
MAX_ROUNDS       = 5            # hard cap, non-negotiable
PASS_THRESHOLD   = 8            # every rubric dimension must be ≥ this
STALL_ROUNDS     = 2            # consecutive critic rounds with no best-score gain
MAX_COST_USD     = 5.00         # cumulative, summed from total_cost_usd
BUILDER_MODEL    = "sonnet"
CRITIC_MODEL     = "opus"       # different, stronger model = less self-flattery
BUILDER_MAX_TURNS = 80
CRITIC_MAX_TURNS  = 40
BUILDER_TIMEOUT_S = 1800        # subprocess timeout per builder round
GATES = [
    ("tests", ["python", "-m", "pytest", "-q", "--timeout=60"]),
    ("lint",  ["ruff", "check", "."]),
]
```

---

## 4. The controller (`crucible.py`)

### 4.1 CLI

```
python crucible.py IDEA.md            # start a run from an idea file
python crucible.py "one-line idea"    # or inline text
```

Creates `runs/<timestamp>_<slug>/`, copies/writes `IDEA.md`, `git init`s `workspace/`, then enters the loop.

### 4.2 Round lifecycle

For each round `n` in `1..MAX_ROUNDS`:

1. **Build.** Render the prompt (init template for n=1, revise template otherwise, with feedback injected inline — feedback is small JSON/text, never piped files). Invoke the builder (§5). On `is_error: true` or non-zero exit: retry once; if it fails again, terminate with `stop_reason: builder_error`, best-so-far preserved.
2. **Safety commit.** The builder is instructed to commit, but the controller enforces it: `git add -A && git commit -m "round {n}" --allow-empty`, then `git tag round-{n}`.
3. **Gates.** Run each command in `GATES` inside `workspace/` via `subprocess` with timeout. Capture combined output to `rounds/round_{n}/gates.txt`. **Any gate fails → skip the critic**, set feedback to the last ~120 lines of failure output, record the round with `gates: fail`, continue to next round. Gate-fail rounds count toward `MAX_ROUNDS` but not toward stall detection (stall is measured on critic scores only).
4. **Critique.** Gates green → invoke the critic (§7). Parse `structured_output`. `overall = min(scores.values())` — a chain is as strong as its weakest dimension.
5. **Track best.** If `overall > best_overall`: update `best_overall`, `best_round`.
6. **Stop rules** — first match wins:

| # | Rule | Condition | `stop_reason` |
|---|------|-----------|----------------|
| 1 | Pass | `verdict == "PASS"` and every score ≥ `PASS_THRESHOLD` and `must_fix` empty and gates green | `pass` |
| 2 | Round cap | `n == MAX_ROUNDS` | `max_rounds` |
| 3 | Stall | best_overall unimproved for `STALL_ROUNDS` consecutive critic rounds | `stalled` |
| 4 | Budget | Σ `total_cost_usd` (builder + critic) > `MAX_COST_USD` | `budget` |

7. **Continue.** Feedback for the next round = the critique's `must_fix` list + `summary`.

### 4.3 Termination

- `git tag -f best round-{best_round}` (falls back to the last gates-green round; if no round ever passed gates, tag the final round and say so in the report).
- Write `RUN_REPORT.md`: idea, stop reason, per-round table (round, gates, min score, verdict, cost), total cost, `git checkout best` instructions.
- `state.json` is rewritten after every round: `{run_id, round, best_round, best_overall, total_cost_usd, history: [...], stop_reason}` — so a crashed run is inspectable.

---

## 5. Builder invocation

Fresh session per round, from Python (no shell quoting issues):

```python
subprocess.run(
    ["claude", "-p", prompt,
     "--output-format", "json",
     "--model", BUILDER_MODEL,
     "--max-turns", str(BUILDER_MAX_TURNS),
     "--dangerously-skip-permissions"],
    cwd=workspace, capture_output=True, text=True, timeout=BUILDER_TIMEOUT_S,
)
```

The JSON envelope on stdout includes `result`, `session_id`, `total_cost_usd`, `is_error`, `num_turns` — persist it verbatim to `rounds/round_{n}/builder.json` and add `total_cost_usd` to the running total.

Notes:
- `--dangerously-skip-permissions` is acceptable **only because** `cwd` is a throwaway `workspace/` created by the run. Never point CRUCIBLE at an existing repo. A stricter alternative is an `--allowedTools` whitelist (scoped `Bash(...)` patterns, `Edit`, `Write`, `Read`) — leave this as a commented-out config option.
- `--max-turns` overflow exits non-zero: treat as a builder error (retry-once rule above).

### 5.1 `prompts/builder_init.md` (round 1)

```
You are building a small, complete, working project from scratch in the
current directory (already a git repo).

## The idea
{IDEA}

## Hard requirements
1. Working code, no placeholders, no TODOs, no stubbed functions.
2. pytest tests that assert real behavior (not just "imports work").
   Target the core logic paths. Trivial or vacuous tests are a failure.
3. requirements.txt (prefer stdlib; keep dependencies minimal).
4. README.md: what it is, how to install, how to run, how to test.
5. `ruff check .` must pass.
6. Keep scope tight: the smallest system that fully satisfies the idea.
7. When done: `git add -A && git commit -m "round 1: initial build"`.

Do not ask questions — there is no one to answer. Make reasonable
decisions and record them in the README.
```

### 5.2 `prompts/builder_revise.md` (round ≥ 2)

```
You are revising the project in the current directory. It was reviewed
and must be improved.

## The original idea
{IDEA}

## Feedback to address (from {SOURCE: "failing gates" | "code review"})
{FEEDBACK}

## Rules
1. Address EVERY item above. Do not skip or defer any.
2. Do not refactor unrelated code. Minimal, targeted changes.
3. If behavior intentionally changes, update the tests to match — but
   never weaken or delete tests just to make them pass.
4. All hard requirements from the initial build still apply.
5. When done: `git add -A && git commit -m "round {N}: address review"`.
```

---

## 6. Objective gates

Run by the **controller**, never by an LLM. Defaults: `pytest -q --timeout=60` and `ruff check .` (config list in §3, easy to extend with a build/smoke command per project type). Deterministic pass/fail; output captured for feedback.

Gaming risk: a builder can satisfy gates with vacuous tests. That is why `test_quality` is a scored critic dimension (§7) — hollow tests block PASS even when pytest is green.

---

## 7. Critic invocation

Fresh session, read-only tools, structured output enforced natively:

```python
subprocess.run(
    ["claude", "-p", critic_prompt,
     "--output-format", "json",
     "--json-schema", "schemas/critique.schema.json",
     "--model", CRITIC_MODEL,
     "--max-turns", str(CRITIC_MAX_TURNS),
     "--allowedTools", "Read,Glob,Grep,Bash(git diff *),Bash(git log *)"],
    cwd=workspace, capture_output=True, text=True, timeout=900,
)
```

Parse the `structured_output` field of the JSON envelope — `--json-schema` guarantees it conforms, so no regex/parse-retry machinery is needed. If the invocation itself errors, retry once, then terminate with `stop_reason: critic_error` (best-so-far preserved).

### 7.1 `schemas/critique.schema.json`

```json
{
  "type": "object",
  "properties": {
    "scores": {
      "type": "object",
      "properties": {
        "correctness":  {"type": "integer", "minimum": 1, "maximum": 10},
        "completeness": {"type": "integer", "minimum": 1, "maximum": 10},
        "robustness":   {"type": "integer", "minimum": 1, "maximum": 10},
        "code_quality": {"type": "integer", "minimum": 1, "maximum": 10},
        "test_quality": {"type": "integer", "minimum": 1, "maximum": 10}
      },
      "required": ["correctness", "completeness", "robustness",
                   "code_quality", "test_quality"]
    },
    "must_fix": {"type": "array", "items": {"type": "string"}},
    "summary":  {"type": "string"},
    "verdict":  {"type": "string", "enum": ["PASS", "REVISE"]}
  },
  "required": ["scores", "must_fix", "summary", "verdict"]
}
```

### 7.2 `prompts/critic.md`

```
You are a harsh senior engineer reviewing a project you did not write.
Your reputation depends on catching problems, not on being agreeable.

## What was asked for
{IDEA}

## Test & lint results (already verified green)
{GATES_SUMMARY}

## Your job
Read the code with your tools. Then score each dimension 1-10:
- correctness: does it actually do what the idea asked, on edge cases too?
- completeness: is every part of the idea implemented, or were pieces
  quietly dropped?
- robustness: error handling, bad input, empty state, boundary conditions.
- code_quality: structure, naming, readability, no dead code.
- test_quality: do the tests assert real behavior? Vacuous, trivial, or
  tautological tests cap this at 4.

Calibration: 10 means you would ship it to production unchanged. A typical
competent first draft scores 4-6. Do not inflate.

must_fix: every issue blocking a PASS, as concrete, file-scoped, actionable
items (e.g. "tracker.py: add() accepts negative amounts — validate and
raise ValueError"). Vague items like "improve error handling" are
forbidden.

verdict: PASS only if every score ≥ {PASS_THRESHOLD} AND must_fix is
empty. Otherwise REVISE. Start by listing the 3 most serious flaws you
found — if you cannot find 3, justify why in summary.
```

---

## 8. `IDEA.md` template (what Charan writes)

```
# Idea
One or two paragraphs: what the thing is and who/what it's for.

# Must have
- Bullet list of non-negotiable behavior.

# Constraints
- Language/stack (default: Python 3.11, stdlib-first).
- Anything explicitly out of scope.

# Definition of done
- e.g. "I can run `expense add 40 food` and `expense summary` shows totals
  per category from a JSON file."
```

The sharper "Definition of done" is, the better the critic's correctness scoring anchors.

---

## 9. Failure handling

| Failure | Handling |
|---|---|
| Builder `is_error` / non-zero / timeout | Retry once → terminate `builder_error`, keep best |
| Critic invocation error | Retry once → terminate `critic_error`, keep best |
| Gates command missing (no ruff etc.) | Terminate immediately with a clear install message — never silently skip a gate |
| Builder forgot to commit | Controller safety-commit (§4.2 step 2) covers it |
| Empty workspace after round 1 | Terminate `builder_error` |
| Cost overrun mid-round | Round completes, then rule 4 fires |

Everything is logged per round; nothing is only in memory.

---

## 10. Implementation phases

**Phase 1 — happy path.** Scaffold layout, config, CLI parsing, run dir creation, one builder round end-to-end: prompt render → invoke → parse envelope → safety commit + tag → state.json.

**Phase 2 — gates + revise loop.** Gate runner with timeout/capture, gate-fail fast path, revise-template rounds, round cap.

**Phase 3 — critic + stop rules.** Schema file, critic invocation with `--json-schema`, min-score logic, stall + budget rules, best tagging, RUN_REPORT.md.

**Phase 4 — polish + verification.** Timeouts everywhere, retry-once rules, tidy logging. Then the acceptance run:

```
python crucible.py "CLI expense tracker: `add <amount> <category>`,
`list`, `summary` (totals per category). JSON file storage. Python
stdlib + pytest."
```

Acceptance criteria: run terminates within 5 rounds with a valid `stop_reason`; every round dir contains its artifacts; `state.json` parses at every step; `git tag` shows `round-*` + `best`; RUN_REPORT.md table matches reality; total cost reported.

---

## 11. Non-goals (v1)

- No web UI, no dashboard (Forge territory, later).
- No parallel candidates / best-of-N sampling (strong v2 idea).
- No resumable/crashed-run recovery — `state.json` makes runs inspectable, not restartable.
- No pointing at existing repos. Fresh `workspace/` only.

## 12. References

- Headless mode (flags, JSON envelope, `--json-schema` → `structured_output`): https://code.claude.com/docs/en/headless
- CLI reference: https://docs.claude.com/en/docs/claude-code/cli-reference
