# CRUCIBLE — interview prep

Rehearsal material. Read it out loud; the timings assume you're talking, not
reading.

---

## The 30-second pitch

> CRUCIBLE turns one idea into a working repo. It runs Claude Code in a loop —
> but the model doesn't get to decide anything important. Several builders race
> the same problem in isolated git worktrees, tests and lint judge whether the
> code actually runs, a separate reviewer that never saw the build scores it,
> and plain Python decides when to stop. The interesting bit is what I removed:
> "is this good enough?" is a rule, not the model's opinion.

Practice until it's 30 seconds. It's your answer to "so what did you build?"

---

## The 2-minute walkthrough

**Open with the problem — it's more interesting than the solution.**

> Everyone's tried looping an LLM on its own code. It doesn't work, for a
> specific reason: when a model critiques output it just produced, it's grading
> in the same context that produced it. It gets agreeable. Ask it twice and it
> starts defending its earlier choices instead of re-examining them.
>
> So I was doing the loop by hand — run it, paste the traceback back in, squint
> at the diff, decide it was probably fine. And I noticed my "probably fine"
> correlated mostly with how tired I was. The model wasn't the bottleneck. My
> judgment was.

**Then the design principle.**

> The design question wasn't "how do I loop an LLM." It was: which decisions
> must the model never make? There turned out to be three. Whether the code
> runs — that's pytest and ruff, deterministic, no opinion involved. Whether
> it's any good — that needs a reviewer with no memory of writing it, so it's a
> separate session with read-only tools and a stronger model. And whether to
> stop — that's arithmetic, and it lives in Python where I can read it.

**Then the mechanism.**

> Every round is a tournament. N builders fork from the same commit into
> separate git worktrees and run in parallel, each with a different emphasis —
> one goes for simplicity, one for robustness, one just reads the spec straight.
> They all get gated. Only the ones that pass reach the critic. The winner
> fast-forwards onto trunk and the losers stay as branches, so you can go look
> at what the rejected approach did.
>
> Scoring is the minimum across five dimensions, not the average — because
> averaging lets beautiful code with hollow tests coast to a pass, and hollow
> tests were exactly the failure I was worried about.

**Close with the honest bit.** This is what separates you from someone
describing a tutorial project.

> The part I'm proudest of is the testing. The interesting logic decides which
> of several *model outputs* wins — you can't test that against a real model,
> because you can't make candidate 3 better on demand. So I wrote a fake Claude
> CLI that speaks the same JSON envelope and hands each candidate deliberately
> different quality. The tests run real loops — real worktrees, real pytest —
> with only the model faked. So I can assert that trunk is byte-identical to the
> commit of the candidate that *should* have won. Not "a winner was promoted."
> The right one.

---

## STAR stories

### Story 1 — The gates were lying

**Situation.** The entire architecture rests on one claim: the gates are
deterministic ground truth. Everything else — critic, scoring, stop rules —
trusts them.

**Task.** During verification a round failed with an assertion whose source read
`assert add(2, 3) == 5` while the error reported `assert 5 == 6`. The source and
the failure disagreed, which should be impossible.

**Action.** I traced it to pytest's assertion-rewrite cache, which keys on the
source file's mtime and size. A builder rewriting a same-size file within one
second collides on that key, so pytest runs stale bytecode. I disabled bytecode
caching for gate subprocesses — one line — and then went back through every other
component I'd called "deterministic" to check what else I'd assumed rather than
verified.

**Result.** The gates now report on the working tree, always. The wider outcome
was a rule I now apply everywhere: the component you trust most is the one you
should test hardest, because everything downstream is built to believe it.

> **Why this story works:** it's a subtle bug in someone else's tool, found by
> noticing an inconsistency rather than a crash. It shows you read error output
> instead of skimming it.

### Story 2 — Making merge conflicts impossible

**Situation.** Moving from one builder per round to N meant several candidates
editing the same files, with one winner to promote.

**Task.** Merging the winner into trunk invites conflict resolution into the hot
path of an unattended loop — precisely where you can't have a human decision, and
where an automated resolution would silently corrupt the result.

**Action.** Rather than solving conflicts, I removed the possibility. Every
candidate branches from trunk's current commit and trunk doesn't move during the
round, so the winner is always a descendant and `git merge --ff-only` always
succeeds. I kept a `reset --hard` fallback for the case where a builder rewrites
its own history — it has shell access, after all. Then I wrote a test asserting
trunk's history stays a straight line, because a bug that silently merged
everyone would still produce green gates and a plausible report.

**Result.** Zero conflict-handling code, and a failure mode that can't occur
rather than one that's handled. The test matters as much as the design: it
catches the regression where someone "helpfully" changes `--ff-only` to a plain
merge.

### Story 3 — The test suite that lied to me

**Situation.** I'd built a fake CLI so I could test orchestration logic that
operates on model output. Twenty-four tests, all green, including end-to-end runs
with real git worktrees.

**Task.** Before claiming it worked, I wanted one real invocation against the
actual CLI — specifically to confirm the structured-output contract, the one
thing a fake can't prove.

**Action.** It failed immediately:
`--json-schema is not valid JSON: Unrecognized token '/'`. The flag takes the
schema **inline**; I was passing a filesystem path. Every critic call in every
real run would have failed — and my suite was green, because the fake only
checked the flag was *present* and never looked at its value. I fixed the
invocation, then fixed the deeper problem: the fake now parses that argument as
JSON and exits non-zero if it can't, exactly like the real thing. I verified the
guard by reintroducing the bug and confirming the suite goes red.

**Result.** The critic contract is verified end-to-end against the real CLI now.
The lasting lesson is about test doubles: **make them enforce the contract, not
just impersonate the interface.** A fake that accepts more than the real thing
turns a loud production failure into a silent green build — which is worse than
having no test, because it manufactures confidence.

> **Why this story works:** it's a bug *you* found in *your own* verification,
> and the fix is a principle rather than a patch. It also demonstrates you don't
> confuse "tests pass" with "it works" — which is exactly the instinct the whole
> project is about.

### Story 4 — Best-of-N that actually bought something

**Situation.** The first tournament implementation ran N identical prompts in
parallel and picked a winner.

**Task.** It barely helped. Independent samples of the same prompt cluster around
the same solution, so I was paying 3× to choose between three nearly identical
answers.

**Action.** I gave each candidate a different emphasis — simplicity, robustness,
structure, completeness — while keeping candidate #1 reading the idea straight as
a control. That decorrelates the samples, so the tournament has something real to
choose between. It also keeps `-n 1` behaving exactly like the original
sequential loop, which meant the whole feature was backward-compatible by
construction.

**Result.** The tournament started producing genuinely different approaches to
compare. A side effect I didn't design for: fault tolerance. In the old loop a
single builder error killed the run; now it costs one candidate and the round
continues on the survivors.

---

## Likely technical questions

**"Why not just use a bigger model, or more turns?"**
> Neither fixes self-review. A bigger model is still grading in the context that
> produced the work, and more turns means more context rot. The problem isn't
> capability, it's who's holding the red pen. That's why the critic is a separate
> session with no memory of the build — and a stronger model than the builder, so
> it isn't marking its own homework.

**"How do you stop the model gaming the tests?"**
> You can't stop it, so you make it insufficient. `assert True` satisfies pytest
> perfectly. So `test_quality` is its own scored dimension, the critic is told to
> cap it at 4 for vacuous or tautological tests, and the round scores the
> *minimum* across dimensions — which makes any single hollow dimension a veto.
> Green gates are necessary, never sufficient.

**"Why worktrees instead of copying the directory, or containers?"**
> Worktrees give real filesystem isolation while sharing one object store, so
> forking N candidates is cheap and — more usefully — every candidate ends up as
> a real branch in the same repo. The losing attempts stay inspectable and
> diffable. A copied directory gives you isolation but throws away the history;
> containers give you stronger isolation than this threat model needs, since the
> workspace is already disposable.

**"How is it concurrent, and is that safe?"**
> ThreadPoolExecutor, because every unit of work is `subprocess.run` — it blocks
> on the OS, so threads are exactly right and asyncio would be a rewrite for no
> gain. Worktrees keep separate indexes, so concurrent `git add` is fine, but
> refs and the object store are shared — so controller-side git takes a lock.
> The lock covers milliseconds; the builds, which are minutes, stay parallel.

**"What happens when things fail?"**
> Layered. One builder failing twice drops that candidate and the round continues
> — that fault tolerance came free with N>1. Every builder failing terminates
> with `builder_error` and preserves best-so-far. If no candidate passes the
> gates, the least-broken one carries forward so the next round revises the most
> promising line. A missing gate tool refuses to start entirely, because a
> silently skipped gate is a lie about the result.

**"Why minimum instead of average?"**
> Averaging is forgiving in exactly the wrong place. A build with excellent
> structure and hollow tests averages well, and hollow tests were the cheapest
> way to defeat the whole system. Minimum makes every dimension a veto. It makes
> PASS harder to reach, which is the point.

**"How do you know any of this works?"**
> Twenty-six tests, and the end-to-end ones run real loops — real worktrees, real
> gates — with only the model faked. Separately I verified the critic contract
> against the real CLI: envelope shape, `--json-schema` producing conforming
> `structured_output`, read-only tool scoping. That's also how I found a bug the
> green test suite was hiding, which is a story in itself.
>
> The honest boundary: a complete multi-round tournament driven by real builders
> is still untried, and the reason is cost — every invocation carries about
> twenty cents of cache creation before it does any work.

> **Note:** stating that boundary is a strength, not a weakness. Interviewers
> notice people who know where their evidence stops. Don't dress it up.

**"What did the real critic actually say?"**
> Good question to invite, because the answer sells the design. I pointed it at a
> deliberately weak module — a `divide` function with one test,
> `assert divide(6, 3) == 2`. It scored test_quality 2 out of 10 and explained
> that the assertion is satisfied by `return 2`, by integer division, and by
> `return 6 / b` — so it cannot distinguish a correct implementation from three
> wrong ones. That's the anti-gaming rubric doing precisely what it was built
> for, on the first real run.

**"What does it cost to run?"**
> It scales with N: roughly 3× the builder cost per round at three candidates,
> plus one critic call per green candidate. There's a hard cumulative cap that
> terminates the run, and red-gates rounds skip the critic entirely — no point
> paying a strong model to review code that fails its own tests.

**"Isn't `--dangerously-skip-permissions` reckless?"**
> It would be, pointed anywhere else. Each builder's working directory is a
> throwaway worktree created by the run, inside a directory the run owns. The
> README says never point it at an existing repo, and there's a commented
> `--allowedTools` whitelist for anyone who wants the stricter posture. The
> tradeoff is deliberate and documented, not accidental.

---

## What I'd improve next

- **Adaptive N** — sample wide on round 1 where variance is highest, narrow as it converges. Same budget, better allocation.
- **A critic ensemble** for close calls. One opinion currently decides a whole round, and that's the thinnest part of the design.
- **Cross-pollination** — the losing candidates often solve *part* of the problem better. Right now that work is discarded; feeding it to the next round's builders is the obvious next lever.
- **The full live acceptance run.** The critic contract is verified against the real CLI and the orchestration against the fake; a complete multi-round tournament with real builders is the remaining gap, and cost is why.
- **A hard per-call budget** using the CLI's `--max-budget-usd`, which would make the cost cap a limit rather than an after-the-fact check.
- **Resumability**, which I deliberately skipped — `state.json` makes a crashed run inspectable but not restartable. Reconciling on-disk git state with recorded state is real complexity for a rare case, and I'd rather say that than pretend it was an oversight.

---

## Questions to ask them

Pick two or three; don't run the list.

- How do you currently decide a piece of AI-generated code is good enough to merge? Is that a person, a checklist, or a gate?
- Where has an LLM feature made it into production here — and what broke first?
- When something looks great in a notebook and falls apart in production, what's usually the cause in your stack?
- What does the review loop look like for the AI-assisted code your team writes now?
- Six months in, what would make you glad you hired me — a shipped system, or a change in how the team works?
- What's the thing you're all quietly working around that nobody's had time to fix?
