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
network. Set LLVM_REPO to the clone path (default ./.llvm-cache) and
optionally ACTIVITY_MONTHS (default 36).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PATHS = ["flang", "flang-rt"]
ROOT = Path(__file__).resolve().parent.parent
REPO = Path(os.environ.get("LLVM_REPO", ROOT / ".llvm-cache"))
WINDOW_MONTHS = int(os.environ.get("ACTIVITY_MONTHS", "36"))

ACTIVITY = ROOT / "data" / "activity.json"
RELEASES = ROOT / "data" / "releases.json"
LOC = ROOT / "data" / "loc.json"

TRACKED_LANGS = ["C++", "C/C++ Header", "Fortran 90", "Fortran 77", "Python", "CMake"]


def git(*args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO), *args],
        check=True, capture_output=True, text=True,
    )
    return res.stdout


# ── monthly activity ──────────────────────────────────────────────────
def _fresh_month() -> dict:
    return {
        "commits": 0, "insertions": 0, "deletions": 0, "files": 0,
        "_authors": set(),
        "by_path": {p: {"commits": 0, "insertions": 0, "deletions": 0,
                        "files": 0} for p in PATHS},
    }


def _top_path(path: str) -> str | None:
    for p in PATHS:
        if path.startswith(p + "/"):
            return p
    return None


def collect_activity() -> dict:
    """Bucket per-month churn/commits/authors from `git log --numstat`."""
    # Pathspec after `--` limits the numstat diff to those subtrees, so the
    # insertion/deletion counts are already scoped to flang / flang-rt.
    out = git(
        "log", "--no-merges", f"--since={WINDOW_MONTHS} months ago",
        "--numstat", "--no-renames",
        "--pretty=format:@@@%H\t%aI\t%aE", "--", *PATHS,
    )

    months: dict[str, dict] = {}
    cur: dict | None = None
    cur_paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("@@@"):
            _, iso, email = line[3:].split("\t")
            cur = months.setdefault(iso[:7], _fresh_month())
            cur["commits"] += 1
            cur["_authors"].add(email.lower())
            cur_paths = set()
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

    rows = []
    for m in sorted(months):
        d = months[m]
        authors = d.pop("_authors")
        d["month"] = m
        d["authors"] = len(authors)
        d["churn"] = d["insertions"] + d["deletions"]
        d["net"] = d["insertions"] - d["deletions"]
        rows.append(d)
    return rows


def collect_totals() -> dict:
    """All-time scale figures over the two subtrees (commits/trees only)."""
    out = git("log", "--no-merges", "--pretty=format:%aI\t%aE", "--", *PATHS)
    lines = [ln for ln in out.splitlines() if ln]
    if not lines:
        return {}
    authors = {ln.split("\t")[1].lower() for ln in lines}
    dates = [ln.split("\t")[0] for ln in lines]  # newest first
    return {
        "commits": len(lines),
        "authors": len(authors),
        "first_commit": dates[-1][:10],
        "last_commit": dates[0][:10],
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

    print("collecting monthly activity", flush=True)
    months = collect_activity()
    totals = collect_totals()
    write_json(ACTIVITY, {
        "repo": "llvm/llvm-project", "paths": PATHS,
        "window_months": WINDOW_MONTHS, "head_sha": head,
        "updated_at": now, "months": months, "totals": totals,
    })
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
