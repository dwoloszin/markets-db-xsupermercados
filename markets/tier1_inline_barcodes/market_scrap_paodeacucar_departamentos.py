import html as html_module
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from db.db_manager import DatabaseManager


class PaoDeAcucarDepartamentosScraper:
    """Scraper for Pão de Açúcar using the GPA Linx Search API directly.

    API endpoint: POST https://api.vendas.gpa.digital/pa/search/category-page
    Body: { "partner": "linx", "page": N, "resultsPerPage": 48,
            "multiCategory": "<slug>", "sortBy": "relevance",
            "department": "ecom", "storeId": 483, "customerPlus": true }

    Response: { "page", "totalPages", "totalProducts", "products": [...] }

    No Playwright needed — plain HTTP POST with pagination.
    storeId=483 is the Pão de Açúcar São Paulo online store.
    Products have promo prices in productPromotion.unitPrice.
    Barcodes are not in the list API — fetched from PDP via JSON-LD.
    """

    API_URL = "https://api.vendas.gpa.digital/pa/search/category-page"
    BASE_URL = "https://www.paodeacucar.com"
    IMAGE_CDN = "https://www.paodeacucar.com"
    STORE_ID = "paodeacucar"
    GPA_STORE_ID = 101           # numeric storeId for Pão de Açúcar online store
    PAGE_SIZE = 48              # max tested; API uses 21 by default, we request more

    # Categories discovered from sitemap; used as fallback
    DEFAULT_CATEGORY_SLUGS = [
        "alimentos", "bebidas", "limpeza", "descartaveis",
        "bebe-e-crianca", "perfumaria", "petshop", "bazar",
        "textil", "caras-do-brasil",
    ]

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
        })
        self.market_name = "Pão de Açúcar"
        # PDP enrichment is now handled by extra_barcode_enrich.run()
        # called separately from main.py after save_offers

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Category discovery
    # ------------------------------------------------------------------

    def discover_category_slugs(self) -> List[str]:
        """Return top-level category slugs from sitemap or defaults."""
        sitemap_urls = [
            f"{self.BASE_URL}/sitemap/mapa-de-categorias",
            f"{self.BASE_URL}/sitemap.xml",
            f"{self.BASE_URL}/sitemap/category-0.xml",
        ]
        for sitemap_url in sitemap_urls:
            try:
                r = self.session.get(sitemap_url, timeout=20)
                if r.status_code == 200 and r.text:
                    # Try /categoria/ pattern first
                    matches = re.findall(
                        r'/categoria/([^\s<>#?/"]+)',
                        r.text, flags=re.IGNORECASE,
                    )
                    if not matches:
                        # Try <loc> tags with department slugs
                        locs = re.findall(r'<loc>(.*?)</loc>', r.text)
                        base = self.BASE_URL.rstrip("/")
                        for loc in locs:
                            path = loc.strip().replace(base, "").lstrip("/")
                            if path and "/" not in path and len(path) > 2:
                                matches.append(path)
                    seen: set = set()
                    slugs: List[str] = []
                    for slug in matches:
                        if slug not in seen:
                            seen.add(slug)
                            slugs.append(slug)
                    if slugs:
                        print(f"PdA: discovered {len(slugs)} category slugs from {sitemap_url}.")
                        return slugs
            except Exception as exc:
                print(f"PdA: sitemap error ({sitemap_url}): {exc}")
        print("PdA: using DEFAULT_CATEGORY_SLUGS.")
        return list(self.DEFAULT_CATEGORY_SLUGS)

    # ------------------------------------------------------------------
    # API pagination
    # ------------------------------------------------------------------

    def _fetch_slug_page(self, slug: str, page: int, store_id: int = None) -> Dict[str, Any]:
        """POST one page request to the GPA Linx API."""
        payload = {
            "partner": "linx",
            "page": page,
            "resultsPerPage": self.PAGE_SIZE,
            "multiCategory": slug,
            "sortBy": "relevance",
            "department": "ecom",
            "storeId": store_id or self.GPA_STORE_ID,
            "customerPlus": True,
        }
        for attempt in range(3):
            try:
                r = self.session.post(self.API_URL, json=payload, timeout=25)
                if r.status_code == 200:
                    return r.json() or {}
                if r.status_code in (429, 503):
                    wait = int(r.headers.get("Retry-After", 5)) * (attempt + 1)
                    print(f"  PdA {slug} p{page} HTTP {r.status_code}, waiting {wait}s")
                    time.sleep(wait)
                else:
                    print(f"  PdA {slug} p{page} HTTP {r.status_code}")
                    return {}
            except Exception as exc:
                print(f"  PdA {slug} p{page} error: {exc}")
                if attempt < 2:
                    time.sleep(2)
        return {}

    # Fallback storeIds to try if primary GPA_STORE_ID returns no results
    FALLBACK_STORE_IDS = [532, 483, 1]

    def _fetch_all_slug_products(
        self,
        slug: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Paginate the GPA API for one category slug, return all raw items.
        Tries GPA_STORE_ID first, then fallback IDs if no results returned.
        """
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        all_items: List[Dict[str, Any]] = []
        seen_ids: set = set()

        # Determine working storeId — try primary first, then fallbacks
        working_store_id = self.GPA_STORE_ID
        test_data = self._fetch_slug_page(slug, 1, store_id=self.GPA_STORE_ID)
        if not (test_data.get("products") or test_data.get("totalProducts")):
            for fallback_id in self.FALLBACK_STORE_IDS:
                if fallback_id == self.GPA_STORE_ID:
                    continue
                test_data = self._fetch_slug_page(slug, 1, store_id=fallback_id)
                if test_data.get("products") or test_data.get("totalProducts"):
                    print(f"  PdA {slug}: storeId={fallback_id} works (primary {self.GPA_STORE_ID} empty)")
                    working_store_id = fallback_id
                    break

        page = 1

        # Use pre-fetched first page if we already have it
        first_page_data = test_data if page == 1 else None

        while True:
            data = first_page_data if first_page_data is not None else self._fetch_slug_page(slug, page, store_id=working_store_id)
            first_page_data = None
            if not data:
                break

            total_pages = data.get("totalPages") or 1
            total_products = data.get("totalProducts") or 0
            items = data.get("products") or []

            if page == 1:
                print(f"  PdA {slug}: {total_products} total products, {total_pages} pages")

            new_count = 0
            for item in items:
                pid = str(item.get("id") or "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_items.append(item)
                    new_count += 1
                    if max_items is not None and len(all_items) >= max_items:
                        break

            print(f"    page={page}/{total_pages} items={len(items)} new={new_count} total={len(all_items)}")

            if not items or new_count == 0:
                break
            if page >= total_pages:
                break
            if max_items is not None and len(all_items) >= max_items:
                break

            page += 1
            time.sleep(0.15)

        return all_items
    # ------------------------------------------------------------------

    def _standardize_product(
        self, item: Dict[str, Any], zip_code: str
    ) -> Optional[Dict[str, Any]]:
        name = str(item.get("name") or "").strip()
        if not name:
            return None

        offer_id = self.db.build_offer_id("paodeacucar", self.STORE_ID, None, None, name)
        if not offer_id:
            return None

        # Price: regular = item.price
        # Promo: productPromotion.unitPrice (if present and < regular price)
        regular_price = self._to_float(item.get("price"))
        promo_price = None
        promo_end_at = None
        app_exclusive = False

        promo = item.get("productPromotion") or {}
        offer_name_pda = None
        if promo:
            promo_val = self._to_float(promo.get("unitPrice"))
            if promo_val and regular_price and promo_val < regular_price:
                promo_price = promo_val
            promo_end_at = promo.get("endDate") or None
            app_exclusive = bool(promo.get("appExclusive", False))
            card_exclusive = bool(promo.get("cardExclusive", False))
            card_name = str(promo.get("cardName") or promo.get("card") or "").strip()
            promo_label = (
                promo.get("tagLabel") or promo.get("description") or
                promo.get("label") or promo.get("tag") or ""
            ).strip()
            discount_pct = promo.get("discountPercentage") or promo.get("discount")
            promo_min_q  = promo.get("minimumQuantity") or promo.get("minQuantity")
            parts = []
            if app_exclusive or card_exclusive:
                parts.append(f"Cartão {card_name}" if card_name else "App/Cartão exclusivo")
            if promo_label:
                parts.append(promo_label)
            elif discount_pct:
                qty_text = f" na {promo_min_q}ª un" if promo_min_q and int(promo_min_q) > 1 else ""
                parts.append(f"-{discount_pct}%{qty_text}")
            offer_name_pda = " | ".join(parts) if parts else None

        # Image: list of relative CDN paths → prepend base URL
        image_url = None
        images = item.get("productImages") or []
        if isinstance(images, list) and images:
            img_path = str(images[0])
            image_url = img_path if img_path.startswith("http") else f"{self.IMAGE_CDN}{img_path}"

        product_url = str(item.get("urlDetails") or "").strip() or None
        brand = str(item.get("brand") or "").strip() or None

        # stock: boolean in GPA API → store as 1/0 integer for DB
        stock_raw = item.get("stock")
        stock_general = 1 if stock_raw is True else (0 if stock_raw is False else None)

        return {
            "id": offer_id,
            "product_name": name,
            "brand": brand,
            "description": None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": None,
            "unit": None,
            "gtin": None,
            "barcode": None,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": None,
            "stock_general": stock_general,
            "sold_quantity": None,
            "offer_name": offer_name_pda,
            "offer_tag": (promo.get("tag") or promo.get("type")) if promo else None,
            "app_membership_required": app_exclusive,
            "promo_end_at": promo_end_at,
            "last_updated": datetime.now().isoformat(),
            "store_id": self.STORE_ID,
            "zip_code": zip_code,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        print("Fetching Pão de Açúcar departamentos offers...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        slugs = self.discover_category_slugs()
        print(f"Pão de Açúcar: scraping {len(slugs)} categories via GPA API")

        all_products: Dict[str, Dict[str, Any]] = {}

        for slug in slugs:
            remaining = None
            if max_items is not None:
                remaining = max(0, max_items - len(all_products))
                if remaining <= 0:
                    break

            raw_items = self._fetch_all_slug_products(slug, limit=remaining)
            new_count = 0
            for item in raw_items:
                offer = self._standardize_product(item, zip_code)
                if not offer:
                    continue
                oid = offer["id"]
                if oid not in all_products:
                    new_count += 1
                all_products[oid] = offer
                if max_items is not None and len(all_products) >= max_items:
                    break

            print(f"PdA {slug}: {len(raw_items)} items ({new_count} new), global total={len(all_products)}")
            if max_items is not None and len(all_products) >= max_items:
                break
            time.sleep(0.3)

        result = list(all_products.values())
        if max_items is not None:
            result = result[:max_items]
        print(f"Pão de Açúcar: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = PaoDeAcucarDepartamentosScraper()
    offers = scraper.fetch_offers("08032-230", limit=100)
    print(f"\nTotal: {len(offers)} offers")
    for o in offers[:3]:
        print(o)
