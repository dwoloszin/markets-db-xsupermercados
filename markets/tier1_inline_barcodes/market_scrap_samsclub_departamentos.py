"""
market_scrap_samsclub_departamentos.py — Sam's Club Brasil scraper.

Sam's Club Brasil runs on the VTEX platform (samsclub.myvtex.com).

Store network: ~12 physical stores in Brazil. Prices are UNIFORM across
all stores (confirmed via API testing). The regionId parameter only
controls shipping/fulfillment availability, not pricing. Therefore:
  - STORE_ID is kept as "samsclub.com.br" (stable, ZIP-independent offer IDs)
  - regionId is resolved from ZIP and passed to product searches so that
    availability (AvailableQuantity / IsAvailable) is accurate for the
    ZIP's nearest store

VTEX catalog_system limits:
  - Hard cap: _from cannot exceed 2500 (error returned beyond that)
  - Total catalogue: ~17,778 products across 23 root departments

Strategy:
  1. Resolve regionId from ZIP via /api/checkout/pub/regions/
  2. Fetch category tree → collect top-level department IDs
  3. For each department, paginate with fq=C:/{dept_id}/&regionId=...
     - If total ≤ 2499: fetch directly
     - If total > 2499: split into sub-categories using full paths
       (VTEX requires full ancestor path: C:/6/42/ not just C:/42/)
  4. Deduplicate by productId across all fetches

EAN is exposed in items[0].ean.
"""

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from db.db_manager import DatabaseManager


class SamsClubDepartamentosScraper:
    BASE_URL  = "https://www.samsclub.com.br"
    STORE_ID  = "samsclub.com.br"
    PAGE_SIZE = 50
    # VTEX hard limit: _from > 2500 returns error
    VTEX_MAX_FROM = 2450   # last safe _from (fetches positions 2450-2499)

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
            "Referer":         "https://www.samsclub.com.br/",
        })
        self.market_name = "Sam's Club"
        self._resolved_store_metadata: Optional[Dict] = None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Region / nearest-store resolution
    # ------------------------------------------------------------------

    def _resolve_region(self, zip_code: str) -> Optional[str]:
        """Return the regionId (base64) for the nearest Sam's Club store.

        Also populates self._resolved_store_metadata with the nearest store's
        address so it can be saved to store_mappings.
        Falls back to None if the ZIP has no nearby store — product search
        still works without regionId (uses national defaults).
        """
        self._resolved_store_metadata = None
        zip_clean = zip_code.replace("-", "").strip()
        zip_fmt   = f"{zip_clean[:5]}-{zip_clean[5:]}" if len(zip_clean) == 8 else zip_code
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/checkout/pub/regions/",
                params={"country": "BRA", "postalCode": zip_fmt},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    data = data[0]
                region_id = data.get("id") if isinstance(data, dict) else None
                sellers   = data.get("sellers", []) if isinstance(data, dict) else []
                if sellers:
                    names = ", ".join(s.get("name", s.get("id", "")) for s in sellers[:3])
                    print(f"Sam's Club: ZIP {zip_fmt} → {len(sellers)} store(s): {names}")
                    # Capture nearest store details for store_mappings
                    nearest = sellers[0]
                    addr = nearest.get("address") or {}
                    # VTEX geoCoordinates: [lon, lat]
                    geo = addr.get("geoCoordinates") or addr.get("geoCoords") or []
                    lat = addr.get("latitude") or addr.get("lat")
                    lon = addr.get("longitude") or addr.get("lng")
                    if lat is None and isinstance(geo, (list, tuple)) and len(geo) >= 2:
                        lon, lat = geo[0], geo[1]
                    try:
                        lat = float(lat) if lat is not None else None
                        lon = float(lon) if lon is not None else None
                    except (TypeError, ValueError):
                        lat = lon = None
                    self._resolved_store_metadata = {
                        "store_name": nearest.get("name") or nearest.get("tradeName"),
                        "store_address": ", ".join(
                            part for part in [
                                addr.get("street"),
                                addr.get("number"),
                                addr.get("complement"),
                                addr.get("neighborhood"),
                            ] if part
                        ) or None,
                        "store_city": addr.get("city"),
                        "store_state": addr.get("state") or addr.get("stateCode"),
                        "latitude": lat,
                        "longitude": lon,
                        "store_payload": json.dumps(nearest, ensure_ascii=False),
                    }
                    print(
                        f"Sam's Club: nearest store = {self._resolved_store_metadata.get('store_name')} "
                        f"({self._resolved_store_metadata.get('store_city')})"
                    )
                return region_id or None
        except Exception as exc:
            print(f"Sam's Club: region lookup error: {exc}")
        return None

    # ------------------------------------------------------------------
    # Category tree → list of (fq_path, label, total) tuples
    # ------------------------------------------------------------------

    def _fetch_tree(self) -> List[Dict]:
        """Fetch the VTEX category tree (3 levels deep)."""
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/category/tree/3",
                params={"sc": 1},
                timeout=20,
            )
            if r.status_code == 200:
                return r.json() or []
        except Exception as exc:
            print(f"Sam's Club: category tree error: {exc}")
        return []

    def _get_category_total(self, fq: str, region_id: Optional[str] = None) -> int:
        """Return the total product count for a given fq filter (one HEAD request)."""
        params: Dict = {"fq": fq, "_from": 0, "_to": 0, "sc": 1}
        if region_id:
            params["regionId"] = region_id
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/catalog_system/pub/products/search",
                params=params,
                timeout=15,
            )
            resources = r.headers.get("resources", "")
            if resources:
                return int(resources.split("/")[-1])
        except Exception:
            pass
        return 0

    def _build_segments(self, tree: List[Dict], region_id: Optional[str] = None) -> List[Tuple[str, str]]:
        """
        Convert the category tree into a list of (fq_filter, label) segments
        that are each guaranteed to have ≤ VTEX_MAX_FROM + PAGE_SIZE products.

        Top-level departments (direct children of tree root) are tried first.
        Any department with >2499 products is split into its sub-departments
        using VTEX's required full-path format: C:/dept_id/sub_id/
        """
        segments: List[Tuple[str, str]] = []

        for dept in tree:
            dept_id   = dept.get("id") or dept.get("Id")
            dept_name = dept.get("name") or dept.get("Name") or str(dept_id)
            if not dept_id:
                continue

            fq    = f"C:/{dept_id}/"
            total = self._get_category_total(fq, region_id)

            if total <= self.VTEX_MAX_FROM + self.PAGE_SIZE:
                segments.append((fq, dept_name))
                print(f"  Sam's Club dept [{dept_id}] {dept_name}: {total} products → single segment")
            else:
                # Split into sub-categories (children)
                print(
                    f"  Sam's Club dept [{dept_id}] {dept_name}: {total} products → splitting into sub-depts"
                )
                children = dept.get("children") or []
                if not children:
                    # No children in tree — use the dept as-is (best effort)
                    segments.append((fq, dept_name))
                else:
                    for sub in children:
                        sub_id   = sub.get("id") or sub.get("Id")
                        sub_name = sub.get("name") or sub.get("Name") or str(sub_id)
                        if not sub_id:
                            continue
                        # Full path required by VTEX: parent/child
                        sub_fq    = f"C:/{dept_id}/{sub_id}/"
                        sub_total = self._get_category_total(sub_fq, region_id)
                        segments.append((sub_fq, f"{dept_name} / {sub_name}"))
                        print(
                            f"    Sam's Club sub [{dept_id}/{sub_id}] {sub_name}: {sub_total} products"
                        )

        return segments

    # ------------------------------------------------------------------
    # Paginate one segment
    # ------------------------------------------------------------------

    def _fetch_segment(
        self, fq: str, label: str, max_items: Optional[int],
        region_id: Optional[str] = None,
    ) -> Dict[str, Dict]:
        """Fetch all products for one fq segment. Returns {productId: item}."""
        seen: Dict[str, Dict] = {}
        page = 0

        while True:
            from_idx = page * self.PAGE_SIZE
            to_idx   = from_idx + self.PAGE_SIZE - 1

            if from_idx > self.VTEX_MAX_FROM:
                # Should not happen if segments are sized correctly
                print(f"  Sam's Club {label}: hit VTEX _from limit, stopping")
                break

            params: Dict = {"fq": fq, "_from": from_idx, "_to": to_idx, "sc": 1}
            if region_id:
                params["regionId"] = region_id

            try:
                r = self.session.get(
                    f"{self.BASE_URL}/api/catalog_system/pub/products/search",
                    params=params,
                    timeout=20,
                )
            except Exception as exc:
                print(f"  Sam's Club {label} error (page {page}): {exc}")
                break

            if r.status_code not in (200, 206):
                print(f"  Sam's Club {label} HTTP {r.status_code} on page {page}")
                break

            try:
                items = r.json()
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break

            new_count = 0
            for item in items:
                pid = str(item.get("productId") or item.get("id") or "")
                if pid and pid not in seen:
                    seen[pid] = item
                    new_count += 1

            total = None
            resources = r.headers.get("resources", "")
            if resources:
                try:
                    total = int(resources.split("/")[-1])
                except ValueError:
                    pass

            page += 1
            if len(items) < self.PAGE_SIZE or new_count == 0:
                break
            if total is not None and from_idx + len(items) >= total:
                break
            if max_items and len(seen) >= max_items:
                break

            time.sleep(0.12)

        return seen

    # ------------------------------------------------------------------
    # Promotion helpers
    # ------------------------------------------------------------------

    _MEMBERSHIP_KEYWORDS = {"sócio", "socio", "plus", "clube", "club", "member", "mark"}

    @classmethod
    def _extract_teasers(cls, co: Dict) -> List[Dict]:
        """Return a normalised list of promotion teaser dicts from commertialOffer.

        VTEX exposes promotions in two parallel fields:
          - PromotionTeasers  → clean format  {Name, Conditions.MinimumQuantity, ...}
          - Teasers           → backing-field format  {"<Name>k__BackingField": ..., ...}
        We prefer PromotionTeasers; fall back to parsing Teasers when it is absent/empty.
        """
        promos = co.get("PromotionTeasers") or []
        if promos and isinstance(promos, list):
            return [p for p in promos if isinstance(p, dict)]

        raw = co.get("Teasers") or []
        normalised = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            name = (
                t.get("Name")
                or t.get("<Name>k__BackingField")
                or ""
            )
            cond = t.get("Conditions") or t.get("<Conditions>k__BackingField") or {}
            min_qty = (
                cond.get("MinimumQuantity")
                or cond.get("<MinimumQuantity>k__BackingField")
            )
            normalised.append({"Name": name, "Conditions": {"MinimumQuantity": min_qty}})
        return normalised

    @classmethod
    def _parse_promos(cls, co: Dict, product_clusters: Dict) -> Dict:
        """Extract offer_name, promo_min_quantity, offer_tag, app_membership_required,
        and promo_end_at from a commertialOffer + product-level cluster dict."""
        teasers = cls._extract_teasers(co)

        offer_name: Optional[str] = None
        promo_min_quantity: Optional[int] = None
        app_membership_required = False

        if teasers:
            # Use first teaser as the primary offer name
            first = teasers[0]
            raw_name = (first.get("Name") or "").strip()
            if raw_name:
                offer_name = raw_name
            cond = first.get("Conditions") or {}
            min_qty = cond.get("MinimumQuantity")
            if min_qty and int(min_qty) > 1:
                promo_min_quantity = int(min_qty)

            # Check all teaser names for membership keywords
            all_names = " ".join(
                (t.get("Name") or "").lower() for t in teasers
            )
            if any(kw in all_names for kw in cls._MEMBERSHIP_KEYWORDS):
                app_membership_required = True

        # product clusters → offer_tag (e.g. "Melhores Promoções", "Leve mais por menos")
        # productClusters is a dict: {"45488": "2.2 - Melhores Promoções", ...}
        cluster_names: List[str] = []
        for cname in (product_clusters or {}).values():
            cname_str = str(cname).strip()
            if cname_str:
                cluster_names.append(cname_str)
                # Also check cluster names for membership markers
                if any(kw in cname_str.lower() for kw in cls._MEMBERSHIP_KEYWORDS):
                    app_membership_required = True
        offer_tag = " | ".join(cluster_names) if cluster_names else None

        # promo_end_at from PriceValidUntil (skip values >2 years out — those are cache TTLs)
        promo_end_at: Optional[str] = None
        pvu = co.get("PriceValidUntil")
        if pvu and isinstance(pvu, str):
            try:
                from datetime import timezone
                exp = datetime.fromisoformat(pvu.replace("Z", "+00:00"))
                now_utc = datetime.now(timezone.utc)
                if (exp - now_utc).days <= 730:   # only store if ≤ 2 years away
                    promo_end_at = exp.isoformat()
            except Exception:
                pass

        return {
            "offer_name": offer_name,
            "promo_min_quantity": promo_min_quantity,
            "offer_tag": offer_tag,
            "app_membership_required": app_membership_required,
            "promo_end_at": promo_end_at,
        }

    # ------------------------------------------------------------------
    # Standardize VTEX product → offer dict
    # ------------------------------------------------------------------

    def _standardize(self, item: Dict) -> Optional[Dict]:
        name = (
            item.get("productName") or item.get("name") or item.get("title") or ""
        ).strip()
        if not name:
            return None

        raw_barcode   = None
        regular_price = None
        promo_price   = None
        image_url     = None
        product_url   = None
        co: Dict      = {}

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
                list_price = self._to_float(co.get("ListPrice"))
                sale_price = self._to_float(co.get("Price"))
                if list_price and sale_price:
                    regular_price = list_price
                    promo_price   = sale_price if sale_price < list_price else None
                else:
                    regular_price = list_price or sale_price
            images = sku.get("images") or []
            if images:
                image_url = images[0].get("imageUrl")

        if not raw_barcode:
            raw_barcode = item.get("ean") or item.get("gtin") or item.get("barcode")
        if regular_price is None:
            regular_price = self._to_float(item.get("price") or item.get("preco"))

        link = item.get("link") or item.get("linkText") or ""
        if link:
            product_url = link if link.startswith("http") else f"{self.BASE_URL}/{link.lstrip('/')}/p"

        brand = item.get("brand") or item.get("marca")
        if isinstance(brand, dict):
            brand = brand.get("name") or brand.get("nome")

        product_clusters = item.get("productClusters") or {}
        promo_info = self._parse_promos(co, product_clusters)

        # If a teaser has a promo but Price == ListPrice, the discount is conditional
        # (e.g. "40% na 2ª unidade" — unit price doesn't change until qty threshold).
        # Keep promo_price=None in that case; the offer_name already documents the deal.

        gtin_text = str(raw_barcode).strip() if raw_barcode else None
        barcode   = self.db.normalize_barcode(gtin_text) if gtin_text else None
        offer_id  = self.db.build_offer_id("samsclub", self.STORE_ID, barcode, gtin_text, name)
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": name,
            "brand": str(brand).strip() if brand else None,
            "description": item.get("description") or item.get("metaTagDescription"),
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": promo_info["promo_min_quantity"],
            "unit": item.get("unitMultiplier"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": str(image_url) if image_url else None,
            "stock_balance": None,
            "stock_general": None,
            "sold_quantity": None,
            "offer_name": promo_info["offer_name"],
            "offer_tag": promo_info["offer_tag"],
            "app_membership_required": promo_info["app_membership_required"],
            "promo_end_at": promo_info["promo_end_at"],
            "last_updated": datetime.now().isoformat(),
            "store_id": self.STORE_ID,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict]:
        print("Fetching Sam's Club departamentos offers...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        tree = self._fetch_tree()
        if not tree:
            print("Sam's Club: category tree empty, aborting.")
            return []

        region_id = self._resolve_region(zip_code)

        # Save nearest store address to store_mappings
        metadata = self._resolved_store_metadata or {}
        self.db.cache_store_id(
            zip_code,
            self.market_name,
            self.STORE_ID,
            store_name=metadata.get("store_name"),
            store_address=metadata.get("store_address"),
            store_city=metadata.get("store_city"),
            store_state=metadata.get("store_state"),
            latitude=metadata.get("latitude"),
            longitude=metadata.get("longitude"),
            store_payload=metadata.get("store_payload"),
        )

        print(f"Sam's Club: building segments from {len(tree)} top-level departments...")
        segments = self._build_segments(tree, region_id)
        print(f"Sam's Club: {len(segments)} fetch segments ready")

        all_products: Dict[str, Dict] = {}

        for fq, label in segments:
            if max_items and len(all_products) >= max_items:
                break

            remaining = (max_items - len(all_products)) if max_items else None
            batch = self._fetch_segment(fq, label, remaining, region_id)

            new = 0
            for pid, item in batch.items():
                if pid not in all_products:
                    offer = self._standardize(item)
                    if not offer:
                        continue
                    all_products[pid] = offer
                    new += 1
                    if max_items and len(all_products) >= max_items:
                        break

            print(f"Sam's Club [{label}]: {len(batch)} raw ({new} new), total={len(all_products)}")
            time.sleep(0.2)

        result = list(all_products.values())
        if max_items:
            result = result[:max_items]
        print(f"Sam's Club: {len(result)} unique products collected.")
        return result


if __name__ == "__main__":
    scraper = SamsClubDepartamentosScraper()
    offers = scraper.fetch_offers("08032-230", limit=100)
    print(f"\nTotal: {len(offers)} offers")
    for o in offers[:3]:
        print(json.dumps(o, ensure_ascii=False, indent=2))
