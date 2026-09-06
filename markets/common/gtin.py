"""
gtin.py - Barcode (GTIN/EAN/UPC) validation and canonicalisation.

The barcode is the MAIN KEY of this project: it is the only field that lets us
match the same physical product across different supermarkets. Every barcode
written to the database goes through `normalize_gtin()` so that:

  * only real GTINs survive (check digit verified, mod-10)
  * the same product always gets the same canonical string, whatever the
    source formatting was (leading zeros, GTIN-14 with indicator 0, spaces...)

Canonical form
--------------
  1. keep digits only
  2. strip leading zeros
  3. re-pad to the shortest standard length that fits:
       <= 8 digits  -> GTIN-8   (e.g. Heineken 330ml "78936683")
       <= 12 digits -> UPC-A / GTIN-12
       13 digits    -> EAN-13  (most Brazilian products, "789..." / "790...")
       14 digits    -> GTIN-14 (case/box codes, indicator digit != 0)
  4. verify the check digit; anything that fails is rejected (returns None)

GTIN-8 is only accepted when the caller explicitly allows it (`allow_gtin8`),
because several store APIs put internal 8-digit reference ids in "gtin"-like
fields (Atacadao GraphQL, applay "material"...). A real GTIN-8 has a valid
check digit but so does 1 in 10 random 8-digit ids, so the per-market flag
keeps precision high.
"""

from __future__ import annotations

import re
from typing import Optional

_DIGITS_RE = re.compile(r"\d+")


def check_digit_ok(digits: str) -> bool:
    """Return True if the last digit is the correct GTIN mod-10 check digit."""
    if not digits or not digits.isdigit() or len(digits) < 8:
        return False
    body, check = digits[:-1], int(digits[-1])
    total = 0
    # weights 3,1,3,1... starting from the rightmost body digit
    for idx, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if idx % 2 == 0 else 1)
    return (10 - (total % 10)) % 10 == check


def normalize_gtin(value: object, *, allow_gtin8: bool = False) -> Optional[str]:
    """
    Canonicalise a barcode candidate. Returns the canonical string or None.

    normalize_gtin("7894900011517")                 -> "7894900011517"
    normalize_gtin("07894900011517")                -> "7894900011517"  (GTIN-14, indicator 0)
    normalize_gtin("78936683", allow_gtin8=True)    -> "78936683"
    normalize_gtin("5624968")                       -> None (internal ref id)
    """
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    stripped = digits.lstrip("0")
    if not stripped:
        return None
    n = len(stripped)
    if n > 14:
        return None
    if n <= 8:
        if not allow_gtin8:
            return None
        canon = stripped.rjust(8, "0")
    elif n <= 12:
        canon = stripped.rjust(12, "0")
    else:
        canon = stripped  # 13 or 14 digits
    return canon if check_digit_ok(canon) else None


def first_gtin(*candidates: object, allow_gtin8: bool = False) -> Optional[str]:
    """Return the first candidate that normalises to a valid GTIN."""
    for cand in candidates:
        if cand in (None, "", 0):
            continue
        if isinstance(cand, (list, tuple)):
            found = first_gtin(*cand, allow_gtin8=allow_gtin8)
            if found:
                return found
            continue
        found = normalize_gtin(cand, allow_gtin8=allow_gtin8)
        if found:
            return found
    return None


def gtin_from_text(text: object, *, allow_gtin8: bool = False) -> Optional[str]:
    """
    Extract a GTIN from free text such as an image file name
    ("78936683_1_2_1200_72_RGB.png", "/sku/1000051420/530/7896536500168.png").
    Longest digit runs are tried first.
    """
    if not text:
        return None
    runs = _DIGITS_RE.findall(str(text))
    for run in sorted(runs, key=len, reverse=True):
        if 8 <= len(run) <= 14:
            found = normalize_gtin(run, allow_gtin8=allow_gtin8)
            if found:
                return found
    return None
