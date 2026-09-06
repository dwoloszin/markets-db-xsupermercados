"""
scraper_higas.py - Higas Supermercados (Instabuy platform)

Platform : Instabuy -> the storefront moved to <subdomain>.instabuy.app.br (Next.js) and
           the product API moved from api.instabuy.com.br/apiv3 (dead, 404) to
           https://api.ibecom.com.br/api_ecommerce/v5  (needs header x-store-id).
Store    : GET https://api.instabuy.com.br/apiv3/store?partner_id=replicarhigas&zip_code=CEP
           still works -> branches with address/coords -> nearest -> store id + subdomain.
Listing  : GET /api_ecommerce/v5/items?limit=30&page=N  (30/page max; pagination.total_pages)
           item: {id, name, brand, slug, image, unit_type, price_config{price,
                  price_discount{promo_price,end_date}}, stock{has_available_stock,max_purchase_quantity}}
Barcode  : the v5 API no longer exposes `barcodes` (apiv3 did). Filled from the legacy
           table (~65-80% of the catalogue) and tools/crossfill_barcodes.py.

RATE LIMIT (learned 2026-09-06): api.ibecom.com.br sits behind Cloudflare AND has its
own limiter. Around 8 requests in quick succession answer 400/429, then 403
{"error_message":"Acesso bloqueado"} for a long time (hours) for the whole IP. So:
  * one request every HIGAS_DELAY seconds (default 2.5 s -> ~18 min for 425 pages)
  * browser headers + the site's `ibsessionid`, curl_cffi Chrome impersonation when installed
  * on "Acesso bloqueado" we stop immediately (retrying only extends the ban)
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from markets.common.geo import format_zip, haversine_km, normalize_zip, to_float, zip_coords
from markets.common.http import get_json, looks_like_challenge, make_session
from markets.common.offer import make_offer

STORE_KEY = "higas"
PARTNER_ID = "replicarhigas"
DEFAULT_STORE_ID = "66466cdefafdf200a3352cd5"
DEFAULT_SUBDOMAIN = "supermercadohigas6"
API_V5 = "https://api.ibecom.com.br/api_ecommerce/v5"
IMAGE_BASE = "https://assets.ibecom.com.br/ib.item.image.medium/m-"
PAGE_LIMIT = 30
DELAY = float(os.getenv("HIGAS_DELAY", "2.5"))


def resolve_store(session, db, zip_code: str) -> Dict[str, Any]:
    zdigits = normalize_zip(zip_code)
    data = get_json(session, "https://api.instabuy.com.br/apiv3/store",
                    params={"partner_id": PARTNER_ID, "zip_code": zdigits}, log_prefix="[higas] ")
    stores = [s for s in ((data or {}).get("data") or []) if isinstance(s, dict) and s.get("id")]
    if not stores:
        db.save_store_info(DEFAULT_STORE_ID, query_zip=format_zip(zdigits), name="Higas (default store)")
        return {"id": DEFAULT_STORE_ID, "subdomain": DEFAULT_SUBDOMAIN}
    coords = zip_coords(zdigits, session)

    def score(s):
        api_d = to_float(s.get("distance") or s.get("distance_km"))
        addr = s.get("address") or {}
        sz = normalize_zip(addr.get("zipcode"))
        zip_gap = abs(int(sz) - int(zdigits)) if len(sz) == 8 and len(zdigits) == 8 else 1e9
        geo = (s.get("spatial_position") or {}).get("coordinates") or []
        lat, lon = (to_float(geo[1]), to_float(geo[0])) if len(geo) >= 2 else (to_float(s.get("latitude")), to_float(s.get("longitude")))
        geo_d = haversine_km(coords[0], coords[1], lat, lon) if coords and lat is not None and lon is not None else 1e9
        return (api_d if api_d is not None else 1e9, geo_d, zip_gap)

    best = min(stores, key=score)
    addr = best.get("address") or {}
    geo = (best.get("spatial_position") or {}).get("coordinates") or []
    db.save_store_info(
        str(best["id"]), query_zip=format_zip(zdigits), name=best.get("name"),
        address=", ".join(str(p) for p in [addr.get("street"), addr.get("street_number"), addr.get("neighborhood")] if p) or None,
        city=addr.get("city"), state=addr.get("state"), store_zip=addr.get("zipcode"),
        latitude=geo[1] if len(geo) >= 2 else best.get("latitude"), longitude=geo[0] if len(geo) >= 2 else best.get("longitude"),
        payload={k: v for k, v in best.items() if k in ("id", "name", "subdomain", "address", "phone", "partner_id")},
    )
    print(f"[higas] store {best.get('name')} id={best['id']} subdomain={best.get('subdomain')} ({addr.get('city')})")
    return best


def _offer(item: Dict[str, Any], store_id: str, web_base: str) -> Optional[Dict[str, Any]]:
    pc = item.get("price_config") or {}
    disc = pc.get("price_discount") or {}
    stock = item.get("stock") or {}
    image = item.get("image") or ((item.get("images") or [None])[0])
    slug = item.get("slug")
    return make_offer(
        product_id=item.get("id"), store_id=store_id, product_name=item.get("name"),
        regular_price=pc.get("price"), promo_price=disc.get("promo_price"),
        promo_end_at=disc.get("end_date"),
        offer_tag="Exclusivo e-commerce" if disc.get("exclusive_for_ecommerce") else None,
        brand=item.get("brand"), unit=item.get("unit_type"),
        is_available=stock.get("has_available_stock") if isinstance(stock.get("has_available_stock"), bool) else None,
        stock=stock.get("max_purchase_quantity"),
        product_url=f"{web_base}/produto/{slug}" if slug else None,
        image_url=f"{IMAGE_BASE}{image}" if image else None,
    )


class _ApiClient:
    """requests by default; curl_cffi (Chrome TLS fingerprint) when installed."""

    def __init__(self, headers: Dict[str, str]):
        self.headers = headers
        self.kind = "requests"
        try:
            from curl_cffi import requests as cr  # type: ignore
            self.session = cr.Session(impersonate="chrome")
            self.kind = "curl_cffi"
        except Exception:
            self.session = make_session(headers)

    def get(self, url: str, params: Dict[str, Any]):
        return self.session.get(url, params=params, headers=self.headers, timeout=40)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session()
    store = resolve_store(session, db, zip_code)
    store_id = str(store["id"])
    subdomain = store.get("subdomain") or DEFAULT_SUBDOMAIN
    web_base = f"https://{subdomain}.instabuy.app.br"
    headers = {"x-store-id": store_id, "Origin": web_base, "Referer": web_base + "/", "Accept": "application/json",
               "Content-Type": "application/json"}
    sess = get_json(session, "https://api.instabuy.com.br/auth/client/session",
                    params={"subdomain": subdomain, "host": f"{subdomain}.instabuy.app.br"}, max_attempts=2)
    sid = ((sess or {}).get("data") or {}).get("id")
    if sid:
        headers["ibsessionid"] = str(sid)
    client = _ApiClient(headers)
    print(f"[higas] client={client.kind} delay={DELAY}s")

    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    page, total_pages = 1, 1
    soft_errors = 0
    while page <= total_pages and page <= 2000:
        try:
            r = client.get(f"{API_V5}/items", {"limit": PAGE_LIMIT, "page": page})
        except Exception as exc:
            soft_errors += 1
            if soft_errors > 5:
                raise
            print(f"[higas] page {page}: {exc.__class__.__name__} - waiting 20s")
            time.sleep(20)
            continue
        text = r.text if hasattr(r, "text") else ""
        if r.status_code == 403 and "bloqueado" in text.lower():
            raise RuntimeError("Higas: API answered 'Acesso bloqueado' (IP banned for a while). "
                               "Increase HIGAS_DELAY or run later / from another IP.")
        if r.status_code != 200 or looks_like_challenge(text):
            soft_errors += 1
            if soft_errors > 5:
                raise RuntimeError(f"Higas: too many HTTP {r.status_code} answers - stopping to avoid a ban")
            wait = 30 * soft_errors
            print(f"[higas] page {page}: HTTP {r.status_code} - waiting {wait}s")
            time.sleep(wait)
            continue
        try:
            body = r.json() or {}
        except ValueError:
            body = {}
        items = body.get("data") or []
        total_pages = int((body.get("pagination") or {}).get("total_pages") or total_pages)
        batch = []
        for it in items:
            if not isinstance(it, dict) or it.get("item_type") not in (None, "product"):
                continue
            offer = _offer(it, store_id, web_base)
            if offer and offer["product_id"] not in seen:
                seen.add(offer["product_id"])
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            rr = db.save(batch)
            for k in total:
                total[k] += rr[k]
        if page % 20 == 0 or page == total_pages:
            print(f"[higas] page {page}/{total_pages} (total {len(seen)})")
        if not items or (limit and len(seen) >= limit):
            break
        page += 1
        time.sleep(DELAY)
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
