"""
gpa.py - Shared scraper for the GPA group (Extra Mercado / Pao de Acucar).

API      : POST https://api.vendas.gpa.digital/{ex|pa}/search/category-page
           body {partner:"linx", page:N, resultsPerPage:48, multiCategory:<slug>,
                 sortBy:"relevance", department:"ecom", storeId:<id>, customerPlus:true}
           -> {totalPages, totalProducts, products:[{id, name, price, brand, stock,
                productPromotion{unitPrice,endDate,appExclusive,...}, productImages[], urlDetails}]}
Categories: /sitemap/mapa-de-categorias -> /categoria/<slug> top-level slugs.
Store    : online store ids (Extra 483, PdA 101) - fallbacks tried when a slug is empty.
Barcode  : NOT in the list API nor in /{prefix}/products/{id}. It is in the PDP HTML
           ("ean":"789..."), fetched once per new product by the enricher.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from markets.common.enrich import fetch_page_gtin, run_enrichment
from markets.common.geo import format_zip
from markets.common.http import make_session, post_json, request_with_retry
from markets.common.offer import make_offer, parse_brl

PAGE_SIZE = 48
DEFAULT_SLUGS = ["alimentos", "bebidas", "limpeza", "descartaveis", "bebe-e-crianca", "perfumaria", "petshop",
                 "bazar", "textil", "caras-do-brasil", "hortifruti", "carnes-e-aves", "frios-e-laticinios",
                 "padaria", "congelados"]


class GpaMarket:
    def __init__(self, *, store_key: str, prefix: str, web_base: str, store_id: str,
                 gpa_store_ids: List[int], card_label: str):
        self.key = store_key
        self.api = f"https://api.vendas.gpa.digital/{prefix}/search/category-page"
        self.web = web_base
        self.store_id = store_id
        self.gpa_store_ids = gpa_store_ids
        self.card_label = card_label
        self.headers = {"Origin": web_base, "Referer": web_base + "/"}
        self.log = f"[{store_key}] "

    # ------------------------------------------------------------ categories
    def slugs(self, session) -> List[str]:
        r = request_with_retry(session, "GET", f"{self.web}/sitemap/mapa-de-categorias", timeout=25, max_attempts=2)
        if r is not None and r.status_code == 200:
            found = []
            for slug in re.findall(r'/categoria/([^"\'#?/\s<>]+)', r.text, flags=re.I):
                if slug not in found:
                    found.append(slug)
            if found:
                return found
        return DEFAULT_SLUGS

    def page(self, session, slug: str, page: int, gpa_store_id: int) -> Dict[str, Any]:
        body = {"partner": "linx", "page": page, "resultsPerPage": PAGE_SIZE, "multiCategory": slug,
                "sortBy": "relevance", "department": "ecom", "storeId": gpa_store_id, "customerPlus": True}
        return post_json(session, self.api, json_body=body, headers=self.headers, timeout=30, log_prefix=self.log) or {}

    # ------------------------------------------------------------ offer
    def offer(self, item: Dict[str, Any], category: str) -> Optional[Dict[str, Any]]:
        promo = item.get("productPromotion") or {}
        regular = parse_brl(item.get("price"))
        promo_price = parse_brl(promo.get("unitPrice")) if promo else None
        label = str(promo.get("tagLabel") or promo.get("description") or promo.get("label") or
                    promo.get("promoText") or "").strip() if promo else ""
        ptype = str(promo.get("tag") or promo.get("type") or "").strip() if promo else ""
        card = bool(promo.get("cardExclusive")) or ("cart" in (label + ptype).lower())
        app_only = bool(promo.get("appExclusive")) or ("app" in (label + ptype).lower())
        min_q = promo.get("minimumQuantity") or promo.get("minQuantity")
        if not min_q and label:
            m = re.search(r"na\s+(\d+)[ªa°]", label, re.I) or re.search(r"leve\s+(\d+)", label, re.I)
            if m:
                min_q = int(m.group(1))
        parts = []
        if card:
            parts.append(self.card_label)
        elif app_only:
            parts.append("App exclusivo")
        if label:
            parts.append(label)
        elif ptype:
            parts.append(ptype)
        images = item.get("productImages") or []
        img = str(images[0]) if images else ""
        image_url = img if img.startswith("http") else (f"{self.web}{img}" if img else None)
        url = str(item.get("urlDetails") or "").strip()
        if url and not url.startswith("http"):
            url = f"{self.web}{url}"
        stock = item.get("stock")
        return make_offer(
            product_id=item.get("id"), store_id=self.store_id, product_name=item.get("name"),
            regular_price=regular, promo_price=promo_price, promo_min_quantity=min_q,
            promo_end_at=promo.get("endDate") if promo else None,
            brand=item.get("brand"), category_path=category,
            offer_name=" | ".join(parts) or None,
            offer_tag=(self.card_label if card else ("App" if app_only else ("Desconto" if promo_price else None))),
            app_membership_required=(card or app_only) if promo else None,
            is_available=bool(stock) if isinstance(stock, bool) else None,
            product_url=url or None, image_url=image_url,
        )

    # ------------------------------------------------------------ scrape
    def scrape(self, db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
        session = make_session(self.headers)
        db.save_store_info(self.store_id, query_zip=format_zip(zip_code), name=f"{self.key} (online)")
        slugs = self.slugs(session)
        print(f"{self.log}{len(slugs)} categories")
        seen: set = set()
        total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
        for slug in slugs:
            # pick a storeId that returns products for this slug
            first = {}
            working = self.gpa_store_ids[0]
            for sid in self.gpa_store_ids:
                first = self.page(session, slug, 1, sid)
                if first.get("products") or first.get("totalProducts"):
                    working = sid
                    break
            page, added, empty_streak = 1, 0, 0
            data = first
            while data:
                items = data.get("products") or []
                total_pages = int(data.get("totalPages") or 1)
                batch = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    offer = self.offer(it, slug)
                    if offer and offer["product_id"] not in seen:
                        seen.add(offer["product_id"])
                        batch.append(offer)
                    if limit and len(seen) >= limit:
                        break
                if batch:
                    r = db.save(batch)
                    for k in total:
                        total[k] += r[k]
                    added += len(batch)
                    empty_streak = 0
                else:
                    # relevance sort repeats products across pages; a page with nothing
                    # new is normal (do NOT stop), only a long streak means the end
                    empty_streak += 1
                if not items or page >= total_pages or empty_streak >= 8 or (limit and len(seen) >= limit):
                    break
                if page % 25 == 0:
                    print(f"{self.log}{slug[:35]:<35} page {page}/{total_pages} (total {len(seen)})")
                page += 1
                time.sleep(0.12)
                data = self.page(session, slug, page, working)
            print(f"{self.log}{slug[:35]:<35} +{added:>5} pages={page} (total {len(seen)})")
            if limit and len(seen) >= limit:
                break
        return total

    # ------------------------------------------------------------ enrich
    def enrich(self, db, workers: int = 12, limit: Optional[int] = None) -> Dict[str, int]:
        import config

        def fetch(session, store_id, product_id, url):
            return fetch_page_gtin(session, url, allow_gtin8=True)

        # The GPA storefront WAF answers 403 to PDP requests that carry a Referer
        # together with Accept/Accept-Language (learned on paodeacucar, 2026-09-06:
        # same URL, same UA -> 200 without Referer, 403 with it). So: no Referer,
        # and a gentle 4 threads + 0.25 s.
        return run_enrichment(db, fetch=fetch, source="pdp", workers=min(workers, 4), limit=limit,
                              retry_not_found_days=config.BARCODE_RETRY_NOT_FOUND_DAYS,
                              headers={"Referer": None}, json_accept=False, delay=0.25, label=self.log)
