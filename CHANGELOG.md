# Changelog

Notable changes to CRUCIBLE. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.1] — 2026-07

Found by running the real CLI for the first time instead of only the fake.

### Fixed
- **The critic could never have worked.** `--json-schema` takes the schema
  *inline*; it was being passed a filesystem path, which the real CLI rejects
  with `not valid JSON: Unrecognized token '/'`. Every critic call in every real
  run would have failed. The test suite was green throughout, because the fake
  CLI only checked the flag was present and never parsed its value.
- **The fake now enforces the contract** rather than impersonating the
  interface: it parses the schema argument as JSON and exits non-zero if it
  can't, exactly like the real CLI. A test additionally asserts the invocation
  inlines the schema, and the guard was verified by reintroducing the bug.
- **Every invocation stalled 3 seconds** waiting for stdin. The controller now
  passes `stdin=DEVNULL` — dead time that was multiplied by candidates and rounds.
- **Aborted rounds leaked worktrees.** A round where every candidate died
  returned before cleanup, leaving worktrees on disk and registered with git,
  and never writing `selection.json` — losing the forensics for precisely the
  round you most want them. Both now run in a `finally`.
- **Site linked to `blob/main/`** on a repo that has no `main` branch yet;
  switched to `blob/HEAD/`, which resolves to whatever the default branch is.

### Added
- `BUILDER_ALLOWED_TOOLS` (and `CRUCIBLE_BUILDER_ALLOWED_TOOLS`) promotes the
  previously commented-out scoped-permissions option into a working one. The
  CLI refuses `--dangerously-skip-permissions` under root/sudo, so containers
  and CI need it; preflight now detects root and explains the fix instead of
  letting every builder invocation fail with an unparseable envelope.

### Verified
- `--max-turns` overflow behaves as the spec assumed: exit 1, `is_error: true`,
  `subtype: error_max_turns`. Worth pinning down because the flag is accepted
  but undocumented in `claude -p --help`.
- The critic contract now runs against the real CLI end to end: envelope keys,
  `--json-schema` producing conforming `structured_output`, and read-only tool
  scoping. On its first real invocation the rubric scored a tautological test
  2/10 and explained that `assert divide(6, 3) == 2` is satisfied by `return 2`
  — the anti-gaming design working as intended.

## [2.0.0] — 2026-07

Every round became a tournament.

### Added
- **Best-of-N candidates.** N builders fork from trunk's current commit into
  isolated git worktrees, build in parallel, and compete. The winner
  fast-forwards onto trunk; losers persist as `cand-r<round>-<id>` branches.
- **Decorrelating emphases** (`CANDIDATE_ANGLES`) so candidates explore
  genuinely different approaches. Candidate #1 always reads the idea straight.
- `-n` / `--candidates` to set the field size per run. `-n 1` is the v1 loop.
- `--version`.
- Test suite — 24 tests, including end-to-end runs against a fake `claude` CLI
  (`tests/fake_claude.py`) that makes winner selection assertable.
- GitHub Actions CI: ruff + pytest on Python 3.11, 3.12 and 3.13.
- `pyproject.toml`, `LICENSE` (MIT), and a static site under `site/`.
- `selection.json` per round, and candidate detail tables in `RUN_REPORT.md`.
- `CRUCIBLE_RUNS_DIR` env override, so tests run real loops outside the repo.

### Changed
- Fault tolerance: a builder failing twice now drops one candidate instead of
  ending the run. Only a round where every candidate dies terminates.
- Rounds where no candidate passes the gates carry the least-broken attempt
  forward rather than an arbitrary one.
- `workspace/` starts on an explicit `trunk` branch with a base commit, since
  worktrees must fork from a commit.
- Controller-side git calls are serialized behind a lock; builds stay parallel.

## [1.0.0] — 2026-07

First implementation of [`CRUCIBLE_SPEC.md`](CRUCIBLE_SPEC.md): sequential
build → gate → critique → decide loop.

### Added
- Single-file controller: fresh `claude -p` session per round, `pytest`/`ruff`
  gates, read-only critic with `--json-schema` structured verdict.
- Rule-based stopping: `pass` / `max_rounds` / `stalled` / `budget`, plus
  retry-once on builder and critic errors.
- Per-round git tags with `best` pointing at the highest-scoring round.
- `state.json` rewritten every round; `RUN_REPORT.md` at termination.
- Preflight that refuses to start on a missing gate tool, prompt or schema.

### Fixed
- **Gates could report on stale bytecode.** pytest keys its assertion-rewrite
  cache on (mtime, size), so a builder rewriting a same-size file within one
  second got graded on the previous version. Gates now run with
  `PYTHONDONTWRITEBYTECODE=1`.
- **`best` could point at a ref that didn't exist.** A run dying inside the
  builder never reached its safety commit, but termination tagged `best` anyway
  and the report told the reader to check it out. Termination now verifies the
  ref and says plainly when there's nothing to see.
- **The critic's schema path was relative** to a working directory that has no
  `schemas/` dir. Now absolute.
- **Gate runs polluted the delivered repo.** `__pycache__/`, `.pytest_cache/`
  and `.ruff_cache/` were swept into the next round's commit; `workspace/` now
  seeds a `.gitignore`.
