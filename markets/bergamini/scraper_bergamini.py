"""
scraper_bergamini.py - Hiper Bergamini, zona norte SP (https://www.hiperbergamini.com.br)

Platform : VipCommerce, organisation 282 (see markets/common/vipcommerce.py). Same JSON API
           and shared storefront login key as Rossi/Davo/Spani/Hirota/Violeta.
Store    : CD 1 = Hipermercado Bergamini (Jacana). CD 2 is a different banner,
           "Supermercado Ourinhos" (Tremembe), with its own catalogue (8 of 160 products in
           common, same prices on those). Scraped from CD 1 -> store_id "bergamini:1:1".
Barcode  : inline codigo_barras on every product, ~99% valid GTIN (measured 2026-09-22).
Runtime  : ~10 min for ~14.2k products (20 per page, 16 departments).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "bergamini"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="hiperbergamini.com.br",
                           web_base="https://www.hiperbergamini.com.br", org_id=282)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)
