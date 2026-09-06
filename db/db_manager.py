"""
db_manager.py - PostgreSQL (Neon) persistence, one database per market.

Design (borrowed from markets_db_farm, adapted for supermarkets):

  offers                current state per (store_id, product_id); pure upsert.
                        `barcode` is NEVER overwritten with NULL once known.
  price_history         append-only; a DB trigger writes a row on INSERT and on
                        UPDATE only when a price actually changes.
  store_info            the physical store chosen for the ZIP (address, coords).
  barcode_enrich_state  what we already tried to backfill (found/not_found),
                        so enrichers only fetch pages for NEW products.

Legacy data: the previous generation of this project kept an `offers` table
keyed by `id` (market_storehash_barcode). On first connect that table is
renamed to `offers_legacy` (nothing is deleted) and its barcodes are used to
seed the new rows (`seed_barcodes_from_legacy`). Drop it when you are happy:
    python -m db.db_manager drop-legacy <store>

CLI:
    python -m db.db_manager stats                      # coverage/freshness for every market
    python -m db.db_manager export <store>             # offers + price_history -> exports/
    python -m db.db_manager export-all-together        # one CSV, barcode-keyed, all markets
    python -m db.db_manager sync-hub                   # optional: app_offers table in DATABASE_URL_HUB
    python -m db.db_manager prune <store|all> --days 180
    python -m db.db_manager mark-stale <store|all> --hours 48
    python -m db.db_manager drop-legacy <store|all>
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")

from markets.common.gtin import normalize_gtin  # noqa: E402
from markets.common.offer import OFFER_FIELDS, norm_name  # noqa: E402

# ---------------------------------------------------------------------------
# .env loader (no dependency on python-dotenv)
# ---------------------------------------------------------------------------

def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines into os.environ (never overwrites existing vars)."""
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS offers (
    store_id                TEXT NOT NULL DEFAULT '',
    product_id              TEXT NOT NULL,
    market                  TEXT NOT NULL,
    product_name            TEXT NOT NULL,
    brand                   TEXT,
    category_path           TEXT,
    barcode                 TEXT,
    barcode_source          TEXT,
    regular_price           NUMERIC(10, 2),
    promo_price             NUMERIC(10, 2),
    discount_pct            NUMERIC(5, 1),
    promo_min_quantity      INTEGER,
    promo_end_at            TIMESTAMPTZ,
    offer_tag               TEXT,
    offer_name              TEXT,
    app_membership_required BOOLEAN,
    unit                    TEXT,
    is_available            BOOLEAN,
    stock                   INTEGER,
    product_url             TEXT,
    image_url               TEXT,
    scraped_at              TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (store_id, product_id)
);
CREATE INDEX IF NOT EXISTS offers_barcode_idx  ON offers (barcode);
CREATE INDEX IF NOT EXISTS offers_updated_idx  ON offers (updated_at);

CREATE TABLE IF NOT EXISTS price_history (
    id                  BIGSERIAL PRIMARY KEY,
    store_id            TEXT NOT NULL DEFAULT '',
    product_id          TEXT NOT NULL,
    barcode             TEXT,
    product_name        TEXT,
    regular_price       NUMERIC(10, 2),
    promo_price         NUMERIC(10, 2),
    discount_pct        NUMERIC(5, 1),
    promo_min_quantity  INTEGER,
    offer_tag           TEXT,
    offer_name          TEXT,
    is_available        BOOLEAN,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ph_product_idx  ON price_history (store_id, product_id);
CREATE INDEX IF NOT EXISTS ph_recorded_idx ON price_history (recorded_at DESC);

CREATE TABLE IF NOT EXISTS store_info (
    store_id        TEXT PRIMARY KEY,
    market          TEXT NOT NULL,
    store_name      TEXT,
    store_address   TEXT,
    store_city      TEXT,
    store_state     TEXT,
    store_zip       TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    query_zip       TEXT,
    payload         TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS barcode_enrich_state (
    store_id            TEXT NOT NULL DEFAULT '',
    product_id          TEXT NOT NULL,
    status              TEXT NOT NULL,
    http_status         INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 1,
    last_attempted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (store_id, product_id)
);

CREATE OR REPLACE FUNCTION trg_price_history()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'INSERT') OR (
        TG_OP = 'UPDATE' AND (
            NEW.regular_price      IS DISTINCT FROM OLD.regular_price OR
            NEW.promo_price        IS DISTINCT FROM OLD.promo_price OR
            NEW.promo_min_quantity IS DISTINCT FROM OLD.promo_min_quantity
        )
    ) THEN
        INSERT INTO price_history (
            store_id, product_id, barcode, product_name,
            regular_price, promo_price, discount_pct, promo_min_quantity,
            offer_tag, offer_name, is_available, recorded_at
        ) VALUES (
            NEW.store_id, NEW.product_id, NEW.barcode, NEW.product_name,
            NEW.regular_price, NEW.promo_price, NEW.discount_pct, NEW.promo_min_quantity,
            NEW.offer_tag, NEW.offer_name, NEW.is_available, NOW()
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS price_history_trigger ON offers;
CREATE TRIGGER price_history_trigger
AFTER INSERT OR UPDATE ON offers
FOR EACH ROW EXECUTE FUNCTION trg_price_history();
"""

_UPSERT_SQL = """
INSERT INTO offers (
    store_id, product_id, market, product_name, brand, category_path,
    barcode, barcode_source,
    regular_price, promo_price, discount_pct, promo_min_quantity, promo_end_at,
    offer_tag, offer_name, app_membership_required,
    unit, is_available, stock, product_url, image_url, scraped_at, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s, %s, NOW()
)
ON CONFLICT (store_id, product_id) DO UPDATE SET
    market                  = EXCLUDED.market,
    product_name            = EXCLUDED.product_name,
    brand                   = COALESCE(EXCLUDED.brand, offers.brand),
    category_path           = COALESCE(EXCLUDED.category_path, offers.category_path),
    barcode                 = COALESCE(EXCLUDED.barcode, offers.barcode),
    barcode_source          = CASE WHEN EXCLUDED.barcode IS NOT NULL THEN EXCLUDED.barcode_source ELSE offers.barcode_source END,
    regular_price           = EXCLUDED.regular_price,
    promo_price             = EXCLUDED.promo_price,
    discount_pct            = EXCLUDED.discount_pct,
    promo_min_quantity      = EXCLUDED.promo_min_quantity,
    promo_end_at            = EXCLUDED.promo_end_at,
    offer_tag               = EXCLUDED.offer_tag,
    offer_name              = EXCLUDED.offer_name,
    app_membership_required = EXCLUDED.app_membership_required,
    unit                    = COALESCE(EXCLUDED.unit, offers.unit),
    is_available            = EXCLUDED.is_available,
    stock                   = EXCLUDED.stock,
    product_url             = COALESCE(EXCLUDED.product_url, offers.product_url),
    image_url               = COALESCE(EXCLUDED.image_url, offers.image_url),
    scraped_at              = EXCLUDED.scraped_at,
    updated_at              = NOW()
"""


def _parse_ts(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # promo_end_at values decades away are cache TTLs, not promotions
    if dt.year > datetime.now(timezone.utc).year + 2:
        return None
    return dt


# ---------------------------------------------------------------------------
# StoreDB
# ---------------------------------------------------------------------------

class StoreDB:
    """One instance = one market database. Reconnects once on network drops."""

    def __init__(self, store_key: str, database_url: Optional[str] = None):
        import config
        meta = config.store_meta(store_key)
        self.store_key = store_key
        self.market_name = str(meta["name"])
        self.db_env = str(meta["db_env"])
        url = database_url or os.environ.get(self.db_env, "")
        if not url:
            raise RuntimeError(f"{self.db_env} is not set. Add it to .env (one Neon project per market).")
        self._url = url
        self._conn = self._connect()
        self._ensure_tables()

    # ---------------------------------------------------------------- basics
    def _connect(self) -> psycopg.Connection:
        # prepare_threshold=None: required for transaction poolers (Neon -pooler hosts)
        conn = psycopg.connect(self._url, connect_timeout=20, prepare_threshold=None)
        conn.autocommit = False
        return conn

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._connect()
        print("  DB reconnected.")

    def _run(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            print(f"  DB connection lost ({exc.__class__.__name__}) - reconnecting and retrying once...")
            self._reconnect()
            return fn(*args, **kwargs)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ----------------------------------------------------------- schema
    def _columns(self, cur: psycopg.Cursor, table: str) -> List[str]:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]

    def _table_exists(self, cur: psycopg.Cursor, table: str) -> bool:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        return cur.fetchone()[0] is not None

    def _ensure_tables(self) -> None:
        with self._conn.cursor() as cur:
            # legacy schema (offers.id PK, no product_id) -> rename, never drop
            cols = self._columns(cur, "offers")
            if cols and "product_id" not in cols and "id" in cols:
                suffix = "" if not self._table_exists(cur, "offers_legacy") else datetime.now().strftime("_%Y%m%d%H%M")
                cur.execute(f"ALTER TABLE offers RENAME TO offers_legacy{suffix}")
                print(f"  legacy offers table renamed to offers_legacy{suffix} (barcodes will be reused)")
                ph_cols = self._columns(cur, "price_history")
                if ph_cols and "offer_id" in ph_cols:
                    cur.execute(f"ALTER TABLE price_history RENAME TO price_history_legacy{suffix}")
                    print(f"  legacy price_history renamed to price_history_legacy{suffix}")
            cur.execute(_DDL)
        self._conn.commit()
        print(f"[{self.store_key}] DB ready ({self._url.split('@')[-1].split('/')[0]})")

    # ----------------------------------------------------------- store info
    def save_store_info(self, store_id: str, *, query_zip: str, name: Any = None, address: Any = None,
                        city: Any = None, state: Any = None, store_zip: Any = None,
                        latitude: Any = None, longitude: Any = None, payload: Any = None) -> None:
        import json

        def _f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO store_info (store_id, market, store_name, store_address, store_city, store_state,
                                            store_zip, latitude, longitude, query_zip, payload, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id) DO UPDATE SET
                        market = EXCLUDED.market,
                        store_name = COALESCE(EXCLUDED.store_name, store_info.store_name),
                        store_address = COALESCE(EXCLUDED.store_address, store_info.store_address),
                        store_city = COALESCE(EXCLUDED.store_city, store_info.store_city),
                        store_state = COALESCE(EXCLUDED.store_state, store_info.store_state),
                        store_zip = COALESCE(EXCLUDED.store_zip, store_info.store_zip),
                        latitude = COALESCE(EXCLUDED.latitude, store_info.latitude),
                        longitude = COALESCE(EXCLUDED.longitude, store_info.longitude),
                        query_zip = EXCLUDED.query_zip,
                        payload = COALESCE(EXCLUDED.payload, store_info.payload),
                        updated_at = NOW()
                    """,
                    (
                        str(store_id), self.store_key,
                        (str(name).strip() or None) if name else None,
                        (str(address).strip() or None) if address else None,
                        (str(city).strip() or None) if city else None,
                        (str(state).strip() or None) if state else None,
                        (str(store_zip).strip() or None) if store_zip else None,
                        _f(latitude), _f(longitude), query_zip,
                        json.dumps(payload, ensure_ascii=False, default=str)[:20000] if payload is not None else None,
                    ),
                )
            self._conn.commit()

        self._run(_impl)

    # ----------------------------------------------------------- save
    def save(self, offers: Sequence[Dict[str, Any]], batch_size: int = 500, verbose: bool = False) -> Dict[str, int]:
        """Upsert offers. Returns {"upserted", "skipped", "with_barcode"}."""
        rows: List[tuple] = []
        skipped = 0
        with_barcode = 0
        for o in offers:
            if not o:
                skipped += 1
                continue
            reg = o.get("regular_price")
            if reg is None or float(reg) <= 0:
                skipped += 1
                continue
            barcode = normalize_gtin(o.get("barcode"), allow_gtin8=True) if o.get("barcode") else None
            if barcode:
                with_barcode += 1
            rows.append((
                str(o.get("store_id") or ""), str(o["product_id"]), self.store_key,
                o["product_name"], o.get("brand"), o.get("category_path"),
                barcode, o.get("barcode_source") if barcode else None,
                reg, o.get("promo_price"), o.get("discount_pct"), o.get("promo_min_quantity"), _parse_ts(o.get("promo_end_at")),
                o.get("offer_tag"), o.get("offer_name"), o.get("app_membership_required"),
                o.get("unit"), o.get("is_available"), o.get("stock"), o.get("product_url"), o.get("image_url"),
                _parse_ts(o.get("scraped_at")) or datetime.now(timezone.utc),
            ))
        # de-duplicate inside the batch (last one wins) - Postgres rejects the same key twice in one INSERT
        dedup: Dict[Tuple[str, str], tuple] = {}
        for r in rows:
            dedup[(r[0], r[1])] = r
        rows = list(dedup.values())

        def _impl():
            with self._conn.cursor() as cur:
                for i in range(0, len(rows), batch_size):
                    cur.executemany(_UPSERT_SQL, rows[i:i + batch_size])
            self._conn.commit()

        if rows:
            self._run(_impl)
        if verbose:
            print(f"  saved {len(rows):,} offers ({with_barcode:,} with barcode, {skipped:,} skipped)")
        return {"upserted": len(rows), "skipped": skipped, "with_barcode": with_barcode}

    # ----------------------------------------------------------- barcode backfill
    def load_missing_barcodes(self, *, retry_not_found_days: int = 14, store_id: Optional[str] = None,
                              need_url: bool = True) -> List[Tuple[str, str, Optional[str], str]]:
        """
        Rows still without barcode that we should try to enrich now:
        (store_id, product_id, product_url, product_name).
        Skips rows marked 'found' (should not exist) and rows marked 'not_found'
        more recently than `retry_not_found_days`.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(retry_not_found_days), 0))

        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT o.store_id, o.product_id, o.product_url, o.product_name
                    FROM offers o
                    LEFT JOIN barcode_enrich_state s
                           ON s.store_id = o.store_id AND s.product_id = o.product_id
                    WHERE o.barcode IS NULL
                      AND (%s::text IS NULL OR o.store_id = %s)
                      AND (NOT %s OR (o.product_url IS NOT NULL AND o.product_url <> ''))
                      AND (s.product_id IS NULL
                           OR s.status = 'error'
                           OR (s.status = 'not_found' AND s.last_attempted_at < %s))
                    ORDER BY o.updated_at DESC
                    """,
                    (store_id, store_id, need_url, cutoff),
                )
                return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

        return self._run(_impl)

    def update_barcodes(self, rows: Iterable[Tuple[str, str, str, str]]) -> int:
        """rows: (store_id, product_id, barcode, source). Only fills NULL barcodes. Returns rows updated."""
        clean = []
        for store_id, product_id, barcode, source in rows:
            b = normalize_gtin(barcode, allow_gtin8=True)
            if b:
                clean.append((b, source, store_id, product_id))
        if not clean:
            return 0

        def _impl():
            total = 0
            with self._conn.cursor() as cur:
                for i in range(0, len(clean), 500):
                    cur.executemany(
                        "UPDATE offers SET barcode = %s, barcode_source = %s, updated_at = NOW() "
                        "WHERE store_id = %s AND product_id = %s AND barcode IS NULL",
                        clean[i:i + 500],
                    )
                    total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self._conn.commit()
            return total

        return self._run(_impl)

    def record_enrich_state(self, rows: Iterable[Tuple[str, str, str, Optional[int]]]) -> None:
        """rows: (store_id, product_id, status, http_status)."""
        data = [(s, p, st, h) for s, p, st, h in rows]
        if not data:
            return

        def _impl():
            with self._conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO barcode_enrich_state (store_id, product_id, status, http_status, attempts, last_attempted_at)
                    VALUES (%s, %s, %s, %s, 1, NOW())
                    ON CONFLICT (store_id, product_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        http_status = EXCLUDED.http_status,
                        attempts = barcode_enrich_state.attempts + 1,
                        last_attempted_at = NOW()
                    """,
                    data,
                )
            self._conn.commit()

        self._run(_impl)

    def seed_barcodes_from_legacy(self) -> int:
        """
        Reuse barcodes from the old `offers_legacy` table (previous project
        generation) for rows that still lack one. Matches by product_url first,
        then by normalised product name. Cheap; safe to run every time.
        """
        def _impl():
            with self._conn.cursor() as cur:
                if not self._table_exists(cur, "offers_legacy"):
                    return 0
                cur.execute("SELECT store_id, product_id, product_url, product_name FROM offers WHERE barcode IS NULL")
                missing = cur.fetchall()
                if not missing:
                    return 0
                cols = self._columns(cur, "offers_legacy")
                bc_expr = "COALESCE(barcode, gtin)" if "gtin" in cols else "barcode"
                cur.execute(f"SELECT product_url, product_name, {bc_expr} FROM offers_legacy WHERE {bc_expr} IS NOT NULL")
                by_url: Dict[str, str] = {}
                by_name: Dict[str, str] = {}
                for url, name, bc in cur.fetchall():
                    b = normalize_gtin(bc, allow_gtin8=True)
                    if not b:
                        continue
                    if url:
                        by_url.setdefault(str(url).strip().lower(), b)
                    n = norm_name(name)
                    if n:
                        if n in by_name and by_name[n] != b:
                            by_name[n] = ""  # ambiguous name -> never use
                        else:
                            by_name.setdefault(n, b)
            updates = []
            for store_id, product_id, url, name in missing:
                b = by_url.get(str(url or "").strip().lower()) or by_name.get(norm_name(name))
                if b:
                    updates.append((store_id, product_id, b, "legacy"))
            return self.update_barcodes(updates) if updates else 0

        return self._run(_impl)

    # ----------------------------------------------------------- reads / maintenance
    def load_existing_product_ids(self, store_id: Optional[str] = None) -> set:
        def _impl():
            with self._conn.cursor() as cur:
                if store_id:
                    cur.execute("SELECT product_id FROM offers WHERE store_id = %s", (store_id,))
                else:
                    cur.execute("SELECT product_id FROM offers")
                return {r[0] for r in cur.fetchall()}
        return self._run(_impl)

    def load_barcode_map(self) -> Dict[str, str]:
        """{product_id: barcode} for every row that already has one (all stores)."""
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute("SELECT product_id, barcode FROM offers WHERE barcode IS NOT NULL")
                return {r[0]: r[1] for r in cur.fetchall()}
        return self._run(_impl)

    def barcode_coverage(self) -> Dict[str, Any]:
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), COUNT(barcode), MAX(updated_at), COUNT(DISTINCT store_id) FROM offers"
                )
                total, with_bc, last, stores = cur.fetchone()
                cur.execute("SELECT barcode_source, COUNT(*) FROM offers WHERE barcode IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
                sources = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute("SELECT pg_database_size(current_database())")
                size = cur.fetchone()[0]
            return {"offers": total, "with_barcode": with_bc, "pct": (100.0 * with_bc / total) if total else 0.0,
                    "last_update": last, "stores": stores, "sources": sources, "db_bytes": size}
        return self._run(_impl)

    def print_barcode_coverage(self) -> None:
        c = self.barcode_coverage()
        src = ", ".join(f"{k}={v:,}" for k, v in c["sources"].items()) or "-"
        print(f"[{self.store_key}] offers={c['offers']:,} barcode={c['with_barcode']:,} ({c['pct']:.1f}%) "
              f"sources: {src} | db={c['db_bytes'] / 1024 / 1024:.0f} MB")

    def last_update(self) -> Optional[datetime]:
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute("SELECT MAX(updated_at) FROM offers")
                row = cur.fetchone()
                return row[0] if row else None
        return self._run(_impl)

    def mark_stale_unavailable(self, hours: int = 48) -> int:
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE offers SET is_available = false "
                    "WHERE is_available IS DISTINCT FROM false AND updated_at < NOW() - make_interval(hours => %s)",
                    (hours,),
                )
                n = cur.rowcount
            self._conn.commit()
            return n
        n = self._run(_impl)
        print(f"  [{self.store_key}] marked {n:,} stale offers unavailable (not seen in {hours}h)")
        return n

    def prune_history(self, days: int = 180) -> int:
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM price_history
                    WHERE recorded_at < NOW() - make_interval(days => %s)
                      AND id NOT IN (SELECT MIN(id) FROM price_history GROUP BY store_id, product_id)
                    """,
                    (days,),
                )
                n = cur.rowcount
            self._conn.commit()
            return n
        n = self._run(_impl)
        print(f"  [{self.store_key}] pruned {n:,} price_history rows older than {days} days")
        return n

    def drop_legacy(self) -> List[str]:
        def _impl():
            dropped = []
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND (tablename LIKE 'offers_legacy%%' OR tablename LIKE 'price_history_legacy%%')"
                )
                for (t,) in cur.fetchall():
                    cur.execute(f'DROP TABLE IF EXISTS "{t}"')
                    dropped.append(t)
            self._conn.commit()
            return dropped
        dropped = self._run(_impl)
        print(f"  [{self.store_key}] dropped legacy tables: {', '.join(dropped) or 'none'}")
        return dropped

    # ----------------------------------------------------------- export
    def export(self, output_dir: str = "exports", tables: Optional[List[str]] = None) -> List[str]:
        os.makedirs(output_dir, exist_ok=True)
        tables = tables or ["offers", "price_history"]
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        written = []
        for table in tables:
            path = os.path.join(output_dir, f"{self.store_key}_{table}_{ts}.csv")
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} ORDER BY 1, 2")
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
            print(f"  [{self.store_key}] {table}: {len(rows):,} rows -> {path}")
            written.append(path)
        return written

    def combined_rows(self, *, only_with_barcode: bool = True) -> List[Dict[str, Any]]:
        """Rows in the barcode-keyed combined format (see COMBINED_FIELDS)."""
        def _impl():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT o.barcode,
                           CASE WHEN o.promo_price IS NOT NULL
                                 AND (o.promo_end_at IS NULL OR o.promo_end_at > NOW())
                                THEN o.promo_price ELSE o.regular_price END AS price,
                           COALESCE(o.promo_min_quantity, 1) AS quantity,
                           o.store_id, si.store_zip, o.scraped_at,
                           ph.min_price, ph.max_price,
                           o.product_url, o.image_url, o.product_name, o.brand,
                           o.regular_price, o.promo_price, o.product_id, o.is_available
                    FROM offers o
                    LEFT JOIN store_info si ON si.store_id = o.store_id
                    LEFT JOIN (
                        SELECT store_id, product_id,
                               MIN(COALESCE(promo_price, regular_price)) AS min_price,
                               MAX(COALESCE(promo_price, regular_price)) AS max_price
                        FROM price_history GROUP BY store_id, product_id
                    ) ph ON ph.store_id = o.store_id AND ph.product_id = o.product_id
                    WHERE o.regular_price IS NOT NULL AND o.regular_price > 0
                      AND (NOT %s OR o.barcode IS NOT NULL)
                    ORDER BY o.barcode
                    """,
                    (only_with_barcode,),
                )
                return cur.fetchall()

        def _fmt(v):
            return "" if v is None else f"{float(v):.2f}"

        out = []
        for r in self._run(_impl):
            (barcode, price, qty, store_id, store_zip, scraped_at, mn, mx,
             url, img, name, brand, reg, promo, pid, avail) = r
            out.append({
                "barcode": barcode or "", "price": _fmt(price), "quantity": qty,
                "store_name": self.market_name, "store_id": store_id, "store_cep": store_zip or "",
                "date_recorded": scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ") if scraped_at else "",
                "notes": f"Min:{_fmt(mn or price)} Max:{_fmt(mx or price)}",
                "product_url": url or "", "image_url": img or "", "product_name": name or "",
                "brand": brand or "", "regular_price": _fmt(reg), "promo_price": _fmt(promo),
                "market": self.store_key, "product_id": pid, "is_available": avail,
            })
        return out


COMBINED_FIELDS = [
    "barcode", "price", "quantity", "store_name", "store_id", "store_cep", "date_recorded", "notes",
    "product_url", "image_url", "product_name", "brand", "regular_price", "promo_price", "market",
    "product_id", "is_available",
]


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def open_store_db(store_key: str) -> StoreDB:
    return StoreDB(store_key)


def all_store_keys() -> List[str]:
    import config
    return list(config.STORES)


def export_all_together(output_dir: str = "exports", only_with_barcode: bool = True) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"all_markets_combined_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    total = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COMBINED_FIELDS)
        w.writeheader()
        for key in all_store_keys():
            if not os.environ.get(str(__import__("config").STORES[key]["db_env"])):
                print(f"  [{key}] no DB url - skipped")
                continue
            try:
                db = StoreDB(key)
                rows = db.combined_rows(only_with_barcode=only_with_barcode)
                w.writerows(rows)
                total += len(rows)
                print(f"  [{key}] {len(rows):,} rows")
                db.close()
            except Exception as exc:
                print(f"  [{key}] ERROR: {exc}")
    print(f"\nCombined export: {total:,} rows -> {path}")
    return path


_HUB_DDL = """
CREATE TABLE IF NOT EXISTS app_offers (
    market          TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    barcode         TEXT,
    price           NUMERIC(10,2),
    quantity        INTEGER,
    store_name      TEXT,
    store_cep       TEXT,
    date_recorded   TIMESTAMPTZ,
    notes           TEXT,
    product_url     TEXT,
    image_url       TEXT,
    product_name    TEXT,
    brand           TEXT,
    regular_price   NUMERIC(10,2),
    promo_price     NUMERIC(10,2),
    is_available    BOOLEAN,
    PRIMARY KEY (market, store_id, product_id)
);
CREATE INDEX IF NOT EXISTS app_offers_barcode_idx ON app_offers (barcode);
CREATE INDEX IF NOT EXISTS app_offers_market_idx ON app_offers (market);
"""


def sync_hub(stores: Optional[List[str]] = None) -> int:
    """
    Optional consolidated table for apps: copies every market's barcode-keyed
    rows into `app_offers` inside DATABASE_URL_HUB (another Neon DB). Each
    market is replaced atomically; other markets are untouched.
    """
    hub_url = os.environ.get("DATABASE_URL_HUB", "")
    if not hub_url:
        raise RuntimeError("DATABASE_URL_HUB is not set (optional feature)")
    import config
    hub = psycopg.connect(hub_url, connect_timeout=20, prepare_threshold=None)
    with hub.cursor() as cur:
        cur.execute(_HUB_DDL)
    hub.commit()
    total = 0
    for key in stores or all_store_keys():
        if not os.environ.get(str(config.STORES[key]["db_env"])):
            continue
        try:
            db = StoreDB(key)
            rows = db.combined_rows(only_with_barcode=True)
            db.close()
        except Exception as exc:
            print(f"  [{key}] ERROR reading: {exc}")
            continue
        data = [(
            r["market"], r["store_id"], r["product_id"], r["barcode"] or None,
            r["price"] or None, r["quantity"], r["store_name"], r["store_cep"] or None,
            r["date_recorded"] or None, r["notes"], r["product_url"] or None, r["image_url"] or None,
            r["product_name"], r["brand"] or None, r["regular_price"] or None, r["promo_price"] or None,
            r["is_available"],
        ) for r in rows]
        with hub.cursor() as cur:
            cur.execute("DELETE FROM app_offers WHERE market = %s", (key,))
            for i in range(0, len(data), 500):
                cur.executemany(
                    "INSERT INTO app_offers (market, store_id, product_id, barcode, price, quantity, store_name, store_cep, "
                    "date_recorded, notes, product_url, image_url, product_name, brand, regular_price, promo_price, is_available) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    data[i:i + 500],
                )
        hub.commit()
        total += len(data)
        print(f"  [{key}] hub app_offers: {len(data):,} rows")
    hub.close()
    print(f"hub sync complete: {total:,} rows")
    return total


def print_stats() -> None:
    import config
    print(f"{'market':<16}{'offers':>9}{'barcode':>9}{'pct':>7}{'stores':>7}{'db MB':>7}  last update (UTC)")
    for key, meta in config.STORES.items():
        if not os.environ.get(str(meta["db_env"])):
            print(f"{key:<16}{'-':>9}  (no {meta['db_env']})")
            continue
        try:
            db = StoreDB(key)
            c = db.barcode_coverage()
            db.close()
            last = c["last_update"].strftime("%Y-%m-%d %H:%M") if c["last_update"] else "never"
            print(f"{key:<16}{c['offers']:>9,}{c['with_barcode']:>9,}{c['pct']:>6.1f}%{c['stores']:>7}"
                  f"{c['db_bytes'] / 1024 / 1024:>7.0f}  {last}")
        except Exception as exc:
            print(f"{key:<16} ERROR: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DB manager for market scrapers (Neon PostgreSQL)")
    parser.add_argument("--env", default=".env")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="Coverage/freshness for every market")
    p = sub.add_parser("export", help="Dump one market's tables to CSV")
    p.add_argument("store")
    p.add_argument("--dir", default="exports")
    p.add_argument("--tables", nargs="+", default=None)
    p = sub.add_parser("export-all-together", help="Single barcode-keyed CSV for all markets")
    p.add_argument("--dir", default="exports")
    p.add_argument("--all-rows", action="store_true", help="Include rows without barcode")
    p = sub.add_parser("sync-hub", help="Copy barcode-keyed rows into DATABASE_URL_HUB.app_offers")
    p.add_argument("--stores", nargs="+", default=None)
    p = sub.add_parser("prune", help="Delete old price_history rows")
    p.add_argument("store")
    p.add_argument("--days", type=int, default=180)
    p = sub.add_parser("mark-stale", help="Flip offers not seen recently to unavailable")
    p.add_argument("store")
    p.add_argument("--hours", type=int, default=48)
    p = sub.add_parser("drop-legacy", help="Drop offers_legacy / price_history_legacy tables")
    p.add_argument("store")
    p = sub.add_parser("seed-legacy", help="Re-run legacy barcode seeding")
    p.add_argument("store")
    args = parser.parse_args()

    load_env(args.env)
    keys = all_store_keys()

    def _targets(name: str) -> List[str]:
        return keys if name == "all" else [name]

    if args.cmd == "stats":
        print_stats()
    elif args.cmd == "export":
        db = StoreDB(args.store)
        db.export(args.dir, args.tables)
        db.close()
    elif args.cmd == "export-all-together":
        export_all_together(args.dir, only_with_barcode=not args.all_rows)
    elif args.cmd == "sync-hub":
        sync_hub(args.stores)
    elif args.cmd in ("prune", "mark-stale", "drop-legacy", "seed-legacy"):
        for k in _targets(args.store):
            try:
                db = StoreDB(k)
                if args.cmd == "prune":
                    db.prune_history(args.days)
                elif args.cmd == "mark-stale":
                    db.mark_stale_unavailable(args.hours)
                elif args.cmd == "drop-legacy":
                    db.drop_legacy()
                else:
                    print(f"  [{k}] seeded {db.seed_barcodes_from_legacy():,} barcodes from legacy")
                db.close()
            except Exception as exc:
                print(f"  [{k}] ERROR: {exc}")
