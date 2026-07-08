import json
import re
import time
import urllib.parse
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from db.db_manager import DatabaseManager


class SwiftDepartamentosScraper:
    BASE_URL = "https://www.swift.com.br"
    STORE_ID = "swift.com.br"
    DEFAULT_POSTAL_CODE = "01153000"

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "pt-BR,pt;q=0.9",
            }
        )
        self._active_store_id = self.STORE_ID
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None
        self.market_name = "Swift"

    @staticmethod
    def _normalize_zip(zip_code: str) -> Optional[str]:
        digits = "".join(ch for ch in str(zip_code or "") if ch.isdigit())
        if len(digits) == 8:
            return digits
        return None

    @staticmethod
    def _slugify_text(value: Optional[str]) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
        return slug

    def _resolve_zip_metadata(self, zip_code: str) -> Optional[Dict[str, str]]:
        normalized_zip = self._normalize_zip(zip_code)
        if not normalized_zip:
            return None

        try:
            response = self.session.get(
                f"https://viacep.com.br/ws/{normalized_zip}/json/",
                timeout=12,
            )
            if response.status_code != 200:
                return None

            payload = response.json() or {}
            if payload.get("erro"):
                return None

            city = str(payload.get("localidade") or "").strip()
            state = str(payload.get("uf") or "").strip().upper()
            neighborhood = str(payload.get("bairro") or "").strip()
            if not city or not state:
                return None

            return {
                "zip": normalized_zip,
                "city": city,
                "state": state,
                "neighborhood": neighborhood,
            }
        except Exception:
            return None

    def _compose_store_id(self, zip_code: str) -> Optional[str]:
        metadata = self._resolve_zip_metadata(zip_code)
        if not metadata:
            return None

        city_slug = self._slugify_text(metadata.get("city"))
        state_slug = self._slugify_text(metadata.get("state"))
        if not city_slug or not state_slug:
            return None
        return f"swift:{state_slug}:{city_slug}"

    def _set_postal_code_cookie(self, zip_code: str) -> None:
        postal_code = self._normalize_zip(zip_code) or self.DEFAULT_POSTAL_CODE
        self.session.cookies.set("postalcode", postal_code, domain="www.swift.com.br")

    def _is_zip_serviceable(self, category_slug: str) -> bool:
        test_url = f"{self.BASE_URL}/{category_slug}"
        try:
            response = self.session.get(test_url, timeout=20)
        except Exception:
            return False

        if response.status_code == 200:
            body = (response.text or "").lower()
            if "cep inválido" in body or "cep invalido" in body or "não disponível" in body:
                return False
            return True

        # For out-of-coverage CEPs Swift can answer 500 on category pages.
        if response.status_code == 500:
            return False
        # Redirects (3xx) typically mean the page exists but moved — treat as serviceable.
        # 404 means the category slug is wrong, not that the ZIP is unserviceable.
        return response.status_code in (301, 302, 404)

    def resolve_store(self, zip_code: str) -> Optional[str]:
        normalized_zip = self._normalize_zip(zip_code)
        if not normalized_zip:
            self._resolved_store_metadata = None
            return None

        # Fetch metadata once and reuse — avoids two ViaCEP calls.
        metadata = self._resolve_zip_metadata(normalized_zip) or {}
        city_slug = self._slugify_text(metadata.get("city"))
        state_slug = self._slugify_text(metadata.get("state"))
        derived_store_id = (
            f"swift:{state_slug}:{city_slug}"
            if city_slug and state_slug
            else f"swift:cep:{normalized_zip}"
        )

        self._resolved_store_metadata = {
            "store_name": "Swift",
            "store_address": None,
            "store_city": metadata.get("city"),
            "store_state": metadata.get("state"),
            "latitude": None,
            "longitude": None,
            "store_payload": {
                "zip": normalized_zip,
                "city": metadata.get("city"),
                "state": metadata.get("state"),
                "neighborhood": metadata.get("neighborhood"),
            },
        }
        return derived_store_id

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        max_retries: int = 4,
    ) -> Optional[Any]:
        delay = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers or {},
                    data=data,
                    timeout=30,
                )
                if response.status_code == 200:
                    return response.json()
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = max(int(response.headers.get("Retry-After", delay)), delay)
                    print(
                        f"Swift: HTTP {response.status_code} on {url} "
                        f"(attempt {attempt}/{max_retries}), waiting {wait}s..."
                    )
                    time.sleep(wait)
                    delay = min(delay * 2, 45)
                    continue
                print(f"Swift: HTTP {response.status_code} on {url}")
                return None
            except Exception as exc:
                print(f"Swift: request error on {url}: {exc}")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        return None

    def _request_text(
        self,
        url: str,
        *,
        max_retries: int = 4,
    ) -> Optional[str]:
        delay = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = self.session.get(url, timeout=30)
                if response.status_code == 200:
                    return response.text
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = max(int(response.headers.get("Retry-After", delay)), delay)
                    print(
                        f"Swift: HTTP {response.status_code} on {url} "
                        f"(attempt {attempt}/{max_retries}), waiting {wait}s..."
                    )
                    time.sleep(wait)
                    delay = min(delay * 2, 45)
                    continue
                print(f"Swift: HTTP {response.status_code} on {url}")
                return None
            except Exception as exc:
                print(f"Swift: request error on {url}: {exc}")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        return None

    @staticmethod
    def _extract_remix_context(html_text: str) -> Optional[Dict[str, Any]]:
        marker = "window.__remixContext = "
        start = html_text.find(marker)
        if start < 0:
            return None

        segment = html_text[start + len(marker) :]
        json_start = segment.find("{")
        if json_start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False
        json_end = -1

        for idx, ch in enumerate(segment[json_start:], json_start):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        json_end = idx
                        break

        if json_end < 0:
            return None

        raw_json = segment[json_start : json_end + 1]
        try:
            return json.loads(raw_json)
        except Exception:
            return None

    def discover_categories(self) -> List[Tuple[str, str]]:
        data = self._request_json("GET", f"{self.BASE_URL}/api/categories")
        if not isinstance(data, dict):
            return []

        category_list = data.get("categoryList") or []
        seen: set = set()
        categories: List[Tuple[str, str]] = []

        for item in category_list:
            if not isinstance(item, dict):
                continue
            link_id = str(item.get("linkId") or "").strip()
            if not link_id or link_id == "/":
                continue

            parsed = urllib.parse.urlparse(link_id)
            slug = parsed.path.strip("/")
            if not slug:
                continue

            slug = urllib.parse.unquote(slug)
            if slug in seen:
                continue
            seen.add(slug)

            name = str(item.get("name") or slug).strip()
            categories.append((slug, name))

        return categories

    def _extract_initial_products(self, slug: str) -> Tuple[List[Dict[str, Any]], int]:
        html_text = self._request_text(f"{self.BASE_URL}/{slug}")
        if not html_text:
            return [], 0

        remix = self._extract_remix_context(html_text)
        if not remix:
            return [], 0

        loader_data = (
            (remix.get("state") or {})
            .get("loaderData", {})
            .get("routes/$id.$", {})
        )
        category_products = (loader_data.get("categoryProducts") or {}).get("products") or []
        if not isinstance(category_products, list):
            category_products = []

        total_products = 0
        if category_products and isinstance(category_products[0], dict):
            total_products = int(category_products[0].get("totalProductsCount") or 0)
        if total_products <= 0:
            total_products = len(category_products)

        return category_products, total_products

    def _fetch_vtex_products(
        self,
        slug: str,
        from_idx: int,
        to_idx: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Fetch products from the VTEX catalog REST API with offset pagination.

        The Remix getMoreProducts action only returns one additional batch
        regardless of the parameters sent. The VTEX catalog API supports
        arbitrary _from/_to offsets and is the correct way to iterate through
        all products in a category.

        Returns (products_list, total_count). total_count comes from the
        'resources' response header (format '0-49/137').
        """
        url = f"{self.BASE_URL}/api/catalog_system/pub/products/search/{urllib.parse.quote(slug, safe='')}"
        try:
            resp = self.session.get(
                url,
                params={"_from": from_idx, "_to": to_idx},
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except Exception as exc:
            print(f"Swift VTEX API error for {slug}: {exc}")
            return [], 0

        if resp.status_code not in (200, 206):
            print(f"Swift VTEX API HTTP {resp.status_code} for {slug}")
            return [], 0

        # Total count is in the "resources" header: "0-49/137"
        total = 0
        resources = resp.headers.get("resources", "")
        if resources and "/" in resources:
            try:
                total = int(resources.split("/")[1])
            except (ValueError, IndexError):
                pass

        try:
            data = resp.json()
        except Exception:
            return [], 0

        return (data if isinstance(data, list) else []), total

    def _normalize_product_path(self, raw_path: Optional[str]) -> Optional[str]:
        path = str(raw_path or "").strip()
        if not path:
            return None

        parsed = urllib.parse.urlparse(path)
        route = urllib.parse.unquote(parsed.path or path).strip()
        if not route:
            return None

        route = route.lstrip("/")
        if route.lower().endswith("/p"):
            route = route[:-2]
        route = route.strip("/")
        if not route:
            return None

        if route.startswith("detail/"):
            detail_slug = route
        else:
            detail_slug = f"detail/{route}"

        return f"{self.BASE_URL}/{detail_slug}"

    def _build_public_product_url(self, primary_link: Optional[str], fallback_slug: Optional[str] = None) -> Optional[str]:
        candidate = str(primary_link or "").strip()
        if not candidate:
            return self._normalize_product_path(fallback_slug)

        parsed = urllib.parse.urlparse(candidate)

        if parsed.scheme in ("http", "https"):
            host = (parsed.netloc or "").lower()

            # Some VTEX payloads return an admin login redirect URL.
            if "admin/site/login.aspx" in (parsed.path or "").lower():
                query = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=False)
                return_url = (query.get("ReturnUrl") or query.get("returnurl") or [None])[0]
                normalized = self._normalize_product_path(return_url)
                if normalized:
                    return normalized

            if "swift.com.br" in host:
                normalized = self._normalize_product_path(parsed.path)
                if normalized:
                    return normalized

            if "myvtex.com" in host:
                normalized = self._normalize_product_path(parsed.path)
                if normalized:
                    return normalized

        normalized_candidate = self._normalize_product_path(candidate)
        if normalized_candidate:
            return normalized_candidate

        return self._normalize_product_path(fallback_slug)

    def _standardize_vtex_product(
        self,
        product: Dict[str, Any],
        *,
        zip_code: str,
    ) -> Optional[Dict[str, Any]]:
        """Standardize a product from the VTEX catalog API response format."""
        product_id = str(product.get("productId") or "").strip()
        if not product_id:
            return None

        items = product.get("items") or []
        first_item = items[0] if items and isinstance(items[0], dict) else {}

        raw_ean = first_item.get("ean")
        gtin = str(raw_ean) if raw_ean else None
        barcode = self.db.normalize_barcode(gtin) if gtin else None

        images = first_item.get("images") or []
        image_url = None
        if images and isinstance(images[0], dict):
            image_url = images[0].get("imageUrl")

        sellers = first_item.get("sellers") or []
        first_seller = sellers[0] if sellers and isinstance(sellers[0], dict) else {}
        offer = first_seller.get("commertialOffer") or {}
        list_price = self._safe_float(offer.get("ListPrice"))
        selling_price = self._safe_float(offer.get("Price"))

        if list_price and selling_price and round(selling_price, 2) < round(list_price, 2):
            regular_price = round(list_price, 2)
            promo_price = round(selling_price, 2)
        else:
            regular_price = round(selling_price, 2) if selling_price else (round(list_price, 2) if list_price else None)
            promo_price = None

        link = str(product.get("link") or "").strip()
        link_text = str(product.get("linkText") or "").strip()
        product_url = self._build_public_product_url(link, fallback_slug=link_text)

        offer_id = self.db.build_offer_id("swift", self._active_store_id, barcode, gtin, product.get("productName") or product.get("name"))
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": product.get("productName") or product.get("name"),
            "brand": product.get("brand"),
            "description": product.get("description"),
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": None,
            "unit": None,
            "gtin": gtin,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": None,
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": None,
            "offer_tag": None,
            "app_membership_required": False,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": self._active_store_id,
            "zip_code": zip_code,
        }

    def _safe_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_price_value(self, value: Any) -> Optional[float]:
        """Convert Swift price payload to BRL float.

        Swift sends integer cent values (e.g., 1890 => 18.90) for product prices.
        This helper also handles strings with comma decimal notation.
        """
        if value is None:
            return None

        if isinstance(value, str):
            cleaned = value.strip().replace("R$", "").replace(" ", "")
            cleaned = cleaned.replace(".", "").replace(",", ".")
            if not cleaned:
                return None
            try:
                return round(float(cleaned), 2)
            except ValueError:
                return None

        if isinstance(value, int):
            return round(value / 100.0, 2)

        if isinstance(value, float):
            # Floats from the API are already in BRL — do not divide.
            return round(value, 2)

        numeric = self._safe_float(value)
        if numeric is None:
            return None
        return round(numeric, 2)

    @staticmethod
    def _parse_stamp_promo(stamp: Optional[str]) -> Tuple[Optional[int], Optional[float]]:
        """Parse Swift stamp text like 'A partir de 2 un 10,90 cada'."""
        if not stamp:
            return None, None

        text = str(stamp)
        qty_match = re.search(r"A\s+partir\s+de\s+(\d+)\s+un", text, flags=re.IGNORECASE)
        price_match = re.search(r"(\d+[\.,]\d{2})", text)

        promo_min_qty = int(qty_match.group(1)) if qty_match else None
        promo_price = None
        if price_match:
            price_text = price_match.group(1).replace(".", "").replace(",", ".")
            try:
                promo_price = round(float(price_text), 2)
            except ValueError:
                promo_price = None

        return promo_min_qty, promo_price

    def _standardize_product(
        self,
        product: Dict[str, Any],
        *,
        zip_code: str,
    ) -> Optional[Dict[str, Any]]:
        product_id = product.get("productId")
        if not product_id:
            return None

        raw_ean = product.get("ean")
        gtin = str(raw_ean) if raw_ean else None
        barcode = self.db.normalize_barcode(gtin) if gtin else None

        images = product.get("images") or []
        image_url = None
        if isinstance(images, list) and images:
            first_image = images[0]
            if isinstance(first_image, str):
                image_url = first_image
            elif isinstance(first_image, dict):
                image_url = first_image.get("imageUrl") or first_image.get("url")

        regular_price = self._parse_price_value(product.get("price"))
        selling_price = self._parse_price_value(product.get("sellingPrice"))
        stamp = product.get("stamp")
        promo_min_quantity, promo_from_stamp = self._parse_stamp_promo(stamp)
        promo_price = None
        if promo_from_stamp is not None:
            promo_price = promo_from_stamp
        elif regular_price is not None and selling_price is not None and selling_price < regular_price:
            promo_price = selling_price

        product_link = str(product.get("productLink") or "").strip()
        slug = str(product.get("slug") or "").strip()
        product_url = self._build_public_product_url(product_link, fallback_slug=slug)

        offer_id = self.db.build_offer_id("swift", self._active_store_id, barcode, gtin, product.get("productName") or product.get("name"))
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": product.get("productName") or product.get("name"),
            "brand": product.get("brand"),
            "description": product.get("description"),
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": promo_min_quantity,
            "unit": None,
            "gtin": gtin,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": None,
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": stamp,
            "offer_tag": None,
            "app_membership_required": False,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": self._active_store_id,
            "zip_code": zip_code,
        }

    def _fetch_category_products(
        self,
        slug: str,
        *,
        zip_code: str,
        vtex_batch: int = 50,
        max_pages: int = 80,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        # Get the first batch and declared total from the Remix HTML (fast, cached).
        raw_initial, declared_total = self._extract_initial_products(slug)
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        products_by_id: Dict[str, Dict[str, Any]] = {}
        for raw_product in raw_initial:
            if not isinstance(raw_product, dict):
                continue
            standardized = self._standardize_product(raw_product, zip_code=zip_code)
            if not standardized:
                continue
            products_by_id[standardized["id"]] = standardized
            if max_items is not None and len(products_by_id) >= max_items:
                break

        # Fetch remaining products via VTEX catalog API (_from/_to offset pagination).
        # The Remix getMoreProducts POST is broken beyond the first additional batch,
        # but the VTEX catalog REST API supports arbitrary offsets correctly.
        vtex_from = 0
        vtex_total = declared_total
        pages_fetched = 0

        while pages_fetched < max_pages:
            if max_items is not None and len(products_by_id) >= max_items:
                break
            if vtex_total > 0 and len(products_by_id) >= vtex_total:
                break

            vtex_to = vtex_from + vtex_batch - 1
            batch, reported_total = self._fetch_vtex_products(slug, vtex_from, vtex_to)
            pages_fetched += 1

            if reported_total > 0:
                vtex_total = reported_total

            if not batch:
                break

            new_count = 0
            for raw_product in batch:
                if not isinstance(raw_product, dict):
                    continue
                standardized = self._standardize_vtex_product(raw_product, zip_code=zip_code)
                if not standardized:
                    continue
                pid = standardized["id"]
                if pid not in products_by_id:
                    new_count += 1
                products_by_id[pid] = standardized
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            print(
                f"Swift {slug}: vtex_from={vtex_from} batch={len(batch)} new={new_count} "
                f"total={len(products_by_id)}/{vtex_total}"
            )

            vtex_from += len(batch)

            if len(batch) < vtex_batch:
                break

            time.sleep(0.35)

        category_products = list(products_by_id.values())
        if max_items is not None:
            category_products = category_products[:max_items]
        return category_products

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        print(f"Fetching Swift departamentos offers for {zip_code}...")
        normalized_zip = self._normalize_zip(zip_code)
        if not normalized_zip:
            print("Swift: invalid ZIP format (expected 8 digits). Skipping scan.")
            return []

        resolved_store_id = self.resolve_store(normalized_zip)
        self._active_store_id = resolved_store_id or f"swift:cep:{normalized_zip}"

        if resolved_store_id:
            metadata = self._resolved_store_metadata or {}
            self.db.cache_store_id(
                zip_code,
                self.market_name,
                resolved_store_id,
                store_name=metadata.get("store_name"),
                store_address=metadata.get("store_address"),
                store_city=metadata.get("store_city"),
                store_state=metadata.get("store_state"),
                latitude=metadata.get("latitude"),
                longitude=metadata.get("longitude"),
                store_payload=metadata.get("store_payload"),
            )

        self._set_postal_code_cookie(zip_code)
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        categories = self.discover_categories()
        if not categories:
            print("Swift: no categories discovered.")
            return []

        probe_slug = categories[0][0]
        if not self._is_zip_serviceable(probe_slug):
            print(
                f"Swift: CEP {normalized_zip} is invalid or not serviceable for this store. "
                "Skipping scan."
            )
            return []

        print(f"Swift: discovered {len(categories)} categories")

        all_products: Dict[str, Dict[str, Any]] = {}
        for idx, (slug, name) in enumerate(categories, 1):
            if max_items is not None and len(all_products) >= max_items:
                break

            remaining_limit = None
            if max_items is not None:
                remaining_limit = max(0, max_items - len(all_products))

            print(f"[{idx}/{len(categories)}] Swift category: {name} ({slug})")
            category_products = self._fetch_category_products(
                slug,
                zip_code=zip_code,
                limit=remaining_limit,
            )
            new_count = 0
            for product in category_products:
                pid = product["id"]
                if pid not in all_products:
                    new_count += 1
                all_products[pid] = product
                if max_items is not None and len(all_products) >= max_items:
                    break
            print(
                f"Swift {name}: category_items={len(category_products)} "
                f"new={new_count} global_total={len(all_products)}"
            )

        result = list(all_products.values())
        if max_items is not None:
            result = result[:max_items]
        print(f"Swift: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = SwiftDepartamentosScraper()
    products = scraper.fetch_offers("08032-230")
    print(f"Total products: {len(products)}")
