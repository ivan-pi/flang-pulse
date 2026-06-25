# AGENTS.md

Guidance for AI agents (and humans) working in **flang/pulse**. Read this
before touching anything. The README is the user-facing tour; this file is the
contract you work under.

## What this project is

A self-updating, **server-less dashboard** that tracks development activity in
the LLVM Fortran stack (`flang/` + `flang-rt/` and the related GitHub labels).
A scheduled GitHub Action snapshots metrics, commits them as JSON, and deploys
a static page that graphs them over time.

There is no backend, no database, no build step. **The committed JSON in
`data/` *is* the database.** The site is a single hand-written HTML file. Keep
it that way.

## Top-level philosophy

1. **Correctness of the data is the first priority — above features, above
   polish, above everything.** A wrong number on a dashboard is worse than a
   missing one, because people will believe it. When in doubt, omit a point or
   label it, never guess or interpolate silently. Reconstructed/estimated
   values must be flagged in the data (see `"reconstructed": true`) and shown
   honestly in the UI.
2. **Stay simple and dependency-free.** Data lives in plain JSON (or txt). The
   collectors are stdlib-only Python. The site is one static file with no
   framework and no bundler. Every dependency you add is a future maintenance
   cost and a thing that can break the twice-weekly run. Default to *not*
   adding one.
3. **The data is append-mostly and reproducible.** Git-derived metrics are
   recomputed from the clone every run, so they're always exact. Label history
   is accumulated snapshots plus a one-time backfill. Prefer mechanisms that
   stay correct under re-runs over clever caching.
4. **Cheap to run.** The llvm-project clone is blobless and cached between
   runs; the search API is rate-limited and paced. Don't introduce work that
   re-downloads the world or hammers the API.
5. **Honest by construction.** Labels, axes, and captions should describe
   exactly what was measured — no more. If a metric can't answer the question
   a viewer will ask, say so rather than implying it can.

## Project structure

```
scripts/collect.py              issue/PR collector — GitHub Search API → data/history.json
scripts/git_stats.py            git-log collector — churn / contributors / releases / size
data/labels.json                tracked labels (THE ONE hand-edited data file)
data/history.json               accumulated issue/PR series        (machine-written)
data/activity.json              monthly churn/commits/contributors (machine-written)
data/releases.json              llvmorg release tags + dates       (machine-written)
data/loc.json                   source-size series                 (machine-written)
site/index.html                 the dashboard — static, vanilla JS/CSS, no build
.github/workflows/collect.yml   schedule + cached clone + commit + Pages deploy
docs/preview.png                README screenshot
README.md                       user-facing documentation
```

### The two collectors

- **`collect.py`** — per-label issue/PR counts from the GitHub Search API.
  GitHub stores no historical counts, so the first run **backfills** monthly
  open-issue history from issue timestamps (exact, not interpolated) and every
  run **appends** that day's exact snapshot. Reconstructed points carry
  `"reconstructed": true`.
- **`git_stats.py`** — everything derivable from `git log` over the Fortran
  subtrees (per-path line deltas have no API equivalent). Recomputes the whole
  trailing window each run, so this history is always exact. Reads an existing
  local clone; it never clones itself.

### The site

`site/index.html` is the whole front end: vanilla HTML/CSS/JS, no framework,
no build. It fetches the JSON from `data/` (falling back to `../data/` so the
repo layout serves locally) and draws the charts. If you can do it without a
dependency, do it without a dependency.

## Rules

### Data integrity (the important one)

- **Never hand-edit the machine-written data files** —
  `data/history.json`, `data/activity.json`, `data/releases.json`,
  `data/loc.json`. They are written *only* by the collectors. If a value is
  wrong, fix the **collector** and let it regenerate the data; do not patch
  the JSON by hand. A hand-edited number is unreproducible and will silently
  diverge on the next run.
- **`data/labels.json` is the only data file humans/agents edit directly.** It
  is configuration, not collected data. New labels backfill automatically on
  the next run.
- **Preserve history.** Don't drop, reorder, or rewrite existing snapshots to
  make a chart look nicer. Removing a label stops new snapshots but its past
  data stays until deliberately pruned.
- **Never fabricate or interpolate.** No placeholder data, no "reasonable
  guesses," no smoothing that invents points. Missing is fine; wrong is not.
- **Flag anything estimated** in the data and surface that flag in the UI.
- After changing a collector, **run it and inspect the diff** to confirm the
  output is what you expect before committing.

### Code & changes

- Keep `collect.py` and `git_stats.py` **stdlib-only**. The only external
  runtime tool they may shell out to is `git` and `cloc` (already assumed by
  the workflow). No `pip install` in the run path.
- Keep `site/index.html` **build-free and framework-free**. No npm runtime
  deps, no bundler, no CDN framework imports for core functionality.
- Match the surrounding style: the existing comment density and the descriptive
  `# ── section ──` banners in the Python; the existing CSS-variable theme in
  the HTML.
- Keep the workflow cheap and idempotent: blobless + cached clone, paced API
  calls, recompute-don't-mutate. Don't add steps that re-download or that
  could double-deploy Pages (note the `concurrency` group).

### Git workflow

- Develop on the branch you were assigned; create it locally if needed.
- Clear, descriptive commit messages. Match the existing convention
  (`feat:`, `fix:`, `data:`, `chore:`, `refactor:`).
- Push with `git push -u origin <branch>`. **Do not open a pull request unless
  explicitly asked.**
- Don't commit `_site/`, `__pycache__/`, or a local llvm clone (see
  `.gitignore`).

## Allowed tools

Day-to-day, agents work with:

- **`git`** — version control.
- **`python3` (3.12, stdlib only)** — run and edit the collectors. No third-
  party runtime packages.
- **`cloc`** — source-size measurement, invoked by `git_stats.py`.
- **`python -m http.server`** — serve the site locally for a visual check.
- **JSON validation** (e.g. `python -m json.tool`) — sanity-check any JSON you
  touch.

Sanctioned **dev-time** tools (for working on the repo, **not** added to the
scheduled run path or as runtime dependencies):

- **A Python linter/formatter** (`ruff` / `black`) for the two scripts.
- **Prettier / a JS formatter** for `site/index.html`.
- **Playwright (headless Chromium)** to screenshot the dashboard for visual
  checks and to refresh `docs/preview.png`.

If a task needs a tool outside this list, **ask first** — adding tooling is a
deliberate decision, not a default, given the zero-dependency posture.

## Running locally

```bash
# issue/PR counts (token avoids the ~10 req/min unauthenticated search limit)
GITHUB_TOKEN=ghp_xxx python scripts/collect.py

# git metrics — point LLVM_REPO at a blobless clone; needs git + cloc
git clone --filter=blob:none --no-checkout https://github.com/llvm/llvm-project.git /tmp/llvm
LLVM_REPO=/tmp/llvm python scripts/git_stats.py

# serve — site tries data/ then ../data/, so this layout works
python -m http.server 8000   # open http://localhost:8000/site/index.html
```

Useful env vars: `BACKFILL_MONTHS` (collect.py, default 18),
`ACTIVITY_MONTHS` (git_stats.py, default 12/36), `SINCE_COMMIT` (git_stats.py).

## Definition of done

- Data files only ever changed by the collectors, never by hand.
- Collectors stay stdlib-only; site stays build-free.
- You ran the affected collector (or served the site) and checked the result.
- The diff contains nothing that breaks the twice-weekly run.

---

Not affiliated with the LLVM project.
