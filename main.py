"""
main.py - Run market scrapers in parallel (one subprocess per market).

Each market runs as an independent process (memory isolated, own log file) and
writes to its own Neon database. Markets that share a backend/WAF (config
STORES[..]["group"]) run one at a time; everything else runs in parallel.

Usage:
    python -m main                                # all markets, default ZIP (config.SCRAPE_ZIP_CODE)
    python -m main --stores atacadao carrefour    # a subset
    python -m main --limit 100                    # test run (100 products per market)
    python -m main --zip 04646-000                # another region
    python -m main --log                          # per-market log files in logs/
    python -m main --enrich-only                  # only the barcode backfill steps
    python -m main --skip-enrich                  # scrape only
    python -m main --only-stale --stale-hours 24  # refresh only markets older than 24h
    python -m main --post                         # after scraping: mark-stale + prune price_history
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")

STORES = list(config.STORES)


def _build_cmd(store: str, args: argparse.Namespace) -> List[str]:
    cmd = [sys.executable, "-m", f"markets.{store}.scraper_{store}"]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.zip:
        cmd += ["--zip", args.zip]
    if args.env != ".env":
        cmd += ["--env", args.env]
    if args.csv:
        cmd += ["--csv"]
    if args.workers != 12:
        cmd += ["--workers", str(args.workers)]
    if args.skip_enrich:
        cmd += ["--skip-enrich"]
    if args.enrich_only:
        cmd += ["--enrich-only"]
    return cmd


def _log(store: str, msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{store}] {msg}", flush=True)


def _tail(path: Path, lines: int = 30) -> None:
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            tail = f.readlines()[-lines:]
        print(f"\n--- last {lines} lines of {path.name} ---")
        for line in tail:
            print(f"  {line}", end="")
        print("--- end ---\n")
    except Exception:
        pass


def _run_store(store: str, cmd: List[str], log_path: Path, results: Dict[str, bool], durations: Dict[str, float],
               starts: Dict[str, float], lock: threading.Lock, log_enabled: bool,
               group_lock: Optional[threading.Lock]) -> None:
    if group_lock is not None and not group_lock.acquire(blocking=False):
        _log(store, "queued (shared backend busy) ...")
        group_lock.acquire()
    try:
        t0 = time.time()
        with lock:
            starts[store] = t0
        _log(store, f"started{'  (log -> ' + log_path.name + ')' if log_enabled else ''}")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            if log_enabled:
                with log_path.open("w", encoding="utf-8") as lf:
                    proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True, env=env)
            else:
                proc = subprocess.run(cmd, env=env)
            ok = proc.returncode == 0
            elapsed = time.time() - t0
            with lock:
                results[store] = ok
                durations[store] = elapsed
                starts.pop(store, None)
            _log(store, f"{'done' if ok else f'FAILED (exit {proc.returncode})'}  [{elapsed / 60:.1f} min]")
            if not ok and log_enabled:
                _tail(log_path)
        except Exception as exc:
            with lock:
                results[store] = False
                durations[store] = time.time() - t0
                starts.pop(store, None)
            _log(store, f"ERROR: {exc}")
    finally:
        if group_lock is not None:
            group_lock.release()


def _progress_pct(log_path: Optional[Path]) -> Optional[float]:
    if not log_path or not log_path.exists():
        return None
    try:
        with log_path.open(encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-100:]
    except Exception:
        return None
    latest = None
    for line in lines:
        for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)%", line):
            v = float(m.group(1))
            if 0 < v <= 100:
                latest = v
    return latest


def _monitor(stores, results, durations, starts, log_paths, lock, t_start, done_event) -> None:
    while not done_event.wait(timeout=30):
        with lock:
            completed = len(results)
            running = [s for s in stores if s in starts and s not in results]
        elapsed = time.time() - t_start
        print(f"[{datetime.now().strftime('%H:%M:%S')}] progress {completed}/{len(stores)} done | "
              f"running: {', '.join(running[:6]) or 'none'} | elapsed {elapsed / 60:.1f} min", flush=True)


def _filter_stale(stores: List[str], hours: int) -> List[str]:
    from db.db_manager import StoreDB
    now = datetime.now(timezone.utc)
    keep: List[str] = []
    print(f"\nChecking Neon freshness (threshold {hours}h) ...")
    for store in stores:
        env_key = str(config.STORES[store]["db_env"])
        if not os.environ.get(env_key):
            print(f"  {store:<16} no {env_key} - skip")
            continue
        try:
            db = StoreDB(store)
            last = db.last_update()
            db.close()
        except Exception as exc:
            print(f"  {store:<16} DB error ({exc.__class__.__name__}) - STALE")
            keep.append(store)
            continue
        if last is None:
            print(f"  {store:<16} empty - STALE")
            keep.append(store)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (now - last).total_seconds() / 3600
        if age >= hours:
            print(f"  {store:<16} {age:5.1f}h old - STALE")
            keep.append(store)
        else:
            print(f"  {store:<16} {age:5.1f}h old - fresh, skip")
    return keep


def _post_steps(stores: List[str]) -> None:
    from db.db_manager import StoreDB
    print("\nPost steps: mark stale offers unavailable + prune price history")
    for store in stores:
        try:
            db = StoreDB(store)
            db.mark_stale_unavailable(config.STALE_HOURS)
            db.prune_history(config.PRICE_HISTORY_KEEP_DAYS)
            db.close()
        except Exception as exc:
            print(f"  [{store}] post step error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run market scrapers in parallel.")
    parser.add_argument("--stores", nargs="+", default=None, choices=STORES, metavar="STORE",
                        help=f"Markets to run (default: all). Choices: {', '.join(STORES)}")
    parser.add_argument("--limit", type=int, default=config.SCRAPE_LIMIT, help="Max products per market (test)")
    parser.add_argument("--zip", type=str, default=None, help="CEP (default: config.SCRAPE_ZIP_CODE)")
    parser.add_argument("--workers", type=int, default=12, help="Threads for barcode enrichment")
    parser.add_argument("--csv", action="store_true", help="Also export a CSV per market")
    parser.add_argument("--env", type=str, default=".env")
    parser.add_argument("--log", action="store_true", help="Write per-market logs to logs/")
    parser.add_argument("--skip-enrich", action="store_true", help="Scrape only (no barcode backfill)")
    parser.add_argument("--enrich-only", action="store_true", help="Barcode backfill only (no scrape)")
    parser.add_argument("--only-stale", action="store_true", help="Run only markets whose data is older than --stale-hours")
    parser.add_argument("--stale-hours", type=int, default=24)
    parser.add_argument("--post", action="store_true", help="After scraping: mark-stale + prune for the markets that ran")
    parser.add_argument("--parallel", type=int, default=0, help="Max concurrent markets (0 = all at once)")
    args = parser.parse_args()

    from db.db_manager import load_env
    load_env(args.env)

    stores = args.stores or STORES
    stores = [s for s in stores if os.environ.get(str(config.STORES[s]["db_env"]))] or stores
    if args.enrich_only:
        stores = [s for s in stores if config.STORES[s]["enrich"]]
        if not stores:
            print("No selected market has a barcode enricher.")
            return
    if args.only_stale:
        stores = _filter_stale(stores, args.stale_hours)
        if not stores:
            print("All selected markets are fresh. Nothing to do.")
            return

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    print(f"\nRunning {len(stores)} market(s): {', '.join(stores)}"
          f"{'  [limit ' + str(args.limit) + ']' if args.limit else ''}"
          f"{'  [zip ' + args.zip + ']' if args.zip else '  [zip ' + config.SCRAPE_ZIP_CODE + ']'}\n")

    results: Dict[str, bool] = {}
    durations: Dict[str, float] = {}
    starts: Dict[str, float] = {}
    log_paths: Dict[str, Path] = {}
    lock = threading.Lock()
    group_locks: Dict[str, threading.Lock] = {}
    sem = threading.Semaphore(args.parallel) if args.parallel and args.parallel > 0 else None
    threads: List[threading.Thread] = []
    for store in stores:
        grp = config.STORES[store]["group"]
        glock = group_locks.setdefault(str(grp), threading.Lock()) if grp else None
        log_path = log_dir / f"{store}_{ts}.log"
        log_paths[store] = log_path
        cmd = _build_cmd(store, args)

        def target(store=store, cmd=cmd, log_path=log_path, glock=glock):
            if sem:
                sem.acquire()
            try:
                _run_store(store, cmd, log_path, results, durations, starts, lock, args.log, glock)
            finally:
                if sem:
                    sem.release()

        threads.append(threading.Thread(target=target, name=store, daemon=False))

    t_start = time.time()
    done = threading.Event()
    mon = threading.Thread(target=_monitor, args=(stores, results, durations, starts, log_paths, lock, t_start, done), daemon=True)
    for t in threads:
        t.start()
    mon.start()
    for t in threads:
        t.join()
    done.set()

    wall = time.time() - t_start
    print(f"\n{'=' * 60}\n  finished in {wall / 60:.1f} min")
    for s in stores:
        print(f"    - {s:<16} {'OK' if results.get(s) else 'FAILED':<7} {durations.get(s, 0) / 60:6.1f} min")
    failed = [s for s in stores if not results.get(s)]
    if failed:
        print(f"  FAILED: {', '.join(failed)}" + (f"  (see logs/<market>_{ts}.log)" if args.log else "  (re-run with --log)"))
    print("=" * 60)

    if args.post:
        _post_steps([s for s in stores if results.get(s)])

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
