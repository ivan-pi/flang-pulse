# flang/pulse

A self-updating dashboard that tracks development activity in the
[LLVM](https://github.com/llvm/llvm-project) Fortran stack — per label
(`flang`, `flang:ir`, `flang:openmp`, `openmp`, `openacc`, and so on).

A GitHub Action snapshots issue and PR counts twice a week, appends them to
`data/history.json`, also records the size of the `flang/` source tree to
`data/loc.json`, and deploys a static site that graphs each metric over time.
No server, no database — the committed JSON *is* the database.

![open issues over time](docs/preview.png)

## What it tracks

For every configured label, per snapshot:

- open issues
- closed issues
- open pull requests
- merged pull requests

The site graphs any of these as a multi-line time series, one line per label,
with a metric switcher and a toggleable legend.

Plus, repository-wide (not per-label):

- total lines of code in the `flang/` subtree, with a per-language breakdown

shown in a dedicated "Source size" panel.

## How the history works

GitHub does not store historical counts — the search API only answers
"how many right now." Two mechanisms fill that gap:

1. **Backfill (first run).** For each label, the collector reconstructs
   monthly open-issue history from issue timestamps. The open count at month
   *m* equals issues created before *m* minus issues closed before *m* — an
   exact figure, not an estimate. This populates the graph immediately,
   `BACKFILL_MONTHS` deep (default 18).

2. **Snapshots (every run).** Each run records that day's exact counts for
   all four metrics and appends them. Over time the forward history becomes a
   precise record. Reconstructed points are flagged `"reconstructed": true`
   so the two are distinguishable.

Lines of code has no backfill — it starts as a single point and builds
forward, one snapshot per run (see *Lines of code* below).

## Lines of code

`scripts/loc.py` measures the `flang/` subtree of llvm/llvm-project. Cloning
all of llvm-project is expensive, so it does the cheap thing: a **blobless,
depth-1, sparse checkout** of just `flang/` (`git clone --filter=blob:none
--depth=1 --no-checkout` then `git sparse-checkout set flang`), which fetches
only that subtree at HEAD — tens of MB, not the multi-GB full repo — and runs
[`cloc`](https://github.com/AlDanial/cloc) over it. It appends one dated point
to `data/loc.json` per run, recording total code/comment/blank lines, file
count, the HEAD sha, and a per-language code-line breakdown.

This is whole-subtree size over time, not a per-label figure — GitHub exposes
line deltas per *repository*, not per *label*, so there is no accurate
per-label LOC to graph.

## Setup

1. **Create a repo** from these files (or fork/copy).

2. **Enable Pages**: Settings → Pages → Source = "GitHub Actions".

3. **Allow the workflow to commit**: Settings → Actions → General →
   Workflow permissions → "Read and write permissions".

4. **First run**: Actions tab → "collect-and-deploy" → "Run workflow".
   The first run does the full backfill, so it takes a few minutes (it makes
   roughly `labels × (BACKFILL_MONTHS + 1) × 2` search calls, paced to stay
   under the rate limit). Subsequent runs add one point each and finish fast.

After that it runs on its own at 06:17 UTC every Monday and Thursday. Your
dashboard lives at `https://<you>.github.io/<repo>/`.

## Configuring labels

Edit `data/labels.json`:

```json
{ "id": "flang:codegen", "name": "flang:codegen", "desc": "code generation", "accent": "#f78c6b" }
```

- `id` — the exact GitHub label (quoted internally, so colons are fine).
- `desc` — shown under the count on the card.
- `accent` — the line/swatch color.

A new label is backfilled automatically on the next run. Removing a label
stops new snapshots; its past data stays in `history.json` until you prune it.

Invalid labels (typos, nonexistent) are reported and skipped rather than
failing the run.

## Running locally

```bash
# collect issue/PR counts (needs a token to avoid the ~10 req/min limit)
GITHUB_TOKEN=ghp_xxx python scripts/collect.py

# collect lines of code (needs git and cloc on PATH)
python scripts/loc.py

# serve — the site tries data/ then ../data/, so this layout works
python -m http.server 8000
# open http://localhost:8000/site/index.html
```

## Adjusting cadence

Edit the `cron` in `.github/workflows/collect.yml`. `17 6 * * 1,4` is Monday
and Thursday; `17 6 * * *` is daily; `17 6 */2 * *` is every other day.
GitHub may delay scheduled runs under load, which is harmless here — a missed
run just means one fewer point.

## Files

```
scripts/collect.py            issue/PR collector (GitHub Search API)
scripts/loc.py                lines-of-code collector (sparse checkout + cloc)
data/labels.json              tracked labels (edit this)
data/history.json             accumulated issue/PR series (machine-written)
data/loc.json                 accumulated lines-of-code series (machine-written)
site/index.html               the dashboard (static, no build step)
.github/workflows/collect.yml schedule + commit + Pages deploy
```

Not affiliated with the LLVM project.
