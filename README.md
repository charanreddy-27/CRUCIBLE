# CRUCIBLE

Build → critique → rebuild loop agent. One idea in, working repo out.

Claude Code (headless) builds the project, deterministic gates and a
fresh-context critic score it, and a plain-Python controller decides whether to
iterate or stop. "Satisfied" is a set of rules, never the model's feeling.

Full design: [`CRUCIBLE_SPEC.md`](CRUCIBLE_SPEC.md).

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
```

`crucible.py` refuses to start if any gate tool, prompt template, or the critic
schema is missing — a gate is never silently skipped.

## What a run produces

```
runs/2026-07-12_1430_expense-tracker/
├── IDEA.md          # the idea, copied in at run start
├── workspace/       # the git repo Claude Code built
├── rounds/round_N/  # builder.json, gates.txt, critique.json per round
├── state.json       # rewritten every round — a crashed run is still inspectable
└── RUN_REPORT.md    # stop reason, per-round table, total cost
```

Loop metadata lives outside `workspace/`, so the builder never sees or edits it.

To get the result:

```bash
cd runs/<run-id>/workspace && git checkout best
```

`best` is the **highest-scoring** round, not the last one. Every round is also
tagged `round-1` … `round-N`.

## The loop

Each round: build (fresh `claude -p` session, full tools) → gates (`pytest`,
`ruff`, run by the controller) → critic (fresh session, read-only tools,
`--json-schema` structured verdict) → stop rules.

Red gates skip the critic — no point reviewing broken code, and it saves an
opus call. The failure output becomes the next round's feedback.

A round's score is `min(scores.values())` across the five rubric dimensions:
correctness, completeness, robustness, code_quality, test_quality. A chain is
as strong as its weakest link.

Stopping is rule-based, first match wins: `pass` (every dimension ≥ threshold,
empty `must_fix`, green gates) → `max_rounds` → `stalled` → `budget`.
Builder or critic failing twice terminates with `builder_error` / `critic_error`,
best-so-far preserved.

## Config

Top of `crucible.py`. Defaults: 5 rounds, pass at 8/10, stall after 2 flat
critic rounds, $5.00 budget, `sonnet` builds and `opus` critiques (a different,
stronger model means less self-flattery).

## Safety

The builder runs with `--dangerously-skip-permissions`. That is acceptable
**only because** its cwd is a throwaway `workspace/` created by the run.
Never point CRUCIBLE at an existing repo. A stricter `--allowedTools` whitelist
is left commented out in the config block.
