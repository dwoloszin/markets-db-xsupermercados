#!/usr/bin/env python3
"""
sync_app_offers.py — Syncs all NeonDB market offers into a single flat
`app_offers` table in marketsdb-manager2 (Supabase).

Usage:
    python sync_app_offers.py              # upsert sync
    python sync_app_offers.py --dry-run    # count rows only, no writes
    python sync_app_offers.py --truncate   # wipe table before full reload
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, ".")
from env_loader import load_env_file

load_env_file()

import psycopg

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_URL = os.getenv("DATABASE_URL_COMMON_INFERENCE", "")  # marketsdb-manager2

MARKET_URLS: Dict[str, str] = {
    k.replace("DATABASE_URL_", "").title(): v
    for k, v in os.environ.items()
    if k.startswith("DATABASE_URL_")
    and v
    and "neon.tech" in v
    and not k.startswith("DATABASE_URL_COMMON")
    and k != "DATABASE_URL_MANAGER"
}

BATCH_SIZE = 500

_CEP_RE = re.compile(r"\d{5}-\d{3}")

# ── DDL ───────────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_offers (
    offer_id        TEXT PRIMARY KEY,
    barcode         TEXT,
    price           DOUBLE PRECISION,
    quantity        INTEGER,
    store_name      TEXT,
    store_id        TEXT,
    store_cep       TEXT,
    date_recorded   TIMESTAMP,
    notes           TEXT,
    product_url     TEXT,
    image_url       TEXT,
    product_name    TEXT,
    brand           TEXT,
    regular_price   DOUBLE PRECISION,
    promo_price     DOUBLE PRECISION,
    market_name     TEXT
);

CREATE INDEX IF NOT EXISTS app_offers_barcode_idx ON app_offers (barcode);
CREATE INDEX IF NOT EXISTS app_offers_market_idx  ON app_offers (market_name);
CREATE INDEX IF NOT EXISTS app_offers_price_idx   ON app_offers (price);
"""

UPSERT_SQL = """
INSERT INTO app_offers (
    offer_id, barcode, price, quantity, store_name, store_id, store_cep,
    date_recorded, notes, product_url, image_url, product_name,
    brand, regular_price, promo_price, market_name
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s
)
ON CONFLICT (offer_id) DO UPDATE SET
    barcode       = EXCLUDED.barcode,
    price         = EXCLUDED.price,
    quantity      = EXCLUDED.quantity,
    store_name    = EXCLUDED.store_name,
    store_id      = EXCLUDED.store_id,
    store_cep     = EXCLUDED.store_cep,
    date_recorded = EXCLUDED.date_recorded,
    notes         = EXCLUDED.notes,
    product_url   = EXCLUDED.product_url,
    image_url     = EXCLUDED.image_url,
    product_name  = EXCLUDED.product_name,
    brand         = EXCLUDED.brand,
    regular_price = EXCLUDED.regular_price,
    promo_price   = EXCLUDED.promo_price,
    market_name   = EXCLUDED.market_name;
"""

# Price logic:
#   Use promo_price when it exists AND (promo_end_at IS NULL OR promo_end_at > NOW()).
#   If promo_end_at is set and already expired, fall back to regular_price.
# Quantity logic:
#   Use promo_min_quantity when set, else 1.
FETCH_SQL = """
SELECT
    id,
    barcode,
    CASE
        WHEN promo_price IS NOT NULL
             AND (promo_end_at IS NULL OR promo_end_at > NOW())
        THEN promo_price
        ELSE regular_price
    END                                       AS price,
    COALESCE(promo_min_quantity, 1)           AS quantity,
    market_name,
    store_id,
    last_updated,
    product_url,
    image_url,
    product_name,
    brand,
    regular_price,
    promo_price
FROM offers
WHERE barcode IS NOT NULL
  AND COALESCE(promo_price, regular_price) IS NOT NULL
  AND COALESCE(promo_price, regular_price) >= 0.05;
"""

FETCH_STORE_MAPPINGS_SQL = """
SELECT market_name, store_id, store_address FROM store_mappings;
"""

DB_SIZE_SQL = "SELECT pg_database_size(current_database())"


def _connect(url: str, label: str = "") -> psycopg.Connection:
    try:
        return psycopg.connect(url, connect_timeout=20, prepare_threshold=None)
    except Exception as exc:
        print(f"  Connection failed ({label}): {exc}")
        raise


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.1f} KB"


def _extract_cep(address: Optional[str]) -> str:
    """Extract Brazilian CEP (XXXXX-XXX) from an address string. Returns '' if not found."""
    if not address:
        return ""
    m = _CEP_RE.search(address)
    return m.group(0) if m else ""


def _load_store_ceps(manager_url: str) -> Dict[Tuple[str, str], str]:
    """Load (market_name, store_id) → CEP from store_mappings in Supabase manager."""
    result: Dict[Tuple[str, str], str] = {}
    if not manager_url:
        return result
    try:
        conn = _connect(manager_url, "manager/store_mappings")
        cur = conn.cursor()
        cur.execute(FETCH_STORE_MAPPINGS_SQL)
        for market_name, store_id, store_address in cur.fetchall():
            result[(market_name, store_id)] = _extract_cep(store_address)
        conn.close()
        print(f"  Loaded {len(result)} store mappings from manager")
    except Exception as exc:
        print(f"  Warning: could not load store_mappings: {exc}")
    return result


def _fetch_market_offers(url: str, label: str) -> List[tuple]:
    conn = _connect(url, label)
    cur = conn.cursor()
    cur.execute(FETCH_SQL)
    rows = cur.fetchall()
    conn.close()
    return rows


def _map_row(row: tuple, store_ceps: Dict[Tuple[str, str], str]) -> tuple:
    (
        offer_id, barcode, price, quantity, market_name, store_id,
        last_updated, product_url, image_url, product_name, brand,
        regular_price, promo_price,
    ) = row

    store_cep = store_ceps.get((market_name, store_id), "") if store_id else ""

    return (
        offer_id,       # offer_id
        barcode,        # barcode
        price,          # price  (promo if valid, else regular)
        quantity,       # quantity  (promo_min_quantity or 1)
        market_name,    # store_name
        store_id,       # store_id
        store_cep,      # store_cep  (extracted from store_mappings.store_address)
        last_updated,   # date_recorded
        None,           # notes
        product_url,    # product_url
        image_url,      # image_url
        product_name,   # product_name
        brand,          # brand
        regular_price,  # regular_price
        promo_price,    # promo_price
        market_name,    # market_name
    )


def prepare_sync() -> Dict[Tuple[str, str], str]:
    """
    Ensure app_offers table exists in manager2 and return the store CEPs dict.
    Call once before the market scraping loop starts.
    Raises on connection failure.
    """
    if not TARGET_URL:
        raise RuntimeError("DATABASE_URL_COMMON_INFERENCE is not set")
    conn = _connect(TARGET_URL, "manager2/app_offers")
    cur = conn.cursor()
    for stmt in CREATE_TABLE_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)
    conn.commit()
    conn.close()
    manager_url = os.getenv("DATABASE_URL_MANAGER", "")
    return _load_store_ceps(manager_url)


def sync_one_market(market_url: str, store_ceps: Dict[Tuple[str, str], str]) -> int:
    """
    Replace one market's rows in app_offers atomically:
      1. DELETE all rows for this market_name
      2. INSERT fresh rows from NeonDB (barcode-only)
    Other markets' data is never touched.
    Returns the number of rows inserted.
    """
    if not TARGET_URL or not market_url:
        return 0
    rows = _fetch_market_offers(market_url, "")
    if not rows:
        return 0
    mapped = [_map_row(r, store_ceps) for r in rows]
    # market_name is position 15 (last column) in the mapped tuple
    market_name = mapped[0][15]
    conn = _connect(TARGET_URL, "manager2/app_offers")
    cur = conn.cursor()
    cur.execute("DELETE FROM app_offers WHERE market_name = %s", (market_name,))
    for i in range(0, len(mapped), BATCH_SIZE):
        cur.executemany(UPSERT_SQL, mapped[i : i + BATCH_SIZE])
    conn.commit()
    conn.close()
    return len(mapped)


def run_sync(truncate: bool = True) -> Dict[str, int]:
    """
    End-of-run redundant full sync.
    truncate=True  → wipe the whole table then re-insert all markets (clean final state).
    truncate=False → per-market replace (preserves data for markets that weren't processed).
    Returns {"upserted": N, "db_bytes": N, "elapsed": N}.
    """
    if not TARGET_URL:
        raise RuntimeError("DATABASE_URL_COMMON_INFERENCE is not set")

    manager_url = os.getenv("DATABASE_URL_MANAGER", "")
    store_ceps = _load_store_ceps(manager_url)

    total_upserted = 0
    t0 = time.time()

    if truncate:
        conn = _connect(TARGET_URL, "manager2/app_offers")
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE app_offers")
        conn.commit()
        conn.close()

    for market_label, url in sorted(MARKET_URLS.items()):
        try:
            rows = _fetch_market_offers(url, market_label)
            if not rows:
                continue
            mapped = [_map_row(r, store_ceps) for r in rows]
            conn = _connect(TARGET_URL, "manager2/app_offers")
            cur = conn.cursor()
            if not truncate:
                market_name = mapped[0][15]
                cur.execute("DELETE FROM app_offers WHERE market_name = %s", (market_name,))
            for i in range(0, len(mapped), BATCH_SIZE):
                cur.executemany(UPSERT_SQL, mapped[i : i + BATCH_SIZE])
            conn.commit()
            conn.close()
            total_upserted += len(mapped)
            print(f"  app_offers sync: {market_label:<20} {len(mapped):>7,} rows")
        except Exception as exc:
            print(f"  app_offers sync: {market_label:<20} ERROR: {exc}")

    conn = _connect(TARGET_URL, "manager2/app_offers")
    cur = conn.cursor()
    cur.execute(DB_SIZE_SQL)
    db_bytes = cur.fetchone()[0]
    conn.close()

    elapsed = time.time() - t0
    print(
        f"  app_offers full sync complete: {total_upserted:,} rows in {elapsed:.1f}s "
        f"| manager2: {_fmt_bytes(db_bytes)}"
    )
    return {"upserted": total_upserted, "db_bytes": db_bytes, "elapsed": elapsed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync NeonDB offers → Supabase app_offers")
    parser.add_argument("--dry-run",  action="store_true", help="Count rows only, no writes")
    parser.add_argument("--truncate", action="store_true", help="Truncate table before sync")
    args = parser.parse_args()

    if not TARGET_URL:
        print("ERROR: DATABASE_URL_COMMON_INFERENCE is not set in .env")
        sys.exit(1)
    if not MARKET_URLS:
        print("ERROR: No market DATABASE_URL_* entries found in .env")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else ("TRUNCATE + FULL SYNC" if args.truncate else "UPSERT")
    print(f"Target : marketsdb-manager2 (DATABASE_URL_COMMON_INFERENCE)")
    print(f"Markets: {len(MARKET_URLS)}")
    print(f"Mode   : {mode}")

    manager_url = os.getenv("DATABASE_URL_MANAGER", "")
    store_ceps = _load_store_ceps(manager_url)

    if not args.dry_run:
        target_conn = _connect(TARGET_URL, "manager2/app_offers")
        target_cur = target_conn.cursor()

        for stmt in CREATE_TABLE_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                target_cur.execute(stmt)
        target_conn.commit()
        print("  Table app_offers ready")

        if args.truncate:
            target_cur.execute("TRUNCATE TABLE app_offers")
            target_conn.commit()
            print("  Truncated app_offers")

    total_rows = 0
    total_upserted = 0
    t0 = time.time()

    for market_label, url in sorted(MARKET_URLS.items()):
        try:
            rows = _fetch_market_offers(url, market_label)
            total_rows += len(rows)

            if args.dry_run:
                print(f"  {market_label:<20} {len(rows):>7,} offers")
                continue

            mapped = [_map_row(r, store_ceps) for r in rows]
            for i in range(0, len(mapped), BATCH_SIZE):
                target_cur.executemany(UPSERT_SQL, mapped[i : i + BATCH_SIZE])
                target_conn.commit()

            total_upserted += len(mapped)
            print(f"  {market_label:<20} {len(rows):>7,} offers  upserted")

        except Exception as exc:
            print(f"  {market_label:<20} ERROR: {exc}")

    elapsed = time.time() - t0

    if args.dry_run:
        print(f"\nDry run: {total_rows:,} total rows across {len(MARKET_URLS)} markets")
        return

    target_cur.execute(DB_SIZE_SQL)
    db_bytes = target_cur.fetchone()[0]
    target_cur.execute("SELECT COUNT(*) FROM app_offers")
    table_count = target_cur.fetchone()[0]
    target_conn.close()

    print(f"\n{'-'*50}")
    print(f"  Upserted : {total_upserted:,} rows in {elapsed:.1f}s")
    print(f"  app_offers table: {table_count:,} rows")
    print(f"  manager2 DB size: {_fmt_bytes(db_bytes)} / 500.0 MB")
    print(f"  Free tier usage : {db_bytes / (500*1024*1024)*100:.1f}%")


if __name__ == "__main__":
    main()
