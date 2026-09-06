"""
scraper_atacadao.py - Atacadao (https://www.atacadao.com.br)

Platform : VTEX FastStore (GraphQL storefront) on top of a classic VTEX catalog.
Store    : GET /api/checkout/pub/regions?postalCode=CEP&country=BRA -> sellers
           "atacadaobr##"; nearest one by coordinates (seller list) -> seller_id.
           regionId = base64("SW#" + seller_id) makes GraphQL return that store's prices.
Listing  : GET /api/graphql?operationName=ProductsQuery&variables={first:100, after:offset,
           selectedFacets:[{c:<slug>},{channel:{salesChannel,seller,regionId}},{locale}]}
           (100/page; slugs come from /sitemap/category-0.xml). Store prices + bulk tiers
           (offers[].minQuantity) are here. `gtin` in GraphQL is an internal ref id - IGNORED.
Barcode  : the classic catalog API still works at /io/api/catalog_system/pub/products/search
           and returns items[].ean. We batch 50 productIds per call (repeat fq=productId:X),
           only for products whose barcode is still NULL -> after the first run this is
           almost free. (Old approach did one call per product: 1h40 per run.)
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from markets.common.geo import format_zip, haversine_km, to_float, zip_coords
from markets.common.gtin import first_gtin
from markets.common.http import get_json, make_session
from markets.common.offer import make_offer, parse_brl
from markets.common.vtex import VtexCatalog

STORE_KEY = "atacadao"
BASE_URL = "https://www.atacadao.com.br"
GRAPHQL_URL = f"{BASE_URL}/api/graphql"
DEFAULT_SELLER = "atacadaobr60"
DEFAULT_SLUGS = ["bebidas", "mercearia", "limpeza", "higiene-e-beleza", "laticinios-e-frios", "congelados",
                 "carnes", "hortifruti", "padaria", "pet-shop", "bazar", "eletronicos"]
PAGE_SIZE = 100


def _region_id(seller: str) -> str:
    return base64.b64encode(f"SW#{seller}".encode()).decode()


def _seller_coords(session) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    data = get_json(session, f"{BASE_URL}/io/api/catalog_system/pub/seller/list", log_prefix="[atacadao] ")
    for s in data or []:
        sid = str(s.get("SellerId") or s.get("sellerId") or s.get("id") or "")
        addr = s.get("Address") or s.get("address") or {}
        geo = addr.get("GeoCoordinates") or addr.get("geoCoordinates") or []
        lat = addr.get("Latitude") or addr.get("latitude") or (geo[1] if len(geo) >= 2 else None)
        lon = addr.get("Longitude") or addr.get("longitude") or (geo[0] if len(geo) >= 2 else None)
        if sid and lat is not None and lon is not None:
            try:
                out[sid] = (float(lat), float(lon))
            except (TypeError, ValueError):
                pass
    return out


def _seller_address(session, seller_id: str) -> Dict[str, Any]:
    data = get_json(session, f"{BASE_URL}/io/api/catalog_system/pub/seller/details/{seller_id}",
                    max_attempts=2, log_prefix="[atacadao] ") or {}
    addr = data.get("Address") or data.get("address") or {}
    geo = addr.get("GeoCoordinates") or addr.get("geoCoordinates") or []
    return {
        "street": addr.get("Street") or addr.get("street"), "number": addr.get("Number") or addr.get("number"),
        "neighborhood": addr.get("Neighborhood") or addr.get("neighborhood"),
        "city": addr.get("City") or addr.get("city"), "state": addr.get("State") or addr.get("state"),
        "zip": addr.get("PostalCode") or addr.get("postalCode"),
        "lat": geo[1] if len(geo) >= 2 else addr.get("latitude"),
        "lon": geo[0] if len(geo) >= 2 else addr.get("longitude"),
        "name": data.get("Name") or data.get("name"),
    }


def resolve_store(session, db, zip_code: str) -> str:
    data = get_json(session, f"{BASE_URL}/api/checkout/pub/regions",
                    params={"postalCode": format_zip(zip_code), "country": "BRA"}, log_prefix="[atacadao] ")
    candidates: List[Dict[str, Any]] = []
    for item in data or []:
        for seller in (item or {}).get("sellers") or []:
            if str((seller or {}).get("id", "")).startswith("atacadaobr"):
                candidates.append(seller)
    if not candidates:
        print(f"[atacadao] no seller for CEP {zip_code}; using {DEFAULT_SELLER}")
        db.save_store_info(DEFAULT_SELLER, query_zip=format_zip(zip_code), name="Atacadao (default)")
        return DEFAULT_SELLER
    best = candidates[0]
    if len(candidates) > 1:
        coords = zip_coords(zip_code, session)
        seller_xy = _seller_coords(session)
        if coords:
            def dist(s):
                xy = seller_xy.get(str(s.get("id")))
                return haversine_km(coords[0], coords[1], xy[0], xy[1]) if xy else 1e9
            best = min(candidates, key=dist)
    seller_id = str(best["id"])
    addr = _seller_address(session, seller_id)
    db.save_store_info(
        seller_id, query_zip=format_zip(zip_code), name=best.get("name") or addr.get("name"),
        address=", ".join(str(p) for p in [addr.get("street"), addr.get("number"), addr.get("neighborhood")] if p) or None,
        city=addr.get("city"), state=addr.get("state"), store_zip=addr.get("zip"),
        latitude=addr.get("lat"), longitude=addr.get("lon"), payload=best,
    )
    print(f"[atacadao] store {seller_id} {best.get('name')} ({addr.get('city')}) of {len(candidates)} candidates")
    return seller_id


def discover_slugs(session) -> List[str]:
    try:
        r = session.get(f"{BASE_URL}/sitemap/category-0.xml", timeout=30)
        if r.status_code != 200:
            return DEFAULT_SLUGS
        top, parents = set(), set()
        for loc in re.findall(r"<loc>(.*?)</loc>", r.text, flags=re.I):
            path = urlparse(loc).path.strip("/")
            if not path or path == "sitemap":
                continue
            (top if "/" not in path else parents).add(path.split("/")[0])
        slugs = sorted(top) or sorted(parents)
        return slugs or DEFAULT_SLUGS
    except Exception:
        return DEFAULT_SLUGS


def _price_tiers(offers: List[Dict[str, Any]]) -> Tuple[Any, Any, Any]:
    """(regular, promo, promo_min_qty) from FastStore offers (BRL floats)."""
    valid = [o for o in offers if isinstance(o, dict) and o.get("price") is not None]
    if not valid:
        return None, None, None
    singles = [o for o in valid if (o.get("minQuantity") or 1) <= 1] or valid
    base = min(singles, key=lambda o: o["price"])
    cheapest = min(valid, key=lambda o: o["price"])
    regular = base.get("listPrice") or base.get("price")
    promo = cheapest.get("price")
    min_q = cheapest.get("minQuantity") or 1
    if parse_brl(promo) is not None and parse_brl(regular) is not None and promo < regular:
        return regular, promo, (min_q if min_q > 1 else None)
    if parse_brl(base.get("price")) is not None and parse_brl(regular) is not None and base["price"] < regular:
        return regular, base["price"], None
    return regular, None, None


def _offer(node: Dict[str, Any], seller_id: str) -> Optional[Dict[str, Any]]:
    offers_data = node.get("offers") or {}
    all_offers = offers_data.get("offers") or []
    mine = [o for o in all_offers if isinstance(o, dict) and isinstance(o.get("seller"), dict)
            and o["seller"].get("identifier") == seller_id] or [o for o in all_offers if isinstance(o, dict)]
    regular, promo, min_q = _price_tiers(mine)
    if regular is None:
        regular = offers_data.get("highPrice") or offers_data.get("lowPrice")
        promo = offers_data.get("lowPrice") if regular else None
    variant = node.get("isVariantOf") or {}
    product_id = str(variant.get("productGroupID") or node.get("id") or "").strip()
    crumbs = [c.get("name") for c in ((node.get("breadcrumbList") or {}).get("itemListElement") or [])[:-1]
              if isinstance(c, dict) and c.get("name")]
    image = (node.get("image") or [{}])[0]
    image_url = image.get("url") if isinstance(image, dict) else None
    slug = node.get("slug") or ""
    brand = node.get("brand") or {}
    barcode = first_gtin(node.get("gtin"), allow_gtin8=False)  # 12-14 digit only; 7/8-digit ref ids are dropped
    return make_offer(
        product_id=product_id, store_id=seller_id, product_name=node.get("name"),
        regular_price=regular, promo_price=promo, promo_min_quantity=min_q,
        barcode=barcode, barcode_source="inline",
        brand=brand.get("name") if isinstance(brand, dict) else brand,
        category_path="/".join(crumbs) if crumbs else None,
        unit=node.get("measurementUnit"),
        product_url=f"{BASE_URL}/{slug}/p" if slug else None, image_url=image_url,
        is_available=bool(mine) if all_offers else None,
    )


def _query(session, slug: str, channel: str, offset: int, first: int) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    """One GraphQL page -> (nodes, totalCount, had_error)."""
    variables = {"first": first, "after": str(offset), "sort": "score_desc", "term": "",
                 "selectedFacets": [{"key": "c", "value": slug}, {"key": "channel", "value": channel},
                                    {"key": "locale", "value": "pt-BR"}]}
    data = get_json(session, GRAPHQL_URL, params={"operationName": "ProductsQuery",
                                                   "variables": json.dumps(variables)}, log_prefix="[atacadao] ")
    if not data:
        return [], None, True
    products = (((data.get("data") or {}).get("search") or {}).get("products") or {})
    total = (products.get("pageInfo") or {}).get("totalCount")
    nodes = [(e or {}).get("node") or {} for e in (products.get("edges") or [])]
    return nodes, total, bool(data.get("errors"))


def _fetch_slug(session, slug: str, seller_id: str, seen: set, limit: Optional[int]) -> List[Dict[str, Any]]:
    """
    Page through one category. A single product with a broken offer makes the
    whole GraphQL page fail ("Cannot read properties of undefined (reading 'price')"),
    so an erroring page is re-fetched in slices of 10 and only the broken slice
    is skipped - never the rest of the category.
    """
    channel = json.dumps({"salesChannel": "1", "seller": seller_id, "regionId": _region_id(seller_id)})
    out: List[Dict[str, Any]] = []
    total_count = None
    offset = 0
    while offset < 20000:
        nodes, total, err = _query(session, slug, channel, offset, PAGE_SIZE)
        if err and not nodes:
            nodes = []
            for sub in range(0, PAGE_SIZE, 10):
                sub_nodes, sub_total, sub_err = _query(session, slug, channel, offset + sub, 10)
                if sub_err and not sub_nodes:
                    print(f"[atacadao] {slug} offset {offset + sub}: broken slice skipped")
                    continue
                nodes.extend(sub_nodes)
                total = total or sub_total
                time.sleep(0.05)
        if total_count is None and total is not None:
            total_count = total
        new = 0
        for node in nodes:
            offer = _offer(node, seller_id)
            if offer and offer["product_id"] not in seen:
                seen.add(offer["product_id"])
                out.append(offer)
                new += 1
            if limit and len(seen) >= limit:
                return out
        offset += PAGE_SIZE
        if total_count is not None and offset >= total_count:
            break
        if not nodes and total_count is None:
            break
        time.sleep(0.15)
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    seller_id = resolve_store(session, db, zip_code)
    slugs = discover_slugs(session)
    print(f"[atacadao] seller={seller_id} slugs={len(slugs)}")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for slug in slugs:
        batch = _fetch_slug(session, slug, seller_id, seen, limit)
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[atacadao] {slug:<30} +{len(batch):>5} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
        time.sleep(0.3)
    return total


def enrich(db, workers: int = 12, limit: Optional[int] = None) -> Dict[str, int]:
    """Backfill barcodes from the classic VTEX catalog API, 50 productIds per call."""
    import config
    missing = db.load_missing_barcodes(retry_not_found_days=config.BARCODE_RETRY_NOT_FOUND_DAYS, need_url=False)
    if limit:
        missing = missing[:limit]
    print(f"[atacadao] products needing barcode: {len(missing):,}")
    if not missing:
        return {"fetched": 0, "found": 0, "updated": 0}
    session = make_session({"Referer": BASE_URL + "/"})
    catalog = VtexCatalog(session, BASE_URL, api_prefix="/io/api", delay=0.1, log_prefix="[atacadao] ")
    by_pid: Dict[str, List[Tuple[str, str]]] = {}
    for store_id, product_id, _url, _name in missing:
        by_pid.setdefault(product_id, []).append((store_id, product_id))
    pids = list(by_pid)
    found_rows, state_rows = [], []
    found = updated = 0
    for i in range(0, len(pids), 50):
        chunk = pids[i:i + 50]
        products = catalog.lookup_by_product_ids(chunk)
        for pid in chunk:
            p = products.get(pid)
            barcode = None
            if p:
                for item in p.get("items") or []:
                    barcode = first_gtin(item.get("ean"), allow_gtin8=True)
                    if barcode:
                        break
            for store_id, product_id in by_pid[pid]:
                if barcode:
                    found += 1
                    found_rows.append((store_id, product_id, barcode, "catalog"))
                    state_rows.append((store_id, product_id, "found", 200))
                else:
                    state_rows.append((store_id, product_id, "not_found", 200 if p else 404))
        if len(found_rows) >= 300 or len(state_rows) >= 600:
            updated += db.update_barcodes(found_rows)
            db.record_enrich_state(state_rows)
            found_rows.clear()
            state_rows.clear()
        done = min(i + 50, len(pids))
        if done % 500 < 50 or done == len(pids):
            print(f"[atacadao]   progress {done}/{len(pids)} found={found}")
    if found_rows or state_rows:
        updated += db.update_barcodes(found_rows)
        db.record_enrich_state(state_rows)
    print(f"[atacadao] barcodes found: {found:,}/{len(pids):,} written: {updated:,}")
    return {"fetched": len(pids), "found": found, "updated": updated}


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
