#!/usr/bin/env python3
"""
flang/pulse lines-of-code collector.

Snapshots the size of the Fortran stack (the `flang/` subtree of
llvm/llvm-project) and appends a dated point to data/loc.json.

Checking out all of llvm-project is expensive, so this does the cheap
thing: a *blobless, depth-1, sparse* checkout that downloads only the
`flang/` tree at HEAD — tens of MB instead of multiple GB — then runs
`cloc` over it. Like the issue collector, it records forward snapshots;
each run adds one point, building a precise size-over-time series.

Requirements: `git` (>= 2.27 for cone sparse-checkout) and `cloc` on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO_URL = "https://github.com/llvm/llvm-project.git"
SUBTREE = "flang"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "loc.json"

# Languages worth breaking out on the dashboard; everything else folds
# into "Other". Keys are cloc's language names.
TRACKED_LANGS = ["C++", "C/C++ Header", "Fortran 90", "Fortran 77", "Python", "CMake"]


def run(cmd: list[str], cwd: str | None = None) -> str:
    print("  $ " + " ".join(cmd), flush=True)
    res = subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    )
    return res.stdout


def sparse_checkout(dest: str) -> str:
    """Cheaply materialise only llvm-project/flang/ at HEAD. Returns HEAD sha."""
    run(["git", "clone", "--filter=blob:none", "--no-checkout",
         "--depth=1", REPO_URL, dest])
    run(["git", "sparse-checkout", "init", "--cone"], cwd=dest)
    run(["git", "sparse-checkout", "set", SUBTREE], cwd=dest)
    run(["git", "checkout"], cwd=dest)
    return run(["git", "rev-parse", "HEAD"], cwd=dest).strip()


def count_loc(subtree_path: str) -> dict:
    """Run cloc and return totals plus a per-language code-line breakdown."""
    raw = run(["cloc", "--json", "--quiet", subtree_path])
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


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main() -> int:
    if not shutil.which("cloc"):
        print("error: cloc not found on PATH", file=sys.stderr)
        return 1

    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    tmp = tempfile.mkdtemp(prefix="flang-loc-")
    try:
        print(f"sparse-checking out {REPO_URL}#{SUBTREE}", flush=True)
        head = sparse_checkout(tmp)
        print(f"counting lines in {SUBTREE}/ (HEAD {head[:10]})", flush=True)
        counts = count_loc(str(Path(tmp) / SUBTREE))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    snap = {"date": today, "head_sha": head, "collected_at": now, **counts}

    history = load_json(DATA, {"repo": "llvm/llvm-project", "path": SUBTREE,
                               "snapshots": []})
    history.setdefault("snapshots", [])
    # one point per day — replace any earlier same-day snapshot
    history["snapshots"] = [
        s for s in history["snapshots"] if s.get("date") != today
    ]
    history["snapshots"].append(snap)
    history["snapshots"].sort(key=lambda s: s["date"])
    history["updated_at"] = now

    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(history, indent=2) + "\n")
    print(f"wrote {DATA}: {counts['code']:,} code lines across "
          f"{counts['files']:,} files", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
