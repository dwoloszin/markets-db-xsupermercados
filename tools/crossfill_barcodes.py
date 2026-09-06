"""
crossfill_barcodes.py - Deterministic cross-market barcode fill (no AI).

Some markets never expose a barcode for part of their catalogue (Nagumo ~86%,
Higas after the API change, fresh produce everywhere). This tool reuses what the
other markets already know:

  1. read (normalised product name -> barcode) from every market that has barcodes
  2. keep only UNAMBIGUOUS names (the same name never maps to two barcodes)
  3. fill rows whose barcode is NULL when their normalised name matches exactly

Exact full-name equality across retailers is strict enough to be safe; there is
no fuzzy matching and no model, so it runs in seconds. Rows get
barcode_source = 'crossfill' so they can be audited or reverted:

    UPDATE offers SET barcode = NULL, barcode_source = NULL WHERE barcode_source = 'crossfill';

Usage:
    python -m tools.crossfill_barcodes                 # targets: every market
    python -m tools.crossfill_barcodes --targets nagumo higas
    python -m tools.crossfill_barcodes --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db.db_manager import StoreDB, load_env  # noqa: E402
from markets.common.offer import norm_name  # noqa: E402

MIN_TOKENS = 3  # "leite integral 1l" ok, "banana kg" too short to trust


def build_reference(stores: List[str]) -> Dict[str, str]:
    names: Dict[str, Set[str]] = defaultdict(set)
    for key in stores:
        if not os.environ.get(str(config.STORES[key]["db_env"])):
            continue
        try:
            db = StoreDB(key)
            with db._conn.cursor() as cur:
                cur.execute("SELECT product_name, barcode FROM offers WHERE barcode IS NOT NULL "
                            "AND barcode_source IS DISTINCT FROM 'crossfill'")
                rows = cur.fetchall()
            db.close()
        except Exception as exc:
            print(f"  [{key}] skipped: {exc}")
            continue
        for name, barcode in rows:
            n = norm_name(name)
            if n and len(n.split()) >= MIN_TOKENS:
                names[n].add(barcode)
        print(f"  [{key}] {len(rows):,} barcoded names read")
    ref = {n: next(iter(b)) for n, b in names.items() if len(b) == 1}
    print(f"reference: {len(ref):,} unambiguous names ({len(names) - len(ref):,} ambiguous dropped)")
    return ref


def fill(targets: List[str], ref: Dict[str, str], dry_run: bool) -> int:
    total = 0
    for key in targets:
        if not os.environ.get(str(config.STORES[key]["db_env"])):
            continue
        try:
            db = StoreDB(key)
            missing = db.load_missing_barcodes(retry_not_found_days=0, need_url=False)
            updates: List[Tuple[str, str, str, str]] = []
            for store_id, product_id, _url, name in missing:
                b = ref.get(norm_name(name))
                if b:
                    updates.append((store_id, product_id, b, "crossfill"))
            n = 0 if dry_run else db.update_barcodes(updates)
            db.close()
        except Exception as exc:
            print(f"  [{key}] error: {exc}")
            continue
        print(f"  [{key}] missing={len(missing):,} matched={len(updates):,} written={n:,}")
        total += n
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing barcodes by exact name match across markets")
    parser.add_argument("--targets", nargs="+", default=None, choices=list(config.STORES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    load_env(args.env)
    print("Building reference from all markets ...")
    ref = build_reference(list(config.STORES))
    targets = args.targets or list(config.STORES)
    print(f"\nFilling {'(dry run) ' if args.dry_run else ''}targets: {', '.join(targets)}")
    total = fill(targets, ref, args.dry_run)
    print(f"\nDone: {total:,} barcodes written")


if __name__ == "__main__":
    main()
