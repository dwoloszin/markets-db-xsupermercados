import csv
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from env_loader import load_env_file
import config
from db.historical_pricing_analyzer import HistoricalPricingAnalyzer


class DatabaseManager:
    VALID_GTIN_LENGTHS = {8, 12, 13, 14}
    MARKET_ATACADAO = "Atacad\u00e3o"
    BARCODE_REFERENCE_MARKET_COLUMNS = {
        "Rossi": "rossi_id",
        MARKET_ATACADAO: "atacadao_id",
        "AtacadÃ£o": "atacadao_id",
        "Atacad�o": "atacadao_id",
        "Nagumo": "nagumo_id",
        "Higas": "higas_id",
        "Swift": "swift_id",
        "Sonda Delivery": "sonda_id",
        "XSupermercados": "xsupermercados_id",
        "Barbosa": "barbosa_id",
        "Oba Hortifruti": "oba_id",
        "Extra": "extra_id",
        "Pão de Açúcar": "paodeacucar_id",
        "Tenda Atacado": "tenda_id",
        "Davo": "davo_id",
        "Giga": "giga_id",
    }
    CANONICAL_MARKET_NAMES = {
        "AtacadÃ£o": MARKET_ATACADAO,
        "Atacad�o": MARKET_ATACADAO,
    }
    MARKET_DB_ENV_SUFFIX = {
        "Rossi": "ROSSI",
        MARKET_ATACADAO: "ATACADAO",
        "Nagumo": "NAGUMO",
        "Higas": "HIGAS",
        "Swift": "SWIFT",
        "Sonda Delivery": "SONDA",
        "XSupermercados": "XSUPERMERCADOS",
        "Barbosa": "BARBOSA",
        "Carrefour": "CARREFOUR",
        "Oba Hortifruti": "OBA",
        "Extra": "EXTRA",
        "Pão de Açúcar": "PAODEACUCAR",
        "Tenda Atacado": "TENDA",
        "Sam's Club":    "SAMSCLUB",
        "Davo": "DAVO",
        "Giga": "GIGA",
    }
    OFFER_ID_PREFIX_TO_MARKET = {
        "rossi": "Rossi",
        "atacadao": MARKET_ATACADAO,
        "nagumo": "Nagumo",
        "higas": "Higas",
        "swift": "Swift",
        "sonda": "Sonda Delivery",
        "xsupermercados": "XSupermercados",
        "barbosa": "Barbosa",
        "carrefour": "Carrefour",
        "oba": "Oba Hortifruti",
        "extra": "Extra",
        "paodeacucar": "Pão de Açúcar",
        "tenda": "Tenda Atacado",
        "samsclub": "Sam's Club",
        "davo": "Davo",
        "giga": "Giga",
    }
    COMMON_TABLES = {
        "barcode_reference_market_map",
        "store_mappings",
        "match_audit",
        "model_inference_audit",
        "barcode_inference_state",
        "barcode_fingerprint_cache",
        "process_timing",
        "product_catalog",
        "higas_barcode_enrich_state",
        "extra_barcode_enrich_state",
        "paodeacucar_barcode_enrich_state",
        "oba_barcode_enrich_state",
        "app_offers",
    }
    COMMON_TABLE_CATEGORY = {
        "product_catalog": "CATALOG",
        "barcode_reference_market_map": "CATALOG",
        "barcode_fingerprint_cache": "CATALOG",
        "barcode_inference_state": "INFERENCE",
        "higas_barcode_enrich_state": "INFERENCE",
        "extra_barcode_enrich_state": "INFERENCE",
        "paodeacucar_barcode_enrich_state": "INFERENCE",
        "oba_barcode_enrich_state": "INFERENCE",
        "app_offers": "INFERENCE",
        "process_timing": "TIMING",
        "match_audit": "AUDIT",
        "model_inference_audit": "AUDIT",
        "store_mappings": "STORE",
    }
    MARKET_TABLES = {
        "offers",
        "price_history",
        "store_pricing_insights",
        "product_price_patterns",
    }

    @classmethod
    def _canonical_market_name(cls, market_name: Optional[object]) -> str:
        raw = str(market_name or "").strip()
        if not raw:
            return ""
        lowered = raw.casefold()
        if lowered.startswith("atacad") and (
            "\u00e3o" in raw
            or "\ufffdo" in raw
            or "\u00c3\u00a3o" in raw
        ):
            return cls.MARKET_ATACADAO
        return cls.CANONICAL_MARKET_NAMES.get(raw, raw)

    @classmethod
    def _market_name_aliases(cls, market_name: Optional[object]) -> List[str]:
        canonical = cls._canonical_market_name(market_name)
        if not canonical:
            return []
        aliases = {canonical}
        for alias, canonical_name in cls.CANONICAL_MARKET_NAMES.items():
            if canonical_name == canonical:
                aliases.add(alias)
        return sorted(aliases)

    def __init__(self):
        load_env_file()
        self.database_url_manager = (
            os.getenv("DATABASE_URL_MANAGER", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        self.market_database_urls = self._load_market_database_urls()
        self.common_database_urls = self._load_common_database_urls()
        self._initialized_market_db_urls: set = set()
        self._initialized_common_db_urls: set = set()
        # Backward compatibility for helper scripts that still read this attribute.
        self.database_url = self.database_url_manager
        # Backward compatibility for legacy checks in helper scripts/methods.
        # This project now runs PostgreSQL-only, so this flag is always True.
        self.use_postgres = True
        if not self.database_url_manager:
            raise RuntimeError(
                "DATABASE_URL_MANAGER (or fallback DATABASE_URL) is not set. "
                "PostgreSQL is required. Please set it in your .env file."
            )
        self._init_db()

    def _load_market_database_urls(self) -> Dict[str, str]:
        urls: Dict[str, str] = {}
        for market_name, suffix in self.MARKET_DB_ENV_SUFFIX.items():
            canonical_market = self._canonical_market_name(market_name)
            primary_key = f"DATABASE_URL_{suffix}"
            legacy_key = f"DATABASE_URL_MARKET_{suffix}"
            market_url = os.getenv(primary_key, "").strip() or os.getenv(legacy_key, "").strip()
            if market_url:
                urls[canonical_market] = market_url
        return urls

    def _load_common_database_urls(self) -> Dict[str, str]:
        urls: Dict[str, str] = {}
        categories = {category for category in self.COMMON_TABLE_CATEGORY.values()}
        for category in categories:
            key = f"DATABASE_URL_COMMON_{category}"
            url = os.getenv(key, "").strip()
            if url:
                urls[category] = url
        return urls

    def _get_market_database_url(self, market_name: Optional[str]) -> str:
        canonical_market = self._canonical_market_name(market_name)
        if canonical_market and canonical_market in self.market_database_urls:
            return self.market_database_urls[canonical_market]
        return self.database_url_manager

    def _get_common_table_database_url(self, table_name: str) -> str:
        normalized = str(table_name or "").strip().lower()
        category = self.COMMON_TABLE_CATEGORY.get(normalized)
        if category and category in self.common_database_urls:
            return self.common_database_urls[category]
        return self.database_url_manager

    def _iter_common_database_urls(self) -> List[str]:
        deduped: List[str] = []
        seen: set = set()
        for url in self.common_database_urls.values():
            clean_url = str(url or "").strip()
            if not clean_url or clean_url in seen:
                continue
            seen.add(clean_url)
            deduped.append(clean_url)
        return deduped

    def _iter_market_database_urls(self) -> List[str]:
        # Only include manager URL if some market has no dedicated URL and falls back to it.
        has_fallback_market = len(self.market_database_urls) < len(self.MARKET_DB_ENV_SUFFIX)
        sources = (
            [self.database_url_manager, *self.market_database_urls.values()]
            if has_fallback_market
            else list(self.market_database_urls.values())
        )
        deduped: List[str] = []
        seen: set = set()
        for url in sources:
            clean_url = str(url or "").strip()
            if not clean_url or clean_url in seen:
                continue
            seen.add(clean_url)
            deduped.append(clean_url)
        return deduped

    @classmethod
    def _market_from_offer_id(cls, offer_id: Optional[str]) -> Optional[str]:
        if not offer_id:
            return None
        prefix = str(offer_id).split("_", 1)[0].strip().casefold()
        return cls.OFFER_ID_PREFIX_TO_MARKET.get(prefix)

    def _group_markets_by_database(self, markets: Sequence[str]) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for market_name in markets:
            canonical_market = self._canonical_market_name(market_name)
            if not canonical_market:
                continue
            db_url = self._get_market_database_url(canonical_market)
            grouped.setdefault(db_url, [])
            if canonical_market not in grouped[db_url]:
                grouped[db_url].append(canonical_market)
        return grouped

    def _get_pg(self):
        return self._connect_pg(self.database_url_manager)

    def _get_pg_for_market(self, market_name: Optional[str]):
        market_url = self._get_market_database_url(market_name)
        self._ensure_market_db_initialized(market_url)
        return self._connect_pg(market_url)

    def _get_pg_for_common_table(self, table_name: str):
        common_url = self._get_common_table_database_url(table_name)
        self._ensure_common_db_initialized(common_url)
        return self._connect_pg(common_url)

    @staticmethod
    def _is_truthy(value: str) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _should_init_all_market_dbs_on_startup(self) -> bool:
        env_raw = os.getenv("DB_INIT_ALL_MARKET_DBS_ON_STARTUP")
        if env_raw is not None and str(env_raw).strip() != "":
            return self._is_truthy(env_raw)
        return bool(getattr(config, "DB_INIT_ALL_MARKET_DBS_ON_STARTUP", False))

    def _ensure_market_db_initialized(self, database_url: str) -> None:
        clean_url = str(database_url or "").strip()
        if not clean_url or clean_url == self.database_url_manager:
            return
        if clean_url in self._initialized_market_db_urls:
            return
        self._run_pg_init_with_retry(clean_url, "market")
        self._initialized_market_db_urls.add(clean_url)

    def _ensure_common_db_initialized(self, database_url: str) -> None:
        clean_url = str(database_url or "").strip()
        if not clean_url or clean_url == self.database_url_manager:
            return
        if clean_url in self._initialized_common_db_urls:
            return
        self._run_pg_init_with_retry(clean_url, "manager")
        self._initialized_common_db_urls.add(clean_url)

    @staticmethod
    def _is_transient_pg_error(exc: Exception) -> bool:
        msg = str(exc or "").lower()
        transient_markers = (
            "ssl connection has been closed unexpectedly",
            "ssl error: bad length",
            "ssl syscall error: eof detected",
            "eof detected",
            "server closed the connection unexpectedly",
            "connection not open",
            "connection reset",
            "broken pipe",
            "timeout",
            "could not connect to server",
            "connection refused",
            "terminating connection due to administrator command",
            "deadlock detected",
            "could not serialize access",
            "serialization failure",
            "lock not available",
            "canceling statement due to lock timeout",
        )
        if any(marker in msg for marker in transient_markers):
            return True

        # psycopg SQLSTATEs for retryable concurrency/availability conditions.
        sqlstate = str(getattr(exc, "sqlstate", "") or getattr(exc, "pgcode", "") or "").upper()
        return sqlstate in {
            "40P01",  # deadlock_detected
            "40001",  # serialization_failure
            "55P03",  # lock_not_available
            "57014",  # query_canceled (e.g., lock timeout)
        }

    def _connect_pg(self, database_url: str):
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL URL is set but psycopg is not installed. Add 'psycopg[binary]' to requirements."
            ) from exc

        max_retries = 3
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                return psycopg.connect(database_url, connect_timeout=20)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries or not self._is_transient_pg_error(exc):
                    raise
                wait_seconds = min(6.0, 1.5 * attempt)
                print(
                    f"Postgres connect transient error (attempt {attempt}/{max_retries}): {exc}. "
                    f"Retrying in {wait_seconds:.1f}s..."
                )
                time.sleep(wait_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("PostgreSQL connection failed without an explicit error.")

    def _init_db(self):
        self._init_db_postgres()

    def _run_pg_init_with_retry(self, database_url: str, scope: str) -> None:
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            conn = None
            try:
                conn = self._connect_pg(database_url)
                cursor = conn.cursor()
                if scope == "manager":
                    self._create_common_tables_postgres(cursor)
                    self._cleanup_invalid_barcodes_common_postgres(cursor)
                    self._create_indexes_common_postgres(cursor)
                    self._migrate_legacy_barcode_references_to_market_map_postgres(cursor)
                    self._migrate_barcode_inference_state(cursor)
                    self._ensure_barcode_fingerprint_cache(cursor)
                    self._enable_rls_postgres(cursor, scope="manager")
                else:
                    self._create_market_tables_postgres(cursor)
                    self._cleanup_invalid_barcodes_market_postgres(cursor)
                    self._dedupe_postgres_offers_by_market_barcode(cursor)
                    self._backfill_price_history_store_id_postgres(cursor)
                    self._create_indexes_market_postgres(cursor)
                    self._enable_rls_postgres(cursor, scope="market")
                conn.commit()
                return
            except Exception as exc:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if attempt >= max_retries or not self._is_transient_pg_error(exc):
                    raise
                wait_seconds = min(6.0, 1.5 * attempt)
                print(
                    f"Postgres init transient error [{scope}] (attempt {attempt}/{max_retries}): {exc}. "
                    f"Retrying in {wait_seconds:.1f}s..."
                )
                time.sleep(wait_seconds)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _init_db_postgres(self):
        self._run_pg_init_with_retry(self.database_url_manager, "manager")

        for common_db_url in self._iter_common_database_urls():
            if common_db_url == self.database_url_manager:
                continue
            self._run_pg_init_with_retry(common_db_url, "manager")
            self._initialized_common_db_urls.add(common_db_url)

        if self._should_init_all_market_dbs_on_startup():
            for market_db_url in self._iter_market_database_urls():
                if market_db_url == self.database_url_manager:
                    continue
                self._run_pg_init_with_retry(market_db_url, "market")
                self._initialized_market_db_urls.add(market_db_url)

    def _create_market_tables_postgres(self, cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY,
                market_name TEXT NOT NULL,
                product_name TEXT,
                brand TEXT,
                description TEXT,
                regular_price DOUBLE PRECISION,
                promo_price DOUBLE PRECISION,
                promo_min_quantity INTEGER,
                unit TEXT,
                gtin TEXT,
                barcode TEXT,
                product_url TEXT,
                image_url TEXT,
                stock_balance INTEGER,
                stock_general INTEGER,
                promo_end_at TIMESTAMP,
                last_updated TIMESTAMP NOT NULL,
                store_id TEXT,
                sold_quantity INTEGER,
                offer_name TEXT,
                offer_tag TEXT,
                app_membership_required BOOLEAN
            )
            """
        )
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS promo_min_quantity INTEGER")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS barcode TEXT")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS stock_general INTEGER")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS sold_quantity INTEGER")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS offer_name TEXT")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS offer_tag TEXT")
        cursor.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS app_membership_required BOOLEAN")
        # Migration: drop zip_code column from offers (user ZIP is irrelevant — store_id identifies the store)
        cursor.execute("SAVEPOINT sp_offers_drop_zip")
        try:
            cursor.execute("ALTER TABLE offers DROP COLUMN IF EXISTS zip_code")
            cursor.execute("RELEASE SAVEPOINT sp_offers_drop_zip")
        except Exception:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_offers_drop_zip")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                offer_id TEXT NOT NULL,
                store_id TEXT,
                market_name TEXT NOT NULL,
                product_name TEXT,
                regular_price REAL,
                promo_price REAL,
                offer_name TEXT,
                offer_tag TEXT,
                app_membership_required BOOLEAN,
                recorded_at TIMESTAMP NOT NULL
            )
            """
        )
        cursor.execute("ALTER TABLE price_history ADD COLUMN IF NOT EXISTS store_id TEXT")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS store_pricing_insights (
                market_name TEXT NOT NULL,
                store_id TEXT NOT NULL,
                best_buy_weekday SMALLINT,
                best_buy_weekday_label TEXT,
                best_weekday_avg_discount_pct DOUBLE PRECISION,
                best_weekday_promo_rate DOUBLE PRECISION,
                best_weekday_avg_effective_price DOUBLE PRECISION,
                overall_promo_rate DOUBLE PRECISION,
                overall_avg_discount_pct DOUBLE PRECISION,
                total_price_events INTEGER NOT NULL DEFAULT 0,
                total_products INTEGER NOT NULL DEFAULT 0,
                analyzed_at TIMESTAMP NOT NULL,
                PRIMARY KEY (market_name, store_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS product_price_patterns (
                market_name TEXT NOT NULL,
                store_id TEXT NOT NULL,
                offer_id TEXT PRIMARY KEY,
                product_name TEXT,
                current_regular_price DOUBLE PRECISION,
                current_promo_price DOUBLE PRECISION,
                current_effective_price DOUBLE PRECISION,
                observed_min_price DOUBLE PRECISION,
                observed_max_price DOUBLE PRECISION,
                low_price_mode DOUBLE PRECISION,
                high_price_mode DOUBLE PRECISION,
                price_points_json TEXT,
                pattern_type TEXT,
                samples_count INTEGER NOT NULL DEFAULT 0,
                best_buy_weekday SMALLINT,
                best_buy_weekday_label TEXT,
                best_weekday_price DOUBLE PRECISION,
                avg_toggle_interval_days DOUBLE PRECISION,
                toggle_interval_std_days DOUBLE PRECISION,
                predicted_next_toggle_at TIMESTAMP,
                predicted_next_price DOUBLE PRECISION,
                predicted_direction TEXT,
                prediction_confidence DOUBLE PRECISION,
                prediction_source TEXT,
                last_observed_change_at TIMESTAMP,
                promo_end_at TIMESTAMP,
                analyzed_at TIMESTAMP NOT NULL
            )
            """
        )
        # Legacy compatibility: older schemas may have this column as INTEGER.
        cursor.execute(
            """
            ALTER TABLE price_history
            ALTER COLUMN app_membership_required
            TYPE BOOLEAN
            USING CASE
                WHEN app_membership_required IS NULL THEN NULL
                WHEN app_membership_required::text IN ('1', 't', 'true', 'TRUE') THEN TRUE
                ELSE FALSE
            END
            """
        )

    def _create_common_tables_postgres(self, cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_references (
                barcode TEXT PRIMARY KEY,
                rossi_id TEXT,
                atacadao_id TEXT,
                nagumo_id TEXT,
                higas_id TEXT,
                swift_id TEXT,
                sonda_id TEXT,
                xsupermercados_id TEXT,
                product_name TEXT,
                brand TEXT,
                last_updated TIMESTAMP
            )
            """
        )
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS swift_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS sonda_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS xsupermercados_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS barbosa_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS oba_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS extra_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS paodeacucar_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS tenda_id TEXT")
        cursor.execute("ALTER TABLE barcode_references ADD COLUMN IF NOT EXISTS giga_id TEXT")
        # Migration: drop legacy barcode_references pivot table
        # All data lives in barcode_reference_market_map (normalized long format)
        cursor.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'barcode_references'
                ) THEN
                    DROP TABLE barcode_references;
                END IF;
            END $$;
            """
        )
        

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_reference_market_map (
                barcode TEXT NOT NULL,
                market_name TEXT NOT NULL,
                market_offer_id TEXT NOT NULL,
                product_name TEXT,
                brand TEXT,
                last_updated TIMESTAMP,
                PRIMARY KEY (barcode, market_name)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS store_mappings (
                market_name TEXT NOT NULL,
                store_id     TEXT NOT NULL,
                store_name TEXT,
                store_address TEXT,
                store_city TEXT,
                store_state TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                store_payload TEXT,
                last_successful_update TIMESTAMP,
                PRIMARY KEY (market_name, store_id)
            )
            """
        )
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS store_address TEXT")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS store_city TEXT")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS store_state TEXT")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS store_payload TEXT")
        cursor.execute("ALTER TABLE store_mappings ADD COLUMN IF NOT EXISTS last_successful_update TIMESTAMP")
        # Migration: move from old schemas to (market_name, store_id) composite PK.
        # Old schemas: PRIMARY KEY (zip_code, market_name)  or  PRIMARY KEY (market_name).
        # Uses SAVEPOINT so a failure here never aborts the outer transaction.
        cursor.execute("SAVEPOINT sp_store_mappings_migration")
        try:
            # Drop zip_code if still present (old schema)
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='store_mappings' AND column_name='zip_code'"
            )
            if cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE store_mappings DROP CONSTRAINT IF EXISTS store_mappings_pkey"
                )
                cursor.execute(
                    "ALTER TABLE store_mappings DROP COLUMN IF EXISTS zip_code"
                )

            # Migrate from single-column PK (market_name) to composite (market_name, store_id).
            # Detect by checking if the current PK column list is just "market_name".
            cursor.execute(
                """
                SELECT string_agg(a.attname, ',' ORDER BY x.ordinality)
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN unnest(i.indkey) WITH ORDINALITY AS x(attnum, ordinality)
                    ON TRUE
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = x.attnum
                WHERE c.relname = 'store_mappings' AND i.indisprimary
                """
            )
            pk_row = cursor.fetchone()
            current_pk = pk_row[0] if pk_row else None
            if current_pk and current_pk.strip() == "market_name":
                # Remove rows without a store_id so we can make it NOT NULL
                cursor.execute(
                    "DELETE FROM store_mappings WHERE store_id IS NULL OR TRIM(store_id) = ''"
                )
                cursor.execute(
                    "ALTER TABLE store_mappings DROP CONSTRAINT store_mappings_pkey"
                )
                cursor.execute(
                    "ALTER TABLE store_mappings ALTER COLUMN store_id SET NOT NULL"
                )
                cursor.execute(
                    "ALTER TABLE store_mappings ADD PRIMARY KEY (market_name, store_id)"
                )
            cursor.execute("RELEASE SAVEPOINT sp_store_mappings_migration")
        except Exception:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_store_mappings_migration")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS known_barcodes (
                barcode TEXT PRIMARY KEY,
                source_market TEXT NOT NULL,
                source_market_id TEXT NOT NULL,
                product_name TEXT,
                brand TEXT,
                description TEXT,
                normalized_name TEXT,
                normalized_brand TEXT,
                measure_token TEXT,
                market_count INTEGER NOT NULL DEFAULT 1,
                last_updated TIMESTAMP NOT NULL
            )
            """
        )
        # Add market_count if upgrading from older schema without it
        cursor.execute(
            "ALTER TABLE known_barcodes ADD COLUMN IF NOT EXISTS market_count INTEGER NOT NULL DEFAULT 1"
        )
        # Migration for existing tables that still have the old composite PK.
        # Must run BEFORE creating the unique index on barcode, or the index
        # creation fails with UniqueViolation on duplicate barcode values.
        cursor.execute(
            """
            DO $$
            DECLARE
                old_pk_cols TEXT;
            BEGIN
                -- Check if the current PK includes source_market (old schema)
                SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
                INTO old_pk_cols
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid
                    AND a.attnum = ANY(c.conkey)
                WHERE c.conrelid = 'known_barcodes'::regclass
                  AND c.contype = 'p';

                IF old_pk_cols LIKE '%source_market%' THEN
                    -- Step 1: Remove duplicate barcodes, keep most recently updated row
                    DELETE FROM known_barcodes k
                    WHERE k.ctid <> (
                        SELECT ctid FROM known_barcodes k2
                        WHERE k2.barcode = k.barcode
                        ORDER BY k2.last_updated DESC NULLS LAST, k2.ctid DESC
                        LIMIT 1
                    );
                    -- Step 2: Drop old composite PK
                    ALTER TABLE known_barcodes DROP CONSTRAINT known_barcodes_pkey;
                    -- Step 3: Add barcode as the new PK
                    ALTER TABLE known_barcodes ADD PRIMARY KEY (barcode);
                END IF;
            END $$;
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS match_audit (
                target_offer_id TEXT PRIMARY KEY,
                target_market TEXT NOT NULL,
                inferred_barcode TEXT NOT NULL,
                source_market TEXT NOT NULL,
                source_market_id TEXT NOT NULL,
                match_method TEXT NOT NULL,
                confidence REAL,
                reasoning TEXT,
                last_updated TIMESTAMP NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_inference_state (
                offer_id TEXT PRIMARY KEY,
                offer_signature TEXT NOT NULL,
                catalog_snapshot TEXT,
                matched INTEGER NOT NULL,
                no_match_count INTEGER NOT NULL DEFAULT 0,
                blacklisted INTEGER NOT NULL DEFAULT 0,
                last_attempted_at TIMESTAMP NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS process_timing (
                id SERIAL PRIMARY KEY,
                process_name TEXT NOT NULL,
                step_name TEXT NOT NULL,
                market_name TEXT,
                zip_code TEXT,
                store_id TEXT,
                status TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP NOT NULL,
                run_type TEXT NOT NULL DEFAULT 'individual',
                details TEXT
            )
            """
        )
        cursor.execute(
            "ALTER TABLE process_timing ADD COLUMN IF NOT EXISTS run_type TEXT NOT NULL DEFAULT 'individual'"
        )
        cursor.execute(
            "ALTER TABLE process_timing ADD COLUMN IF NOT EXISTS store_id TEXT"
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS product_catalog (
                barcode          TEXT PRIMARY KEY,
                canonical_name   TEXT NOT NULL,
                canonical_brand  TEXT,
                canonical_description TEXT,
                canonical_unit   TEXT,
                market_count     INTEGER NOT NULL DEFAULT 1,
                source_market    TEXT,
                source_market_id TEXT,
                normalized_name  TEXT,
                normalized_brand TEXT,
                measure_token    TEXT,
                image_url        TEXT,
                last_updated     TIMESTAMP NOT NULL
            )
            """
        )
        # Migration: add new columns if upgrading from old product_catalog schema
        for _col, _defn in [
            ("source_market_id", "TEXT"),
            ("normalized_name",  "TEXT"),
            ("normalized_brand", "TEXT"),
            ("measure_token",    "TEXT"),
            ("image_url",        "TEXT"),
        ]:
            cursor.execute(
                f"ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS {_col} {_defn}"
            )
        # Migration: migrate data from known_barcodes into product_catalog, then drop it
        cursor.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'known_barcodes'
                ) THEN
                    INSERT INTO product_catalog (
                        barcode, canonical_name, canonical_brand,
                        canonical_description, canonical_unit, market_count,
                        source_market, source_market_id,
                        normalized_name, normalized_brand, measure_token,
                        last_updated
                    )
                    SELECT
                        barcode,
                        COALESCE(product_name, ''),
                        brand,
                        description,
                        measure_token,
                        COALESCE(market_count, 1),
                        source_market,
                        source_market_id,
                        normalized_name,
                        normalized_brand,
                        measure_token,
                        last_updated
                    FROM known_barcodes
                    ON CONFLICT (barcode) DO UPDATE SET
                        normalized_name  = COALESCE(EXCLUDED.normalized_name,  product_catalog.normalized_name),
                        normalized_brand = COALESCE(EXCLUDED.normalized_brand, product_catalog.normalized_brand),
                        measure_token    = COALESCE(EXCLUDED.measure_token,    product_catalog.measure_token),
                        source_market_id = COALESCE(EXCLUDED.source_market_id, product_catalog.source_market_id),
                        last_updated     = EXCLUDED.last_updated;
                    DROP TABLE known_barcodes;
                END IF;
            END $$;
            """
        )

    def _backfill_price_history_store_id_postgres(self, cursor) -> None:
        # Populate legacy history rows with current offer store when missing.
        cursor.execute(
            """
            UPDATE price_history AS ph
            SET store_id = o.store_id
            FROM offers AS o
            WHERE ph.offer_id = o.id
              AND (ph.store_id IS NULL OR TRIM(ph.store_id) = '')
              AND o.store_id IS NOT NULL
              AND TRIM(o.store_id) <> ''
            """
        )

    @classmethod
    def _is_valid_gtin(cls, code: str) -> bool:
        """Check if a code is a valid GTIN for configured lengths and checksum."""
        if code is None:
            return False
        text = str(code).strip()
        if not text or not text.isdigit():
            return False
        allowed_lengths = cls._get_allowed_gtin_lengths()
        if len(text) not in allowed_lengths:
            return False
        return cls._passes_gtin_checksum(text)

    @classmethod
    def _get_allowed_gtin_lengths(cls) -> set:
        raw = str(os.getenv("BARCODE_ALLOWED_LENGTHS", "12,13,14")).strip()
        if not raw:
            return {12, 13, 14}

        parsed = set()
        for part in raw.split(","):
            piece = part.strip()
            if piece.isdigit():
                parsed.add(int(piece))

        valid_parsed = parsed & cls.VALID_GTIN_LENGTHS
        return valid_parsed if valid_parsed else {12, 13, 14}

    @staticmethod
    def _passes_gtin_checksum(code: str) -> bool:
        if not code or not code.isdigit() or len(code) < 2:
            return False

        body = code[:-1]
        check_digit = int(code[-1])

        weighted_sum = 0
        weight = 3
        for digit in reversed(body):
            weighted_sum += int(digit) * weight
            weight = 1 if weight == 3 else 3

        expected_check = (10 - (weighted_sum % 10)) % 10
        return expected_check == check_digit

    @classmethod
    def normalize_barcode(cls, value: Optional[object]) -> Optional[str]:
        """Normalize and validate a barcode. Returns None if invalid."""
        if value is None:
            return None
        text = str(value).strip()
        return text if cls._is_valid_gtin(text) else None

    @classmethod
    def build_offer_id(
        cls,
        market_prefix: str,
        store_id: Optional[object],
        barcode: Optional[object] = None,
        gtin: Optional[object] = None,
        product_name: Optional[str] = None,
    ) -> Optional[str]:
        store_hash = cls._store_id_hash(store_id)

        normalized_barcode = cls.normalize_barcode(barcode)
        if normalized_barcode:
            return f"{market_prefix}_{store_hash}_{normalized_barcode}"

        normalized_gtin = cls.normalize_barcode(gtin)
        if normalized_gtin:
            return f"{market_prefix}_{store_hash}_{normalized_gtin}"

        # Stable fallback: normalized product name (no random native IDs)
        if product_name:
            import re as _re
            slug = _re.sub(r"[^a-z0-9]+", "-", (product_name or "").strip().lower()).strip("-")
            if slug:
                return f"{market_prefix}_{store_hash}_{slug}"

        return None

    @staticmethod
    def _store_id_hash(store_id: object) -> str:
        """Return a deterministic 8-char hex hash of store_id for use in offer IDs."""
        return hashlib.md5(str(store_id or "").strip().encode()).hexdigest()[:8]

    @classmethod
    def build_offer_logical_key(
        cls,
        market_name: Optional[object],
        barcode: Optional[object],
        store_id: Optional[object] = None,
    ) -> Optional[Tuple]:
        market_text = str(market_name).strip() if market_name is not None else ""
        normalized_barcode = cls.normalize_barcode(barcode)
        if not market_text or not normalized_barcode:
            return None
        store_text = str(store_id or "").strip()
        return (market_text, normalized_barcode, store_text)

    @staticmethod
    def _select_preferred_existing_offer(
        current: Optional[Dict[str, Any]],
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        if current is None:
            return candidate

        # Only compare expected IDs when barcode is present; barcode-less offers
        # use native_id-based IDs and should never be compared via barcode suffix.
        current_barcode = current.get("barcode") or ""
        candidate_barcode = candidate.get("barcode") or ""
        if current_barcode and candidate_barcode:
            current_expected_id = f"{current['market_name']}_{current_barcode}"
            candidate_expected_id = f"{candidate['market_name']}_{candidate_barcode}"
            current_is_stable = current["id"] == current_expected_id
            candidate_is_stable = candidate["id"] == candidate_expected_id
        else:
            current_is_stable = candidate_is_stable = True
        if current_is_stable != candidate_is_stable:
            return candidate if candidate_is_stable else current

        current_updated = current.get("last_updated") or ""
        candidate_updated = candidate.get("last_updated") or ""
        if candidate_updated > current_updated:
            return candidate
        return current

    def _load_existing_offers_for_save(
        self,
        market_name: str,
        offer_ids: Sequence[str],
        logical_keys: Sequence[Tuple],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple, Dict[str, Any]], Dict[Tuple, set]]:
        existing_by_id: Dict[str, Dict[str, Any]] = {}
        existing_by_key: Dict[Tuple, Dict[str, Any]] = {}
        existing_ids_by_key: Dict[Tuple, set] = {}

        if not offer_ids and not logical_keys:
            return existing_by_id, existing_by_key, existing_ids_by_key

        markets = sorted({key[0] for key in logical_keys})
        barcodes = sorted({key[1] for key in logical_keys if len(key) > 1})

        rows: List[Tuple[Any, ...]] = []
        conn = self._get_pg_for_market(market_name)
        cursor = conn.cursor()

        query_parts: List[str] = []
        params: List[Any] = []
        if offer_ids:
            query_parts.append("id = ANY(%s)")
            params.append(list(offer_ids))
        if markets and barcodes:
            query_parts.append("(market_name = ANY(%s) AND barcode = ANY(%s))")
            params.extend([markets, barcodes])

        cursor.execute(
            f"""
            SELECT id, market_name, barcode, promo_price, last_updated, store_id
            FROM offers
            WHERE {' OR '.join(query_parts)}
            """,
            params,
        )
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            row_id, row_market_name, barcode, promo_price, last_updated, row_store_id = row
            row_data = {
                "id": row_id,
                "market_name": row_market_name,
                "barcode": barcode,
                "promo_price": promo_price,
                "last_updated": last_updated,
                "store_id": row_store_id,
            }
            existing_by_id[row_id] = row_data

            logical_key = self.build_offer_logical_key(row_market_name, barcode, row_store_id)
            if logical_key is None:
                continue

            if logical_key not in existing_ids_by_key:
                existing_ids_by_key[logical_key] = set()
            existing_ids_by_key[logical_key].add(row_id)

            existing_by_key[logical_key] = self._select_preferred_existing_offer(
                existing_by_key.get(logical_key),
                row_data,
            )

        return existing_by_id, existing_by_key, existing_ids_by_key

    def _dedupe_postgres_offers_by_market_barcode(self, cursor) -> None:
        # Keep the newest row for each logical offer key before creating a unique index.
        # Step 1: remap price_history references from duplicates to survivors.
        cursor.execute(
            """
            WITH ranked AS (
                SELECT
                    ctid,
                    id,
                    market_name,
                    barcode,
                    ROW_NUMBER() OVER (
                        PARTITION BY market_name, barcode
                        ORDER BY last_updated DESC NULLS LAST, id ASC
                    ) AS rank_num,
                    FIRST_VALUE(id) OVER (
                        PARTITION BY market_name, barcode
                        ORDER BY last_updated DESC NULLS LAST, id ASC
                    ) AS survivor_id
                FROM offers
                WHERE barcode IS NOT NULL
                  AND TRIM(barcode) <> ''
            ),
            duplicate_map AS (
                SELECT id AS duplicate_id, survivor_id
                FROM ranked
                WHERE rank_num > 1
            )
            UPDATE price_history AS ph
            SET offer_id = dm.survivor_id
            FROM duplicate_map AS dm
            WHERE ph.offer_id = dm.duplicate_id
            """
        )
        # Step 2: delete the duplicate offer rows now that history is remapped.
        cursor.execute(
            """
            DELETE FROM offers
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY market_name, barcode
                            ORDER BY last_updated DESC NULLS LAST, id ASC
                        ) AS rank_num
                    FROM offers
                    WHERE barcode IS NOT NULL
                      AND TRIM(barcode) <> ''
                ) ranked
                WHERE rank_num > 1
            )
            """
        )

    def _cleanup_invalid_barcodes_market_postgres(self, cursor) -> None:
        allowed_lengths = sorted(self._get_allowed_gtin_lengths())
        if not allowed_lengths:
            return

        placeholders = ", ".join(["%s"] * len(allowed_lengths))
        invalid_predicate = (
            "barcode IS NOT NULL "
            "AND TRIM(barcode) <> '' "
            "AND (TRIM(barcode) !~ '^[0-9]+$' "
            f"OR LENGTH(TRIM(barcode)) NOT IN ({placeholders}))"
        )

        # For offers, keep the row and clear invalid barcode values.
        cursor.execute(
            f"""
            UPDATE offers
            SET barcode = NULL
            WHERE {invalid_predicate}
            """,
            allowed_lengths,
        )

    def _cleanup_invalid_barcodes_common_postgres(self, cursor) -> None:
        allowed_lengths = sorted(self._get_allowed_gtin_lengths())
        if not allowed_lengths:
            return

        placeholders = ", ".join(["%s"] * len(allowed_lengths))
        invalid_predicate = (
            "barcode IS NOT NULL "
            "AND TRIM(barcode) <> '' "
            "AND (TRIM(barcode) !~ '^[0-9]+$' "
            f"OR LENGTH(TRIM(barcode)) NOT IN ({placeholders}))"
        )

        for table_name in ("barcode_reference_market_map",):
            cursor.execute(
                f"""
                DELETE FROM {table_name}
                WHERE {invalid_predicate}
                """,
                allowed_lengths,
            )

    def _create_indexes_market_postgres(self, cursor):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_offers_market_store ON offers (market_name, store_id)"
        )
        # Migrate old single-store index if it exists, then create the store-aware one.
        # The old index (market_name, barcode) is replaced by (market_name, barcode, store_id)
        # so that the same barcode may appear in multiple stores of the same market chain.
        cursor.execute("DROP INDEX IF EXISTS uq_offers_market_barcode")
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_market_barcode_store
            ON offers (market_name, barcode, store_id)
            WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
              AND store_id IS NOT NULL AND TRIM(store_id) <> ''
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_offers_barcode ON offers (barcode)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_offers_gtin ON offers (gtin)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_offers_last_updated ON offers (last_updated)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_offer_id ON price_history (offer_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_offer_store ON price_history (offer_id, store_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_recorded_at ON price_history (recorded_at)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_store_pricing_insights_store ON store_pricing_insights (market_name, store_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_price_patterns_store ON product_price_patterns (market_name, store_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_price_patterns_prediction ON product_price_patterns (predicted_next_toggle_at)"
        )

    def _migrate_barcode_inference_state(self, cursor):
        """Add inferred_barcode column to existing barcode_inference_state table (idempotent)."""
        cursor.execute(
            "ALTER TABLE barcode_inference_state ADD COLUMN IF NOT EXISTS inferred_barcode TEXT"
        )

    def _ensure_barcode_fingerprint_cache(self, cursor):
        """
        Create the product fingerprint cache table.

        fingerprint: sha256[:16] of (normalized_brand|normalized_name|measure_token) —
                     market-agnostic so the same physical product maps to one entry
                     regardless of which market or offer_id surfaced it first.
        """
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_fingerprint_cache (
                fingerprint     TEXT PRIMARY KEY,
                inferred_barcode TEXT NOT NULL,
                confidence      REAL NOT NULL,
                method          TEXT NOT NULL,
                source_market   TEXT,
                source_offer_id TEXT,
                confirmed_at    TIMESTAMP NOT NULL,
                hit_count       INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_barcode_fingerprint_cache_barcode "
            "ON barcode_fingerprint_cache (inferred_barcode)"
        )

    def _create_indexes_common_postgres(self, cursor):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_catalog_brand_measure ON product_catalog (normalized_brand, measure_token)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_barcode_inference_state_last_attempted ON barcode_inference_state (last_attempted_at)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_process_timing_process ON process_timing (process_name, started_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_process_timing_market ON process_timing (market_name, store_id)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_barcode_reference_market_map_market ON barcode_reference_market_map (market_name, barcode)"
        )

    def _enable_rls_postgres(self, cursor, scope: str = "manager") -> None:
        """Enable Row Level Security on every table in the public schema that lacks it.

        Blocks Supabase PostgREST (anon/authenticated roles) from reading any
        table directly. Direct psycopg connections using the postgres superuser
        are unaffected — RLS does not apply to table owners or superusers.

        Uses pg_tables so newly created tables are covered automatically.
        """
        cursor.execute(
            """
            DO $$
            DECLARE t text;
            BEGIN
                FOR t IN
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND NOT rowsecurity
                LOOP
                    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
                END LOOP;
            END $$;
            """
        )

    @staticmethod
    def _parse_datetime(raw_value: Optional[Any]) -> Optional[datetime]:
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        if not text:
            return None

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed

    def get_market_last_updated(
        self,
        market_name: str,
        store_id: Optional[str] = None,
    ) -> Optional[datetime]:
        canonical_market = self._canonical_market_name(market_name)
        market_aliases = self._market_name_aliases(market_name)
        if not market_aliases:
            return None

        row = None
        if self.use_postgres:
            conn = self._get_pg_for_market(canonical_market)
            cursor = conn.cursor()
            market_clause = "market_name = ANY(%s)"
            if store_id:
                cursor.execute(
                    f"SELECT MAX(last_updated) FROM offers WHERE {market_clause} AND store_id = %s",
                    (market_aliases, store_id),
                )
            else:
                cursor.execute(
                    f"SELECT MAX(last_updated) FROM offers WHERE {market_clause}",
                    (market_aliases,),
                )
            row = cursor.fetchone()
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(market_aliases))
            if store_id:
                cursor.execute(
                    f"SELECT MAX(last_updated) FROM offers WHERE market_name IN ({placeholders}) AND store_id = ?",
                    tuple(market_aliases) + (store_id,),
                )
            else:
                cursor.execute(
                    f"SELECT MAX(last_updated) FROM offers WHERE market_name IN ({placeholders})",
                    tuple(market_aliases),
                )
            row = cursor.fetchone()
            conn.close()

        if not row:
            return None
        return self._parse_datetime(row[0])

    def get_market_last_run_succeeded(
        self,
        market_name: str,
        store_id: Optional[str] = None,
    ) -> bool:
        """Return True only if the most recent process_timing row for this market
        (optionally filtered by store_id) has status='success'.
        A missing row (never run) is treated as not-succeeded so the first real
        run is never skipped.
        Filtering by store_id means two different ZIPs that resolve to the same
        physical store share the same succeeded/failed status."""
        market_aliases = self._market_name_aliases(market_name)
        if not market_aliases:
            return False

        try:
            if self.use_postgres:
                conn = self._get_pg_for_common_table("process_timing")
                cursor = conn.cursor()
                if store_id:
                    cursor.execute(
                        "SELECT status FROM process_timing "
                        "WHERE market_name = ANY(%s) AND store_id = %s "
                        "ORDER BY started_at DESC LIMIT 1",
                        (market_aliases, store_id),
                    )
                else:
                    cursor.execute(
                        "SELECT status FROM process_timing "
                        "WHERE market_name = ANY(%s) "
                        "ORDER BY started_at DESC LIMIT 1",
                        (market_aliases,),
                    )
                row = cursor.fetchone()
                conn.close()
            else:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                placeholders = ",".join(["?"] * len(market_aliases))
                if store_id:
                    cursor.execute(
                        f"SELECT status FROM process_timing "
                        f"WHERE market_name IN ({placeholders}) AND store_id = ? "
                        f"ORDER BY started_at DESC LIMIT 1",
                        tuple(market_aliases) + (store_id,),
                    )
                else:
                    cursor.execute(
                        f"SELECT status FROM process_timing "
                        f"WHERE market_name IN ({placeholders}) "
                        f"ORDER BY started_at DESC LIMIT 1",
                        tuple(market_aliases),
                    )
                row = cursor.fetchone()
                conn.close()

            if not row:
                return False
            return str(row[0]).lower() == "success"
        except Exception:
            # If we can't query timing (e.g. table doesn't exist yet), don't block
            return True

    def log_process_timing(
        self,
        process_name: str,
        step_name: str,
        status: str,
        duration_seconds: float,
        started_at: datetime,
        finished_at: datetime,
        market_name: Optional[str] = None,
        zip_code: Optional[str] = None,
        store_id: Optional[str] = None,
        run_type: str = "individual",
        details: Optional[str] = None,
    ):
        if self.use_postgres:
            conn = self._get_pg_for_common_table("process_timing")
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO process_timing (
                    process_name, step_name, market_name, zip_code, store_id,
                    status, duration_seconds, started_at, finished_at,
                    run_type, details
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    process_name,
                    step_name,
                    market_name,
                    zip_code,
                    store_id,
                    status,
                    float(duration_seconds),
                    started_at.isoformat(),
                    finished_at.isoformat(),
                    run_type,
                    details,
                ),
            )
            conn.commit()
            conn.close()
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO process_timing (
                process_name, step_name, market_name, zip_code, store_id,
                status, duration_seconds, started_at, finished_at,
                run_type, details
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                process_name,
                step_name,
                market_name,
                zip_code,
                store_id,
                status,
                float(duration_seconds),
                started_at.isoformat(),
                finished_at.isoformat(),
                run_type,
                details,
            ),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _prices_differ(a: Optional[float], b: Optional[float]) -> bool:
        """Return True when two promo prices are meaningfully different."""
        if a is None and b is None:
            return False
        if a is None or b is None:
            return True
        try:
            return abs(float(a) - float(b)) > 0.001
        except (TypeError, ValueError):
            return str(a) != str(b)

    def _save_offers_for_market(self, market_name: str, offers: List[tuple]) -> Tuple[int, int, int]:
        if not offers:
            return 0, 0, 0

        expected_offer_columns = 22

        offer_ids = [offer[0] for offer in offers if offer and offer[0]]
        logical_keys = [
            logical_key
            for logical_key in (
                self.build_offer_logical_key(
                    offer[1] if len(offer) > 1 else None,
                    offer[10] if len(offer) > 10 else None,
                    offer[17] if len(offer) > 17 else None,
                )
                for offer in offers
            )
            if logical_key is not None
        ]
        existing_data, existing_by_key, existing_ids_by_key = self._load_existing_offers_for_save(
            market_name,
            offer_ids,
            logical_keys,
        )

        now_iso = datetime.now().isoformat()
        sanitized_offers: List[tuple] = []
        history_records: List[tuple] = []
        duplicate_offer_ids = set()
        duplicate_offer_id_map: Dict[str, str] = {}

        for offer in offers:
            offer_as_list = list(offer)
            # Backward compatibility: some producers still send legacy tuples with
            # a deprecated zip_code field between store_id and sold_quantity.
            # Normalize every tuple to the current 22-column offers schema.
            if len(offer_as_list) == expected_offer_columns + 1:
                del offer_as_list[18]
            if len(offer_as_list) < expected_offer_columns:
                offer_as_list.extend([None] * (expected_offer_columns - len(offer_as_list)))
            elif len(offer_as_list) > expected_offer_columns:
                offer_as_list = offer_as_list[:expected_offer_columns]

            if len(offer_as_list) > 1:
                offer_as_list[1] = self._canonical_market_name(offer_as_list[1])
            if len(offer_as_list) > 10:
                offer_as_list[10] = self.normalize_barcode(offer_as_list[10])
            logical_key = self.build_offer_logical_key(
                offer_as_list[1] if len(offer_as_list) > 1 else None,
                offer_as_list[10] if len(offer_as_list) > 10 else None,
                offer_as_list[17] if len(offer_as_list) > 17 else None,
            )

            existing = existing_data.get(offer_as_list[0])
            if existing is None and logical_key is not None:
                existing = existing_by_key.get(logical_key)

            if len(offer_as_list) > 10 and offer_as_list[10] is None and existing and existing.get("barcode"):
                offer_as_list[10] = existing["barcode"]

            if logical_key is not None:
                for existing_id in existing_ids_by_key.get(logical_key, set()):
                    if existing_id == offer_as_list[0]:
                        continue
                    duplicate_offer_ids.add(existing_id)
                    duplicate_offer_id_map[existing_id] = offer_as_list[0]

            sanitized_offers.append(tuple(offer_as_list))

            new_promo = offer_as_list[6] if len(offer_as_list) > 6 else None
            old_promo = existing["promo_price"] if existing else None
            if existing is None or self._prices_differ(old_promo, new_promo):
                history_records.append((
                    offer_as_list[0],
                    offer_as_list[17] if len(offer_as_list) > 17 else None,
                    offer_as_list[1],
                    offer_as_list[2],
                    offer_as_list[5],
                    new_promo,
                    offer_as_list[19] if len(offer_as_list) > 19 else None,
                    offer_as_list[20] if len(offer_as_list) > 20 else None,
                    offer_as_list[21] if len(offer_as_list) > 21 else None,
                    now_iso,
                ))

        # Deduplicate within-batch by (market_name, barcode, store_id): two offers from
        # different categories may share the same (market, barcode, store) triple.
        # ON CONFLICT only resolves conflicts with existing DB rows, not within-batch
        # duplicates, so sending both would raise UniqueViolation.
        # Keep the offer with the most populated fields; fall back to first seen.
        _seen_barcode_keys: Dict[tuple, int] = {}  # key -> index in _deduped
        _deduped: List[tuple] = []
        for _offer in sanitized_offers:
            _barcode = _offer[10] if len(_offer) > 10 else None
            if _barcode and str(_barcode).strip():
                _key = (
                    str(_offer[1] or "").strip().lower(),
                    str(_barcode).strip(),
                    str(_offer[17] or "").strip() if len(_offer) > 17 else "",
                )
                if _key in _seen_barcode_keys:
                    _existing = _deduped[_seen_barcode_keys[_key]]
                    _existing_score = sum(1 for v in _existing if v is not None and str(v).strip())
                    _new_score = sum(1 for v in _offer if v is not None and str(v).strip())
                    if _new_score > _existing_score:
                        _deduped[_seen_barcode_keys[_key]] = _offer
                else:
                    _seen_barcode_keys[_key] = len(_deduped)
                    _deduped.append(_offer)
            else:
                _deduped.append(_offer)
        sanitized_offers = _deduped

        conn = self._get_pg_for_market(market_name)
        cursor = conn.cursor()

        if duplicate_offer_id_map:
            cursor.executemany(
                "UPDATE price_history SET offer_id = %s WHERE offer_id = %s",
                [(new_id, old_id) for old_id, new_id in duplicate_offer_id_map.items()],
            )
        if duplicate_offer_ids:
            cursor.execute(
                "DELETE FROM offers WHERE id = ANY(%s)",
                (list(duplicate_offer_ids),),
            )

        offers_with_barcode = [
            offer
            for offer in sanitized_offers
            if (
                len(offer) > 10
                and offer[10]
                and str(offer[10]).strip()
                and offer[0] not in existing_data
            )
        ]
        offers_without_barcode = [
            offer
            for offer in sanitized_offers
            if not (
                len(offer) > 10
                and offer[10]
                and str(offer[10]).strip()
                and offer[0] not in existing_data
            )
        ]

        if offers_with_barcode:
            cursor.executemany(
                """
                INSERT INTO offers (
                    id, market_name, product_name, brand, description,
                    regular_price, promo_price, promo_min_quantity, unit,
                    gtin, barcode, product_url, image_url, stock_balance,
                    stock_general, promo_end_at, last_updated, store_id,
                    sold_quantity, offer_name, offer_tag, app_membership_required
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (market_name, barcode, store_id)
                WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
                  AND store_id IS NOT NULL AND TRIM(store_id) <> ''
                DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    brand = EXCLUDED.brand,
                    description = EXCLUDED.description,
                    regular_price = EXCLUDED.regular_price,
                    promo_price = EXCLUDED.promo_price,
                    promo_min_quantity = EXCLUDED.promo_min_quantity,
                    unit = EXCLUDED.unit,
                    gtin = EXCLUDED.gtin,
                    product_url = EXCLUDED.product_url,
                    image_url = EXCLUDED.image_url,
                    stock_balance = EXCLUDED.stock_balance,
                    stock_general = EXCLUDED.stock_general,
                    promo_end_at = EXCLUDED.promo_end_at,
                    last_updated = EXCLUDED.last_updated,
                    store_id = EXCLUDED.store_id,
                    sold_quantity = EXCLUDED.sold_quantity,
                    offer_name = EXCLUDED.offer_name,
                    offer_tag = EXCLUDED.offer_tag,
                    app_membership_required = EXCLUDED.app_membership_required
                """,
                offers_with_barcode,
            )

        if offers_without_barcode:
            cursor.executemany(
                """
                INSERT INTO offers (
                    id, market_name, product_name, brand, description,
                    regular_price, promo_price, promo_min_quantity, unit,
                    gtin, barcode, product_url, image_url, stock_balance,
                    stock_general, promo_end_at, last_updated, store_id,
                    sold_quantity, offer_name, offer_tag, app_membership_required
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    market_name = EXCLUDED.market_name,
                    product_name = EXCLUDED.product_name,
                    brand = COALESCE(NULLIF(TRIM(EXCLUDED.brand), ''), offers.brand),
                    description = COALESCE(NULLIF(TRIM(EXCLUDED.description::text), ''), offers.description),
                    regular_price = EXCLUDED.regular_price,
                    promo_price = EXCLUDED.promo_price,
                    promo_min_quantity = EXCLUDED.promo_min_quantity,
                    unit = COALESCE(NULLIF(TRIM(EXCLUDED.unit), ''), offers.unit),
                    gtin = COALESCE(NULLIF(TRIM(EXCLUDED.gtin), ''), offers.gtin),
                    barcode = COALESCE(NULLIF(TRIM(EXCLUDED.barcode), ''), offers.barcode),
                    product_url = COALESCE(NULLIF(TRIM(EXCLUDED.product_url), ''), offers.product_url),
                    image_url = COALESCE(NULLIF(TRIM(EXCLUDED.image_url), ''), offers.image_url),
                    stock_balance = EXCLUDED.stock_balance,
                    stock_general = EXCLUDED.stock_general,
                    promo_end_at = EXCLUDED.promo_end_at,
                    last_updated = EXCLUDED.last_updated,
                    store_id = EXCLUDED.store_id,
                    sold_quantity = EXCLUDED.sold_quantity,
                    offer_name = EXCLUDED.offer_name,
                    offer_tag = EXCLUDED.offer_tag,
                    app_membership_required = EXCLUDED.app_membership_required
                """,
                offers_without_barcode,
            )

        if history_records:
            cursor.executemany(
                """
                INSERT INTO price_history
                    (offer_id, store_id, market_name, product_name, regular_price, promo_price,
                     offer_name, offer_tag, app_membership_required, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                history_records,
            )

        affected_store_ids = sorted(
            {
                str(offer[17]).strip()
                for offer in sanitized_offers
                if len(offer) > 17 and str(offer[17] or "").strip()
            }
        )
        if affected_store_ids:
            analysis_stats = HistoricalPricingAnalyzer(conn, market_name).refresh(affected_store_ids)
            if analysis_stats["stores"] or analysis_stats["products"]:
                print(
                    f"  Historical pricing analysis refreshed for {market_name}: "
                    f"{analysis_stats['stores']} stores, {analysis_stats['products']} products"
                )

        conn.commit()
        conn.close()
        return len(sanitized_offers), len(history_records), len(duplicate_offer_ids)

    def save_offers(self, offers: List[tuple]):
        if not offers:
            return

        grouped_offers: Dict[str, List[tuple]] = {}
        for offer in offers:
            if not offer or len(offer) < 2:
                continue
            market_name = self._canonical_market_name(offer[1])
            if not market_name:
                continue
            grouped_offers.setdefault(market_name, []).append(offer)

        saved_count = 0
        history_count = 0
        duplicate_count = 0
        for market_name, market_offers in grouped_offers.items():
            saved, history, duplicates = self._save_offers_for_market(market_name, market_offers)
            saved_count += saved
            history_count += history
            duplicate_count += duplicates

        print(
            f"Saved {saved_count} offers to the database "
            f"({history_count} price history records written, "
            f"{duplicate_count} duplicate offer rows merged)."
        )

    def get_store_id(self, zip_code: str, market_name: str) -> Optional[str]:
        # zip_code is kept for API compatibility but is not stored in the table.
        # Returns any cached store_id for this market (used as a skip-check fallback).
        # The real store is always resolved at run-time via the scraper's resolve_store().
        market_aliases = self._market_name_aliases(market_name)
        if not market_aliases:
            return None

        if self.use_postgres:
            conn = self._get_pg_for_common_table("store_mappings")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT store_id FROM store_mappings WHERE market_name = ANY(%s) LIMIT 1",
                (market_aliases,),
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(market_aliases))
        cursor.execute(
            f"SELECT store_id FROM store_mappings WHERE market_name IN ({placeholders}) LIMIT 1",
            tuple(market_aliases),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def cache_store_id(
        self,
        zip_code: str,
        market_name: str,
        store_id: str,
        store_name: Optional[str] = None,
        store_address: Optional[str] = None,
        store_city: Optional[str] = None,
        store_state: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        store_payload: Optional[Any] = None,
    ):
        market_name = self._canonical_market_name(market_name)
        serialized_payload: Optional[str]
        if store_payload is None:
            serialized_payload = None
        elif isinstance(store_payload, str):
            serialized_payload = store_payload
        else:
            serialized_payload = json.dumps(store_payload, ensure_ascii=False)

        if self.use_postgres:
            conn = self._get_pg_for_common_table("store_mappings")
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO store_mappings (
                    market_name, store_id, store_name,
                    store_address, store_city, store_state,
                    latitude, longitude, store_payload, last_successful_update
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_name, store_id) DO UPDATE
                SET
                    store_name = COALESCE(EXCLUDED.store_name, store_mappings.store_name),
                    store_address = COALESCE(EXCLUDED.store_address, store_mappings.store_address),
                    store_city = COALESCE(EXCLUDED.store_city, store_mappings.store_city),
                    store_state = COALESCE(EXCLUDED.store_state, store_mappings.store_state),
                    latitude = COALESCE(EXCLUDED.latitude, store_mappings.latitude),
                    longitude = COALESCE(EXCLUDED.longitude, store_mappings.longitude),
                    store_payload = COALESCE(EXCLUDED.store_payload, store_mappings.store_payload),
                    last_successful_update = EXCLUDED.last_successful_update
                """,
                (
                    market_name,
                    store_id,
                    store_name,
                    store_address,
                    store_city,
                    store_state,
                    latitude,
                    longitude,
                    serialized_payload,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            conn.close()
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO store_mappings (
                market_name, store_id, store_name,
                store_address, store_city, store_state,
                latitude, longitude, store_payload, last_successful_update
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_name, store_id) DO UPDATE SET
                store_name = COALESCE(excluded.store_name, store_mappings.store_name),
                store_address = COALESCE(excluded.store_address, store_mappings.store_address),
                store_city = COALESCE(excluded.store_city, store_mappings.store_city),
                store_state = COALESCE(excluded.store_state, store_mappings.store_state),
                latitude = COALESCE(excluded.latitude, store_mappings.latitude),
                longitude = COALESCE(excluded.longitude, store_mappings.longitude),
                store_payload = COALESCE(excluded.store_payload, store_mappings.store_payload),
                last_successful_update = excluded.last_successful_update
            """,
            (
                market_name,
                store_id,
                store_name,
                store_address,
                store_city,
                store_state,
                latitude,
                longitude,
                serialized_payload,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    def _upsert_barcode_reference_market_rows(
        self,
        rows: Sequence[Tuple[str, str, str, str, str, str]],
    ) -> None:
        if not rows:
            return

        if self.use_postgres:
            conn = self._get_pg_for_common_table("barcode_reference_market_map")
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO barcode_reference_market_map
                    (barcode, market_name, market_offer_id, product_name, brand, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (barcode, market_name) DO UPDATE SET
                    market_offer_id = EXCLUDED.market_offer_id,
                    product_name = EXCLUDED.product_name,
                    brand = EXCLUDED.brand,
                    last_updated = EXCLUDED.last_updated
                """,
                rows,
            )
            conn.commit()
            conn.close()
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO barcode_reference_market_map
                (barcode, market_name, market_offer_id, product_name, brand, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()

    def _migrate_legacy_barcode_references_to_market_map_postgres(self, cursor) -> None:
        # Guard: barcode_references was dropped as part of the consolidation migration.
        # If it no longer exists, this migration is already done — skip silently.
        cursor.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'barcode_references'
            """
        )
        if not cursor.fetchone():
            return  # table already dropped — nothing to migrate

        cursor.execute(
            """
            SELECT barcode, rossi_id, atacadao_id, nagumo_id, higas_id, swift_id, sonda_id, xsupermercados_id, barbosa_id, oba_id, extra_id, paodeacucar_id, product_name, brand, last_updated
            FROM barcode_references
            WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
            """
        )
        rows = cursor.fetchall()
        upsert_rows: List[Tuple[str, str, str, str, str, str]] = []
        for row in rows:
            (
                barcode,
                rossi_id,
                atacadao_id,
                nagumo_id,
                higas_id,
                swift_id,
                sonda_id,
                xsupermercados_id,
                barbosa_id,
                oba_id,
                extra_id,
                paodeacucar_id,
                product_name,
                brand,
                last_updated,
            ) = row
            normalized_barcode = self.normalize_barcode(barcode)
            if not normalized_barcode:
                continue

            market_ids = {
                "Rossi": rossi_id,
                "Atacadão": atacadao_id,
                "Nagumo": nagumo_id,
                "Higas": higas_id,
                "Swift": swift_id,
                "Sonda Delivery": sonda_id,
                "XSupermercados": xsupermercados_id,
                "Barbosa": barbosa_id,
                "Oba Hortifruti": oba_id,
                "Extra": extra_id,
                "Pão de Açúcar": paodeacucar_id,
            }
            for market_name, market_offer_id in market_ids.items():
                market_offer_text = str(market_offer_id or "").strip()
                if not market_offer_text:
                    continue
                upsert_rows.append(
                    (
                        normalized_barcode,
                        self._canonical_market_name(market_name),
                        market_offer_text,
                        str(product_name or ""),
                        str(brand or ""),
                        str(last_updated or datetime.now().isoformat()),
                    )
                )

        if upsert_rows:
            cursor.executemany(
                """
                INSERT INTO barcode_reference_market_map
                    (barcode, market_name, market_offer_id, product_name, brand, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (barcode, market_name) DO UPDATE SET
                    market_offer_id = EXCLUDED.market_offer_id,
                    product_name = EXCLUDED.product_name,
                    brand = EXCLUDED.brand,
                    last_updated = EXCLUDED.last_updated
                """,
                upsert_rows,
            )

    def save_barcode_reference(
        self,
        barcode: str,
        market_id: str,
        product_id: str,
        product_name: str,
        brand: str,
    ):
        self.save_barcode_references_bulk(
            [(barcode, market_id, product_id, product_name, brand)]
        )

    def save_barcode_references_bulk(
        self,
        references: Sequence[Tuple[str, str, str, str, str]],
    ):
        """Save barcode cross-references.

        barcode_references (legacy pivot table) has been dropped.
        All data now goes exclusively to barcode_reference_market_map
        (normalized long-format table that scales with any number of markets).

        Input: list of (barcode, market_name, offer_id, product_name, brand)
        """
        if not references:
            return

        now_iso = datetime.now().isoformat()
        normalized_rows: List[Tuple[str, str, str, str, str, str]] = []

        for barcode, market_id, product_id, product_name, brand in references:
            normalized_barcode = self.normalize_barcode(barcode)
            canonical_market = self._canonical_market_name(market_id)
            if not normalized_barcode or not canonical_market or not product_id:
                continue
            normalized_rows.append((
                normalized_barcode,
                canonical_market,
                str(product_id),
                str(product_name or ""),
                str(brand or ""),
                now_iso,
            ))

        if normalized_rows:
            self._upsert_barcode_reference_market_rows(normalized_rows)

    def fetch_offers_with_barcodes(self, markets: Sequence[str]) -> List[Tuple[Any, ...]]:
        if not markets:
            return []

        if self.use_postgres:
            rows: List[Tuple[Any, ...]] = []
            grouped_markets = self._group_markets_by_database(markets)
            for db_url, db_markets in grouped_markets.items():
                conn = self._connect_pg(db_url)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_name = 'offers'
                    )
                    """
                )
                has_offers_table = bool(cursor.fetchone()[0])
                if not has_offers_table:
                    conn.close()
                    continue
                cursor.execute(
                    """
                    SELECT market_name, id, barcode, product_name, brand, description, image_url
                    FROM offers
                    WHERE barcode IS NOT NULL
                      AND barcode <> ''
                      AND market_name = ANY(%s)
                    """,
                    (db_markets,),
                )
                rows.extend(cursor.fetchall())
                conn.close()
            return rows

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(markets))
        cursor.execute(
            f"""
            SELECT market_name, id, barcode, product_name, brand, description, image_url
            FROM offers
            WHERE barcode IS NOT NULL
              AND TRIM(barcode) <> ''
              AND market_name IN ({placeholders})
            """,
            list(markets),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def fetch_registered_barcodes_for_market(self, market_name: str) -> List[str]:
        canonical_market = self._canonical_market_name(market_name)
        if not canonical_market:
            return []

        if self.use_postgres:
            conn = self._get_pg_for_common_table("barcode_reference_market_map")
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT barcode
                FROM barcode_reference_market_map
                WHERE market_name = %s
                  AND barcode IS NOT NULL
                  AND TRIM(barcode) <> ''
                """,
                (canonical_market,),
            )
            rows = cursor.fetchall()
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT barcode
                FROM barcode_reference_market_map
                WHERE market_name = ?
                  AND barcode IS NOT NULL
                  AND TRIM(barcode) <> ''
                """,
                (canonical_market,),
            )
            rows = cursor.fetchall()
            conn.close()

        seen = set()
        normalized_rows: List[str] = []
        for row in rows:
            normalized = self.normalize_barcode(row[0])
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_rows.append(normalized)
        return normalized_rows

    def fetch_offers_missing_barcode(
        self,
        markets: Sequence[str],
        limit: Optional[int] = None,
        offer_ids: Optional[Sequence[str]] = None,
    ) -> List[Tuple[Any, ...]]:
        if not markets:
            return []

        filtered_offer_ids = [offer_id for offer_id in (offer_ids or []) if offer_id]

        if self.use_postgres:
            rows: List[Tuple[Any, ...]] = []
            grouped_markets = self._group_markets_by_database(markets)
            for db_url, db_markets in grouped_markets.items():
                conn = self._connect_pg(db_url)
                cursor = conn.cursor()
                query = """
                    SELECT id, market_name, product_name, brand, description, unit, product_url, image_url
                    FROM offers
                    WHERE barcode IS NULL
                      AND market_name = ANY(%s)
                """
                params: List[Any] = [db_markets]
                if filtered_offer_ids:
                    query += " AND id = ANY(%s)"
                    params.append(filtered_offer_ids)
                query += " ORDER BY market_name, product_name"
                cursor.execute(query, params)
                rows.extend(cursor.fetchall())
                conn.close()

            rows = sorted(rows, key=lambda row: (str(row[1] or ""), str(row[2] or "")))
            return rows[:limit] if limit is not None else rows

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(markets))
        query = f"""
            SELECT id, market_name, product_name, brand, description, unit, product_url, image_url
            FROM offers
            WHERE barcode IS NULL
              AND market_name IN ({placeholders})
        """
        params: List[Any] = list(markets)
        if filtered_offer_ids:
            offer_placeholders = ",".join(["?"] * len(filtered_offer_ids))
            query += f" AND id IN ({offer_placeholders})"
            params.extend(filtered_offer_ids)
        query += " ORDER BY market_name, product_name"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def update_offer_barcode_if_null(self, offer_id: str, barcode: str) -> bool:
        normalized_barcode = self.normalize_barcode(barcode)
        if not normalized_barcode:
            return False

        prefix = str(offer_id).split("_", 1)[0]

        if self.use_postgres:
            try:
                import psycopg  # type: ignore
                transient_errors = (psycopg.OperationalError, psycopg.InterfaceError)
            except Exception:
                transient_errors = (Exception,)

            market_name = self._market_from_offer_id(offer_id)
            candidate_urls = [self._get_market_database_url(market_name)] if market_name else self._iter_market_database_urls()
            for db_url in candidate_urls:
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    conn = None
                    try:
                        conn = self._connect_pg(db_url)
                        cursor = conn.cursor()
                        # Skip if another offer in the same (market, store) already owns this barcode
                        cursor.execute(
                            """
                            UPDATE offers AS target
                            SET barcode = %s
                            WHERE target.id = %s
                              AND target.barcode IS NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM offers AS existing
                                  WHERE existing.market_name = target.market_name
                                    AND existing.store_id    = target.store_id
                                    AND existing.barcode     = %s
                                    AND existing.id <> target.id
                              )
                            """,
                            (normalized_barcode, offer_id, normalized_barcode),
                        )
                        updated = cursor.rowcount > 0
                        if updated:
                            # Build new ID: {prefix}_{barcode}_{store_hash8}
                            cursor.execute("SELECT store_id FROM offers WHERE id = %s", (offer_id,))
                            row = cursor.fetchone()
                            store_id = str(row[0] or "").strip() if row else ""
                            new_id = (
                                f"{prefix}_{normalized_barcode}_{self._store_id_hash(store_id)}"
                                if store_id
                                else f"{prefix}_{normalized_barcode}"
                            )
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
                        if updated:
                            return True
                        break
                    except transient_errors as exc:
                        if conn is not None:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                        if attempt >= max_attempts:
                            print(
                                "Warning: update_offer_barcode_if_null failed after retries "
                                f"for offer_id={offer_id}: {type(exc).__name__}: {exc}"
                            )
                            break
                        backoff_seconds = min(2.0, 0.25 * attempt)
                        time.sleep(backoff_seconds)
                    finally:
                        if conn is not None:
                            try:
                                conn.close()
                            except Exception:
                                pass
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE offers SET barcode = ? WHERE id = ? AND barcode IS NULL",
            (normalized_barcode, offer_id),
        )
        updated = cursor.rowcount > 0
        if updated:
            cursor.execute("SELECT store_id FROM offers WHERE id = ?", (offer_id,))
            row = cursor.fetchone()
            store_id = str(row[0] or "").strip() if row else ""
            new_id = (
                f"{prefix}_{normalized_barcode}_{self._store_id_hash(store_id)}"
                if store_id
                else f"{prefix}_{normalized_barcode}"
            )
            if new_id != offer_id:
                cursor.execute(
                    "UPDATE offers SET id = ? WHERE id = ? AND NOT EXISTS (SELECT 1 FROM offers WHERE id = ?)",
                    (new_id, offer_id, new_id),
                )
        conn.commit()
        conn.close()
        return updated

    # ------------------------------------------------------------------
    # Table discovery
    # ------------------------------------------------------------------

    def _list_tables_in_db(self, database_url: str) -> List[str]:
        """Return all user table names in the given Postgres database."""
        conn = self._connect_pg(database_url)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
            """
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables

    def discover_all_tables(self) -> Dict[str, str]:
        """
        Discover every table that exists across all connected databases.

        Returns a dict: { table_name -> "manager" | "market" | "both" }

        "manager" = lives only in the manager DB (common/shared tables)
        "market"  = lives in one or more market DBs (offers, price_history, etc.)
        "both"    = same name found in both (rare, but possible)
        """
        result: Dict[str, str] = {}

        # Manager DB tables (primary + all additional common DB URLs)
        try:
            manager_tables = set(self._list_tables_in_db(self.database_url_manager))
            for t in manager_tables:
                result[t] = "manager"
        except Exception as exc:
            print(f"Warning: could not list manager DB tables: {exc}")
            manager_tables = set()

        # Additional common DB URLs (e.g. separate Supabase projects for TIMING/INFERENCE/AUDIT)
        for common_url in self._iter_common_database_urls():
            if common_url == self.database_url_manager:
                continue
            try:
                for t in self._list_tables_in_db(common_url):
                    if t not in result:
                        result[t] = "manager"
            except Exception as exc:
                print(f"Warning: could not list tables from common DB {common_url[:40]}...: {exc}")

        # Market DB tables (union across all market DBs)
        for db_url in self._iter_market_database_urls():
            if db_url == self.database_url_manager:
                continue
            try:
                market_tables = set(self._list_tables_in_db(db_url))
                for t in market_tables:
                    if t in result:
                        result[t] = "both"
                    else:
                        result[t] = "market"
            except Exception as exc:
                print(f"Warning: could not list tables from {db_url[:40]}...: {exc}")

        # If using a single shared DB (no separate market DBs), everything is in manager
        if not self._iter_market_database_urls() or all(
            u == self.database_url_manager for u in self._iter_market_database_urls()
        ):
            for t in list(result.keys()):
                result[t] = "manager"

        return result

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_table_to_csv(self, table_name: str, filename: str) -> str:
        """
        Export any table to CSV. Automatically routes to the correct DB(s):
        - Tables in MARKET_TABLES (offers, price_history) are unioned
          across all market databases.
        - All other tables are read from the manager DB.

        No hardcoded allowlist — any table that exists can be exported.
        """
        import csv as _csv

        if table_name == "app_offers":
            return self._export_app_offers_to_csv(filename)

        if self.use_postgres:
            rows: List[Tuple[Any, ...]] = []
            columns: List[str] = []

            if table_name in self.MARKET_TABLES:
                # Union across all market DBs
                for db_url in self._iter_market_database_urls():
                    try:
                        conn = self._connect_pg(db_url)
                        cursor = conn.cursor()
                        cursor.execute(f'SELECT * FROM "{table_name}"')
                        fetched = cursor.fetchall()
                        if not columns and cursor.description:
                            columns = [desc[0] for desc in cursor.description]
                        rows.extend(fetched)
                        conn.close()
                    except Exception as exc:
                        print(f"  Warning: {table_name} not found in {db_url[:40]}...: {exc}")
            else:
                # Manager DB (common/shared tables)
                conn = self._get_pg_for_common_table(table_name)
                cursor = conn.cursor()
                cursor.execute(f'SELECT * FROM "{table_name}"')
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                conn.close()

        else:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(f'SELECT * FROM "{table_name}"')
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            conn.close()

        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)

        print(f"  OK {table_name}: {len(rows):,} rows -> {filename}")
        return filename

    def _export_app_offers_to_csv(self, filename: str) -> str:
        """Export app_offers enriched with historical price-pattern columns."""
        import csv as _csv

        extra_columns = [
            "observed_min_price",
            "observed_max_price",
            "pattern_type",
            "best_buy_weekday",
            "best_buy_weekday_label",
            "predicted_next_toggle_at",
        ]

        if self.use_postgres:
            conn = self._get_pg_for_common_table("app_offers")
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM "app_offers"')
            app_offer_rows = cursor.fetchall()
            app_offer_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            conn.close()

            pattern_by_offer: Dict[Tuple[str, str, str], Tuple[Any, ...]] = {}
            pattern_query = """
                SELECT
                    market_name,
                    store_id,
                    offer_id,
                    observed_min_price,
                    observed_max_price,
                    pattern_type,
                    best_buy_weekday,
                    best_buy_weekday_label,
                    predicted_next_toggle_at
                FROM product_price_patterns
            """
            for db_url in self._iter_market_database_urls():
                try:
                    pattern_conn = self._connect_pg(db_url)
                    pattern_cursor = pattern_conn.cursor()
                    pattern_cursor.execute(pattern_query)
                    for pattern_row in pattern_cursor.fetchall():
                        market_name, store_id, offer_id, *extras = pattern_row
                        key = (
                            self._canonical_market_name(market_name),
                            str(store_id or "").strip(),
                            str(offer_id or "").strip(),
                        )
                        pattern_by_offer[key] = tuple(extras)
                    pattern_conn.close()
                except Exception as exc:
                    print(f"  Warning: product_price_patterns not found in {db_url[:40]}...: {exc}")

            offer_id_idx = app_offer_columns.index("offer_id")
            market_name_idx = app_offer_columns.index("market_name")
            store_id_idx = app_offer_columns.index("store_id")
            rows = []
            for app_offer_row in app_offer_rows:
                key = (
                    self._canonical_market_name(app_offer_row[market_name_idx]),
                    str(app_offer_row[store_id_idx] or "").strip(),
                    str(app_offer_row[offer_id_idx] or "").strip(),
                )
                extras = pattern_by_offer.get(key, (None,) * len(extra_columns))
                rows.append(tuple(app_offer_row) + extras)
            columns = app_offer_columns + extra_columns
        else:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    ao.*, 
                    ppp.observed_min_price,
                    ppp.observed_max_price,
                    ppp.pattern_type,
                    ppp.best_buy_weekday,
                    ppp.best_buy_weekday_label,
                    ppp.predicted_next_toggle_at
                FROM app_offers ao
                LEFT JOIN product_price_patterns ppp
                  ON ppp.market_name = ao.market_name
                 AND ppp.store_id = ao.store_id
                 AND ppp.offer_id = ao.offer_id
                """
            )
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            conn.close()

        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)

        print(f"  OK app_offers: {len(rows):,} rows -> {filename}")
        return filename

    def export_to_csv(self, prefix: str = "export", skip_tables: Optional[List[str]] = None) -> dict:
        """
        Export ALL tables from all connected databases to CSV files.

        Fully dynamic — discovers tables at runtime by querying pg_tables,
        so adding a new table to the schema is automatically included
        without any code changes.

        Files are named:  {prefix}_{table_name}.csv

        Also produces a special cross-market price comparison file:
          {prefix}_price_comparison.csv

        Args:
            prefix:       Filename prefix (default "export")
            skip_tables:  Optional list of table names to skip (e.g. large
                          internal audit tables you don't want exported)
        """
        skip = set(skip_tables or [])

        # Always skip internal Postgres system noise if it leaks through
        skip.update({
            "pg_stat_statements",
            "spatial_ref_sys",
        })

        print(f"\nDiscovering tables...")
        table_map = self.discover_all_tables()
        all_tables = sorted(table_map.keys())
        exportable = [t for t in all_tables if t not in skip]
        print(f"Found {len(exportable)} tables to export (skipping {len(skip & set(all_tables))})")

        exports: Dict[str, str] = {}
        failed: List[str] = []

        for table in exportable:
            filename = f"{prefix}_{table}.csv"
            try:
                self.export_table_to_csv(table, filename)
                exports[table] = filename
            except Exception as exc:
                print(f"  ✗ {table}: {exc}")
                failed.append(table)

        # Cross-market price comparison (computed view, not a raw table)
        try:
            filename = f"{prefix}_price_comparison.csv"
            self._export_price_comparison(filename)
            exports["price_comparison"] = filename
        except Exception as exc:
            print(f"  ✗ price_comparison: {exc}")

        print(f"\nExport complete: {len(exports)} files written")
        if failed:
            print(f"Failed tables: {failed}")

        return exports

    def _export_price_comparison(self, filename: str) -> str:
        """
        Cross-market price comparison: one row per (barcode × market),
        filtered to barcodes that appear in 2+ markets.
        Ready for pivot analysis in Excel or pandas.
        """
        import csv as _csv
        from collections import Counter as _Counter

        query = """
            SELECT
                barcode,
                product_name,
                brand,
                market_name,
                regular_price,
                promo_price,
                store_id,
                product_url,
                image_url,
                last_updated
            FROM offers
            WHERE barcode IS NOT NULL
              AND TRIM(barcode) <> ''
            ORDER BY barcode, market_name
        """

        all_rows: List[Tuple[Any, ...]] = []
        if self.use_postgres:
            for db_url in self._iter_market_database_urls():
                try:
                    conn = self._connect_pg(db_url)
                    cursor = conn.cursor()
                    cursor.execute(query)
                    all_rows.extend(cursor.fetchall())
                    conn.close()
                except Exception:
                    pass
        else:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(query)
            all_rows = cursor.fetchall()
            conn.close()

        barcode_market_counts = _Counter(row[0] for row in all_rows)
        multi_market = {b for b, c in barcode_market_counts.items() if c >= 2}
        comparison_rows = [r for r in all_rows if r[0] in multi_market]

        columns = [
            "barcode", "product_name", "brand", "market_name",
            "regular_price", "promo_price", "store_id",
            "product_url", "image_url", "last_updated",
        ]
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow(columns)
            writer.writerows(comparison_rows)

        print(
            f"  ✓ price_comparison: {len(comparison_rows):,} rows "
            f"({len(multi_market):,} barcodes in 2+ markets) → {filename}"
        )
        return filename

    def get_summary(self) -> dict:
        if self.use_postgres:
            rows: List[Tuple[Any, ...]] = []
            for db_url in self._iter_market_database_urls():
                conn = self._connect_pg(db_url)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT market_name, COUNT(*) AS count, COUNT(DISTINCT product_name) AS unique_products
                    FROM offers
                    GROUP BY market_name
                    ORDER BY count DESC
                    """
                )
                rows.extend(cursor.fetchall())
                conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT market_name, COUNT(*) AS count, COUNT(DISTINCT product_name) AS unique_products
                FROM offers
                GROUP BY market_name
                ORDER BY count DESC
                """
            )
            rows = cursor.fetchall()
            conn.close()

        summary = {}
        total = 0
        for market, count, unique_products in rows:
            current = summary.get(market, {"count": 0, "unique_products": 0})
            current["count"] += int(count or 0)
            current["unique_products"] += int(unique_products or 0)
            summary[market] = current
            total += int(count or 0)
        summary["TOTAL"] = total
        return summary

    def get_store_pricing_insights(
        self,
        market_name: str,
        store_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        canonical_market = self._canonical_market_name(market_name)
        safe_limit = max(1, int(limit or 20))
        if self.use_postgres:
            conn = self._get_pg_for_market(canonical_market)
            cursor = conn.cursor()
            if store_id:
                cursor.execute(
                    """
                    SELECT *
                    FROM store_pricing_insights
                    WHERE market_name = %s AND store_id = %s
                    ORDER BY analyzed_at DESC
                    LIMIT %s
                    """,
                    (canonical_market, store_id, safe_limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM store_pricing_insights
                    WHERE market_name = %s
                    ORDER BY analyzed_at DESC, total_price_events DESC
                    LIMIT %s
                    """,
                    (canonical_market, safe_limit),
                )
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            conn.close()
            return self._rows_to_dicts(columns, rows)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        if store_id:
            cursor.execute(
                """
                SELECT *
                FROM store_pricing_insights
                WHERE market_name = ? AND store_id = ?
                ORDER BY analyzed_at DESC
                LIMIT ?
                """,
                (canonical_market, store_id, safe_limit),
            )
        else:
            cursor.execute(
                """
                SELECT *
                FROM store_pricing_insights
                WHERE market_name = ?
                ORDER BY analyzed_at DESC, total_price_events DESC
                LIMIT ?
                """,
                (canonical_market, safe_limit),
            )
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        conn.close()
        return self._rows_to_dicts(columns, rows)

    def get_product_price_patterns(
        self,
        market_name: str,
        store_id: Optional[str] = None,
        search_text: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        canonical_market = self._canonical_market_name(market_name)
        safe_limit = max(1, int(limit or 20))
        search_value = str(search_text or "").strip()
        if self.use_postgres:
            conn = self._get_pg_for_market(canonical_market)
            cursor = conn.cursor()
            query = [
                "SELECT * FROM product_price_patterns WHERE market_name = %s"
            ]
            params: List[Any] = [canonical_market]
            if store_id:
                query.append("AND store_id = %s")
                params.append(store_id)
            if search_value:
                query.append("AND product_name ILIKE %s")
                params.append(f"%{search_value}%")
            query.append("ORDER BY prediction_confidence DESC NULLS LAST, analyzed_at DESC LIMIT %s")
            params.append(safe_limit)
            cursor.execute(" ".join(query), params)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            conn.close()
            return self._rows_to_dicts(columns, rows)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        query = [
            "SELECT * FROM product_price_patterns WHERE market_name = ?"
        ]
        params = [canonical_market]
        if store_id:
            query.append("AND store_id = ?")
            params.append(store_id)
        if search_value:
            query.append("AND product_name LIKE ?")
            params.append(f"%{search_value}%")
        query.append("ORDER BY prediction_confidence DESC, analyzed_at DESC LIMIT ?")
        params.append(safe_limit)
        cursor.execute(" ".join(query), params)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        conn.close()
        return self._rows_to_dicts(columns, rows)

    def get_market_gtins(self, market_name: str, min_len: int = 8, limit: Optional[int] = None) -> List[str]:
        if self.use_postgres:
            canonical_market = self._canonical_market_name(market_name)
            conn = self._get_pg_for_market(canonical_market)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT COALESCE(barcode, gtin)
                FROM offers
                WHERE market_name = %s
                  AND COALESCE(barcode, gtin) IS NOT NULL
                  AND TRIM(COALESCE(barcode, gtin)) <> ''
                """,
                (market_name,),
            )
            raw_codes = [str(row[0]).strip() for row in cursor.fetchall()]
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT COALESCE(barcode, gtin)
                FROM offers
                WHERE market_name = ?
                  AND COALESCE(barcode, gtin) IS NOT NULL
                  AND TRIM(COALESCE(barcode, gtin)) <> ''
                """,
                (market_name,),
            )
            raw_codes = [str(row[0]).strip() for row in cursor.fetchall()]
            conn.close()

        normalized_codes: List[str] = []
        seen = set()
        for code in raw_codes:
            normalized = self.normalize_barcode(code)
            if not normalized or len(normalized) < min_len or normalized in seen:
                continue
            seen.add(normalized)
            normalized_codes.append(normalized)

        return normalized_codes[:limit] if limit is not None else normalized_codes

    def optimize_database(self) -> dict:
        if self.use_postgres:
            db_urls = [self.database_url_manager]
            db_urls.extend(self._iter_common_database_urls())
            db_urls.extend(self._iter_market_database_urls())

            seen: set = set()
            for db_url in db_urls:
                clean_url = str(db_url or "").strip()
                if not clean_url or clean_url in seen:
                    continue
                seen.add(clean_url)
                conn = self._connect_pg(db_url)
                conn.autocommit = True
                cursor = conn.cursor()
                # VACUUM reclaims dead-tuple space so pages can be reused
                # (prevents heap bloat from accumulating across daily UPSERTs)
                cursor.execute("VACUUM ANALYZE")
                conn.close()
            return {
                "db_path": "postgres",
                "size_before_bytes": 0,
                "size_after_bytes": 0,
                "bytes_saved": 0,
            }

        size_before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("ANALYZE")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("VACUUM")
        conn.close()

        size_after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "db_path": self.db_path,
            "size_before_bytes": size_before,
            "size_after_bytes": size_after,
            "bytes_saved": max(size_before - size_after, 0),
        }

    @staticmethod
    def _rows_to_dicts(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
        return [dict(zip(columns, row)) for row in rows]

    def get_offer_by_id(self, offer_id: str) -> Optional[Dict[str, Any]]:
        if self.use_postgres:
            market_name = self._market_from_offer_id(offer_id)
            candidate_urls = [self._get_market_database_url(market_name)] if market_name else self._iter_market_database_urls()
            row = None
            columns: List[str] = []
            for db_url in candidate_urls:
                conn = self._connect_pg(db_url)
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM offers WHERE id = %s", (offer_id,))
                row = cursor.fetchone()
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                conn.close()
                if row:
                    break
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM offers WHERE id = ?", (offer_id,))
            row = cursor.fetchone()
            columns = [info[1] for info in conn.execute("PRAGMA table_info(offers)").fetchall()]
            conn.close()

        if not row:
            return None
        return dict(zip(columns, row))

    def query_offers(
        self,
        market_name: Optional[str] = None,
        search_text: Optional[str] = None,
        only_with_barcode: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        limit = max(1, int(limit))

        if self.use_postgres:
            db_urls = []
            if market_name:
                db_urls = [self._get_market_database_url(self._canonical_market_name(market_name))]
            else:
                db_urls = self._iter_market_database_urls()

            rows: List[Sequence[Any]] = []
            columns: List[str] = []
            for db_url in db_urls:
                conn = self._connect_pg(db_url)
                cursor = conn.cursor()
                query = "SELECT * FROM offers WHERE 1=1"
                params: List[Any] = []
                if market_name:
                    query += " AND market_name = %s"
                    params.append(self._canonical_market_name(market_name))
                if search_text:
                    query += " AND (product_name ILIKE %s OR brand ILIKE %s)"
                    like_term = f"%{search_text}%"
                    params.extend([like_term, like_term])
                if only_with_barcode:
                    query += " AND barcode IS NOT NULL AND TRIM(barcode) <> ''"
                query += " ORDER BY last_updated DESC"
                cursor.execute(query, params)
                fetched = cursor.fetchall()
                if fetched and not columns:
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows.extend(fetched)
                conn.close()

            records = self._rows_to_dicts(columns, rows)
            records.sort(key=lambda item: str(item.get("last_updated") or ""), reverse=True)
            return records[:limit]

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        query = "SELECT * FROM offers WHERE 1=1"
        params: List[Any] = []
        if market_name:
            query += " AND market_name = ?"
            params.append(market_name)
        if search_text:
            query += " AND (product_name LIKE ? OR brand LIKE ?)"
            like_term = f"%{search_text}%"
            params.extend([like_term, like_term])
        if only_with_barcode:
            query += " AND barcode IS NOT NULL AND TRIM(barcode) <> ''"
        query += " ORDER BY last_updated DESC LIMIT ?"
        params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [info[1] for info in conn.execute("PRAGMA table_info(offers)").fetchall()]
        conn.close()
        return self._rows_to_dicts(columns, rows)

    def fetch_barcode_reference_catalog(
        self,
        source_markets: Optional[Sequence[str]] = None,
    ) -> List[Tuple[str, str, str, Optional[str], Optional[str], Optional[str]]]:
        requested = {
            self._canonical_market_name(name)
            for name in (
                source_markets
                or [
                    "Rossi",
                    "Atacadão",
                    "Nagumo",
                    "Higas",
                    "Swift",
                    "Sonda Delivery",
                    "XSupermercados",
                    "Barbosa",
                    "Carrefour",
                    "Oba Hortifruti",
                    "Extra",
                    "Pão de Açúcar",
                    "Tenda Atacado",
                    "Sam's Club",
                ]
            )
            if self._canonical_market_name(name)
        }

        requested_markets = sorted(m for m in requested if m)
        if not requested_markets:
            return []

        if self.use_postgres:
            conn = self._get_pg_for_common_table("product_catalog")
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT barcode, market_name, market_offer_id, product_name, brand, last_updated
                FROM barcode_reference_market_map
                WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
                  AND market_name = ANY(%s)
                """,
                (requested_markets,),
            )
            rows = cursor.fetchall()
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(requested_markets))
            cursor.execute(
                f"""
                SELECT barcode, market_name, market_offer_id, product_name, brand, last_updated
                FROM barcode_reference_market_map
                WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
                  AND market_name IN ({placeholders})
                """,
                requested_markets,
            )
            rows = cursor.fetchall()
            conn.close()

        catalog_rows: List[Tuple[str, str, str, Optional[str], Optional[str], Optional[str]]] = []
        for barcode, market_name, market_offer_id, product_name, brand, last_updated in rows:
            canonical_market = self._canonical_market_name(market_name)
            if canonical_market not in requested or not market_offer_id:
                continue
            normalized_barcode = self.normalize_barcode(barcode)
            if not normalized_barcode:
                continue
            catalog_rows.append(
                (
                    canonical_market,
                    str(market_offer_id),
                    normalized_barcode,
                    product_name,
                    brand,
                    last_updated,
                )
            )
        return catalog_rows

    def upsert_known_barcodes(self, rows: Sequence[Tuple[Any, ...]]) -> int:
        """
        Upsert into product_catalog. Rows are 10-tuples:
          (barcode, source_market, source_market_id, product_name, brand,
           description, normalized_name, normalized_brand, measure_token, last_updated)
        OR 11-tuples with market_count appended.

        product_catalog is the single source of truth for the barcode catalog.
        """
        if not rows:
            return 0

        # Ensure 12-tuple with market_count (default 1) and image_url (default None)
        def _pad(row: tuple) -> tuple:
            lst = list(row)
            if len(lst) == 10:
                lst.insert(9, 1)   # insert market_count before last_updated
            if len(lst) == 11:
                lst.insert(10, None)  # insert image_url before last_updated
            return tuple(lst)

        padded = [_pad(r) for r in rows]

        if self.use_postgres:
            sql = """
                INSERT INTO product_catalog (
                    barcode,
                    source_market,
                    source_market_id,
                    canonical_name,
                    canonical_brand,
                    canonical_description,
                    normalized_name,
                    normalized_brand,
                    measure_token,
                    market_count,
                    image_url,
                    last_updated
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (barcode) DO UPDATE SET
                    source_market     = EXCLUDED.source_market,
                    source_market_id  = EXCLUDED.source_market_id,
                    canonical_name    = COALESCE(EXCLUDED.canonical_name,    product_catalog.canonical_name),
                    canonical_brand   = COALESCE(EXCLUDED.canonical_brand,   product_catalog.canonical_brand),
                    canonical_description = COALESCE(EXCLUDED.canonical_description, product_catalog.canonical_description),
                    normalized_name   = COALESCE(EXCLUDED.normalized_name,   product_catalog.normalized_name),
                    normalized_brand  = COALESCE(EXCLUDED.normalized_brand,  product_catalog.normalized_brand),
                    measure_token     = COALESCE(EXCLUDED.measure_token,     product_catalog.measure_token),
                    image_url         = COALESCE(EXCLUDED.image_url,         product_catalog.image_url),
                    market_count      = GREATEST(EXCLUDED.market_count,      product_catalog.market_count),
                    last_updated      = EXCLUDED.last_updated
            """

            batch_size = max(50, config.PG_UPSERT_BATCH_SIZE)
            # Keep a stable lock acquisition order across concurrent runners.
            pg_rows = sorted(
                padded,
                key=lambda row: (
                    str(row[0] or ""),  # barcode
                    str(row[1] or ""),  # source_market
                    str(row[2] or ""),  # source_market_id
                ),
            )
            processed = 0

            def _apply_missing_catalog_columns_once() -> None:
                try:
                    fix_conn = self._get_pg_for_common_table("product_catalog")
                    fix_cursor = fix_conn.cursor()
                    for _col, _defn in [
                        ("source_market",     "TEXT"),
                        ("source_market_id",  "TEXT"),
                        ("normalized_name",   "TEXT"),
                        ("normalized_brand",  "TEXT"),
                        ("measure_token",     "TEXT"),
                        ("image_url",         "TEXT"),
                        ("market_count",      "INTEGER NOT NULL DEFAULT 1"),
                    ]:
                        try:
                            fix_cursor.execute(
                                f"ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS {_col} {_defn}"
                            )
                        except Exception:
                            fix_conn.rollback()
                    fix_conn.commit()
                    fix_conn.close()
                except Exception:
                    pass

            def _upsert_batch_with_retry(batch_rows: Sequence[Tuple[Any, ...]]) -> int:
                max_attempts = 4
                attempted_column_fix = False

                def _execute_batch(cursor, rows_to_write: Sequence[Tuple[Any, ...]]) -> int:
                    if len(rows_to_write) <= 25:
                        for row in rows_to_write:
                            cursor.execute(sql, row)
                        return len(rows_to_write)
                    cursor.executemany(sql, list(rows_to_write))
                    return len(rows_to_write)

                for attempt in range(1, max_attempts + 1):
                    conn = None
                    try:
                        conn = self._get_pg_for_common_table("product_catalog")
                        cursor = conn.cursor()
                        # Serialize concurrent upserts from parallel jobs into one writer
                        # per transaction, avoiding ON CONFLICT deadlock cycles.
                        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", (93621, 7))
                        written = _execute_batch(cursor, batch_rows)
                        conn.commit()
                        return written
                    except Exception as exc:
                        if conn is not None:
                            try:
                                conn.rollback()
                            except Exception:
                                pass

                        err_str = str(exc).lower()
                        missing_column_error = (
                            "column" in err_str and ("does not exist" in err_str or "undefined" in err_str)
                        )
                        if missing_column_error and not attempted_column_fix:
                            attempted_column_fix = True
                            _apply_missing_catalog_columns_once()
                            continue

                        transient = self._is_transient_pg_error(exc)
                        if transient and attempt < max_attempts:
                            wait_seconds = min(10.0, 1.2 * (2 ** (attempt - 1)))
                            print(
                                "product_catalog upsert retry after transient Postgres error: "
                                f"{type(exc).__name__} (attempt {attempt}/{max_attempts}), "
                                f"sleeping {wait_seconds:.1f}s"
                            )
                            time.sleep(wait_seconds)
                            continue
                        raise
                    finally:
                        if conn is not None:
                            try:
                                conn.close()
                            except Exception:
                                pass

                return 0

            def _upsert_batch_adaptive(batch_rows: Sequence[Tuple[Any, ...]]) -> int:
                try:
                    return _upsert_batch_with_retry(batch_rows)
                except Exception as exc:
                    if self._is_transient_pg_error(exc) and len(batch_rows) > 1:
                        split_at = len(batch_rows) // 2
                        left = batch_rows[:split_at]
                        right = batch_rows[split_at:]
                        print(
                            "product_catalog upsert transient error after retries; "
                            f"splitting batch {len(batch_rows)} -> {len(left)} + {len(right)}"
                        )
                        return _upsert_batch_adaptive(left) + _upsert_batch_adaptive(right)
                    raise

            for start in range(0, len(pg_rows), batch_size):
                batch = pg_rows[start : start + batch_size]
                processed += _upsert_batch_adaptive(batch)

            return processed

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO product_catalog (
                barcode,
                source_market,
                source_market_id,
                canonical_name,
                canonical_brand,
                canonical_description,
                normalized_name,
                normalized_brand,
                measure_token,
                market_count,
                image_url,
                last_updated
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            padded,
        )
        conn.commit()
        conn.close()
        return len(rows)

    def fetch_known_barcodes_candidates(
        self,
        target_market: str,
        target_brand: str,
        target_measure: str,
        anchor_token: str,
        limit: int = 250,
    ) -> List[Tuple[Any, ...]]:
        limit = max(1, int(limit))
        if self.use_postgres:
            conn = self._get_pg_for_common_table("product_catalog")
            cursor = conn.cursor()
            query = """
                SELECT
                    barcode,
                    source_market,
                    source_market_id,
                    canonical_name,
                    canonical_brand,
                    canonical_description,
                    normalized_name,
                    normalized_brand,
                    measure_token
                FROM product_catalog
                WHERE source_market != %s
                  AND (
                        (%s <> '' AND normalized_brand = %s)
                     OR (%s <> '' AND measure_token = %s)
                     OR (%s <> '' AND normalized_name LIKE %s)
                  )
                LIMIT %s
                """
            params = (
                target_market,
                target_brand, target_brand,
                target_measure, target_measure,
                anchor_token, f"%{anchor_token}%",
                limit,
            )
            try:
                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()
                return rows
            except Exception:
                # Auto-migrate missing columns then retry
                conn.rollback()
                for _col, _defn in [
                    ("source_market",    "TEXT"),
                    ("source_market_id", "TEXT"),
                    ("normalized_name",  "TEXT"),
                    ("normalized_brand", "TEXT"),
                    ("measure_token",    "TEXT"),
                ]:
                    try:
                        cursor.execute(
                            f"ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS {_col} {_defn}"
                        )
                    except Exception:
                        conn.rollback()
                conn.commit()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()
                return rows

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                barcode,
                source_market,
                source_market_id,
                canonical_name,
                canonical_brand,
                canonical_description,
                normalized_name,
                normalized_brand,
                measure_token
            FROM product_catalog
            WHERE source_market != ?
              AND (
                    (? <> '' AND normalized_brand = ?)
                 OR (? <> '' AND measure_token = ?)
                 OR (? <> '' AND normalized_name LIKE ?)
              )
            LIMIT ?
            """,
            (
                target_market,
                target_brand,
                target_brand,
                target_measure,
                target_measure,
                anchor_token,
                f"%{anchor_token}%",
                limit,
            ),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def fetch_all_known_barcodes(self) -> List[Tuple[Any, ...]]:
        """Bulk-fetch the entire product_catalog for in-memory matching.

        Returns rows as 9-tuples:
          (barcode, source_market, source_market_id, canonical_name,
           canonical_brand, canonical_description,
           normalized_name, normalized_brand, measure_token)

        Falls back gracefully if the new columns don't exist yet on an
        older schema — runs ALTER TABLE automatically then retries.
        """
        sql = """
            SELECT
                barcode,
                source_market,
                source_market_id,
                canonical_name,
                canonical_brand,
                canonical_description,
                normalized_name,
                normalized_brand,
                measure_token,
                image_url
            FROM product_catalog
        """
        if self.use_postgres:
            conn = self._get_pg_for_common_table("product_catalog")
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
                conn.close()
                return rows
            except Exception:
                # Columns may not exist on older schema — migrate then retry
                conn.rollback()
                for _col, _defn in [
                    ("source_market",     "TEXT"),
                    ("source_market_id",  "TEXT"),
                    ("normalized_name",   "TEXT"),
                    ("normalized_brand",  "TEXT"),
                    ("measure_token",     "TEXT"),
                    ("image_url",         "TEXT"),
                ]:
                    try:
                        cursor.execute(
                            f"ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS {_col} {_defn}"
                        )
                    except Exception:
                        conn.rollback()
                conn.commit()
                cursor.execute(sql)
                rows = cursor.fetchall()
                conn.close()
                return rows
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            conn.close()
            return rows

    def get_known_barcodes_snapshot(self, source_markets: Optional[Sequence[str]] = None) -> Optional[str]:
        filters = list(source_markets or [])

        if self.use_postgres:
            conn = self._get_pg_for_common_table("product_catalog")
            cursor = conn.cursor()
            if filters:
                cursor.execute(
                    """
                    SELECT source_market, source_market_id, barcode, normalized_name, normalized_brand, measure_token
                    FROM product_catalog
                    WHERE source_market = ANY(%s)
                    ORDER BY source_market, source_market_id
                    """,
                    (filters,),
                )
            else:
                cursor.execute(
                    """
                    SELECT source_market, source_market_id, barcode, normalized_name, normalized_brand, measure_token
                    FROM product_catalog
                    ORDER BY source_market, source_market_id
                    """
                )
            rows = cursor.fetchall()
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            if filters:
                placeholders = ",".join(["?"] * len(filters))
                cursor.execute(
                    f"""
                    SELECT source_market, source_market_id, barcode, normalized_name, normalized_brand, measure_token
                    FROM product_catalog
                    WHERE source_market IN ({placeholders})
                    ORDER BY source_market, source_market_id
                    """,
                    filters,
                )
            else:
                cursor.execute(
                    """
                    SELECT source_market, source_market_id, barcode, normalized_name, normalized_brand, measure_token
                    FROM product_catalog
                    ORDER BY source_market, source_market_id
                    """
                )
            rows = cursor.fetchall()
            conn.close()

        if not rows:
            return None

        digest = hashlib.sha1()
        for row in rows:
            digest.update("\x1f".join("" if value is None else str(value) for value in row).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def fetch_barcode_inference_state(self, offer_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not offer_ids:
            return {}

        if self.use_postgres:
            conn = self._get_pg_for_common_table("barcode_inference_state")
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT offer_id, offer_signature, catalog_snapshot, matched, no_match_count, blacklisted, last_attempted_at
                FROM barcode_inference_state
                WHERE offer_id = ANY(%s)
                """,
                (list(offer_ids),),
            )
            rows = cursor.fetchall()
            conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(offer_ids))
            cursor.execute(
                f"""
                SELECT offer_id, offer_signature, catalog_snapshot, matched, no_match_count, blacklisted, last_attempted_at
                FROM barcode_inference_state
                WHERE offer_id IN ({placeholders})
                """,
                list(offer_ids),
            )
            rows = cursor.fetchall()
            conn.close()

        return {
            row[0]: {
                "offer_signature": row[1],
                "catalog_snapshot": row[2],
                "matched": row[3],
                "no_match_count": int(row[4] or 0),
                "blacklisted": bool(row[5]),
                "last_attempted_at": row[6],
            }
            for row in rows
        }

    def upsert_barcode_inference_states(
        self,
        rows: Sequence[Tuple[str, str, Optional[str], bool, int, bool, str]],
    ) -> int:
        if not rows:
            return 0

        # Postgres INTEGER columns reject Python bools — cast matched/blacklisted to int
        # Row layout: (offer_id, offer_signature, catalog_snapshot, matched, no_match_count, blacklisted, last_attempted_at)
        def _coerce(row: tuple) -> tuple:
            lst = list(row)
            if len(lst) > 3:
                lst[3] = int(lst[3]) if isinstance(lst[3], bool) else lst[3]  # matched
            if len(lst) > 5:
                lst[5] = int(lst[5]) if isinstance(lst[5], bool) else lst[5]  # blacklisted
            return tuple(lst)

        coerced = [_coerce(r) for r in rows]

        # Detect whether any row carries the inferred_barcode (8th column).
        # If so, pad all 7-element rows to 8 so every row matches the same query.
        has_barcode_col = any(len(r) >= 8 for r in coerced)
        if has_barcode_col:
            coerced = [r if len(r) >= 8 else r + (None,) for r in coerced]

        if self.use_postgres:
            conn = self._get_pg_for_common_table("barcode_inference_state")
            cursor = conn.cursor()
            if has_barcode_col:
                cursor.executemany(
                    """
                    INSERT INTO barcode_inference_state (
                        offer_id, offer_signature, catalog_snapshot,
                        matched, no_match_count, blacklisted, last_attempted_at, inferred_barcode
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (offer_id) DO UPDATE SET
                        offer_signature = EXCLUDED.offer_signature,
                        catalog_snapshot = EXCLUDED.catalog_snapshot,
                        matched = EXCLUDED.matched,
                        no_match_count = EXCLUDED.no_match_count,
                        blacklisted = EXCLUDED.blacklisted,
                        last_attempted_at = EXCLUDED.last_attempted_at,
                        inferred_barcode = COALESCE(EXCLUDED.inferred_barcode, barcode_inference_state.inferred_barcode)
                    """,
                    coerced,
                )
            else:
                cursor.executemany(
                    """
                    INSERT INTO barcode_inference_state (
                        offer_id, offer_signature, catalog_snapshot,
                        matched, no_match_count, blacklisted, last_attempted_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (offer_id) DO UPDATE SET
                        offer_signature = EXCLUDED.offer_signature,
                        catalog_snapshot = EXCLUDED.catalog_snapshot,
                        matched = EXCLUDED.matched,
                        no_match_count = EXCLUDED.no_match_count,
                        blacklisted = EXCLUDED.blacklisted,
                        last_attempted_at = EXCLUDED.last_attempted_at
                    """,
                    coerced,
                )
            conn.commit()
            conn.close()
            return len(rows)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO barcode_inference_state (
                offer_id,
                offer_signature,
                catalog_snapshot,
                matched,
                no_match_count,
                blacklisted,
                last_attempted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            coerced,
        )
        conn.commit()
        conn.close()
        return len(rows)


    def bulk_lookup_inferred_barcodes(self, offer_ids: Sequence[str]) -> Dict[str, str]:
        """Return {offer_id: inferred_barcode} for all matched offers that have a saved barcode.

        Used as Phase 0 of inference: instantly re-apply previously inferred barcodes
        to offer rows that were re-scraped without barcodes (same stable offer_id).
        """
        if not offer_ids or not self.use_postgres:
            return {}
        conn = self._get_pg_for_common_table("barcode_inference_state")
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT offer_id, inferred_barcode
            FROM barcode_inference_state
            WHERE offer_id = ANY(%s)
              AND matched = 1
              AND inferred_barcode IS NOT NULL
              AND TRIM(inferred_barcode) <> ''
            """,
            (list(offer_ids),),
        )
        rows = cursor.fetchall()
        conn.close()
        return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

    def bulk_lookup_fingerprint_cache(self, fingerprints: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Return {fingerprint: {inferred_barcode, confidence, method, ...}} for cache hits.

        Phase 1 of inference: cross-market product fingerprint cache.
        Same physical product (same brand+name+size) maps to one fingerprint
        regardless of which market or offer_id surfaced it.
        """
        if not fingerprints or not self.use_postgres:
            return {}
        conn = self._get_pg_for_common_table("barcode_fingerprint_cache")
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT fingerprint, inferred_barcode, confidence, method, source_market, source_offer_id
            FROM barcode_fingerprint_cache
            WHERE fingerprint = ANY(%s)
            """,
            (list(fingerprints),),
        )
        rows = cursor.fetchall()
        conn.close()
        return {
            str(row[0]): {
                "inferred_barcode": str(row[1]),
                "confidence": float(row[2] or 0.0),
                "method": str(row[3] or ""),
                "source_market": row[4],
                "source_offer_id": row[5],
            }
            for row in rows if row[0] and row[1]
        }

    def upsert_fingerprint_cache_rows(self, rows: Sequence[tuple]) -> int:
        """Insert/update fingerprint cache rows.

        Row layout: (fingerprint, inferred_barcode, confidence, method, source_market, source_offer_id, confirmed_at)
        """
        if not rows or not self.use_postgres:
            return 0
        conn = self._get_pg_for_common_table("barcode_fingerprint_cache")
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO barcode_fingerprint_cache
                (fingerprint, inferred_barcode, confidence, method, source_market, source_offer_id, confirmed_at, hit_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (fingerprint) DO UPDATE SET
                inferred_barcode = EXCLUDED.inferred_barcode,
                confidence = EXCLUDED.confidence,
                method = EXCLUDED.method,
                source_market = EXCLUDED.source_market,
                source_offer_id = EXCLUDED.source_offer_id,
                confirmed_at = EXCLUDED.confirmed_at,
                hit_count = barcode_fingerprint_cache.hit_count + 1
            """,
            list(rows),
        )
        conn.commit()
        conn.close()
        return len(rows)

    def upsert_model_inference_audit_rows(self, rows) -> int:
        """Persist embedding/model inference audit rows to model_inference_audit table."""
        if not rows:
            return 0
        if self.use_postgres:
            conn = self._get_pg_for_common_table("model_inference_audit")
            try:
                cursor = conn.cursor()
                # Ensure table exists
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS model_inference_audit (
                        id SERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        offer_id TEXT NOT NULL,
                        market_name TEXT,
                        model_name TEXT,
                        embedding_matched BOOLEAN,
                        applied_to_offer BOOLEAN,
                        selected_barcode TEXT,
                        source_market TEXT,
                        source_market_id TEXT,
                        confidence REAL,
                        second_confidence REAL,
                        reasoning TEXT,
                        offer_signature TEXT,
                        recorded_at TIMESTAMP NOT NULL
                    )
                    """
                )
                cursor.executemany(
                    """
                    INSERT INTO model_inference_audit (
                        run_id, offer_id, market_name, model_name,
                        embedding_matched, applied_to_offer,
                        selected_barcode, source_market, source_market_id,
                        confidence, second_confidence, reasoning,
                        offer_signature, recorded_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    list(rows),
                )
                conn.commit()
            finally:
                conn.close()
            return len(rows)
        # SQLite fallback
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS model_inference_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    market_name TEXT,
                    model_name TEXT,
                    embedding_matched INTEGER,
                    applied_to_offer INTEGER,
                    selected_barcode TEXT,
                    source_market TEXT,
                    source_market_id TEXT,
                    confidence REAL,
                    second_confidence REAL,
                    reasoning TEXT,
                    offer_signature TEXT,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            cursor.executemany(
                """
                INSERT INTO model_inference_audit (
                    run_id, offer_id, market_name, model_name,
                    embedding_matched, applied_to_offer,
                    selected_barcode, source_market, source_market_id,
                    confidence, second_confidence, reasoning,
                    offer_signature, recorded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                list(rows),
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)


    def run_select_query(self, query: str, params=None) -> list:
        """Execute a raw SELECT query and return rows as dicts."""
        stripped = query.strip().rstrip(";")
        if not stripped.upper().startswith("SELECT"):
            raise ValueError("Only SELECT queries are allowed via run_select_query.")
        if self.use_postgres:
            conn = self._get_pg()
            try:
                cursor = conn.cursor()
                cursor.execute(stripped, params or [])
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
            finally:
                conn.close()
            return self._rows_to_dicts(columns, rows)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(stripped, params or [])
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            conn.close()
        return self._rows_to_dicts(columns, rows)

    def upsert_match_audit(
        self,
        target_offer_id: str,
        target_market: str,
        inferred_barcode: str,
        source_market: str,
        source_market_id: str,
        match_method: str,
        confidence: float,
        reasoning: str,
        last_updated: str,
    ):
        if self.use_postgres:
            conn = self._get_pg_for_common_table("match_audit")
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO match_audit (
                    target_offer_id,
                    target_market,
                    inferred_barcode,
                    source_market,
                    source_market_id,
                    match_method,
                    confidence,
                    reasoning,
                    last_updated
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (target_offer_id) DO UPDATE SET
                    target_market = EXCLUDED.target_market,
                    inferred_barcode = EXCLUDED.inferred_barcode,
                    source_market = EXCLUDED.source_market,
                    source_market_id = EXCLUDED.source_market_id,
                    match_method = EXCLUDED.match_method,
                    confidence = EXCLUDED.confidence,
                    reasoning = EXCLUDED.reasoning,
                    last_updated = EXCLUDED.last_updated
                """,
                (
                    target_offer_id,
                    target_market,
                    inferred_barcode,
                    source_market,
                    source_market_id,
                    match_method,
                    confidence,
                    reasoning,
                    last_updated,
                ),
            )
            conn.commit()
            conn.close()
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO match_audit (
                target_offer_id,
                target_market,
                inferred_barcode,
                source_market,
                source_market_id,
                match_method,
                confidence,
                reasoning,
                last_updated
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_offer_id,
                target_market,
                inferred_barcode,
                source_market,
                source_market_id,
                match_method,
                confidence,
                reasoning,
                last_updated,
            ),
        )
        conn.commit()
        conn.close()
