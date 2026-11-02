"""
scraper_master.py - Master Supermercados, Erechim RS (https://www.lojaonline.mastersupermercados.com.br)

Platform : VipCommerce, organisation 193 (see markets/common/vipcommerce.py). Same JSON API
           and shared storefront login key as Rossi/Davo/Spani/Hirota/Violeta/Bergamini.
Store    : a single distribution centre, "Master Supermercados Matriz" (Fatima, Erechim) ->
           store_id "master:1:1", national prices.
Barcode  : inline codigo_barras on every product, ~99% valid GTIN (measured 2026-10-20; the
           rest are PLU codes on butcher items).
Runtime  : ~15 min for ~22.3k products (20 per page, 15 departments).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "master"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="lojaonline.mastersupermercados.com.br",
                           web_base="https://www.lojaonline.mastersupermercados.com.br", org_id=193)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
