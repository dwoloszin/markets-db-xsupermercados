"""
markets/__init__.py — Auto-discovers all market scrapers from tier subfolders.
"""

from __future__ import annotations
import importlib
import pkgutil
from pathlib import Path
from typing import Any, List, Tuple

TIER_PACKAGES = [
    "markets.tier1_inline_barcodes",
    "markets.tier2_partial_barcodes",
    "markets.tier3_no_barcodes",
]

TIER_LABELS = {
    "markets.tier1_inline_barcodes":  "Tier 1 (inline barcodes)",
    "markets.tier2_partial_barcodes": "Tier 2 (partial barcodes)",
    "markets.tier3_no_barcodes":      "Tier 3 (cross-market enrichment)",
}


def discover_scraper_modules() -> List[Tuple[str, Any]]:
    found = []
    for pkg_name in TIER_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        pkg_path = Path(pkg.__file__).parent
        for _finder, mod_name, _ispkg in pkgutil.iter_modules([str(pkg_path)]):
            if mod_name.startswith("market_scrap_") and mod_name.endswith("_departamentos"):
                full_name = f"{pkg_name}.{mod_name}"
                try:
                    module = importlib.import_module(full_name)
                    found.append((pkg_name, module))
                except Exception as exc:
                    print(f"  markets: could not import {full_name}: {exc}")
    return found


def get_scraper_class(module: Any):
    for name in dir(module):
        if name.endswith("DepartamentosScraper") and not name.startswith("_"):
            return getattr(module, name)
    return None


__all__ = ["TIER_PACKAGES", "TIER_LABELS", "discover_scraper_modules", "get_scraper_class"]
