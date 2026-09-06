"""
scraper_paodeacucar.py - Pao de Acucar (https://www.paodeacucar.com) - GPA group.
See markets/common/gpa.py for the API notes. Barcodes come from the PDP HTML (enricher).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.gpa import GpaMarket

STORE_KEY = "paodeacucar"
MARKET = GpaMarket(store_key=STORE_KEY, prefix="pa", web_base="https://www.paodeacucar.com",
                   store_id="paodeacucar", gpa_store_ids=[101, 532, 483, 1], card_label="Cartao Pao de Acucar")


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


def enrich(db, workers: int = 12, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.enrich(db, workers=workers, limit=limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape, enrich=enrich)
