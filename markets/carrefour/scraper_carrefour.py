"""
scraper_carrefour.py - Carrefour Mercado (https://mercado.carrefour.com.br)

Platform : VTEX FastStore behind a React-Router (Remix-style) storefront. The GraphQL
           and catalog APIs answer 403/503 (captcha) to non-browser clients, but the
           server-rendered category HTML is open.
Store    : POST /action/cep {CEP} -> city; POST /action/stores-from-pickups {city} -> stores;
           nearest by CEP; POST /action/set-regionalization -> cookies -> store prices.
Listing  : GET /categoria/<path>?page=N  (15 cards/page). Each card:
             <a href="/produto/<slug>-<id>" data-testid="search-product-card">
               <img src=".../ids/<n>-200-auto/<GTIN>_...png" alt="name">
               <h2>name</h2> <span>R$ 9,99</span> [<div>-15%</div> <span>R$ 8,49</span>]
           first price = regular, second = promo.
Barcode  : the image file name starts with the GTIN for ~all products (inline);
           the few misses are enriched from the PDP JSON-LD ("gtin").
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from markets.common.enrich import fetch_page_gtin, run_enrichment
from markets.common.geo import format_zip, normalize_zip
from markets.common.gtin import gtin_from_text
from markets.common.http import ThreadSessions, looks_like_challenge, make_session, request_with_retry
from markets.common.offer import make_offer, parse_brl, slugify

STORE_KEY = "carrefour"
BASE_URL = "https://mercado.carrefour.com.br"
DEFAULT_STORE_ID = "carrefour-mercado"
DEFAULT_CATEGORIES = ["bebidas", "mercearia", "limpeza", "hortifruti", "carnes-aves-e-peixes",
                      "frios-e-laticinios", "padaria", "higiene-e-beleza", "bebe", "pet"]
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Referer": BASE_URL + "/"}

_CARD_RE = re.compile(r'<a[^>]*href="(/produto/[^"]+)"[^>]*data-testid="search-product-card"[^>]*>(.*?)</a>', re.S)
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*alt="([^"]*)"', re.S)
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_PRICE_RE = re.compile(r"R\$\s*([\d.]+,\d{2})")
_TAG_RE = re.compile(r'style="background-color[^"]*"><span>([^<]{1,40})</span>')


def _post_action(session, path: str, form: Dict[str, Any]) -> Dict[str, Any]:
    r = request_with_retry(session, "POST", f"{BASE_URL}{path}", data=form, timeout=25, max_attempts=3)
    if r is None or r.status_code != 200:
        return {}
    try:
        return (r.json() or {}).get("result") or {}
    except ValueError:
        return {}


def regionalize(session, db, zip_code: str) -> str:
    """Select the nearest pickup store for the ZIP so the HTML shows its prices."""
    zdigits = normalize_zip(zip_code)
    ctx = _post_action(session, "/action/cep", {"CEP": format_zip(zdigits)})
    address = ctx.get("address") or {}
    city = address.get("city") or ""
    stores = (_post_action(session, "/action/stores-from-pickups", {"city": city}) or {}).get("stores") or []
    stores = [s for s in stores if isinstance(s, dict)]
    if not stores:
        db.save_store_info(DEFAULT_STORE_ID, query_zip=format_zip(zdigits), name="Carrefour Mercado (default)",
                           city=city or None, state=address.get("state"))
        print("[carrefour] no pickup store for this CEP - default regionalisation")
        return DEFAULT_STORE_ID

    def gap(s):
        sz = normalize_zip(s.get("cep_clique_retire") or s.get("postal_code"))
        return abs(int(sz) - int(zdigits)) if len(sz) == 8 and len(zdigits) == 8 else 10 ** 9

    store = min(stores, key=gap)
    target_zip = normalize_zip(store.get("cep_clique_retire") or store.get("postal_code") or zdigits)
    _post_action(session, "/action/set-regionalization", {
        "CEP": target_zip, "name": store.get("name") or "", "city": store.get("city") or "",
        "state": store.get("state") or "", "postal_code": target_zip, "store": json.dumps(store, ensure_ascii=False),
    })
    store_id = ":".join(p for p in ["carrefour", slugify(store.get("state") or address.get("state")),
                                    slugify(store.get("city") or city), slugify(store.get("name")), target_zip] if p)
    db.save_store_info(
        store_id, query_zip=format_zip(zdigits), name=store.get("name"),
        address=", ".join(str(p) for p in [store.get("street"), store.get("number"), store.get("neighborhood")] if p) or None,
        city=store.get("city") or city, state=store.get("state") or address.get("state"), store_zip=format_zip(target_zip),
        latitude=store.get("latitude") or store.get("lat"), longitude=store.get("longitude") or store.get("lng"),
        payload=store,
    )
    print(f"[carrefour] store {store.get('name')} ({store.get('city')}) zip={target_zip}")
    return store_id


def discover_categories(session) -> List[str]:
    """
    Leaf category paths from the sitemap. /sitemap.xml lists category-N.xml files
    whose <loc>s are the category paths WITHOUT the /categoria/ prefix
    (e.g. https://mercado.carrefour.com.br/bebidas/cervejas). A listing page is
    capped at 50 pages (750 products), so we must use the deepest paths (~385).
    """
    r = request_with_retry(session, "GET", f"{BASE_URL}/sitemap.xml", timeout=25, max_attempts=2)
    sitemaps = [loc for loc in re.findall(r"<loc>(.*?)</loc>", r.text, flags=re.I)
                if "/sitemap/category-" in loc] if r is not None and r.status_code == 200 else []
    sitemaps = sitemaps or [f"{BASE_URL}/sitemap/category-1.xml", f"{BASE_URL}/sitemap/category-0.xml"]
    paths: List[str] = []
    for url in sitemaps:
        r = request_with_retry(session, "GET", url, timeout=25, max_attempts=2)
        if r is None or r.status_code != 200:
            continue
        for loc in re.findall(r"<loc>(.*?)</loc>", r.text, flags=re.I):
            path = urlparse(loc).path.strip("/")
            if path.startswith("categoria/"):
                path = path[len("categoria/"):]
            if path and path not in ("sitemap",) and "." not in path.split("/")[-1]:
                paths.append(path)
    paths = sorted(set(paths))
    if not paths:
        return DEFAULT_CATEGORIES
    prefixes = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
    leaves = [p for p in paths if p not in prefixes]
    return leaves


def parse_cards(page_html: str, store_id: str, category: str) -> List[Dict[str, Any]]:
    out = []
    for href, body in _CARD_RE.findall(page_html):
        body = html_module.unescape(body)
        m_id = re.search(r"-(\d+)$", href.strip("/"))
        pid = m_id.group(1) if m_id else href.strip("/").split("/")[-1]
        img = _IMG_RE.search(body)
        image_url = img.group(1) if img else None
        h2 = _H2_RE.search(body)
        name = re.sub(r"<[^>]+>", " ", h2.group(1)).strip() if h2 else (img.group(2) if img else "")
        prices = [parse_brl(p) for p in _PRICE_RE.findall(body)]
        prices = [p for p in prices if p]
        if not prices:
            continue
        regular = prices[0]
        promo = prices[1] if len(prices) > 1 else None
        if promo is not None and promo > regular:
            regular, promo = promo, regular
        tag = _TAG_RE.search(body)
        image_name = image_url.rsplit("/", 1)[-1].split("?")[0] if image_url else ""
        barcode = gtin_from_text(image_name.split("_")[0].split(".")[0], allow_gtin8=True) if image_name else None
        offer = make_offer(
            product_id=pid, store_id=store_id, product_name=name, regular_price=regular, promo_price=promo,
            barcode=barcode, barcode_source="image", category_path=category.replace("/", " / "),
            offer_tag=tag.group(1) if tag else None,
            product_url=f"{BASE_URL}{href}", image_url=image_url.replace("width=200&height=200", "width=500&height=500") if image_url else None,
        )
        if offer:
            out.append(offer)
    return out


PAGE_CAP = 50  # the site never serves ?page=51 -> use leaf categories to stay under 750/category


def _fetch_category(pool: ThreadSessions, category: str, store_id: str, cookies: Dict[str, str],
                    max_pages: int = PAGE_CAP) -> List[Dict[str, Any]]:
    session = pool.get()
    for k, v in cookies.items():
        session.cookies.set(k, v)
    out: List[Dict[str, Any]] = []
    seen_local: set = set()
    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/categoria/{category}" + (f"?page={page}" if page > 1 else "")
        r = request_with_retry(session, "GET", url, timeout=40, max_attempts=4, log_prefix="[carrefour] ")
        if r is None or r.status_code != 200:
            break
        r.encoding = "utf-8"  # the page is UTF-8 but sends no charset -> requests would guess latin-1 (mojibake)
        if looks_like_challenge(r.text):
            print(f"[carrefour] captcha page for {category} p{page} - pausing 30s")
            time.sleep(30)
            continue
        cards = parse_cards(r.text, store_id, category)
        new = [c for c in cards if c["product_id"] not in seen_local]
        for c in new:
            seen_local.add(c["product_id"])
        out.extend(new)
        if not cards or not new or len(cards) < 15:
            break
        if page == max_pages:
            print(f"[carrefour] WARNING: {category} hit the {max_pages}-page cap - some products are not listed")
        time.sleep(0.1)
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session(HTML_HEADERS, json_accept=False)
    store_id = regionalize(session, db, zip_code)
    cookies = {c.name: c.value for c in session.cookies}
    categories = discover_categories(session)
    print(f"[carrefour] {len(categories)} categories, store_id={store_id}")
    pool = ThreadSessions(headers=HTML_HEADERS, json_accept=False)
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch_category, pool, c, store_id, cookies): c for c in categories}
        for fut in as_completed(futures):
            cat = futures[fut]
            done += 1
            try:
                cards = fut.result()
            except Exception as exc:
                print(f"[carrefour] {cat}: {exc}")
                continue
            batch = []
            for c in cards:
                if c["product_id"] in seen:
                    continue
                seen.add(c["product_id"])
                batch.append(c)
                if limit and len(seen) >= limit:
                    break
            if batch:
                r = db.save(batch)
                for k in total:
                    total[k] += r[k]
            print(f"[carrefour] [{done}/{len(categories)}] {cat[:45]:<45} +{len(batch):>4} (total {len(seen)})")
            if limit and len(seen) >= limit:
                for f in futures:
                    f.cancel()
                break
    return total


def enrich(db, workers: int = 8, limit: Optional[int] = None) -> Dict[str, int]:
    import config

    def fetch(session, store_id, product_id, url):
        return fetch_page_gtin(session, url, allow_gtin8=True)

    return run_enrichment(db, fetch=fetch, source="pdp", workers=min(workers, 8), limit=limit,
                          retry_not_found_days=config.BARCODE_RETRY_NOT_FOUND_DAYS,
                          headers=HTML_HEADERS, json_accept=False, delay=0.05, label="[carrefour] ")


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
