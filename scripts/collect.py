#!/usr/bin/env python3
"""
flang/pulse collector.

Runs on a schedule (GitHub Actions) and appends a dated snapshot of
per-label issue/PR counts to data/history.json. On the first run it
backfills monthly history by reconstructing open-issue counts from
issue timestamps, so the graphs are populated immediately.

Auth: set GITHUB_TOKEN in the environment (the Actions runner provides
one automatically). Unauthenticated runs work but hit the ~10 req/min
search limit quickly.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

REPO = "llvm/llvm-project"
SEARCH = "https://api.github.com/search/issues"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "history.json"
CONFIG = ROOT / "data" / "labels.json"

# How many months of history to reconstruct on first run.
BACKFILL_MONTHS = 18

# Polite spacing between search calls. The authenticated search limit is
# 30 req/min; 2.2s keeps us comfortably under it without a token too.
SLEEP = 2.2


# ── HTTP ──────────────────────────────────────────────────────────────
def gh_search(query: str, token: str | None) -> int:
    """Return total_count for a search query, with retry on rate limit."""
    url = f"{SEARCH}?q={query}&per_page=1"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
                return int(payload["total_count"])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("x-ratelimit-reset")
                wait = 30
                if reset:
                    wait = max(5, int(reset) - int(time.time()) + 2)
                wait = min(wait, 90)
                print(f"  rate-limited; sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            if e.code == 422:
                raise ValueError(f"invalid query/label: {query}") from e
            raise
    raise RuntimeError(f"giving up on query after retries: {query}")


def q(label: str, extra: str) -> str:
    label_q = urllib.parse.quote(f'"{label}"')
    return f"repo:{REPO}+label:{label_q}+{urllib.parse.quote(extra, safe=':<>=.')}"


# ── snapshot ──────────────────────────────────────────────────────────
def snapshot_label(label: str, token: str | None) -> dict:
    """Current headline counts for one label."""
    out = {
        "open_issues": gh_search(q(label, "is:issue is:open"), token),
    }
    time.sleep(SLEEP)
    out["closed_issues"] = gh_search(q(label, "is:issue is:closed"), token)
    time.sleep(SLEEP)
    out["open_prs"] = gh_search(q(label, "is:pr is:open"), token)
    time.sleep(SLEEP)
    out["merged_prs"] = gh_search(q(label, "is:pr is:merged"), token)
    time.sleep(SLEEP)
    return out


def month_starts(n: int) -> list[str]:
    today = date.today()
    out = []
    for i in range(n, -1, -1):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12
            y -= 1
        out.append(date(y, m, 1).isoformat())
    return out


def backfill_label(label: str, token: str | None) -> list[dict]:
    """Reconstruct monthly open-issue history from issue timestamps.

    open(m) = (#issues created before m) - (#issues closed before m)
    This is exact, not interpolated.
    """
    points = []
    for cutoff in month_starts(BACKFILL_MONTHS):
        created = gh_search(q(label, f"is:issue created:<{cutoff}"), token)
        time.sleep(SLEEP)
        closed = gh_search(q(label, f"is:issue closed:<{cutoff}"), token)
        time.sleep(SLEEP)
        points.append({"date": cutoff, "open_issues": created - closed})
        print(f"  {label} @ {cutoff}: {created - closed} open", flush=True)
    return points


# ── main ──────────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("warning: no GITHUB_TOKEN set; running unauthenticated.", flush=True)

    config = load_json(CONFIG, {"labels": []})
    labels = config.get("labels", [])
    if not labels:
        print("no labels configured in data/labels.json", file=sys.stderr)
        return 1

    history = load_json(DATA, {"repo": REPO, "labels": {}, "series": {}})
    history.setdefault("labels", {})
    history.setdefault("series", {})

    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    for entry in labels:
        label = entry["id"]
        print(f"collecting {label}", flush=True)
        history["labels"][label] = entry

        series = history["series"].setdefault(
            label, {"snapshots": [], "backfilled": False}
        )

        # First time we see this label: reconstruct monthly history.
        if not series.get("backfilled"):
            try:
                back = backfill_label(label, token)
                existing_dates = {p["date"] for p in series["snapshots"]}
                for p in back:
                    if p["date"] not in existing_dates:
                        series["snapshots"].append(
                            {
                                "date": p["date"],
                                "open_issues": p["open_issues"],
                                "reconstructed": True,
                            }
                        )
                series["backfilled"] = True
            except ValueError as e:
                print(f"  skipping backfill: {e}", flush=True)

        # Today's exact snapshot (replaces any earlier same-day point).
        try:
            snap = snapshot_label(label, token)
        except ValueError as e:
            print(f"  {e} — skipping label", flush=True)
            continue
        snap["date"] = today
        snap["collected_at"] = now
        series["snapshots"] = [
            s for s in series["snapshots"] if s.get("date") != today
        ]
        series["snapshots"].append(snap)
        series["snapshots"].sort(key=lambda s: s["date"])

    history["updated_at"] = now
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(history, indent=2) + "\n")
    print(f"wrote {DATA} ({sum(len(s['snapshots']) for s in history['series'].values())} points)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
