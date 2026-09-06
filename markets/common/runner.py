"""
runner.py - CLI boilerplate shared by every scraper module.

A scraper module only has to expose:

    STORE_KEY = "atacadao"
    def scrape(db, zip_code: str, limit: Optional[int]) -> dict   # saves via db.save()
    (optional) def enrich(db, workers: int, limit: Optional[int]) -> dict

and finish with:

    if __name__ == "__main__":
        from markets.common.runner import run_cli
        run_cli(STORE_KEY, scrape, enrich=enrich)

`run_cli` parses --limit/--zip/--env/--csv/--workers/--skip-enrich, loads .env,
opens the store DB, runs the scrape, then the inline barcode enrichment (if the
market has one), seeds barcodes from the legacy table when present, prints a
summary and exits non-zero on failure so GitHub Actions goes red.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from typing import Callable, Dict, Optional

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")


class ScrapeStats:
    """Accumulates save() results; `db.save()` returns dicts we add here."""

    def __init__(self) -> None:
        self.upserted = 0
        self.skipped = 0
        self.batches = 0
        self.with_barcode = 0

    def add(self, result: Dict[str, int]) -> None:
        self.upserted += int(result.get("upserted", 0))
        self.skipped += int(result.get("skipped", 0))
        self.with_barcode += int(result.get("with_barcode", 0))
        self.batches += 1

    def as_dict(self) -> Dict[str, int]:
        return {"upserted": self.upserted, "skipped": self.skipped,
                "with_barcode": self.with_barcode, "batches": self.batches}


def run_cli(store_key: str, scrape: Callable, *, enrich: Optional[Callable] = None) -> None:
    parser = argparse.ArgumentParser(description=f"Scrape {store_key} -> PostgreSQL (Neon)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N products (test mode)")
    parser.add_argument("--zip", type=str, default=None, help="CEP used to pick the store (default: config/SCRAPE_ZIP_CODE)")
    parser.add_argument("--env", type=str, default=".env", help=".env file path")
    parser.add_argument("--csv", action="store_true", help="Also export offers to exports/<store>_offers_<ts>.csv")
    parser.add_argument("--workers", type=int, default=12, help="Threads for barcode enrichment")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip the inline barcode enrichment step")
    parser.add_argument("--enrich-only", action="store_true", help="Only run barcode enrichment (no scrape)")
    args = parser.parse_args()

    from db.db_manager import load_env, open_store_db
    import config

    load_env(args.env)
    zip_code = args.zip or config.SCRAPE_ZIP_CODE
    t0 = time.time()
    db = open_store_db(store_key)
    ok = True
    try:
        if not args.enrich_only:
            print(f"[{store_key}] scrape start zip={zip_code} limit={args.limit or 'none'}")
            result = scrape(db, zip_code, args.limit) or {}
            elapsed = time.time() - t0
            print(f"[{store_key}] scrape done in {elapsed / 60:.1f} min: {result}")
            if not result.get("upserted") and not args.limit:
                print(f"[{store_key}] ERROR: no offers were saved - failing the run so it is noticed")
                ok = False
            seeded = db.seed_barcodes_from_legacy()
            if seeded:
                print(f"[{store_key}] barcodes seeded from legacy table: {seeded:,}")
        if enrich is not None and not args.skip_enrich and ok:
            t1 = time.time()
            print(f"[{store_key}] barcode enrichment start (workers={args.workers})")
            stats = enrich(db, workers=args.workers, limit=args.limit) or {}
            print(f"[{store_key}] barcode enrichment done in {(time.time() - t1) / 60:.1f} min: {stats}")
        db.print_barcode_coverage()
        if args.csv:
            db.export("exports", tables=["offers"])
    except Exception:
        traceback.print_exc()
        ok = False
    finally:
        db.close()
    print(f"[{store_key}] total {(time.time() - t0) / 60:.1f} min - {'OK' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)
