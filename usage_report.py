#!/usr/bin/env python3
"""
usage_report.py — Free-tier usage dashboard for Supabase, NeonDB, and Firebase.

Usage:
    python usage_report.py
    python usage_report.py --json        # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, ".")
from env_loader import load_env_file

load_env_file()

# ── Free-tier limits ──────────────────────────────────────────────────────────
SUPABASE_FREE_DB_BYTES   = 500 * 1024 * 1024        # 500 MB per project
NEONDB_FREE_STORAGE_BYTES = 512 * 1024 * 1024        # 0.5 GB
FIREBASE_FREE_READS_DAY  = 50_000
FIREBASE_FREE_WRITES_DAY = 20_000
FIREBASE_FREE_STORAGE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _pct(used: int, limit: int) -> str:
    p = used / limit * 100 if limit else 0
    bar_len = 20
    filled = int(bar_len * p / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    warn = " ⚠" if p >= 80 else (" !" if p >= 60 else "")
    return f"[{bar}] {p:.1f}%{warn}"


def _connect(url: str):
    import psycopg
    return psycopg.connect(url, connect_timeout=15)


TABLE_SIZE_SQL = """
    SELECT
        relname AS table_name,
        pg_total_relation_size(c.oid) AS total_bytes,
        pg_relation_size(c.oid) AS data_bytes,
        COALESCE((SELECT reltuples::bigint FROM pg_class WHERE oid = c.oid), 0) AS est_rows
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname = 'public'
    ORDER BY total_bytes DESC;
"""


def query_db_stats(url: str) -> Tuple[int, List[dict]]:
    """Returns (total_db_bytes, list of table stats)."""
    conn = _connect(url)
    cur = conn.cursor()
    cur.execute("SELECT pg_database_size(current_database())")
    db_bytes = cur.fetchone()[0]
    cur.execute(TABLE_SIZE_SQL)
    rows = cur.fetchall()
    tables = [
        {"table": r[0], "total_bytes": r[1], "data_bytes": r[2], "est_rows": r[3]}
        for r in rows
    ]
    conn.close()
    return db_bytes, tables


# ── Supabase ──────────────────────────────────────────────────────────────────

SUPABASE_PROJECTS = {
    "marketsdb-manager  (catalog/store)":  os.getenv("DATABASE_URL_MANAGER", ""),
    "marketsdb-manager2 (inference/audit)": os.getenv("DATABASE_URL_COMMON_INFERENCE", ""),
}


def report_supabase(verbose: bool = False) -> dict:
    results = {}
    print("\n" + "=" * 60)
    print("  SUPABASE  (free tier: 500 MB / project)")
    print("=" * 60)

    for label, url in SUPABASE_PROJECTS.items():
        if not url:
            print(f"\n  {label}: URL not set — skipping")
            continue
        try:
            db_bytes, tables = query_db_stats(url)
            pct_bar = _pct(db_bytes, SUPABASE_FREE_DB_BYTES)
            print(f"\n  {label}")
            print(f"  DB size : {_fmt_bytes(db_bytes)} / {_fmt_bytes(SUPABASE_FREE_DB_BYTES)}")
            print(f"  Usage   : {pct_bar}")
            if verbose:
                for t in tables:
                    print(f"    {t['table']:45s} {_fmt_bytes(t['total_bytes']):>10s}  ~{t['est_rows']:,} rows")
            results[label] = {"db_bytes": db_bytes, "tables": tables}
        except Exception as exc:
            print(f"  {label}: ERROR => {exc}")

    return results


# ── NeonDB ────────────────────────────────────────────────────────────────────

NEON_MARKET_URLS: Dict[str, str] = {
    k.replace("DATABASE_URL_", ""): v
    for k, v in os.environ.items()
    if k.startswith("DATABASE_URL_")
    and "neon.tech" in v
    and not k.startswith("DATABASE_URL_COMMON")
}


def report_neondb(verbose: bool = False) -> dict:
    results = {}
    print("\n" + "=" * 60)
    print(f"  NEONDB  ({len(NEON_MARKET_URLS)} databases | free tier: 0.5 GB total)")
    print("=" * 60)

    total_bytes = 0
    market_stats = []

    for market, url in sorted(NEON_MARKET_URLS.items()):
        try:
            db_bytes, tables = query_db_stats(url)
            total_bytes += db_bytes

            offers_row  = next((t for t in tables if t["table"] == "offers"), None)
            history_row = next((t for t in tables if t["table"] == "price_history"), None)
            offers_count  = offers_row["est_rows"] if offers_row else 0
            history_count = history_row["est_rows"] if history_row else 0

            market_stats.append({
                "market": market,
                "db_bytes": db_bytes,
                "offers": offers_count,
                "price_history": history_count,
            })
            results[market] = {"db_bytes": db_bytes, "tables": tables}
        except Exception as exc:
            print(f"  {market}: ERROR => {exc}")

    # Sort by size descending
    market_stats.sort(key=lambda x: x["db_bytes"], reverse=True)

    print(f"\n  {'Market':<20} {'Size':>10}  {'Offers':>8}  {'History':>8}")
    print(f"  {'-'*20} {'-'*10}  {'-'*8}  {'-'*8}")
    for s in market_stats:
        print(f"  {s['market']:<20} {_fmt_bytes(s['db_bytes']):>10}  {s['offers']:>8,}  {s['price_history']:>8,}")

    print(f"\n  Total across all NeonDB: {_fmt_bytes(total_bytes)}")
    print(f"  Free tier usage        : {_pct(total_bytes, NEONDB_FREE_STORAGE_BYTES)}")
    print(f"  (Note: NeonDB charges per-project; each DB has its own 0.5 GB limit)")

    for s in market_stats:
        bar = _pct(s["db_bytes"], NEONDB_FREE_STORAGE_BYTES)
        print(f"    {s['market']:<20} {bar}")

    results["_total_bytes"] = total_bytes
    return results


# ── Firebase estimate ─────────────────────────────────────────────────────────

def report_firebase(neon_results: dict) -> dict:
    """
    Estimate Firebase Firestore usage based on the price_history rows in NeonDB.
    Each price_history row = 1 write to Firestore when exported.
    Each unique barcode record read daily = 1 Firestore read.
    """
    print("\n" + "=" * 60)
    print("  FIREBASE FIRESTORE  (free: 50K reads/day · 20K writes/day · 1 GB)")
    print("=" * 60)

    total_offers  = 0
    total_history = 0

    for market, data in neon_results.items():
        if market.startswith("_"):
            continue
        for t in data.get("tables", []):
            if t["table"] == "offers":
                total_offers  += t["est_rows"]
            if t["table"] == "price_history":
                total_history += t["est_rows"]

    # Estimates
    # Writes: each price_history row = 1 write when exported to Firestore.
    # If you export daily, writes ≈ new rows added per day.
    # Conservative: assume 10% of offers change price daily.
    est_daily_writes = int(total_offers * 0.10)
    # Reads: assume your app reads each unique offer once per session,
    # and you have some daily active users.
    est_stored_docs  = total_offers  # one Firestore doc per active offer

    print(f"\n  Data in NeonDB (source for Firebase exports):")
    print(f"    Total offers (active)  : {total_offers:>10,}")
    print(f"    Total price history    : {total_history:>10,}")
    print(f"\n  Firebase estimates:")
    print(f"    Stored documents       : ~{est_stored_docs:,}  (one per active offer)")
    print(f"    Est. daily writes      : ~{est_daily_writes:,}  (10% price refresh rate)")
    print(f"    Write headroom/day     : {_pct(est_daily_writes, FIREBASE_FREE_WRITES_DAY)}")
    print(f"\n  Tip — to reduce Firebase reads/writes:")
    print(f"    • Only export offers where price changed since last export")
    print(f"    • Batch Firestore writes (up to 500 ops per batch)")
    print(f"    • Use Firestore onSnapshot listeners instead of polling reads")
    print(f"    • Cache reads client-side (Firestore offline persistence)")

    return {
        "total_offers": total_offers,
        "total_price_history": total_history,
        "est_daily_writes": est_daily_writes,
        "est_stored_docs": est_stored_docs,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Database usage report")
    parser.add_argument("--json",    action="store_true", help="Output JSON")
    parser.add_argument("--verbose", action="store_true", help="Show per-table breakdown")
    parser.add_argument(
        "--only",
        choices=["supabase", "neondb", "firebase"],
        help="Run only one section",
    )
    args = parser.parse_args()

    report = {}

    run_all = args.only is None

    if run_all or args.only == "supabase":
        report["supabase"] = report_supabase(verbose=args.verbose)

    if run_all or args.only in ("neondb", "firebase"):
        report["neondb"] = report_neondb(verbose=args.verbose)

    if run_all or args.only == "firebase":
        neon_data = report.get("neondb", {})
        report["firebase"] = report_firebase(neon_data)

    if args.json:
        # Strip non-serialisable objects before dumping
        def _clean(obj):
            if isinstance(obj, dict):
                return {k: _clean(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_clean(i) for i in obj]
            if isinstance(obj, (int, float, str, bool, type(None))):
                return obj
            return str(obj)
        print(json.dumps(_clean(report), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
