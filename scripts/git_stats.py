#!/usr/bin/env python3
"""
flang/pulse git-history collector.

Everything here is derived from `git log` over the Fortran stack — the
`flang/` and `flang-rt/` subtrees of llvm/llvm-project — because the
metrics the dashboard cares about (how much code is changing, how many
people are changing it, how big the project is) have no GitHub API
equivalent. The API exposes line deltas per *commit*, never per *path*.

It reads an existing local clone (the workflow caches a blobless clone and
fetches it incrementally, so this script never clones itself). From that
clone it writes three datasets:

  data/activity.json  monthly commits / authors / insertions / deletions,
                      combined and split per path, plus all-time totals
  data/releases.json  llvmorg-X.Y.0 release tags with dates (chart markers)
  data/loc.json       current size (cloc) of the two subtrees at HEAD

Recomputing the whole window every run keeps the logic simple and always
correct; with the cached clone the blobs are already local, so it costs no
network.

Environment variables
---------------------
LLVM_REPO        path to local clone (default ./.llvm-cache)
ACTIVITY_MONTHS  months of history to collect (default 12)
SINCE_COMMIT     if set, collect history reachable from this commit SHA
                 instead of using ACTIVITY_MONTHS.  Suggested starting
                 point for a compact first run: 3623fe6 (≈ August 2025).

git log is run in streaming mode (Popen) so that the output is never
fully buffered in memory — the script processes each line as it arrives.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

PATHS = ["flang", "flang-rt"]
ROOT = Path(__file__).resolve().parent.parent
REPO = Path(os.environ.get("LLVM_REPO", ROOT / ".llvm-cache"))
WINDOW_MONTHS = int(os.environ.get("ACTIVITY_MONTHS", "12"))
SINCE_COMMIT = os.environ.get("SINCE_COMMIT", "").strip()

ACTIVITY = ROOT / "data" / "activity.json"
RELEASES = ROOT / "data" / "releases.json"
LOC = ROOT / "data" / "loc.json"

TRACKED_LANGS = ["C++", "C/C++ Header", "Fortran 90", "Fortran 77", "Python", "CMake"]

# Print a heartbeat line every this many commits while streaming.
HEARTBEAT_EVERY = 500


def git(*args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO), *args],
        check=True, capture_output=True, text=True,
    )
    return res.stdout


def _git_stream(*args: str) -> subprocess.Popen:
    """Return a Popen object whose stdout can be iterated line-by-line."""
    return subprocess.Popen(
        ["git", "-C", str(REPO), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _range_args() -> list[str]:
    """Return the git-log range flags based on env configuration."""
    if SINCE_COMMIT:
        return [f"{SINCE_COMMIT}..HEAD"]
    return [f"--since={WINDOW_MONTHS} months ago"]


# ── monthly activity ──────────────────────────────────────────────────
def _fresh_month() -> dict:
    return {
        "commits": 0, "insertions": 0, "deletions": 0, "files": 0,
        "_authors": set(),
        "by_path": {p: {"commits": 0, "insertions": 0, "deletions": 0,
                        "files": 0} for p in PATHS},
    }


# Trailing window (in months) for the "rolling active contributors" line.
ROLLING_MONTHS = 3


def _months_back(month: str, n: int) -> list[str]:
    """Return [month, month-1, …] as n consecutive 'YYYY-MM' keys."""
    y, m = int(month[:4]), int(month[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def index_history() -> tuple[dict[str, str], dict[str, set]]:
    """Walk *all* history once to build two indexes over the subtrees:

      first_seen        author email -> 'YYYY-MM' of their first-ever commit
      month_authors     'YYYY-MM' -> set of author emails active that month

    `first_seen` lets us flag genuinely new contributors; `month_authors`
    spans the full history (not just the activity window) so the rolling
    contributor count is correct even for the earliest months on the chart.

    Metadata-only log (no --numstat), so it needs no blob content and stays
    cheap even on a blobless clone.
    """
    first: dict[str, str] = {}
    month_authors: dict[str, set] = {}
    proc = _git_stream(
        "log", "--no-merges", "--pretty=format:%aI\t%aE", "--", *PATHS,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        iso, email = line.split("\t", 1)
        month, email = iso[:7], email.lower()
        month_authors.setdefault(month, set()).add(email)
        # git log is newest-first, so the last value we record for an email
        # is their oldest (first) commit month.
        first[email] = month

    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        print(f"  git log exited with code {rc}: {stderr.strip()}", file=sys.stderr)
    if proc.stderr:
        proc.stderr.close()
    return first, month_authors


def _top_path(path: str) -> str | None:
    for p in PATHS:
        if path.startswith(p + "/"):
            return p
    return None


def collect_activity(first_seen: dict[str, str],
                     month_authors: dict[str, set]) -> list[dict]:
    """Bucket per-month churn/commits/authors from `git log --numstat`.

    Uses streaming (Popen) to avoid buffering gigabytes of numstat output
    in memory when the history window is large. `first_seen` lets us count
    first-time contributors per month; `month_authors` (full history) feeds
    the rolling active-contributor line so its earliest points aren't biased
    low by the window edge.
    """
    t0 = time.monotonic()
    months: dict[str, dict] = {}
    cur: dict | None = None
    cur_paths: set[str] = set()
    commit_count = 0

    proc = _git_stream(
        "log", "--no-merges", *_range_args(),
        "--numstat", "--no-renames",
        "--pretty=format:@@@%H\t%aI\t%aE", "--", *PATHS,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("@@@"):
            _, iso, email = line[3:].split("\t")
            cur = months.setdefault(iso[:7], _fresh_month())
            cur["commits"] += 1
            cur["_authors"].add(email.lower())
            cur_paths = set()
            commit_count += 1
            if commit_count % HEARTBEAT_EVERY == 0:
                print(f"  … processed {commit_count} commits", flush=True)
            continue
        if cur is None:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add, dele, path = parts
        top = _top_path(path)
        if top is None:
            continue
        a = 0 if add == "-" else int(add)
        d = 0 if dele == "-" else int(dele)
        cur["insertions"] += a
        cur["deletions"] += d
        cur["files"] += 1
        bp = cur["by_path"][top]
        bp["insertions"] += a
        bp["deletions"] += d
        bp["files"] += 1
        if top not in cur_paths:
            bp["commits"] += 1
            cur_paths.add(top)

    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        print(f"  git log exited with code {rc}: {stderr.strip()}", file=sys.stderr)
    if proc.stderr:
        proc.stderr.close()

    elapsed = time.monotonic() - t0
    print(f"  streamed {commit_count} commits in {elapsed:.1f}s", flush=True)

    month_keys = sorted(months)

    # Rolling active contributors: distinct authors over a trailing window,
    # giving a smoother "is the community sustained?" line than the spiky
    # per-month count. Drawn from full history (month_authors), so months at
    # the window's left edge still see their true preceding two months.
    for mk in month_keys:
        window = set()
        for prev in _months_back(mk, ROLLING_MONTHS):
            window |= month_authors.get(prev, set())
        months[mk]["active_rolling"] = len(window)

    # First-time contributors: people whose first-ever commit falls in a month
    # that is inside our window.
    new_per_month: dict[str, int] = {}
    for fm in first_seen.values():
        if fm in months:
            new_per_month[fm] = new_per_month.get(fm, 0) + 1

    rows = []
    for m in month_keys:
        d = months[m]
        authors = d.pop("_authors")
        d["month"] = m
        d["authors"] = len(authors)
        d["new_authors"] = new_per_month.get(m, 0)
        d["churn"] = d["insertions"] + d["deletions"]
        d["net"] = d["insertions"] - d["deletions"]
        rows.append(d)
    return rows


def collect_totals() -> dict:
    """Scale figures over the tracked subtrees (commits/authors/date range).

    Bounded by the same window as collect_activity() and streamed line by
    line to avoid buffering large histories in memory.
    """
    commits = 0
    authors: set[str] = set()
    first_date = ""
    last_date = ""

    proc = _git_stream(
        "log", "--no-merges", *_range_args(),
        "--pretty=format:%aI\t%aE", "--", *PATHS,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        iso, email = line.split("\t", 1)
        authors.add(email.lower())
        if not last_date:
            last_date = iso[:10]   # first line = newest commit
        first_date = iso[:10]      # last line  = oldest commit
        commits += 1

    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        print(f"  git log exited with code {rc}: {stderr.strip()}", file=sys.stderr)
    if proc.stderr:
        proc.stderr.close()

    if not commits:
        return {}
    return {
        "commits": commits,
        "authors": len(authors),
        "first_commit": first_date,
        "last_commit": last_date,
    }


# ── releases ──────────────────────────────────────────────────────────
REL_RE = re.compile(r"^llvmorg-(\d+)\.(\d+)\.0$")  # final X.Y.0 only


def collect_releases() -> list[dict]:
    out = git("tag", "--list", "llvmorg-*")
    rels = []
    for tag in out.splitlines():
        m = REL_RE.match(tag.strip())
        if not m:
            continue
        iso = git("log", "-1", "--format=%aI", tag).strip()
        rels.append({
            "tag": tag.strip(),
            "version": f"{int(m.group(1))}.{int(m.group(2))}.0",
            "date": iso[:10],
            "_sort": (int(m.group(1)), int(m.group(2))),
        })
    rels.sort(key=lambda r: r.pop("_sort"))
    return rels


# ── current size (cloc) ───────────────────────────────────────────────
def collect_loc() -> dict | None:
    if not shutil.which("cloc"):
        print("  cloc not found — skipping size snapshot", flush=True)
        return None
    # Materialise the two subtrees at HEAD (fetches current blobs only).
    subprocess.run(["git", "-C", str(REPO), "sparse-checkout", "init", "--cone"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(REPO), "sparse-checkout", "set", *PATHS],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(REPO), "checkout"],
                   check=True, capture_output=True, text=True)

    targets = [str(REPO / p) for p in PATHS if (REPO / p).exists()]
    if not targets:
        return None
    raw = subprocess.run(["cloc", "--json", "--quiet", *targets],
                         check=True, capture_output=True, text=True).stdout
    data = json.loads(raw)
    total = data.get("SUM", {})
    by_lang: dict[str, int] = {}
    other = 0
    for lang, stats in data.items():
        if lang in ("header", "SUM"):
            continue
        code = int(stats.get("code", 0))
        if lang in TRACKED_LANGS:
            by_lang[lang] = code
        else:
            other += code
    if other:
        by_lang["Other"] = other
    return {
        "files": int(total.get("nFiles", 0)),
        "code": int(total.get("code", 0)),
        "comment": int(total.get("comment", 0)),
        "blank": int(total.get("blank", 0)),
        "by_language": by_lang,
    }


# ── io ────────────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def main() -> int:
    if not (REPO / ".git").exists() and not (REPO / "HEAD").exists():
        print(f"error: no git clone at {REPO} (set LLVM_REPO)", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat()
    head = git("rev-parse", "HEAD").strip()

    if SINCE_COMMIT:
        print(f"window: {SINCE_COMMIT}..HEAD", flush=True)
    else:
        print(f"window: last {WINDOW_MONTHS} months", flush=True)

    print("indexing all-time contributors", flush=True)
    first_seen, month_authors = index_history()
    print(f"  {len(first_seen)} contributors on record", flush=True)

    print("collecting monthly activity", flush=True)
    months = collect_activity(first_seen, month_authors)
    totals = collect_totals()
    activity_meta: dict = {
        "repo": "llvm/llvm-project", "paths": PATHS,
        "head_sha": head, "updated_at": now,
        "months": months, "totals": totals,
    }
    if SINCE_COMMIT:
        activity_meta["since_commit"] = SINCE_COMMIT
    else:
        activity_meta["window_months"] = WINDOW_MONTHS
    write_json(ACTIVITY, activity_meta)
    print(f"  {len(months)} months, totals={totals}", flush=True)

    print("collecting releases", flush=True)
    rels = collect_releases()
    write_json(RELEASES, {"repo": "llvm/llvm-project",
                          "updated_at": now, "releases": rels})
    print(f"  {len(rels)} release tags", flush=True)

    print("collecting current size", flush=True)
    counts = collect_loc()
    if counts:
        today = date.today().isoformat()
        loc = load_json(LOC, {"repo": "llvm/llvm-project", "paths": PATHS,
                              "snapshots": []})
        loc["paths"] = PATHS
        loc.setdefault("snapshots", [])
        loc["snapshots"] = [s for s in loc["snapshots"]
                            if s.get("date") != today]
        loc["snapshots"].append({"date": today, "head_sha": head,
                                 "collected_at": now, **counts})
        loc["snapshots"].sort(key=lambda s: s["date"])
        loc["updated_at"] = now
        write_json(LOC, loc)
        print(f"  {counts['code']:,} code lines, {counts['files']:,} files",
              flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
