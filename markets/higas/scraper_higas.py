"""
scraper_higas.py - Higas Supermercados (Instabuy platform)

Platform : Instabuy -> the storefront moved to <subdomain>.instabuy.app.br (Next.js) and
           the product API moved from api.instabuy.com.br/apiv3 (dead, 404) to
           https://api.ibecom.com.br/api_ecommerce/v5  (needs header x-store-id).
Store    : GET https://api.instabuy.com.br/apiv3/store?partner_id=replicarhigas&zip_code=CEP
           still works -> branches with address/coords -> nearest -> store id + subdomain.
Listing  : GET /api_ecommerce/v5/menu -> departments; then per department
           GET /api_ecommerce/v5/items?category_id=<department id>&limit=30&page=N
           (30/page max, pagination.total_pages). Without a filter the API refuses
           page > 7 ("Paginacao profunda sem filtro nao e permitida") and every such 400
           counts towards a temporary 429 and then the IP ban.
           item: {id, name, brand, slug, image, unit_type, price_config{price,
                  price_discount{promo_price,end_date}}, stock{has_available_stock,max_purchase_quantity}}
Barcode  : the v5 API no longer exposes `barcodes` (apiv3 did). Filled from the legacy
           table (~65-80% of the catalogue) and tools/crossfill_barcodes.py.

RATE LIMIT (learned 2026-09-06 with the --probe mode on GitHub runners): api.ibecom.com.br
sits behind Cloudflare AND has its own limiter; a few 400/429 answers in a row lead to
403 {"error_message":"Acesso bloqueado"} for hours for the whole IP. So:
  * 400/429 = quota: cool down HIGAS_BACKOFF s (300) and retry the SAME page, never skip
  * one request every HIGAS_DELAY seconds (default 4 s)
  * browser headers WITHOUT the site's `ibsessionid` (with it the quota hits after ~7 calls),
    curl_cffi Chrome impersonation when installed
  * on "Acesso bloqueado" we stop immediately (retrying only extends the ban)
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

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
BACKOFF = float(os.getenv("HIGAS_BACKOFF", "300"))


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


def _departments(client) -> List[Tuple[str, str]]:
    """(label, id) of the departments in the storefront menu (18 for Higas)."""
    r = client.get(f"{API_V5}/menu", {})
    out: List[Tuple[str, str]] = []
    try:
        for it in ((r.json() or {}).get("data") or {}).get("items") or []:
            link = it.get("link") or {}
            if link.get("type") == "department" and link.get("department_id"):
                out.append((str(it.get("label") or link.get("department_slug") or ""), str(link["department_id"])))
    except Exception:
        pass
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session()
    store = resolve_store(session, db, zip_code)
    store_id = str(store["id"])
    subdomain = store.get("subdomain") or DEFAULT_SUBDOMAIN
    web_base = f"https://{subdomain}.instabuy.app.br"
    headers = {"x-store-id": store_id, "Origin": web_base, "Referer": web_base + "/", "Accept": "application/json",
               "Content-Type": "application/json"}
    client = _ApiClient(headers)
    time.sleep(DELAY)
    depts = _departments(client)
    if not depts:
        raise RuntimeError("Higas: /menu returned no departments (blocked?)")
    print(f"[higas] client={client.kind} delay={DELAY}s departments={len(depts)}")

    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    soft_errors = 0
    for label, dept_id in depts:
        page, total_pages, added = 1, 1, 0
        while page <= total_pages and page <= 200:
            time.sleep(DELAY)
            try:
                # category_id=<menu department id> is the filter the API honours; with a filter
                # deep pagination is allowed (without one, page > 7 answers 400 -> ban).
                r = client.get(f"{API_V5}/items", {"category_id": dept_id, "limit": PAGE_LIMIT, "page": page})
            except Exception as exc:
                soft_errors += 1
                if soft_errors > 8:
                    raise
                print(f"[higas] {label} page {page}: {exc.__class__.__name__} - waiting 30s")
                time.sleep(30)
                continue
            text = r.text if hasattr(r, "text") else ""
            if r.status_code == 403 and "bloqueado" in text.lower():
                raise RuntimeError("Higas: API answered 'Acesso bloqueado' (IP banned for a while). "
                                   "Increase HIGAS_DELAY or run later / from another IP.")
            if r.status_code != 200 or looks_like_challenge(text):
                soft_errors += 1
                if soft_errors > 8:
                    raise RuntimeError(f"Higas: too many HTTP {r.status_code} answers - giving up for this run")
                wait = BACKOFF * min(soft_errors, 4)
                print(f"[higas] {label} page {page}: HTTP {r.status_code} - cooldown {wait:.0f}s")
                time.sleep(wait)
                continue
            soft_errors = 0
            try:
                body = r.json() or {}
            except ValueError:
                body = {}
            items = body.get("data") or []
            total_pages = int((body.get("pagination") or {}).get("total_pages") or 0)
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
                added += len(batch)
            if not items or (limit and len(seen) >= limit):
                break
            page += 1
        print(f"[higas] {label[:32]:<32} +{added:>5} pages={total_pages} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


def probe(zip_code: str) -> None:
    """Endpoint diagnostics (run on a fresh IP with --probe): which filter parameter the API honours."""
    session = make_session()
    store = resolve_store(session, _NullDB(), zip_code)
    store_id = str(store["id"]); sub = store.get("subdomain") or DEFAULT_SUBDOMAIN
    web = f"https://{sub}.instabuy.app.br"
    client = _ApiClient({"x-store-id": store_id, "Origin": web, "Referer": web + "/", "Accept": "application/json"})
    print(f"[higas-probe] client={client.kind} store={store_id}")

    def call(label, path, params):
        time.sleep(6)
        try:
            r = client.get(f"{API_V5}/{path}", params)
            body = r.text[:160].replace(chr(10), " ")
            n = None; pag = None
            try:
                d = r.json(); n = len(d.get("data") or []) if isinstance(d.get("data"), list) else None
                pag = d.get("pagination")
            except Exception:
                pass
            print(f"[higas-probe] {label:<44} HTTP {r.status_code} items={n} pagination={pag} {'' if n else body}")
            return r
        except Exception as exc:
            print(f"[higas-probe] {label:<44} EXC {exc.__class__.__name__}")

    r = call("menu", "menu", {})
    deps = []
    try:
        for it in (r.json().get("data") or {}).get("items") or []:
            link = it.get("link") or {}
            if link.get("type") == "department":
                cats = [(c.get("label"), (c.get("link") or {}).get("category_id"), (c.get("link") or {}).get("category_slug"))
                        for c in it.get("children") or [] if (c.get("link") or {}).get("type") == "category"]
                deps.append((it.get("label"), link.get("department_id"), link.get("department_slug"), cats))
    except Exception:
        pass
    print(f"[higas-probe] departments: {len(deps)}")
    if len(deps) < 2:
        return
    d1, d2 = deps[0], deps[1]
    c1 = d1[3][0] if d1[3] else (None, None, None)
    print(f"[higas-probe] D1={d1[:3]} C1={c1}")
    call("items category_id=DEPT(D1)", "items", {"category_id": d1[1], "limit": 30, "page": 1})
    call("items category_id=DEPT(D2)", "items", {"category_id": d2[1], "limit": 30, "page": 1})
    call("items category_id=DEPT(D1) page=12", "items", {"category_id": d1[1], "limit": 30, "page": 12})
    call("items subcategory_id=C1", "items", {"subcategory_id": c1[1], "limit": 30, "page": 1})
    call("recs/departments/D1 N=30 page=10", f"recommendations/departments/{d1[1]}", {"N": 30, "page": 10})
    call("recs/departments/D1 N=30 page=60", f"recommendations/departments/{d1[1]}", {"N": 30, "page": 60})
    call("recs/categories/C1 N=30 page=3", f"recommendations/categories/{c1[1]}", {"N": 30, "page": 3})


class _NullDB:
    def save_store_info(self, *a, **k):
        pass


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, probe=probe)
