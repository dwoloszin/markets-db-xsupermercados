"""
scraper_nagumo.py - Nagumo (https://www.nagumo.com.br)

Platform : Salesforce Commerce Cloud (Demandware, SFRA).
Store    : Stores-FindStores?lat=&long=&radius=100 (JSON) -> nearest branch "M_<ID>" (store_info). The catalogue itself is shared, so
           the category pages are fetched WITHOUT the pmid store filter (full range).
Listing  : GET /categoria/<slug>/?sz=<total>&start=0  -> the whole category in one page;
           products are embedded as JSON in <search-card-grid products="...">.
           Fallback: Search-UpdateGrid?cgid=<slug>&sz=120&start=N (JSON, productsSearchResult).
Barcode  : none in the listing since 2026 (the old `upc` field is gone), none on the PDP either.
           The rest is filled from the legacy table and by tools/crossfill_barcodes.py
           (exact name match against the other markets).
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from markets.common.geo import format_zip, haversine_km, to_float, zip_coords
from markets.common.gtin import first_gtin
from markets.common.http import make_session, request_with_retry
from markets.common.offer import make_offer, parse_brl

STORE_KEY = "nagumo"
BASE_URL = "https://www.nagumo.com.br"
DEFAULT_STORE = "M_13"
DEFAULT_CATEGORIES = ["a%C3%A7ougue", "departamentos/hortifruti", "departamentos/padaria", "mercearia-salgada",
                      "mercearia-doce", "higiene-e-perfumaria", "departamentos/limpeza",
                      "departamentos/laticinios-e-frios", "departamentos/congelados", "bebidas", "pet", "bebe"]
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Referer": BASE_URL + "/"}
_TOTAL_PATTERNS = [r'<search-card-grid[^>]*?\btotal=["\'](\d+)["\']', r'"totalCount"\s*:\s*(\d+)',
                   r'"total"\s*:\s*(\d+)', r'(\d[\d.,]+)\s+Produtos encontrados', r'(\d[\d.]*)\s+produto']


def resolve_store(session, db, zip_code: str) -> str:
    """
    Stores-FindStores?lat=&long=&radius=100 returns the branches (JSON) with
    coordinates; StoreLocator-GetNearestStores?postalCode= no longer answers
    with a list. Nearest branch by haversine from the ZIP coordinates.
    """
    coords = zip_coords(zip_code, session)
    stores: List[Dict[str, Any]] = []
    if coords:
        url = f"{BASE_URL}/on/demandware.store/Sites-Nagumo-Site/pt_BR/Stores-FindStores"
        r = request_with_retry(session, "GET", url, params={"lat": coords[0], "long": coords[1], "radius": 100},
                               headers={"Accept": "application/json"}, timeout=25, max_attempts=2)
        if r is not None and r.status_code == 200:
            try:
                data = json.loads(r.content.decode("utf-8", errors="replace"))
                stores = [s for s in (data.get("stores") or []) if isinstance(s, dict) and s.get("ID")]
            except ValueError:
                stores = []
    if not stores or not coords:
        db.save_store_info(DEFAULT_STORE, query_zip=format_zip(zip_code), name="Nagumo (default store)")
        print(f"[nagumo] store locator unavailable - using {DEFAULT_STORE}")
        return DEFAULT_STORE

    def dist(s):
        lat, lon = to_float(s.get("latitude")), to_float(s.get("longitude"))
        return haversine_km(coords[0], coords[1], lat, lon) if lat is not None and lon is not None else 1e9

    best = min(stores, key=dist)
    store_id = f"M_{best['ID']}"
    db.save_store_info(
        store_id, query_zip=format_zip(zip_code), name=best.get("name"),
        address=", ".join(str(p) for p in [best.get("address1"), best.get("address2")] if p) or None,
        city=best.get("city"), state=best.get("stateCode"), store_zip=best.get("postalCode"),
        latitude=best.get("latitude"), longitude=best.get("longitude"),
        payload={k: v for k, v in best.items() if not isinstance(v, (dict, list))},
    )
    print(f"[nagumo] store {store_id} {best.get('name')} ({best.get('city')}) {dist(best):.1f} km, of {len(stores)}")
    return store_id


def discover_categories(session) -> List[str]:
    found: List[str] = []
    r = request_with_retry(session, "GET", BASE_URL + "/", timeout=25, max_attempts=2)
    if r is not None and r.status_code == 200:
        for m in re.finditer(r'href="(?:https?://www\.nagumo\.com\.br)?/categoria/([^"?#]+?)/?"', r.text):
            slug = m.group(1).strip("/")
            if slug and slug not in found:
                found.append(slug)
    for d in DEFAULT_CATEGORIES:
        if d not in found:
            found.append(d)
    return found


def _extract_products(page_html: str) -> List[Dict[str, Any]]:
    m = re.search(r'<search-card-grid[^>]*\sproducts="([^"]+)"', page_html, flags=re.I)
    if not m:
        return []
    try:
        data = json.loads(html_module.unescape(m.group(1)))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _total(page_html: str) -> Optional[int]:
    for pat in _TOTAL_PATTERNS:
        m = re.search(pat, page_html, flags=re.I)
        if m:
            try:
                v = int(m.group(1).replace(".", "").replace(",", ""))
                if v > 0:
                    return v
            except ValueError:
                continue
    return None


def _offer(p: Dict[str, Any], store_id: str, category: str) -> Optional[Dict[str, Any]]:
    price = p.get("price") or {}
    sales = parse_brl((price.get("sales") or {}).get("value"))
    lst = parse_brl((price.get("list") or {}).get("value"))
    regular, promo = (lst, sales) if lst else (sales, None)
    # Promotions live in flagtypes[]: {"flagType": "NGM_26_M", "valueFlag": 7.59, "valueFlagType": "R$ 7,59"}
    # valueFlag is the promotional unit price; a flagType ending in "_M" is a "Meu Nagumo"
    # (loyalty app) price. price.list is always null on this site.
    flags = [f for f in (p.get("flagtypes") or []) if isinstance(f, dict)]
    flag_prices = [(parse_brl(f.get("valueFlag")) or parse_brl(f.get("valueFlagType")), f) for f in flags]
    flag_prices = [(v, f) for v, f in flag_prices if v and regular and v < regular]
    membership = False
    offer_tag = None
    if promo is None and flag_prices:
        promo, flag = min(flag_prices, key=lambda x: x[0])
        ftype = str(flag.get("flagType") or "")
        membership = ftype.upper().endswith("_M") or "MEU NAGUMO" in str(flag.get("valueFlagType") or "").upper()
        offer_tag = ftype or None
    promo_disc = p.get("promotionDiscount") if isinstance(p.get("promotionDiscount"), dict) else {}
    offer_name = promo_disc.get("name") or promo_disc.get("label") or ("Meu Nagumo" if membership else None)
    images = (p.get("images") or {}).get("medium") or []
    image_url = images[0].get("absURL") if images and isinstance(images[0], dict) else None
    stock = p.get("ATSInCurrentStore") if p.get("ATSInCurrentStore") is not None else p.get("ATSInGenerealStock")
    return make_offer(
        product_id=p.get("id"), store_id=store_id, product_name=p.get("productName"),
        regular_price=regular, promo_price=promo,
        barcode=first_gtin(p.get("upc"), p.get("ean"), allow_gtin8=False), barcode_source="inline",
        brand=p.get("brand"), category_path=category,
        offer_name=offer_name, offer_tag=promo_disc.get("type") or promo_disc.get("id") or offer_tag,
        app_membership_required=membership if (flags or promo_disc) else None,
        unit=p.get("productMeasureValue"), stock=stock,
        is_available=p.get("available") if isinstance(p.get("available"), bool) else None,
        product_url=p.get("productShowFullUrl"), image_url=image_url,
    )


def _fetch_category(session, slug: str, limit: Optional[int]) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/categoria/{slug}/"
    r = request_with_retry(session, "GET", url, params={"sz": 1, "start": 0, "srule": "Relevance"}, timeout=40, max_attempts=3)
    if r is None or r.status_code != 200:
        return []
    total = _total(r.text)
    products: List[Dict[str, Any]] = []
    if total:
        sz = min(total, limit) if limit else total
        r = request_with_retry(session, "GET", url, params={"sz": sz, "start": 0, "srule": "Relevance"}, timeout=120, max_attempts=2)
        if r is not None and r.status_code == 200:
            products = _extract_products(r.text)
    if not products:  # JSON grid fallback
        grid = f"{BASE_URL}/on/demandware.store/Sites-Nagumo-Site/pt_BR/Search-UpdateGrid"
        start = 0
        while True:
            r = request_with_retry(session, "GET", grid, params={"cgid": slug.split("/")[-1], "srule": "Relevance",
                                                                  "start": start, "sz": 120}, timeout=60, max_attempts=2,
                                   headers={"Accept": "application/json"})
            if r is None or r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                break
            try:
                rows = (r.json() or {}).get("productsSearchResult") or []
            except ValueError:
                break
            if not rows:
                break
            products.extend(rows)
            if len(rows) < 120 or (limit and len(products) >= limit):
                break
            start += len(rows)
    return products


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session(HTML_HEADERS, json_accept=False)
    store_id = resolve_store(session, db, zip_code)
    cats = discover_categories(session)
    print(f"[nagumo] {len(cats)} categories (catalogue fetched without store filter)")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for slug in cats:
        if slug.strip("/") == "departamentos":
            continue
        label = urlparse("/" + slug).path.strip("/").split("/")[-1].replace("-", " ")
        products = _fetch_category(session, slug, limit)
        batch = []
        for p in products:
            if not isinstance(p, dict):
                continue
            offer = _offer(p, store_id, label)
            if offer and offer["product_id"] not in seen:
                seen.add(offer["product_id"])
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[nagumo] {label[:35]:<35} +{len(batch):>5} of {len(products)} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
        time.sleep(0.3)
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
