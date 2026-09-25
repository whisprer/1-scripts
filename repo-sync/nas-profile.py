#!/usr/bin/env python3
"""
nas-profile.py  -  READ ONLY.  Where does 'dupes' actually spend its time?

Takes a random sample of the repo copies on the share, times each step of the check
separately, compares against a local repo, and projects the whole run. Nothing is
written, deleted or pushed. Run it next to repo_sync.py:

    py -3 nas-profile.py --root "Z:\\" --p52-base "D:\\code" --nas-base "Z:\\GitHub-Repos"
    py -3 nas-profile.py --sample 30            look at more copies
    py -3 nas-profile.py --sweep 40             find the --jobs the share is happiest at

Run it when nothing else is hammering the share, or the numbers measure the other job.

It reuses repo_sync.py's own git wrapper, so what it measures is what dupes does.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import repo_sync as R
except ImportError:
    sys.exit("put this next to repo_sync.py")


def tty() -> bool:
    try:
        return bool(sys.stdout.isatty())     # a rewriting line smears when redirected
    except (AttributeError, ValueError):
        return False


def clock(fn):
    t = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t), out


def count_files(p: Path, cap: int = 200_000) -> tuple[int, int]:
    """(files in the working tree, files under .git) - what a walk has to get through."""
    work = git = 0
    for dirpath, dirnames, filenames in os.walk(R.longpath(p)):
        rel = os.path.relpath(dirpath, R.longpath(p)).replace("\\", "/")
        if rel.split("/")[0] == ".git":
            git += len(filenames)
        else:
            dirnames[:] = [d for d in dirnames if d not in R.CACHE_DIR_NAMES]
            work += len(filenames)
        if work + git > cap:
            break
    return work, git


def profile_one(git: R.Git, p: Path) -> dict:
    d: dict = {"path": p}
    d["t_refs"], r = clock(lambda: git.run(p, "rev-list", "--exclude=refs/repo-sync/*",
                                           "--all", "--reflog", "--no-walk"))
    d["tips"] = len(set(r.out.split()))
    d["t_tags"], _ = clock(lambda: git.run(p, "for-each-ref", "--format=%(objectname)", "refs/tags"))
    d["t_roots"], _ = clock(lambda: R.root_commits(git, p))
    d["t_status"], res = clock(lambda: git.run(
        p, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
        "--ignored=traditional", "--no-renames"))
    ign = [i for i in res.out.split("\x00") if i.startswith("!! ")]
    d["ignored_entries"] = len(ign)
    d["ignored_dirs"] = sum(1 for i in ign if i.endswith("/"))
    d["t_count"], counted = clock(lambda: count_files(p))
    d["work_files"], d["git_files"] = counted
    d["total"] = d["t_refs"] + d["t_tags"] + d["t_roots"] + d["t_status"]
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="measure what makes 'dupes' slow on this share")
    ap.add_argument("--root", default=R.DEFAULT_NAS_ROOT)
    ap.add_argument("--p52-base", default=R.DEFAULT_P52_BASE)
    ap.add_argument("--nas-base", default=R.DEFAULT_NAS_BASE)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--sample", type=int, default=15)
    ap.add_argument("--jobs", type=int, default=48, help="the --jobs you'd use for the real run")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sweep", type=int, default=0, metavar="N",
                    help="also re-check N copies at 4/8/16/32/48/64 workers to find where the share saturates")
    a = ap.parse_args()

    git = R.Git(R.find_git())
    root, p52 = Path(a.root), Path(a.p52_base)
    if not os.path.isdir(R.longpath(root)):
        sys.exit(f"--root {root} not found")

    live = []
    for base in (p52, Path(a.nas_base)):
        if os.path.isdir(R.longpath(base)):
            try:
                live += [base / e.name for e in os.scandir(R.longpath(base))
                         if e.is_dir(follow_symlinks=False) and os.path.isdir(R.longpath(base / e.name / ".git"))]
            except OSError:
                pass
    exclude = {R._real(x) for x in live}
    print(f"Finding copies under {root} ...")
    cands = R.find_repos([root], a.depth, exclude)
    if not cands:
        sys.exit(f"no repo copies found under {root}")
    random.seed(a.seed)
    sample = random.sample(cands, min(a.sample, len(cands)))
    print(f"{len(cands)} copies on the share; timing {len(sample)} of them, one at a time.\n")

    rows = []
    for i, p in enumerate(sample, 1):
        if tty():
            print(f"  {i}/{len(sample)}  {str(p.name)[:40]} ...".ljust(70), end="\r", flush=True)
        try:
            rows.append(profile_one(git, p))
        except OSError as e:
            print(f"  skipped {p}: {e}")
    if tty():
        print(" " * 70, end="\r")
    if not rows:
        sys.exit("nothing could be measured")

    # a local repo, for comparison
    local = None
    try:
        for e in os.scandir(R.longpath(p52)):
            if e.is_dir(follow_symlinks=False) and os.path.isdir(R.longpath(p52 / e.name / ".git")):
                local = profile_one(git, p52 / e.name)
                break
    except OSError:
        pass

    def med(key: str) -> float:
        return statistics.median(r[key] for r in rows)

    print(f"{'copy':<34}{'files':>8}{'refs':>8}{'roots':>8}{'status':>9}{'total':>9}")
    print("-" * 76)
    for r in sorted(rows, key=lambda x: x["total"], reverse=True):
        name = str(r["path"].name)[:32]
        print(f"{name:<34}{r['work_files'] + r['git_files']:>8}{r['t_refs']:>8.2f}"
              f"{r['t_roots']:>8.2f}{r['t_status']:>9.2f}{r['total']:>9.2f}")
    print("-" * 76)
    print(f"{'median':<34}{statistics.median(r['work_files'] + r['git_files'] for r in rows):>8.0f}"
          f"{med('t_refs'):>8.2f}{med('t_roots'):>8.2f}{med('t_status'):>9.2f}{med('total'):>9.2f}")

    if local:
        print(f"\nSame steps on a local repo ({local['path'].name}, "
              f"{local['work_files'] + local['git_files']} files): "
              f"status {local['t_status']:.2f}s, total {local['total']:.2f}s")
        if local["t_status"] > 0:
            print(f"  -> the share is about {med('t_status') / local['t_status']:.0f}x slower per check")

    totals = sorted(r["total"] for r in rows)
    mean = statistics.mean(totals)
    heavy = sum(totals[-max(1, len(totals) // 3):]) / sum(totals) * 100 if sum(totals) else 0
    print(f"\nPer copy: median {statistics.median(totals):.1f}s, mean {mean:.1f}s, worst {totals[-1]:.1f}s.")
    print(f"The slowest third of them is {heavy:.0f}% of all the time, which is why the MEAN is what")
    print("the projection below uses - a median would flatter it enormously.")
    share = sum(r["t_status"] for r in rows) / sum(totals) * 100 if sum(totals) else 0
    print(f"git status (the working-tree walk) is {share:.0f}% of the total.")

    serial = mean * len(cands)
    print(f"\nProjection for all {len(cands)} copies (mean-based):")
    for j in sorted({8, 16, 32, a.jobs, 96}):
        if j > 0:
            print(f"   --jobs {j:<4} about {R.fmt_duration(serial / j)}"
                  + ("   <- what you asked about" if j == a.jobs else ""))
    print("\nThat assumes the share keeps up as workers are added. It often doesn't - see the sweep.")

    if a.sweep:
        print("\nConcurrency sweep - the same copies re-checked at each setting.")
        print("More workers stops helping once the NAS is saturated, and past that it gets worse.\n")
        pool = random.sample(cands, min(a.sweep, len(cands)))

        def one(q: Path) -> None:
            git.run(q, "rev-list", "--exclude=refs/repo-sync/*", "--all", "--reflog", "--no-walk")
            git.run(q, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
                    "--ignored=traditional", "--no-renames")

        with ThreadPoolExecutor(max_workers=8) as ex:      # warm up: the first pass pays for
            list(ex.map(one, pool))                        # caches the later ones get free
        settings = (4, 8, 16, 32, 48, 64)
        rates: list[tuple[int, float]] = []
        for j in settings:
            if tty():
                print(f"   measuring --jobs {j} ...".ljust(60), end="\r", flush=True)
            t = time.perf_counter()
            with ThreadPoolExecutor(max_workers=j) as ex:
                list(ex.map(one, pool))
            el = time.perf_counter() - t
            rates.append((j, len(pool) / el if el else 0.0))
        if tty():
            print(" " * 60, end="\r")
        top = max(r for _, r in rates) or 1.0             # bars scale to the best, once all are in
        for j, rate in rates:
            print(f"   --jobs {j:<3} {rate:6.2f} copies/sec  {'#' * int(rate / top * 40)}")
        best_j, best_rate = max(rates, key=lambda x: x[1])
        print(f"\n   Best here: --jobs {best_j} "
              f"({R.fmt_duration(len(cands) / best_rate) if best_rate else '?'} for all "
              f"{len(cands)}, refs+status only).")
        print("   If the rate stops climbing, the share is the limit and more workers won't help.")

    worst = max(rows, key=lambda r: r["ignored_entries"])
    if worst["ignored_entries"] > 50:
        print(f"\nHeads-up: {worst['path'].name} has {worst['ignored_entries']} ignored entries "
              f"({worst['ignored_dirs']} of them folders). Ignored files get compared against the live\n"
              f"repo one by one, so copies like that are the slow tail. --ignore '<pattern>' skips a repo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
