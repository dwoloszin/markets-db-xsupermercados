"""
list_stores.py - Enumerate every physical store the per-store markets expose,
with a CEP that selects it, so you can build SCRAPE_ZIP_CODES.

Each CEP in SCRAPE_ZIP_CODES selects ONE store per per-store market (Atacadao,
Carrefour, Tenda, Higas). To scrape a given store, put its CEP (or any CEP next
to it) in the list. This tool writes exports/stores_<market>.csv and prints a
summary. It only reads public store lists; nothing is scraped.

Sources (2026-09-09):
  tenda      GET api.tendaatacado.com.br/api/public/branch/zip/<cep>   -> ALL 41 branches, with address
  higas      GET api.instabuy.com.br/apiv3/store?partner_id=replicarhigas&zip_code=<cep> -> all 6 stores
  carrefour  POST mercado.carrefour.com.br/action/stores-from-pickups {city: ""} -> all ~130 pickup stores
  atacadao   GET www.atacadao.com.br/api/checkout/pub/regions?postalCode=<cep> only answers per CEP
             (sellers "atacadaobr##", no address) -> probed over a grid of CEPs; the CEP column is
             the probe CEP that returned the seller first (use it as-is: it selects that seller)

Usage:
    python -m tools.list_stores                 # all four markets -> exports/stores_*.csv
    python -m tools.list_stores --markets tenda carrefour
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import OrderedDict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from markets.common.geo import format_zip, normalize_zip  # noqa: E402
from markets.common.http import get_json, make_session, request_with_retry  # noqa: E402

FIELDS = ["market", "store_id", "name", "city", "state", "cep", "address", "note"]

# One CEP per Brazilian capital / big SP-state city: enough to reveal every Atacadao seller.
ATACADAO_PROBE_CEPS = [
    "01001-000", "02401-200", "03015-000", "04675-170", "05001-000", "06086-060", "07010-000", "08032-230",
    "09010-000", "09710-000", "09910-000", "06400-000", "06700-000", "06300-000", "08500-000", "08600-000",
    "08900-000", "07400-000", "12200-000", "13010-000", "13200-000", "18010-000", "11010-000", "14010-000",
    "15010-000", "16010-000", "17010-000", "19010-000", "20010-000", "30110-000", "80010-000", "70040-000",
    "40020-000", "60025-000", "50010-000", "90010-000", "88010-000", "74003-000", "69005-000", "66010-000",
    "79002-000", "78005-000", "29010-000", "64000-000", "65010-000", "57020-000", "59010-000", "58010-000",
    "49010-000", "68900-000", "76801-000", "69301-000", "77001-000", "72800-000", "38400-000", "36010-000",
]


def tenda() -> List[Dict]:
    s = make_session({"Origin": "https://www.tendaatacado.com.br", "Referer": "https://www.tendaatacado.com.br/"})
    rows = []
    for b in get_json(s, "https://api.tendaatacado.com.br/api/public/branch/zip/01001000") or []:
        a = b.get("address") or {}
        rows.append({"market": "tenda", "store_id": str(b.get("id")), "name": b.get("name"), "city": a.get("city"),
                     "state": a.get("state"), "cep": format_zip(a.get("zipCode") or a.get("zip") or ""),
                     "address": a.get("addressLine1"), "note": "branch id -> cookie _Tendaatacado-branchID"})
    return rows


def higas() -> List[Dict]:
    s = make_session()
    d = get_json(s, "https://api.instabuy.com.br/apiv3/store", params={"partner_id": "replicarhigas", "zip_code": "01001000"}) or {}
    rows = []
    for st in d.get("data") or []:
        a = st.get("address") or {}
        rows.append({"market": "higas", "store_id": str(st.get("id")), "name": st.get("name"), "city": a.get("city"),
                     "state": a.get("state"), "cep": format_zip(a.get("zipcode") or ""),
                     "address": ", ".join(str(p) for p in [a.get("street"), a.get("street_number"), a.get("neighborhood")] if p),
                     "note": f"subdomain {st.get('subdomain')}"})
    return rows


def carrefour() -> List[Dict]:
    s = make_session({"Accept": "text/html,*/*"}, json_accept=False)
    r = request_with_retry(s, "POST", "https://mercado.carrefour.com.br/action/stores-from-pickups", data={"city": ""},
                           timeout=30, max_attempts=3)
    stores = (((r.json() or {}).get("result") or {}).get("stores") or []) if r is not None and r.status_code == 200 else []
    rows = []
    for st in stores:
        cep = st.get("cep_clique_retire") or st.get("postal_code") or ""
        rows.append({"market": "carrefour", "store_id": ":".join(p for p in ["carrefour", str(st.get("state") or "").lower(),
                                                                          str(st.get("city") or "").lower().replace(" ", "-"),
                                                                          str(st.get("name") or "").lower().replace(" ", "-"),
                                                                          normalize_zip(cep)] if p),
                     "name": st.get("name"), "city": st.get("city"), "state": st.get("state"), "cep": format_zip(cep),
                     "address": ", ".join(str(p) for p in [st.get("street"), st.get("number"), st.get("neighborhood")] if p),
                     "note": "pickup store; the scraper picks the store whose cep_clique_retire is closest to the CEP"})
    return rows


def atacadao() -> List[Dict]:
    s = make_session({"Referer": "https://www.atacadao.com.br/"})
    found: "OrderedDict[str, Dict]" = OrderedDict()
    for cep in ATACADAO_PROBE_CEPS:
        d = get_json(s, "https://www.atacadao.com.br/api/checkout/pub/regions",
                     params={"postalCode": cep, "country": "BRA"}, max_attempts=2) or []
        for item in d:
            for sel in (item or {}).get("sellers") or []:
                sid = str(sel.get("id") or "")
                if sid.startswith("atacadaobr") and sid not in found:
                    found[sid] = {"market": "atacadao", "store_id": sid, "name": sel.get("name"), "city": "", "state": "",
                                  "cep": cep, "address": "", "note": "seller; CEP = probe CEP that selects it (first candidate)"}
    return list(found.values())


ENUMERATORS = {"tenda": tenda, "higas": higas, "carrefour": carrefour, "atacadao": atacadao}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export every store of the per-store markets with a CEP that selects it")
    parser.add_argument("--markets", nargs="+", default=list(ENUMERATORS), choices=list(ENUMERATORS))
    parser.add_argument("--dir", default="exports")
    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    for m in args.markets:
        try:
            rows = ENUMERATORS[m]()
        except Exception as exc:
            print(f"[{m}] ERROR: {exc}")
            continue
        path = os.path.join(args.dir, f"stores_{m}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        cities = len({(r.get("city") or "").lower() for r in rows if r.get("city")})
        print(f"[{m}] {len(rows)} stores{f' in {cities} cities' if cities else ''} -> {path}")


if __name__ == "__main__":
    main()
