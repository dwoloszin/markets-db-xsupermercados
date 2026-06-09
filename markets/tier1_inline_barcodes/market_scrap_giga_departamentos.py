"""
market_scrap_giga_departamentos.py — Giga VTEX public catalog scraper.

Source endpoints discovered from giga.com.vc frontend:
- https://www.giga.com.vc/api/catalog_system/pub/products/search
- https://www.giga.com.vc/api/io/_v/api/intelligent-search/catalog_count

Behavior observed:
- /products/search supports offset pagination with _from/_to.
- Response payload already includes GTIN/EAN, prices and stock in item.sellers[].
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from db.db_manager import DatabaseManager


class GigaDepartamentosScraper:
    WEB_BASE = "https://www.giga.com.vc"
    API_PRODUCTS = f"{WEB_BASE}/api/catalog_system/pub/products/search"
    API_CATALOG_COUNT = f"{WEB_BASE}/api/io/_v/api/intelligent-search/catalog_count"
    STORE_ID = "giga.com.vc"
    PAGE_SIZE = 50

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
        self.market_name = "Giga"

    @staticmethod
    def _normalize_zip(zip_code: str) -> str:
        digits = "".join(ch for ch in str(zip_code or "") if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:5]}-{digits[5:]}"
        return "01001-000"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_first_item(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        items = product.get("items") or []
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        return first if isinstance(first, dict) else None

    @staticmethod
    def _extract_offer(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sellers = item.get("sellers") or []
        if not isinstance(sellers, list):
            return None
        for seller in sellers:
            if not isinstance(seller, dict):
                continue
            offer = seller.get("commertialOffer") or {}
            if isinstance(offer, dict):
                return offer
        return None

    @staticmethod
    def _extract_image_url(item: Dict[str, Any]) -> Optional[str]:
        images = item.get("images") or []
        if not isinstance(images, list) or not images:
            return None
        first = images[0]
        if not isinstance(first, dict):
            return None
        image_url = str(first.get("imageUrl") or "").strip()
        return image_url or None

    def _build_offer(self, product: Dict[str, Any], zip_code: str) -> Optional[Dict[str, Any]]:
        product_id = str(product.get("productId") or "").strip()
        if not product_id:
            return None

        item = self._extract_first_item(product)
        if not item:
            return None

        gtin_text = str(item.get("ean") or "").strip() or None
        barcode = self.db.normalize_barcode(gtin_text)

        offer = self._extract_offer(item) or {}
        regular_price = self._to_float(offer.get("ListPrice"))
        promo_price = self._to_float(offer.get("Price"))
        stock_balance = offer.get("AvailableQuantity")
        is_available = bool(offer.get("IsAvailable"))

        if regular_price is None and promo_price is not None:
            regular_price = promo_price
        if promo_price is None and regular_price is not None:
            promo_price = regular_price
        if regular_price is not None and promo_price is not None and promo_price >= regular_price:
            promo_price = None

        unit = item.get("measurementUnit")
        multiplier = item.get("unitMultiplier")
        if unit and multiplier not in (None, "", 1, 1.0):
            unit = f"{multiplier}{unit}"

        product_name = (
            str(product.get("productName") or item.get("name") or "").strip() or None
        )
        offer_id = self.db.build_offer_id(
            "giga",
            self.STORE_ID,
            barcode,
            gtin_text,
            product_name,
        )
        if not offer_id:
            return None

        product_url = str(product.get("link") or "").strip()
        if product_url and product_url.startswith("/"):
            product_url = f"{self.WEB_BASE}{product_url}"

        return {
            "id": offer_id,
            "product_name": product_name,
            "brand": (product.get("brand") or "").strip() or None,
            "description": (product.get("description") or "").strip() or None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "unit": unit,
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url or None,
            "image_url": self._extract_image_url(item),
            "stock_balance": stock_balance if is_available else 0,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": self.STORE_ID,
            "zip_code": zip_code,
        }

    def _fetch_catalog_total(self, zip_code: str) -> Optional[int]:
        try:
            response = self.session.get(
                self.API_CATALOG_COUNT,
                params={"zip-code": self._normalize_zip(zip_code)},
                timeout=30,
            )
            if response.status_code != 200:
                return None
            body = response.json() or {}
            total = body.get("total")
            if isinstance(total, int) and total >= 0:
                return total
        except Exception:
            return None
        return None

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        print(f"Fetching Giga departamentos offers for {zip_code}...")

        max_items = limit if isinstance(limit, int) and limit > 0 else None
        reported_total = self._fetch_catalog_total(zip_code)
        if reported_total is not None:
            print(f"Giga catalog_count total={reported_total}")

        offers_by_id: Dict[str, Dict[str, Any]] = {}
        page = 0
        max_pages = 400

        while page < max_pages:
            offset = page * self.PAGE_SIZE
            end_index = offset + self.PAGE_SIZE - 1
            params = {"_from": offset, "_to": end_index}

            try:
                response = self.session.get(self.API_PRODUCTS, params=params, timeout=60)
                if response.status_code not in (200, 206):
                    print(f"Giga page {page + 1}: HTTP {response.status_code}, stopping")
                    break

                rows = response.json() or []
                if not isinstance(rows, list) or not rows:
                    print(f"Giga page {page + 1}: no more products")
                    break

                added = 0
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    normalized = self._build_offer(raw, zip_code)
                    if not normalized:
                        continue
                    offers_by_id[normalized["id"]] = normalized
                    added += 1
                    if max_items is not None and len(offers_by_id) >= max_items:
                        break

                print(f"Giga page {page + 1}: {added} products (unique_total={len(offers_by_id)})")

                if max_items is not None and len(offers_by_id) >= max_items:
                    break
                if reported_total is not None and len(offers_by_id) >= reported_total:
                    break

                page += 1
                time.sleep(0.08)
            except Exception as exc:
                print(f"Giga page {page + 1}: error {exc}")
                break

        all_rows = list(offers_by_id.values())
        if max_items is not None:
            all_rows = all_rows[:max_items]

        print(f"Giga: {len(all_rows)} offers collected.")
        return all_rows


if __name__ == "__main__":
    scraper = GigaDepartamentosScraper()
    scraper.fetch_offers("08032-230")
