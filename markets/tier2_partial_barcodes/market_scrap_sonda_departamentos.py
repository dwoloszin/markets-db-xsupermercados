import html as html_module
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from db.db_manager import DatabaseManager


class SondaDepartamentosScraper:
    """Scraper for all products from Sonda Delivery by iterating department/category pages.

    Products are server-side rendered in HTML pages. Pagination is via
    ``linkPaginaProxima`` next-page links (~15 products per page).
    Categories are discovered from the main navigation as "Ver Todos" links.
    """

    BASE_URL = "https://www.sondadelivery.com.br"
    STORE_ID = "sondadelivery"

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9",
            }
        )
        self.market_name = "Sonda"

    def _get_page(self, url: str, max_retries: int = 3) -> Optional[str]:
        """Fetch a page with retry on rate-limit or server errors."""
        delay = 5
        for attempt in range(1, max_retries + 1):
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (429, 503):
                    wait = max(int(r.headers.get("Retry-After", delay)), delay)
                    print(
                        f"Sonda: HTTP {r.status_code} on {url} "
                        f"(attempt {attempt}/{max_retries}), waiting {wait}s..."
                    )
                    time.sleep(wait)
                    delay = min(delay * 2, 60)
                else:
                    print(f"Sonda: HTTP {r.status_code} on {url}")
                    return None
            except Exception as exc:
                print(f"Sonda: error fetching {url}: {exc}")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        return None

    def discover_categories(self) -> List[Tuple[str, str]]:
        """Discover all top-level department categories from the main navigation.

        Returns list of (url_slug, display_name) tuples.
        "Ver Todos" anchors in the navigation identify department-level category pages.
        """
        page_html = self._get_page(f"{self.BASE_URL}/delivery")
        if not page_html:
            return []

        # Find all "Ver Todos" category links in the navigation
        slugs = re.findall(
            r'href="/delivery/categoria/([^"]+)"[^>]*>\s*[Vv]er [Tt]odos\s*</a>',
            page_html,
        )
        seen: set = set()
        result: List[Tuple[str, str]] = []
        for slug in slugs:
            if slug in seen:
                continue
            seen.add(slug)
            decoded_slug = urllib.parse.unquote(slug)
            clean_slug = decoded_slug[:-2] if decoded_slug.endswith("-l") else decoded_slug
            display_name = clean_slug.replace("_", " ").replace("-", " ").strip()
            result.append((slug, display_name))
        return result

    def _extract_products(self, page_html: str, zip_code: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse products from a Sonda Delivery category page.

        Product data is embedded in HTML via:
        - ``ViewItemAnalytics(price, 'sku', 'name')`` onclick attributes
          (single quotes are HTML-entity encoded as ``&#39;``)
        - Product image CDN paths: ``/sku/<sku>/<size>/<ean>.<ext>``
          which expose EAN barcodes when the filename is a 7–14-digit number
        - Product page hrefs: ``/delivery/produto/<slug>/<sku>``
        """
        decoded = html_module.unescape(page_html)

        # price, sku, name
        analytics = re.findall(
            r"ViewItemAnalytics\(([0-9.]+),\s*'([^']+)',\s*'([^']+)'\)",
            decoded,
        )

        # EAN from image CDN path
        sku_ean_pairs = re.findall(r"/sku/(\d+)/\d+/([0-9]{7,14})[.]", decoded)
        ean_map: Dict[str, str] = {}
        for sku, ean in sku_ean_pairs:
            if sku not in ean_map:
                ean_map[sku] = ean

        # Product page URL from href
        sku_slugs = re.findall(r'href="/delivery/produto/([^/]+)/(\d+)"', decoded)
        url_map: Dict[str, str] = {}
        for slug, sku in sku_slugs:
            if sku not in url_map:
                url_map[sku] = f"{self.BASE_URL}/delivery/produto/{slug}/{sku}"

        # Extract promotional prices from data-preco-por attributes.
        # Sonda renders: data-sku="123" ... data-preco-por="12.90"
        promo_map: Dict[str, float] = {}
        for sku_p, price_p in re.findall(
            r'data-preco-por="([0-9]+[.,][0-9]+)"[^>]*data-sku="(\d+)"'
            r'|data-sku="(\d+)"[^>]*data-preco-por="([0-9]+[.,][0-9]+)"',
            decoded,
        ):
            pass  # handled below via named approach
        # Simpler two-pass: find all (sku, promo_price) pairs near each other
        for block in re.finditer(
            r'data-sku="(\d+)"(?:[^"]*"[^"]*"){0,20}?[^>]*data-preco-por="([0-9,\.]+)"',
            decoded,
        ):
            try:
                promo_map[block.group(1)] = float(block.group(2).replace(",", "."))
            except ValueError:
                pass

        # Each product appears twice in ``analytics`` (main grid + recommendation zone).
        # Deduplicate by SKU, keeping first occurrence.
        seen_skus: set = set()
        products: List[Dict[str, Any]] = []
        for price_str, sku, name in analytics:
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            try:
                regular_price = float(price_str)
            except ValueError:
                continue
            if regular_price <= 0:
                continue

            ean_raw = ean_map.get(sku)
            barcode = self.db.normalize_barcode(ean_raw) if ean_raw else None
            offer_id = self.db.build_offer_id("sonda", self.STORE_ID, barcode, ean_raw, name)
            if not offer_id:
                continue

            products.append(
                {
                    "id": offer_id,
                    "product_name": name,
                    "brand": None,
                    "description": None,
                    "regular_price": regular_price,
                    "promo_price": (
                        promo_map[sku]
                        if sku in promo_map and promo_map[sku] < regular_price
                        else None
                    ),
                    "promo_min_quantity": None,
                    "unit": None,
                    "gtin": ean_raw,
                    "barcode": barcode,
                    "product_url": url_map.get(sku),
                    "image_url": None,
                    "stock_balance": None,
                    "stock_general": None,
                    "sold_quantity": None,
                    "offer_name": None,
                    "offer_tag": None,
                    "app_membership_required": False,
                    "promo_end_at": None,
                    "last_updated": datetime.now().isoformat(),
                    "store_id": self.STORE_ID,
                    "zip_code": zip_code,
                }
            )
        return products

    def _next_page_url(self, page_html: str) -> Optional[str]:
        """Return the URL of the next page from pagination, or None if last page."""
        decoded = html_module.unescape(page_html)
        m = re.search(
            r'id="ctl00_conteudo_linkPaginaProxima"[^>]*href="([^"]+)"',
            decoded,
        )
        if m:
            return self.BASE_URL + m.group(1)
        return None

    def _fetch_category_products(
        self,
        slug: str,
        *,
        zip_code: Optional[str] = None,
        max_pages: int = 200,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Paginate through all pages of one category and return unique products."""
        url: Optional[str] = f"{self.BASE_URL}/delivery/categoria/{slug}"
        products_by_id: Dict[str, Dict[str, Any]] = {}
        page = 1
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        while url and page <= max_pages:
            page_html = self._get_page(url)
            if not page_html:
                break

            page_products = self._extract_products(page_html, zip_code=zip_code)
            new_count = 0
            for p in page_products:
                pid = p["id"]
                if pid not in products_by_id:
                    new_count += 1
                products_by_id[pid] = p
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            print(
                f"  Sonda {slug} page={page}: "
                f"items={len(page_products)} new={new_count} total={len(products_by_id)}"
            )

            # Stop early if no new products were found on this page
            if new_count == 0:
                break
            if max_items is not None and len(products_by_id) >= max_items:
                break

            url = self._next_page_url(page_html)
            page += 1
            if url:
                time.sleep(0.5)

        return list(products_by_id.values())

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch all products from all Sonda Delivery departments."""
        print("Fetching Sonda Delivery departamentos offers...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        categories = self.discover_categories()
        if not categories:
            print("Sonda Delivery: no categories discovered, aborting.")
            return []

        print(f"Sonda Delivery: discovered {len(categories)} departments")
        for i, (slug, name) in enumerate(categories, 1):
            print(f"  [{i}] {name} ({slug})")

        all_products: Dict[str, Dict[str, Any]] = {}
        for slug, name in categories:
            remaining_limit = None
            if max_items is not None:
                remaining_limit = max(0, max_items - len(all_products))
                if remaining_limit <= 0:
                    break

            cat_products = self._fetch_category_products(slug, zip_code=zip_code, limit=remaining_limit)
            new_in_cat = sum(1 for p in cat_products if p["id"] not in all_products)
            for p in cat_products:
                all_products[p["id"]] = p
                if max_items is not None and len(all_products) >= max_items:
                    break
            print(
                f"Sonda {name}: {len(cat_products)} items "
                f"({new_in_cat} new), global total={len(all_products)}"
            )
            if max_items is not None and len(all_products) >= max_items:
                break

        result = list(all_products.values())
        if max_items is not None:
            result = result[:max_items]
        print(f"Sonda Delivery: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = SondaDepartamentosScraper()
    offers = scraper.fetch_offers("08032-230")
    print(f"\nTotal: {len(offers)} offers")
    for o in offers[:3]:
        print(o)
