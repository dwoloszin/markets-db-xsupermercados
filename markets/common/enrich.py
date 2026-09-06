"""
enrich.py - Generic barcode backfill from product pages (PDP) or lookup APIs.

Pattern (same for Extra, Pao de Acucar, Sonda, Carrefour):
  1. `db.load_missing_barcodes()` -> rows without barcode that were not tried
     recently (state table). After the first full pass this list is tiny:
     only NEW products need a page fetch. Barcodes never change.
  2. Fetch pages in a thread pool (thread-local sessions), extract the GTIN.
  3. Write barcodes in batches; record found/not_found per product so the next
     run skips them (not_found is retried after BARCODE_RETRY_NOT_FOUND_DAYS).

Extraction helpers:
  * gtin_from_jsonld(html)  - schema.org Product gtin/gtin13/gtin14/gtin12/gtin8
  * gtin_from_html_keys(html) - "ean": "789...", "gtin13": "...", EAN: 789...
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .gtin import first_gtin
from .http import ThreadSessions, looks_like_challenge

_LDJSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_KEY_RE = re.compile(r'"(?:ean|gtin13|gtin14|gtin12|gtin8|gtin|barcode|codigo_barras|eanCode)"\s*:\s*"?(\d{8,14})', re.I)
_EAN_TEXT_RE = re.compile(r'\bEAN\b[^0-9]{0,20}(\d{8,14})', re.I)

# (store_id, product_id, barcode|None, http_status|None)
FetchResult = Tuple[str, str, Optional[str], Optional[int]]


def _iter_products(node):
    if isinstance(node, dict):
        if node.get("@type") in ("Product", ["Product"]):
            yield node
        for v in node.values():
            yield from _iter_products(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_products(v)


def gtin_from_jsonld(text: str, *, allow_gtin8: bool = True) -> Optional[str]:
    for block in _LDJSON_RE.findall(text or ""):
        raw = html_module.unescape(block).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for prod in _iter_products(data):
            found = first_gtin(prod.get("gtin13"), prod.get("gtin"), prod.get("gtin14"), prod.get("gtin12"),
                               prod.get("gtin8"), prod.get("ean"), allow_gtin8=allow_gtin8)
            if found:
                return found
    return None


def gtin_from_html_keys(text: str, *, allow_gtin8: bool = True) -> Optional[str]:
    for m in _KEY_RE.finditer(text or ""):
        found = first_gtin(m.group(1), allow_gtin8=allow_gtin8)
        if found:
            return found
    m = _EAN_TEXT_RE.search(text or "")
    if m:
        return first_gtin(m.group(1), allow_gtin8=allow_gtin8)
    return None


def fetch_page_gtin(session: requests.Session, url: str, *, allow_gtin8: bool = True,
                    timeout: float = 25.0) -> Tuple[Optional[str], Optional[int]]:
    """GET a product page and extract a GTIN (JSON-LD first, then key regex)."""
    for attempt in range(2):
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException:
            if attempt == 0:
                time.sleep(2)
                continue
            return None, None
        if r.status_code == 429 or (r.status_code in (403, 503) and looks_like_challenge(r.text)):
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None, r.status_code
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1"):
            r.encoding = "utf-8"
        text = r.text
        return (gtin_from_jsonld(text, allow_gtin8=allow_gtin8)
                or gtin_from_html_keys(text, allow_gtin8=allow_gtin8)), 200
    return None, 429


def run_enrichment(
    db,
    *,
    fetch: Callable[[requests.Session, str, str, str], Tuple[Optional[str], Optional[int]]],
    source: str,
    workers: int = 12,
    limit: Optional[int] = None,
    retry_not_found_days: int = 14,
    headers: Optional[Dict[str, str]] = None,
    json_accept: bool = False,
    need_url: bool = True,
    delay: float = 0.0,
    label: str = "",
) -> Dict[str, int]:
    """
    Generic driver. `fetch(session, store_id, product_id, product_url)` returns
    (barcode_or_None, http_status_or_None).
    """
    missing = db.load_missing_barcodes(retry_not_found_days=retry_not_found_days, need_url=need_url)
    if limit:
        missing = missing[:limit]
    total = len(missing)
    print(f"{label}products needing barcode: {total:,}")
    if not total:
        return {"fetched": 0, "found": 0, "updated": 0}

    pool = ThreadSessions(headers=headers, json_accept=json_accept)
    found_rows: List[Tuple[str, str, str, str]] = []
    state_rows: List[Tuple[str, str, str, Optional[int]]] = []
    fetched = found = updated = blocked = 0
    log_every = max(1, total // 20)

    def _work(store_id: str, product_id: str, url: Optional[str]) -> FetchResult:
        if delay:
            time.sleep(delay)
        try:
            bc, status = fetch(pool.get(), store_id, product_id, url or "")
        except Exception:
            bc, status = None, None
        return store_id, product_id, bc, status

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_work, s, p, u) for s, p, u, _name in missing]
        for fut in as_completed(futures):
            store_id, product_id, bc, status = fut.result()
            fetched += 1
            if bc:
                found += 1
                found_rows.append((store_id, product_id, bc, source))
                state_rows.append((store_id, product_id, "found", status))
            elif status in (200, 404, 410):
                # the page exists (or is gone) and has no barcode -> retry only after the TTL
                state_rows.append((store_id, product_id, "not_found", status))
            else:
                # 403/429/5xx/network: a blocked or failing fetch is NOT "no barcode" -> retry next run
                state_rows.append((store_id, product_id, "error", status))
                blocked += 1
                if blocked >= 30 and found == 0:
                    print(f"{label}too many blocked fetches (HTTP {status}) - stopping this pass to avoid a ban")
                    for f in futures:
                        f.cancel()
                    break
            if len(found_rows) >= 300 or len(state_rows) >= 600:
                updated += db.update_barcodes(found_rows)
                db.record_enrich_state(state_rows)
                found_rows.clear()
                state_rows.clear()
            if fetched % log_every == 0 or fetched == total:
                print(f"{label}  progress {fetched:>6}/{total} ({100 * fetched / total:5.1f}%) found={found}")
    if found_rows or state_rows:
        updated += db.update_barcodes(found_rows)
        db.record_enrich_state(state_rows)
    print(f"{label}barcodes found: {found:,}/{total:,}  written: {updated:,}")
    return {"fetched": fetched, "found": found, "updated": updated}
