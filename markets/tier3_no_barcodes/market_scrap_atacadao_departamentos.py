import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import config
from db.db_manager import DatabaseManager


class AtacadaoDepartamentosScraper:
    DEFAULT_STORE = "atacadaobr60"
    BASE_URL = "https://www.atacadao.com.br"
    GRAPHQL_URL = "https://www.atacadao.com.br/api/graphql"
    CATEGORY_SITEMAP_URL = "https://www.atacadao.com.br/sitemap/category-0.xml"

    # Fallback slugs if sitemap discovery fails
    DEFAULT_SLUGS = [
        "bebidas",
        "mercearia",
        "limpeza",
        "higiene-e-beleza",
        "laticinios-e-frios",
        "congelados",
        "carnes",
        "hortifruti",
        "padaria",
        "pet-shop",
        "bazar",
        "eletronicos",
    ]

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
        self.market_name = "Atacadão"
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None
        self._catalog_barcode_cache: Dict[str, Optional[str]] = {}
        self._catalog_ean_lookup_count = 0
        self._catalog_ean_lookup_hits = 0
        self._catalog_ean_lookup_budget = self._read_catalog_ean_lookup_budget()
        self._catalog_ean_lookup_workers = self._read_catalog_ean_lookup_workers()

    @staticmethod
    def _read_catalog_ean_lookup_budget() -> Optional[int]:
        raw = os.getenv("ATACADAO_CATALOG_EAN_MAX_CALLS", "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = int(getattr(config, "ATACADAO_CATALOG_EAN_MAX_CALLS", 0) or 0)
        else:
            value = int(getattr(config, "ATACADAO_CATALOG_EAN_MAX_CALLS", 0) or 0)
        return value if value > 0 else None

    @staticmethod
    def _read_catalog_ean_lookup_workers() -> int:
        raw = os.getenv("ATACADAO_CATALOG_EAN_WORKERS", "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = int(getattr(config, "ATACADAO_CATALOG_EAN_WORKERS", 8) or 8)
        else:
            value = int(getattr(config, "ATACADAO_CATALOG_EAN_WORKERS", 8) or 8)
        return max(1, min(value, 16))

    # ------------------------------------------------------------------ helpers

    def _get_region_id(self, seller_id: str) -> str:
        normalized = str(seller_id or self.DEFAULT_STORE).strip() or self.DEFAULT_STORE
        return base64.b64encode(f"SW#{normalized}".encode("utf-8")).decode("ascii")

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_valid_gtin(value: Any) -> Optional[str]:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(digits) in (8, 12, 13, 14):
            return digits
        return None

    def _fetch_catalog_ean_by_product_id(self, product_id: Any) -> Optional[str]:
        pid = "".join(ch for ch in str(product_id or "") if ch.isdigit())
        if not pid:
            return None

        if pid in self._catalog_barcode_cache:
            return self._catalog_barcode_cache[pid]

        if (
            self._catalog_ean_lookup_budget is not None
            and self._catalog_ean_lookup_count >= self._catalog_ean_lookup_budget
        ):
            self._catalog_barcode_cache[pid] = None
            return None

        ean_value: Optional[str] = None
        try:
            self._catalog_ean_lookup_count += 1
            response = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/products/search",
                params={"fq": f"productId:{pid}"},
                timeout=15,
            )
            if response.status_code == 200:
                payload = response.json() or []
                if isinstance(payload, list):
                    for product in payload:
                        if not isinstance(product, dict):
                            continue
                        for item in (product.get("items") or []):
                            if not isinstance(item, dict):
                                continue
                            candidate = self._extract_valid_gtin(item.get("ean"))
                            if candidate:
                                ean_value = candidate
                                self._catalog_ean_lookup_hits += 1
                                break
                        if ean_value:
                            break
        except Exception:
            ean_value = None

        self._catalog_barcode_cache[pid] = ean_value
        return ean_value

    def _fetch_catalog_ean_for_pid_with_session(
        self,
        pid: str,
        session: requests.Session,
    ) -> Optional[str]:
        ean_value: Optional[str] = None
        try:
            response = session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/products/search",
                params={"fq": f"productId:{pid}"},
                timeout=15,
            )
            if response.status_code == 200:
                payload = response.json() or []
                if isinstance(payload, list):
                    for product in payload:
                        if not isinstance(product, dict):
                            continue
                        for item in (product.get("items") or []):
                            if not isinstance(item, dict):
                                continue
                            candidate = self._extract_valid_gtin(item.get("ean"))
                            if candidate:
                                ean_value = candidate
                                break
                        if ean_value:
                            break
        except Exception:
            ean_value = None
        return ean_value

    def _prefetch_catalog_eans_for_nodes(self, nodes: List[Dict[str, Any]]) -> None:
        missing_pids: List[str] = []
        for node in nodes:
            raw_gtin = self._extract_valid_gtin(node.get("gtin") or node.get("ean"))
            if raw_gtin:
                continue
            has_item_ean = False
            for sku in (node.get("items") or []):
                if not isinstance(sku, dict):
                    continue
                if self._extract_valid_gtin(sku.get("ean")):
                    has_item_ean = True
                    break
            if has_item_ean:
                continue

            pid = "".join(ch for ch in str(node.get("id") or "") if ch.isdigit())
            if not pid or pid in self._catalog_barcode_cache:
                continue
            missing_pids.append(pid)

        if not missing_pids:
            return

        unique_missing_pids = list(dict.fromkeys(missing_pids))
        if self._catalog_ean_lookup_budget is not None:
            remaining_budget = self._catalog_ean_lookup_budget - self._catalog_ean_lookup_count
            if remaining_budget <= 0:
                for pid in unique_missing_pids:
                    self._catalog_barcode_cache[pid] = None
                return
            unique_missing_pids = unique_missing_pids[:remaining_budget]

        thread_local = threading.local()

        def _get_thread_session() -> requests.Session:
            session = getattr(thread_local, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update(dict(self.session.headers))
                thread_local.session = session
            return session

        def _worker(pid: str) -> Tuple[str, Optional[str]]:
            session = _get_thread_session()
            return pid, self._fetch_catalog_ean_for_pid_with_session(pid, session)

        self._catalog_ean_lookup_count += len(unique_missing_pids)
        with ThreadPoolExecutor(max_workers=self._catalog_ean_lookup_workers) as executor:
            future_map = {executor.submit(_worker, pid): pid for pid in unique_missing_pids}
            for future in as_completed(future_map):
                pid, ean_value = future.result()
                if ean_value:
                    self._catalog_ean_lookup_hits += 1
                self._catalog_barcode_cache[pid] = ean_value

        for pid in missing_pids:
            self._catalog_barcode_cache.setdefault(pid, None)

    @staticmethod
    def _centavos_to_brl(value: Any) -> Optional[float]:
        """Convert VTEX price to BRL.
        VTEX FastStore may return centavos (27900 → R$279) or BRL (279.0 → R$279).
        Heuristic: floats with decimals = already BRL; integers ≥1000 = centavos.
        """
        if value is None:
            return None
        try:
            f_val = float(value)
            if f_val != int(f_val):          # has decimals → already BRL
                return round(f_val, 2)
            int_val = int(f_val)
            if int_val >= 1000:              # large integer → centavos
                return round(int_val / 100, 2)
            return round(f_val, 2)           # small integer → already BRL
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------- store resolution

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _geocode_zip(self, zip_code: str) -> Optional[Tuple[float, float]]:
        """Return (lat, lon) for a Brazilian ZIP.

        Tries in order:
        1. BrasilAPI  — fast, has built-in coords for most ZIPs
        2. ViaCEP + Nominatim — fallback when BrasilAPI has no coordinates
        """
        clean = re.sub(r"\D", "", zip_code)

        # 1. BrasilAPI
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
                    return float(lat), float(lon)
        except Exception as exc:
            print(f"Atacadao geocode_zip BrasilAPI error: {exc}")

        # 2. ViaCEP → address text → Nominatim
        try:
            via = self.session.get(
                f"https://viacep.com.br/ws/{clean}/json/", timeout=8
            )
            if via.status_code == 200:
                addr = via.json() or {}
                if not addr.get("erro"):
                    query = ", ".join(
                        p for p in [
                            addr.get("logradouro") or "",
                            addr.get("bairro") or "",
                            addr.get("localidade") or "",
                            addr.get("uf") or "",
                            "Brasil",
                        ] if p
                    )
                    nom = self.session.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": query, "format": "json", "limit": 1},
                        headers={"User-Agent": "markets_db/1.0 geocode"},
                        timeout=10,
                    )
                    if nom.status_code == 200:
                        results = nom.json() or []
                        if results:
                            return float(results[0]["lat"]), float(results[0]["lon"])
        except Exception as exc:
            print(f"Atacadao geocode_zip Nominatim error: {exc}")

        print(f"Atacadao geocode_zip: could not resolve coordinates for {zip_code}")
        return None

    @staticmethod
    def _extract_store_coords(address_obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        """Extract (lat, lon) from a VTEX address object."""
        lat = address_obj.get("latitude") or address_obj.get("lat")
        lon = address_obj.get("longitude") or address_obj.get("lng") or address_obj.get("lon")
        # VTEX sometimes uses geoCoordinates: [lon, lat]
        geo = address_obj.get("geoCoordinates") or address_obj.get("geoCoords")
        if lat is None and isinstance(geo, (list, tuple)) and len(geo) >= 2:
            lon, lat = float(geo[0]), float(geo[1])
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        return None, None

    def _fetch_seller_coords_batch(self, seller_ids: List[str]) -> Dict[str, Tuple[float, float]]:
        """Fetch coordinates for multiple VTEX sellers in one call via the seller-list API."""
        coords: Dict[str, Tuple[float, float]] = {}
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/seller/list",
                timeout=15,
            )
            if r.status_code == 200:
                for seller in (r.json() or []):
                    sid = str(
                        seller.get("SellerId") or seller.get("sellerId") or seller.get("id") or ""
                    ).strip()
                    if sid not in seller_ids:
                        continue
                    addr = seller.get("Address") or seller.get("address") or {}
                    geo = addr.get("GeoCoordinates") or addr.get("geoCoordinates") or []
                    lat = addr.get("Latitude") or addr.get("latitude")
                    lon = addr.get("Longitude") or addr.get("longitude")
                    if lat is None and isinstance(geo, (list, tuple)) and len(geo) >= 2:
                        lon, lat = float(geo[0]), float(geo[1])
                    if lat is not None and lon is not None:
                        try:
                            coords[sid] = (float(lat), float(lon))
                        except (TypeError, ValueError):
                            pass
            else:
                print(f"Atacadao seller-list API: HTTP {r.status_code}")
        except Exception as exc:
            print(f"Atacadao _fetch_seller_coords_batch error: {exc}")
        return coords

    def _fetch_seller_address(
        self, seller_id: str, zip_coords: Optional[Tuple[float, float]]
    ) -> Dict[str, Any]:
        """Fetch store address for a VTEX seller ID.

        Tries two public VTEX endpoints in order:
        1. /api/catalog_system/pub/seller/details/{id}  — returns PascalCase address
        2. /api/checkout/pub/pickup-points?lat=&lon=    — returns camelCase address
        Returns a normalised dict with lowercase keys, or {} on failure.
        """
        # 1. VTEX catalog seller details
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/seller/details/{seller_id}",
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json() or {}
                addr = data.get("Address") or data.get("address") or {}
                if addr and (addr.get("Street") or addr.get("street") or addr.get("City") or addr.get("city")):
                    geo = addr.get("GeoCoordinates") or addr.get("geoCoordinates") or []
                    return {
                        "street":       addr.get("Street")       or addr.get("street"),
                        "number":       addr.get("Number")       or addr.get("number"),
                        "complement":   addr.get("Complement")   or addr.get("complement"),
                        "neighborhood": addr.get("Neighborhood") or addr.get("neighborhood"),
                        "city":         addr.get("City")         or addr.get("city"),
                        "state":        addr.get("State")        or addr.get("state") or addr.get("stateCode"),
                        "latitude":     float(geo[1]) if len(geo) >= 2 else None,
                        "longitude":    float(geo[0]) if len(geo) >= 2 else None,
                    }
        except Exception as exc:
            print(f"Atacadao seller details lookup error: {exc}")

        # 2. VTEX checkout pickup-points (needs coordinates)
        if zip_coords:
            try:
                lat, lon = zip_coords
                r = self.session.get(
                    f"{self.BASE_URL}/api/checkout/pub/pickup-points",
                    params={"lat": lat, "lon": lon},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    points = data if isinstance(data, list) else (data.get("items") or [])
                    for point in points:
                        pp = point.get("pickupPoint") or point
                        pp_id = str(pp.get("pickupPointId") or pp.get("id") or "")
                        pp_sellers = pp.get("sellers") or []
                        if (seller_id in pp_id
                                or any(str(s.get("id") or "") == seller_id for s in pp_sellers)):
                            addr = pp.get("address") or {}
                            geo = addr.get("geoCoordinates") or addr.get("GeoCoordinates") or []
                            return {
                                "street":       addr.get("street")       or addr.get("Street"),
                                "number":       addr.get("number")       or addr.get("Number"),
                                "complement":   addr.get("complement")   or addr.get("Complement"),
                                "neighborhood": addr.get("neighborhood") or addr.get("Neighborhood"),
                                "city":         addr.get("city")         or addr.get("City"),
                                "state":        addr.get("state") or addr.get("stateCode") or addr.get("State"),
                                "latitude":     float(geo[1]) if len(geo) >= 2 else addr.get("latitude"),
                                "longitude":    float(geo[0]) if len(geo) >= 2 else addr.get("longitude"),
                            }
            except Exception as exc:
                print(f"Atacadao pickup-points lookup error: {exc}")

        return {}

    def _build_store_metadata(
        self, seller: Dict[str, Any], address_obj: Dict[str, Any]
    ) -> Dict[str, Any]:
        # Build full address string including number/complement/neighborhood
        store_address = ", ".join(
            part for part in [
                address_obj.get("street")       or address_obj.get("Street"),
                address_obj.get("number")       or address_obj.get("Number"),
                address_obj.get("complement")   or address_obj.get("Complement"),
                address_obj.get("neighborhood") or address_obj.get("Neighborhood"),
            ] if part
        ) or None
        s_lat, s_lon = self._extract_store_coords(address_obj)
        # _extract_store_coords checks camelCase; also handle the normalised keys
        if s_lat is None:
            s_lat = address_obj.get("latitude")
            s_lon = address_obj.get("longitude")
        return {
            "store_name":    seller.get("name") or seller.get("tradeName"),
            "store_address": store_address,
            "store_city":    address_obj.get("city")  or address_obj.get("City"),
            "store_state":   address_obj.get("state") or address_obj.get("stateCode") or address_obj.get("State"),
            "latitude":      float(s_lat) if s_lat is not None else None,
            "longitude":     float(s_lon) if s_lon is not None else None,
            "store_payload": json.dumps(seller, ensure_ascii=False),
        }

    def resolve_store(self, zip_code: str) -> Optional[str]:
        self._resolved_store_metadata = None
        url = "https://www.atacadao.com.br/api/checkout/pub/regions"
        params = {"postalCode": zip_code, "country": "BRA"}
        try:
            response = self.session.get(url, params=params, timeout=10)
            print(f"Atacadao resolve_store: HTTP {response.status_code}")
            if response.status_code != 200:
                print(f"  body: {response.text[:200]}")
            if response.status_code == 200:
                data = response.json() or []

                # Collect all atacadaobr sellers across all region items
                candidates = []
                for item in data:
                    for seller in item.get("sellers", []):
                        seller_id = (seller or {}).get("id", "")
                        if seller_id.startswith("atacadaobr"):
                            candidates.append(seller)

                if not candidates:
                    print(f"  no atacadaobr seller found in {len(data)} region items")
                else:
                    # Log all candidates so store selection is visible
                    print(f"  {len(candidates)} atacadaobr candidate(s):")
                    for s in candidates:
                        print(f"    id={s.get('id')} name={s.get('name')} "
                              f"api_dist={s.get('distance') or s.get('distancia') or 'n/a'}")

                    # Check if the API provided distance for any candidate
                    no_api_dist = all(
                        (s.get("distance") or s.get("distancia") or s.get("distanceInKm")) is None
                        for s in candidates
                    )

                    # Geocode ZIP for haversine
                    zip_coords = self._geocode_zip(zip_code) if len(candidates) > 1 else None

                    # When API has no distance, fetch store coordinates in one batch call
                    seller_coord_map: Dict[str, Tuple[float, float]] = {}
                    if no_api_dist and len(candidates) > 1 and zip_coords is not None:
                        candidate_ids = [s.get("id", "") for s in candidates]
                        seller_coord_map = self._fetch_seller_coords_batch(candidate_ids)
                        print(f"  seller-list coords fetched for "
                              f"{len(seller_coord_map)}/{len(candidates)} stores")

                    best_seller = None
                    best_dist = float("inf")

                    for seller in candidates:
                        sid = seller.get("id", "")
                        # 1. API-provided distance field
                        api_dist = seller.get("distance") or seller.get("distancia") or seller.get("distanceInKm")
                        if api_dist is not None:
                            try:
                                dist = float(api_dist)
                            except (TypeError, ValueError):
                                dist = float("inf")
                        elif zip_coords and sid in seller_coord_map:
                            # 2. Haversine from batch-fetched seller coordinates
                            s_lat, s_lon = seller_coord_map[sid]
                            dist = self._haversine_km(zip_coords[0], zip_coords[1], s_lat, s_lon)
                        else:
                            # 3. Haversine from address obj (rarely populated by regions API)
                            address_obj = seller.get("address") or {}
                            s_lat, s_lon = self._extract_store_coords(address_obj)
                            if zip_coords and s_lat is not None and s_lon is not None:
                                dist = self._haversine_km(zip_coords[0], zip_coords[1], s_lat, s_lon)
                            else:
                                # 4. No distance info — preserve API order
                                dist = 0.0 if best_seller is None else float("inf")

                        if dist < best_dist:
                            best_dist = dist
                            best_seller = seller

                    if best_seller:
                        seller_id = best_seller.get("id", "")
                        address_obj = best_seller.get("address") or {}

                        # VTEX regions API usually returns no address — fetch separately
                        if not any(address_obj.get(k) for k in ("street", "Street", "city", "City")):
                            if zip_coords is None:
                                zip_coords = self._geocode_zip(zip_code)
                            fetched = self._fetch_seller_address(seller_id, zip_coords)
                            if fetched:
                                address_obj = fetched

                        self._resolved_store_metadata = self._build_store_metadata(best_seller, address_obj)
                        dist_str = f"{best_dist:.1f} km" if best_dist not in (0.0, float("inf")) else "unknown dist"
                        print(
                            f"Atacadao store resolved (nearest of {len(candidates)}): "
                            f"id={seller_id} "
                            f"name={self._resolved_store_metadata.get('store_name')} "
                            f"city={self._resolved_store_metadata.get('store_city')} "
                            f"dist={dist_str}"
                        )
                        return seller_id
        except Exception as exc:
            print(f"Atacadao resolve_store exception: {exc}")
        print(f"Atacadao resolve_store: falling back to DEFAULT_STORE={self.DEFAULT_STORE}")
        return self.DEFAULT_STORE

    # ----------------------------------------------------------- price helpers

    @staticmethod
    def _extract_price_tiers(
        seller_offers: List[Dict[str, Any]],
    ) -> tuple:
        """
        Return (regular_price_brl, promo_price_brl, promo_min_quantity).
        VTEX prices are in centavos — division by 100 is applied here.
        """
        if not seller_offers:
            return None, None, None

        sorted_offers = sorted(
            seller_offers,
            key=lambda o: (
                o.get("minQuantity") if o.get("minQuantity") is not None else 1,
                o.get("price") if o.get("price") is not None else float("inf"),
            ),
        )

        # Base price: cheapest single-unit offer
        base_offer = next(
            (o for o in sorted_offers if (o.get("minQuantity") or 1) <= 1),
            sorted_offers[0],
        )
        # Promo price: cheapest offer regardless of quantity
        promo_offer = min(
            sorted_offers,
            key=lambda o: o.get("price") if o.get("price") is not None else float("inf"),
        )

        # Use _centavos_to_brl which handles both centavos (≥1000) and BRL (<1000)
        regular_cents = base_offer.get("listPrice") or base_offer.get("price")
        promo_cents = promo_offer.get("price")
        regular_price = AtacadaoDepartamentosScraper._centavos_to_brl(regular_cents)
        promo_price = AtacadaoDepartamentosScraper._centavos_to_brl(promo_cents)
        promo_min_qty = promo_offer.get("minQuantity") or 1

        if (
            promo_min_qty <= 1
            or promo_price is None
            or regular_price is None
            or promo_price >= regular_price
        ):
            return regular_price, promo_price or regular_price, None
        return regular_price, promo_price, promo_min_qty

    # -------------------------------------------------- department slug discovery

    def discover_department_slugs(self) -> List[str]:
        """
        Discover department slugs from the Atacadão category sitemap.
        Returns top-level slugs only (no '/' in path).
        Falls back to subcategory parent slugs if top-level yields nothing.
        """
        try:
            response = self.session.get(self.CATEGORY_SITEMAP_URL, timeout=30)
            if response.status_code != 200 or not response.text:
                print(f"Atacadao sitemap: HTTP {response.status_code}")
                return []

            loc_urls = re.findall(r"<loc>(.*?)</loc>", response.text, flags=re.IGNORECASE)
            top_level: set = set()
            all_parents: set = set()

            for loc_url in loc_urls:
                path = urlparse(loc_url).path.strip("/")
                if not path or path in {"sitemap"}:
                    continue
                if "/" not in path:
                    top_level.add(path)
                else:
                    # Collect parent slugs from subcategory paths like "bebidas/cervejas"
                    all_parents.add(path.split("/")[0])

            slugs = sorted(top_level) if top_level else sorted(all_parents)
            if slugs:
                print(f"Atacadao departamentos: discovered {len(slugs)} slugs from sitemap.")
            return slugs
        except Exception as exc:
            print(f"Atacadao sitemap discovery error: {exc}")
            return []

    # ---------------------------------------------------- paginated product fetch

    def _fetch_department_products(
        self,
        *,
        slug: str,
        seller_id: str,
        max_pages: int,
        page_size: int,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        region_id = self._get_region_id(seller_id)
        channel_value = json.dumps(
            {"salesChannel": "1", "seller": seller_id, "regionId": region_id}
        )
        all_nodes: List[Dict[str, Any]] = []
        seen_ids: set = set()
        total_count: Optional[int] = None
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        for page in range(max_pages):
            offset = page * page_size
            variables = {
                "first": page_size,
                "after": str(offset),
                "sort": "score_desc",
                "term": "",
                "selectedFacets": [
                    {"key": "c", "value": slug},
                    {"key": "channel", "value": channel_value},
                    {"key": "locale", "value": "pt-BR"},
                ],
            }
            params = {
                "operationName": "ProductsQuery",
                "variables": json.dumps(variables),
            }

            try:
                response = None
                for attempt in range(3):
                    response = self.session.get(self.GRAPHQL_URL, params=params, timeout=25)
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
                    wait = 1.5 * (attempt + 1)
                    print(f"  slug={slug} page={page+1} HTTP {response.status_code}, retrying in {wait:.1f}s")
                    time.sleep(wait)

                if response is None or response.status_code != 200:
                    status = response.status_code if response is not None else "n/a"
                    print(f"Atacadao slug={slug} page={page+1}: HTTP {status}, stopping.")
                    break

                data = response.json() or {}

                # GraphQL errors block (API accepted the request but reported errors)
                errors = data.get("errors")
                if errors:
                    print(f"Atacadao slug={slug} page={page+1}: GraphQL errors={errors}, stopping.")
                    break

                products = (
                    ((data.get("data") or {}).get("search") or {}).get("products") or {}
                )
                edges = products.get("edges") or []
                if total_count is None:
                    total_count = (products.get("pageInfo") or {}).get("totalCount")

                if not edges:
                    break

                new_count = 0
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    node_id = node.get("id")
                    if not node_id or node_id in seen_ids:
                        continue
                    seen_ids.add(node_id)
                    all_nodes.append(node)
                    new_count += 1
                    if max_items is not None and len(all_nodes) >= max_items:
                        break

                print(
                    f"Atacadao slug={slug} page={page+1}: "
                    f"items={len(edges)} new={new_count} total={len(all_nodes)}"
                    + (f"/{total_count}" if total_count else "")
                )

                if new_count == 0 or len(edges) < page_size:
                    break
                if total_count is not None and offset + page_size >= total_count:
                    break
                if max_items is not None and len(all_nodes) >= max_items:
                    break

                # Polite inter-page delay to avoid triggering 429s
                time.sleep(0.2)

            except Exception as exc:
                print(f"Atacadao slug={slug} page={page+1} error: {exc}")
                break

        return all_nodes

    # ------------------------------------------------------------- standardize

    def _standardize_product(
        self, node: Dict[str, Any], seller_id: str
    ) -> Optional[Dict[str, Any]]:
        offers_data = node.get("offers") or {}

        # Filter offers to those belonging to the resolved seller
        seller_offers = [
            offer
            for offer in (offers_data.get("offers") or [])
            if isinstance((offer or {}).get("seller"), dict)
            and (offer["seller"]).get("identifier") == seller_id
        ]

        # If no offer for the exact seller, fall back to any available offer
        if not seller_offers:
            seller_offers = [
                offer for offer in (offers_data.get("offers") or [])
                if offer and offer.get("price") is not None
            ]

        if not seller_offers:
            return None

        regular_price, promo_price, promo_min_quantity = self._extract_price_tiers(seller_offers)

        # priceValidUntil lives on the individual offer — take it from the cheapest
        # (same selection logic as promo_offer in _extract_price_tiers)
        _cheapest = min(
            seller_offers,
            key=lambda o: o.get("price") if o.get("price") is not None else float("inf"),
        )
        promo_end_at = (
            _cheapest.get("priceValidUntil")
            or _cheapest.get("validUntil")
            or _cheapest.get("endDate")
        ) or None

        # Both prices None means the product has no usable pricing — skip it
        if regular_price is None and promo_price is None:
            # Last resort: use aggregate prices from offers object (also in centavos)
            high = self._centavos_to_brl(offers_data.get("highPrice"))
            low = self._centavos_to_brl(offers_data.get("lowPrice"))
            if high is None and low is None:
                return None
            regular_price = high or low
            promo_price = low or high

        # Prefer valid GTIN lengths only; GraphQL often returns internal RefId-like values in `gtin`.
        gtin_text = self._extract_valid_gtin(node.get("gtin") or node.get("ean"))
        if not gtin_text:
            for sku in (node.get("items") or []):
                if not isinstance(sku, dict):
                    continue
                gtin_text = self._extract_valid_gtin(sku.get("ean"))
                if gtin_text:
                    break

        # Web fallback: VTEX catalog endpoint exposes real items[].ean by productId.
        # Also trigger when gtin_text won't produce a valid barcode (e.g. 8-digit internal RefIds
        # from GraphQL that pass _extract_valid_gtin's length check but fail normalize_barcode).
        barcode = self.db.normalize_barcode(gtin_text) if gtin_text else None
        if not barcode:
            catalog_ean = self._fetch_catalog_ean_by_product_id(node.get("id"))
            if catalog_ean:
                gtin_text = catalog_ean
                barcode = self.db.normalize_barcode(gtin_text)

        image_url = None
        image_data = node.get("image") or []
        if isinstance(image_data, list) and image_data:
            first = image_data[0] or {}
            if isinstance(first, dict):
                image_url = first.get("url")

        brand_data = node.get("brand") or {}
        brand_name = brand_data.get("name") if isinstance(brand_data, dict) else None

        product_id = node.get("id")
        offer_id = self.db.build_offer_id("atacadao", seller_id, barcode, gtin_text, node.get("name"))
        if not offer_id:
            return None

        slug = node.get("slug") or ""
        product_url = f"https://www.atacadao.com.br/{slug}/p" if slug else None

        return {
            "id": offer_id,
            "product_name": node.get("name"),
            "brand": brand_name,
            "description": (node.get("description") or node.get("metaTagDescription") or "").strip() or None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": promo_min_quantity,
            "unit": node.get("measurementUnit"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": None,
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": None,
            "offer_tag": None,
            "app_membership_required": False,
            "promo_end_at": promo_end_at,
            "last_updated": datetime.now().isoformat(),
            "store_id": seller_id,
        }

    # --------------------------------------------------------- main entry point

    def fetch_offers(
        self,
        zip_code: str,
        department_slugs: Optional[Iterable[str]] = None,
        max_pages_per_department: int = 140,
        page_size: int = 100,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        print(f"Fetching Atacadao departamentos offers for {zip_code}...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        # ── resolve store — always call resolve_store so we get the nearest
        # store for THIS zip_code, not an arbitrary cached store ─────────────
        seller_id = self.resolve_store(zip_code)
        if seller_id:
            metadata = self._resolved_store_metadata or {}
            self.db.cache_store_id(
                zip_code,
                self.market_name,
                seller_id,
                store_name=metadata.get("store_name"),
                store_address=metadata.get("store_address"),
                store_city=metadata.get("store_city"),
                store_state=metadata.get("store_state"),
                latitude=metadata.get("latitude"),
                longitude=metadata.get("longitude"),
                store_payload=metadata.get("store_payload"),
            )
        if not seller_id:
            seller_id = self.DEFAULT_STORE

        print(f"Atacadao departamentos: seller_id={seller_id}")

        # ── department slugs ───────────────────────────────────────────────────
        if department_slugs is not None:
            slugs = sorted({(s or "").strip().strip("/") for s in department_slugs if s})
        else:
            slugs = self.discover_department_slugs()

        if not slugs:
            print("Atacadao: sitemap discovery failed, using DEFAULT_SLUGS")
            slugs = self.DEFAULT_SLUGS

        print(f"Atacadao departamentos: using {len(slugs)} slugs")
        for idx, slug in enumerate(slugs, start=1):
            print(f"  [{idx}] {slug}")

        # ── scrape each slug ───────────────────────────────────────────────────
        products_by_id: Dict[str, Dict[str, Any]] = {}

        for slug in slugs:
            if max_items is not None and len(products_by_id) >= max_items:
                break

            remaining = (max_items - len(products_by_id)) if max_items is not None else None
            nodes = self._fetch_department_products(
                slug=slug,
                seller_id=seller_id,
                max_pages=max_pages_per_department,
                page_size=page_size,
                limit=remaining,
            )
            self._prefetch_catalog_eans_for_nodes(nodes)
            for node in nodes:
                std = self._standardize_product(node, seller_id)
                if std is None:
                    continue
                products_by_id[std["id"]] = std
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            # Polite inter-department pause
            time.sleep(0.5)

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        lookup_budget_label = (
            str(self._catalog_ean_lookup_budget)
            if self._catalog_ean_lookup_budget is not None
            else "disabled"
        )
        print(
            "Atacadao catalog EAN fallback: "
            f"lookups={self._catalog_ean_lookup_count} hits={self._catalog_ean_lookup_hits} "
            f"budget={lookup_budget_label}"
        )
        print(f"Atacadao departamentos: {len(all_products)} products collected.")
        return all_products


if __name__ == "__main__":
    scraper = AtacadaoDepartamentosScraper()
    scraper.fetch_offers("07110-000")
