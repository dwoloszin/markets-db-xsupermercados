import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import requests

from db.db_manager import DatabaseManager


class TendaAtacadoDepartamentosScraper:
    """Tenda Atacado scraper — CEP-based store resolution, Java/Spring backend.

    Backend:  https://api.tendaatacado.com.br/api
    Platform: Stoom (custom e-commerce) + Java Spring Boot
    Barcodes: None inline → Tier 3 (catalog cross-reference only)
    """

    MARKET_NAME = "Tenda Atacado"
    ID_PREFIX = "tenda"
    API_BASE = "https://api.tendaatacado.com.br/api"
    STORE_LOOKUP_URL = API_BASE + "/public/branch/zip/{cep}"
    DEPARTMENTS_URL   = API_BASE + "/public/store/departments"
    ALL_CATEGORIES_URL = API_BASE + "/public/store/all-categories"
    SEARCH_URL        = API_BASE + "/public/store/search"

    # Known fallback department links (from homepage _next/data probe 2026-03)
    DEFAULT_DEPARTMENTS = [
        {"link": "mercearia",          "name": "Mercearia"},
        {"link": "higiene-e-perfumaria","name": "Higiene e Perfumaria"},
        {"link": "bebidas",             "name": "Bebidas"},
        {"link": "limpeza",             "name": "Limpeza"},
        {"link": "frios-e-laticinios",  "name": "Frios e Laticínios"},
        {"link": "bazar",               "name": "Bazar"},
        {"link": "bomboniere",          "name": "Bomboniere"},
        {"link": "congelados",          "name": "Congelados"},
        {"link": "carnes-aves-e-peixes","name": "Carnes, Aves e Peixes"},
        {"link": "produtos-select",     "name": "Marca Própria"},
        {"link": "hortifruti",          "name": "Hortifrúti"},
        {"link": "paes-e-bolos",        "name": "Pães e Bolos"},
        {"link": "bebe",                "name": "Bebê"},
        {"link": "pet-shop",            "name": "Pet Shop"},
    ]

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.tendaatacado.com.br",
            "Referer": "https://www.tendaatacado.com.br/",
            "Web-Platform": "web-desktop",
        })
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------- store resolution

    def resolve_store(self, zip_code: str) -> Optional[str]:
        """Return the nearest store ID (integer, as string) for the given CEP."""
        self._resolved_store_metadata = None
        cep = zip_code.replace("-", "").strip()
        url = self.STORE_LOOKUP_URL.format(cep=cep)
        try:
            resp = self.session.get(url, timeout=10)
            print(f"Tenda resolve_store: HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(f"  body: {resp.text[:200]}")
                return None
            stores = resp.json()
            if not stores:
                print("  Tenda: no stores returned for CEP")
                return None
            # Stores are already sorted by distance (ascending)
            nearest = stores[0]
            store_id = str(nearest.get("id", ""))
            address = nearest.get("address") or {}
            self._resolved_store_metadata = {
                "store_name": nearest.get("name"),
                "store_address": address.get("addressLine1"),
                "store_city": address.get("city"),
                "store_state": None,
                "latitude": address.get("latitude"),
                "longitude": address.get("longitude"),
                "store_payload": nearest,
            }
            print(
                f"Tenda store resolved: id={store_id} "
                f"name={self._resolved_store_metadata['store_name']} "
                f"city={self._resolved_store_metadata['store_city']} "
                f"dist={nearest.get('distance', '?')}m"
            )
            # Pass branch cookie for all subsequent requests
            self.session.cookies.set("_Tendaatacado-branchID", store_id)
            return store_id
        except Exception as exc:
            print(f"Tenda resolve_store exception: {exc}")
            return None

    # -------------------------------------------------------- department discovery

    # Alphabet sweep: querying each letter catches products that don't appear in category
    # searches. Each letter query returns up to 500 products (25 pages × 20); combined
    # with per-product deduplication this gives comprehensive catalog coverage.
    # Portuguese supermarket products start with every letter so all 26 are useful.
    _ALPHABET_QUERIES = list("abcdefghijklmnopqrstuvwxyz")

    # Common product-keyword queries that catch large groups not covered by letters alone
    _KEYWORD_QUERIES = [
        "leite", "carne", "frango", "pão", "arroz", "feijão", "oleo", "azeite",
        "cafe", "açucar", "sal", "macarrao", "molho", "biscoito", "chocolate",
        "sorvete", "queijo", "iogurte", "manteiga", "presunto",
        "cerveja", "refrigerante", "suco", "agua", "vinho",
        "sabao", "detergente", "shampoo", "desodorante", "creme",
        "fralda", "papel", "esponja", "saco",
    ]

    def discover_search_queries(self) -> List[str]:
        """Return a list of search query strings that collectively cover all products.

        Strategy (three layers for maximum coverage):
        1. Department names (14 broad categories) — fast first sweep
        2. Subcategory names from /all-categories tree — mid-level granularity
        3. Single alphabet letters a–z — sweeps everything missed by names
           (each returns up to 500 products, dedup by product ID ensures no double-counting)

        The alphabet sweep is the key to getting from ~1800 to ~10,000+ products.
        """
        dept_queries = [d["link"] for d in self.DEFAULT_DEPARTMENTS]
        subcategory_queries: List[str] = []

        try:
            resp = self.session.get(self.ALL_CATEGORIES_URL, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    top_nodes = data
                elif isinstance(data, dict):
                    top_nodes = (
                        data.get("categories")
                        or data.get("items")
                        or data.get("data")
                        or []
                    )
                else:
                    top_nodes = []

                dept_link_set = {d["link"] for d in self.DEFAULT_DEPARTMENTS}

                def _walk(nodes):
                    for node in (nodes or []):
                        if not isinstance(node, dict):
                            continue
                        link = (node.get("link") or "").strip("/")
                        # Collect any non-department node with a simple (non-hyphenated)
                        # link — these work best as search query terms
                        if link and link not in dept_link_set and "-" not in link:
                            subcategory_queries.append(link)
                        children = (
                            node.get("children")
                            or node.get("categories")
                            or node.get("subcategories")
                            or []
                        )
                        _walk(children)

                _walk(top_nodes)
        except Exception as exc:
            print(f"Tenda all-categories exception: {exc}")

        # Deduplicate while preserving order
        seen: set = set()
        all_queries: List[str] = []
        for q in dept_queries + subcategory_queries + self._KEYWORD_QUERIES + self._ALPHABET_QUERIES:
            if q not in seen:
                seen.add(q)
                all_queries.append(q)

        print(
            f"Tenda search queries: {len(dept_queries)} dept + "
            f"{len(subcategory_queries)} subcategory + "
            f"{len(self._KEYWORD_QUERIES)} keyword + "
            f"{len(self._ALPHABET_QUERIES)} alphabet = {len(all_queries)} total"
        )
        return all_queries

    # ------------------------------------------------------------ price extraction

    @staticmethod
    def _extract_price_tiers(product: Dict[str, Any]) -> tuple:
        """Return (regular_price, promo_price, promo_min_quantity, offer_tag).

        Price hierarchy (lowest → most attractive):
          1. product.price               — web price (always present)
          2. wholesalePrices[n].price    — bulk discount (min N units)
          3. promotions[DESCONTOU].price — app-only discount
        """
        regular_price = TendaAtacadoDepartamentosScraper._to_float(product.get("price"))

        # Wholesale prices (bulk quantity discounts)
        wholesale = product.get("wholesalePrices") or []
        best_wholesale: Optional[float] = None
        wholesale_min_qty: Optional[int] = None
        if wholesale and isinstance(wholesale, list):
            # Pick the tier with the lowest price
            best = min(
                wholesale,
                key=lambda w: float(w.get("price") or float("inf")),
                default=None,
            )
            if best:
                best_wholesale = TendaAtacadoDepartamentosScraper._to_float(best.get("price"))
                wholesale_min_qty = best.get("minQuantity")

        # App promotions
        promotions = product.get("promotions") or []
        app_price: Optional[float] = None
        offer_tag: Optional[str] = None
        if isinstance(promotions, list):
            for promo in promotions:
                if not isinstance(promo, dict):
                    continue
                p = TendaAtacadoDepartamentosScraper._to_float(promo.get("price"))
                promo_type = str(promo.get("type") or "")
                if p and (app_price is None or p < app_price):
                    app_price = p
                    if "PLUS" in promo_type.upper():
                        offer_tag = "App + Tenda Card"
                    else:
                        offer_tag = "App"

        # Determine best promo (non-app wholesale vs app)
        if best_wholesale and (app_price is None or best_wholesale <= app_price):
            promo_price = best_wholesale
            promo_min_qty = wholesale_min_qty if wholesale_min_qty and wholesale_min_qty > 1 else None
            promo_offer_tag = None
        elif app_price:
            promo_price = app_price
            promo_min_qty = None
            promo_offer_tag = offer_tag
        else:
            promo_price = regular_price
            promo_min_qty = None
            promo_offer_tag = None

        # Sanity: if promo >= regular, treat as no promo
        if promo_price and regular_price and promo_price >= regular_price and not promo_min_qty:
            promo_price = regular_price
            promo_offer_tag = None

        return regular_price, promo_price, promo_min_qty, promo_offer_tag

    # ---------------------------------------------------- paginated product fetch

    def _fetch_query_products(
        self,
        *,
        query: str,
        max_pages: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all products for a single search query, paginating until exhausted.

        The API always returns 20 results per page regardless of perPage param.
        Stop conditions: empty products list, page >= total_pages, or limit reached.
        """
        all_products: List[Dict[str, Any]] = []
        seen_ids: set = set()
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        for page in range(1, max_pages + 1):
            params: Dict[str, Any] = {"query": query, "page": page}
            try:
                resp = None
                for attempt in range(3):
                    resp = self.session.get(self.SEARCH_URL, params=params, timeout=25)
                    if resp.status_code not in (429, 500, 502, 503, 504):
                        break
                    wait = 1.5 * (attempt + 1)
                    print(
                        f"  query={query!r} page={page} HTTP {resp.status_code}, "
                        f"retrying in {wait:.1f}s"
                    )
                    time.sleep(wait)

                if resp is None or resp.status_code != 200:
                    status = resp.status_code if resp is not None else "n/a"
                    if resp is not None and resp.status_code == 400:
                        # API sometimes rejects queries with special chars — skip silently
                        pass
                    else:
                        print(f"Tenda query={query!r} page={page}: HTTP {status}, stopping")
                    break

                data = resp.json() or {}
                products_list = data.get("products") or []
                total_pages = int(data.get("total_pages") or data.get("totalPages") or 1)
                total_products = data.get("total_products") or data.get("totalProducts")

                if not products_list:
                    break

                new_count = 0
                for prod in products_list:
                    if not isinstance(prod, dict):
                        continue
                    prod_id = prod.get("id")
                    if prod_id is None or prod_id in seen_ids:
                        continue
                    seen_ids.add(prod_id)
                    all_products.append(prod)
                    new_count += 1
                    if max_items is not None and len(all_products) >= max_items:
                        break

                print(
                    f"Tenda query={query!r} page={page}/{total_pages}: "
                    f"new={new_count} total={len(all_products)}"
                    + (f"/{total_products}" if total_products else "")
                )

                if new_count == 0 or page >= total_pages:
                    break
                if max_items is not None and len(all_products) >= max_items:
                    break

                time.sleep(0.15)

            except Exception as exc:
                print(f"Tenda query={query!r} page={page} error: {exc}")
                break

        return all_products

    # ---------------------------------------------------------------- standardize

    def _standardize_product(
        self, product: Dict[str, Any], zip_code: str, store_id: str
    ) -> Optional[Dict[str, Any]]:
        regular_price, promo_price, promo_min_qty, offer_tag = self._extract_price_tiers(product)
        if regular_price is None and promo_price is None:
            return None

        prod_id = product.get("id")
        if prod_id is None:
            return None

        # Tenda API returns a `barcode` field with EAN-13 (when available)
        raw_barcode = product.get("barcode") or product.get("ean") or product.get("gtin")
        gtin_text = str(raw_barcode).strip() if raw_barcode else None
        barcode = self.db.normalize_barcode(gtin_text) if gtin_text else None

        offer_id = self.db.build_offer_id(self.ID_PREFIX, store_id, barcode, gtin_text, product.get("name"))
        if not offer_id:
            return None

        # Image: prefer first photo URL, fall back to thumbnail
        image_url = product.get("thumbnail")
        photos = product.get("photos") or []
        if photos and isinstance(photos[0], dict):
            image_url = photos[0].get("url") or photos[0].get("link") or image_url
        elif photos and isinstance(photos[0], str):
            image_url = photos[0]

        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        dept = product.get("department") or {}
        dept_name = dept.get("name") if isinstance(dept, dict) else None

        # Product URL: the `url` field is the full URL slug path
        raw_url = (product.get("url") or "").strip()
        if raw_url.startswith("http"):
            product_url = raw_url
        elif raw_url:
            product_url = f"https://www.tendaatacado.com.br/{raw_url.strip('/')}"
        else:
            token = (product.get("token") or "").strip("/")
            product_url = f"https://www.tendaatacado.com.br/{token}" if token else None

        # Stock: sum inventory for the resolved branch
        stock = None
        inventory = product.get("inventory") or []
        if isinstance(inventory, list) and store_id:
            for inv in inventory:
                if isinstance(inv, dict) and str(inv.get("branchId", "")) == str(store_id):
                    stock = inv.get("totalAvailable") or inv.get("quantity")
                    break
        if stock is None:
            stock = product.get("totalStock")

        return {
            "id": offer_id,
            "product_name": product.get("name"),
            "brand": brand or None,
            "description": (product.get("description") or "").strip() or None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": promo_min_qty,
            "unit": product.get("unit") or product.get("measurementUnit"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": stock,
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": dept_name,
            "offer_tag": offer_tag,
            "app_membership_required": offer_tag is not None,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": store_id,
            "zip_code": zip_code,
        }

    # ---------------------------------------------------------------- main entry

    def fetch_offers(
        self,
        zip_code: str,
        search_queries: Optional[Iterable[str]] = None,
        max_pages_per_query: int = 25,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all offers for Tenda Atacado.

        The API requires a non-empty `query` string — there is no category filter param.
        We query by each category/subcategory link (e.g. "bebidas", "refrigerantes") which
        acts as a full-text search term and returns up to 500 products (25 pages × 20).
        Results are deduplicated across queries by product ID.
        """
        print(f"Fetching Tenda Atacado departamentos offers for {zip_code}...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        # ── resolve store ──────────────────────────────────────────────────────
        store_id = self.db.get_store_id(zip_code, self.MARKET_NAME)
        if not store_id:
            store_id = self.resolve_store(zip_code)
            if store_id:
                metadata = self._resolved_store_metadata or {}
                self.db.cache_store_id(
                    zip_code,
                    self.MARKET_NAME,
                    store_id,
                    store_name=metadata.get("store_name"),
                    store_address=metadata.get("store_address"),
                    store_city=metadata.get("store_city"),
                    store_state=metadata.get("store_state"),
                    latitude=metadata.get("latitude"),
                    longitude=metadata.get("longitude"),
                    store_payload=metadata.get("store_payload"),
                )
            else:
                print("Tenda: store resolution failed, proceeding without store filter")
                store_id = ""

        if store_id:
            self.session.cookies.set("_Tendaatacado-branchID", str(store_id))
        print(f"Tenda Atacado departamentos: store_id={store_id}")

        # ── build query list ───────────────────────────────────────────────────
        if search_queries is not None:
            queries = [q.strip("/") for q in search_queries if q and q.strip()]
        else:
            queries = self.discover_search_queries()

        print(f"Tenda Atacado: {len(queries)} search queries to run")

        # ── run each query ─────────────────────────────────────────────────────
        products_by_id: Dict[str, Dict[str, Any]] = {}

        for idx, query in enumerate(queries, 1):
            if max_items is not None and len(products_by_id) >= max_items:
                break

            remaining = (max_items - len(products_by_id)) if max_items is not None else None
            raw_products = self._fetch_query_products(
                query=query,
                max_pages=max_pages_per_query,
                limit=remaining,
            )
            before = len(products_by_id)
            for prod in raw_products:
                std = self._standardize_product(prod, zip_code, store_id)
                if std is None:
                    continue
                products_by_id[std["id"]] = std
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            added = len(products_by_id) - before
            if added > 0:
                print(
                    f"  [{idx}/{len(queries)}] query={query!r}: "
                    f"+{added} new, running total={len(products_by_id)}"
                )
            time.sleep(0.25)

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        print(f"Tenda Atacado departamentos: {len(all_products)} products collected.")
        return all_products


if __name__ == "__main__":
    scraper = TendaAtacadoDepartamentosScraper()
    scraper.fetch_offers("01310-100")
