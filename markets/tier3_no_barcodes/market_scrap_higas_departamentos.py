import json
import time
import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests

from db.db_manager import DatabaseManager


class HigasDepartamentosScraper:
    DEFAULT_STORE_ID = "66466cdefafdf200a3352cd5"
    PARTNER_ID = "replicarhigas"
    DEFAULT_STOREFRONT_BASE = "https://supermercadohigas6.instabuy.com.br"

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
        self.market_name = "Higas"
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None
        self._active_partner_id: str = self.PARTNER_ID
        self._active_storefront_base: str = self.DEFAULT_STOREFRONT_BASE

    @staticmethod
    def _normalize_storefront_base(subdomain: Optional[str]) -> str:
        text = str(subdomain or "").strip().strip("/")
        if not text:
            return HigasDepartamentosScraper.DEFAULT_STOREFRONT_BASE
        if text.startswith("http://") or text.startswith("https://"):
            return text.rstrip("/")
        host = text if "." in text else f"{text}.instabuy.com.br"
        return f"https://{host}".rstrip("/")

    def _headers_for_store(self) -> Dict[str, str]:
        return {
            "Referer": f"{self._active_storefront_base}/",
            "Origin": self._active_storefront_base,
            "Accept": "application/json",
        }

    def _apply_store_context(self, store: Dict[str, Any]) -> None:
        partner_id = str(store.get("partner_id") or self.PARTNER_ID).strip()
        if partner_id:
            self._active_partner_id = partner_id

        subdomain = store.get("subdomain") or store.get("domain") or store.get("store_url")
        self._active_storefront_base = self._normalize_storefront_base(subdomain)

    def _get_with_retry(
        self,
        url: str,
        params: dict,
        headers: Optional[dict] = None,
        max_retries: int = 5,
    ) -> requests.Response:
        delay = 5
        response = None
        for attempt in range(1, max_retries + 1):
            response = self.session.get(url, params=params, headers=headers or {}, timeout=25)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", delay))
                wait = max(retry_after, delay)
                print(f"Higas departamentos: 429 (attempt {attempt}/{max_retries}), waiting {wait}s...")
                time.sleep(wait)
                delay = min(delay * 2, 60)
            elif response.status_code >= 500:
                print(
                    f"Higas departamentos: HTTP {response.status_code} "
                    f"(attempt {attempt}/{max_retries}), waiting {delay}s..."
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                return response
        if response is None:
            raise RuntimeError(f"Higas: _get_with_retry exhausted {max_retries} retries for {url}")
        return response

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_valid_gtin(*values: Any) -> Optional[str]:
        for value in values:
            if value in (None, ""):
                continue
            digits = "".join(ch for ch in str(value) if ch.isdigit())
            if len(digits) in (8, 12, 13, 14):
                return digits
        return None

    @staticmethod
    def _normalize_zip(zip_code: str) -> str:
        return "".join(ch for ch in str(zip_code or "") if ch.isdigit())

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        r = 6371.0
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(d_lon / 2) ** 2
        )
        return 2 * r * math.asin(math.sqrt(a))

    def _resolve_zip_coordinates(self, zip_code: str) -> Optional[Tuple[float, float]]:
        normalized_zip = self._normalize_zip(zip_code)
        if len(normalized_zip) != 8:
            return None

        try:
            via_cep = self.session.get(
                f"https://viacep.com.br/ws/{normalized_zip}/json/",
                timeout=12,
            )
            if via_cep.status_code != 200:
                return None

            address = via_cep.json() or {}
            if address.get("erro"):
                return None

            street = address.get("logradouro") or ""
            neighborhood = address.get("bairro") or ""
            city = address.get("localidade") or ""
            state = address.get("uf") or ""
            query_parts = [part for part in [street, neighborhood, city, state, "Brasil"] if part]
            fallback_parts = [part for part in [city, state, "Brasil"] if part]

            headers = {
                "User-Agent": "markets-db-higas-scraper/1.0",
                "Accept": "application/json",
            }

            for candidate in [", ".join(query_parts), ", ".join(fallback_parts)]:
                if not candidate:
                    continue
                geo = self.session.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": candidate, "format": "json", "limit": 1},
                    headers=headers,
                    timeout=15,
                )
                if geo.status_code != 200:
                    continue
                rows = geo.json() or []
                if not rows:
                    continue
                first = rows[0]
                lat = float(first.get("lat"))
                lon = float(first.get("lon"))
                return lat, lon
        except Exception:
            return None

        return None

    def _store_coordinates(self, store: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        spatial = store.get("spatial_position") or {}
        coords = spatial.get("coordinates") if isinstance(spatial, dict) else None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon = self._to_float(coords[0])
            lat = self._to_float(coords[1])
            if lat is not None and lon is not None:
                return lat, lon

        lat = self._to_float(store.get("latitude") or store.get("lat"))
        lon = self._to_float(store.get("longitude") or store.get("lng") or store.get("lon"))
        if lat is not None and lon is not None:
            return lat, lon

        return None

    def _store_zip_distance(self, user_zip: str, store: Dict[str, Any]) -> Optional[int]:
        address_obj = store.get("address") or {}
        store_zip = self._normalize_zip(address_obj.get("zipcode") or "")
        if len(user_zip) != 8 or len(store_zip) != 8:
            return None
        try:
            return abs(int(user_zip) - int(store_zip))
        except ValueError:
            return None

    def resolve_store(self, zip_code: str) -> Optional[str]:
        self._resolved_store_metadata = None
        # Reset context so stale state from a previous call never bleeds in
        self._active_partner_id = self.PARTNER_ID
        self._active_storefront_base = self.DEFAULT_STOREFRONT_BASE
        normalized_zip = self._normalize_zip(zip_code)
        lookup_url = "https://api.instabuy.com.br/apiv3/store"
        params = {
            "partner_id": self.PARTNER_ID,
            "zip_code": normalized_zip,
        }

        try:
            response = self.session.get(lookup_url, params=params, timeout=15)
            if response.status_code == 200:
                # Force UTF-8: instabuy API may declare charset=iso-8859-1 in
                # Content-Type, causing requests to mangle accented characters.
                stores = (json.loads(response.content.decode("utf-8")) or {}).get("data", [])
                if stores:
                    zip_coords = self._resolve_zip_coordinates(normalized_zip)

                    best_store = None
                    best_score = (float("inf"), float("inf"), float("inf"), float("inf"))
                    for idx, store in enumerate(stores):
                        api_distance = self._to_float(
                            store.get("distance")
                            or store.get("distancia")
                            or store.get("distance_km")
                        )

                        geo_distance = None
                        if zip_coords is not None:
                            store_coords = self._store_coordinates(store)
                            if store_coords is not None:
                                geo_distance = self._haversine_km(
                                    zip_coords[0],
                                    zip_coords[1],
                                    store_coords[0],
                                    store_coords[1],
                                )

                        zip_distance = self._store_zip_distance(normalized_zip, store)

                        # Prefer explicit API distance, then CEP proximity, then geodesic fallback.
                        score = (
                            api_distance if api_distance is not None else float("inf"),
                            float(zip_distance) if zip_distance is not None else float("inf"),
                            geo_distance if geo_distance is not None else float("inf"),
                            float(idx),
                        )

                        if best_store is None or score < best_score:
                            best_store = store
                            best_score = score

                    selected_store = best_store or stores[0]

                    address_obj = (selected_store.get("address") or {}) if isinstance(selected_store, dict) else {}
                    street_parts = [
                        str(address_obj.get("street") or "").strip(),
                        str(address_obj.get("number") or "").strip(),
                        str(address_obj.get("neighborhood") or "").strip(),
                    ]
                    address_text = ", ".join(part for part in street_parts if part)
                    self._apply_store_context(selected_store or {})
                    self._resolved_store_metadata = {
                        "store_name": (selected_store or {}).get("name"),
                        "store_address": address_text or address_obj.get("address"),
                        "store_city": address_obj.get("city"),
                        "store_state": address_obj.get("state"),
                        "latitude": (selected_store or {}).get("latitude"),
                        "longitude": (selected_store or {}).get("longitude"),
                        "store_subdomain": (selected_store or {}).get("subdomain"),
                        "storefront_base_url": self._active_storefront_base,
                        "partner_id": self._active_partner_id,
                        "store_payload": json.dumps(selected_store, ensure_ascii=False),
                    }
                    return (selected_store or {}).get("id")
        except Exception as exc:
            print(f"Higas resolve_store: exception {exc}")

        self._active_partner_id = self.PARTNER_ID
        self._active_storefront_base = self.DEFAULT_STOREFRONT_BASE
        print(f"Higas resolve_store: falling back to DEFAULT_STORE_ID={self.DEFAULT_STORE_ID}")
        return self.DEFAULT_STORE_ID

    def discover_categories(self, store_id: str) -> List[Dict[str, str]]:
        url = "https://api.instabuy.com.br/apiv3/category"
        params = {
            "store_id": store_id,
            "partner_id": self._active_partner_id,
        }
        try:
            response = self._get_with_retry(url, params=params)
            if response.status_code != 200:
                return []
            data = (response.json() or {}).get("data", [])
            categories = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                title = item.get("title")
                if cid and title:
                    categories.append({"id": str(cid), "title": str(title)})
            return categories
        except Exception:
            return []

    def _fetch_category_products(
        self,
        *,
        zip_code: str,
        store_id: str,
        category_id: str,
        max_pages: int,
        page_size: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        url = "https://api.instabuy.com.br/apiv3/offers"
        headers = self._headers_for_store()
        products_by_id: Dict[str, Dict[str, Any]] = {}
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        for page in range(1, max_pages + 1):
            params = {
                "page": page,
                "N": page_size,
                "order_by": "recents",
                "store_id": store_id,
                "partner_id": self._active_partner_id,
                "category_id": category_id,
            }
            try:
                response = self._get_with_retry(url, params=params, headers=headers)
                if response.status_code != 200:
                    print(
                        f"Higas departamentos category={category_id} page={page}: "
                        f"status={response.status_code}, stopping."
                    )
                    break

                body = response.json() or {}
                products = body.get("data", [])
                if not products:
                    break

                new_count = 0
                for p in products:
                    standardized = self._standardize_product(
                        p,
                        zip_code=zip_code,
                        store_id=store_id,
                    )
                    if not standardized:
                        continue
                    pid = standardized["id"]
                    if pid not in products_by_id:
                        new_count += 1
                    products_by_id[pid] = standardized
                    if max_items is not None and len(products_by_id) >= max_items:
                        break

                print(
                    f"Higas departamentos category={category_id} page={page}: "
                    f"items={len(products)} new={new_count} total={len(products_by_id)}"
                )

                # Stop when server signals last page — NOT when len < page_size.
                if new_count == 0:
                    break
                if max_items is not None and len(products_by_id) >= max_items:
                    break
                time.sleep(0.25)
            except Exception as exc:
                print(f"Higas departamentos category={category_id} page={page} error: {exc}")
                break

        rows = list(products_by_id.values())
        if max_items is not None:
            rows = rows[:max_items]
        return rows

    def _fetch_all_products(
        self,
        *,
        zip_code: str,
        store_id: str,
        max_pages: int,
        page_size: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch full catalog from offers endpoint without category filter.

        Instabuy for Higas currently ignores category/subcategory query filters,
        so a single global pagination is the reliable way to get all products.
        """
        url = "https://api.instabuy.com.br/apiv3/offers"
        headers = self._headers_for_store()
        products_by_id: Dict[str, Dict[str, Any]] = {}
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        for page in range(1, max_pages + 1):
            params = {
                "page": page,
                "N": page_size,
                "order_by": "recents",
                "store_id": store_id,
                "partner_id": self._active_partner_id,
            }
            try:
                response = self._get_with_retry(url, params=params, headers=headers)
                if response.status_code != 200:
                    print(f"Higas all-products page={page}: status={response.status_code}, stopping.")
                    break

                body = response.json() or {}
                products = body.get("data", [])
                if not products:
                    break

                new_count = 0
                for p in products:
                    standardized = self._standardize_product(
                        p,
                        zip_code=zip_code,
                        store_id=store_id,
                    )
                    if not standardized:
                        continue
                    pid = standardized["id"]
                    if pid not in products_by_id:
                        new_count += 1
                    products_by_id[pid] = standardized
                    if max_items is not None and len(products_by_id) >= max_items:
                        break

                paginator = body.get("paginator") or {}
                total_pages = paginator.get("total_pages") or paginator.get("last_page") or 0
                total_count = paginator.get("total_count") or paginator.get("total") or 0
                print(
                    f"Higas all-products page={page}: "
                    f"items={len(products)} new={new_count} total={len(products_by_id)}"
                    + (f"/{total_count}" if total_count else "")
                )
                # Stop when server signals last page — NOT when len < page_size.
                # The Instabuy API caps results at ~30/page regardless of N= param.
                if new_count == 0:
                    break
                if total_pages and page >= total_pages:
                    break
                if total_count and len(products_by_id) >= total_count:
                    break
                if max_items is not None and len(products_by_id) >= max_items:
                    break
                time.sleep(0.25)
            except Exception as exc:
                print(f"Higas all-products page={page} error: {exc}")
                break

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        return all_products

    def _standardize_product(
        self,
        p: Dict[str, Any],
        *,
        zip_code: str,
        store_id: str,
    ) -> Optional[Dict[str, Any]]:
        prices = p.get("prices", []) or []
        price_info = prices[0] if prices and isinstance(prices[0], dict) else {}

        product_id = p.get("id")

        raw_barcodes = p.get("barcodes") or []
        gtin_text = None
        if isinstance(raw_barcodes, list):
            for code in raw_barcodes:
                candidate = self._extract_valid_gtin(code)
                if candidate:
                    gtin_text = candidate
                    break

        if not gtin_text:
            for price_row in (p.get("prices") or []):
                if not isinstance(price_row, dict):
                    continue
                for code in (price_row.get("bar_codes") or []):
                    candidate = self._extract_valid_gtin(code)
                    if candidate:
                        gtin_text = candidate
                        break
                if gtin_text:
                    break

        if not gtin_text:
            for info in (p.get("custom_infos") or []):
                if isinstance(info, dict):
                    candidate = self._extract_valid_gtin(
                        info.get("barcode"),
                        info.get("ean"),
                        info.get("gtin"),
                        info.get("value"),
                    )
                    if candidate:
                        gtin_text = candidate
                        break

        barcode = self.db.normalize_barcode(gtin_text) if gtin_text else None

        stock_infos = p.get("stock_infos") or {}

        images = p.get("images") or []
        image_url = images[0] if isinstance(images, list) and images else None

        offer_id = self.db.build_offer_id("higas", store_id, barcode, gtin_text, p.get("name"))
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "native_product_id": str(product_id).strip() if product_id is not None else None,
            "product_name": p.get("name"),
            "brand": p.get("brand"),
            "description": p.get("description"),
            "regular_price": price_info.get("price"),
            "promo_price": price_info.get("promo_price"),
            "promo_min_quantity": None,
            "unit": p.get("unit_type"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": (
                f"{self._active_storefront_base}/?produto={quote(str(p.get('slug')), safe='_-~.')}"
                if p.get("slug")
                else None
            ),
            "image_url": image_url,
            "stock_balance": self._to_int(stock_infos.get("stock_balance")),
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": None,
            "offer_tag": None,
            "app_membership_required": False,
            "promo_end_at": price_info.get("promo_end_at"),
            "last_updated": datetime.now().isoformat(),
            "store_id": store_id,
            "zip_code": zip_code,
        }

    def fetch_offers(
        self,
        zip_code: str,
        category_ids: Optional[Iterable[str]] = None,
        max_pages_per_category: int = 120,
        page_size: int = 100,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        print(f"Fetching Higas departamentos offers for {zip_code}...")

        # Always resolve store by ZIP so the run targets the correct Higas branch.
        store_id = self.resolve_store(zip_code)
        if store_id:
            metadata = self._resolved_store_metadata or {}
            self.db.cache_store_id(
                zip_code,
                self.market_name,
                store_id,
                store_name=metadata.get("store_name"),
                store_address=metadata.get("store_address"),
                store_city=metadata.get("store_city"),
                store_state=metadata.get("store_state"),
                latitude=metadata.get("latitude"),
                longitude=metadata.get("longitude"),
                store_payload=metadata.get("store_payload"),
            )
        if not store_id:
            print("Could not resolve Higas store ID")
            return []

        metadata = self._resolved_store_metadata or {}
        print(
            "Higas departamentos: resolved store "
            f"id={store_id} name={metadata.get('store_name') or 'N/A'} "
            f"partner={self._active_partner_id} base={self._active_storefront_base}"
        )

        if category_ids:
            print(
                "Higas departamentos: category filters are currently ignored by the API; "
                "falling back to global full-catalog pagination."
            )

        # Keep category discovery for observability, even though filtering is ignored.
        categories = self.discover_categories(store_id)
        if categories:
            print(f"Higas departamentos: discovered {len(categories)} categories (informational).")

        all_products = self._fetch_all_products(
            zip_code=zip_code,
            store_id=store_id,
            max_pages=max_pages_per_category,
            page_size=page_size,
            limit=limit,
        )
        print(f"Higas departamentos: {len(all_products)} products collected.")
        return all_products


if __name__ == "__main__":
    scraper = HigasDepartamentosScraper()
    scraper.fetch_offers("07110-000")
