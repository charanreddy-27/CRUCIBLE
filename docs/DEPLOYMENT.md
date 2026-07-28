# Deployment

Two separate things live in this repo:

1. **The site** (`site/`) — a static page set. This is what deploys to Vercel.
2. **The tool** (`crucible.py`) — a local CLI that shells out to `claude` and
   spawns git worktrees. **This does not and cannot run on Vercel**, and isn't
   meant to. Serverless functions have no persistent filesystem, no git, and a
   runtime ceiling measured in seconds; a builder round can take half an hour.

If you only want to ship the landing page, everything below applies. If you
want to run the tool, see [Running the tool](#running-the-tool).

---

## Deploy the site to Vercel

### Option A — dashboard (2 minutes)

1. Go to [vercel.com/new](https://vercel.com/new) and import
   `charanreddy-27/CRUCIBLE`.
2. Set these, exactly:

   | Setting | Value |
   |---|---|
   | Framework Preset | **Other** |
   | Root Directory | **`site`** |
   | Build Command | *(leave empty)* |
   | Output Directory | *(leave empty)* |
   | Install Command | *(leave empty)* |

   It's plain HTML and CSS — there's nothing to build. If Vercel auto-detects a
   framework, override it to **Other**, or the deploy will fail looking for a
   build script that doesn't exist.
3. Deploy.

`site/vercel.json` handles the rest: `cleanUrls` serves `about.html` at
`/about`, plus security headers and a one-year immutable cache on static assets.

### Option B — CLI

```bash
npm i -g vercel
cd site
vercel            # preview deploy
vercel --prod     # production
```

### No environment variables are needed

The site is fully static. There is no API, no database, no secret. If you later
wire the contact form to a real endpoint (see below), that's the only thing that
would introduce one.

---

## Custom domain

Assuming you want `crucible.charanreddy.dev` — the URL already baked into the
canonical tags and OG metadata:

1. Vercel project → **Settings → Domains → Add** → `crucible.charanreddy.dev`.
2. At your DNS provider for `charanreddy.dev`, add the record Vercel shows you:

   | Type | Name | Value |
   |---|---|---|
   | CNAME | `crucible` | `cname.vercel-dns.com` |

3. Wait for propagation; Vercel issues the TLS certificate automatically.

**If you use a different hostname, update these** — they're absolute URLs and
will otherwise point at a domain you don't control:

- `<link rel="canonical">` in all three pages under `site/`
- `og:url` and `og:image` in all three pages
- `twitter:image` in all three pages

```bash
# from the repo root, after picking your real host:
grep -rl "crucible.charanreddy.dev" site/ | xargs sed -i 's|crucible\.charanreddy\.dev|YOUR-HOST|g'
```

---

## The contact form

`site/about.html` composes a `mailto:` draft in the visitor's mail client. No
backend, nothing stored, and — importantly — it doesn't pretend to submit
somewhere it doesn't.

To make it a real form, sign up for [Formspree](https://formspree.io) or
[Web3Forms](https://web3forms.com), then in `site/about.html` replace the
`<script>` block at the bottom with a plain form action:

```html
<form id="contact-form" action="https://formspree.io/f/YOUR_ID" method="POST">
```

...and delete the JavaScript handler. Keep the `name` attributes as they are.

---

## Screenshots and recordings

The README currently uses `site/og.png` as its hero image. Replace it with a
recording of a real run — that's the single highest-value asset on this page,
because the tournament is much more convincing in motion than in prose.

```bash
# terminal recording -> GIF
brew install asciinema agg          # or: apt install asciinema
asciinema rec crucible-run.cast
python crucible.py "CLI expense tracker: add, list, summary. JSON storage."
# ctrl-D to stop
agg crucible-run.cast docs/crucible-run.gif
```

Then swap the image line near the top of `README.md`:

```markdown
![CRUCIBLE run](docs/crucible-run.gif)
```

Good moments to make sure are on camera: three candidates building in parallel,
one going red, the critic scores landing at different values, and the winner
being promoted.

---

## Running the tool

Locally, not on Vercel.

```bash
git clone https://github.com/charanreddy-27/CRUCIBLE.git
cd CRUCIBLE
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

You also need the [`claude` CLI](https://code.claude.com/docs/en/headless)
authenticated and on PATH. CRUCIBLE preflights every dependency and refuses to
start if one is missing.

```bash
python crucible.py "your idea here"
```

Optional editable install, which puts a `crucible` command on your PATH:

```bash
pip install -e .
crucible --version
```

> Editable install is the supported path. A normal `pip install .` would copy
> `crucible.py` into site-packages while leaving `prompts/` and `schemas/`
> behind, and the preflight would fail. This isn't published to PyPI.

### Running in a container or as root

The CLI refuses `--dangerously-skip-permissions` under root/sudo, which is how
most containers run. Take the scoped path instead:

```bash
export CRUCIBLE_BUILDER_ALLOWED_TOOLS="Edit,Write,Read,Glob,Grep,Bash(git *),Bash(python *),Bash(pytest *),Bash(ruff *)"
python crucible.py IDEA.md
```

Preflight refuses to start without it when running as root, and prints that
line. It's a hard failure on purpose: the alternative is every builder
invocation dying on a non-JSON error and reporting "unparseable JSON envelope",
which is accurate and completely unhelpful.

### CI

`.github/workflows/ci.yml` runs ruff and the full suite on Python 3.11–3.13 for
every push and PR. The end-to-end tests build real git worktrees, so CI sets a
global git identity — worth knowing if you fork it.

---

## Manual checklist — only you can do these

- [ ] **Deploy to Vercel** — import the repo, set Root Directory to `site`, framework **Other**.
- [ ] **Point DNS** — add the `crucible` CNAME at your DNS provider, or update the absolute URLs if you pick another hostname.
- [ ] **Verify the social card** — paste the live URL into [opengraph.xyz](https://www.opengraph.xyz) and confirm `og.png` renders. LinkedIn caches aggressively; use their [Post Inspector](https://www.linkedin.com/post-inspector/) to force a refresh.
- [ ] **Record a real run** and replace the README hero image.
- [x] ~~Run the live acceptance run~~ — done at `-n 1`, recorded in [ACCEPTANCE_RUN.md](ACCEPTANCE_RUN.md). Cost $3.12 over two rounds.
- [ ] **Optionally run a live `-n 3` tournament** to exercise multi-candidate selection against real builders. Budget roughly 3× the single-candidate run per round; `--budget` and `--max-rounds` bound it.
- [ ] **Confirm your email** — the docs and site use `charanreddychanda@gmail.com`. Your git identity here is `charanreddychanda27@gmail.com`. Pick one and make it consistent.
- [ ] **Point the Résumé button** at a real file — it currently links to your portfolio homepage (`site/about.html`, contact card).
- [ ] **Post on LinkedIn**, then drop the URL into `site/project.html` (the "LinkedIn post" row, currently a placeholder) and any README reference.
- [ ] **Wire the contact form** to a real endpoint if you want submissions to reach you without the visitor's mail client.
- [ ] **Enable branch protection** on `main` once CI is green, so the badge means something.
