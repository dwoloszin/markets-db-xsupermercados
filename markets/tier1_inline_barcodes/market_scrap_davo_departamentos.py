"""
market_scrap_davo_departamentos.py — Davo scraper via public storefront API.

Source endpoints discovered from davo.com.br frontend:
- https://davo.com.br/api/products
- https://davo.com.br/api/categories

Behavior observed:
- /api/products supports page + limit pagination.
- Response shape: {totalResults, offset, limit, items:[...]}
- SKU often contains GTIN digits in format like: prod_1220000250147
"""

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from db.db_manager import DatabaseManager


class DavoDepartamentosScraper:
    WEB_BASE = "https://davo.com.br"
    API_BASE = f"{WEB_BASE}/api"
    IMAGE_BASE = "https://ecomapi.davo.com.br/images"
    STORE_ID = "davo.com.br"
    PAGE_SIZE = 100

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": f"{self.WEB_BASE}/",
            }
        )
        self.market_name = "Davo"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_gtin_from_sku(sku: Any) -> Optional[str]:
        text = str(sku or "").strip()
        if not text:
            return None
        # Prefer the longest 8-14 digit run (e.g. prod_1220000250147)
        matches = re.findall(r"\d{8,14}", text)
        if not matches:
            return None
        matches.sort(key=len, reverse=True)
        return matches[0]

    def _build_product_url(self, product: Dict[str, Any], barcode: Optional[str] = None) -> Optional[str]:
        slug = str(product.get("seoUrlSlug") or "").strip().strip("/")
        if slug:
            suffix = f"/prod_{barcode}" if barcode else ""
            return f"{self.WEB_BASE}/produto/{slug}{suffix}"
        return None

    def _build_image_url(self, product: Dict[str, Any]) -> Optional[str]:
        main_image = str(product.get("mainImage") or "").strip().lstrip("/")
        if main_image:
            return f"{self.IMAGE_BASE}/{main_image}?w=300&h=300"
        images = product.get("images") or []
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str) and first.strip():
                path = first.strip().lstrip("/")
                return f"{self.IMAGE_BASE}/{path}?w=300&h=300"
            if isinstance(first, dict):
                candidate = str(first.get("url") or first.get("path") or "").strip()
                if candidate.startswith("http"):
                    return candidate
                if candidate:
                    return f"{self.IMAGE_BASE}/{candidate.lstrip('/')}?w=300&h=300"
        return None

    def _standardize_product(self, product: Dict[str, Any], zip_code: str) -> Optional[Dict[str, Any]]:
        product_id = str(product.get("_id") or product.get("sku") or "").strip()
        if not product_id:
            return None

        gtin_text = self._extract_gtin_from_sku(product.get("sku"))
        barcode = self.db.normalize_barcode(gtin_text)

        regular_price = self._to_float(product.get("listPrice"))
        final_price = self._to_float(product.get("finalPrice"))
        sale_price = self._to_float(product.get("salePrice"))
        promo_price = sale_price if sale_price is not None else final_price

        if regular_price is None and promo_price is not None:
            regular_price = promo_price
        if promo_price is None and regular_price is not None:
            promo_price = regular_price

        if regular_price is not None and promo_price is not None and promo_price >= regular_price:
            promo_price = None

        offer_id = self.db.build_offer_id(
            "davo",
            self.STORE_ID,
            barcode,
            gtin_text,
            product.get("displayName") or product.get("description"),
        )
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": (product.get("displayName") or product.get("description") or "").strip() or None,
            "brand": (product.get("brand") or "").strip() or None,
            "description": (product.get("longDescription") or product.get("description") or "").strip() or None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "unit": product.get("dav_measureUnit"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": self._build_product_url(product, barcode),
            "image_url": self._build_image_url(product),
            "stock_balance": product.get("stockLevel") if product.get("stockLevel") is not None else product.get("stock"),
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": self.STORE_ID,
            "zip_code": zip_code,
        }

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        print(f"Fetching Davo departamentos offers for {zip_code}...")

        max_items = limit if isinstance(limit, int) and limit > 0 else None
        page = 1
        max_pages = 200
        total_results: Optional[int] = None

        offers_by_id: Dict[str, Dict[str, Any]] = {}

        while page <= max_pages:
            params = {"page": page, "limit": self.PAGE_SIZE}
            try:
                resp = self.session.get(f"{self.API_BASE}/products", params=params, timeout=60)
                if resp.status_code != 200:
                    print(f"Davo page {page}: HTTP {resp.status_code}, stopping")
                    break

                body = resp.json() or {}
                items = body.get("items") or []
                if not isinstance(items, list) or not items:
                    print(f"Davo page {page}: no more products")
                    break

                if total_results is None:
                    tr = body.get("totalResults")
                    total_results = int(tr) if isinstance(tr, (int, float, str)) and str(tr).isdigit() else None

                added = 0
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    normalized = self._standardize_product(raw, zip_code)
                    if not normalized:
                        continue
                    offers_by_id[normalized["id"]] = normalized
                    added += 1
                    if max_items is not None and len(offers_by_id) >= max_items:
                        break

                print(f"Davo page {page}: {added} products (unique_total={len(offers_by_id)})")

                if max_items is not None and len(offers_by_id) >= max_items:
                    break
                if total_results is not None and len(offers_by_id) >= total_results:
                    break

                page += 1
                time.sleep(0.1)
            except Exception as exc:
                print(f"Davo page {page}: error {exc}")
                break

        all_rows = list(offers_by_id.values())
        if max_items is not None:
            all_rows = all_rows[:max_items]

        print(f"Davo: {len(all_rows)} offers collected.")
        return all_rows


if __name__ == "__main__":
    scraper = DavoDepartamentosScraper()
    scraper.fetch_offers("08032-230")
