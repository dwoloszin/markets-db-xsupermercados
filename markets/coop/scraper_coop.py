"""
scraper_coop.py - Coop Supermercados, ABC paulista (https://www.coopsupermercado.com.br)

Platform : VTEX IO (account `coopsp`), classic catalog API + checkout simulation.
Store    : PRICES ARE PER STORE (measured 2026-09-22: the same oil costs 6.79-7.79 depending on
           the seller, ~15% spread). /api/checkout/pub/regions returns the SAME 11 white-label
           sellers (coopsp<xx>) for every CEP, so the store is chosen as the nearest pickup
           point (/api/checkout/pub/pickup-points gives seller id + coordinates) -> store_id is
           the seller id (e.g. "coopspid" = Coop Industrial, Santo Andre).
Listing  : catalog_system products/search per category segment (31 segments, ~7.2k products):
           product name, ean, image, url, sku id. The catalog price is NOT the selling price.
Prices   : POST /api/checkout/pub/orderForms/simulation with up to 50 {id, seller} items ->
           listPrice / sellingPrice / availability for that seller (1.3 s per call, ~145 calls).
Barcode  : inline items[0].ean (~87% valid GTIN; produce carries short PLU codes).
Runtime  : ~5 min per store.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from markets.common.geo import format_zip, haversine_km, zip_coords
from markets.common.gtin import first_gtin
from markets.common.http import get_json, make_session, post_json
from markets.common.offer import make_offer
from markets.common.vtex import VtexCatalog

STORE_KEY = "coop"
BASE_URL = "https://www.coopsupermercado.com.br"
DEFAULT_SELLER = "coopspid"  # Coop Industrial, Santo Andre
SIM_BATCH = 50
PICKUP_CENTER = "-46.53;-23.66"  # Santo Andre; the API lists every pickup point from here


def _pickup_sellers(session) -> Dict[str, Dict[str, Any]]:
    """seller id -> {name, city, zip, lat, lon, address} from the pickup-point list."""
    data = get_json(session, f"{BASE_URL}/api/checkout/pub/pickup-points",
                    params={"geoCoordinates": PICKUP_CENTER, "countryCode": "BRA", "pageSize": 100},
                    log_prefix="[coop] ") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for it in data.get("items") or []:
        pp = (it or {}).get("pickupPoint") or {}
        pid = str(pp.get("id") or "")
        seller = pid.split("_")[1] if pid.startswith("1_") else pid.split("_")[0]
        if not seller.startswith("coopsp"):
            continue
        addr = pp.get("address") or {}
        geo = addr.get("geoCoordinates") or [None, None]
        parts = [addr.get("street"), addr.get("number"), addr.get("neighborhood")]
        out.setdefault(seller, {"name": pp.get("friendlyName"), "city": addr.get("city"), "state": addr.get("state"),
                                "zip": addr.get("postalCode"), "lon": geo[0], "lat": geo[1],
                                "address": ", ".join(str(p) for p in parts if p) or None})
    return out


def _region_sellers(session, zip_code: str) -> List[str]:
    data = get_json(session, f"{BASE_URL}/api/checkout/pub/regions",
                    params={"postalCode": format_zip(zip_code), "country": "BRA"}, log_prefix="[coop] ") or []
    return [str(s.get("id")) for r in data for s in (r or {}).get("sellers") or []
            if str(s.get("id", "")).startswith("coopsp")]


def resolve_store(session, db, zip_code: str) -> str:
    sellers = _region_sellers(session, zip_code)
    pickups = _pickup_sellers(session)
    # only sellers that actually price a basket (pickup-only points answer an empty simulation)
    candidates = [s for s in sellers if s in pickups] or list(pickups)
    seller, info = DEFAULT_SELLER, pickups.get(DEFAULT_SELLER, {})
    coords = zip_coords(zip_code, session)
    if coords and candidates:
        def dist(s: str) -> float:
            p = pickups.get(s) or {}
            if p.get("lat") is None or p.get("lon") is None:
                return 1e9
            return haversine_km(coords[0], coords[1], p["lat"], p["lon"])
        seller = min(candidates, key=dist)
        info = pickups.get(seller, {})
        print(f"[coop] nearest store {seller} {info.get('name')} ({info.get('city')}) {dist(seller):.1f} km, "
              f"{len(candidates)} candidates")
    else:
        print(f"[coop] could not geocode {zip_code}; using {seller}")
    db.save_store_info(seller, query_zip=format_zip(zip_code), name=f"Coop {info.get('name') or seller}",
                       address=info.get("address"), city=info.get("city"), state=info.get("state") or "SP",
                       store_zip=info.get("zip"), latitude=info.get("lat"), longitude=info.get("lon"), payload=info)
    return seller


def _simulate(session, seller: str, skus: List[str], zip_code: str) -> Dict[str, Dict[str, Any]]:
    body = {"items": [{"id": s, "quantity": 1, "seller": seller} for s in skus],
            "postalCode": format_zip(zip_code), "country": "BRA"}
    data = post_json(session, f"{BASE_URL}/api/checkout/pub/orderForms/simulation?sc=1",
                     json_body=body, log_prefix="[coop] ") or {}
    return {str(i.get("id")): i for i in data.get("items") or [] if isinstance(i, dict)}


def _offer(product: Dict[str, Any], sim: Dict[str, Any], store_id: str, label: str) -> Optional[Dict[str, Any]]:
    item = (product.get("items") or [{}])[0]
    listp, sellp = sim.get("listPrice"), sim.get("sellingPrice")
    if not sellp:
        return None
    # Weighed items: listPrice is per kg while sellingPrice already includes the
    # unitMultiplier (e.g. 0.065 kg of garlic) - compare like with like or every
    # weighed product looks like a 90% promo.
    try:
        mult = float(sim.get("unitMultiplier") or 1) or 1.0
    except (TypeError, ValueError):
        mult = 1.0
    list_unit = (listp or 0) * mult
    regular = (list_unit if listp else sellp) / 100.0
    promo = sellp / 100.0 if listp and sellp < list_unit - 1 else None
    avail = sim.get("availability")
    images = item.get("images") or []
    link_text = product.get("linkText")
    return make_offer(
        product_id=product.get("productId"), store_id=store_id, product_name=product.get("productName"),
        regular_price=regular, promo_price=promo,
        barcode=first_gtin(item.get("ean"), allow_gtin8=False), barcode_source="inline",
        brand=product.get("brand"), category_path=label, unit=sim.get("measurementUnit"),
        is_available=(avail != "withoutStock") if avail else None,
        product_url=product.get("link") or (f"{BASE_URL}/{link_text}/p" if link_text else None),
        image_url=images[0].get("imageUrl") if images and isinstance(images[0], dict) else None,
    )


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/", "Origin": BASE_URL})
    seller = resolve_store(session, db, zip_code)
    if not db.begin_store(seller):
        print(f"[coop] store {seller} already scraped in this run (another CEP maps to it) - skipping")
        return {"upserted": 0, "skipped": 0, "with_barcode": 0, "duplicate_store": True}
    catalog = VtexCatalog(session, BASE_URL, delay=0.1, log_prefix="[coop] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[coop] {len(segments)} segments, prices via simulation for seller {seller}")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for fq, label in segments:
        products: List[Dict[str, Any]] = []
        for p in catalog.iter_products(fq, label=label):
            pid = str(p.get("productId") or "")
            if pid and pid not in seen and (p.get("items") or [{}])[0].get("itemId"):
                seen.add(pid)
                products.append(p)
            if limit and len(seen) >= limit:
                break
        batch: List[Dict[str, Any]] = []
        for i in range(0, len(products), SIM_BATCH):
            chunk = products[i:i + SIM_BATCH]
            sims = _simulate(session, seller, [str(p["items"][0]["itemId"]) for p in chunk], zip_code)
            for p in chunk:
                sim = sims.get(str(p["items"][0]["itemId"]))
                offer = _offer(p, sim, seller, label) if sim else None
                if offer:
                    batch.append(offer)
            time.sleep(0.1)
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[coop] {str(label)[:40]:<40} {len(products):>4} listed, +{len(batch):>4} priced (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
