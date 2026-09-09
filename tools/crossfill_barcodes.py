"""
crossfill_barcodes.py - Deterministic cross-market barcode fill (no AI).

Some markets never expose a barcode for part of their catalogue (Nagumo: none
at all since 2026, Higas after its API change, fresh produce everywhere). This
tool reuses what the other markets already know, in two passes:

  1. exact  - normalised product name equality               -> barcode_source = 'crossfill'
  2. tokens - order-independent token set, units normalised
              ("395 G" = "395g", "1,5L" = "1.5l"), filler words
              dropped, a SIZE TOKEN REQUIRED                  -> barcode_source = 'crossfill-tokens'
  3. --fuzzy T  best Jaccard similarity between token sets that share the same
              size token, accepted when >= T. Measured on Nagumo 2026-09-06:
              T=0.8 -> +1,153 barcodes, samples all correct; T=0.7 -> wrong
              matches appear. Use 0.8.                       -> barcode_source = 'crossfill-jaccard'

Both passes only use names that map to exactly ONE barcode across all markets
(ambiguous names are dropped) and need >= 3 tokens. No fuzzy matching, no
model; seconds to run; auditable and reversible with one statement:

    UPDATE offers SET barcode = NULL, barcode_source = NULL WHERE barcode_source LIKE 'crossfill%';

Usage:
    python -m tools.crossfill_barcodes                       # targets: every market
    python -m tools.crossfill_barcodes --targets nagumo higas
    python -m tools.crossfill_barcodes --dry-run --show 30   # audit the token matches first
    python -m tools.crossfill_barcodes --fuzzy 0.8 --targets nagumo --dry-run --show 40   # audit pass 3
    python -m tools.crossfill_barcodes --fuzzy 0.8 --targets nagumo                      # apply pass 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from collections import defaultdict
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db.db_manager import StoreDB, load_env  # noqa: E402
from markets.common.offer import norm_name  # noqa: E402

MIN_TOKENS = 3  # "leite integral 1l" ok, "banana kg" too short to trust
STOPWORDS = {"de", "da", "do", "das", "dos", "e", "com", "c", "p", "em", "a", "o", "para", "un", "unidade",
             "pacote", "caixa", "garrafa", "lata", "pote", "frasco", "sache", "tradicional", "original"}
_SIZE_RE = re.compile(r"^\d+(\.\d+)?(kg|g|mg|ml|l|un|und)$")


def token_key(name: object) -> Optional[FrozenSet[str]]:
    """
    Order-independent key for the token pass. Returns None unless the name has
    >= 3 tokens AND an explicit size token, so "Molho de tomate Heinz" can never
    match a specific 240g / 340g product.
    """
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"(\d)[,](\d)", r"\1.\2", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*(kg|g|mg|ml|l|un|und|unid)\b", r"\1\2", s)
    toks = [t for t in re.sub(r"[^a-z0-9.]+", " ", s).split() if t not in STOPWORDS]
    if len(toks) < MIN_TOKENS or not any(_SIZE_RE.match(t) for t in toks):
        return None
    return frozenset(toks)


def size_token(tk: FrozenSet[str]) -> Optional[str]:
    for t in tk:
        if _SIZE_RE.match(t):
            return t
    return None


def build_reference(stores: List[str]) -> Tuple[Dict[str, str], Dict[FrozenSet[str], str]]:
    names: Dict[str, Set[str]] = defaultdict(set)
    tokens: Dict[FrozenSet[str], Set[str]] = defaultdict(set)
    for key in stores:
        if not os.environ.get(str(config.STORES[key]["db_env"])):
            continue
        try:
            db = StoreDB(key)
            with db._conn.cursor() as cur:
                cur.execute("SELECT product_name, barcode FROM offers WHERE barcode IS NOT NULL "
                            "AND barcode_source NOT LIKE 'crossfill%'")
                rows = cur.fetchall()
            db.close()
        except Exception as exc:
            print(f"  [{key}] skipped: {exc}")
            continue
        for name, barcode in rows:
            n = norm_name(name)
            if n and len(n.split()) >= MIN_TOKENS:
                names[n].add(barcode)
            tk = token_key(name)
            if tk:
                tokens[tk].add(barcode)
        print(f"  [{key}] {len(rows):,} barcoded names read")
    ref = {n: next(iter(b)) for n, b in names.items() if len(b) == 1}
    tref = {t: next(iter(b)) for t, b in tokens.items() if len(b) == 1}
    print(f"reference: {len(ref):,} exact names, {len(tref):,} token keys "
          f"({len(names) - len(ref):,} / {len(tokens) - len(tref):,} ambiguous dropped)")
    return ref, tref


def fill(targets: List[str], ref: Dict[str, str], tref: Dict[FrozenSet[str], str],
         dry_run: bool, show: int = 0, fuzzy: float = 0.0) -> int:
    total = 0
    # optional third pass: best Jaccard similarity of token sets sharing the SAME size token
    by_size: Dict[str, List[Tuple[FrozenSet[str], str]]] = defaultdict(list)
    if fuzzy:
        for tk, bc in tref.items():
            sz = size_token(tk)
            if sz:
                by_size[sz].append((tk, bc))
    for key in targets:
        if not os.environ.get(str(config.STORES[key]["db_env"])):
            continue
        try:
            db = StoreDB(key)
            missing = db.load_missing_barcodes(retry_not_found_days=0, need_url=False)
            updates: List[Tuple[str, str, str, str]] = []
            exact = 0
            for store_id, product_id, _url, name in missing:
                b = ref.get(norm_name(name))
                source = "crossfill"
                if b:
                    exact += 1
                else:
                    tk = token_key(name)
                    b = tref.get(tk) if tk else None
                    source = "crossfill-tokens"
                    if not b and fuzzy and tk:
                        best, best_bc = 0.0, None
                        for rt, rbc in by_size.get(size_token(tk) or "", []):
                            j = len(tk & rt) / len(tk | rt)
                            if j > best:
                                best, best_bc = j, rbc
                        if best_bc and best >= fuzzy:
                            b, source = best_bc, "crossfill-jaccard"
                if b:
                    updates.append((store_id, product_id, b, source))
                    if show > 0 and source != "crossfill":
                        print(f"      {source:<17} {str(name)[:60]:<60} -> {b}")
                        show -= 1
            n = 0 if dry_run else db.update_barcodes(updates)
            db.close()
        except Exception as exc:
            print(f"  [{key}] error: {exc}")
            continue
        n_j = sum(1 for u in updates if u[3] == "crossfill-jaccard")
        print(f"  [{key}] missing={len(missing):,} matched={len(updates):,} "
              f"(exact {exact:,}, tokens {len(updates) - exact - n_j:,}, jaccard {n_j:,}) written={n:,}")
        total += n
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing barcodes by name match across markets")
    parser.add_argument("--targets", nargs="+", default=None, choices=list(config.STORES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=0, help="Print the first N token/jaccard matches (audit)")
    parser.add_argument("--fuzzy", type=float, default=0.0,
                        help="Third pass: best Jaccard similarity of token sets with the same size token, "
                             "accepted when >= this value (0.8 measured safe; 0.7 is NOT). Off by default.")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    load_env(args.env)
    print("Building reference from all markets ...")
    ref, tref = build_reference(list(config.STORES))
    targets = args.targets or list(config.STORES)
    print(f"\nFilling {'(dry run) ' if args.dry_run else ''}targets: {', '.join(targets)}")
    total = fill(targets, ref, tref, args.dry_run, show=args.show, fuzzy=args.fuzzy)
    print(f"\nDone: {total:,} barcodes written")


if __name__ == "__main__":
    main()
