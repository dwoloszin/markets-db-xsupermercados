"""
scraper_samsclub.py - Sam's Club Brasil (https://www.samsclub.com.br)

Platform : VTEX (samsclub.myvtex.com)
Region   : GET /api/checkout/pub/regions/?country=BRA&postalCode=CEP -> regionId + nearest sellers.
           Prices are uniform across the ~12 stores; regionId only affects availability,
           so store_id stays "samsclub.com.br" and the nearest store goes to store_info.
Listing  : category tree -> products/search?fq=C:/dept/&sc=1&regionId=... (50/page, 2500 cap,
           departments above the cap are split into children with the full path).
Barcode  : inline items[0].ean (~99%).
"""

from __future__ import annotations

import json
from typing import Dict, Optional

from markets.common.geo import format_zip
from markets.common.http import get_json, make_session
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "samsclub"
BASE_URL = "https://www.samsclub.com.br"
STORE_ID = "samsclub.com.br"


def _resolve_region(session, db, zip_code: str) -> Optional[str]:
    data = get_json(session, f"{BASE_URL}/api/checkout/pub/regions/",
                    params={"country": "BRA", "postalCode": format_zip(zip_code)}, log_prefix="[samsclub] ")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Sam's Club (online)")
        return None
    region_id = data.get("id")
    sellers = data.get("sellers") or []
    nearest = sellers[0] if sellers else {}
    addr = nearest.get("address") or {}
    geo = addr.get("geoCoordinates") or []
    lat = addr.get("latitude") or (geo[1] if len(geo) >= 2 else None)
    lon = addr.get("longitude") or (geo[0] if len(geo) >= 2 else None)
    db.save_store_info(
        STORE_ID, query_zip=format_zip(zip_code),
        name=nearest.get("name") or "Sam's Club (online)",
        address=", ".join(p for p in [addr.get("street"), addr.get("number"), addr.get("neighborhood")] if p) or None,
        city=addr.get("city"), state=addr.get("state"), store_zip=addr.get("postalCode"),
        latitude=lat, longitude=lon, payload=json.dumps(nearest, ensure_ascii=False)[:5000] if nearest else None,
    )
    print(f"[samsclub] regionId={region_id} nearest={nearest.get('name')} ({addr.get('city')})")
    return region_id


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    region_id = _resolve_region(session, db, zip_code)
    extra = {"sc": 1}
    if region_id:
        extra["regionId"] = region_id
    catalog = VtexCatalog(session, BASE_URL, delay=0.12, extra_params=extra, log_prefix="[samsclub] ")
    segments = catalog.segments(depth=3)
    print(f"[samsclub] {len(segments)} segments")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for fq, label in segments:
        batch = []
        for product in catalog.iter_products(fq, label=label):
            pid = str(product.get("productId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            offer = vtex_offer(product, store_id=STORE_ID, base_url=BASE_URL, category_label=label, allow_gtin8=True)
            if offer:
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[samsclub] {label[:45]:<45} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
