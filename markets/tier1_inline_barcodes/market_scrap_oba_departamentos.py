"""
market_scrap_oba_departamentos.py — Oba Hortifruti scraper.

Oba Hortifruti runs on the VTEX platform.

Strategy (in order):
  1. VTEX category tree API  → discover all leaf categories
  2. /api/catalog_system/pub/products/search  (fast REST, no auth needed)
  3. /api/io/_v/api/intelligent-search/product_search  (fallback)
  4. Playwright interception with VTEX-aware heuristics (last resort)

The old Playwright-only approach failed because:
  - _is_product_response() didn't recognize VTEX field names (productId, productName)
  - The wait time (3s) was too short for VTEX SPAs
  - VTEX catalog responses use nested items[].sellers[].commertialOffer for prices
"""

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from db.db_manager import DatabaseManager


class ObaDepartamentosScraper:
    BASE_URL  = "https://www.obahortifruti.com.br"
    STORE_ID  = "obahortifruti"
    PAGE_SIZE = 50

    DEFAULT_CATEGORY_SLUGS = [
        "hortifruti", "frutas", "verduras", "legumes",
        "mercearia", "bebidas", "laticinios-e-frios",
        "padaria", "carnes", "congelados",
        "limpeza", "higiene-e-perfumaria",
    ]

    def __init__(self):
        self.db      = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Referer":         "https://www.obahortifruti.com.br/",
        })
        self.market_name = "Oba Hortifruti"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace("R$","").replace("\xa0","").replace(" ","")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Category discovery via VTEX category tree
    # ------------------------------------------------------------------

    def _discover_vtex_categories(self) -> List[Dict[str, Any]]:
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/category/tree/3",
                timeout=20,
            )
            if r.status_code == 200:
                tree = r.json()
                cats: List[Dict] = []
                self._walk_tree(tree, cats)
                if cats:
                    print(f"Oba: discovered {len(cats)} categories from VTEX tree.")
                    return cats
        except Exception as exc:
            print(f"Oba: category tree error: {exc}")

        print("Oba: using DEFAULT_CATEGORY_SLUGS fallback.")
        return [{"id": None, "name": s, "url": f"{self.BASE_URL}/{s}"}
                for s in self.DEFAULT_CATEGORY_SLUGS]

    def _walk_tree(self, node: Any, out: List[Dict]) -> None:
        if isinstance(node, list):
            for n in node:
                self._walk_tree(n, out)
        elif isinstance(node, dict):
            children = node.get("children") or []
            if not children:
                cat_id = node.get("id") or node.get("Id")
                name   = node.get("name") or node.get("Name") or ""
                url    = node.get("url") or node.get("Url") or ""
                if cat_id or url:
                    out.append({"id": cat_id, "name": name, "url": url})
            else:
                for c in children:
                    self._walk_tree(c, out)

    # ------------------------------------------------------------------
    # Strategy 1: VTEX catalog_system REST
    # ------------------------------------------------------------------

    def _fetch_catalog_api(
        self, cat_id: Optional[int], cat_url: str, max_items: Optional[int]
    ) -> List[Dict]:
        products: List[Dict] = []
        page = 0
        slug = cat_url.rstrip("/").split("/")[-1] if cat_url else "?"

        if not cat_id:
            m = re.search(r"/(\d+)/?$", cat_url)
            if m:
                cat_id = int(m.group(1))

        while True:
            from_idx = page * self.PAGE_SIZE
            to_idx   = from_idx + self.PAGE_SIZE - 1
            params: Dict = {"_from": from_idx, "_to": to_idx}
            if cat_id:
                params["fq"] = f"C:/{cat_id}/"

            try:
                r = self.session.get(
                    f"{self.BASE_URL}/api/catalog_system/pub/products/search",
                    params=params, timeout=20,
                )
            except Exception as exc:
                print(f"  Oba {slug} catalog error: {exc}")
                break

            if r.status_code != 200:
                break
            try:
                items = r.json()
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break

            products.extend(items)
            page += 1

            resources = r.headers.get("resources", "")
            if resources:
                try:
                    total = int(resources.split("/")[-1])
                    if from_idx + len(items) >= total:
                        break
                except ValueError:
                    pass

            if len(items) < self.PAGE_SIZE:
                break
            if max_items and len(products) >= max_items:
                break
            time.sleep(0.15)

        return products

    # ------------------------------------------------------------------
    # Strategy 2: VTEX Intelligent Search
    # ------------------------------------------------------------------

    def _fetch_intelligent_search(self, slug: str, max_items: Optional[int]) -> List[Dict]:
        products: List[Dict] = []
        page = 1
        while True:
            try:
                r = self.session.get(
                    f"{self.BASE_URL}/api/io/_v/api/intelligent-search/product_search",
                    params={"query": slug, "page": page, "count": self.PAGE_SIZE,
                            "hideUnavailableItems": "false"},
                    timeout=20,
                )
            except Exception as exc:
                print(f"  Oba IS {slug} error: {exc}")
                break
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            items = (data.get("products")
                     or (data.get("data") or {}).get("productSearch", {}).get("products")
                     or [])
            if not items:
                break
            products.extend(items)
            page += 1
            if len(items) < self.PAGE_SIZE or (max_items and len(products) >= max_items):
                break
            time.sleep(0.15)
        return products

    # ------------------------------------------------------------------
    # Strategy 3: Playwright interception (VTEX-aware)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_vtex_product_response(body: Any) -> bool:
        if isinstance(body, list) and body and isinstance(body[0], dict):
            keys = set(body[0].keys())
            return bool(keys & {
                "productId","productName","brand","items","linkText",
                "id","nome","name","preco","price","codigo_barras","ean","gtin","barcode",
            })
        if isinstance(body, dict):
            for key in ("products","data","items","resultado","produtos"):
                val = body.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return True
        return False

    @staticmethod
    def _extract_vtex_items(body: Any) -> List[Dict]:
        if isinstance(body, list):
            return [i for i in body if isinstance(i, dict)]
        if isinstance(body, dict):
            for key in ("products","data","items","resultado","produtos"):
                val = body.get(key)
                if isinstance(val, list):
                    return [i for i in val if isinstance(i, dict)]
        return []

    def _playwright_fallback(self, cat_url: str, max_items: Optional[int]) -> List[Dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        all_items: List[Dict] = []
        seen: set = set()
        batch: List[Dict] = []

        def _on_response(response) -> None:
            url = response.url
            if not any(h in url for h in [
                "obahortifruti","vtex","/_v/","intelligent-search",
                "/api/","/products","/catalog","/search","graphql",
            ]):
                return
            if response.status != 200:
                return
            if "json" not in response.headers.get("content-type",""):
                return
            try:
                body = response.json()
            except Exception:
                return
            if self._is_vtex_product_response(body):
                batch.extend(self._extract_vtex_items(body))

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 900}, locale="pt-BR",
            )
            page = ctx.new_page()
            page.set_default_timeout(45_000)
            page.on("response", _on_response)

            for page_num in range(1, 20):
                batch.clear()
                target = cat_url if page_num == 1 else f"{cat_url}?page={page_num}"
                try:
                    page.goto(target, wait_until="networkidle", timeout=40_000)
                except Exception:
                    try:
                        page.goto(target, wait_until="domcontentloaded", timeout=35_000)
                        page.wait_for_timeout(6000)  # extra wait for VTEX SPA
                    except Exception:
                        break

                page.wait_for_timeout(5000)
                new = 0
                for item in batch:
                    pid = (item.get("productId") or item.get("id") or
                           item.get("sku") or item.get("productName"))
                    key = str(pid) if pid else None
                    if key and key not in seen:
                        seen.add(key)
                        all_items.append(item)
                        new += 1

                slug = cat_url.rstrip("/").split("/")[-1]
                print(f"  Oba {slug} page={page_num}: intercepted={len(batch)} new={new} total={len(all_items)}")
                if not batch or new == 0:
                    break
                if max_items and len(all_items) >= max_items:
                    break

            browser.close()
        return all_items

    # ------------------------------------------------------------------
    # Standardize VTEX product → offer dict
    # ------------------------------------------------------------------

    def _standardize(self, item: Dict, zip_code: str) -> Optional[Dict]:
        name = (
            item.get("productName") or item.get("nome") or
            item.get("name") or item.get("title") or ""
        ).strip()
        if not name:
            return None

        native_id = (item.get("productId") or item.get("id") or
                     item.get("produto_id") or item.get("sku"))

        raw_barcode = None
        regular_price = None
        promo_price   = None
        image_url     = None
        product_url   = None

        # VTEX catalog_system: prices/images/ean are nested in items[].sellers[]
        vtex_skus = item.get("items") or []
        if vtex_skus and isinstance(vtex_skus, list):
            sku = vtex_skus[0]
            raw_barcode = sku.get("ean")
            if not raw_barcode:
                ref = sku.get("referenceId") or []
                if ref and isinstance(ref, list):
                    raw_barcode = ref[0].get("Value")
            sellers = sku.get("sellers") or []
            if sellers:
                co = sellers[0].get("commertialOffer") or {}
                regular_price = self._to_float(co.get("ListPrice") or co.get("Price"))
                promo_price   = self._to_float(co.get("Price"))
                if promo_price and regular_price and promo_price >= regular_price:
                    promo_price = None
            images = sku.get("images") or []
            if images:
                image_url = images[0].get("imageUrl")

        # Fallbacks for other API shapes
        if not raw_barcode:
            raw_barcode = (item.get("ean") or item.get("codigo_barras") or
                           item.get("gtin") or item.get("barcode"))
        if regular_price is None:
            regular_price = self._to_float(item.get("preco") or item.get("price") or item.get("valor"))
        if not image_url:
            image_url = item.get("imagem") or item.get("image") or item.get("imageUrl")

        link = item.get("linkText") or item.get("link") or item.get("slug") or ""
        if link:
            product_url = link if link.startswith("http") else f"{self.BASE_URL}/{link.lstrip('/')}/p"

        brand = item.get("brand") or item.get("marca")
        if isinstance(brand, dict):
            brand = brand.get("name") or brand.get("nome")

        gtin_text = str(raw_barcode).strip() if raw_barcode else None
        barcode   = self.db.normalize_barcode(gtin_text) if gtin_text else None
        offer_id  = self.db.build_offer_id("oba", self.STORE_ID, barcode, gtin_text, name)
        if not offer_id:
            return None

        return {
            "id": offer_id, "product_name": name,
            "brand": str(brand).strip() if brand else None,
            "description": item.get("description") or item.get("metaTagDescription"),
            "regular_price": regular_price, "promo_price": promo_price,
            "promo_min_quantity": None, "unit": item.get("unitMultiplier"),
            "gtin": gtin_text, "barcode": barcode,
            "product_url": product_url, "image_url": str(image_url) if image_url else None,
            "stock_balance": None, "stock_general": None, "sold_quantity": None,
            "offer_name": None, "offer_tag": None, "app_membership_required": False,
            "promo_end_at": None, "last_updated": datetime.now().isoformat(),
            "store_id": self.STORE_ID, "zip_code": zip_code,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict]:
        print("Fetching Oba Hortifruti departamentos offers...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        categories = self._discover_vtex_categories()

        print(f"Oba Hortifruti: scraping {len(categories)} categories")
        all_products: Dict[str, Dict] = {}

        for cat in categories:
            if max_items and len(all_products) >= max_items:
                break

            cat_id  = cat.get("id")
            cat_url = cat.get("url", "")
            slug    = cat_url.rstrip("/").split("/")[-1] if cat_url else cat.get("name","?")
            remaining = (max_items - len(all_products)) if max_items else None

            raw = self._fetch_catalog_api(cat_id, cat_url, remaining)
            strategy = "catalog_api"

            if not raw and slug:
                raw = self._fetch_intelligent_search(slug, remaining)
                strategy = "intelligent_search"

            if not raw and cat_url:
                print(f"  Oba {slug}: REST APIs returned nothing — trying Playwright...")
                raw = self._playwright_fallback(cat_url, remaining)
                strategy = "playwright"

            new = 0
            for item in raw:
                offer = self._standardize(item, zip_code)
                if not offer:
                    continue
                oid = offer["id"]
                if oid not in all_products:
                    new += 1
                all_products[oid] = offer
                if max_items and len(all_products) >= max_items:
                    break

            print(f"Oba {slug}: {len(raw)} raw ({new} new) via {strategy}, total={len(all_products)}")
            time.sleep(0.3)

        result = list(all_products.values())[:max_items] if max_items else list(all_products.values())
        print(f"Oba Hortifruti: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = ObaDepartamentosScraper()
    offers = scraper.fetch_offers("08032-230", limit=30)
    print(f"\nTotal: {len(offers)} offers")
    for o in offers[:3]:
        print(json.dumps(o, ensure_ascii=False, indent=2))
