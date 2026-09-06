"""
scraper_tenda.py - Tenda Atacado (https://www.tendaatacado.com.br)

Platform : Stoom / Java Spring API  https://api.tendaatacado.com.br/api
Store    : GET /public/branch/zip/{cep} -> branches sorted by distance; the chosen
           branch id goes into cookie `_Tendaatacado-branchID` (prices/stock per branch).
Listing  : there is NO category listing endpoint; /public/store/search?query=..&page=N
           returns 20/page, max 25 pages per query. Coverage comes from running many
           queries (department links + sub-category links + keywords + a-z) and
           de-duplicating by product id. Queries run in 4 threads.
Barcode  : inline `barcode` (EAN-13, ~98%).
Prices   : price (web) | wholesalePrices[].price/minQuantity (bulk tiers) | promotions[] (app).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from markets.common.geo import format_zip, normalize_zip
from markets.common.gtin import first_gtin
from markets.common.http import ThreadSessions, get_json, make_session
from markets.common.offer import make_offer, parse_brl

STORE_KEY = "tenda"
API_BASE = "https://api.tendaatacado.com.br/api"
WEB_BASE = "https://www.tendaatacado.com.br"
HEADERS = {"Origin": WEB_BASE, "Referer": WEB_BASE + "/", "Web-Platform": "web-desktop"}

DEPARTMENTS = ["mercearia", "higiene-e-perfumaria", "bebidas", "limpeza", "frios-e-laticinios", "bazar",
               "bomboniere", "congelados", "carnes-aves-e-peixes", "produtos-select", "hortifruti",
               "paes-e-bolos", "bebe", "pet-shop"]
KEYWORDS = ["leite", "carne", "frango", "pao", "arroz", "feijao", "oleo", "azeite", "cafe", "acucar", "sal",
            "macarrao", "molho", "biscoito", "chocolate", "sorvete", "queijo", "iogurte", "manteiga", "presunto",
            "cerveja", "refrigerante", "suco", "agua", "vinho", "sabao", "detergente", "shampoo", "desodorante",
            "creme", "fralda", "papel", "esponja", "saco", "farinha", "tempero", "salgadinho", "bala", "cereal"]
ALPHABET = list("abcdefghijklmnopqrstuvwxyz")


def _resolve_store(session, db, zip_code: str) -> str:
    cep = normalize_zip(zip_code)
    data = get_json(session, f"{API_BASE}/public/branch/zip/{cep}", log_prefix="[tenda] ")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Tenda: no branch found for CEP {cep}")
    nearest = data[0]
    addr = nearest.get("address") or {}
    store_id = str(nearest.get("id"))
    db.save_store_info(store_id, query_zip=format_zip(cep), name=nearest.get("name"),
                       address=addr.get("addressLine1"), city=addr.get("city"), state=addr.get("state"),
                       store_zip=addr.get("zipCode") or addr.get("zip"),
                       latitude=addr.get("latitude"), longitude=addr.get("longitude"), payload=nearest)
    print(f"[tenda] branch {store_id} {nearest.get('name')} ({addr.get('city')}) dist={nearest.get('distance')}m")
    return store_id


def _queries(session) -> List[str]:
    subs: List[str] = []
    data = get_json(session, f"{API_BASE}/public/store/all-categories", log_prefix="[tenda] ")
    nodes = data if isinstance(data, list) else ((data or {}).get("categories") or (data or {}).get("items") or [])

    def walk(items):
        for n in items or []:
            if not isinstance(n, dict):
                continue
            link = str(n.get("link") or "").strip("/")
            if link and link not in DEPARTMENTS and "-" not in link:
                subs.append(link)
            walk(n.get("children") or n.get("categories") or n.get("subcategories") or [])

    walk(nodes)
    out, seen = [], set()
    for q in DEPARTMENTS + subs + KEYWORDS + ALPHABET:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _prices(p: Dict[str, Any]) -> Tuple[Any, Any, Any, Optional[str]]:
    regular = parse_brl(p.get("price"))
    best_w, best_w_q = None, None
    for w in p.get("wholesalePrices") or []:
        if not isinstance(w, dict):
            continue
        price = parse_brl(w.get("price"))
        if price and (best_w is None or price < best_w):
            best_w, best_w_q = price, w.get("minQuantity")
    app_price, tag = None, None
    for promo in p.get("promotions") or []:
        if not isinstance(promo, dict):
            continue
        price = parse_brl(promo.get("price"))
        if price and (app_price is None or price < app_price):
            app_price = price
            tag = "App + Tenda Card" if "PLUS" in str(promo.get("type") or "").upper() else "App"
    if best_w and (app_price is None or best_w <= app_price):
        return regular, best_w, best_w_q, None
    if app_price:
        return regular, app_price, None, tag
    return regular, None, None, None


def _offer(p: Dict[str, Any], store_id: str) -> Optional[Dict[str, Any]]:
    regular, promo, min_q, tag = _prices(p)
    photos = p.get("photos") or []
    image = p.get("thumbnail")
    if photos:
        first = photos[0]
        image = (first.get("url") or first.get("link")) if isinstance(first, dict) else first
    brand = p.get("brand")
    dept = p.get("department") or {}
    raw_url = str(p.get("url") or "").strip()
    url = raw_url if raw_url.startswith("http") else (f"{WEB_BASE}/{raw_url.strip('/')}" if raw_url else None)
    stock = None
    for inv in p.get("inventory") or []:
        if isinstance(inv, dict) and str(inv.get("branchId", "")) == str(store_id):
            stock = inv.get("totalAvailable") or inv.get("quantity")
            break
    if stock is None:
        stock = p.get("totalStock")
    return make_offer(
        product_id=p.get("id"), store_id=store_id, product_name=p.get("name"),
        regular_price=regular, promo_price=promo,
        barcode=first_gtin(p.get("barcode"), p.get("ean"), p.get("gtin"), allow_gtin8=False), barcode_source="inline",
        brand=brand.get("name") if isinstance(brand, dict) else brand,
        category_path=dept.get("name") if isinstance(dept, dict) else None,
        promo_min_quantity=min_q, offer_tag=tag, app_membership_required=bool(tag) if tag else None,
        unit=p.get("unit") or p.get("measurementUnit"), stock=stock,
        is_available=(int(float(stock)) > 0) if stock not in (None, "") else None,
        product_url=url, image_url=image,
    )


def _run_query(pool: ThreadSessions, query: str, max_pages: int = 25) -> List[Dict[str, Any]]:
    session = pool.get()
    out: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = get_json(session, f"{API_BASE}/public/store/search", params={"query": query, "page": page},
                        max_attempts=3, log_prefix="[tenda] ")
        if not data:
            break
        products = data.get("products") or []
        if not products:
            break
        out.extend(p for p in products if isinstance(p, dict))
        total_pages = int(data.get("total_pages") or data.get("totalPages") or 1)
        if page >= total_pages:
            break
        time.sleep(0.05)
    return out


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session(HEADERS)
    store_id = _resolve_store(session, db, zip_code)
    queries = _queries(session)
    print(f"[tenda] {len(queries)} search queries, 4 threads")
    pool = ThreadSessions(headers=HEADERS, cookies={"_Tendaatacado-branchID": store_id})
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_run_query, pool, q): q for q in queries}
        for fut in as_completed(futures):
            q = futures[fut]
            done += 1
            try:
                products = fut.result()
            except Exception as exc:
                print(f"[tenda] query {q!r} failed: {exc}")
                continue
            batch = []
            for p in products:
                offer = _offer(p, store_id)
                if offer and offer["product_id"] not in seen:
                    seen.add(offer["product_id"])
                    batch.append(offer)
                if limit and len(seen) >= limit:
                    break
            if batch:
                r = db.save(batch)
                for k in total:
                    total[k] += r[k]
            if batch or done % 10 == 0:
                print(f"[tenda] [{done}/{len(queries)}] {q!r:<28} +{len(batch):>4} (total {len(seen)})")
            if limit and len(seen) >= limit:
                for f in futures:
                    f.cancel()
                break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
