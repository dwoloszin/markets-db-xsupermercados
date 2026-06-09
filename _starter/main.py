"""
main.py — Entry point for the store scraper system.

Reads SCRAPE_MARKET (or MARKET_NAME) env var to know which store to run.
If neither is set, runs all stores defined in STORE_REGISTRY.

Usage:
    python main.py                     # run all stores
    SCRAPE_MARKET="Drogaria Example" python main.py   # run one store
"""

import os
import sys
from datetime import datetime
from typing import Optional

from env_loader import load_env_file
from db.db_manager import DatabaseManager
import config

load_env_file()

# ── Single-market mode ────────────────────────────────────────────────────────
_SINGLE_MARKET = (
    os.environ.get("SCRAPE_MARKET", "").strip()
    or os.environ.get("MARKET_NAME", "").strip()
)
if _SINGLE_MARKET:
    print(f"[single-store mode] STORE={_SINGLE_MARKET!r}")


# ── Store registry ────────────────────────────────────────────────────────────
# Register your scrapers here: "Store Name" -> import path + class name
# The scraper class must implement:
#   fetch_offers(zip_code: str, limit: Optional[int] = None) -> list[dict]
#
# Example:
#   from markets.example.scraper_template import ExampleStoreScraper
#   STORE_REGISTRY = {
#       "Drogaria Example": ExampleStoreScraper,
#   }

STORE_REGISTRY: dict = {
    # "Drogaria Example": ExampleStoreScraper,
}


def run_store(store_name: str, db: DatabaseManager) -> bool:
    """Fetch and save offers for one store. Returns True on success."""
    scraper_class = STORE_REGISTRY.get(store_name)
    if scraper_class is None:
        print(f"[ERROR] No scraper registered for store: {store_name!r}")
        print(f"  Register it in STORE_REGISTRY in main.py")
        return False

    zip_code = os.environ.get("SCRAPE_ZIP_CODE", "").strip() or config.SCRAPE_ZIP_CODE
    limit = config.SCRAPE_LIMIT

    print(f"\n{'='*60}")
    print(f"  Store: {store_name}")
    print(f"  ZIP:   {zip_code}")
    print(f"  Limit: {limit or 'no limit'}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    try:
        scraper = scraper_class()
        offers = scraper.fetch_offers(zip_code=zip_code, limit=limit)
        if not offers:
            print(f"[WARN] {store_name}: no offers returned")
            return False

        print(f"\n{store_name}: saving {len(offers)} offers...")
        db.save_offers(store_name, offers)
        print(f"{store_name}: done.")
        return True

    except Exception as exc:
        print(f"[ERROR] {store_name}: {exc}")
        import traceback
        traceback.print_exc()
        return False


def main() -> None:
    db = DatabaseManager()

    stores_to_run = (
        [_SINGLE_MARKET] if _SINGLE_MARKET
        else list(STORE_REGISTRY.keys())
    )

    if not stores_to_run:
        print("[ERROR] No stores to run.")
        print("  Either set SCRAPE_MARKET env var or add stores to STORE_REGISTRY in main.py")
        sys.exit(1)

    results = {}
    for store in stores_to_run:
        results[store] = run_store(store, db)

    print(f"\n{'='*60}")
    ok = [s for s, v in results.items() if v]
    failed = [s for s, v in results.items() if not v]
    print(f"DONE: {len(ok)} ok, {len(failed)} failed")
    if failed:
        print(f"Failed: {failed}")
        sys.exit(1)


if __name__ == "__main__":
    main()
