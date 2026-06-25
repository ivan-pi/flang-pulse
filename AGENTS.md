# AGENTS.md

Guidance for agents (and humans) working in **flang/pulse**. The README is the
user-facing tour; this file is the contract you work under.

## What this project is

A self-updating, **server-less dashboard** tracking development activity in the
LLVM Fortran stack (`flang/` + `flang-rt/` and the related GitHub labels). A
scheduled GitHub Action snapshots metrics, commits them as JSON, and deploys a
static page that graphs them over time.

No backend, no database, no build step. **The committed JSON in `data/` *is*
the database.** The site is a single hand-written HTML file.

## Philosophy

1. **Correct data above everything — features, polish, all of it.** A wrong
   number is worse than no number, because people believe it. Never guess,
   interpolate, or fabricate. Values that are reconstructed or estimated must
   be flagged in the data (see `"reconstructed": true`) and shown honestly in
   the UI — surface the uncertainty, don't hide it and don't silently drop it.
2. **Keep it simple.** Plain JSON (or txt) for data, stdlib Python collectors,
   one static HTML file. Dependencies aren't forbidden — but adding one is a
   **conscious decision**, weighed against the maintenance cost and the chance
   it breaks the twice-weekly run. Don't reach for one by reflex; if a task
   genuinely calls for it, make the case.
3. **Reproducible.** Git metrics recompute from the clone every run, so they're
   always exact. Label history is accumulated snapshots plus a one-time
   backfill. Prefer mechanisms that stay correct under re-runs.
4. **Cheap to run.** The llvm-project clone is blobless and cached; the search
   API is paced. Don't add work that re-downloads the world or hammers the API.
5. **Honest by construction.** Labels, axes, and captions describe exactly what
   was measured — no more.

## Structure

```
scripts/collect.py              issue/PR collector — GitHub Search API → data/history.json
scripts/git_stats.py            git-log collector — churn / contributors / releases / size
data/labels.json                tracked labels (the one hand-edited data file)
data/history.json               accumulated issue/PR series        (machine-written)
data/activity.json              monthly churn/commits/contributors (machine-written)
data/releases.json              llvmorg release tags + dates       (machine-written)
data/loc.json                   source-size series                 (machine-written)
site/index.html                 the dashboard — static, vanilla JS/CSS, no build
.github/workflows/collect.yml   schedule + cached clone + commit + Pages deploy
```

- **`collect.py`** — per-label issue/PR counts. GitHub keeps no historical
  counts, so the first run **backfills** monthly open-issue history from issue
  timestamps (exact, not interpolated) and every run **appends** that day's
  snapshot. Reconstructed points carry `"reconstructed": true`.
- **`git_stats.py`** — metrics from `git log` over the Fortran subtrees
  (per-path line deltas have no API equivalent). Recomputes the whole trailing
  window each run, so this history is always exact. Reads an existing clone; it
  never clones itself.
- **`site/index.html`** — the whole front end: vanilla HTML/CSS/JS, no
  framework, no build. Fetches the JSON from `data/` (falling back to `../data/`
  so the repo layout serves locally) and draws the charts.

## Rules

**Data integrity — the one that matters most:**

- **Never hand-edit the machine-written files** (`history.json`,
  `activity.json`, `releases.json`, `loc.json`). The collectors are their only
  authors. If a value is wrong, fix the **collector** and regenerate — a
  hand-patched number is unreproducible and diverges on the next run.
- **`labels.json` is the only data file you edit by hand.** It's configuration;
  new labels backfill automatically next run.
- **Preserve history.** Don't drop, reorder, or rewrite existing snapshots to
  make a chart look nicer.
- **Flag anything estimated** in the data and show that flag in the UI.
- After changing a collector, **run it and inspect the diff** before committing.

**Code:**

- Match the surrounding style — the comment density and `# ── section ──`
  banners in the Python, the CSS-variable theme in the HTML.
- Keep the workflow cheap and idempotent (blobless + cached clone, paced API
  calls, recompute-don't-mutate; note the Pages `concurrency` group).
- Any new dependency is a deliberate choice — see philosophy #2.

**Git:**

- Develop on your assigned branch; create it locally if needed.
- Descriptive commit messages matching the existing prefixes (`feat:`, `fix:`,
  `data:`, `chore:`, `refactor:`). Push with `git push -u origin <branch>`.
- **No pull request unless explicitly asked.**
- Don't commit `_site/`, `__pycache__/`, or a local llvm clone.

## Tools

Everyday: **`git`**, **`python3`** (3.12), **`cloc`** (used by `git_stats.py`),
**`python -m http.server`** to preview the site, and **`python -m json.tool`**
to validate JSON.

Available when a task needs them: a **Python linter/formatter** (`ruff` /
`black`), **Prettier** for `index.html`, and **Playwright** (headless Chromium)
to screenshot the dashboard / refresh `docs/preview.png`. These are dev-time
tools, not part of the scheduled run. Reaching beyond this list is fine when
justified — just flag it, consistent with the dependency stance above.

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

Env vars: `BACKFILL_MONTHS` (collect.py), `ACTIVITY_MONTHS` / `SINCE_COMMIT`
(git_stats.py).

---

Not affiliated with the LLVM project.
