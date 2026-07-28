# CRUCIBLE — deep dive

How it's put together, why it's put together that way, and where the bodies are
buried. If you only read one section, make it [The four decisions that shaped
everything](#the-four-decisions-that-shaped-everything).

---

## 1. The shape of the problem

An LLM writing code is fast and frequently wrong. The naive fix — loop it,
asking for improvements — fails for three specific reasons:

1. **Self-review doesn't work.** A model critiquing output it just produced is
   grading in the context that produced it. It gets agreeable. Ask twice and it
   starts defending earlier choices rather than re-examining them.
2. **Context rots.** A long session accumulates its own dead ends. By round four
   the model is reasoning about a version of the code that no longer exists.
3. **Nobody knows when to stop.** "Is this good enough?" asked of the thing that
   wrote it is not a question, it's a formality.

CRUCIBLE's answer is to remove all three judgments from the model and put them
somewhere accountable — two of them in deterministic tooling, one in a stranger.

## 2. Architecture

```
crucible.py  ── the controller. Plain Python, stdlib only, no model calls of
                its own. Owns the loop, the git operations, the gates, the
                stop rules, and every artifact written to disk.
   │
   ├── prompts/builder_init.md     round 1 template
   ├── prompts/builder_revise.md   round >= 2 template
   ├── prompts/critic.md           rubric + calibration
   └── schemas/critique.schema.json  enforced by the CLI, not by us
```

A run is a directory. Nothing lives only in memory:

```
runs/2026-07-12_1430_expense-tracker/
├── IDEA.md                       # copied in at run start
├── workspace/                    # the delivered git repo; trunk = promoted winners
├── candidates/round_N/cand_K/    # worktrees, pruned after each round
├── rounds/round_N/
│   ├── cand_K/builder.json       # raw claude -p envelope, verbatim
│   ├── cand_K/gates.txt          # exact pytest/ruff output
│   ├── cand_K/critique.json      # structured_output from the critic
│   └── selection.json            # who ran, who won
├── state.json                    # rewritten after every round
└── RUN_REPORT.md                 # written at termination
```

**Loop metadata lives outside every worktree.** Builders see a plain git repo
containing their project and nothing else — no scores, no rubric, no knowledge
that they're in a competition. A builder that could read the critic's rubric
would optimise against it, and a builder that could read `state.json` would know
it was losing.

## 3. Data flow through one round

```
                        ┌──────────────────────────────────┐
   IDEA.md ────────────►│  render prompt (init | revise)   │
   feedback from N-1 ──►│  + per-candidate emphasis        │
                        └────────────────┬─────────────────┘
                                         │  N copies, one per candidate
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
     git worktree add            git worktree add           git worktree add
     cand-rN-1 @ base            cand-rN-2 @ base           cand-rN-3 @ base
              │                          │                          │
     claude -p (sonnet)          claude -p (sonnet)         claude -p (sonnet)
     full tools, fresh           full tools, fresh          full tools, fresh
              │                          │                          │
     safety commit               safety commit              safety commit
              │                          │                          │
     pytest + ruff               pytest + ruff              pytest + ruff
              │                          │                          │
              └──────────┬───────────────┴──────────┬───────────────┘
                         │ gates green only         │ gates red: skipped
                         ▼                          ✕
                claude -p (opus)
                read-only tools, --json-schema
                         │
                         ▼
              min(scores) per candidate
                         │
                         ▼
              winner = max(min_score), ties to lower id
                         │
                         ▼
              git merge --ff-only <winner>   ── trunk
              git tag round-N
                         │
                         ▼
              stop rules (pass | max_rounds | stalled | budget)
```

Threads run the builder and critic phases; everything else is sequential.

## 4. The four decisions that shaped everything

### 4.1 Promotion is fast-forward-only, so conflicts cannot exist

Three candidates edit the same files and one has to win. The obvious design —
merge the winner — invites conflict resolution into the hot path of an
unattended loop, which is exactly where you don't want a human decision.

Instead: every candidate branches from trunk's current commit, and **trunk does
not move during the round**. The winner is therefore always a descendant of
trunk, and `git merge --ff-only` always succeeds. Not "usually". Always.

```python
base = git(["rev-parse", "HEAD"], workspace).stdout.strip()
# ... every candidate: git worktree add -b cand-rN-K <path> base
# ... later:
ff = git(["merge", "--ff-only", winner.branch], workspace, check=False)
if ff.returncode != 0:                      # only if a builder rewrote history
    git(["reset", "--hard", winner.branch], workspace)
```

The fallback exists because a builder has shell access and could rebase or reset
its own branch. `reset --hard` reaches the same end state — trunk becomes exactly
the winner — without pretending the ancestry was clean.

A test asserts trunk's history stays a straight line, because a bug that
silently merged every candidate would still produce green gates and a plausible
report.

### 4.2 Parallel builds, serialized git

Worktrees share one object store and one set of refs. Separate indexes mean
concurrent `git add` is safe, but concurrent ref updates are not reliably so. The
resolution is unglamorous:

```python
GIT_LOCK = threading.Lock()

def git(args, cwd, check=True):
    with GIT_LOCK:
        proc = subprocess.run(["git", *args], cwd=cwd, ...)
```

The lock covers controller-side git only — milliseconds. The builds, which are
minutes, stay fully parallel. Builders run their own git commands inside their
own worktrees without taking the lock; that's tolerable because each writes to
its own branch and its own index.

### 4.3 A round scores the minimum, not the mean

```python
overall = min(critique["scores"].values())
```

Five dimensions: correctness, completeness, robustness, code_quality,
test_quality. Averaging would let a beautiful implementation with vacuous tests
score 8/10 — and vacuous tests were the failure mode I was most worried about,
because they're the cheapest way to satisfy the gates dishonestly.

Taking the minimum means every dimension is a veto. It makes PASS harder to
reach and the score harder to game.

### 4.4 Diversity is the product; sampling is the mechanism

The first tournament ran N identical prompts and picked between N nearly
identical answers. Technically best-of-N. Practically a waste of money.

```python
CANDIDATE_ANGLES = [
    "",                                  # one candidate always reads it straight
    "Bias toward the simplest design ...",
    "Bias toward robustness ...",
    "Bias toward structure and testability ...",
    "Bias toward completeness ...",
]
```

Candidate #1 stays neutral deliberately — it's the control, and it keeps `-n 1`
behaving exactly like the original sequential loop. The others decorrelate the
sample so the tournament has something real to choose between.

## 5. The anti-gaming problem

Any objective gate is a target. Tell a system to make `pytest` pass and
`assert True` satisfies the letter of the requirement perfectly.

Two defenses, deliberately layered:

1. **`test_quality` is a scored dimension**, with the critic instructed to cap it
   at 4 for tests that are "vacuous, trivial, or tautological". Combined with
   min-scoring, hollow tests make PASS unreachable no matter how good the rest is.
2. **The critic is a stranger.** Separate invocation, read-only tools
   (`Read,Glob,Grep,Bash(git diff *),Bash(git log *)`), a stronger model than the
   builder, and an adversarial prompt that demands three flaws before it's
   allowed to be happy. It cannot have been party to the shortcut.

Green gates are necessary. They were never sufficient.

## 6. The bug that justified the whole design

During v1 verification a round failed with an assertion whose source read
`assert add(2, 3) == 5` while the error reported `assert 5 == 6`. The source and
the failure disagreed.

pytest rewrites assertions and caches the rewritten bytecode, keyed on the source
file's **mtime and size**. A builder that rewrites a same-size file inside one
second produces a cache key collision — and pytest grades stale bytecode.

The fix is one line:

```python
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
```

The lesson is bigger than the fix. Gates are the ground truth this entire
architecture rests on; I had verified everything *except* the thing I trusted
most. A gate that lies is worse than no gate, because the whole system is built
to believe it.

## 7. Failure handling

| Failure | Behavior |
|---|---|
| One builder errors twice | Candidate dropped, round continues on survivors |
| **Every** builder errors | Terminate `builder_error`, best-so-far preserved |
| One critic errors twice | Candidate unscored — it simply can't win |
| Every critic errors | Terminate `critic_error` |
| No candidate passes gates | Least-broken (most gates passed) carried forward; failure output becomes next round's brief |
| Gate tool missing | Refuse to start, with an install message. Never silently skip |
| Builder forgot to commit | Controller safety-commits with `--allow-empty` |
| Candidate produced nothing | Dropped as dead |
| Cost overrun mid-round | Round completes, then the budget rule fires |

Fault tolerance was a side effect, not a goal. In v1 a single builder error
killed the entire run; with N>1 it costs you one candidate.

### The dangling-tag bug

A run that died inside the builder never reached its safety commit, so
`round-1` never existed — but termination still ran `git tag -f best round-1`
with `check=False`, which failed silently, and `RUN_REPORT.md` cheerfully told
the reader to `git checkout best`. Termination now verifies the ref before
claiming it, and says plainly when there's nothing to check out. A report that
lies about its own artifacts is worse than no report.

## 8. Stop rules

First match wins, evaluated after every round:

| # | Rule | Condition |
|---|---|---|
| 1 | `pass` | verdict PASS, every score ≥ threshold, `must_fix` empty, gates green |
| 2 | `max_rounds` | `n == MAX_ROUNDS` |
| 3 | `stalled` | best score unimproved for `STALL_ROUNDS` consecutive critic rounds |
| 4 | `budget` | cumulative `total_cost_usd` over cap |

Stall is measured on critic scores only — gate-fail rounds count toward the round
cap but not toward stalling, because a round that never got reviewed hasn't
demonstrated anything about convergence.

`best_overall` uses strict `>`, so a tie doesn't move `best`. The earliest round
to reach a score keeps the tag, which is the honest reading: if round 4 didn't
beat round 3, round 3 is still the achievement.

## 9. Testing a non-deterministic system

The interesting logic — winner selection, promotion, carry-forward, stop rules —
operates on model output. You cannot assert "candidate 3 wins" against a real
model, because you cannot make candidate 3 better on demand.

So the suite ships [`tests/fake_claude.py`](../tests/fake_claude.py): a fake CLI
that speaks the real headless JSON envelope, recovers its identity from its own
working directory (`…/candidates/round_N/cand_K`), and produces deliberately
graded quality per candidate.

```python
base = 9 if rnd >= 2 else {1: 5, 2: 7, 3: 9}.get(cid, 6)
```

The end-to-end tests then run **real** loops — real subprocesses, real worktrees,
real pytest and ruff — with only the model faked. That's what makes assertions
like this possible:

```python
assert git_out(ws, "rev-parse", "trunk") == git_out(ws, "rev-parse", "cand-r1-3")
```

Not "a winner was promoted" but "the *correct* winner was promoted, and trunk is
byte-identical to its commit."

Scenarios covered: `tourney`, `allred`, `onedies`, `alldie`, `slowpass` — normal
tournament, total gate failure, partial builder failure, total builder failure,
and late convergence (which proves `best` pins the *first* round to reach the top
score, not the last).

### When the fake lied

A fake is only as good as the contract it enforces, and mine had a hole.

`--json-schema` takes the schema **inline**. I was passing a filesystem path.
Against the real CLI that fails instantly —
`Error: --json-schema is not valid JSON: Unrecognized token '/'` — which means
every critic call in every real run would have failed. The suite was green the
whole time, because the fake only checked that the flag was *present* and never
looked at its value.

Two things came out of that:

1. The fake now parses the schema argument as JSON and exits non-zero if it
   can't, exactly like the real CLI. A test additionally asserts the invocation
   inlines the schema rather than passing a path.
2. A rule I'd now apply to any test double: **make it enforce the contract, not
   merely impersonate the interface.** A fake that accepts more than the real
   thing converts a loud production failure into a silent green build — strictly
   worse than having no test at all, because it manufactures confidence.

Running it for real also surfaced that the CLI waits 3 seconds for stdin on
every invocation unless you redirect it. Multiplied by candidates and rounds,
that was pure dead time; the controller now passes `stdin=DEVNULL`.

## 10. Tradeoffs I'd defend

**Single file.** 830 lines is at the edge of comfortable, but the controller is
one cohesive thing: a loop and its rules. Splitting it into `gates.py`,
`tournament.py`, `state.py` would add import ceremony without reducing the
concepts you must hold in your head.

**Threads, not asyncio.** Every unit of work is `subprocess.run`. Threads block
on the OS, which is exactly right. asyncio would mean rewriting every call site
for no measurable gain.

**No resumability.** `state.json` makes a crashed run *inspectable*, not
restartable. Resumption means reconciling on-disk git state with recorded state
and deciding what a half-finished round means — real complexity for a rare case.

**Cost scales with N.** Three candidates burn roughly 3× the builder cost per
round plus one critic call per green candidate. That's the honest price of a
tournament; the budget rule is the backstop, and it needs raising before running
wide.

## 11. What I'd change next

- **Adaptive N** — sample wide on round 1 where variance is highest, then narrow.
- **A critic ensemble** for close calls, since one opinion decides a whole round.
- **Cross-pollination** — feed the winner the losers' best ideas instead of discarding them.
- **A hard per-call budget.** The CLI exposes `--max-budget-usd`, which would turn the cost cap from a check that happens *after* a round into a limit that cannot be exceeded. The cross-cutting part is that candidates run in parallel, so "remaining budget" needs a lock to be meaningful.
- **A live multi-candidate tournament.** The full loop is now verified end to end against the real CLI — see [ACCEPTANCE_RUN.md](ACCEPTANCE_RUN.md) — but at `-n 1`. Candidate selection, promotion and carry-forward are covered by the end-to-end tests against the fake (which can control each candidate's quality precisely, so the right winner is actually assertable), not yet by a live `-n 3` run. Cost is the reason: roughly 3× this run's spend per round.
