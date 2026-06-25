#!/usr/bin/env python3
"""
flang/pulse history backfill — a one-time, offline helper.

The weekly collectors only ever know "right now": git_stats.py snapshots the
current source size / NYI / test counts, and collect.py snapshots today's
issue and PR counts. That is why a freshly-seeded dashboard shows a single
point on the source-size and PR charts. This script reconstructs the missing
history so those charts have a curve from day one. It is meant to be run by
hand, not in CI (it is slower and heavier than a normal collection).

Two independent phases — run whichever inputs you have:

  git   reconstruct source size + NYI markers + test-suite size, one point per
        month, by walking flang/ + flang-rt/ at the commit that was HEAD at
        each month boundary and re-running cloc / git grep there.
        Needs:  LLVM_REPO pointing at a clone (blobless is fine), and cloc.
        Writes: data/loc.json (merged, never clobbering live snapshots).

  prs   reconstruct monthly open- and merged-PR counts per label, the same way
        collect.py already backfills open issues: a merged PR keeps its
        merged:/closed: timestamps forever, so created-before-m minus
        closed-before-m is exact, not interpolated.
        Needs:  GITHUB_TOKEN (to avoid the search rate limit).
        Writes: data/history.json (merges into the reconstructed points).

Usage
-----
    # both phases (each is skipped if its inputs are missing)
    LLVM_REPO=/tmp/llvm GITHUB_TOKEN=ghp_xxx python scripts/backfill.py

    # just one phase
    python scripts/backfill.py --only git
    python scripts/backfill.py --only prs

    # how many months back (default 18, matching collect.py's BACKFILL_MONTHS)
    python scripts/backfill.py --months 36
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402  (gh_search, q, month_starts, load_json, …)
import git_stats as gs  # noqa: E402  (git, materialize, cloc_counts, code_metrics, …)


# ── git phase: source size / NYI / tests over time ────────────────────
def month_firsts(n: int) -> list[str]:
    """First-of-month ISO dates, oldest→newest, for the last n+1 months."""
    return collect.month_starts(n)


def commit_before(when_iso: str, tip: str) -> str | None:
    """Last commit reachable from `tip` strictly before `when_iso`, or None.

    `tip` is a fixed starting ref, never "HEAD": the backfill checks out
    historical commits as it goes, which detaches HEAD onto old commits, so
    walking from HEAD would only ever see the oldest one.
    """
    sha = gs.git("rev-list", "-1", "--first-parent",
                 f"--before={when_iso}", tip).strip()
    return sha or None


def backfill_git(months: int) -> int:
    repo = gs.REPO
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        print(f"git phase: no clone at {repo} (set LLVM_REPO) — skipping",
              file=sys.stderr)
        return 0

    import shutil
    if not shutil.which("cloc"):
        print("git phase: cloc not found — skipping", file=sys.stderr)
        return 0

    # Remember where the clone was so we can put it back when we are done;
    # walking history leaves it on a detached historical commit otherwise.
    original = gs.git("rev-parse", "HEAD").strip()

    now = datetime.now(timezone.utc).isoformat()
    loc = gs.load_json(gs.LOC, {"repo": "llvm/llvm-project",
                                "paths": gs.PATHS, "snapshots": []})
    loc["paths"] = gs.PATHS
    loc.setdefault("snapshots", [])
    have = {s.get("date") for s in loc["snapshots"]}

    # For month m we want the subtree as it stood at the end of that month,
    # i.e. the last commit before the first of the *next* month. The current
    # (newest) month has no "next first" yet, so it uses HEAD.
    firsts = month_firsts(months)
    boundaries = firsts[1:] + [None]  # next-month boundary for each entry

    written = 0
    try:
        for month, boundary in zip(firsts, boundaries):
            if month in have:
                print(f"  {month}: already present — skipping", flush=True)
                continue
            sha = commit_before(boundary, original) if boundary else original
            if not sha:
                print(f"  {month}: no commit before {boundary} — skipping",
                      flush=True)
                continue
            counts = gs.collect_loc(sha)
            if not counts:
                print(f"  {month}: cloc produced nothing — skipping", flush=True)
                continue
            loc["snapshots"].append({
                "date": month, "head_sha": sha,
                "collected_at": now, "reconstructed": True, **counts,
            })
            written += 1
            print(f"  {month} @ {sha[:10]}: {counts['code']:,} lines, "
                  f"{counts['nyi']:,} NYI, {counts['tests']:,} tests",
                  flush=True)
    finally:
        # Restore the clone to where it started, whatever happened above.
        gs.git("checkout", "--force", original)

    loc["snapshots"].sort(key=lambda s: s["date"])
    loc["updated_at"] = now
    gs.write_json(gs.LOC, loc)
    print(f"git phase: wrote {written} new monthly points to {gs.LOC}",
          flush=True)
    return 0


# ── prs phase: open / merged PR counts over time ──────────────────────
def backfill_prs(months: int, token: str | None) -> int:
    config = collect.load_json(collect.CONFIG, {"labels": []})
    labels = config.get("labels", [])
    if not labels:
        print("prs phase: no labels in data/labels.json — skipping",
              file=sys.stderr)
        return 0

    history = collect.load_json(
        collect.DATA, {"repo": collect.REPO, "labels": {}, "series": {}})
    history.setdefault("labels", {})
    history.setdefault("series", {})
    now = datetime.now(timezone.utc).isoformat()

    cutoffs = collect.month_starts(months)
    for entry in labels:
        label = entry["id"]
        print(f"prs phase: {label}", flush=True)
        history["labels"][label] = entry
        series = history["series"].setdefault(
            label, {"snapshots": [], "backfilled": False})
        by_date = {s["date"]: s for s in series["snapshots"]}

        for cutoff in cutoffs:
            try:
                created = collect.gh_search(
                    collect.q(label, f"is:pr created:<{cutoff}"), token)
                time.sleep(collect.SLEEP)
                closed = collect.gh_search(
                    collect.q(label, f"is:pr closed:<{cutoff}"), token)
                time.sleep(collect.SLEEP)
                merged = collect.gh_search(
                    collect.q(label, f"is:pr is:merged merged:<{cutoff}"), token)
                time.sleep(collect.SLEEP)
            except ValueError as e:
                print(f"  skipping {label}: {e}", flush=True)
                break

            open_prs = max(0, created - closed)
            snap = by_date.get(cutoff)
            if snap is None:
                snap = {"date": cutoff, "reconstructed": True}
                series["snapshots"].append(snap)
                by_date[cutoff] = snap
            snap["open_prs"] = open_prs
            snap["merged_prs"] = merged
            print(f"  {label} @ {cutoff}: {open_prs} open, {merged} merged",
                  flush=True)

        series["snapshots"].sort(key=lambda s: s["date"])

    history["updated_at"] = now
    collect.DATA.parent.mkdir(parents=True, exist_ok=True)
    collect.DATA.write_text(__import__("json").dumps(history, indent=2) + "\n")
    print(f"prs phase: wrote {collect.DATA}", flush=True)
    return 0


# ── main ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="One-time flang/pulse backfill.")
    ap.add_argument("--only", choices=["git", "prs"],
                    help="run a single phase (default: both)")
    ap.add_argument("--months", type=int, default=collect.BACKFILL_MONTHS,
                    help=f"months of history to reconstruct "
                         f"(default {collect.BACKFILL_MONTHS})")
    args = ap.parse_args()

    rc = 0
    if args.only in (None, "git"):
        print("=== git phase ===", flush=True)
        rc |= backfill_git(args.months)
    if args.only in (None, "prs"):
        print("=== prs phase ===", flush=True)
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("prs phase: no GITHUB_TOKEN — running unauthenticated "
                  "(rate limits will bite)", flush=True)
        rc |= backfill_prs(args.months, token)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
