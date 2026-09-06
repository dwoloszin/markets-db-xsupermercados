"""
scraper_swift.py - Swift (https://www.swift.com.br)

Platform : VTEX (storefront is Remix, catalog is plain VTEX)
Listing  : GET /api/categories  -> categoryList[].linkId (category slugs)
           GET /api/catalog_system/pub/products/search/{slug}?_from&_to (50/page)
Store    : prices are national; store_id = swift:<uf>:<city> from the ZIP
           (postalcode cookie only affects serviceability).
Barcode  : inline, items[0].ean (~99.6%) -> Tier 1, no enrichment.
Runtime  : ~1-2 min (about 1k products).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from markets.common.geo import format_zip, normalize_zip, zip_info
from markets.common.http import get_json, make_session
from markets.common.offer import slugify
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "swift"
BASE_URL = "https://www.swift.com.br"


def _categories(session) -> List[Tuple[str, str]]:
    data = get_json(session, f"{BASE_URL}/api/categories", log_prefix="[swift] ")
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in (data or {}).get("categoryList") or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("linkId") or "").strip()
        slug = unquote(urlparse(link).path.strip("/"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, str(item.get("name") or slug)))
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    zdigits = normalize_zip(zip_code) or "01153000"
    info = zip_info(zdigits, session)
    store_id = f"swift:{slugify(info.get('state'))}:{slugify(info.get('city'))}" if info else f"swift:cep:{zdigits}"
    db.save_store_info(store_id, query_zip=format_zip(zdigits), name="Swift (entrega)",
                       city=info.get("city"), state=info.get("state"), store_zip=format_zip(zdigits))
    session.cookies.set("postalcode", zdigits, domain="www.swift.com.br")

    cats = _categories(session)
    print(f"[swift] {len(cats)} categories, store_id={store_id}")
    catalog = VtexCatalog(session, BASE_URL, delay=0.2, log_prefix="[swift] ")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for slug, name in cats:
        batch = []
        for product in catalog.iter_products(None, path=slug, label=slug):
            pid = str(product.get("productId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            offer = vtex_offer(product, store_id=store_id, base_url=BASE_URL, category_label=name,
                               allow_gtin8=True)
            if offer:
                link_text = product.get("linkText") or ""
                if link_text:
                    offer["product_url"] = f"{BASE_URL}/detail/{link_text}"
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[swift] {name[:40]:<40} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
        time.sleep(0.2)
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
