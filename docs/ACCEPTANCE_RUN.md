# Live acceptance run

The run from [`CRUCIBLE_SPEC.md`](../CRUCIBLE_SPEC.md) §10 Phase 4, executed
against the real `claude` CLI. Everything below is recorded output, not
reconstruction.

```bash
python crucible.py "CLI expense tracker: \`add <amount> <category>\`, \`list\`, \
\`summary\` (totals per category). JSON file storage. Python stdlib + pytest." \
  -n 1 --budget 3.00 --max-rounds 3
```

Bounded to a single candidate and $3.00 deliberately: every `claude` invocation
carries roughly $0.20 of cache creation before it does any work, so this is the
cheapest configuration that still exercises the whole loop.

## Result

| Round | Gates | Min score | Verdict | `must_fix` | Cost |
|---|---|---|---|---|---|
| 1 | pass (2/2) | **3**/10 | REVISE | 13 | $1.6042 |
| 2 | pass (2/2) | **6**/10 | REVISE | 9 | $1.5162 |

```
stop reason : budget      ($3.1204 exceeded the $3.00 cap)
best        : round 2     (min score 6)
total cost  : $3.1204
```

Every §10 acceptance criterion holds:

- terminated with a valid `stop_reason` inside the round cap
- every round directory contains `builder.json`, `critic.json`, `critique.json`,
  `gates.txt`, plus `selection.json` per round
- `state.json` parsed at every step
- `git tag` shows `round-1`, `round-2`, `best` — and `best` resolves to the same
  commit as `round-2`, the highest-scoring round
- the `RUN_REPORT.md` table matches the recorded history
- no worktrees leaked; only the main workspace remained registered

## What the critic caught

Round 1 passed **both gates** — `pytest` green, `ruff` clean — and the critic
still returned 13 blocking defects and scored it 3/10. This is the entire thesis
of the project, on real model-written code:

> `expense_tracker.py:42` — the `if amount <= 0` guard does not reject NaN or
> Infinity (all NaN comparisons are False, and `inf <= 0` is False), so
> `add nan food` / `add inf rent` are accepted.

> `expense_tracker.py:37-38` — `save_expenses()` truncates the file with
> `open(path,'w')` before writing, so an interrupt, crash, or full disk mid-dump
> destroys every previously recorded expense.

> `expense_tracker.py:38` — `json.dump()` uses the default `allow_nan=True`,
> which writes the non-standard tokens NaN/Infinity and produces a file no
> strict JSON parser will read.

> `expense_tracker.py:115` — `return 1` is unreachable dead code because
> `add_subparsers(required=True)` makes argparse `exit(2)` before the if-chain
> falls through.

> `tests/test_expense_tracker.py:120` — `assert 'Error' in capsys.readouterr().err`
> would pass on any error text; assert the specific message.

Note the last one. The critic reviewed the *tests* as adversarially as the code
— which is exactly what `test_quality` being its own scored dimension is for.

## What the next round did about it

| Finding | Round 2 |
|---|---|
| NaN/inf bypass the `<= 0` guard | `math.isfinite(amount)` check |
| `allow_nan=True` emits invalid JSON | `json.dump(..., allow_nan=False)` |
| Truncating write destroys data on crash | `tempfile.mkstemp` + `os.replace`, atomic, with cleanup |
| Missing parent directory crashes | `os.makedirs(directory, exist_ok=True)` |
| No handling for corrupt JSON | `except json.JSONDecodeError` → domain error |
| Edge cases untested | **14 → 26 tests** |

Score moved 3 → 6. The critic then found *new*, deeper problems in the revision
— NaN literals re-entering through `json.loads()` on the read path, and float
rounding losing sub-cent amounts — which is what a real review loop looks like.
It did not run out of things to say, and it did not start agreeing with itself.

## The delivered artifact

`git checkout best`, then:

```
$ python -m pytest -q
40 passed

$ ruff check .
All checks passed!

$ expense_tracker.py add 40 food
Added: food: 40.00
$ expense_tracker.py add 12.50 food
Added: food: 12.50
$ expense_tracker.py add 30 transport
Added: transport: 30.00

$ expense_tracker.py summary
food: 52.50
transport: 30.00
TOTAL: 82.50

$ expense_tracker.py add nan food
Error: amount must be a finite number          (exit 1)
$ expense_tracker.py add -5 food
Error: amount must be greater than 0           (exit 1)
```

That satisfies the spec's definition of done, and the edge cases handled
correctly in the final artifact are precisely the ones the round-1 critic
flagged.

## What this proves, and what it doesn't

**Proved.** The full loop works end to end against the real CLI: builder
invocation, envelope parsing, cost accounting, deterministic gates, the
schema-enforced critic, `min()` scoring, feedback threading into the next
round, best-round tagging, the budget stop rule, and the run report. The
adversarial rubric produces specific, actionable, genuinely correct findings on
code that passes its gates — and the next round acts on them.

**Not proved.** A multi-candidate tournament with *real* builders. Candidate
selection, promotion and carry-forward are covered by the end-to-end tests
against the fake CLI (which controls each candidate's quality precisely, so the
right winner can actually be asserted), but not yet by a live `-n 3` run. The
reason is cost, not doubt: three candidates plus their critics is roughly 3×
this run's spend per round.

**Also worth knowing.** The run stopped on `budget`, not `pass`. Reaching 8/10
on every dimension is a high bar — by design — and two rounds at $1.50 each
weren't enough. That's the system working as specified, not falling short.
