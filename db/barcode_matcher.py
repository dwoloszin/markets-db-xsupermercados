import csv
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import requests

from db.db_manager import DatabaseManager


class BarcodeMatcher:
    """Match products across markets using barcodes and fuzzy name matching.

    All DB access goes through DatabaseManager — no direct SQLite/psycopg calls.
    """

    HIGAS_STORE_ID = "66466cdefafdf200a3352cd5"
    HIGAS_PARTNER_ID = "replicarhigas"
    HIGAS_SEARCH_URL = "https://api.instabuy.com.br/apiv3/search"

    # Minimum SequenceMatcher ratio to accept a fuzzy match.
    FUZZY_THRESHOLD = 0.6
    # Weight applied to name similarity (remainder goes to brand).
    NAME_WEIGHT = 0.6
    # Max fuzzy results returned per product.
    MAX_FUZZY_RESULTS = 3
    # Seconds to sleep between Higas API calls to avoid 429s.
    HIGAS_RATE_LIMIT_DELAY = 0.3

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
        )

    # ------------------------------------------------------------------ Higas API

    def search_higas_barcode(self, barcode: str) -> Optional[Dict]:
        """Search the Higas Instabuy API for a product by barcode."""
        params = {
            "search_barcode": barcode,
            "platform": "store_android",
            "version": "570",
            "store_id": self.HIGAS_STORE_ID,
            "partner_id": self.HIGAS_PARTNER_ID,
        }
        try:
            response = self.session.get(self.HIGAS_SEARCH_URL, params=params, timeout=15)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                print(f"  ⚠ Higas rate-limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                response = self.session.get(self.HIGAS_SEARCH_URL, params=params, timeout=15)
            if response.status_code == 200:
                products = (response.json() or {}).get("data") or []
                return products[0] if products else None
        except Exception as exc:
            print(f"  ✗ Error searching Higas barcode {barcode}: {exc}")
        return None

    # ------------------------------------------------------------------ matching

    def match_rossi_barcodes(self) -> None:
        """Find all Rossi barcodes and search for matches in Higas and other markets."""
        # Use DatabaseManager's method — works with both Postgres and SQLite.
        rossi_rows = self.db.fetch_offers_with_barcodes(["Rossi"])
        # rows: (market_name, id, barcode, product_name, brand, description)
        rossi_products = [
            (row[1], row[2], row[3], row[4])
            for row in rossi_rows
            if row[2]  # must have barcode
        ]

        print(f"\n{'=' * 60}")
        print("BARCODE MATCHING")
        print(f"{'=' * 60}")
        print(f"Found {len(rossi_products)} Rossi products with barcodes")
        print("Searching for matches in other markets...\n")

        # Pre-load all non-Rossi products once for fuzzy matching.
        # Use DatabaseManager so the query goes to the right DB.
        other_markets = [m for m in self.db.MARKET_DB_ENV_SUFFIX if m != "Rossi"]
        all_other_rows = self.db.fetch_offers_with_barcodes(other_markets)
        # Build an in-memory list of (offer_id, market, product_name, brand)
        other_products: List[Tuple[str, str, str, str]] = [
            (row[1], row[0], str(row[3] or ""), str(row[4] or ""))
            for row in all_other_rows
        ]

        matched = 0
        bulk_refs: List[Tuple[str, str, str, str, str]] = []

        for i, (rossi_id, barcode, product_name, brand) in enumerate(rossi_products, 1):
            print(f"[{i}/{len(rossi_products)}] Barcode: {barcode}")

            # Try exact barcode match via Higas API.
            higas_match = self.search_higas_barcode(barcode)
            if higas_match:
                higas_native_id = higas_match.get("id")
                higas_offer_id = f"higas_{higas_native_id}" if higas_native_id else None
                print(f"  ✓ Found in Higas: {higas_match.get('name')}")
                if higas_offer_id:
                    bulk_refs.append((barcode, "Higas", higas_offer_id, str(product_name or ""), str(brand or "")))
                matched += 1
                time.sleep(self.HIGAS_RATE_LIMIT_DELAY)
                continue

            # Fallback: fuzzy name/brand match in pre-loaded other-market data.
            if product_name:
                fuzzy = self._fuzzy_match_in_memory(product_name, brand, other_products)
                if fuzzy:
                    print(f"  ~ Fuzzy matched in {len(fuzzy)} market(s):")
                    for market, offer_id, match_name, score in fuzzy:
                        print(f"    - {market}: {match_name} ({score:.0%})")
                else:
                    print("  ✗ No matches found")
            else:
                print("  ✗ No product name — skipping fuzzy match")

            time.sleep(self.HIGAS_RATE_LIMIT_DELAY)

        # Bulk-save all barcode references in one DB round-trip.
        if bulk_refs:
            self.db.save_barcode_references_bulk(bulk_refs)
            print(f"\nSaved {len(bulk_refs)} barcode references to database.")

        print(f"\n{'=' * 60}")
        print(f"Matching complete: {matched} barcodes found in Higas")
        print(f"{'=' * 60}\n")

    # ------------------------------------------------------------------ fuzzy

    def _fuzzy_match_in_memory(
        self,
        product_name: str,
        brand: Optional[str],
        other_products: List[Tuple[str, str, str, str]],
    ) -> List[Tuple[str, str, str, float]]:
        """
        Fuzzy-match a product against a pre-loaded list of other-market products.

        `other_products` is a list of (offer_id, market_name, product_name, brand).
        Returns up to MAX_FUZZY_RESULTS matches above FUZZY_THRESHOLD, sorted by score.
        """
        name_lower = product_name.lower()
        brand_lower = (brand or "").lower()
        matches: List[Tuple[str, str, str, float]] = []

        for offer_id, market, other_name, other_brand in other_products:
            if not other_name:
                continue
            name_score = SequenceMatcher(None, name_lower, other_name.lower()).ratio()
            brand_score = (
                SequenceMatcher(None, brand_lower, other_brand.lower()).ratio()
                if brand_lower and other_brand
                else 0.0
            )
            combined = name_score * self.NAME_WEIGHT + brand_score * (1 - self.NAME_WEIGHT)
            if combined >= self.FUZZY_THRESHOLD:
                matches.append((market, offer_id, other_name, combined))

        matches.sort(key=lambda x: x[3], reverse=True)
        return matches[: self.MAX_FUZZY_RESULTS]

    # ------------------------------------------------------------------ export

    def export_matching_report(self, filename: str = "barcode_matching_report.csv") -> Optional[str]:
        """Export a CSV report of all known barcode cross-references."""
        # fetch_barcode_reference_catalog returns rows:
        # (canonical_market, market_offer_id, barcode, product_name, brand, last_updated)
        catalog_rows = self.db.fetch_barcode_reference_catalog()

        if not catalog_rows:
            print("No barcode matches to export.")
            return None

        # Group by barcode → {market: offer_id, ...}
        by_barcode: Dict[str, Dict] = {}
        for market, offer_id, barcode, product_name, brand, last_updated in catalog_rows:
            if barcode not in by_barcode:
                by_barcode[barcode] = {
                    "product_name": product_name,
                    "brand": brand,
                }
            by_barcode[barcode][market] = offer_id

        all_markets = sorted(
            {row[0] for row in catalog_rows}
        )

        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Barcode", "Product Name", "Brand", *all_markets, "Markets Count"])
            for barcode, data in sorted(by_barcode.items()):
                market_ids = [data.get(m, "") for m in all_markets]
                markets_count = sum(1 for v in market_ids if v)
                writer.writerow(
                    [barcode, data.get("product_name", ""), data.get("brand", ""), *market_ids, markets_count]
                )

        print(f"✓ Barcode matching report exported to {filename} ({len(by_barcode)} barcodes)")
        return filename
