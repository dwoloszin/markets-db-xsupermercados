"""
scraper_marche.py - St. Marche (https://marche.com.br)

Platform : Shopify. The public front-end (marche.com.br) is Shopify Hydrogen/Oxygen and does
           NOT expose products.json; the Shopify store domain behind it does:
             https://checkout.marche.com.br/products.json?limit=250&page=N   (61 pages, ~15.2k)
           The bulk feed has price / compare_at_price / available but its variants carry NO
           `barcode` key.
Barcode  : GET https://checkout.marche.com.br/products/<handle>.json -> variants[0].barcode,
           a ';'-separated list of codes (supplier codes, UPC-12, one or more EAN-13). Fetched
           once per product by the enrichment step (barcodes are cached, only new products are
           looked up). 95% of products have a valid GTIN, 77% a valid EAN-13 (2026-09-22).
Store    : "Selecione uma loja" on the site only changes fulfilment; Shopify has one price per
           variant -> national prices, store_id "marche".
Runtime  : listing ~1 min; first enrichment ~30 min at 6 threads (15k lookups), then seconds.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from markets.common.enrich import run_enrichment
from markets.common.geo import format_zip
from markets.common.gtin import first_gtin
from markets.common.http import get_json, make_session
from markets.common.offer import make_offer

STORE_KEY = "marche"
STORE_ID = "marche"
WEB_BASE = "https://marche.com.br"
SHOP_BASE = "https://checkout.marche.com.br"
HEADERS = {"Referer": WEB_BASE + "/", "Origin": WEB_BASE}
PAGE_LIMIT = 250


def pick_gtin(raw: Any) -> Optional[str]:
    """';'-separated list -> a valid EAN-13 if there is one, else the first valid GTIN."""
    cands = [c.strip() for c in str(raw or "").split(";") if c.strip()]
    for c in cands:
        if len(c) == 13 and first_gtin(c, allow_gtin8=False):
            return c
    return first_gtin(*cands, allow_gtin8=False) if cands else None


def _offer(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    v = (p.get("variants") or [{}])[0]
    price, compare = v.get("price"), v.get("compare_at_price")
    regular, promo = price, None
    try:
        if compare and float(compare) > float(price or 0):
            regular, promo = compare, price
    except (TypeError, ValueError):
        pass
    images = p.get("images") or []
    image = images[0].get("src") if images and isinstance(images[0], dict) else None
    handle = p.get("handle") or ""
    tags = [t for t in (p.get("tags") or []) if t and "algolia" not in t.lower()]
    return make_offer(
        product_id=p.get("id"), store_id=STORE_ID, product_name=p.get("title"),
        regular_price=regular, promo_price=promo,
        barcode=pick_gtin(v.get("barcode")), barcode_source="inline" if v.get("barcode") else None,
        brand=p.get("vendor") if str(p.get("vendor") or "").lower() != "st. marche" else None,
        category_path=p.get("product_type") or (tags[0] if tags else None),
        is_available=bool(v.get("available")),
        product_url=f"{WEB_BASE}/products/{handle}" if handle else None, image_url=image,
    )


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session(HEADERS)
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="St. Marche (online)", city="São Paulo", state="SP")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for page in range(1, 400):
        data = get_json(session, f"{SHOP_BASE}/products.json", params={"limit": PAGE_LIMIT, "page": page},
                        log_prefix="[marche] ")
        products = (data or {}).get("products") or []
        if not products:
            break
        batch = []
        for p in products:
            offer = _offer(p)
            if offer and offer["product_id"] not in seen:
                seen.add(offer["product_id"])
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[marche] page {page:>3} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
        time.sleep(0.1)
    return total


def _fetch_barcode(session: requests.Session, store_id: str, product_id: str, url: str) -> Tuple[Optional[str], Optional[int]]:
    handle = url.rstrip("/").rsplit("/", 1)[-1]
    if not handle:
        return None, None
    for attempt in range(2):
        try:
            r = session.get(f"{SHOP_BASE}/products/{handle}.json", timeout=25)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None, r.status_code
        try:
            variants = (r.json().get("product") or {}).get("variants") or []
        except ValueError:
            return None, 200
        return (pick_gtin(variants[0].get("barcode")) if variants else None), 200
    return None, 429


def enrich(db, workers: int = 6, limit: Optional[int] = None) -> Dict[str, int]:
    import config
    return run_enrichment(db, fetch=_fetch_barcode, source="pdp-json", workers=min(workers, 6), limit=limit,
                          retry_not_found_days=config.BARCODE_RETRY_NOT_FOUND_DAYS,
                          headers=HEADERS, json_accept=True, delay=0.05, label="[marche] ")


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
