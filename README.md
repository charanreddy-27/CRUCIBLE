# CRUCIBLE

Build → critique → rebuild loop agent. One idea in, working repo out.

Claude Code (headless) builds the project, deterministic gates and a
fresh-context critic score it, and a plain-Python controller decides whether to
iterate or stop. "Satisfied" is a set of rules, never the model's feeling.

Every round is a **tournament**: N builders fork from the same commit into
isolated git worktrees, build in parallel under different emphases, and only
the winner is promoted. Losing attempts are kept as branches.

[`CRUCIBLE_SPEC.md`](CRUCIBLE_SPEC.md) is the v1 design record; best-of-N was
added after it and is documented here.

## Install

```bash
pip install -r requirements.txt          # pytest, pytest-timeout, ruff
```

CRUCIBLE itself is stdlib-only — those dependencies are the *gates* it runs
against the code it builds. You also need the `claude` CLI on PATH
([headless mode docs](https://code.claude.com/docs/en/headless)).

## Run

```bash
python crucible.py IDEA.md            # from an idea file
python crucible.py "one-line idea"    # or inline text
python crucible.py IDEA.md -n 5       # 5 candidates per round
python crucible.py IDEA.md -n 1       # sequential: one builder per round
```

`crucible.py` refuses to start if any gate tool, prompt template, or the critic
schema is missing — a gate is never silently skipped.

## What a run produces

```
runs/2026-07-12_1430_expense-tracker/
├── IDEA.md                       # the idea, copied in at run start
├── workspace/                    # the git repo, trunk = promoted winners
├── candidates/round_N/cand_K/    # per-candidate worktrees (pruned after each round)
├── rounds/round_N/
│   ├── cand_K/                   # builder.json, gates.txt, critique.json
│   └── selection.json            # who ran, who won, and why
├── state.json                    # rewritten every round — a crashed run is inspectable
└── RUN_REPORT.md                 # stop reason, round table, candidate detail, cost
```

Loop metadata lives outside the worktrees, so builders never see or edit it.

To get the result:

```bash
cd runs/<run-id>/workspace && git checkout best
```

`best` is the **highest-scoring** round, not the last one. Every promoted round
is tagged `round-1` … `round-N`, and every candidate that ever built — winners
and losers — survives as a branch `cand-r<round>-<id>`:

```bash
git branch --list 'cand-*'          # the whole tournament history
git diff round-2 cand-r2-1          # what the losing attempt did differently
```

## The round

1. **Fork.** N candidates branch from trunk's current commit into separate git
   worktrees. Shared object store, separate working trees — no collisions.
2. **Build, in parallel.** Each candidate gets its own fresh `claude -p`
   session (full tools) with a different *emphasis* appended to the prompt.
3. **Gate.** `pytest` and `ruff` run per candidate, by the controller, never by
   a model.
4. **Critique the survivors.** Only gates-green candidates reach the critic
   (fresh session, read-only tools, `--json-schema` structured verdict) — no
   point reviewing broken code, and it saves opus calls.
5. **Promote.** The winner fast-forwards onto trunk and gets tagged. Losers stay
   as branches.

A candidate's score is `min(scores.values())` across the five rubric dimensions:
correctness, completeness, robustness, code_quality, test_quality. A chain is as
strong as its weakest link. The winner is the highest such score; ties go to the
lower candidate id.

If **no** candidate passes the gates, the least-broken one (most gates passed)
is carried forward and its failure output becomes the next round's feedback.
If a single builder dies, the round continues on its survivors — only a round
where *every* candidate dies terminates the run.

### Why different emphases

Independent samples of one prompt cluster around one solution, so plain
best-of-N buys surprisingly little. Each candidate instead gets a distinct
angle — simplicity, robustness, structure, completeness — with candidate #1
always reading the idea straight. Decorrelated attempts give the tournament
something real to choose between. See `CANDIDATE_ANGLES` in `crucible.py`.

## Stopping

Rule-based, first match wins: `pass` (every dimension ≥ threshold, empty
`must_fix`, green gates) → `max_rounds` → `stalled` → `budget`. If every
candidate's builder or critic fails twice, the run ends `builder_error` /
`critic_error` with best-so-far preserved.

## Config

Top of `crucible.py`. Defaults: 5 rounds × 3 candidates, pass at 8/10, stall
after 2 flat critic rounds, $5.00 budget, `sonnet` builds and `opus` critiques
(a different, stronger model means less self-flattery).

**Budget scales with N.** Three candidates burn roughly 3× the builder cost per
round, plus one critic call per green candidate. The $5.00 cap is unchanged from
v1, so raise `MAX_COST_USD` before running wide, or the budget rule will stop
the run early.

## Safety

Builders run with `--dangerously-skip-permissions`. That is acceptable **only
because** each one's cwd is a throwaway candidate worktree created by the run.
Never point CRUCIBLE at an existing repo. A stricter `--allowedTools` whitelist
is left commented out in the config block.
