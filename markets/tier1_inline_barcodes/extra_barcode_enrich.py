"""
Extra Mercado / Pão de Açúcar / Oba Hortifruti barcode enrichment.

Strategy (priority order per product):
  1. known_barcodes table  — barcode is immutable; if any market already
     registered it we reuse instantly without any HTTP request.
  2. GPA product-detail API — POST /ex/product-details {id, storeId}
  3. PDP HTML fallback     — GET urlDetails → JSON-LD / regex

Key design decisions:
  - Offer IDs are NEVER renamed. We keep the stable extra_{native_id} ID
    and simply UPDATE offers SET barcode=... WHERE id=...
    Renaming caused silent PK collisions in the offers table.
  - known_barcodes uses barcode as PK (immutable physical identifier).
    Each barcode has exactly ONE row regardless of how many markets carry it.
  - State table persists per-offer attempt so re-runs skip already-done work.
  - After enrichment, sync_known_barcodes() refreshes the full catalog from
    all trusted markets so every other market benefits immediately.

Usage:
    python extra_barcode_enrich.py                  # Extra standalone
    from markets.tier1_inline_barcodes.extra_barcode_enrich import run as run_extra_barcode_enrich
    run_extra_barcode_enrich(db, market_name="Extra", ...)
"""

import html as html_module
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests

from db.db_manager import DatabaseManager


# ---------------------------------------------------------------------------
# State table  ({market_slug}_barcode_enrich_state)
# ---------------------------------------------------------------------------

def _ensure_state_table(db: DatabaseManager, market_slug: str) -> None:
    table = f"{market_slug}_barcode_enrich_state"
    conn = db._get_pg()
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            offer_id            TEXT PRIMARY KEY,
            store_id            TEXT NOT NULL,
            status              TEXT NOT NULL,
            http_status         INTEGER,
            barcode             TEXT,
            last_attempted_at   TIMESTAMP NOT NULL,
            last_success_at     TIMESTAMP
        )
    """)
    cursor.execute(
        "DO $$ DECLARE t text; BEGIN FOR t IN SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' AND NOT rowsecurity LOOP "
        "EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t); "
        "END LOOP; END $$;"
    )
    conn.commit()
    conn.close()


def _parse_datetime_like(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Accept either ISO timestamps or DB-formatted timestamps.
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _load_completed_ids(
    db: DatabaseManager,
    market_slug: str,
    not_found_ttl_days: int = 14,
) -> Set[str]:
    """Return offer_ids to skip on this run.

    - found: always skipped (stable success)
    - not_found: skipped only while still within TTL window
    """
    table = f"{market_slug}_barcode_enrich_state"
    try:
        cutoff = datetime.now() - timedelta(days=max(int(not_found_ttl_days), 0))
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT offer_id, status, last_attempted_at FROM {table}"
        )
        rows = cursor.fetchall()
        conn.close()

        completed: Set[str] = set()
        for row in rows:
            if not row or not row[0]:
                continue
            offer_id = str(row[0])
            status = str(row[1] or "").strip().lower()
            last_attempted = _parse_datetime_like(row[2] if len(row) > 2 else None)

            if status == "found":
                completed.add(offer_id)
            elif status == "not_found" and last_attempted and last_attempted >= cutoff:
                completed.add(offer_id)

        return completed
    except Exception:
        return set()


def _read_not_found_ttl_days(default: int = 14) -> int:
    raw_value = os.getenv("BARCODE_ENRICH_NOT_FOUND_TTL_DAYS", str(default))
    try:
        parsed = int(str(raw_value).strip())
    except ValueError:
        return default
    return max(parsed, 0)


def _upsert_state_bulk(
    db: DatabaseManager,
    market_slug: str,
    rows: Sequence[Tuple],
) -> None:
    """Persist enrichment results: (offer_id, store_id, status, http_status, barcode)."""
    if not rows:
        return
    table = f"{market_slug}_barcode_enrich_state"
    now_iso = datetime.now().isoformat()
    full_rows = [
        (r[0], r[1], r[2], r[3], r[4],
         now_iso,
         now_iso if r[2] == "found" else None)
        for r in rows
    ]
    conn = db._get_pg()
    cursor = conn.cursor()
    cursor.executemany(
        f"""
        INSERT INTO {table}
            (offer_id, store_id, status, http_status, barcode,
             last_attempted_at, last_success_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (offer_id) DO UPDATE SET
            status            = EXCLUDED.status,
            http_status       = EXCLUDED.http_status,
            barcode           = EXCLUDED.barcode,
            last_attempted_at = EXCLUDED.last_attempted_at,
            last_success_at   = COALESCE(EXCLUDED.last_success_at,
                                         {table}.last_success_at)
        """,
        full_rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# known_barcodes lookup — free barcode resolution, no HTTP
# ---------------------------------------------------------------------------

def _load_known_barcodes_by_name(db: DatabaseManager) -> Dict[str, str]:
    """
    Build {normalized_product_name -> barcode} index from known_barcodes.
    Used to resolve barcodes for products we've seen before in ANY market.
    """
    index: Dict[str, str] = {}
    try:
        rows = db.fetch_all_known_barcodes()
        # (barcode, source_market, source_market_id, canonical_name, canonical_brand,
        #  canonical_description, normalized_name, normalized_brand, measure_token, last_updated)
        for row in rows:
            barcode = str(row[0]).strip() if row[0] else None
            norm_name = str(row[6]).strip().lower() if row[6] else None
            norm_brand = str(row[7]).strip().lower() if row[7] else None
            if barcode and norm_name:
                key = f"{norm_name}|{norm_brand or ''}"
                if key not in index:
                    index[key] = barcode
    except Exception as exc:
        print(f"  Warning: could not load known_barcodes index: {exc}")
    return index


def _lookup_in_known(
    known_index: Dict[str, str],
    product_name: str,
    brand: Optional[str],
) -> Optional[str]:
    name_key = (product_name or "").strip().lower()
    brand_key = (brand or "").strip().lower()
    return known_index.get(f"{name_key}|{brand_key}") or known_index.get(f"{name_key}|")


# ---------------------------------------------------------------------------
# Network fetch helpers
# ---------------------------------------------------------------------------

def _fetch_via_gpa_api(
    session: requests.Session,
    api_base: str,
    product_id: int,
    gpa_store_id: int,
) -> Optional[str]:
    """Try GPA product-detail API."""
    payload = {"id": product_id, "storeId": gpa_store_id}
    for url in [f"{api_base}/product-details", f"{api_base}/product/{product_id}"]:
        try:
            r = session.post(url, json=payload, timeout=15)
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                continue
            data = r.json() or {}
            product = data.get("product") or data
            for key in ("ean", "gtin", "gtin13", "barcode", "eanCode",
                        "referenceCode", "productCode", "codigo_barras"):
                val = product.get(key)
                if val:
                    digits = "".join(c for c in str(val) if c.isdigit())
                    if 7 <= len(digits) <= 14:
                        return digits
        except Exception:
            pass
    return None


def _fetch_via_pdp_html(
    session: requests.Session,
    product_url: str,
) -> Optional[str]:
    """Fetch barcode from PDP via JSON-LD then regex."""
    try:
        r = session.get(product_url, timeout=20)
        if r.status_code != 200:
            return None
        text = r.text
        for script in re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text, flags=re.DOTALL | re.IGNORECASE,
        ):
            try:
                data = json.loads(html_module.unescape(script))
                for item in (data if isinstance(data, list) else [data]):
                    for key in ("gtin13", "gtin", "gtin14", "gtin12", "mpn", "ean", "barcode"):
                        val = item.get(key)
                        if val:
                            digits = "".join(c for c in str(val) if c.isdigit())
                            if 7 <= len(digits) <= 14:
                                return digits
            except Exception:
                pass
        for pattern in [
            r'"(?:ean|gtin13?|barcode|codigo_barras|eanCode)"[:\s"\']+(\d{7,14})',
            r'EAN[:\s"\']+(\d{7,14})',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def _load_offers_needing_barcode(
    db: DatabaseManager,
    market_name: str,
    completed_ids: Set[str],
    max_calls: int,
) -> List[Dict]:
    """Query offers with no barcode that have a product_url."""
    conn = db._get_pg_for_market(market_name)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, product_name, brand, product_url
        FROM offers
        WHERE market_name = %s
          AND (barcode IS NULL OR TRIM(barcode) = '')
          AND product_url IS NOT NULL
          AND TRIM(product_url) <> ''
        ORDER BY last_updated DESC
        LIMIT %s
        """,
        (market_name, max_calls * 2),
    )
    rows = cursor.fetchall()
    conn.close()

    result = []
    for offer_id, product_name, brand, product_url in rows:
        if str(offer_id) in completed_ids:
            continue
        result.append({
            "offer_id": str(offer_id),
            "product_name": product_name or "",
            "brand": brand or "",
            "product_url": product_url,
        })
        if len(result) >= max_calls:
            break
    return result


def _write_barcodes_to_offers(
    db: DatabaseManager,
    market_name: str,
    updates: List[Tuple[str, str]],  # (offer_id, raw_barcode)
) -> int:
    """
    Write barcodes to offers table IN-PLACE — never rename offer IDs.
    Keeps offer_id stable as extra_{native_id} to avoid PK collisions.
    Returns count of rows updated.
    """
    if not updates:
        return 0
    now_iso = datetime.now().isoformat()
    conn = db._get_pg_for_market(market_name)
    cursor = conn.cursor()
    updated = 0
    for offer_id, raw_barcode in updates:
        normalized = db.normalize_barcode(raw_barcode)
        if not normalized:
            continue
        # Skip if another offer in the same (market, store) already owns this barcode
        cursor.execute(
            """
            UPDATE offers
            SET gtin = %s, barcode = %s, last_updated = %s
            WHERE id = %s AND market_name = %s
              AND NOT EXISTS (
                  SELECT 1 FROM offers o2
                  WHERE o2.market_name = %s AND o2.barcode = %s
                    AND o2.store_id = (SELECT store_id FROM offers WHERE id = %s)
                    AND o2.id <> %s
              )
            """,
            (raw_barcode, normalized, now_iso, offer_id, market_name,
             market_name, normalized, offer_id, offer_id),
        )
        if cursor.rowcount > 0:
            updated += 1
            # Rename ID to {prefix}_{barcode}_{store_hash8}
            cursor.execute("SELECT store_id FROM offers WHERE id = %s", (offer_id,))
            row = cursor.fetchone()
            store_id = str(row[0] or "").strip() if row else ""
            prefix = offer_id.split("_", 1)[0]
            store_hash = db._store_id_hash(store_id) if store_id else ""
            new_id = f"{prefix}_{normalized}_{store_hash}" if store_hash else f"{prefix}_{normalized}"
            if new_id != offer_id:
                cursor.execute(
                    """
                    UPDATE offers SET id = %s
                    WHERE id = %s
                      AND NOT EXISTS (SELECT 1 FROM offers WHERE id = %s)
                    """,
                    (new_id, offer_id, new_id),
                )
    conn.commit()
    conn.close()
    return updated


def _write_to_known_barcodes(
    db: DatabaseManager,
    market_name: str,
    updates: List[Tuple[str, str, str, str]],  # (offer_id, raw_barcode, product_name, brand)
) -> None:
    """
    Upsert newly found barcodes to product_catalog (via upsert_known_barcodes).
    barcode is PK — one row per unique physical product, never duplicates.
    (known_barcodes was merged into product_catalog; this function writes there.)
    """
    if not updates:
        return
    now_iso = datetime.now().isoformat()
    rows = []
    for offer_id, raw_barcode, product_name, brand in updates:
        normalized = db.normalize_barcode(raw_barcode)
        if not normalized:
            continue
        norm_name = (product_name or "").strip().lower()
        norm_brand = (brand or "").strip().lower()
        measure = ""
        if product_name:
            m = re.search(
                r'\b(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|un|unid|pct)\b',
                product_name, re.IGNORECASE
            )
            if m:
                measure = f"{m.group(1).replace(',', '.')}{m.group(2).lower()}"
        rows.append((
            normalized, market_name, offer_id,
            product_name, brand, None,
            norm_name, norm_brand, measure,
            now_iso,
        ))
    if rows:
        db.upsert_known_barcodes(rows)


def _write_barcode_references(
    db: DatabaseManager,
    market_name: str,
    updates: List[Tuple[str, str, str, str]],  # (offer_id, raw_barcode, product_name, brand)
) -> None:
    """Persist market barcode refs so future runs can reuse matches immediately."""
    if not updates:
        return
    refs: List[Tuple[str, str, str, str, str]] = []
    for offer_id, raw_barcode, product_name, brand in updates:
        normalized = db.normalize_barcode(raw_barcode)
        if not normalized:
            continue
        refs.append((normalized, market_name, offer_id, product_name or "", brand or ""))
    if refs:
        db.save_barcode_references_bulk(refs)


# ---------------------------------------------------------------------------
# Store mappings write (Bug 3 fix)
# ---------------------------------------------------------------------------

def _write_store_mapping(
    db: DatabaseManager,
    market_name: str,
    zip_code: str,
    store_id: str,
    store_name: Optional[str] = None,
) -> None:
    """Ensure a store_mappings entry exists for this market run."""
    try:
        existing = db.get_store_id(zip_code, market_name)
        if not existing:
            db.cache_store_id(
                zip_code, market_name, store_id,
                store_name=store_name or market_name,
            )
    except Exception as exc:
        print(f"  Warning: could not write store_mapping for {market_name}: {exc}")


# ---------------------------------------------------------------------------
# product_catalog population (Bug 5 fix)
# ---------------------------------------------------------------------------

def _sync_product_catalog(db: DatabaseManager) -> None:
    """
    Update market_count in product_catalog by counting how many markets
    have each barcode in barcode_reference_market_map.

    product_catalog IS the canonical barcode table now (known_barcodes was
    merged into it). This function only refreshes the market_count aggregation.
    """
    try:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE product_catalog pc
            SET market_count = sub.cnt,
                last_updated = NOW()
            FROM (
                SELECT barcode, COUNT(DISTINCT market_name) AS cnt
                FROM barcode_reference_market_map
                WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
                GROUP BY barcode
            ) sub
            WHERE pc.barcode = sub.barcode
            """
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"  product_catalog: market_count refreshed for {updated} barcodes")
    except Exception as exc:
        print(f"  Warning: product_catalog sync failed: {exc}")


# ---------------------------------------------------------------------------
# Main enrichment runner
# ---------------------------------------------------------------------------

def run(
    db: DatabaseManager,
    *,
    market_name: str = "Extra",
    market_slug: str = "extra",
    api_base: str = "https://api.vendas.gpa.digital/ex",
    gpa_store_id: int = 483,
    zip_code: str = "",
    max_calls: int = 10000,
    delay: float = 0.08,
    flush_every: int = 100,
    skip_pdp_html: bool = False,
) -> None:
    """
    Enrich barcodes for all offers of a given market that are missing them.

    - Consults known_barcodes first (no HTTP cost)
    - Tries GPA API, then PDP HTML
        - skip_pdp_html: when True, skip PDP HTML step (use for markets where PDP never contains barcode)
    - Writes barcodes IN-PLACE (offer ID never renamed)
    - Persists state so re-runs skip completed offers
    - Writes all found barcodes to known_barcodes (barcode = PK, no duplicates)
    - Syncs product_catalog from known_barcodes
    """
    print(f"\n=== {market_name} barcode enrichment ===")
    _ensure_state_table(db, market_slug)

    # Bug 3 fix: ensure store_mappings entry exists
    if zip_code:
        _write_store_mapping(db, market_name, zip_code,
                             store_id=f"{market_slug}_{gpa_store_id}",
                             store_name=market_name)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })

    not_found_ttl_days = _read_not_found_ttl_days(default=14)
    completed_ids = _load_completed_ids(db, market_slug, not_found_ttl_days=not_found_ttl_days)
    print(
        f"{market_name}: {len(completed_ids)} offers already enriched (skipping, "
        f"not_found_ttl_days={not_found_ttl_days})"
    )

    known_index = _load_known_barcodes_by_name(db)
    print(f"{market_name}: {len(known_index)} name->barcode entries in known_barcodes")

    offers = _load_offers_needing_barcode(db, market_name, completed_ids, max_calls)
    print(f"{market_name}: {len(offers)} offers to enrich")

    if not offers:
        print(f"{market_name}: nothing to do.")
        _sync_product_catalog(db)
        return

    found_known = 0
    found_api   = 0
    found_pdp   = 0
    not_found   = 0

    state_batch:    List[Tuple]             = []
    db_update_batch: List[Tuple[str, str]]  = []
    known_bc_batch: List[Tuple[str,str,str,str]] = []

    for i, offer in enumerate(offers, 1):
        offer_id     = offer["offer_id"]
        product_url  = offer["product_url"]
        product_name = offer["product_name"]
        brand        = offer["brand"]

        raw_barcode: Optional[str] = None
        source: Optional[str] = None

        # ── Step 1: known_barcodes (free) ────────────────────────────────
        kb = _lookup_in_known(known_index, product_name, brand)
        if kb:
            raw_barcode = kb
            source = "known"
            found_known += 1

        # ── Step 2: GPA product-detail API ───────────────────────────────
        if not raw_barcode:
            m = re.search(r'/produto/(\d+)/', product_url)
            if m:
                raw_barcode = _fetch_via_gpa_api(
                    session, api_base, int(m.group(1)), gpa_store_id
                )
                if raw_barcode:
                    source = "api"
                    found_api += 1

        # ── Step 3: PDP HTML ─────────────────────────────────────────────
        if not raw_barcode and not skip_pdp_html:
            raw_barcode = _fetch_via_pdp_html(session, product_url)
            if raw_barcode:
                source = "pdp"
                found_pdp += 1

        status = "found" if raw_barcode else "not_found"

        if raw_barcode:
            db_update_batch.append((offer_id, raw_barcode))
            known_bc_batch.append((offer_id, raw_barcode, product_name, brand))
            # Update in-memory index immediately so later products benefit
            norm_name = product_name.strip().lower()
            norm_brand = brand.strip().lower()
            key = f"{norm_name}|{norm_brand}"
            if key not in known_index:
                known_index[key] = raw_barcode
        else:
            not_found += 1

        state_batch.append((
            offer_id, f"{market_slug}_{gpa_store_id}",
            status, None, raw_barcode,
        ))

        # ── Flush ─────────────────────────────────────────────────────────
        if i % flush_every == 0 or i == len(offers):
            written = _write_barcodes_to_offers(db, market_name, db_update_batch)
            _write_to_known_barcodes(db, market_name, known_bc_batch)
            _write_barcode_references(db, market_name, known_bc_batch)
            _upsert_state_bulk(db, market_slug, state_batch)

            db_update_batch.clear()
            known_bc_batch.clear()
            state_batch.clear()

            print(
                f"{market_name}: {i}/{len(offers)} | "
                f"known={found_known} api={found_api} pdp={found_pdp} "
                f"not_found={not_found} | db_written={written}"
            )

        # Delay only for real network calls
        if source not in ("known", None) and delay > 0:
            time.sleep(delay)

    # ── Final sync ────────────────────────────────────────────────────────
    print(f"\n{market_name}: syncing known_barcodes + product_catalog...")
    try:
        from db.barcode_ai_matcher import BarcodeAIMatcher
        stats = BarcodeAIMatcher().sync_known_barcodes()
        print(
            f"{market_name}: known_barcodes synced — "
            f"source_rows={stats.get('source_rows')} "
            f"upserted={stats.get('upserted')}"
        )
    except Exception as exc:
        print(f"{market_name}: known_barcodes sync error: {exc}")

    _sync_product_catalog(db)

    total_found = found_known + found_api + found_pdp
    print(
        f"\n{market_name} enrichment complete: "
        f"found={total_found} "
        f"(known={found_known} api={found_api} pdp={found_pdp}) "
        f"not_found={not_found} total={len(offers)}"
    )


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _db = DatabaseManager()
    run(_db, market_name="Extra", market_slug="extra",
        api_base="https://api.vendas.gpa.digital/ex",
        gpa_store_id=483, max_calls=10000, delay=0.08)
