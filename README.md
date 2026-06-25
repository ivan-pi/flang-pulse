# flang/pulse

A self-updating dashboard that tracks development activity in the
[LLVM](https://github.com/llvm/llvm-project) Fortran stack — per label
(`flang`, `flang:ir`, `flang:openmp`, `openmp`, `openacc`, and so on).

A GitHub Action runs twice a week. It snapshots issue/PR counts from the
GitHub API and derives development-activity metrics from `git log` over the
Fortran stack (`flang/` + `flang-rt/`), commits the results as JSON, and
deploys a static site that graphs each metric over time. No server, no
database — the committed JSON *is* the database.

![open issues over time](docs/preview.png)

## What it tracks

**Per label** (from the GitHub Search API), one line per label:

- open issues · closed issues · open pull requests · merged pull requests

**Across the Fortran stack** (`flang/` + `flang-rt/`, from `git log`):

- lines changed per month (added / removed) — *the volume of change*
- commits per month
- active contributors per month
- source size over time (`cloc`, with a per-language breakdown)
- all-time scale: total commits, distinct contributors, project age

LLVM `X.Y.0` releases are drawn as dashed vertical markers across every time
series, so you can see how activity moves around release boundaries.

## How the history works

GitHub does not store historical counts — the search API only answers "how
many right now." For **labels**, two mechanisms fill the gap:

1. **Backfill (first run).** The collector reconstructs monthly open-issue
   history from issue timestamps: open count at month *m* = issues created
   before *m* minus issues closed before *m* — exact, not interpolated.
   `BACKFILL_MONTHS` deep (default 18).

2. **Snapshots (every run).** Each run records that day's exact counts and
   appends them. Reconstructed points are flagged `"reconstructed": true`.

For the **git-derived** metrics there is no such problem — git *is* the
historical record. Each run recomputes the trailing `ACTIVITY_MONTHS` window
(default 36) directly from the clone, so the history is always exact.

## The git metrics (and why they need a clone)

The headline question — *how much code is flowing into flang, and is it
speeding up or slowing down?* — has no GitHub API answer. The API exposes
line deltas per *commit*, never per *path*, so per-folder churn can only come
from `git log --numstat`. `scripts/git_stats.py` reads a local clone and
writes three datasets:

- `data/activity.json` — monthly commits / authors / insertions / deletions,
  combined and split per path, plus all-time totals
- `data/releases.json` — `llvmorg-X.Y.0` tags with dates (the chart markers)
- `data/loc.json` — current size of both subtrees (`cloc`), appended per run

Cloning all of llvm-project is expensive, which is the whole concern. Two
things keep the cost down:

1. **Blobless clone** (`git clone --filter=blob:none`) — downloads the commit
   and tree history but *not* file contents, which is all `git log` needs to
   attribute commits and authors to paths.
2. **Cached between runs** — the workflow stores the clone with
   `actions/cache` and only `git fetch`es new commits on later runs, so the
   heavy download is a one-time cost. Recomputing the metrics from the cached
   clone needs no network.

This is whole-subtree activity, not per-label — line deltas exist per
repository/path, not per GitHub label.

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

# collect git metrics: point LLVM_REPO at a clone (blobless is fine).
# needs git, and cloc for the size snapshot.
git clone --filter=blob:none --no-checkout https://github.com/llvm/llvm-project.git /tmp/llvm
LLVM_REPO=/tmp/llvm python scripts/git_stats.py

# serve — the site tries data/ then ../data/, so this layout works
python -m http.server 8000
# open http://localhost:8000/site/index.html
```

`ACTIVITY_MONTHS` (default 36) controls how many months of history the
activity charts cover.

## Adjusting cadence

Edit the `cron` in `.github/workflows/collect.yml`. `17 6 * * 1,4` is Monday
and Thursday; `17 6 * * *` is daily; `17 6 */2 * *` is every other day.
GitHub may delay scheduled runs under load, which is harmless here — a missed
run just means one fewer point.

## Files

```
scripts/collect.py            issue/PR collector (GitHub Search API)
scripts/git_stats.py          git-log collector (churn / contributors / releases / size)
data/labels.json              tracked labels (edit this)
data/history.json             accumulated issue/PR series (machine-written)
data/activity.json            monthly churn/commits/contributors (machine-written)
data/releases.json            llvmorg release tags + dates (machine-written)
data/loc.json                 source-size series (machine-written)
site/index.html               the dashboard (static, no build step)
.github/workflows/collect.yml schedule + cached clone + commit + Pages deploy
```

Not affiliated with the LLVM project.
