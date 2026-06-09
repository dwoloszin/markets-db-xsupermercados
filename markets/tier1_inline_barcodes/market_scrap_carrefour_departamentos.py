import html as html_module
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import requests

from db.db_manager import DatabaseManager

# Sentinel used to distinguish "not in PDP cache" from "cached as empty result"
_MISSING = object()


class CarrefourDepartamentosScraper:
    """Scraper for Carrefour Mercado department/category pages.

    This implementation favors resilient HTML extraction (JSON-LD first,
    link-text fallback) so it keeps working even when API contracts change.
    """

    BASE_URL = "https://mercado.carrefour.com.br"
    STORE_ID = "carrefour-mercado"
    DEFAULT_DEPARTMENT_URLS = [
        "https://mercado.carrefour.com.br/categoria/bebidas",
        "https://mercado.carrefour.com.br/categoria/mercearia",
        "https://mercado.carrefour.com.br/categoria/limpeza",
        "https://mercado.carrefour.com.br/categoria/hortifruti",
    ]

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
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9",
            }
        )
        self.market_name = "Carrefour"
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None
        self._active_store_id: str = self.STORE_ID
        self._current_zip_code: Optional[str] = None
        self._pdp_cache: Dict[str, Dict[str, Any]] = {}
        self._pdp_cache_lock = threading.Lock()
        self._thread_local = threading.local()
        self._pdp_enrich_enabled = self._is_truthy(os.getenv("CARREFOUR_ENABLE_PDP_ENRICH", "1"))
        self._pdp_enrich_limit = self._safe_int(os.getenv("CARREFOUR_PDP_ENRICH_LIMIT", "120"), default=120)
        self._pdp_enrich_delay_seconds = self._safe_float(
            os.getenv("CARREFOUR_PDP_ENRICH_DELAY_SECONDS", "0.08"),
            default=0.08,
        )
        self._pdp_workers = self._safe_int(os.getenv("CARREFOUR_PDP_WORKERS", "8"), default=4)
        self._dept_workers = self._safe_int(os.getenv("CARREFOUR_DEPT_WORKERS", "3"), default=3)

    @staticmethod
    def _is_truthy(raw: Optional[str]) -> bool:
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _safe_int(raw: Optional[str], default: int) -> int:
        try:
            parsed = int(str(raw or "").strip())
        except ValueError:
            return default
        return parsed if parsed >= 0 else default

    @staticmethod
    def _safe_float(raw: Optional[str], default: float) -> float:
        try:
            parsed = float(str(raw or "").strip())
        except ValueError:
            return default
        return parsed if parsed >= 0 else default

    @staticmethod
    def _normalize_department_urls(urls: Iterable[str]) -> List[str]:
        normalized: List[str] = []
        seen = set()
        for raw_url in urls:
            if not raw_url:
                continue
            parsed = urlparse(str(raw_url).strip())
            path = (parsed.path or "").rstrip("/")
            if not path.startswith("/categoria/"):
                continue
            suffix = path[len("/categoria/") :].strip("/")
            if not suffix:
                continue
            clean_url = f"https://mercado.carrefour.com.br/categoria/{suffix}"
            if clean_url not in seen:
                seen.add(clean_url)
                normalized.append(clean_url)
        return normalized

    @staticmethod
    def _parse_price_text(value: Any) -> Optional[float]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()
        if not text:
            return None
        text = text.replace("R$", "").replace(" ", "")
        if "," in text:
            # PT-BR formatted decimal: 1.234,56
            text = text.replace(".", "").replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None

    @classmethod
    def _extract_prices_from_text(cls, text: Any) -> List[float]:
        if text is None:
            return []
        raw = str(text)
        if not raw.strip():
            return []

        matches = re.findall(
            r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+(?:[.,][0-9]{2})?)",
            raw,
            flags=re.IGNORECASE,
        )
        prices: List[float] = []
        for match in matches:
            parsed = cls._parse_price_text(match)
            if parsed is not None:
                prices.append(parsed)
        return prices

    @staticmethod
    def _extract_unit_from_text(*values: Optional[str]) -> Optional[str]:
        combined = " ".join(str(value or "") for value in values)
        if not combined:
            return None
        # Common patterns: 800 g, 1kg, 350ml, 2L, 12un
        unit_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|un|und|unid|pct|pc)\b", combined, re.I)
        if not unit_match:
            return None
        number = unit_match.group(1).replace(",", ".")
        unit = unit_match.group(2).lower()
        return f"{number}{unit}"

    @staticmethod
    def _extract_barcode(product_obj: Dict[str, Any]) -> Optional[str]:
        candidates = [
            product_obj.get("gtin13"),
            product_obj.get("gtin"),
            product_obj.get("gtin14"),
            product_obj.get("gtin12"),
            product_obj.get("mpn"),
            product_obj.get("ean"),
            product_obj.get("barcode"),
        ]
        for value in candidates:
            if not value:
                continue
            raw = "".join(ch for ch in str(value) if ch.isdigit())
            if raw:
                return raw
        return None

    def _extract_offer_prices(self, offer_obj: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
        regular_candidates = [
            offer_obj.get("highPrice"),
            offer_obj.get("listPrice"),
            offer_obj.get("priceBeforeDiscount"),
        ]
        promo_candidates = [
            offer_obj.get("price"),
            offer_obj.get("lowPrice"),
            offer_obj.get("salePrice"),
        ]

        price_spec = offer_obj.get("priceSpecification")
        if isinstance(price_spec, dict):
            regular_candidates.append(price_spec.get("listPrice"))
            promo_candidates.append(price_spec.get("price"))
        elif isinstance(price_spec, list):
            for item in price_spec:
                if not isinstance(item, dict):
                    continue
                regular_candidates.append(item.get("listPrice"))
                promo_candidates.append(item.get("price"))

        regular_price = next(
            (parsed for parsed in (self._parse_price_text(value) for value in regular_candidates) if parsed is not None),
            None,
        )
        promo_price = next(
            (parsed for parsed in (self._parse_price_text(value) for value in promo_candidates) if parsed is not None),
            None,
        )

        if regular_price is None:
            regular_price = promo_price
        # Only keep promo_price if it is STRICTLY less than regular_price
        if promo_price is None or regular_price is None or promo_price >= regular_price:
            promo_price = None
        return regular_price, promo_price

    @staticmethod
    def _normalize_product_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        text = str(url).strip()
        if not text:
            return None
        if text.startswith("http://") or text.startswith("https://"):
            return text
        if text.startswith("/"):
            return f"https://mercado.carrefour.com.br{text}"
        return f"https://mercado.carrefour.com.br/{text.lstrip('/')}"


    @staticmethod
    def _clean_product_name(raw_name: Optional[str]) -> Optional[str]:
        """Strip price info, discount % and UI labels baked into Carrefour JSON-LD names.

        Carrefour's JSON-LD includes the price and CTA in the name field, e.g.:
          "Chocolate Bis Xtra Oreo 45g R$ 4,39 Patrocinado Adicionar"
          "Café 3 Corações 500g R$ 34,89 - 20 % R$ 27,90 Adicionar"
        We strip everything from the first R$ onwards, then remove trailing noise.
        """
        if not raw_name:
            return None
        name = str(raw_name).strip()
        # Remove encoding artefacts
        name = name.replace("Ã¡", "á").replace("Ã£", "ã").replace("Ã§", "ç")
        name = name.replace("Ã©", "é").replace("Ã³", "ó").replace("Ãº", "ú")
        name = name.replace("Ãª", "ê").replace("Ãµ", "õ").replace("Ã­", "í")
        # Strip price/promo text: "R$ X,XX" onwards
        name = re.sub(r'\s+R\$\s*[\d][\d.,]*.*$', '', name, flags=re.IGNORECASE).strip()
        # Strip trailing UI labels
        for noise in [" Patrocinado", " Adicionar", " OFF", " OFERTA"]:
            if name.upper().endswith(noise.upper()):
                name = name[: -len(noise)].strip()
        return name.strip() or None

    def _build_product_payload(self, product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw_name = product.get("name") or ""
        # Extract prices embedded in the raw name BEFORE cleaning it
        # Pattern: "... R$ 34,89 - 20 % R$ 27,90 Adicionar" or "... R$ 4,39 Patrocinado"
        name_prices = self._extract_prices_from_text(raw_name)
        name_regular_price: Optional[float] = None
        name_promo_price: Optional[float] = None
        if len(name_prices) >= 2:
            # Two prices in name: first = regular, second = promo (after discount %)
            name_regular_price = name_prices[0]
            name_promo_price = name_prices[1]
            if name_regular_price is not None and name_promo_price is not None and name_promo_price >= name_regular_price:
                name_promo_price = None  # not actually a discount
        elif len(name_prices) == 1:
            name_regular_price = name_prices[0]

        name = self._clean_product_name(raw_name)
        if not name:
            return None

        product_url = self._normalize_product_url(product.get("url") or product.get("@id"))
        image = product.get("image")
        if isinstance(image, list) and image:
            image = image[0]

        offer_obj = product.get("offers") or {}
        if isinstance(offer_obj, list):
            offer_obj = offer_obj[0] if offer_obj else {}
        if not isinstance(offer_obj, dict):
            offer_obj = {}

        regular_price, promo_price = self._extract_offer_prices(offer_obj)
        # Supplement with prices extracted from the name (more reliable for Carrefour)
        if regular_price is None and name_regular_price is not None:
            regular_price = name_regular_price
        if promo_price is None and name_promo_price is not None:
            promo_price = name_promo_price
        # If offer block only exposed one effective price via promo field, normalize it to regular.
        if regular_price is None and promo_price is not None:
            regular_price = promo_price
            promo_price = None
        # Final guard: promo must be strictly less than regular
        if promo_price is not None and regular_price is not None and promo_price >= regular_price:
            promo_price = None

        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        description = product.get("description")
        category = product.get("category")

        availability = str(offer_obj.get("availability") or "").lower()
        stock_general: Optional[int]
        if "instock" in availability:
            stock_general = 1
        elif availability:
            stock_general = 0
        else:
            stock_general = None

        eligible_qty = offer_obj.get("eligibleQuantity")
        promo_min_quantity = None
        if isinstance(eligible_qty, dict):
            promo_min_quantity = self._safe_int(str(eligible_qty.get("value") or "").strip(), default=0) or None
        elif eligible_qty is not None:
            promo_min_quantity = self._safe_int(str(eligible_qty).strip(), default=0) or None

        raw_barcode = self._extract_barcode(product)
        normalized_barcode = self.db.normalize_barcode(raw_barcode) if raw_barcode else None
        offer_id = self.db.build_offer_id("carrefour", self._active_store_id, normalized_barcode, raw_barcode, name)
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": name,
            "brand": brand,
            "description": description,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": promo_min_quantity,
            "unit": self._extract_unit_from_text(name, description, str(category or "")),
            "gtin": raw_barcode,
            "barcode": normalized_barcode,
            "product_url": product_url,
            "image_url": image,
            "stock_balance": None,
            "stock_general": stock_general,
            "sold_quantity": None,
            "offer_name": offer_obj.get("name") or (
                # Extract discount tag from raw name e.g. "50% OFF NA 2 UN" or "- 20 %"
                (lambda m: m.group(0).strip() if m else None)(
                    re.search(r'(?:[-−]\s*\d+\s*%|\d+\s*%\s*OFF[^R$]*)', raw_name, re.IGNORECASE)
                )
            ),
            "offer_tag": offer_obj.get("category") or offer_obj.get("itemCondition"),
            "app_membership_required": False,
            "promo_end_at": offer_obj.get("priceValidUntil"),
            "last_updated": datetime.now().isoformat(),
            "store_id": self._active_store_id,
            "zip_code": self._current_zip_code,
        }

    @staticmethod
    def _normalize_zip(value: Any) -> str:
        return "".join(ch for ch in str(value or "") if ch.isdigit())

    @staticmethod
    def _zip_distance_score(reference_zip: str, candidate_zip: str) -> int:
        ref = CarrefourDepartamentosScraper._normalize_zip(reference_zip)
        cand = CarrefourDepartamentosScraper._normalize_zip(candidate_zip)
        if len(ref) != 8 or len(cand) != 8:
            return 10**9
        try:
            return abs(int(ref) - int(cand))
        except ValueError:
            return 10**9

    @staticmethod
    def _slugify_text(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-")

    def _get_thread_session(self) -> requests.Session:
        """Return a per-thread requests.Session (thread-safe alternative to self.session)."""
        if not hasattr(self._thread_local, "session"):
            s = requests.Session()
            s.headers.update(dict(self.session.headers))
            self._thread_local.session = s
        return self._thread_local.session

    def _post_json(self, path: str, form_data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        response = self.session.post(
            url,
            data=form_data,
            timeout=25,
            headers={"User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0")},
        )
        if response.status_code != 200:
            return {}
        try:
            return response.json() or {}
        except Exception:
            return {}

    def _resolve_cep_context(self, zip_code: str) -> Dict[str, Any]:
        normalized_zip = self._normalize_zip(zip_code)
        if len(normalized_zip) != 8:
            return {}
        formatted = f"{normalized_zip[:5]}-{normalized_zip[5:]}"
        payload = self._post_json("/action/cep", {"CEP": formatted})
        return payload.get("result") or {}

    def _list_pickup_stores(self, city: str) -> List[Dict[str, Any]]:
        if not city:
            return []
        payload = self._post_json("/action/stores-from-pickups", {"city": city})
        result = payload.get("result") or {}
        stores = result.get("stores") or []
        if isinstance(stores, list):
            return [store for store in stores if isinstance(store, dict)]
        return []

    def _pick_nearest_store(self, user_zip: str, stores: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not stores:
            return None

        def sort_key(store: Dict[str, Any]):
            store_zip = store.get("cep_clique_retire") or store.get("postal_code") or ""
            score = self._zip_distance_score(user_zip, str(store_zip))
            return (
                score,
                str(store.get("state") or ""),
                str(store.get("city") or ""),
                str(store.get("name") or ""),
            )

        return min(stores, key=sort_key)

    def _apply_selected_store(self, user_zip: str, store: Dict[str, Any]) -> Dict[str, Any]:
        target_zip = store.get("cep_clique_retire") or store.get("postal_code") or user_zip
        payload = {
            "CEP": self._normalize_zip(target_zip),
            "name": store.get("name") or "",
            "city": store.get("city") or "",
            "state": store.get("state") or "",
            "postal_code": self._normalize_zip(target_zip),
            "store": json.dumps(store, ensure_ascii=False),
        }
        response = self._post_json("/action/set-regionalization", payload)
        return response.get("result") or {}

    @staticmethod
    def _iter_product_dicts(payload: Any):
        if isinstance(payload, dict):
            if payload.get("@type") == "Product":
                yield payload
            for value in payload.values():
                yield from CarrefourDepartamentosScraper._iter_product_dicts(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from CarrefourDepartamentosScraper._iter_product_dicts(item)

    def discover_department_urls(self) -> List[str]:
        discovered: List[str] = []

        # Try sitemap first — more reliable than parsing the SPA homepage
        for sitemap_url in [
            f"{self.BASE_URL}/sitemap/category-0.xml",
            f"{self.BASE_URL}/sitemap.xml",
        ]:
            try:
                r = self.session.get(sitemap_url, timeout=20)
                if r.status_code == 200 and r.text:
                    locs = re.findall(r"<loc>(.*?)</loc>", r.text, flags=re.IGNORECASE)
                    for loc in locs:
                        if "/categoria/" in loc:
                            discovered.append(loc)
                    if discovered:
                        break
            except Exception:
                pass

        # Fallback: parse homepage HTML (works only if server pre-renders links)
        if not discovered:
            try:
                response = self.session.get(f"{self.BASE_URL}/", timeout=25)
                if response.status_code == 200 and response.text:
                    links = re.findall(
                        r"""href=["'](https?://mercado\.carrefour\.com\.br/categoria/[^"'#?]+|/categoria/[^"'#?]+)""",
                        response.text,
                        flags=re.IGNORECASE,
                    )
                    discovered.extend(str(link) for link in links)
            except Exception:
                pass

        normalized = self._normalize_department_urls(discovered)
        if normalized:
            print(f"Carrefour departamentos: discovered {len(normalized)} categories.")
            return normalized
        print("Carrefour departamentos: discovery failed, using DEFAULT_DEPARTMENT_URLS")
        return list(self.DEFAULT_DEPARTMENT_URLS)

    def resolve_store(self, zip_code: str) -> Optional[str]:
        self._resolved_store_metadata = None
        self._active_store_id = self.STORE_ID

        cep_context = self._resolve_cep_context(zip_code)
        address = cep_context.get("address") or {}
        city = address.get("city") or ""

        stores = self._list_pickup_stores(str(city))
        selected_store = self._pick_nearest_store(zip_code, stores)

        regionalization_result: Dict[str, Any] = {}
        if selected_store:
            regionalization_result = self._apply_selected_store(zip_code, selected_store)

        region_id = (
            regionalization_result.get("regionId")
            or cep_context.get("regionId")
            or ""
        )
        selected_zip = (
            (regionalization_result.get("regionContextProps") or {}).get("selectStoreZipCode")
            or (selected_store.get("cep_clique_retire") if selected_store else "")
        )
        selected_zip = self._normalize_zip(selected_zip)

        if selected_store:
            store_key = ":".join(
                part
                for part in [
                    "carrefour",
                    self._slugify_text(selected_store.get("state") or address.get("state")),
                    self._slugify_text(selected_store.get("city") or city),
                    self._slugify_text(selected_store.get("name")),
                    selected_zip,
                ]
                if part
            )
        else:
            store_key = ":".join(
                part
                for part in [
                    "carrefour",
                    self._slugify_text(address.get("state")),
                    self._slugify_text(city),
                    self._slugify_text(region_id),
                ]
                if part
            )

        if store_key:
            self._active_store_id = store_key

        if selected_store:
            print(
                "Carrefour regionalization selected store: "
                f"{selected_store.get('name')} ({selected_store.get('city')}-{selected_store.get('state')}) "
                f"zip={selected_zip or selected_store.get('postal_code')} region_id={region_id or 'N/A'}"
            )
        else:
            print(
                "Carrefour regionalization fallback: "
                f"city={address.get('city') or 'N/A'} state={address.get('state') or 'N/A'} "
                f"region_id={region_id or 'N/A'}"
            )

        s = selected_store or {}
        def _try_float(val: Any) -> Optional[float]:
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        self._resolved_store_metadata = {
            "store_name": s.get("name") or "Carrefour Mercado",
            "store_address": ", ".join(
                part
                for part in [
                    s.get("street"),
                    s.get("number"),
                    s.get("complement"),
                    s.get("neighborhood"),
                ]
                if part
            )
            or address.get("street"),
            "store_city": s.get("city") or address.get("city"),
            "store_state": s.get("state") or address.get("state"),
            "latitude": _try_float(s.get("latitude") or s.get("lat")),
            "longitude": _try_float(s.get("longitude") or s.get("lng") or s.get("lon")),
            "store_payload": json.dumps({
                "cep_context": cep_context,
                "selected_store": selected_store,
                "regionalization": regionalization_result,
            }, ensure_ascii=False),
        }

        # Persist resolved store to DB so subsequent runs skip re-resolution
        if self._active_store_id and self._active_store_id != self.STORE_ID:
            metadata = self._resolved_store_metadata or {}
            try:
                self.db.cache_store_id(
                    zip_code,
                    self.market_name,
                    self._active_store_id,
                    store_name=metadata.get("store_name"),
                    store_address=metadata.get("store_address"),
                    store_city=metadata.get("store_city"),
                    store_state=metadata.get("store_state"),
                    latitude=metadata.get("latitude"),
                    longitude=metadata.get("longitude"),
                    store_payload=metadata.get("store_payload"),
                )
            except Exception:
                pass

        return self._active_store_id

    def _extract_products_from_json_ld(self, page_html: str) -> Dict[str, Dict[str, Any]]:
        products: Dict[str, Dict[str, Any]] = {}
        scripts = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for script_body in scripts:
            raw_json = html_module.unescape(script_body).strip()
            if not raw_json:
                continue
            try:
                payload = json.loads(raw_json)
            except Exception:
                continue

            for product in self._iter_product_dicts(payload):
                item_payload = self._build_product_payload(product)
                if not item_payload:
                    continue
                products[item_payload["id"]] = item_payload
        return products

    def _needs_pdp_enrichment(self, product: Dict[str, Any]) -> bool:
        # Only fetch the PDP page when the barcode/gtin is missing.
        # These are the only fields that matter for cross-market matching.
        # brand, description, image_url, unit, stock_general are nice-to-have
        # but not worth an extra HTTP request per product on a category scrape.
        # Regular/promo price is always present from the category page JSON-LD.
        return (
            product.get("barcode") in (None, "")
            and product.get("gtin") in (None, "")
        )

    def _merge_product_data(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in incoming.items():
            if key in {"id", "store_id", "zip_code", "last_updated"}:
                continue
            if merged.get(key) in (None, "") and value not in (None, ""):
                merged[key] = value
        merged["store_id"] = self._active_store_id
        return merged

    def _extract_single_product_from_html(self, page_html: str) -> Optional[Dict[str, Any]]:
        candidates = self._extract_products_from_json_ld(page_html)
        if not candidates:
            return None
        # PDP normally has exactly one product. If multiple, choose one with barcode first.
        prioritized = sorted(
            candidates.values(),
            key=lambda item: (
                0 if item.get("barcode") else 1,
                0 if item.get("gtin") else 1,
                0 if item.get("description") else 1,
            ),
        )
        return prioritized[0] if prioritized else None

    def _enrich_products_from_pdp(self, products: Dict[str, Dict[str, Any]]) -> None:
        if not self._pdp_enrich_enabled:
            return

        target_ids = [
            offer_id
            for offer_id, product in products.items()
            if self._needs_pdp_enrichment(product) and product.get("product_url")
        ]
        if not target_ids:
            return

        max_count = self._pdp_enrich_limit if self._pdp_enrich_limit > 0 else len(target_ids)
        target_ids = target_ids[:max_count]

        def fetch_one(offer_id: str):
            product = products.get(offer_id)
            if not product:
                return offer_id, None
            product_url = str(product.get("product_url") or "").strip()
            if not product_url:
                return offer_id, None

            with self._pdp_cache_lock:
                cached = self._pdp_cache.get(product_url, _MISSING)

            if cached is _MISSING:
                try:
                    response = self._get_thread_session().get(product_url, timeout=25)
                except Exception:
                    return offer_id, None
                if response.status_code != 200 or not response.text:
                    return offer_id, None
                extracted = self._extract_single_product_from_html(response.text) or {}
                with self._pdp_cache_lock:
                    self._pdp_cache[product_url] = extracted
                cached = extracted
                if self._pdp_enrich_delay_seconds > 0:
                    time.sleep(self._pdp_enrich_delay_seconds)

            return offer_id, cached if cached else None

        enriched = 0
        workers = min(self._pdp_workers, len(target_ids))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for offer_id, cached in executor.map(fetch_one, target_ids):
                if cached:
                    products[offer_id] = self._merge_product_data(products[offer_id], cached)
                    enriched += 1

        print(
            "Carrefour PDP enrichment: "
            f"enriched={enriched} candidates={len(target_ids)} limit={max_count}"
        )

    def _extract_products_from_links(self, page_html: str) -> Dict[str, Dict[str, Any]]:
        products: Dict[str, Dict[str, Any]] = {}
        decoded = html_module.unescape(page_html)
        pattern = re.compile(
            r'<a[^>]+href=["\'](/produto/[^"\']+)["\'][^>]*>(.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(decoded):
            href = match.group(1)
            anchor_html = match.group(2)
            text = re.sub(r"<[^>]+>", " ", anchor_html)
            raw_name = " ".join(text.split()).strip()
            name = self._clean_product_name(raw_name) or raw_name
            if not name:
                continue

            tail = decoded[match.end() : match.end() + 320]
            # Extract from both anchor text and nearby HTML to survive layout changes.
            prices = self._extract_prices_from_text(f"{raw_name} {tail}")
            regular_price = prices[0] if prices else None
            promo_price = prices[1] if len(prices) > 1 else None
            if regular_price is None and promo_price is not None:
                regular_price = promo_price
                promo_price = None
            if promo_price is not None and regular_price is not None and promo_price >= regular_price:
                promo_price = None

            product_url = f"{self.BASE_URL}{href}"
            offer_id = self.db.build_offer_id("carrefour", self._active_store_id, None, None, name)
            if not offer_id:
                continue

            products[offer_id] = {
                "id": offer_id,
                "product_name": name,
                "brand": None,
                "description": None,
                "regular_price": regular_price,
                "promo_price": promo_price,
                "promo_min_quantity": None,
                "unit": None,
                "gtin": None,
                "barcode": None,
                "product_url": product_url,
                "image_url": None,
                "stock_balance": None,
                "stock_general": None,
                "sold_quantity": None,
                "offer_name": None,
                "offer_tag": None,
                "app_membership_required": False,
                "promo_end_at": None,
                "last_updated": datetime.now().isoformat(),
                "store_id": self._active_store_id,
                "zip_code": self._current_zip_code,
            }
        return products

    @staticmethod
    def _build_page_url(base_url: str, page: int) -> str:
        parsed = urlparse(base_url)
        query = urlencode({"page": str(page)})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))

    def _fetch_department_products(
        self,
        department_url: str,
        *,
        max_pages: int = 150,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        products_by_id: Dict[str, Dict[str, Any]] = {}

        for page in range(1, max_pages + 1):
            page_url = self._build_page_url(department_url, page)
            try:
                response = self._get_thread_session().get(page_url, timeout=30)
            except Exception as exc:
                print(f"Carrefour: error on page {page_url}: {exc}")
                break

            if response.status_code != 200 or not response.text:
                break

            html = response.text
            parsed_products = self._extract_products_from_json_ld(html)
            if not parsed_products:
                parsed_products = self._extract_products_from_links(html)

            # NOTE: PDP enrichment is done AFTER all pages are collected (see below),
            # not per-page — calling it here caused 21 HTTP requests per page.

            new_count = 0
            for offer_id, item in parsed_products.items():
                if offer_id not in products_by_id:
                    new_count += 1
                products_by_id[offer_id] = item
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            print(
                f"  Carrefour {department_url} page={page}: "
                f"items={len(parsed_products)} new={new_count} total={len(products_by_id)}"
            )

            if new_count == 0:
                break
            if max_items is not None and len(products_by_id) >= max_items:
                break

            time.sleep(0.1)

        # Enrich all products for this department in one pass after pagination completes.
        # This prevents per-page PDP fetches (which multiplied HTTP calls by page count).
        self._enrich_products_from_pdp(products_by_id)

        return list(products_by_id.values())

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        print("Fetching Carrefour departamentos offers...")
        self._current_zip_code = zip_code
        self.resolve_store(zip_code)

        max_items = limit if isinstance(limit, int) and limit > 0 else None
        department_urls = self.discover_department_urls() or list(self.DEFAULT_DEPARTMENT_URLS)

        # Per-department limit: divide evenly so each worker stops early in test/limit mode.
        # Each worker fetches at most this many products; global dedup enforces the total cap.
        per_dept_limit: Optional[int] = None
        if max_items is not None:
            import math
            per_dept_limit = max(1, math.ceil(max_items / max(len(department_urls), 1)))

        # Fetch all departments in parallel; merge results in original order afterwards
        dept_results: Dict[str, List[Dict[str, Any]]] = {}
        workers = min(self._dept_workers, len(department_urls))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_url = {
                executor.submit(self._fetch_department_products, url, limit=per_dept_limit): url
                for url in department_urls
            }
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    dept_results[url] = future.result()
                except Exception as exc:
                    print(f"Carrefour {url}: fetch failed: {exc}")
                    dept_results[url] = []

        all_products: Dict[str, Dict[str, Any]] = {}
        for department_url in department_urls:
            dept_products = dept_results.get(department_url, [])
            new_in_dept = sum(1 for product in dept_products if product["id"] not in all_products)
            for product in dept_products:
                all_products[product["id"]] = product
            print(
                f"Carrefour {department_url}: {len(dept_products)} items "
                f"({new_in_dept} new), global total={len(all_products)}"
            )

        result = list(all_products.values())
        if max_items is not None:
            result = result[:max_items]

        print(f"Carrefour: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = CarrefourDepartamentosScraper()
    offers = scraper.fetch_offers("08032-230", limit=100)
    print(f"Total offers: {len(offers)}")
    for offer in offers[:3]:
        print(offer)
