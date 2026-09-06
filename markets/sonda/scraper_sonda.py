"""
scraper_sonda.py - Sonda Delivery (https://www.sondadelivery.com.br)

Platform : ASP.NET WebForms, server-rendered HTML (no JSON API).
Listing  : /delivery -> department links (/delivery/categoria/<slug>, "Ver Todos");
           each category page lists ~15 products, next page via #ctl00_conteudo_linkPaginaProxima.
           Product data is inside ViewItemAnalytics(price,'sku','name') onclick handlers.
           The price shown ("Por") is the price you pay; discounted items only carry a
           <div class="product--discount">17% OFF</div> badge - the site never prints the
           old price (not even on the PDP), so regular_price = por / (1 - pct) (derived).
Store    : single delivery store (store_id "sondadelivery").
Barcode  : image CDN path /sku/<sku>/<size>/<EAN>.png exposes the EAN for ~41% of the
           catalogue inline; the rest is enriched from the PDP JSON-LD ("gtin") once.
"""

from __future__ import annotations

import html as html_module
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from markets.common.enrich import fetch_page_gtin, run_enrichment
from markets.common.geo import format_zip
from markets.common.gtin import first_gtin
from markets.common.http import make_session, request_with_retry
from markets.common.offer import make_offer

STORE_KEY = "sonda"
BASE_URL = "https://www.sondadelivery.com.br"
STORE_ID = "sondadelivery"
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Referer": BASE_URL + "/delivery"}

_ANALYTICS_RE = re.compile(r"ViewItemAnalytics\(([0-9.]+),\s*'([^']+)',\s*'([^']+)'\)")
_EAN_PATH_RE = re.compile(r"/sku/(\d+)/\d+/([0-9]{7,14})[.]")
_URL_RE = re.compile(r'href="/delivery/produto/([^/"]+)/(\d+)"')
_DISCOUNT_RE = re.compile(r'class="product--discount"[^>]*>\s*(\d{1,2})%\s*OFF', re.I)
_NEXT_RE = re.compile(r'id="ctl00_conteudo_linkPaginaProxima"[^>]*href="([^"]+)"')


def _get(session, url: str) -> Optional[str]:
    r = request_with_retry(session, "GET", url, timeout=30, max_attempts=4, log_prefix="[sonda] ")
    return r.text if r is not None and r.status_code == 200 else None


def discover_categories(session) -> List[Tuple[str, str]]:
    page = _get(session, f"{BASE_URL}/delivery") or ""
    slugs = re.findall(r'href="/delivery/categoria/([^"]+)"[^>]*>\s*[Vv]er [Tt]odos\s*</a>', page)
    if not slugs:
        slugs = re.findall(r'href="/delivery/categoria/([^"/]+)"', page)
    out, seen = [], set()
    for slug in slugs:
        if slug in seen:
            continue
        seen.add(slug)
        name = unquote(slug)
        name = name[:-2] if name.endswith("-l") else name
        out.append((slug, name.replace("_", " ").replace("-", " ")))
    return out


def parse_products(page_html: str, category: str) -> List[Dict[str, Any]]:
    decoded = html_module.unescape(page_html)
    ean_map: Dict[str, str] = {}
    for sku, ean in _EAN_PATH_RE.findall(decoded):
        ean_map.setdefault(sku, ean)
    url_map = {sku: f"{BASE_URL}/delivery/produto/{slug}/{sku}" for slug, sku in _URL_RE.findall(decoded)}
    # discount badge -> the nearest preceding ViewItemAnalytics(...) gives the sku
    discount_map: Dict[str, int] = {}
    analytics_positions = [(m.start(), m.group(2)) for m in _ANALYTICS_RE.finditer(decoded)]
    for m in _DISCOUNT_RE.finditer(decoded):
        prev = [sku for pos, sku in analytics_positions if pos < m.start()]
        if prev:
            discount_map[prev[-1]] = int(m.group(1))
    out, seen = [], set()
    for price_str, sku, name in _ANALYTICS_RE.findall(decoded):
        if sku in seen:
            continue
        seen.add(sku)
        ean = ean_map.get(sku)
        paid = float(price_str)
        pct = discount_map.get(sku)
        regular = round(paid / (1 - pct / 100.0), 2) if pct and 0 < pct < 100 else paid
        offer = make_offer(
            product_id=sku, store_id=STORE_ID, product_name=name,
            regular_price=regular, promo_price=paid if pct else None,
            offer_tag=f"{pct}% OFF" if pct else None,
            barcode=first_gtin(ean, allow_gtin8=True), barcode_source="image",
            category_path=category, product_url=url_map.get(sku),
            image_url=f"{BASE_URL}/sku/{sku}/530/{ean}.png" if ean else None,
        )
        if offer:
            out.append(offer)
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session(HTML_HEADERS, json_accept=False)
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Sonda Delivery (online)")
    cats = discover_categories(session)
    print(f"[sonda] {len(cats)} departments")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for slug, name in cats:
        url: Optional[str] = f"{BASE_URL}/delivery/categoria/{slug}"
        page = 1
        added = 0
        while url and page <= 300:
            page_html = _get(session, url)
            if not page_html:
                break
            batch = []
            for offer in parse_products(page_html, name):
                if offer["product_id"] in seen:
                    continue
                seen.add(offer["product_id"])
                batch.append(offer)
                if limit and len(seen) >= limit:
                    break
            if batch:
                r = db.save(batch)
                for k in total:
                    total[k] += r[k]
                added += len(batch)
            if not batch or (limit and len(seen) >= limit):
                break
            m = _NEXT_RE.search(html_module.unescape(page_html))
            url = BASE_URL + m.group(1) if m else None
            if page % 25 == 0:
                print(f"[sonda] {name[:35]:<35} page {page} (total {len(seen)})")
            page += 1
            time.sleep(0.2)
        print(f"[sonda] {name[:40]:<40} +{added:>5} pages={page} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


def enrich(db, workers: int = 6, limit: Optional[int] = None) -> Dict[str, int]:
    import config

    def fetch(session, store_id, product_id, url):
        return fetch_page_gtin(session, url, allow_gtin8=True)

    return run_enrichment(db, fetch=fetch, source="pdp", workers=min(workers, 6), limit=limit,
                          retry_not_found_days=config.BARCODE_RETRY_NOT_FOUND_DAYS,
                          headers=HTML_HEADERS, json_accept=False, delay=0.1, label="[sonda] ")


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
