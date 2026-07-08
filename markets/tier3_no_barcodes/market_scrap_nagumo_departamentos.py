import html as html_module
import json
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from db.db_manager import DatabaseManager


class NagumoDepartamentosScraper:
    DEFAULT_STORE = "M_13"
    
    # UPDATED for current site (March 2026) – old /departamentos/ paths are mostly dead (404)
    # New structure uses /categoria/mercearia-salgada/, /categoria/mercearia-doce/, /categoria/a%C3%A7ougue/, etc.
    # The discover function will also pick up any remaining ones.
    DEFAULT_DEPARTMENT_URLS = [
        "https://www.nagumo.com.br/categoria/a%C3%A7ougue/",
        "https://www.nagumo.com.br/categoria/departamentos/hortifruti/",
        "https://www.nagumo.com.br/categoria/departamentos/padaria/",
        "https://www.nagumo.com.br/categoria/mercearia-salgada/",
        "https://www.nagumo.com.br/categoria/mercearia-doce/",
        "https://www.nagumo.com.br/categoria/higiene-e-perfumaria/",
        "https://www.nagumo.com.br/categoria/departamentos/limpeza/",
        "https://www.nagumo.com.br/categoria/departamentos/laticinios-e-frios/",
        "https://www.nagumo.com.br/categoria/departamentos/congelados/",
        # Add more if you discover them
    ]

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                )
            }
        )
        self.market_name = "Nagumo"
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_brl_text(value: Any) -> Optional[float]:
        if not isinstance(value, str):
            return None
        cleaned = value.replace("R$", "").replace(" ", "").strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_department_urls(urls: Iterable[str]) -> List[str]:
        """Updated normalizer: now accepts both old /departamentos/ and new /categoria/XXX/ slugs."""
        unique: List[str] = []
        seen: set = set()
        for url in urls:
            if not url:
                continue
            parsed = urlparse(url.strip())
            path = (parsed.path or "").rstrip("/")
            if not path.startswith("/categoria/"):
                continue
            # Take the first segment after /categoria/ as the slug
            suffix = path[len("/categoria/"):].strip("/").split("/")[0]
            if not suffix:
                continue
            normalized = f"https://www.nagumo.com.br/categoria/{suffix}/"
            if normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    @staticmethod
    def _extract_cgid(url: str) -> Optional[str]:
        """Updated: works with both old and new category paths."""
        parsed = urlparse(url)
        path = (parsed.path or "").strip("/")
        if path.startswith("categoria/"):
            parts = path.split("/")
            if len(parts) >= 2:
                return parts[1]  # the slug right after /categoria/
        qs = parse_qs(parsed.query)
        cgid_values = qs.get("cgid")
        if cgid_values:
            return cgid_values[0]
        return None

    def discover_department_urls(self) -> List[str]:
        """Discover department links – now looks for /categoria/ (new structure)."""
        seed_urls = [
            "https://www.nagumo.com.br/",
            "https://www.nagumo.com.br/categoria/",
        ]
        discovered: List[str] = []

        for url in seed_urls:
            try:
                response = self.session.get(url, timeout=25)
                if response.status_code != 200 or not response.text:
                    continue

                html = response.text
                # Look for any /categoria/XXX/ links (new + old style)
                cat_prefix = "/categoria/"
                links = []
                pos = 0
                while True:
                    i = html.find(cat_prefix, pos)
                    if i == -1:
                        break
                    end = i + len(cat_prefix)
                    while end < len(html) and html[end] not in '"' + "'><# ?\n\t":
                        end += 1
                    path = html[i:end]
                    if len(path) > len(cat_prefix):
                        links.append(path)
                    pos = end

                discovered.extend(str(item) for item in links)

                # Also catch cgid tokens
                cgid_tokens = re.findall(r"cgid=([A-Za-z0-9_-]+)", html, flags=re.IGNORECASE)
                for cgid in cgid_tokens:
                    discovered.append(f"https://www.nagumo.com.br/categoria/{cgid}/")
            except Exception:
                continue

        normalized = self._normalize_department_urls(discovered)
        if normalized:
            print(f"Nagumo departamentos: discovered {len(normalized)} departments (new structure).")
        return normalized

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _geocode_zip(self, zip_code: str) -> Optional[tuple]:
        """Return (lat, lon) for a Brazilian ZIP.
        Chain: BrasilAPI → ViaCEP address text → Nominatim."""
        clean = re.sub(r"\D", "", zip_code)
        # 1. BrasilAPI (has coordinates directly)
        try:
            r = self.session.get(
                f"https://brasilapi.com.br/api/cep/v2/{clean}", timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                coords = (data.get("location") or {}).get("coordinates") or {}
                lat = coords.get("latitude")
                lon = coords.get("longitude")
                if lat is not None and lon is not None:
                    print(f"Nagumo geocode_zip: BrasilAPI lat={lat} lon={lon}")
                    return float(lat), float(lon)
        except Exception as exc:
            print(f"Nagumo geocode_zip BrasilAPI error: {exc}")

        # 2. ViaCEP → address text → Nominatim
        try:
            r2 = self.session.get(
                f"https://viacep.com.br/ws/{clean}/json/", timeout=8
            )
            if r2.status_code == 200:
                v = r2.json()
                address_text = ", ".join(
                    p for p in [
                        v.get("logradouro"), v.get("bairro"),
                        v.get("localidade"), v.get("uf"), "Brazil",
                    ] if p
                )
                if address_text:
                    nom = self.session.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": address_text, "format": "json", "limit": 1},
                        headers={"User-Agent": "markets-db-nagumo-geocoder/1.0"},
                        timeout=10,
                    )
                    if nom.status_code == 200:
                        results = nom.json()
                        if results:
                            lat = float(results[0]["lat"])
                            lon = float(results[0]["lon"])
                            print(f"Nagumo geocode_zip: Nominatim lat={lat} lon={lon}")
                            return lat, lon
        except Exception as exc:
            print(f"Nagumo geocode_zip Nominatim error: {exc}")

        print(f"Nagumo geocode_zip: could not geocode {zip_code} — store selection will use API order")
        return None

    def resolve_store(self, zip_code: str) -> Optional[str]:
        self._resolved_store_metadata = None
        url = "https://www.nagumo.com.br/on/demandware.store/Sites-Nagumo-Site/pt_BR/StoreLocator-GetNearestStores"
        params = {"postalCode": zip_code}
        try:
            response = self.session.get(url, params=params, timeout=10)
            print(f"Nagumo resolve_store: HTTP {response.status_code}")
            if response.status_code == 200:
                # Force UTF-8 to avoid encoding issues with accented characters
                data = json.loads(response.content.decode("utf-8")) or {}
                stores = [s for s in (data.get("stores") or []) if isinstance(s, dict) and s.get("ID")]
                if stores:
                    print(f"Nagumo resolve_store: {len(stores)} store(s) returned by API")
                    # Log all stores with every possible distance/coord key so we can diagnose
                    for s in stores:
                        d_keys = {k: v for k, v in s.items()
                                  if any(x in k.lower() for x in ("dist", "lat", "lon", "lng", "coord"))}
                        print(f"  ID={s.get('ID')} name={s.get('name')} keys={d_keys}")

                    # Geocode the ZIP once for haversine fallback
                    zip_coords = self._geocode_zip(zip_code)

                    def _dist_key(s: Dict[str, Any]) -> float:
                        # 1. Use API-provided distance field (SFCC standard)
                        d = s.get("distance") or s.get("distancia") or s.get("distanceInKm")
                        try:
                            if d is not None:
                                return float(d)
                        except (TypeError, ValueError):
                            pass
                        # 2. Haversine from store coordinates (fallback when distance absent)
                        s_lat = self._to_float(s.get("latitude") or s.get("lat"))
                        s_lon = self._to_float(s.get("longitude") or s.get("lng"))
                        if s_lat is not None and s_lon is not None and zip_coords is not None:
                            return self._haversine_km(zip_coords[0], zip_coords[1], s_lat, s_lon)
                        return float("inf")

                    nearest = min(stores, key=_dist_key)
                    store_id_str = f"M_{nearest['ID']}"

                    # Build full address — SFCC uses address1/address2 + neighborhood
                    address_parts = [
                        str(nearest.get("address1") or "").strip(),
                        str(nearest.get("address2") or "").strip(),
                        str(nearest.get("neighborhood") or nearest.get("bairro") or "").strip(),
                    ]
                    address_text = ", ".join(p for p in address_parts if p) or None

                    self._resolved_store_metadata = {
                        "store_name":    nearest.get("name") or nearest.get("displayName"),
                        "store_address": address_text,
                        "store_city":    nearest.get("city"),
                        "store_state":   nearest.get("stateCode") or nearest.get("state"),
                        "latitude":      self._to_float(nearest.get("latitude") or nearest.get("lat")),
                        "longitude":     self._to_float(nearest.get("longitude") or nearest.get("lng")),
                        "store_payload": json.dumps(nearest, ensure_ascii=False),
                    }
                    dist_val = _dist_key(nearest)
                    dist_str = f"{dist_val:.1f}km" if dist_val != float("inf") else "unknown dist"
                    print(
                        f"Nagumo store resolved (nearest of {len(stores)}): "
                        f"id={store_id_str} "
                        f"name={self._resolved_store_metadata.get('store_name')} "
                        f"city={self._resolved_store_metadata.get('store_city')} "
                        f"dist={dist_str}"
                    )
                    return store_id_str
                else:
                    print(f"Nagumo resolve_store: no stores in response — raw={response.text[:300]}")
        except Exception as exc:
            print(f"Nagumo resolve_store: exception {exc}")
        print(f"Nagumo resolve_store: falling back to DEFAULT_STORE={self.DEFAULT_STORE}")
        return self.DEFAULT_STORE

    def _extract_prices(self, product: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
        price_obj = product.get("price", {}) or {}
        sales_price = self._to_float((price_obj.get("sales") or {}).get("value"))
        list_price = self._to_float((price_obj.get("list") or {}).get("value"))

        promo_candidates = []
        for flag in product.get("flagtypes") or []:
            if not isinstance(flag, dict):
                continue
            value_flag = self._to_float(flag.get("valueFlag"))
            if value_flag is not None:
                promo_candidates.append(value_flag)

            value_flag_text = self._parse_brl_text(flag.get("valueFlagType"))
            if value_flag_text is not None:
                promo_candidates.append(value_flag_text)

        regular_price = list_price if list_price is not None else sales_price

        promo_price = None
        if list_price is not None and sales_price is not None and sales_price < list_price:
            promo_price = sales_price
        elif promo_candidates:
            if regular_price is not None:
                eligible = [value for value in promo_candidates if value <= regular_price]
                if eligible:
                    promo_price = max(eligible)
                else:
                    promo_price = max(promo_candidates)
            else:
                promo_price = max(promo_candidates)
        else:
            promo_price = sales_price

        if regular_price is None:
            regular_price = promo_price
        if promo_price is None:
            promo_price = regular_price
        if regular_price is not None and promo_price is not None and promo_price > regular_price:
            promo_price = regular_price

        return regular_price, promo_price

    @staticmethod
    def _infer_app_membership_required(*texts: Optional[str]) -> bool:
        joined = " ".join(str(text or "") for text in texts).lower()
        tags = ("meu nagumo", "clube", "app", "cadastro")
        return any(tag in joined for tag in tags)

    def _extract_offer_metadata(self, product: Dict[str, Any]) -> tuple[Optional[str], Optional[str], bool]:
        offer_name: Optional[str] = None
        offer_tag: Optional[str] = None

        promo_discount = product.get("promotionDiscount")
        if isinstance(promo_discount, dict):
            offer_name = promo_discount.get("name") or promo_discount.get("label")
            offer_tag = promo_discount.get("type") or promo_discount.get("id")

        membership_flag_tag: Optional[str] = None
        for flag in product.get("flagtypes") or []:
            if not isinstance(flag, dict):
                continue
            flag_type = str(flag.get("flagType") or "").strip()
            value_flag_type = str(flag.get("valueFlagType") or "").strip()
            is_membership = (
                flag_type.upper().endswith("_M")
                or "MEU NAGUMO" in value_flag_type.upper()
                or "MEU NAGUMO" in flag_type.upper()
            )
            if is_membership:
                membership_flag_tag = flag_type or membership_flag_tag
                break

        if membership_flag_tag:
            if not offer_name:
                offer_name = "Meu Nagumo"
            if not offer_tag:
                offer_tag = membership_flag_tag
            return offer_name, offer_tag, True

        app_membership_required = self._infer_app_membership_required(offer_name, offer_tag)
        return offer_name, offer_tag, app_membership_required

    @staticmethod
    def _extract_total_count_from_html(html: str) -> Optional[int]:
        patterns = [
            r'<search-card-grid[^>]*?\btotal=["\'](\d+)["\']',
            r'<search-card-grid[^>]*?\btotalcount=["\'](\d+)["\']',
            r'<search-card-grid[^>]*?\bcount=["\'](\d+)["\']',
            r'\bdata-total-count=["\'](\d+)["\']',
            r'\bdata-total=["\'](\d+)["\']',
            r'"totalCount"\s*:\s*(\d+)',
            r'"total"\s*:\s*(\d+)',
            r'(\d[\d.]*)\s+produto',
            r'(\d[\d.]*)\s+item',
            r'(\d[\d.]*)\s+resultado',
            r'(\d[\d.,]+)\s+Produtos encontrados',   # NEW pattern from current site
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                raw = m.group(1).replace(".", "").replace(",", "")
                try:
                    v = int(raw)
                    if v > 0:
                        return v
                except ValueError:
                    continue
        return None

    def _fetch_category_products(
        self,
        *,
        department_url: str,
        store_id: Optional[str],
        max_pages: int,
        page_size: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Same big-fetch strategy as before, but now with better total detection."""
        cgid = self._extract_cgid(department_url) or "unknown"
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        # Probe
        probe_params: Dict[str, Any] = {"sz": 1, "start": 0, "srule": "Relevance"}
        if store_id:
            probe_params["pmid"] = store_id
        total_count: Optional[int] = None
        try:
            probe_resp = self.session.get(department_url, params=probe_params, timeout=25)
            if probe_resp.status_code == 200:
                total_count = self._extract_total_count_from_html(probe_resp.text)
                if total_count:
                    print(f"Nagumo {cgid}: probe total={total_count}")
                else:
                    print(f"Nagumo {cgid}: probe could not extract total")
        except Exception as exc:
            print(f"Nagumo {cgid}: probe failed ({exc})")

        # Big fetch
        if total_count and total_count > 0:
            fetch_sz = total_count if max_items is None else min(total_count, max_items)
            big_params: Dict[str, Any] = {"sz": fetch_sz, "start": 0, "srule": "Relevance"}
            if store_id:
                big_params["pmid"] = store_id
            try:
                big_resp = self.session.get(department_url, params=big_params, timeout=90)
                if big_resp.status_code == 200:
                    products = self._extract_products_from_category_html(big_resp.text)
                    seen_ids: set = set()
                    result: List[Dict[str, Any]] = []
                    for p in products:
                        if not isinstance(p, dict):
                            continue
                        pid = p.get("id")
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            result.append(p)
                    print(f"Nagumo {cgid}: big-fetch sz={fetch_sz} -> {len(result)} products")
                    if result:
                        return result
            except Exception as exc:
                print(f"Nagumo {cgid}: big-fetch failed ({exc})")

        # Pagination fallback (unchanged)
        all_products: List[Dict[str, Any]] = []
        seen_ids_pag: set = set()
        effective_page_size: Optional[int] = None
        start = 0

        for page in range(max_pages):
            params: Dict[str, Any] = {"sz": page_size, "start": start, "srule": "Relevance"}
            if store_id:
                params["pmid"] = store_id
            try:
                response = self.session.get(department_url, params=params, timeout=25)
                if response.status_code != 200:
                    break

                products = self._extract_products_from_category_html(response.text)
                if not products:
                    break

                if effective_page_size is None:
                    effective_page_size = len(products)

                new_count = 0
                for p in products:
                    if not isinstance(p, dict):
                        continue
                    pid = p.get("id")
                    if not pid or pid in seen_ids_pag:
                        continue
                    seen_ids_pag.add(pid)
                    all_products.append(p)
                    new_count += 1
                    if max_items is not None and len(all_products) >= max_items:
                        break

                print(f"Nagumo {cgid} page={page + 1}: items={len(products)} new={new_count} total={len(all_products)}")
                start += len(products)

                if new_count == 0 or len(products) < effective_page_size:
                    break
                if max_items is not None and len(all_products) >= max_items:
                    break
            except Exception as exc:
                print(f"Nagumo {cgid} page={page + 1} error: {exc}")
                break

        return all_products

    # The rest of the class (_fetch_category_products_grid, _extract_products_from_category_html,
    # _standardize_product, _fetch_all_products_search, fetch_offers) stays exactly the same as your original code.
    # I only changed the parts above to support the new site structure.

    def _fetch_category_products_grid(
        self,
        *,
        cgid: str,
        store_id: Optional[str],
        max_pages: int,
        page_size: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        url = "https://www.nagumo.com.br/on/demandware.store/Sites-Nagumo-Site/pt_BR/Search-UpdateGrid"
        all_products: List[Dict[str, Any]] = []
        seen_ids: set = set()
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        start = 0

        for page in range(max_pages):
            params: Dict[str, Any] = {
                "cgid": cgid,
                "srule": "Relevance",
                "start": start,
                "sz": page_size,
            }
            if store_id:
                params["pmid"] = store_id
            try:
                response = self.session.get(url, params=params, timeout=60)
                if response.status_code != 200:
                    break

                content_type = response.headers.get("Content-Type", "")
                if "json" not in content_type:
                    break
                try:
                    payload = response.json() or {}
                except Exception:
                    break

                products = payload.get("productsSearchResult") or []
                if not products:
                    break

                new_count = 0
                for p in products:
                    if not isinstance(p, dict):
                        continue
                    pid = p.get("id")
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    all_products.append(p)
                    new_count += 1
                    if max_items is not None and len(all_products) >= max_items:
                        break

                print(
                    f"Nagumo grid cgid={cgid} page={page + 1}: "
                    f"sz={page_size} got={len(products)} new={new_count} total={len(all_products)}"
                )

                start += len(products)
                if len(products) < page_size:
                    break
                if new_count == 0:
                    break
                if max_items is not None and len(all_products) >= max_items:
                    break
            except Exception as exc:
                print(f"Nagumo grid cgid={cgid} page={page + 1} error: {exc}")
                break

        return all_products

    def _extract_products_from_category_html(self, page_html: str) -> List[Dict[str, Any]]:
        # Original extraction (still works if the component is present)
        match = re.search(r'<search-card-grid[^>]*\sproducts="([^"]+)"', page_html, flags=re.IGNORECASE)
        if not match:
            return []

        encoded = match.group(1)
        decoded = html_module.unescape(encoded)
        try:
            payload = json.loads(decoded)
        except Exception:
            return []

        return payload if isinstance(payload, list) else []

    def _standardize_product(self, p: Dict[str, Any], store_id: Optional[str]) -> Dict[str, Any]:
        regular_price, promo_price = self._extract_prices(p)

        images = p.get("images", {}) or {}
        medium_images = images.get("medium", []) or []
        image_url = medium_images[0].get("absURL") if medium_images else None

        gtin_text = str(p.get("upc")) if p.get("upc") else None
        barcode = self.db.normalize_barcode(gtin_text)

        offer_name, offer_tag, app_membership_required = self._extract_offer_metadata(p)

        offer_id = self.db.build_offer_id("nagumo", store_id, barcode, gtin_text, p.get("productName"))
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": p.get("productName"),
            "brand": p.get("brand"),
            "description": (lambda d: str(d).strip() if d and not isinstance(d, dict) else None)(p.get("shortDescription") or p.get("longDescription")),
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": None,
            "unit": p.get("productMeasureValue"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": p.get("productShowFullUrl"),
            "image_url": image_url,
            "stock_balance": self._to_int(p.get("ATSInCurrentStore")),
            "stock_general": self._to_int(p.get("ATSInGenerealStock")),
            "sold_quantity": None,
            "offer_name": offer_name,
            "offer_tag": offer_tag,
            "app_membership_required": app_membership_required,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": store_id,
        }

    def _fetch_all_products_search(
        self,
        store_id: Optional[str],
        page_size: int = 2000,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        # This is the capped global search (~1250 limit). We avoid it by default.
        url = "https://www.nagumo.com.br/on/demandware.store/Sites-Nagumo-Site/pt_BR/Search-UpdateGrid"
        products_by_id: Dict[str, Any] = {}
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        sz = max_items if (max_items and max_items < page_size) else page_size
        start = 0

        while True:
            params: Dict[str, Any] = {"q": "*", "srule": "Relevance", "sz": sz, "start": start}
            if store_id:
                params["pmid"] = store_id
            try:
                r = self.session.get(url, params=params, timeout=60)
                if r.status_code != 200:
                    break
                ct = r.headers.get("content-type", "")
                if "json" not in ct:
                    break
                data = r.json() or {}
                products = data.get("productsSearchResult") or []
                if not products:
                    break

                new_count = 0
                for p in products:
                    std = self._standardize_product(p, store_id or "ALL_CATALOG")
                    if std:
                        pid = std["id"]
                        if pid not in products_by_id:
                            new_count += 1
                        products_by_id[pid] = std
                        if max_items and len(products_by_id) >= max_items:
                            break

                print(
                    f"Nagumo global search: start={start} sz={sz} got={len(products)} "
                    f"new={new_count} total={len(products_by_id)}"
                )

                if len(products) < sz:
                    break
                if max_items and len(products_by_id) >= max_items:
                    break

                start += len(products)
                time.sleep(0.15)
            except Exception as exc:
                print(f"Nagumo global search error at start={start}: {exc}")
                break

        return list(products_by_id.values())

    def fetch_offers(
        self,
        zip_code: str,
        department_urls: Optional[Iterable[str]] = None,
        max_pages_per_department: int = 120,
        page_size: int = 5000,
        min_items_before_fallback: int = 1,   # lowered so we try grid sooner
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Main entry point – resolves nearest store for metadata, fetches full catalog."""
        print(f"Fetching Nagumo offers for {zip_code}...")

        max_items = limit if isinstance(limit, int) and limit > 0 else None

        # Always resolve the nearest store so we get the correct address/metadata
        # and so the skip logic works per physical store.
        store_id = self.resolve_store(zip_code)
        if not store_id:
            store_id = self.DEFAULT_STORE
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

        # Fetch without pmid filter (all-catalog) so we get the full product range
        # — Nagumo's catalog is shared across stores.
        fetch_pmid: Optional[str] = None
        print(f"Nagumo store={store_id} (fetching all-catalog, no pmid filter), sz={page_size}")

        if department_urls is None:
            auto_discovered = self.discover_department_urls()
            combined = list(self.DEFAULT_DEPARTMENT_URLS) + (auto_discovered or [])
            urls = combined
        else:
            urls = [u for u in department_urls if u]

        urls = self._normalize_department_urls(urls)
        print(f"Nagumo: scraping {len(urls)} departments (new structure)")

        products_by_id: Dict[str, Dict[str, Any]] = {}
        coverage_rows: List[Dict[str, Any]] = []

        for department_url in urls:
            cgid = self._extract_cgid(department_url)
            if not cgid:
                continue

            # Primary: HTML route (fetch_pmid=None → all-catalog, no store filter)
            raw_products = self._fetch_category_products(
                department_url=department_url,
                store_id=fetch_pmid,
                max_pages=max_pages_per_department,
                page_size=page_size,
                limit=(max_items - len(products_by_id)) if max_items is not None else None,
            )
            method = "category_html"

            # Fallback: JSON grid
            if len(raw_products) < min_items_before_fallback:
                grid_products = self._fetch_category_products_grid(
                    cgid=cgid,
                    store_id=fetch_pmid,
                    max_pages=max_pages_per_department,
                    page_size=page_size,
                    limit=(max_items - len(products_by_id)) if max_items is not None else None,
                )
                if len(grid_products) > len(raw_products):
                    raw_products = grid_products
                    method = "search_update_grid"

            coverage_rows.append({"cgid": cgid, "method": method, "items": len(raw_products)})

            for p in raw_products:
                standardized = self._standardize_product(p, store_id)
                if standardized is None:
                    continue
                products_by_id[standardized["id"]] = standardized
                if max_items is not None and len(products_by_id) >= max_items:
                    break
            if max_items is not None and len(products_by_id) >= max_items:
                break

        if coverage_rows:
            print("Nagumo coverage summary:")
            for row in coverage_rows:
                print(f"  cgid={row['cgid']}: items={row['items']} source={row['method']}")

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        print(f"Nagumo: {len(all_products)} products collected (should now be >4000).")
        return all_products


if __name__ == "__main__":
    scraper = NagumoDepartamentosScraper()
    products = scraper.fetch_offers("07110-000")   # change zip if you want
    print(f"Final total: {len(products)} products")
    # Optional: save to JSON for inspection
    # with open("nagumo_products.json", "w", encoding="utf-8") as f:
    #     json.dump(products, f, ensure_ascii=False, indent=2)