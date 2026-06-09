import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from db.barcode_ai_matcher import BarcodeAIMatcher
from db.db_manager import DatabaseManager
from cart_builder import build_store_carts, load_shopping_list
from swift_cart_automation import run_swift_cart_automation
from env_loader import load_env_file
import config
from db.storage_controller import run as run_storage_controller
from location_detector import LocationDetector
from markets.tier3_no_barcodes.market_scrap_atacadao_departamentos import AtacadaoDepartamentosScraper
from markets.tier3_no_barcodes.market_scrap_higas_departamentos import HigasDepartamentosScraper
from markets.tier3_no_barcodes.market_scrap_nagumo_departamentos import NagumoDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_tenda_departamentos import TendaAtacadoDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_rossi_departamentos import RossiDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_xsupermercados_departamentos import XSupermercadosDepartamentosScraper
from markets.tier2_partial_barcodes.market_scrap_sonda_departamentos import SondaDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_swift_departamentos import SwiftDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_barbosa_departamentos import BarbosaDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_carrefour_departamentos import CarrefourDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_oba_departamentos import ObaDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_samsclub_departamentos import SamsClubDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_extra_departamentos import ExtraDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_paodeacucar_departamentos import PaoDeAcucarDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_davo_departamentos import DavoDepartamentosScraper
from markets.tier1_inline_barcodes.market_scrap_giga_departamentos import GigaDepartamentosScraper
from markets.tier3_no_barcodes.higas_barcode_enrich import run as run_higas_barcode_enrich
from markets.tier1_inline_barcodes.extra_barcode_enrich import run as run_extra_barcode_enrich

load_env_file()

# ── Single-market mode detection ─────────────────────────────────────────────
# Check BEFORE anything else. SCRAPE_MARKET or MARKET_NAME env var means this
# repo should run only one market. Read directly from os.environ so it works
# whether set via GitHub Actions step env:, shell, or .env file.
_SINGLE_MARKET = (
    os.environ.get("SCRAPE_MARKET", "").strip()
    or os.environ.get("MARKET_NAME", "").strip()
)
# Also check config (which reads from .env via os.getenv)
if not _SINGLE_MARKET:
    _SINGLE_MARKET = os.getenv("SCRAPE_MARKET", "").strip() or os.getenv("MARKET_NAME", "").strip()

if _SINGLE_MARKET:
    print(f"[single-market mode] MARKET={_SINGLE_MARKET!r}")


MARKET_ATACADAO = "Atacad\u00e3o"


def _deduplicate_higas_offers(db) -> int:
    """Remove duplicate Higas offers that share the same barcode.

    After Instabuy enrichment multiple offer rows can end up with the
    same barcode (different product IDs for the same physical product).
    We keep the row with the lowest offer_id (first scraped) and delete
    the rest, preserving the canonical product data.

    Returns the number of duplicate rows removed.
    """
    if not db.use_postgres:
        return 0  # SQLite path not used in production
    try:
        conn = db._get_pg_for_market("Higas")
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM offers
            WHERE market_name = 'Higas'
              AND barcode IS NOT NULL
              AND TRIM(barcode) <> ''
              AND ctid NOT IN (
                  SELECT MIN(ctid)
                  FROM offers
                  WHERE market_name = 'Higas'
                    AND barcode IS NOT NULL
                    AND TRIM(barcode) <> ''
                  GROUP BY barcode
              )
            """
        )
        removed = cursor.rowcount
        conn.commit()
        conn.close()
        if removed > 0:
            print(f"  Higas dedup: removed {removed} duplicate barcode rows")
        return removed
    except Exception as exc:
        print(f"  Higas dedup warning: {exc}")
        return 0


class MarketIntegrationSystem:
    def __init__(self):
        self.db = DatabaseManager()
        self.barcode_ai_matcher = BarcodeAIMatcher()
        self.skip_updated_within_days = self._read_skip_days()
        self.skip_barcode_inference = self._read_skip_barcode_inference()
        # When set, market runners skip expensive per-market inference/backfill
        # so a single centralized barcode phase can run once at the end.
        self._centralized_barcode_phase_active = self._is_truthy(
            os.getenv("CENTRALIZED_BARCODE_PHASE_ACTIVE")
        )

    @staticmethod
    def _is_truthy(raw: Optional[str]) -> bool:
        value = str(raw or "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _read_skip_barcode_inference(self) -> bool:
        return self._is_truthy("1" if config.SKIP_BARCODE_INFERENCE else "")

    @staticmethod
    def _read_skip_days() -> float:
        raw = str(config.SKIP_UPDATED_WITHIN_DAYS).strip()
        try:
            value = float(raw)
        except ValueError:
            return 1.0
        return value if value >= 0 else 1.0

    @staticmethod
    def _normalize_limit(limit: Optional[int]) -> Optional[int]:
        if limit is None:
            return None
        try:
            parsed = int(limit)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours}h {minutes:02d}m {secs:02d}s"
        if minutes > 0:
            return f"{minutes}m {secs:02d}s"
        return f"{secs}s"

    @staticmethod
    def _print_catalog_sync_summary(stats: Dict[str, Any], label: str = "Persistent barcode driver") -> None:
        if stats.get("skipped"):
            interval_hours = float(stats.get("min_interval_hours", 0.0) or 0.0)
            remaining_minutes = int(stats.get("remaining_minutes", 0) or 0)
            print(
                f"{label}: skipped (recent sync within {interval_hours:g}h window, "
                f"~{remaining_minutes}m remaining)"
            )
            return

        print(
            f"{label}: "
            f"{stats['upserted']} upserted from {stats['source_rows']} trusted rows "
            f"into {stats['catalog_db_path']}"
        )

    @staticmethod
    def _runner_to_cli_mode(runner: Any) -> Optional[str]:
        """Map a runner method to the corresponding CLI mode token."""
        runner_name = getattr(runner, "__name__", "")
        if not runner_name.startswith("run_") or not runner_name.endswith("_only"):
            return None
        return runner_name[len("run_"):-len("_only")]

    @staticmethod
    def _resolve_parallel_workers(explicit_workers: Optional[int], market_count: int) -> int:
        if explicit_workers is not None and explicit_workers > 0:
            return max(1, min(explicit_workers, market_count))

        env_workers = str(os.getenv("SCRAPE_PARALLEL_WORKERS", "")).strip()
        if env_workers:
            try:
                parsed = int(env_workers)
                if parsed > 0:
                    return max(1, min(parsed, market_count))
            except ValueError:
                pass

        # Conservative default to avoid overloading DB/network locally.
        return max(1, min(4, market_count))

    def _run_market_subprocess(
        self,
        market_name: str,
        cli_mode: str,
        zip_code: str,
        limit: Optional[int],
    ) -> Dict[str, Any]:
        """Run one market mode in an isolated subprocess for safe local parallelism."""
        started_at = datetime.now()
        script_path = Path(__file__).resolve()
        command = [sys.executable, str(script_path), cli_mode]
        if limit is not None:
            command.append(str(limit))

        env = os.environ.copy()
        env["SCRAPE_ZIP_CODE"] = str(zip_code)
        env["CENTRALIZED_BARCODE_PHASE_ACTIVE"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")

        completed = subprocess.run(
            command,
            cwd=str(script_path.parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        finished_at = datetime.now()
        elapsed = max(0.0, (finished_at - started_at).total_seconds())
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        skipped_recent = (
            completed.returncode == 0
            and "SKIP_UPDATED_WITHIN_DAYS" in stdout
            and "Skipping " in stdout
        )

        if completed.returncode != 0:
            status = f"Failed (exit {completed.returncode})"
            timing_status = "failed"
        elif skipped_recent:
            status = "Skipped (recent update)"
            timing_status = "skipped"
        else:
            status = "Success"
            timing_status = "success"

        return {
            "market_name": market_name,
            "status": status,
            "timing_status": timing_status,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed": elapsed,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _run_parallel_market_batch(
        self,
        markets_batch: List[Tuple[str, str]],
        zip_code: str,
        limit: Optional[int],
        app_offers_ceps: Dict[str, str],
        requested_workers: Optional[int] = None,
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, float, str]]]:
        """Execute a phase of markets concurrently and return summary rows + timings."""
        if not markets_batch:
            return [], []

        workers = self._resolve_parallel_workers(requested_workers, len(markets_batch))
        print(
            f"Running {len(markets_batch)} markets in parallel "
            f"(workers={workers}) for ZIP {zip_code}."
        )

        results_by_market: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self._run_market_subprocess, market_name, cli_mode, zip_code, limit): market_name
                for market_name, cli_mode in markets_batch
            }
            for future in as_completed(future_map):
                market_name = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    now = datetime.now()
                    result = {
                        "market_name": market_name,
                        "status": f"Failed: {exc}",
                        "timing_status": "failed",
                        "started_at": now,
                        "finished_at": now,
                        "elapsed": 0.0,
                        "stdout": "",
                        "stderr": str(exc),
                    }

                results_by_market[market_name] = result
                print(
                    f"  [{market_name}] {result['status']} "
                    f"in {self._format_duration(result['elapsed'])}"
                )
                if result["timing_status"] == "failed":
                    if result["stdout"].strip():
                        print(f"  [{market_name}] stdout:\n{result['stdout'][-4000:]}")
                    if result["stderr"].strip():
                        print(f"  [{market_name}] stderr:\n{result['stderr'][-4000:]}")

        ordered_results: List[Tuple[str, str]] = []
        ordered_timings: List[Tuple[str, float, str]] = []
        for market_name, _cli_mode in markets_batch:
            result = results_by_market.get(market_name)
            if not result:
                continue

            ordered_results.append((market_name, result["status"]))
            ordered_timings.append((market_name, result["elapsed"], result["timing_status"]))

            self._record_timing(
                process_name="all_departamentos",
                run_type="all",
                step_name=f"{market_name} departamentos",
                market_name=market_name,
                status=result["timing_status"],
                started_at=result["started_at"],
                finished_at=result["finished_at"],
                zip_code=zip_code,
                details=(
                    f"{result['status']}"
                    if result["timing_status"] != "success"
                    else None
                ),
            )

            if result["timing_status"] == "success":
                try:
                    from sync_app_offers import sync_one_market as _sync_one_market
                    market_url = self.db._get_market_database_url(market_name)
                    synced_rows = _sync_one_market(market_url, app_offers_ceps)
                    print(f"  app_offers: {market_name} -> {synced_rows:,} rows upserted")
                except Exception as sync_exc:
                    print(f"  app_offers sync warning ({market_name}): {sync_exc}")

        return ordered_results, ordered_timings

    def run_build_cart_from_list(
        self,
        input_path: str = "shopping_list.json",
        output_dir: Optional[str] = None,
        markets: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build per-market cart CSV files from a shopping list JSON.

        Input file accepts either:
        - list of items, or
        - object with {"items": [...], "markets": [...], "output_dir": "..."}
        """
        resolved_input = Path(input_path)
        items = load_shopping_list(resolved_input)

        payload = json.loads(resolved_input.read_text(encoding="utf-8"))
        payload_markets: Optional[List[str]] = None
        payload_output_dir: Optional[str] = None
        if isinstance(payload, dict):
            raw_markets = payload.get("markets")
            if isinstance(raw_markets, list):
                payload_markets = [str(m).strip() for m in raw_markets if str(m or "").strip()]
            raw_out = payload.get("output_dir")
            if raw_out and str(raw_out).strip():
                payload_output_dir = str(raw_out).strip()

        selected_markets = markets or payload_markets
        target_output = Path(output_dir or payload_output_dir or "cart_output")
        target_output = target_output / datetime.now().strftime("%Y%m%d_%H%M%S")

        summary = build_store_carts(
            db=self.db,
            items=items,
            output_dir=target_output,
            markets=selected_markets,
        )

        print("\n--- Cart build summary ---")
        print(f"Input: {resolved_input}")
        print(f"Output folder: {target_output}")
        for market_name, market_summary in summary.get("markets", {}).items():
            print(
                f"- {market_name}: matched {market_summary['items_matched']}/{market_summary['items_requested']} "
                f"({market_summary['coverage']}%), est_total=R$ {market_summary['estimated_total']}"
            )
        unmatched = summary.get("unmatched_items", [])
        if unmatched:
            print(f"Unmatched items ({len(unmatched)}): {', '.join(unmatched)}")
        else:
            print("All items were matched in at least one market.")
        print("--- Cart build complete ---\n")
        return summary

    def _departamentos_runners(self) -> List[Tuple[str, Any]]:
        """Single source of truth for departamentos runners.

        ORDER MATTERS — markets that return barcodes inline run first so their
        data populates product_catalog before barcode-less markets run.
        After all barcode-rich markets finish, a catalog sync runs so
        Higas/Atacadão/Nagumo enrichment has the fullest possible catalog.

        Tier 1 — inline barcodes (feed product_catalog immediately):
          Swift, Carrefour, XSupermercados, Barbosa, Rossi, Extra, Pão de Açúcar

        Tier 2 — partial barcodes (Sonda ~41%):
          Sonda Delivery

        Tier 3 — no barcodes, rely on cross-market catalog lookup:
          Atacadão, Higas, Nagumo
        """
        return [
            # ── Tier 1: inline barcode coverage ≥ 96% ──────────────────────
            ("Swift",          self.run_swift_departamentos_only),
            ("Carrefour",      self.run_carrefour_departamentos_only),
            ("XSupermercados", self.run_xsupermercados_departamentos_only),
            ("Barbosa",        self.run_barbosa_departamentos_only),
            ("Rossi",          self.run_rossi_departamentos_only),
            ("Extra",          self.run_extra_departamentos_only),
            ("Pão de Açúcar",  self.run_paodeacucar_departamentos_only),
            ("Oba Hortifruti", self.run_oba_departamentos_only),
            ("Sam's Club",     self.run_samsclub_departamentos_only),
            ("Tenda Atacado",  self.run_tenda_departamentos_only),
            ("Davo",           self.run_davo_departamentos_only),
            ("Giga",           self.run_giga_departamentos_only),
            # ── Tier 2: partial barcodes ────────────────────────────────────
            ("Sonda Delivery", self.run_sonda_departamentos_only),
            # ── Catalog sync before Tier 3 ──────────────────────────────────
            ("_catalog_sync",  self._run_catalog_sync_step),
            # ── Tier 3: no inline barcodes — cross-market lookup needed ─────
            (MARKET_ATACADAO,       self.run_atacadao_departamentos_only),
            ("Nagumo",              self.run_nagumo_departamentos_only),  # store-specific: resolves nearest store via ZIP
            ("Higas",               self.run_higas_departamentos_only),
        ]

    def _resolve_store_id_for_skip(self, market_name: str, zip_code: str) -> Optional[str]:
        # Fixed-store markets: store_id is always the same regardless of ZIP
        fixed_store_ids = {
            "Sonda Delivery": SondaDepartamentosScraper.STORE_ID,
            "Oba Hortifruti": ObaDepartamentosScraper.STORE_ID,
            "Sam's Club":     SamsClubDepartamentosScraper.STORE_ID,
            "Extra": ExtraDepartamentosScraper.STORE_ID,
            "Pão de Açúcar": PaoDeAcucarDepartamentosScraper.STORE_ID,
            "Davo": DavoDepartamentosScraper.STORE_ID,
            "Giga": GigaDepartamentosScraper.STORE_ID,
        }
        if market_name in fixed_store_ids:
            return fixed_store_ids[market_name]

        scraper: Optional[Any] = None
        try:
            if market_name == "Rossi":
                scraper = RossiDepartamentosScraper()
            elif market_name == "Nagumo":
                scraper = NagumoDepartamentosScraper()
            elif market_name == MARKET_ATACADAO:
                scraper = AtacadaoDepartamentosScraper()
            elif market_name == "Higas":
                scraper = HigasDepartamentosScraper()
            elif market_name == "XSupermercados":
                scraper = XSupermercadosDepartamentosScraper()
            elif market_name == "Barbosa":
                scraper = BarbosaDepartamentosScraper()
            elif market_name == "Swift":
                scraper = SwiftDepartamentosScraper()
            elif market_name == "Carrefour":
                scraper = CarrefourDepartamentosScraper()
            elif market_name == "Tenda Atacado":
                scraper = TendaAtacadoDepartamentosScraper()

            if scraper is None:
                return None

            resolver = getattr(scraper, "resolve_store", None)
            if not callable(resolver):
                return None

            resolved_store_id = resolver(zip_code)
            if not resolved_store_id:
                return None

            metadata = getattr(scraper, "_resolved_store_metadata", None) or {}
            if hasattr(scraper, "db"):
                scraper.db.cache_store_id(
                    zip_code,
                    market_name,
                    str(resolved_store_id),
                    store_name=metadata.get("store_name"),
                    store_address=metadata.get("store_address"),
                    store_city=metadata.get("store_city"),
                    store_state=metadata.get("store_state"),
                    latitude=metadata.get("latitude"),
                    longitude=metadata.get("longitude"),
                    store_payload=metadata.get("store_payload"),
                )
            return str(resolved_store_id)
        except Exception:
            return None

    def _should_skip_recent_market(self, market_name: str, zip_code: str) -> Tuple[bool, Optional[str]]:
        if self.skip_updated_within_days <= 0:
            return False, None

        resolved_store_id = self._resolve_store_id_for_skip(market_name, zip_code)
        if not resolved_store_id:
            return False, None

        last_updated = self.db.get_market_last_updated(market_name, store_id=resolved_store_id)
        if not last_updated:
            return False, None

        # Never skip if the most recent run didn't succeed — the offers table may
        # have a recent timestamp from a partial save in a failed run.
        if not self.db.get_market_last_run_succeeded(market_name, store_id=resolved_store_id):
            return False, None

        age_seconds = (datetime.now() - last_updated).total_seconds()
        threshold_seconds = self.skip_updated_within_days * 86400
        if age_seconds < threshold_seconds:
            age_label = self._format_duration(age_seconds)
            threshold_label = self._format_duration(threshold_seconds)
            return (
                True,
                (
                    f"Skipping {market_name}: last update was {age_label} ago "
                    f"for store_id={resolved_store_id} "
                    f"(< {threshold_label}, SKIP_UPDATED_WITHIN_DAYS={self.skip_updated_within_days:g})."
                ),
            )
        return False, None

    def _record_timing(
        self,
        process_name: str,
        step_name: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        zip_code: Optional[str] = None,
        store_id: Optional[str] = None,
        market_name: Optional[str] = None,
        run_type: str = "individual",
        details: Optional[str] = None,
    ):
        duration_seconds = max(0.0, (finished_at - started_at).total_seconds())
        self.db.log_process_timing(
            process_name=process_name,
            step_name=step_name,
            market_name=market_name,
            zip_code=zip_code,
            store_id=store_id,
            status=status,
            duration_seconds=duration_seconds,
            started_at=started_at,
            finished_at=finished_at,
            run_type=run_type,
            details=details,
        )

    @staticmethod
    def _collect_barcode_refs(
        market_name: str,
        offers_data: List[Dict],
    ) -> List[Tuple[str, str, str, str, str]]:
        refs: List[Tuple[str, str, str, str, str]] = []
        for offer in offers_data:
            barcode = offer.get("barcode") or offer.get("gtin")
            offer_id = offer.get("id")
            if not barcode or not offer_id:
                continue
            refs.append(
                (
                    str(barcode),
                    market_name,
                    str(offer_id),
                    offer.get("product_name") or "",
                    offer.get("brand") or "",
                )
            )
        return refs

    @staticmethod
    def _format_offers_for_db(
        market_name: str,
        offers_data: List[Dict],
    ) -> List[tuple]:
        formatted_offers: List[tuple] = []
        for offer in offers_data:
            description = offer.get("description")
            if isinstance(description, (dict, list)):
                description = json.dumps(description, ensure_ascii=False)

            formatted_offers.append(
                (
                    offer.get("id"),
                    market_name,
                    offer.get("product_name"),
                    offer.get("brand"),
                    description,
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
                    datetime.now().isoformat(),
                    offer.get("store_id"),
                    offer.get("sold_quantity"),
                    offer.get("offer_name"),
                    offer.get("offer_tag"),
                    offer.get("app_membership_required"),
                )
            )
        return formatted_offers

    def run_all(self, zip_code: str = None, limit: Optional[int] = None):
        if not zip_code:
            print("Auto-detecting your location...\n")
            detected_zip = LocationDetector.detect_user_location()
            if detected_zip:
                zip_code = LocationDetector.format_zip_code(detected_zip)
                print(f"Using detected ZIP code: {zip_code}\n")
            else:
                raise RuntimeError("Could not auto-detect location. Provide ZIP code manually.")

        # Main strategy: scrape all market catalogs, then enrich/match barcodes.
        self.run_all_departamentos(zip_code, limit=limit)

    def run_rossi_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Rossi departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Rossi", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Rossi departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = RossiDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Rossi", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Rossi", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Rossi departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Rossi departamentos run complete ---")
        # Sync barcodes into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=["Rossi"])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Rossi: known_barcodes sync warning: {exc}")
        self._record_timing(
            process_name="rossi_departamentos", step_name="scrape_and_save",
            market_name="Rossi", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_nagumo_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run Nagumo departamentos scraper then enrich barcodes via cross-market catalog."""
        should_skip, skip_msg = self._should_skip_recent_market("Nagumo", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Nagumo departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = NagumoDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Nagumo", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Nagumo", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Nagumo departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        # Cross-market barcode enrichment using extra_barcode_enrich PDP strategy
        print("\nNagumo: running cross-market barcode enrichment...")
        try:
            from markets.tier1_inline_barcodes.extra_barcode_enrich import run as _nagumo_enrich
            _nagumo_enrich(
                self.db,
                market_name="Nagumo",
                market_slug="nagumo",
                api_base="https://www.nagumo.com.br/api",
                gpa_store_id=0,
                zip_code=zip_code,
                max_calls=2000,
                delay=0.1,
                           skip_pdp_html=True,
            )
        except Exception as exc:
            print(f"Nagumo enrichment warning (non-fatal): {exc}")

        # Name-based + AI barcode inference for Nagumo offers still missing a barcode
        print("\nNagumo: running barcode inference on remaining unmatched offers...")
        if self._centralized_barcode_phase_active:
            print("Nagumo: per-market inference skipped (centralized barcode phase active).")
        else:
            try:
                self.barcode_ai_matcher.sync_known_barcodes()
                infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(target_markets=["Nagumo"])
                print(
                    f"Nagumo barcode inference: matched={infer_stats.get('matched', 0)} "
                    f"scanned={infer_stats.get('scanned', 0)} "
                    f"ai_calls={infer_stats.get('ai_calls', 0)}"
                )
            except Exception as exc:
                print(f"Nagumo barcode inference warning (non-fatal): {exc}")

        self._record_timing(
            process_name="nagumo_departamentos", step_name="scrape_and_save",
            market_name="Nagumo", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        print("\n--- Nagumo departamentos run complete ---")
        return True

    def run_nagumo_departamentos_all_only(self, zip_code: str, limit: Optional[int] = None):
        """Run Nagumo departamentos in all-catalog mode (no pmid filter)."""
        should_skip, skip_msg = self._should_skip_recent_market("Nagumo", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Nagumo ALL-CATALOG departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        old_all_catalog = os.environ.get("NAGUMO_ALL_CATALOG")
        os.environ["NAGUMO_ALL_CATALOG"] = "1"

        try:
            scraper = NagumoDepartamentosScraper()
            offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []
        finally:
            if old_all_catalog is None:
                os.environ.pop("NAGUMO_ALL_CATALOG", None)
            else:
                os.environ["NAGUMO_ALL_CATALOG"] = old_all_catalog

        formatted_offers = self._format_offers_for_db("Nagumo", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Nagumo", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Nagumo all-catalog departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Nagumo ALL-CATALOG departamentos run complete ---")
        self._record_timing(
            process_name="nagumo_departamentos_all", step_name="scrape_and_save",
            market_name="Nagumo", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_atacadao_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Atacadao departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market(MARKET_ATACADAO, zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Atacadao departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = AtacadaoDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db(MARKET_ATACADAO, offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs(MARKET_ATACADAO, offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Atacadao departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Atacadao departamentos run complete ---")
        # Sync barcodes into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=[MARKET_ATACADAO])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Atacadao: known_barcodes sync warning: {exc}")
        # Backfill barcodes on Atacadão offers that lack them using the cross-market catalog
        if self._centralized_barcode_phase_active:
            print("Atacadao: per-market barcode backfill skipped (centralized barcode phase active).")
        else:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                _atacadao_matcher = BarcodeAIMatcher()
                _atacadao_matcher.sync_known_barcodes()  # ensure catalog includes all trusted markets
                infer_stats = _atacadao_matcher.infer_missing_barcodes(target_markets=[MARKET_ATACADAO])
                print(
                    f"Atacadao barcode backfill: matched={infer_stats.get('matched', 0)} "
                    f"scanned={infer_stats.get('scanned', 0)}"
                )
            except Exception as exc:
                print(f"Atacadao: barcode backfill warning: {exc}")
        self._record_timing(
            process_name="atacadao_departamentos", step_name="scrape_and_save",
            market_name=MARKET_ATACADAO, zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_tenda_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Tenda Atacado departamentos scraper and persist directly in DB."""
        market_name = "Tenda Atacado"
        should_skip, skip_msg = self._should_skip_recent_market(market_name, zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Tenda Atacado departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = TendaAtacadoDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db(market_name, offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs(market_name, offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            f"Tenda Atacado departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Tenda Atacado departamentos run complete ---")

        # Tier 1: push inline barcodes into the shared catalog so other markets benefit
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=[market_name])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Tenda Atacado: known_barcodes sync warning: {exc}")

        self._record_timing(
            process_name="tenda_departamentos", step_name="scrape_and_save",
            market_name=market_name, zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def _run_higas_api_enrich_loop(
        self,
        zip_code: str,
        store_id: Optional[str] = None,
        max_rounds: int = 200,
    ) -> None:
        """Loop run_higas_barcode_enrich in safe batches (~650 calls each) until all
        pending barcodes are exhausted.  Each batch saves to DB before sleeping,
        so progress is never lost even if the process is interrupted.
        """
        _max_calls = int(str(config.HIGAS_ENRICHMENT_MAX_CALLS))
        _base_delay = float(str(config.HIGAS_ENRICHMENT_BASE_DELAY))
        _jitter = float(str(config.HIGAS_ENRICHMENT_JITTER_SECONDS))
        _burst_size = int(str(config.HIGAS_ENRICHMENT_BURST_SIZE))
        _burst_cooldown = int(str(config.HIGAS_ENRICHMENT_BURST_COOLDOWN))
        _flush_every = int(str(config.HIGAS_ENRICHMENT_FLUSH_EVERY))
        _flush_cooldown = int(str(config.HIGAS_ENRICHMENT_FLUSH_COOLDOWN))
        _between_runs = int(
            os.getenv(
                "HIGAS_ENRICHMENT_BETWEEN_RUNS_COOLDOWN_SECONDS",
                str(config.HIGAS_ENRICHMENT_BETWEEN_RUNS_COOLDOWN_SECONDS),
            )
        )

        total_calls = 0
        total_hits = 0
        total_saved = 0

        for round_num in range(1, max_rounds + 1):
            print(
                f"\nHigas API enrich round {round_num}/{max_rounds} "
                f"(budget={_max_calls} calls, {_between_runs}s cooldown between rounds)"
            )
            result = run_higas_barcode_enrich(
                store_id=store_id,
                zip_code=zip_code,
                max_calls=_max_calls,
                base_delay_seconds=_base_delay,
                jitter_seconds=_jitter,
                burst_size=_burst_size,
                burst_cooldown_seconds=_burst_cooldown,
                flush_every_calls=_flush_every,
                flush_cooldown_seconds=_flush_cooldown,
            )
            total_calls += result.get("calls", 0)
            total_hits += result.get("hits", 0)
            total_saved += result.get("offers_saved", 0)
            remaining = result.get("remaining_after_run", 0)

            print(
                f"Round {round_num}: calls={result.get('calls', 0)} "
                f"hits={result.get('hits', 0)} "
                f"offers_saved={result.get('offers_saved', 0)} "
                f"remaining={remaining}"
            )

            if result.get("completed") or remaining == 0:
                print("Higas API enrich: all barcodes processed.")
                break
            if result.get("processed_in_run", 0) <= 0:
                print("Higas API enrich: no progress this round, stopping.")
                break
            print(f"Cooling {_between_runs}s before next round...")
            time.sleep(_between_runs)

        print(
            f"Higas API enrich complete: rounds={round_num} "
            f"total_calls={total_calls} hits={total_hits} offers_saved={total_saved}"
        )

    def run_higas_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run Higas departamentos scraper then enrich barcodes via Instabuy cross-market API.

        Strategy:
          1. Scrape Higas offers (API returns no barcodes) — skipped if recently done
          2. higas_barcode_enrich: takes every barcode already in product_catalog
             (from ALL trusted markets) and searches it in the Higas Instabuy API
             — always runs regardless of scrape skip, so new catalog barcodes are enriched
          3. AI barcode inference for offers still missing barcode after enrichment
          4. Deduplicate any barcode collisions in the Higas offers table
        """
        should_skip, skip_msg = self._should_skip_recent_market("Higas", zip_code)
        started_at = datetime.now()
        used_store_id = "N/A"

        if should_skip:
            print(skip_msg)
            print("Higas: scrape skipped but still running barcode enrichment for new catalog entries...")
        else:
            print(f"--- Starting Higas departamentos run for ZIP Code: {zip_code} ---\n")

            scraper = HigasDepartamentosScraper()
            offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

            formatted_offers = self._format_offers_for_db("Higas", offers_data)
            if formatted_offers:
                self.db.save_offers(formatted_offers)

            barcode_refs = self._collect_barcode_refs("Higas", offers_data)
            if barcode_refs:
                self.db.save_barcode_references_bulk(barcode_refs)

            used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
            print(
                "Higas departamentos summary: "
                f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
            )

            self._record_timing(
                process_name="higas_departamentos", step_name="scrape_and_save",
                market_name="Higas", zip_code=zip_code, store_id=used_store_id, status="success",
                started_at=started_at, finished_at=datetime.now(),
                details=f"offers={len(offers_data)}",
            )

        # Cross-market barcode enrichment via Instabuy API.
        # Skipped in test mode (SKIP_BARCODE_INFERENCE=1).
        if self.skip_barcode_inference:
            print("\nHigas: barcode enrichment skipped (SKIP_BARCODE_INFERENCE=1).")
        else:
            print("\nHigas: running cross-market barcode enrichment via Instabuy API (all batches)...")
            try:
                self._run_higas_api_enrich_loop(
                    zip_code=zip_code,
                    store_id=used_store_id if used_store_id != "N/A" else None,
                )
                _deduplicate_higas_offers(self.db)
            except Exception as exc:
                print(f"Higas enrichment warning (non-fatal): {exc}")

        # Name-based + AI barcode inference for Higas offers still missing a barcode
        # after enrichment (products not present in any other market's catalog).
        print("\nHigas: running barcode inference on remaining unmatched offers...")
        if self._centralized_barcode_phase_active:
            print("Higas: per-market inference skipped (centralized barcode phase active).")
        else:
            try:
                self.barcode_ai_matcher.sync_known_barcodes()
                infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(target_markets=["Higas"])
                print(
                    f"Higas barcode inference: matched={infer_stats.get('matched', 0)} "
                    f"scanned={infer_stats.get('scanned', 0)} "
                    f"ai_calls={infer_stats.get('ai_calls', 0)}"
                )
            except Exception as exc:
                print(f"Higas barcode inference warning (non-fatal): {exc}")

        print("\n--- Higas departamentos run complete ---")
        return True

    def run_higas_enrich_only(self, zip_code: Optional[str] = None, limit: Optional[int] = None):
        """Run the full Higas barcode recovery pipeline without re-scraping offers.

        Order matters:
          1. Refresh trusted known_barcodes snapshot from source markets
          2. Query Higas Instabuy search_barcode API in repeated safe batches
          3. Deduplicate barcode collisions created by enrichment
          4. Run remaining heuristic/AI inference for still-unmatched offers
        """
        print("--- Starting Higas barcode enrichment run ---\n")

        resolved_zip = LocationDetector.format_zip_code(
            zip_code or str(os.getenv("SCRAPE_ZIP_CODE") or "08032-230")
        )

        trusted_snapshot_markets = list(self.barcode_ai_matcher.TRUSTED_SOURCE_MARKETS)
        snapshot_before_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
        snapshot_after_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        source_catalog_changed = snapshot_before_sync != snapshot_after_sync
        self._print_catalog_sync_summary(catalog_stats)

        print("Higas: running Instabuy barcode API enrichment loop...")
        try:
            self._run_higas_api_enrich_loop(zip_code=resolved_zip)
            _deduplicate_higas_offers(self.db)
        except Exception as exc:
            print(f"Higas Instabuy enrichment warning (non-fatal): {exc}")

        infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(
            target_markets=["Higas"],
            limit=self._normalize_limit(limit),
        )

        if infer_stats["ai_enabled"]:
            print(
                "AI barcode enrichment (Higas): "
                f"{infer_stats['matched']} inferred matches from {infer_stats['scanned']} barcode-missing offers "
                f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped)"
            )
        else:
            print(
                "Barcode inference (Higas): "
                f"{infer_stats['matched']} matches from {infer_stats['scanned']} barcode-missing offers "
                f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped) "
                "(heuristic only; set ENABLE_AI_BARCODE_MATCH=1 and HF_TOKEN or OPENROUTER_API_KEY for AI-assisted matching)"
            )

        if source_catalog_changed:
            print("Barcode source catalog changed in this run: Higas barcode-missing offers were reconsidered.")
        else:
            print("Barcode source catalog unchanged: Higas barcode-missing offers were considered.")

        print("\n--- Higas barcode enrichment run complete ---")

    # ------------------------------------------------------------------
    # Standalone barcode enrichment runners (no re-scrape)
    # ------------------------------------------------------------------

    def _run_gpa_enrich_only(
        self,
        market_name: str,
        market_slug: str,
        api_base: str,
        gpa_store_id: int,
        zip_code: Optional[str] = None,
        skip_pdp_html: bool = False,
    ) -> None:
        """Run barcode enrichment for a GPA-style market without re-scraping."""
        print(f"--- Starting {market_name} barcode enrichment run ---\n")
        resolved_zip = LocationDetector.format_zip_code(
            zip_code or str(os.getenv("SCRAPE_ZIP_CODE") or "08032-230")
        )
        catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
        self._print_catalog_sync_summary(catalog_stats)
        try:
            run_extra_barcode_enrich(
                self.db,
                market_name=market_name,
                market_slug=market_slug,
                api_base=api_base,
                gpa_store_id=gpa_store_id,
                zip_code=resolved_zip,
                max_calls=10000,
                delay=0.08,
                skip_pdp_html=skip_pdp_html,
            )
        except Exception as exc:
            print(f"{market_name} enrichment warning (non-fatal): {exc}")
        infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(target_markets=[market_name])
        print(
            f"{market_name} barcode inference: "
            f"matched={infer_stats.get('matched', 0)} "
            f"scanned={infer_stats.get('scanned', 0)} "
            f"ai_calls={infer_stats.get('ai_calls', 0)}"
        )
        print(f"\n--- {market_name} barcode enrichment run complete ---")

    def run_extra_enrich_only(self, zip_code: Optional[str] = None, limit: Optional[int] = None):
        """Run Extra barcode enrichment (PDP + API) without re-scraping."""
        from markets.tier1_inline_barcodes.market_scrap_extra_departamentos import ExtraDepartamentosScraper
        self._run_gpa_enrich_only(
            market_name="Extra",
            market_slug="extra",
            api_base="https://api.vendas.gpa.digital/ex",
            gpa_store_id=ExtraDepartamentosScraper.GPA_STORE_ID,
            zip_code=zip_code,
        )

    def run_paodeacucar_enrich_only(self, zip_code: Optional[str] = None, limit: Optional[int] = None):
        """Run Pão de Açúcar barcode enrichment (PDP + API) without re-scraping."""
        from markets.tier1_inline_barcodes.market_scrap_paodeacucar_departamentos import PaoDeAcucarDepartamentosScraper
        self._run_gpa_enrich_only(
            market_name="Pão de Açúcar",
            market_slug="paodeacucar",
            api_base="https://api.vendas.gpa.digital/pa",
            gpa_store_id=PaoDeAcucarDepartamentosScraper.GPA_STORE_ID,
            zip_code=zip_code,
        )

    def run_oba_enrich_only(self, zip_code: Optional[str] = None, limit: Optional[int] = None):
        """Run Oba Hortifruti barcode enrichment without re-scraping."""
        self._run_gpa_enrich_only(
            market_name="Oba Hortifruti",
            market_slug="oba",
            api_base="https://www.obahortifruti.com.br/api",
            gpa_store_id=0,
            zip_code=zip_code,
        )

    def run_nagumo_enrich_only(self, zip_code: Optional[str] = None, limit: Optional[int] = None):
        """Run Nagumo barcode enrichment without re-scraping."""
        self._run_gpa_enrich_only(
            market_name="Nagumo",
            market_slug="nagumo",
            api_base="https://www.nagumo.com.br/api",
            gpa_store_id=0,
            zip_code=zip_code,
            skip_pdp_html=True,
        )

    def run_embedding_model_inference_only(self, limit: Optional[int] = None):
        """Run the local embedding model against barcode-missing offers and persist a per-run audit."""
        print("--- Starting embedding-model barcode inference run ---\n")

        trusted_snapshot_markets = list(self.barcode_ai_matcher.TRUSTED_SOURCE_MARKETS)
        snapshot_before_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
        snapshot_after_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        source_catalog_changed = snapshot_before_sync != snapshot_after_sync
        self._print_catalog_sync_summary(catalog_stats)

        infer_stats = self.barcode_ai_matcher.run_embedding_model_inference(limit=self._normalize_limit(limit))
        print(
            "Embedding model inference: "
            f"{infer_stats['matched']} applied matches from {infer_stats['scanned']} barcode-missing offers "
            f"(embedding_calls={infer_stats.get('embedding_calls', 0)} "
            f"embedding_matched={infer_stats.get('embedding_matched', 0)} "
            f"skipped_unchanged={infer_stats.get('skipped_unchanged', 0)})"
        )
        print(
            "Embedding audit saved: "
            f"run_id={infer_stats.get('audit_run_id')} "
            f"rows={infer_stats.get('audit_rows_written', 0)} "
            f"table={infer_stats.get('audit_table')}"
        )

        if source_catalog_changed:
            print("Barcode source catalog changed in this run: all barcode-missing offers were reconsidered.")
        else:
            print("Barcode source catalog unchanged: all barcode-missing offers were considered.")

        print("\n--- Embedding-model barcode inference run complete ---")

    def run_xsupermercados_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only X Supermercados departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("XSupermercados", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting X Supermercados departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = XSupermercadosDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("XSupermercados", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("XSupermercados", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "X Supermercados departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- X Supermercados departamentos run complete ---")
        self._record_timing(
            process_name="xsupermercados_departamentos", step_name="scrape_and_save",
            market_name="XSupermercados", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_sonda_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Sonda Delivery departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Sonda Delivery", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Sonda Delivery departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = SondaDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Sonda Delivery", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Sonda Delivery", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Sonda Delivery departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Sonda Delivery departamentos run complete ---")
        # Sync barcodes into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=["Sonda Delivery"])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Sonda Delivery: known_barcodes sync warning: {exc}")
        self._record_timing(
            process_name="sonda_departamentos", step_name="scrape_and_save",
            market_name="Sonda Delivery", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_swift_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Swift departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Swift", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Swift departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = SwiftDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Swift", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Swift", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Swift departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Swift departamentos run complete ---")
        # Sync barcodes into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=["Swift"])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Swift: known_barcodes sync warning: {exc}")
        self._record_timing(
            process_name="swift_departamentos", step_name="scrape_and_save",
            market_name="Swift", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_barbosa_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Barbosa Supermercados departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Barbosa", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Barbosa departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = BarbosaDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Barbosa", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Barbosa", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else "N/A"
        print(
            "Barbosa departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )
        print("\n--- Barbosa departamentos run complete ---")
        # Sync barcodes into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                BarcodeAIMatcher().sync_known_barcodes(source_markets=["Barbosa"])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Barbosa: known_barcodes sync warning: {exc}")
        self._record_timing(
            process_name="barbosa_departamentos", step_name="scrape_and_save",
            market_name="Barbosa", zip_code=zip_code, store_id=used_store_id, status="success",
            started_at=started_at, finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        return True

    def run_carrefour_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Carrefour Mercado departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Carrefour", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Carrefour departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = CarrefourDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Carrefour", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Carrefour", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else CarrefourDepartamentosScraper.STORE_ID
        print(
            "Carrefour departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        # Write store_mappings and process timing
        metadata = getattr(scraper, "_resolved_store_metadata", None) or {}
        self.db.cache_store_id(
            zip_code, "Carrefour", used_store_id,
            store_name=metadata.get("store_name"),
            store_address=metadata.get("store_address"),
            store_city=metadata.get("store_city"),
            store_state=metadata.get("store_state"),
            latitude=metadata.get("latitude"),
            longitude=metadata.get("longitude"),
        )
        self._record_timing(
            process_name="carrefour_departamentos",
            step_name="scrape_and_save",
            market_name="Carrefour",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        # Sync barcodes found by Carrefour scraper into known_barcodes catalog
        if barcode_refs:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                matcher = BarcodeAIMatcher()
                matcher.sync_known_barcodes(source_markets=["Carrefour"])
                from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
                _sync_product_catalog(self.db)
            except Exception as exc:
                print(f"Carrefour: known_barcodes sync warning: {exc}")
        # Backfill barcodes on Carrefour offers that lack them using the cross-market catalog
        if self._centralized_barcode_phase_active:
            print("Carrefour: per-market barcode backfill skipped (centralized barcode phase active).")
        else:
            try:
                from db.barcode_ai_matcher import BarcodeAIMatcher
                infer_stats = BarcodeAIMatcher().infer_missing_barcodes(target_markets=["Carrefour"])
                print(
                    f"Carrefour barcode backfill: matched={infer_stats.get('matched', 0)} "
                    f"scanned={infer_stats.get('scanned', 0)}"
                )
            except Exception as exc:
                print(f"Carrefour: barcode backfill warning: {exc}")
        print("\n--- Carrefour departamentos run complete ---")
        return True

    def run_oba_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Oba Hortifruti departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Oba Hortifruti", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Oba Hortifruti departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = ObaDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Oba Hortifruti", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Oba Hortifruti", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else ObaDepartamentosScraper.STORE_ID
        print(
            "Oba Hortifruti departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        # Bug 3 fix: write store_mappings entry
        self.db.cache_store_id(zip_code, "Oba Hortifruti", used_store_id, store_name="Oba Hortifruti")

        # Bug 4 fix: record process timing
        self._record_timing(
            process_name="oba_departamentos",
            step_name="scrape_and_save",
            market_name="Oba Hortifruti",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )

        # Enrich barcodes and sync to known_barcodes (always run, not just if barcode_refs)
        run_extra_barcode_enrich(
            self.db,
            market_name="Oba Hortifruti",
            market_slug="oba",
            api_base="https://www.obahortifruti.com.br/api",
            gpa_store_id=0,
            zip_code=zip_code,
            max_calls=5000,
            delay=0.08,
        )
        print("\n--- Oba Hortifruti departamentos run complete ---")
        return True

    def run_samsclub_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Sam's Club Brasil departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Sam's Club", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Sam's Club departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = SamsClubDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Sam's Club", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Sam's Club", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else SamsClubDepartamentosScraper.STORE_ID
        print(
            "Sam's Club departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        self.db.cache_store_id(zip_code, "Sam's Club", used_store_id, store_name="Sam's Club")

        self._record_timing(
            process_name="samsclub_departamentos",
            step_name="scrape_and_save",
            market_name="Sam's Club",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        print("\n--- Sam's Club departamentos run complete ---")
        return True

    def run_davo_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Davo departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Davo", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Davo departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = DavoDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Davo", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Davo", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else DavoDepartamentosScraper.STORE_ID
        print(
            "Davo departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        self.db.cache_store_id(zip_code, "Davo", used_store_id, store_name="Davo")

        self._record_timing(
            process_name="davo_departamentos",
            step_name="scrape_and_save",
            market_name="Davo",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        print("\n--- Davo departamentos run complete ---")
        return True

    def run_giga_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Giga departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Giga", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Giga departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = GigaDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Giga", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Giga", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else GigaDepartamentosScraper.STORE_ID
        print(
            "Giga departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        self.db.cache_store_id(zip_code, "Giga", used_store_id, store_name="Giga")

        self._record_timing(
            process_name="giga_departamentos",
            step_name="scrape_and_save",
            market_name="Giga",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )
        print("\n--- Giga departamentos run complete ---")
        return True

    def run_extra_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Extra Mercado departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Extra", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Extra Mercado departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = ExtraDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Extra", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Extra", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else ExtraDepartamentosScraper.STORE_ID
        print(
            "Extra Mercado departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        # Bug 3 fix: write store_mappings entry
        self.db.cache_store_id(zip_code, "Extra", used_store_id, store_name="Extra Mercado")

        # Bug 4 fix: record process timing
        self._record_timing(
            process_name="extra_departamentos",
            step_name="scrape_and_save",
            market_name="Extra",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )

        # Barcode enrichment: resumable, skips already-fetched products
        run_extra_barcode_enrich(
            self.db,
            market_name="Extra",
            market_slug="extra",
            api_base="https://api.vendas.gpa.digital/ex",
            gpa_store_id=ExtraDepartamentosScraper.GPA_STORE_ID,
            zip_code=zip_code,
            max_calls=10000,
            delay=0.08,
        )
        print("\n--- Extra Mercado departamentos run complete ---")
        return True

    def run_paodeacucar_departamentos_only(self, zip_code: str, limit: Optional[int] = None):
        """Run only Pão de Açúcar departamentos scraper and persist directly in DB."""
        should_skip, skip_msg = self._should_skip_recent_market("Pão de Açúcar", zip_code)
        if should_skip:
            print(skip_msg)
            return False

        print(f"--- Starting Pão de Açúcar departamentos run for ZIP Code: {zip_code} ---\n")
        started_at = datetime.now()

        scraper = PaoDeAcucarDepartamentosScraper()
        offers_data = scraper.fetch_offers(zip_code, limit=self._normalize_limit(limit)) or []

        formatted_offers = self._format_offers_for_db("Pão de Açúcar", offers_data)
        if formatted_offers:
            self.db.save_offers(formatted_offers)

        barcode_refs = self._collect_barcode_refs("Pão de Açúcar", offers_data)
        if barcode_refs:
            self.db.save_barcode_references_bulk(barcode_refs)

        used_store_id = offers_data[0].get("store_id") if offers_data else PaoDeAcucarDepartamentosScraper.STORE_ID
        print(
            "Pão de Açúcar departamentos summary: "
            f"store_id={used_store_id} offers={len(offers_data)} barcode_refs={len(barcode_refs)}"
        )

        # Bug 3 fix: write store_mappings entry
        self.db.cache_store_id(zip_code, "Pão de Açúcar", used_store_id, store_name="Pão de Açúcar")

        # Bug 4 fix: record process timing
        self._record_timing(
            process_name="paodeacucar_departamentos",
            step_name="scrape_and_save",
            market_name="Pão de Açúcar",
            zip_code=zip_code,
            store_id=used_store_id,
            status="success",
            started_at=started_at,
            finished_at=datetime.now(),
            details=f"offers={len(offers_data)}",
        )

        # Barcode enrichment: resumable, skips already-fetched products
        run_extra_barcode_enrich(
            self.db,
            market_name="Pão de Açúcar",
            market_slug="paodeacucar",
            api_base="https://api.vendas.gpa.digital/pa",
            gpa_store_id=PaoDeAcucarDepartamentosScraper.GPA_STORE_ID,
            zip_code=zip_code,
            max_calls=10000,
            delay=0.08,
        )
        print("\n--- Pão de Açúcar departamentos run complete ---")
        return True

    def run_barcode_pipeline_only(
        self,
        zip_code: Optional[str] = None,
        target_markets: Optional[List[str]] = None,
        limit: Optional[int] = None,
        force: bool = False,
    ):
        """Run only the barcode sync + inference pipeline — no scraping.

        Designed for parallel-machine deployments where each machine scrapes
        one market with SKIP_BARCODE_INFERENCE=1, then a single coordinator
        machine runs this to process all markets' barcode-missing offers at once.

        Usage:
            python main.py barcode_pipeline_only
            python main.py barcode_pipeline_only 500   # limit infer candidates
            python main.py barcode_pipeline_only force  # ignore cached unmatched state
        """
        print("--- Starting barcode pipeline only run ---\n")
        if force:
            print("FORCE MODE: ignoring cached unmatched state — all offers will be re-evaluated")
        started_at = datetime.now()

        trusted_snapshot_markets = list(self.barcode_ai_matcher.TRUSTED_SOURCE_MARKETS)

        # Step 1: sync known_barcodes from all trusted source markets
        snapshot_before = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
        snapshot_after = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
        source_catalog_changed = snapshot_before != snapshot_after
        self._print_catalog_sync_summary(catalog_stats, label="Barcode sync")

        # Step 2: infer missing barcodes across all (or specified) markets
        print("Barcode inference: starting missing-barcode matching...")
        infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(
            target_markets=target_markets or None,
            limit=self._normalize_limit(limit),
            respect_cached_unmatched=not force,
        )

        if infer_stats["ai_enabled"]:
            print(
                "AI barcode enrichment: "
                f"{infer_stats['matched']} inferred matches from {infer_stats['scanned']} barcode-missing offers "
                f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped)"
            )
        else:
            print(
                "Barcode inference (heuristic only): "
                f"{infer_stats['matched']} matches from {infer_stats['scanned']} barcode-missing offers "
                f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped) "
                "(set ENABLE_AI_BARCODE_MATCH=1 + HF_TOKEN or OPENROUTER_API_KEY for AI-assisted matching)"
            )

        if source_catalog_changed:
            print("Barcode source catalog changed — all barcode-missing offers were reconsidered.")
        else:
            print("Barcode source catalog unchanged — offers were considered against existing catalog.")

        # Step 3: optimize DB
        optimize_stats = self.db.optimize_database()
        if optimize_stats.get("db_path") == "postgres":
            print("Database optimize: ANALYZE completed (postgres).")
        else:
            print(
                "Database optimize: "
                f"saved {optimize_stats['bytes_saved']} bytes in {optimize_stats['db_path']}"
            )

        finished_at = datetime.now()
        self._record_timing(
            process_name="barcode_pipeline_only",
            run_type="all",
            step_name="barcode_pipeline_only",
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            zip_code=zip_code,
            details=f"matched={infer_stats['matched']} scanned={infer_stats['scanned']} catalog_changed={source_catalog_changed}",
        )

        print("\n--- Barcode pipeline only run complete ---")

    def _run_catalog_sync_step(self, zip_code: str, limit: Optional[int] = None) -> bool:
        """
        Mid-pipeline catalog sync — runs between Tier 1/2 and Tier 3 scrapers.

        Flushes all barcodes collected so far into product_catalog so that
        Atacadão, Nagumo, and Higas can use the fullest possible catalog
        for cross-market barcode lookup and Higas API enrichment.
        Also deduplicates product_catalog entries.
        """
        print("\n--- Catalog sync (mid-pipeline) ---")
        try:
            stats = self.barcode_ai_matcher.sync_known_barcodes()
            if stats.get("skipped"):
                interval_hours = float(stats.get("min_interval_hours", 0.0) or 0.0)
                remaining_minutes = int(stats.get("remaining_minutes", 0) or 0)
                print(
                    f"  Catalog sync: skipped (recent sync within {interval_hours:g}h window, "
                    f"~{remaining_minutes}m remaining)"
                )
            else:
                print(
                    f"  Catalog sync: {stats['upserted']} barcodes upserted "
                    f"from {stats['source_rows']} trusted rows"
                )
            from markets.tier1_inline_barcodes.extra_barcode_enrich import _sync_product_catalog
            _sync_product_catalog(self.db)
        except Exception as exc:
            print(f"  Catalog sync warning: {exc}")
        print("--- Catalog sync complete ---\n")
        return True

    def run_all_departamentos(
        self,
        zip_code: str,
        limit: Optional[int] = None,
        parallel: bool = False,
        parallel_workers: Optional[int] = None,
    ):
        """Run all *_departamentos scrapers in sequence and then execute enrichment/optimize."""
        run_started = datetime.now()
        print(f"--- Starting ALL departamentos run for ZIP Code: {zip_code} ---\n")
        print(
            "Recent-update skip window: "
            f"{self.skip_updated_within_days:g} day(s) (set SKIP_UPDATED_WITHIN_DAYS in .env to change)."
        )
        normalized_limit = self._normalize_limit(limit)
        if normalized_limit is not None:
            print(f"Departamentos limit active: {normalized_limit} products per market")
        if parallel:
            workers_msg = parallel_workers if parallel_workers else "auto"
            print(f"Parallel mode: enabled (workers={workers_msg}, set --parallel-workers or SCRAPE_PARALLEL_WORKERS to tune).")
        if self.skip_barcode_inference:
            print("Test mode: SKIP_BARCODE_INFERENCE=1 (barcode sync/inference will be skipped).")

        runners = self._departamentos_runners()

        results: List[Tuple[str, str]] = []
        # Track per-step timing for the final table
        step_timings: List[Tuple[str, float, str]] = []  # (label, seconds, status)

        # Prepare app_offers sync: create table + load store CEPs once before the loop.
        _app_offers_ceps = {}
        try:
            from sync_app_offers import prepare_sync as _prepare_app_offers_sync
            _app_offers_ceps = _prepare_app_offers_sync()
        except Exception as _prep_exc:
            print(f"app_offers sync setup warning (non-fatal): {_prep_exc}")

        # Centralize barcode-heavy inference to one end-of-pipeline phase.
        previous_centralized = self._centralized_barcode_phase_active
        self._centralized_barcode_phase_active = True
        try:
            if parallel:
                pre_sync_batch: List[Tuple[str, str]] = []
                post_sync_batch: List[Tuple[str, str]] = []
                sync_runner = None
                seen_sync_marker = False
                for market_name, runner in runners:
                    if market_name == "_catalog_sync":
                        sync_runner = runner
                        seen_sync_marker = True
                        continue
                    cli_mode = self._runner_to_cli_mode(runner)
                    if not cli_mode:
                        continue
                    if seen_sync_marker:
                        post_sync_batch.append((market_name, cli_mode))
                    else:
                        pre_sync_batch.append((market_name, cli_mode))

                batch_results, batch_timings = self._run_parallel_market_batch(
                    markets_batch=pre_sync_batch,
                    zip_code=zip_code,
                    limit=normalized_limit,
                    app_offers_ceps=_app_offers_ceps,
                    requested_workers=parallel_workers,
                )
                results.extend(batch_results)
                step_timings.extend(batch_timings)

                if sync_runner is not None:
                    sync_started = datetime.now()
                    try:
                        sync_runner(zip_code, limit=normalized_limit)
                        sync_elapsed = (datetime.now() - sync_started).total_seconds()
                        step_timings.append(("Catalog sync", sync_elapsed, "ok"))
                    except Exception as exc:
                        sync_elapsed = (datetime.now() - sync_started).total_seconds()
                        print(f"Catalog sync step failed (non-fatal): {exc}")
                        step_timings.append(("Catalog sync", sync_elapsed, "failed"))

                batch_results, batch_timings = self._run_parallel_market_batch(
                    markets_batch=post_sync_batch,
                    zip_code=zip_code,
                    limit=normalized_limit,
                    app_offers_ceps=_app_offers_ceps,
                    requested_workers=parallel_workers,
                )
                results.extend(batch_results)
                step_timings.extend(batch_timings)
            else:
                for market_name, runner in runners:
                # _catalog_sync is a mid-pipeline step, not a market scraper
                    is_sync_step = market_name == "_catalog_sync"
                    step_started = datetime.now()
                    try:
                        executed = runner(zip_code, limit=normalized_limit)
                        step_finished = datetime.now()
                        elapsed = (step_finished - step_started).total_seconds()
                        if is_sync_step:
                            step_timings.append(("Catalog sync", elapsed, "ok"))
                            continue  # don't add to summary or timing DB record
                        if executed:
                            results.append((market_name, "Success"))
                            step_timings.append((market_name, elapsed, "ok"))
                            self._record_timing(
                                process_name="all_departamentos",
                                run_type="all",
                                step_name=f"{market_name} departamentos",
                                market_name=market_name,
                                status="success",
                                started_at=step_started,
                                finished_at=step_finished,
                                zip_code=zip_code,
                            )
                            try:
                                from sync_app_offers import sync_one_market as _sync_one_market
                                _market_url = self.db._get_market_database_url(market_name)
                                _n = _sync_one_market(_market_url, _app_offers_ceps)
                                print(f"  app_offers: {market_name} -> {_n:,} rows upserted")
                            except Exception as _msync_exc:
                                print(f"  app_offers sync warning ({market_name}): {_msync_exc}")
                        else:
                            elapsed = (step_finished - step_started).total_seconds()
                            results.append((market_name, "Skipped (recent update)"))
                            step_timings.append((market_name, elapsed, "skipped"))
                            self._record_timing(
                                process_name="all_departamentos",
                                run_type="all",
                                step_name=f"{market_name} departamentos",
                                market_name=market_name,
                                status="skipped",
                                started_at=step_started,
                                finished_at=step_finished,
                                zip_code=zip_code,
                                details=f"Skipped because last update is newer than {self.skip_updated_within_days:g} day(s).",
                            )
                    except Exception as exc:
                        elapsed = (datetime.now() - step_started).total_seconds()
                        if is_sync_step:
                            print(f"Catalog sync step failed (non-fatal): {exc}")
                            step_timings.append(("Catalog sync", elapsed, "failed"))
                            continue
                        print(f"{market_name} departamentos failed: {exc}")
                        results.append((market_name, f"Failed: {exc})"))
                        step_timings.append((market_name, elapsed, "failed"))
                        self._record_timing(
                            process_name="all_departamentos",
                            step_name=f"{market_name} departamentos",
                            market_name=market_name,
                            status="failed",
                            started_at=step_started,
                            finished_at=datetime.now(),
                            zip_code=zip_code,
                            details=str(exc),
                        )
        finally:
            self._centralized_barcode_phase_active = previous_centralized

        print("\n" + "=" * 65)
        print("DEPARTAMENTOS SUMMARY")
        print("=" * 65)
        print(f"  {'Market':<22} {'Status':<12} {'Time':>10}")
        print("  " + "-" * 47)
        for (market_name, status), (_, elapsed, _) in zip(
            results,
            [t for t in step_timings if t[0] != "Catalog sync"],
        ):
            icon = "✓" if status == "Success" else ("↷" if "Skipped" in status else "✗")
            print(f"  {icon} {market_name:<21} {status:<12} {self._format_duration(elapsed):>10}")

        # Print catalog sync time if it ran
        cat_syncs = [t for t in step_timings if t[0] == "Catalog sync"]
        if cat_syncs:
            print(f"  {'─'*47}")
            for _, elapsed, st in cat_syncs:
                print(f"  ↺ {'Catalog sync':<21} {'ok' if st=='ok' else st:<12} {self._format_duration(elapsed):>10}")

        barcode_started = datetime.now()
        if self.skip_barcode_inference:
            print("Barcode sync + inference skipped by SKIP_BARCODE_INFERENCE=1.")
            barcode_status = "skipped"
            infer_stats = {}
        else:
            trusted_snapshot_markets = list(self.barcode_ai_matcher.TRUSTED_SOURCE_MARKETS)
            snapshot_before_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
            catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
            snapshot_after_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
            source_catalog_changed = snapshot_before_sync != snapshot_after_sync
            self._print_catalog_sync_summary(catalog_stats)

            infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(target_offer_ids=None)
            if infer_stats["ai_enabled"]:
                print(
                    "AI barcode enrichment: "
                    f"{infer_stats['matched']} inferred matches from {infer_stats['scanned']} barcode-missing offers "
                    f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                    f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped)"
                )
            else:
                print(
                    "Barcode inference: "
                    f"{infer_stats['matched']} matches from {infer_stats['scanned']} barcode-missing offers "
                    f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                    f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped) "
                    "(heuristic only; set ENABLE_AI_BARCODE_MATCH=1 and HF_TOKEN or OPENROUTER_API_KEY for AI-assisted matching)"
                )

            if source_catalog_changed:
                print("Barcode source catalog changed in this run: all barcode-missing target offers were reconsidered.")
            else:
                print("Barcode source catalog unchanged: all barcode-missing offers were considered.")
            barcode_status = "success"

        barcode_finished = datetime.now()
        barcode_elapsed = (barcode_finished - barcode_started).total_seconds()
        self._record_timing(
            process_name="barcode_sync_inference",
            step_name="barcode_sync_inference",
            market_name=None,
            zip_code=zip_code,
            status=barcode_status,
            started_at=barcode_started,
            finished_at=barcode_finished,
            run_type="all",
            details=(
                f"matched={infer_stats.get('matched', 0)} "
                f"scanned={infer_stats.get('scanned', 0)} "
                f"ai_calls={infer_stats.get('ai_calls', 0)}"
            ),
        )

        optimize_started = datetime.now()
        optimize_stats = self.db.optimize_database()
        optimize_elapsed = (datetime.now() - optimize_started).total_seconds()
        optimize_finished = datetime.now()
        self._record_timing(
            process_name="db_optimize",
            step_name="db_optimize",
            market_name=None,
            zip_code=zip_code,
            status="success",
            started_at=optimize_started,
            finished_at=optimize_finished,
            run_type="all",
            details=f"bytes_saved={optimize_stats.get('bytes_saved', 0)}",
        )
        print(
            "Database optimize: "
            f"saved {optimize_stats['bytes_saved']} bytes in {optimize_stats['db_path']}"
        )

        # Storage controller — archive old rows to Parquet/GitHub if DB nears 500 MB limit
        storage_started = datetime.now()
        try:
            run_storage_controller(self.db)
        except Exception as _sc_exc:
            print(f"Storage controller warning (non-fatal): {_sc_exc}")
        storage_finished = datetime.now()
        storage_elapsed = (storage_finished - storage_started).total_seconds()
        self._record_timing(
            process_name="storage_controller",
            step_name="storage_controller",
            market_name=None,
            zip_code=zip_code,
            status="success",
            started_at=storage_started,
            finished_at=storage_finished,
            run_type="all",
        )

        # ── Sync processed offers to app_offers table in Supabase manager2 ──
        sync_started = datetime.now()
        try:
            from sync_app_offers import run_sync as _run_app_offers_sync
            print("\nSyncing app_offers to Supabase manager2...")
            sync_result = _run_app_offers_sync(truncate=True)
            sync_elapsed = (datetime.now() - sync_started).total_seconds()
            step_timings.append(("app_offers sync", sync_elapsed, "ok"))
            self._record_timing(
                process_name="app_offers_sync",
                step_name="app_offers_sync",
                market_name=None,
                zip_code=zip_code,
                status="success",
                started_at=sync_started,
                finished_at=datetime.now(),
                run_type="all",
                details=f"upserted={sync_result.get('upserted', 0)}",
            )
        except Exception as _sync_exc:
            sync_elapsed = (datetime.now() - sync_started).total_seconds()
            step_timings.append(("app_offers sync", sync_elapsed, "failed"))
            print(f"app_offers sync warning (non-fatal): {_sync_exc}")

        # ── Final timing breakdown ─────────────────────────────────────────
        total_elapsed = (datetime.now() - run_started).total_seconds()
        aggregate_work_elapsed = (
            sum(t[1] for t in step_timings)
            + barcode_elapsed
            + optimize_elapsed
            + storage_elapsed
        )

        print("\n" + "=" * 65)
        print("TIMING BREAKDOWN")
        print("=" * 65)
        print(f"  {'Step':<35} {'Time':>10}  {'%':>5}")
        print("  " + "-" * 55)

        all_steps = step_timings + [
            ("Barcode sync + inference", barcode_elapsed, "ok"),
            ("DB optimize",              optimize_elapsed, "ok"),
            ("Storage check",            storage_elapsed,  "ok"),
        ]

        for label, elapsed, status in all_steps:
            if elapsed < 0.1 and status in ("ok", "success"):
                continue
            pct = (elapsed / total_elapsed * 100) if total_elapsed > 0 else 0
            icon = "✓" if status in ("ok", "success") else ("↷" if status == "skipped" else "✗")
            bar = "█" * int(pct / 5)  # mini bar chart, max 20 chars
            print(f"  {icon} {label:<34} {self._format_duration(elapsed):>10}  {pct:>4.0f}%  {bar}")

        print("  " + "─" * 55)
        print(f"  {'Wall-clock total':35} {self._format_duration(total_elapsed):>10}  100%")
        if parallel:
            speedup = (aggregate_work_elapsed / total_elapsed) if total_elapsed > 0 else 0.0
            print(f"  {'Aggregate work total':35} {self._format_duration(aggregate_work_elapsed):>10}")
            print(f"  {'Effective parallelism':35} {speedup:>10.2f}x")
        print("=" * 65)

        print("\n--- ALL departamentos run complete ---")

    def run_test_departamentos(
        self,
        zip_code: str,
        limit: Optional[int] = None,
        parallel: bool = False,
        parallel_workers: Optional[int] = None,
    ):
        """Run a fast, storage-validation friendly departamentos test.

        - Defaults to 100 products per market when no limit is provided.
        - Forces barcode sync/inference skip for faster feedback.
        - Uses the same runner registry as all_departamentos, so adding a new
          market in _departamentos_runners automatically includes it here.
        """
        normalized_limit = self._normalize_limit(limit) or 100
        previous_skip = self.skip_barcode_inference
        previous_skip_days = self.skip_updated_within_days
        self.skip_barcode_inference = True
        self.skip_updated_within_days = 0.0
        try:
            print("--- Starting TEST departamentos mode ---")
            print(
                f"ZIP: {zip_code} | LIMIT: {normalized_limit} per market "
                "| Barcode inference: skipped | Recent-update skip: disabled"
            )
            self.run_all_departamentos(
                zip_code,
                limit=normalized_limit,
                parallel=parallel,
                parallel_workers=parallel_workers,
            )
        finally:
            self.skip_barcode_inference = previous_skip
            self.skip_updated_within_days = previous_skip_days

    def run_full_sequence_with_barcode_pipeline(self, zip_code: str, limit: Optional[int] = None):
        """Run the user-defined full scrape sequence + centralized barcode AI pipeline with timing."""
        print(f"--- Starting FULL sequence run for ZIP Code: {zip_code} ---\n")
        print(
            "Recent-update skip window: "
            f"{self.skip_updated_within_days:g} day(s) (set SKIP_UPDATED_WITHIN_DAYS in .env to change)."
        )
        normalized_limit = self._normalize_limit(limit)
        if self.skip_barcode_inference:
            print("Test mode: SKIP_BARCODE_INFERENCE=1 (barcode sync/inference will be skipped).")

        total_started = time.time()
        step_times: List[Tuple[str, float]] = []

        def run_step(
            label: str,
            func,
            *args,
            process_name: str = "full_sequence_pipeline",
            market_name: Optional[str] = None,
            **kwargs,
        ):
            print(f"\n>>> {label}")
            started_clock = time.time()
            started_at = datetime.now()
            status = "success"
            details = None
            try:
                result = func(*args, **kwargs)
                if result is False:
                    status = "skipped"
                    details = f"Skipped because last update is newer than {self.skip_updated_within_days:g} day(s)."
            except Exception as exc:
                status = "failed"
                details = str(exc)
                raise
            finally:
                elapsed = time.time() - started_clock
                finished_at = datetime.now()
                step_times.append((label, elapsed))
                self._record_timing(
                    process_name=process_name,
                    step_name=label,
                    market_name=market_name,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    zip_code=zip_code,
                    details=details,
                )
                print(f"<<< {label} {status} in {self._format_duration(elapsed)}")

        previous_centralized = self._centralized_barcode_phase_active
        self._centralized_barcode_phase_active = True
        try:
            # Sequence requested by user.
            run_step("Rossi departamentos", self.run_rossi_departamentos_only, zip_code, normalized_limit, market_name="Rossi")
            run_step("Tenda Atacado departamentos", self.run_tenda_departamentos_only, zip_code, normalized_limit, market_name="Tenda Atacado")
            run_step("Nagumo all-catalog departamentos", self.run_nagumo_departamentos_all_only, zip_code, normalized_limit, market_name="Nagumo")
            run_step("Sonda departamentos", self.run_sonda_departamentos_only, zip_code, normalized_limit, market_name="Sonda Delivery")
            run_step("Atacadao departamentos", self.run_atacadao_departamentos_only, zip_code, normalized_limit, market_name=MARKET_ATACADAO)
            run_step("XSupermercados departamentos", self.run_xsupermercados_departamentos_only, zip_code, normalized_limit, market_name="XSupermercados")
            run_step("Swift departamentos", self.run_swift_departamentos_only, zip_code, normalized_limit, market_name="Swift")
            run_step("Barbosa departamentos", self.run_barbosa_departamentos_only, zip_code, normalized_limit, market_name="Barbosa")
            run_step("Carrefour departamentos", self.run_carrefour_departamentos_only, zip_code, normalized_limit, market_name="Carrefour")
            run_step("Higas departamentos", self.run_higas_departamentos_only, zip_code, normalized_limit, market_name="Higas")
        finally:
            self._centralized_barcode_phase_active = previous_centralized

        # Barcode validity filtering + AI matching sequence.
        def _run_barcode_pipeline():
            trusted_snapshot_markets = list(self.barcode_ai_matcher.TRUSTED_SOURCE_MARKETS)
            snapshot_before_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
            catalog_stats = self.barcode_ai_matcher.sync_known_barcodes()
            snapshot_after_sync = self.db.get_known_barcodes_snapshot(trusted_snapshot_markets)
            source_catalog_changed = snapshot_before_sync != snapshot_after_sync
            self._print_catalog_sync_summary(catalog_stats)
            print(
                "Barcode validity filter: only normalized valid GTIN barcodes are kept in known catalog."
            )

            infer_stats = self.barcode_ai_matcher.infer_missing_barcodes(target_offer_ids=None)
            if infer_stats["ai_enabled"]:
                print(
                    "AI barcode enrichment: "
                    f"{infer_stats['matched']} inferred matches from {infer_stats['scanned']} barcode-missing offers "
                    f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                    f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped)"
                )
            else:
                print(
                    "Barcode inference: "
                    f"{infer_stats['matched']} matches from {infer_stats['scanned']} barcode-missing offers "
                    f"({infer_stats.get('skipped_unchanged', 0)} unchanged skipped, "
                    f"{infer_stats.get('ai_skipped_budget', 0)} AI-budget skipped) "
                    "(heuristic only; set ENABLE_AI_BARCODE_MATCH=1 and HF_TOKEN or OPENROUTER_API_KEY for AI-assisted matching)"
                )

            if source_catalog_changed:
                print("Barcode source catalog changed in this run: all barcode-missing offers were reconsidered.")
            else:
                print("Barcode source catalog unchanged: all barcode-missing offers were considered.")

            optimize_stats = self.db.optimize_database()
            if optimize_stats.get("db_path") == "postgres":
                print("Database optimize: ANALYZE completed for postgres (byte savings not applicable).")
            else:
                print(
                    "Database optimize: "
                    f"saved {optimize_stats['bytes_saved']} bytes in {optimize_stats['db_path']}"
                )

        if self.skip_barcode_inference:
            print("\n>>> Barcode sync + AI match")
            print("<<< Barcode sync + AI match skipped by SKIP_BARCODE_INFERENCE=1")
            step_times.append(("Barcode sync + AI match", 0.0))
        else:
            run_step("Barcode sync + AI match", _run_barcode_pipeline)

        total_elapsed = time.time() - total_started
        print("\n" + "=" * 60)
        print("FULL SEQUENCE TIMING")
        print("=" * 60)
        for label, elapsed in step_times:
            print(f"{label:35} {self._format_duration(elapsed)}")
        print("-" * 60)
        print(f"{'TOTAL':35} {self._format_duration(total_elapsed)}")
        print("=" * 60)

        print("\n--- FULL sequence run complete ---")

if __name__ == "__main__":
    system = MarketIntegrationSystem()

    raw_args = [a.strip() for a in sys.argv[1:] if str(a).strip()]
    cli_parallel = False
    cli_parallel_workers: Optional[int] = None
    positional_args: List[str] = []
    idx = 0
    while idx < len(raw_args):
        arg = raw_args[idx]
        arg_lower = arg.lower()
        if arg_lower in {"--parallel", "-p"}:
            cli_parallel = True
            idx += 1
            continue
        if arg_lower.startswith("--parallel-workers="):
            cli_parallel = True
            value = arg.split("=", 1)[1].strip()
            try:
                parsed = int(value)
                if parsed > 0:
                    cli_parallel_workers = parsed
            except ValueError:
                pass
            idx += 1
            continue
        if arg_lower == "--parallel-workers":
            cli_parallel = True
            if idx + 1 < len(raw_args):
                value = raw_args[idx + 1].strip()
                try:
                    parsed = int(value)
                    if parsed > 0:
                        cli_parallel_workers = parsed
                except ValueError:
                    pass
                idx += 2
                continue
            idx += 1
            continue
        positional_args.append(arg)
        idx += 1

    # ── Per-market repo mode — check FIRST ─────────────────────────────────
    # _SINGLE_MARKET is set at module level (above) before any imports modify
    # os.environ. We also re-check here in case .env was loaded with a value.
    scrape_market = _SINGLE_MARKET or (
        os.environ.get("SCRAPE_MARKET", "").strip()
        or os.environ.get("MARKET_NAME", "").strip()
    )

    # Always print so GitHub Actions logs show which market was detected
    print(f"[boot] SCRAPE_MARKET={os.environ.get('SCRAPE_MARKET', '<not set>')} "
          f"MARKET_NAME={os.environ.get('MARKET_NAME', '<not set>')} "
            f"-> resolved='{scrape_market}'")

    mode = config.SCRAPE_MODE.strip().lower()
    zip_code = config.SCRAPE_ZIP_CODE
    env_limit = str(config.SCRAPE_LIMIT or "")

    # Canonical market name → run method mapping
    _market_to_mode = {
        "Rossi":          "rossi_departamentos",
        "Atacadão":       "atacadao_departamentos",
        "Nagumo":         "nagumo_departamentos",
        "Higas":          "higas_departamentos",
        "Tenda Atacado":  "tenda_departamentos",
        "Swift":          "swift_departamentos",
        "Sonda Delivery": "sonda_departamentos",
        "XSupermercados": "xsupermercados_departamentos",
        "Barbosa":        "barbosa_departamentos",
        "Carrefour":      "carrefour_departamentos",
        "Oba Hortifruti": "oba_departamentos",
        "Sam's Club":     "samsclub_departamentos",
        "Extra":          "extra_departamentos",
        "Pão de Açúcar":  "paodeacucar_departamentos",
        "Davo":           "davo_departamentos",
        "Giga":           "giga_departamentos",
    }

    def _normalize_market_key(value: str) -> str:
        return str(value or "").strip().lower()

    _market_aliases = {
        "rossi_departamentos": "Rossi",
        "atacadao_departamentos": "Atacadão",
        "atacadao": "Atacadão",
        "nagumo_departamentos": "Nagumo",
        "nagumo": "Nagumo",
        "higas_departamentos": "Higas",
        "higas": "Higas",
        "tenda_departamentos": "Tenda Atacado",
        "tenda": "Tenda Atacado",
        "tenda atacado": "Tenda Atacado",
        "swift_departamentos": "Swift",
        "swift": "Swift",
        "sonda_departamentos": "Sonda Delivery",
        "sonda": "Sonda Delivery",
        "xsupermercados_departamentos": "XSupermercados",
        "xsupermercados": "XSupermercados",
        "barbosa_departamentos": "Barbosa",
        "barbosa": "Barbosa",
        "carrefour_departamentos": "Carrefour",
        "carrefour": "Carrefour",
        "oba_departamentos": "Oba Hortifruti",
        "oba": "Oba Hortifruti",
        "samsclub_departamentos": "Sam's Club",
        "samsclub": "Sam's Club",
        "sam's club": "Sam's Club",
        "extra_departamentos": "Extra",
        "extra": "Extra",
        "paodeacucar_departamentos": "Pão de Açúcar",
        "paodeacucar": "Pão de Açúcar",
        "pao de acucar": "Pão de Açúcar",
        "pão de açúcar": "Pão de Açúcar",
        "davo_departamentos": "Davo",
        "davo": "Davo",
        "giga_departamentos": "Giga",
        "giga": "Giga",
    }
    for _canonical_market, _mode_name in _market_to_mode.items():
        _market_aliases[_normalize_market_key(_canonical_market)] = _canonical_market
        _market_aliases[_normalize_market_key(_mode_name)] = _canonical_market

    canonical_scrape_market = _market_aliases.get(_normalize_market_key(scrape_market), "") if scrape_market else ""

    if canonical_scrape_market and canonical_scrape_market in _market_to_mode:
        # Single-market mode: run this market then run the barcode pipeline
        market_mode = _market_to_mode[canonical_scrape_market]
        cli_limit = None
        if positional_args:
            try:
                cli_limit = int(positional_args[0])
            except ValueError:
                pass
        effective_limit = system._normalize_limit(cli_limit or (int(env_limit) if env_limit else None))
        zip_code = os.getenv("SCRAPE_ZIP_CODE", "").strip() or config.SCRAPE_ZIP_CODE
        print(
            f"SCRAPE_MARKET mode: {canonical_scrape_market} ({market_mode}) | "
            f"ZIP: {zip_code} | LIMIT: {effective_limit or 'ALL'}"
        )
        # 1. Run this market's scraper
        getattr(system, f"run_{market_mode.replace('_departamentos', '')}_departamentos_only")(
            zip_code, limit=effective_limit
        ) if hasattr(system, f"run_{market_mode.replace('_departamentos', '')}_departamentos_only") else None

        # 2. Run barcode pipeline (sync catalog + AI inference for this market)
        if not system.skip_barcode_inference:
            system.run_barcode_pipeline_only(
                zip_code=zip_code,
                target_markets=[canonical_scrape_market],
                limit=effective_limit,
            )
        sys.exit(0)

    # Usage examples:
    # python main.py                         -> runs all markets (non-departamentos)
    # python main.py all_departamentos       -> runs all departamentos scrapers
    # python main.py all_departamentos --parallel -> run markets in parallel batches locally
    # python main.py all_departamentos --parallel --parallel-workers 6 -> set worker count
    # python main.py all_departamentos 100   -> runs all departamentos with limit=100 per market
    # python main.py test_departamentos      -> fast test mode (defaults to 100 per market)
    # python main.py test_departamentos --parallel 100 -> fast test mode in parallel batches
    # python main.py test_departamentos 80   -> fast test mode with custom limit
    # set SKIP_BARCODE_INFERENCE=1           -> test mode, skips barcode sync/inference
    # python main.py rossi_departamentos     -> runs only Rossi departamentos
    # python main.py nagumo_departamentos    -> runs only Nagumo departamentos
    # python main.py nagumo_departamentos_all -> runs Nagumo all-catalog (no pmid)
    # python main.py atacadao_departamentos  -> runs only Atacadao departamentos
    # python main.py higas_departamentos     -> runs only Higas departamentos
    # python main.py higas_enrich            -> runs only Higas barcode enrichment
    # python main.py embedding_model_infer   -> runs local embedding model on barcode-missing offers and writes audit rows
    # python main.py full_sequence_pipeline  -> runs full user sequence + centralized AI matching
    # python main.py xsupermercados_departamentos -> runs only X Supermercados departamentos
    # python main.py sonda_departamentos     -> runs only Sonda Delivery departamentos
    # python main.py swift_departamentos     -> runs only Swift departamentos
    # python main.py barbosa_departamentos    -> runs only Barbosa departamentos
    # python main.py carrefour_departamentos  -> runs only Carrefour departamentos
    # python main.py oba_departamentos        -> runs only Oba Hortifruti departamentos
    # python main.py samsclub_departamentos   -> runs only Sam's Club departamentos
    # python main.py extra_departamentos      -> runs only Extra Mercado departamentos
    # python main.py paodeacucar_departamentos -> runs only Pão de Açúcar departamentos
    # python main.py tenda_departamentos      -> runs only Tenda Atacado departamentos
    # python main.py davo_departamentos       -> runs only Davo departamentos
    # python main.py giga_departamentos       -> runs only Giga departamentos
    # python main.py barcode_pipeline_only    -> sync + infer barcodes only (coordinator mode for parallel machines)
    # python main.py build_cart shopping_list.json -> builds per-market cart CSV files from input list
    # python main.py build_cart_swift shopping_list.json -> builds Swift-focused cart outputs
    # python main.py swift_cart_auto cart_output/<ts>/swift_cart_actions.json -> auto-add Swift items after login
    cli_mode = positional_args[0].strip().lower() if positional_args else None
    cli_limit = None
    if len(positional_args) > 1:
        try:
            cli_limit = int(positional_args[1])
        except ValueError:
            cli_limit = None
    elif env_limit:
        try:
            cli_limit = int(env_limit)
        except ValueError:
            cli_limit = None

    effective_limit = system._normalize_limit(cli_limit)
    effective_mode = cli_mode or mode
    print(
        f"SCRAPE_MODE active: {effective_mode} | ZIP: {zip_code} | LIMIT: {effective_limit or 'ALL'} "
        f"| SKIP_BARCODE_INFERENCE: {system.skip_barcode_inference} "
        f"| PARALLEL: {cli_parallel}"
    )

    if effective_mode == "all_departamentos":
        system.run_all_departamentos(
            zip_code,
            limit=effective_limit,
            parallel=cli_parallel,
            parallel_workers=cli_parallel_workers,
        )
    elif effective_mode == "test_departamentos":
        system.run_test_departamentos(
            zip_code,
            limit=effective_limit,
            parallel=cli_parallel,
            parallel_workers=cli_parallel_workers,
        )
    elif effective_mode == "rossi_departamentos":
        system.run_rossi_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "nagumo_departamentos":
        system.run_nagumo_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "nagumo_departamentos_all":
        system.run_nagumo_departamentos_all_only(zip_code, limit=effective_limit)
    elif effective_mode == "atacadao_departamentos":
        system.run_atacadao_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "higas_departamentos":
        system.run_higas_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "higas_enrich":
        system.run_higas_enrich_only(zip_code=zip_code, limit=effective_limit)
    elif effective_mode == "extra_enrich":
        system.run_extra_enrich_only(zip_code=zip_code, limit=effective_limit)
    elif effective_mode == "paodeacucar_enrich":
        system.run_paodeacucar_enrich_only(zip_code=zip_code, limit=effective_limit)
    elif effective_mode == "oba_enrich":
        system.run_oba_enrich_only(zip_code=zip_code, limit=effective_limit)
    elif effective_mode == "nagumo_enrich":
        system.run_nagumo_enrich_only(zip_code=zip_code, limit=effective_limit)
    elif effective_mode == "embedding_model_infer":
        system.run_embedding_model_inference_only(limit=effective_limit)
    elif effective_mode == "full_sequence_pipeline":
        system.run_full_sequence_with_barcode_pipeline(zip_code, limit=effective_limit)
    elif effective_mode == "xsupermercados_departamentos":
        system.run_xsupermercados_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "sonda_departamentos":
        system.run_sonda_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "swift_departamentos":
        system.run_swift_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "barbosa_departamentos":
        system.run_barbosa_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "carrefour_departamentos":
        system.run_carrefour_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "oba_departamentos":
        system.run_oba_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "samsclub_departamentos":
        system.run_samsclub_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "extra_departamentos":
        system.run_extra_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "paodeacucar_departamentos":
        system.run_paodeacucar_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "tenda_departamentos":
        system.run_tenda_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "davo_departamentos":
        system.run_davo_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "giga_departamentos":
        system.run_giga_departamentos_only(zip_code, limit=effective_limit)
    elif effective_mode == "barcode_pipeline_only":
        _force = "force" in [a.strip().lower() for a in positional_args[1:]]
        system.run_barcode_pipeline_only(zip_code=zip_code, limit=effective_limit, force=_force)
    elif effective_mode == "build_cart":
        input_path = positional_args[1].strip() if len(positional_args) > 1 else "shopping_list.json"
        system.run_build_cart_from_list(input_path=input_path)
    elif effective_mode == "build_cart_swift":
        input_path = positional_args[1].strip() if len(positional_args) > 1 else "shopping_list.json"
        system.run_build_cart_from_list(input_path=input_path, markets=["Swift"])
    elif effective_mode == "swift_cart_auto":
        actions_path = positional_args[1].strip() if len(positional_args) > 1 else "swift_cart_actions.json"
        run_swift_cart_automation(actions_path=actions_path)
    else:
        system.run_all(zip_code, limit=effective_limit)
