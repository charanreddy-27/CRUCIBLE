# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

You'll also need the [`claude` CLI](https://code.claude.com/docs/en/headless)
on PATH to run the tool itself. **The test suite doesn't need it** — it uses a
fake CLI, which is the point.

## Before you push

```bash
ruff check .
python -m pytest tests/ -q
```

CI runs both on Python 3.11–3.13.

## Testing philosophy

The interesting logic decides which of several *model outputs* wins. That's
untestable against a real model, so `tests/fake_claude.py` impersonates the
headless CLI: it speaks the same JSON envelope, works out which candidate and
round it is from its own working directory, and hands out deliberately graded
quality.

The end-to-end tests then run **real** loops — real subprocesses, real git
worktrees, real pytest and ruff — with only the model faked. If you change the
tournament, promotion, or stop rules, add a scenario to the fake rather than
weakening an assertion.

Two rules worth keeping:

- **Never assert only that "something was promoted."** Assert the *correct*
  candidate was, by comparing commit hashes. A bug that promoted the wrong
  candidate would otherwise still look green.
- **Gates are ground truth.** If you touch `run_gates`, be paranoid. The stale
  bytecode cache bug (see `CHANGELOG.md`) shipped because the component
  everything trusts was the one component nobody had tested.

## Style

- Ruff with `E,F,I,UP,B,SIM`, 100 columns.
- Comments explain *why*, not *what*. If a line needs a "what" comment, rename
  something instead.
- The controller stays stdlib-only. Its dependencies are the gate tools it runs
  against other people's code, not libraries it imports.

## Scope

Deliberately out of scope, and not oversights:

- **Resumable runs.** `state.json` makes a crashed run inspectable, not
  restartable.
- **A web dashboard.** The run report and the git history are the interface.
- **Pointing at an existing repo.** Builders run with permissions skipped; that
  is only safe because the workspace is disposable.

Open an issue before building any of those — they change the safety model.
