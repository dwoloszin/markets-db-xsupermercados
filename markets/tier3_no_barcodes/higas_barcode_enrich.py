import argparse
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set

import sqlite3

import config

from db.db_manager import DatabaseManager
from env_loader import load_env_file
from location_detector import LocationDetector
from markets.tier3_no_barcodes.market_scrap_higas_departamentos import HigasDepartamentosScraper

DEFAULT_STORE_ID = "66466cdefafdf200a3352cd5"
DEFAULT_ZIP_CODE = os.getenv("SCRAPE_ZIP_CODE", "08032-230")


def _resolve_zip_code(zip_code: Optional[str]) -> str:
    provided = str(zip_code or "").strip()
    if provided:
        return LocationDetector.format_zip_code(provided)

    env_zip = str(os.getenv("SCRAPE_ZIP_CODE") or "").strip()
    if env_zip:
        return LocationDetector.format_zip_code(env_zip)

    detected = LocationDetector.detect_user_location()
    if detected:
        return LocationDetector.format_zip_code(detected)

    return LocationDetector.format_zip_code(DEFAULT_ZIP_CODE)


def _normalize_barcode_set(values: Sequence[Any], db: DatabaseManager) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        normalized = db.normalize_barcode(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _ensure_state_table(db: DatabaseManager) -> None:
    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS higas_barcode_enrich_state (
                barcode TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                hits INTEGER DEFAULT 0,
                last_attempted_at TIMESTAMP NOT NULL,
                last_success_at TIMESTAMP
            )
            """
        )
        cursor.execute(
            "DO $$ DECLARE t text; BEGIN FOR t IN SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND NOT rowsecurity LOOP "
            "EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t); "
            "END LOOP; END $$;"
        )
        conn.commit()
        conn.close()
        return

    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS higas_barcode_enrich_state (
            barcode TEXT PRIMARY KEY,
            store_id TEXT NOT NULL,
            status TEXT NOT NULL,
            http_status INTEGER,
            hits INTEGER DEFAULT 0,
            last_attempted_at TEXT NOT NULL,
            last_success_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _reset_found_state(db: DatabaseManager) -> int:
    """Delete all 'found' rows from higas_barcode_enrich_state so they get re-queried.

    'not_found' rows are kept — those barcodes are confirmed absent from Higas and
    don't need to be retried.  Use this to refresh prices for already-enriched products.
    """
    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM higas_barcode_enrich_state WHERE status = 'found'"
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM higas_barcode_enrich_state WHERE status = 'found'")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def _fetch_completed_barcodes(db: DatabaseManager, store_id: str) -> Set[str]:
    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT barcode
            FROM higas_barcode_enrich_state
            WHERE store_id = %s
              AND status IN ('found', 'not_found')
            """,
            (store_id,),
        )
        rows = cursor.fetchall()
        conn.close()
        return {str(row[0]) for row in rows if row and row[0]}

    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT barcode
        FROM higas_barcode_enrich_state
        WHERE store_id = ?
          AND status IN ('found', 'not_found')
        """,
        (store_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return {str(row[0]) for row in rows if row and row[0]}


def _upsert_state(
    db: DatabaseManager,
    *,
    barcode: str,
    store_id: str,
    status: str,
    http_status: Optional[int],
    hits: int,
    success: bool,
) -> None:
    now_iso = datetime.now().isoformat()
    success_at = now_iso if success else None

    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO higas_barcode_enrich_state
                (barcode, store_id, status, http_status, hits, last_attempted_at, last_success_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (barcode) DO UPDATE SET
                store_id = EXCLUDED.store_id,
                status = EXCLUDED.status,
                http_status = EXCLUDED.http_status,
                hits = EXCLUDED.hits,
                last_attempted_at = EXCLUDED.last_attempted_at,
                last_success_at = COALESCE(EXCLUDED.last_success_at, higas_barcode_enrich_state.last_success_at)
            """,
            (barcode, store_id, status, http_status, hits, now_iso, success_at),
        )
        conn.commit()
        conn.close()
        return

    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO higas_barcode_enrich_state
            (barcode, store_id, status, http_status, hits, last_attempted_at, last_success_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            store_id = excluded.store_id,
            status = excluded.status,
            http_status = excluded.http_status,
            hits = excluded.hits,
            last_attempted_at = excluded.last_attempted_at,
            last_success_at = COALESCE(excluded.last_success_at, higas_barcode_enrich_state.last_success_at)
        """,
        (barcode, store_id, status, http_status, hits, now_iso, success_at),
    )
    conn.commit()
    conn.close()


def _upsert_states_bulk(
    db: DatabaseManager,
    rows: Sequence[tuple],
) -> None:
    if not rows:
        return

    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO higas_barcode_enrich_state
                (barcode, store_id, status, http_status, hits, last_attempted_at, last_success_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (barcode) DO UPDATE SET
                store_id = EXCLUDED.store_id,
                status = EXCLUDED.status,
                http_status = EXCLUDED.http_status,
                hits = EXCLUDED.hits,
                last_attempted_at = EXCLUDED.last_attempted_at,
                last_success_at = COALESCE(EXCLUDED.last_success_at, higas_barcode_enrich_state.last_success_at)
            """,
            list(rows),
        )
        conn.commit()
        conn.close()
        return

    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO higas_barcode_enrich_state
            (barcode, store_id, status, http_status, hits, last_attempted_at, last_success_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            store_id = excluded.store_id,
            status = excluded.status,
            http_status = excluded.http_status,
            hits = excluded.hits,
            last_attempted_at = excluded.last_attempted_at,
            last_success_at = COALESCE(excluded.last_success_at, higas_barcode_enrich_state.last_success_at)
        """,
        list(rows),
    )
    conn.commit()
    conn.close()


def _resolve_store_context(scraper: HigasDepartamentosScraper, zip_code: str, store_id: str) -> None:
    """Best effort: apply partner/subdomain context for the provided store_id."""
    lookup_url = "https://api.instabuy.com.br/apiv3/store"
    params = {
        "partner_id": scraper.PARTNER_ID,
        "zip_code": "".join(ch for ch in str(zip_code or "") if ch.isdigit()),
    }
    try:
        response = scraper.session.get(lookup_url, params=params, timeout=20)
        if response.status_code != 200:
            return
        stores = (response.json() or {}).get("data") or []
        for store in stores:
            if str((store or {}).get("id") or "").strip() == str(store_id).strip():
                scraper._apply_store_context(store)
                return
    except Exception:
        return


def _build_offers_rows(offers: List[Dict[str, Any]], zip_code: str) -> List[tuple]:
    rows: List[tuple] = []
    now_iso = datetime.now().isoformat()
    for offer in offers:
        rows.append(
            (
                offer.get("id"),
                "Higas",
                offer.get("product_name"),
                offer.get("brand"),
                offer.get("description"),
                offer.get("regular_price"),
                offer.get("promo_price"),
                offer.get("promo_min_quantity"),
                offer.get("unit"),
                offer.get("gtin"),
                offer.get("barcode"),
                offer.get("product_url"),
                offer.get("image_url"),
                offer.get("stock_balance"),
                offer.get("stock_general"),
                offer.get("promo_end_at"),
                now_iso,
                offer.get("store_id"),
                zip_code,
                offer.get("sold_quantity"),
                offer.get("offer_name"),
                offer.get("offer_tag"),
                offer.get("app_membership_required"),
            )
        )
    return rows


def _build_barcode_refs(offers: List[Dict[str, Any]]) -> List[tuple]:
    refs: List[tuple] = []
    for offer in offers:
        barcode = offer.get("barcode") or offer.get("gtin")
        offer_id = offer.get("id")
        if not barcode or not offer_id:
            continue
        refs.append((str(barcode), "Higas", str(offer_id), offer.get("product_name") or "", offer.get("brand") or ""))
    return refs


def _backfill_from_known_barcodes(db: DatabaseManager, market_name: str) -> int:
    """Name-based backfill: update existing offers without barcode using known_barcodes catalog.

    For each Higas offer missing a barcode, look up (normalized_name, brand) in the
    cross-market catalog and apply the barcode if found.  Also renames the offer ID
    to market_barcode format (handled inside update_offer_barcode_if_null).
    """
    known_index: Dict[str, str] = {}
    try:
        catalog_rows = db.fetch_all_known_barcodes()
        # row: (barcode, source_market, source_market_id, canonical_name,
        #       canonical_brand, canonical_description, normalized_name, normalized_brand, ...)
        for row in catalog_rows:
            bc = str(row[0]).strip() if row[0] else None
            norm_name = str(row[6]).strip().lower() if row[6] else None
            norm_brand = str(row[7]).strip().lower() if row[7] else None
            if bc and norm_name:
                key = f"{norm_name}|{norm_brand or ''}"
                if key not in known_index:
                    known_index[key] = bc
    except Exception as exc:
        print(f"  {market_name} name-backfill: could not load known_barcodes: {exc}")
        return 0

    try:
        conn = db._get_pg_for_market(market_name)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, product_name, brand FROM offers "
            "WHERE market_name = %s AND (barcode IS NULL OR TRIM(barcode) = '')",
            (market_name,),
        )
        without_barcode = cursor.fetchall()
        conn.close()
    except Exception as exc:
        print(f"  {market_name} name-backfill: could not load offers: {exc}")
        return 0

    updated = 0
    for offer_id, product_name, brand in without_barcode:
        name_key = (product_name or "").strip().lower()
        brand_key = (brand or "").strip().lower()
        matched = (
            known_index.get(f"{name_key}|{brand_key}")
            or known_index.get(f"{name_key}|")
        )
        if matched and db.update_offer_barcode_if_null(str(offer_id), matched):
            updated += 1

    return updated


def _delete_stale_native_id_rows(db: DatabaseManager, offers: List[Dict[str, Any]]) -> int:
    """Delete old barcode-less scraped rows for products now enriched with a barcode.

    When enrichment finds a product via barcode search, it saves a new row with
    id=higas_<barcode>_<store_hash>.  The original scraped row (id=higas_<native_id>,
    barcode=NULL) becomes stale and must be removed so the offer table stays clean.
    """
    stale_ids = []
    for offer in offers:
        barcode = offer.get("barcode")
        native_id = offer.get("native_product_id")
        if not barcode or not native_id:
            continue
        stale_id = f"higas_{native_id}"
        stale_ids.append(stale_id)

    if not stale_ids:
        return 0

    removed = 0
    if db.use_postgres:
        conn = db._get_pg_for_market("Higas")
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM offers
            WHERE market_name = 'Higas'
              AND id = ANY(%s)
              AND (barcode IS NULL OR TRIM(barcode) = '')
            """,
            (stale_ids,),
        )
        removed = cursor.rowcount
        conn.commit()
        conn.close()
    return removed


def _flush_chunk(
    *,
    db: DatabaseManager,
    offers_by_id: Dict[str, Dict[str, Any]],
    state_rows: List[tuple],
    zip_code: str,
) -> int:
    _upsert_states_bulk(db, state_rows)
    state_rows.clear()

    if not offers_by_id:
        return 0

    offers = list(offers_by_id.values())
    db.save_offers(_build_offers_rows(offers, zip_code))
    refs = _build_barcode_refs(offers)
    if refs:
        db.save_barcode_references_bulk(refs)
    removed = _delete_stale_native_id_rows(db, offers)
    if removed:
        print(f"  flush: removed {removed} stale barcode-less rows superseded by enriched offers")
    count = len(offers)
    offers_by_id.clear()
    return count


def run_refresh_registered(
    *,
    store_id: Optional[str],
    zip_code: Optional[str],
    max_calls: int,
    base_delay_seconds: float,
    jitter_seconds: float,
    burst_size: int,
    burst_cooldown_seconds: int,
    flush_every_calls: int,
    flush_cooldown_seconds: int,
) -> Dict[str, Any]:
    """Re-query the Instabuy search API for barcodes ALREADY registered in Higas offers.

    The normal run() skips already_registered barcodes (they're in the offers table).
    This function queries those same barcodes again and saves updated prices via
    db.save_offers(), which triggers price_history writes for any changes detected.
    """
    load_env_file()
    db = DatabaseManager()
    scraper = HigasDepartamentosScraper()
    _ensure_state_table(db)

    resolved_zip_code = _resolve_zip_code(zip_code)
    resolved_store_id = str(store_id or "").strip()
    if not resolved_store_id:
        resolved_store_id = str(scraper.resolve_store(resolved_zip_code) or "").strip()
        if not resolved_store_id:
            resolved_store_id = DEFAULT_STORE_ID
    if resolved_store_id == DEFAULT_STORE_ID and not str(store_id or "").strip():
        _resolve_store_context(scraper, resolved_zip_code, resolved_store_id)

    _resolve_store_context(scraper, resolved_zip_code, resolved_store_id)

    print(
        f"Higas refresh-registered context: zip_code={resolved_zip_code} "
        f"store_id={resolved_store_id} partner={scraper._active_partner_id}"
    )

    registered_higas: List[str] = sorted(db.fetch_registered_barcodes_for_market("Higas"))
    total = len(registered_higas)
    print(f"Higas refresh-registered: {total} barcodes already registered in offers — re-querying for price updates")

    safe_call_cap = max(0, int(os.getenv("HIGAS_ENRICHMENT_SAFE_CALL_CAP", str(config.HIGAS_ENRICHMENT_SAFE_CALL_CAP))))
    max_429_cooldowns = max(0, int(os.getenv("HIGAS_ENRICHMENT_MAX_429_COOLDOWNS", str(config.HIGAS_ENRICHMENT_MAX_429_COOLDOWNS))))
    effective_max_calls = min(max_calls, safe_call_cap) if safe_call_cap > 0 else max_calls
    current_base_delay = max(0.0, base_delay_seconds)
    current_jitter = max(0.0, jitter_seconds)
    current_burst_size = max(0, burst_size)
    cooldowns_429 = 0

    offers_by_id: Dict[str, Dict[str, Any]] = {}
    pending_state_rows: List[tuple] = []
    calls = 0
    hits = 0
    failures = 0
    processed = 0
    offers_saved_total = 0

    for barcode in registered_higas:
        if calls >= effective_max_calls:
            break

        params = {
            "search_barcode": barcode,
            "platform": "store_android",
            "version": "570",
            "store_id": resolved_store_id,
            "partner_id": scraper._active_partner_id,
        }

        try:
            response = scraper._get_with_retry(
                "https://api.instabuy.com.br/apiv3/search",
                params=params,
                max_retries=1,
            )
            calls += 1
            processed += 1

            if response.status_code == 429:
                failures += 1
                cooldowns_429 += 1
                retry_after_raw = response.headers.get("Retry-After") if response.headers else None
                try:
                    retry_after = int(str(retry_after_raw or "0").strip())
                except ValueError:
                    retry_after = 0
                wait_seconds = max(retry_after, max(8, burst_cooldown_seconds * 2))
                current_base_delay = min(max(0.10, current_base_delay * 1.25), 0.40)
                current_burst_size = max(80, int(current_burst_size * 0.9)) if current_burst_size > 0 else 0
                print(
                    f"429 detected (cooldown {cooldowns_429}/{max_429_cooldowns}); "
                    f"sleeping {wait_seconds}s, base_delay={current_base_delay:.2f}"
                )
                time.sleep(wait_seconds)
                if max_429_cooldowns > 0 and cooldowns_429 >= max_429_cooldowns:
                    print("Reached max 429 cooldowns; stopping early.")
                    break
                continue

            if response.status_code != 200:
                failures += 1
                continue

            data = (response.json() or {}).get("data") or []
            if data:
                hits += 1
            for product in data:
                if not isinstance(product, dict):
                    continue
                standardized = scraper._standardize_product(
                    product,
                    zip_code=resolved_zip_code,
                    store_id=resolved_store_id,
                )
                if not standardized:
                    continue
                standardized["barcode"] = barcode
                if not scraper.db.normalize_barcode(standardized.get("gtin")):
                    standardized["gtin"] = barcode
                new_id = scraper.db.build_offer_id("higas", resolved_store_id, barcode, barcode)
                if new_id:
                    standardized["id"] = new_id
                offers_by_id[standardized["id"]] = standardized

            if calls % 25 == 0:
                print(
                    f"refresh progress calls={calls}/{effective_max_calls} processed={processed}/{total} "
                    f"hits={hits} buffered={len(offers_by_id)} failures={failures}"
                )

            if flush_every_calls > 0 and calls % flush_every_calls == 0 and offers_by_id:
                flushed = _flush_chunk(
                    db=db, offers_by_id=offers_by_id,
                    state_rows=pending_state_rows, zip_code=resolved_zip_code,
                )
                offers_saved_total += flushed
                print(f"  flush checkpoint: {flushed} offers saved")
                if flush_cooldown_seconds > 0:
                    time.sleep(flush_cooldown_seconds)

            if current_burst_size > 0 and calls % current_burst_size == 0:
                print(f"burst cooldown: sleeping {burst_cooldown_seconds}s after {calls} calls")
                time.sleep(max(0, burst_cooldown_seconds))
            else:
                delay = current_base_delay + random.uniform(0.0, current_jitter)
                time.sleep(delay)

        except Exception as exc:
            failures += 1
            print(f"  refresh error barcode={barcode}: {exc}")
            continue

    flushed = _flush_chunk(
        db=db, offers_by_id=offers_by_id,
        state_rows=pending_state_rows, zip_code=resolved_zip_code,
    )
    offers_saved_total += flushed

    done = processed >= total
    result = {
        "store_id": resolved_store_id,
        "zip_code": resolved_zip_code,
        "registered_total": total,
        "processed_in_run": processed,
        "calls": calls,
        "effective_max_calls": effective_max_calls,
        "cooldowns_429": cooldowns_429,
        "hits": hits,
        "failures": failures,
        "offers_saved": offers_saved_total,
        "remaining_after_run": max(0, total - processed),
        "completed": done,
    }
    print(f"Higas refresh-registered summary: {result}")
    return result


def run(
    *,
    store_id: Optional[str],
    zip_code: Optional[str],
    max_calls: int,
    base_delay_seconds: float,
    jitter_seconds: float,
    burst_size: int,
    burst_cooldown_seconds: int,
    flush_every_calls: int,
    flush_cooldown_seconds: int,
) -> Dict[str, Any]:
    load_env_file()
    db = DatabaseManager()
    scraper = HigasDepartamentosScraper()
    _ensure_state_table(db)

    resolved_zip_code = _resolve_zip_code(zip_code)

    resolved_store_id = str(store_id or "").strip()
    if not resolved_store_id:
        resolved_store_id = str(scraper.resolve_store(resolved_zip_code) or "").strip()
        if not resolved_store_id:
            resolved_store_id = DEFAULT_STORE_ID
    if resolved_store_id == DEFAULT_STORE_ID and not str(store_id or "").strip():
        _resolve_store_context(scraper, resolved_zip_code, resolved_store_id)

    # Resolve metadata context using the active ZIP for correct partner/subdomain.
    _resolve_store_context(scraper, resolved_zip_code, resolved_store_id)

    print(
        f"Higas barcode enrich context: zip_code={resolved_zip_code} "
        f"store_id={resolved_store_id} partner={scraper._active_partner_id}"
    )

    registered_higas = set(db.fetch_registered_barcodes_for_market("Higas"))
    catalog_rows = db.fetch_barcode_reference_catalog()
    source_barcodes = _normalize_barcode_set([row[2] for row in catalog_rows], db)
    completed_barcodes = _fetch_completed_barcodes(db, resolved_store_id)

    pending = [
        barcode
        for barcode in source_barcodes
        if barcode not in registered_higas and barcode not in completed_barcodes
    ]
    total_pending = len(pending)

    safe_call_cap = max(0, int(os.getenv("HIGAS_ENRICHMENT_SAFE_CALL_CAP", str(config.HIGAS_ENRICHMENT_SAFE_CALL_CAP))))
    max_429_cooldowns = max(0, int(os.getenv("HIGAS_ENRICHMENT_MAX_429_COOLDOWNS", str(config.HIGAS_ENRICHMENT_MAX_429_COOLDOWNS))))
    effective_max_calls = min(max_calls, safe_call_cap) if safe_call_cap > 0 else max_calls
    current_base_delay = max(0.0, base_delay_seconds)
    current_jitter = max(0.0, jitter_seconds)
    current_burst_size = max(0, burst_size)
    cooldowns_429 = 0

    print(
        f"Higas barcode enrich: store_id={resolved_store_id} pending={total_pending} "
        f"already_registered={len(registered_higas)} completed_state={len(completed_barcodes)} "
        f"max_calls={max_calls} effective_max_calls={effective_max_calls} safe_call_cap={safe_call_cap}"
    )

    offers_by_id: Dict[str, Dict[str, Any]] = {}
    pending_state_rows: List[tuple] = []
    calls = 0
    hits = 0
    failures = 0
    processed = 0
    offers_saved_total = 0

    for barcode in pending:
        if calls >= effective_max_calls:
            break

        params = {
            "search_barcode": barcode,
            "platform": "store_android",
            "version": "570",
            "store_id": resolved_store_id,
            "partner_id": scraper._active_partner_id,
        }

        try:
            response = scraper._get_with_retry(
                "https://api.instabuy.com.br/apiv3/search",
                params=params,
                max_retries=1,
            )
            calls += 1
            processed += 1
            if response.status_code == 429:
                failures += 1
                cooldowns_429 += 1
                now_iso = datetime.now().isoformat()
                pending_state_rows.append(
                    (
                        barcode,
                        resolved_store_id,
                        "http_error",
                        429,
                        0,
                        now_iso,
                        None,
                    )
                )

                retry_after_raw = response.headers.get("Retry-After") if response.headers else None
                try:
                    retry_after = int(str(retry_after_raw or "0").strip())
                except ValueError:
                    retry_after = 0

                wait_seconds = max(retry_after, max(8, burst_cooldown_seconds * 2))
                current_base_delay = min(max(0.10, current_base_delay * 1.25), 0.40)
                current_burst_size = max(80, int(current_burst_size * 0.9)) if current_burst_size > 0 else 0

                print(
                    f"429 detected (cooldown {cooldowns_429}/{max_429_cooldowns}); "
                    f"sleeping {wait_seconds}s, base_delay={current_base_delay:.2f}, "
                    f"burst_size={current_burst_size}"
                )
                time.sleep(wait_seconds)

                if max_429_cooldowns > 0 and cooldowns_429 >= max_429_cooldowns:
                    print("Reached max 429 cooldowns in this round; stopping early to avoid hard block.")
                    break
                continue

            if response.status_code != 200:
                failures += 1
                now_iso = datetime.now().isoformat()
                pending_state_rows.append(
                    (
                        barcode,
                        resolved_store_id,
                        "http_error",
                        int(response.status_code),
                        0,
                        now_iso,
                        None,
                    )
                )
                continue

            data = (response.json() or {}).get("data") or []
            if data:
                hits += 1
            for product in data:
                if not isinstance(product, dict):
                    continue
                standardized = scraper._standardize_product(
                    product,
                    zip_code=resolved_zip_code,
                    store_id=resolved_store_id,
                )
                if not standardized:
                    continue
                # The Instabuy search endpoint is queried by barcode; use that barcode
                # as canonical for persistence even when the payload omits/mismatches it.
                standardized["barcode"] = barcode
                if not scraper.db.normalize_barcode(standardized.get("gtin")):
                    standardized["gtin"] = barcode

                new_id = scraper.db.build_offer_id("higas", resolved_store_id, barcode, barcode)
                if new_id:
                    standardized["id"] = new_id
                offers_by_id[standardized["id"]] = standardized

            now_iso = datetime.now().isoformat()
            pending_state_rows.append(
                (
                    barcode,
                    resolved_store_id,
                    "found" if data else "not_found",
                    200,
                    len(data),
                    now_iso,
                    now_iso,
                )
            )

            if calls % 25 == 0:
                print(
                    f"progress calls={calls}/{effective_max_calls} processed={processed}/{total_pending} "
                    f"hits={hits} offers={len(offers_by_id)} failures={failures}"
                )

            if current_burst_size > 0 and calls % current_burst_size == 0:
                print(f"burst cooldown: sleeping {burst_cooldown_seconds}s after {calls} calls")
                time.sleep(max(0, burst_cooldown_seconds))
            else:
                delay = current_base_delay + random.uniform(0.0, current_jitter)
                time.sleep(delay)

            if flush_every_calls > 0 and calls % flush_every_calls == 0:
                flushed = _flush_chunk(
                    db=db,
                    offers_by_id=offers_by_id,
                    state_rows=pending_state_rows,
                    zip_code=resolved_zip_code,
                )
                offers_saved_total += flushed
                print(
                    f"flush checkpoint at call {calls}: flushed_offers={flushed} "
                    f"state_rows_saved, cooling {flush_cooldown_seconds}s"
                )
                time.sleep(max(0, flush_cooldown_seconds))
        except Exception:
            failures += 1
            calls += 1
            processed += 1
            now_iso = datetime.now().isoformat()
            pending_state_rows.append(
                (
                    barcode,
                    resolved_store_id,
                    "exception",
                    None,
                    0,
                    now_iso,
                    None,
                )
            )

    flushed = _flush_chunk(
        db=db,
        offers_by_id=offers_by_id,
        state_rows=pending_state_rows,
        zip_code=resolved_zip_code,
    )
    offers_saved_total += flushed

    done = processed >= total_pending

    result = {
        "store_id": resolved_store_id,
        "zip_code": resolved_zip_code,
        "source_barcodes": len(source_barcodes),
        "pending": total_pending,
        "processed_in_run": processed,
        "calls": calls,
        "effective_max_calls": effective_max_calls,
        "cooldowns_429": cooldowns_429,
        "hits": hits,
        "failures": failures,
        "offers_saved": offers_saved_total,
        "remaining_after_run": max(0, total_pending - processed),
        "completed": done,
        "state_table": "higas_barcode_enrich_state",
    }
    print(f"Higas barcode enrich summary: {result}")

    # Name-based backfill: match existing barcodeless Higas offers against the
    # cross-market catalog by product name.  This fills in barcodes that the API
    # search didn't cover and also renames offer IDs to market_barcode format.
    print("Higas: running name-based backfill from known_barcodes catalog...")
    name_backfill_count = _backfill_from_known_barcodes(db, "Higas")
    print(f"Higas: name-backfill updated {name_backfill_count} offers with barcodes")
    result["name_backfill_updated"] = name_backfill_count

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich Higas offers by querying Instabuy search_barcode API with barcodes already mapped in DB. "
            "Includes call-budget + checkpoint strategy to avoid API blocks around ~700 calls."
        )
    )
    parser.add_argument(
        "--store-id",
        default=None,
        help="Optional fixed Higas store_id. If omitted, store is auto-resolved from ZIP.",
    )
    parser.add_argument(
        "--zip-code",
        default=None,
        help="Optional ZIP code. If omitted, uses SCRAPE_ZIP_CODE or auto-detects location.",
    )
    parser.add_argument("--max-calls", type=int, default=int(os.getenv("HIGAS_ENRICHMENT_MAX_CALLS", "650")), help="Max API calls per run (keep under block threshold)")
    parser.add_argument("--base-delay", type=float, default=float(os.getenv("HIGAS_ENRICHMENT_BASE_DELAY", "0.14")), help="Base delay between API calls in seconds")
    parser.add_argument("--jitter", type=float, default=float(os.getenv("HIGAS_ENRICHMENT_JITTER_SECONDS", "0.05")), help="Random jitter seconds added to each delay")
    parser.add_argument("--burst-size", type=int, default=int(os.getenv("HIGAS_ENRICHMENT_BURST_SIZE", "140")), help="Pause every N calls")
    parser.add_argument("--burst-cooldown", type=int, default=int(os.getenv("HIGAS_ENRICHMENT_BURST_COOLDOWN_SECONDS", "20")), help="Cooldown seconds after each burst")
    parser.add_argument("--flush-every-calls", type=int, default=int(os.getenv("HIGAS_ENRICHMENT_FLUSH_EVERY_CALLS", "120")), help="Persist offers/state every N calls")
    parser.add_argument("--flush-cooldown", type=int, default=int(os.getenv("HIGAS_ENRICHMENT_FLUSH_COOLDOWN_SECONDS", "8")), help="Cooldown seconds after each DB flush checkpoint")
    parser.add_argument(
        "--run-until-done",
        action="store_true",
        help="Run repeated batches until pending barcodes are exhausted",
    )
    parser.add_argument(
        "--between-runs-cooldown",
        type=int,
            default=int(os.getenv("HIGAS_ENRICHMENT_BETWEEN_RUNS_COOLDOWN_SECONDS", str(config.HIGAS_ENRICHMENT_BETWEEN_RUNS_COOLDOWN_SECONDS))),
        help="Cooldown seconds between full batch runs when --run-until-done is enabled",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=200,
        help="Safety cap for number of rounds when --run-until-done is enabled",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help=(
            "Delete all 'found' entries from higas_barcode_enrich_state before running. "
            "Re-queries every previously found barcode to refresh prices. "
            "'not_found' entries are kept to avoid wasting calls on absent products."
        ),
    )
    parser.add_argument(
        "--refresh-registered",
        action="store_true",
        help=(
            "Re-query all barcodes already registered in Higas offers to refresh their prices. "
            "Unlike the normal run, this deliberately re-queries already_registered barcodes, "
            "which saves any price changes detected via save_offers / price_history."
        ),
    )
    args = parser.parse_args()

    run_kwargs = {
        "store_id": str(args.store_id).strip() if args.store_id else None,
        "zip_code": str(args.zip_code).strip() if args.zip_code else None,
        "max_calls": max(1, int(args.max_calls)),
        "base_delay_seconds": float(args.base_delay),
        "jitter_seconds": float(args.jitter),
        "burst_size": max(0, int(args.burst_size)),
        "burst_cooldown_seconds": max(0, int(args.burst_cooldown)),
        "flush_every_calls": max(0, int(args.flush_every_calls)),
        "flush_cooldown_seconds": max(0, int(args.flush_cooldown)),
    }

    if args.reset_state:
        load_env_file()
        _db = DatabaseManager()
        _ensure_state_table(_db)
        deleted = _reset_found_state(_db)
        print(f"--reset-state: deleted {deleted} 'found' rows from higas_barcode_enrich_state. Re-enrichment will re-check all of them.")

    if args.refresh_registered:
        if not args.run_until_done:
            run_refresh_registered(**run_kwargs)
            return

        max_rounds = max(1, int(args.max_rounds))
        between_runs = max(0, int(args.between_runs_cooldown))
        final_result: Optional[Dict[str, Any]] = None
        for round_index in range(1, max_rounds + 1):
            print(f"\n=== Higas refresh-registered round {round_index}/{max_rounds} ===")
            final_result = run_refresh_registered(**run_kwargs)
            if final_result.get("completed"):
                print("All registered barcodes refreshed.")
                break
            if final_result.get("processed_in_run", 0) <= 0:
                print("No progress in this round; stopping to avoid tight loop.")
                break
            if round_index < max_rounds:
                print(f"Cooling {between_runs}s before next round...")
                time.sleep(between_runs)
        if final_result and not final_result.get("completed"):
            print("Stopped before full completion. Re-run with --refresh-registered --run-until-done to continue.")
        return

    if not args.run_until_done:
        run(**run_kwargs)
        return

    max_rounds = max(1, int(args.max_rounds))
    between_runs = max(0, int(args.between_runs_cooldown))
    final_result: Optional[Dict[str, Any]] = None
    for round_index in range(1, max_rounds + 1):
        print(f"\n=== Higas enrich round {round_index}/{max_rounds} ===")
        final_result = run(**run_kwargs)
        if final_result.get("completed"):
            print("All pending barcodes processed.")
            break
        if final_result.get("processed_in_run", 0) <= 0:
            print("No progress in this round; stopping to avoid tight loop.")
            break
        if round_index < max_rounds:
            print(f"Cooling {between_runs}s before next round...")
            time.sleep(between_runs)

    if final_result and not final_result.get("completed"):
        print("Stopped before full completion. Re-run with --run-until-done to continue.")


if __name__ == "__main__":
    main()
