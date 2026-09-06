"""
scraper_extra.py - Extra Mercado (https://www.extramercado.com.br) - GPA group.
See markets/common/gpa.py for the API notes. Barcodes come from the PDP HTML (enricher).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.gpa import GpaMarket

STORE_KEY = "extra"
MARKET = GpaMarket(store_key=STORE_KEY, prefix="ex", web_base="https://www.extramercado.com.br",
                   store_id="extramercado", gpa_store_ids=[483, 532, 1, 101], card_label="Cartao Extra")


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


def enrich(db, workers: int = 12, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.enrich(db, workers=workers, limit=limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
