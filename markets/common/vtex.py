"""
vtex.py - Shared client for VTEX "catalog_system" public APIs.

Used by: swift, oba, samsclub, giga (listing + EAN inline) and atacadao
(EAN lookup by productId). VTEX stores expose, with no auth at all:

  GET /api/catalog_system/pub/category/tree/{depth}
  GET /api/catalog_system/pub/products/search?fq=C:/{path}/&_from=0&_to=49
        -> 50 products/page, header `resources: 0-49/1234` gives the total.
        -> HARD CAP: _from > 2500 returns an error. Any category with more
           than ~2500 products must be split into its children (with the
           FULL ancestor path, e.g. C:/6/42/ not C:/42/).
  GET /api/catalog_system/pub/products/search?fq=productId:123&fq=productId:456
        -> batch lookup (repeat fq); we use 50 ids per call for EAN backfill.

Product shape: items[0].ean is the barcode, items[0].sellers[0].commertialOffer
has ListPrice/Price/AvailableQuantity/IsAvailable, PromotionTeasers/Teasers
carry promo names ("Leve 3 pague 2"), images[0].imageUrl is the picture.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from .gtin import first_gtin
from .http import decode_json, request_with_retry
from .offer import make_offer, parse_brl

PAGE_SIZE = 50
VTEX_MAX_FROM = 2450          # last safe _from (fetches 2450..2499)
VTEX_MAX_TOTAL = VTEX_MAX_FROM + PAGE_SIZE


class VtexCatalog:
    def __init__(self, session: requests.Session, base_url: str, *, api_prefix: str = "/api",
                 delay: float = 0.1, extra_params: Optional[Dict[str, Any]] = None, log_prefix: str = ""):
        self.session = session
        self.base = base_url.rstrip("/")
        self.prefix = api_prefix
        self.delay = delay
        self.extra = dict(extra_params or {})
        self.log = log_prefix

    # ------------------------------------------------------------------ tree
    def category_tree(self, depth: int = 3) -> List[Dict[str, Any]]:
        for attempt in range(5):
            resp = request_with_retry(self.session, "GET",
                                      f"{self.base}{self.prefix}/catalog_system/pub/category/tree/{depth}",
                                      params=self.extra or None, timeout=25, log_prefix=self.log)
            data = decode_json(resp)
            if isinstance(data, list):
                return data
            time.sleep(3 * (attempt + 1))
        return []

    def flat_nodes(self, depth: int = 3) -> List[Dict[str, Any]]:
        """Flatten the tree: [{id, name, path '7/59/503', fq 'C:/7/59/503/', is_leaf, url}]"""
        out: List[Dict[str, Any]] = []

        def walk(nodes: List[Dict[str, Any]], id_path: str, name_path: str) -> None:
            for node in nodes or []:
                nid = node.get("id") or node.get("Id")
                if nid is None:
                    continue
                name = str(node.get("name") or node.get("Name") or "").strip()
                new_path = f"{id_path}/{nid}" if id_path else str(nid)
                new_names = f"{name_path}/{name}" if name_path else name
                children = node.get("children") or []
                out.append({
                    "id": nid, "name": name, "path": new_path, "name_path": new_names,
                    "fq": f"C:/{new_path}/", "is_leaf": not children,
                    "url": str(node.get("url") or ""), "children": children,
                })
                if children:
                    walk(children, new_path, new_names)

        walk(self.category_tree(depth), "", "")
        return out

    # ------------------------------------------------------------------ search
    def search_page(self, fq: Any, from_: int, to_: int, *, path: str = "") -> Tuple[List[Dict[str, Any]], int, int]:
        """Return (products, total, status). `fq` may be a str or a list of str.
        `path` appends a category path to the search URL (/products/search/bebidas/cervejas)."""
        params: List[Tuple[str, Any]] = []
        if isinstance(fq, (list, tuple)):
            params.extend(("fq", f) for f in fq)
        elif fq:
            params.append(("fq", fq))
        params.append(("_from", from_))
        params.append(("_to", to_))
        params.extend(self.extra.items())
        url = f"{self.base}{self.prefix}/catalog_system/pub/products/search"
        if path:
            url = f"{url}/{path.strip('/')}"
        resp = request_with_retry(self.session, "GET", url, params=params, timeout=40, log_prefix=self.log)
        if resp is None:
            return [], 0, 0
        total = 0
        res = resp.headers.get("resources", "")
        if "/" in res:
            try:
                total = int(res.split("/")[1])
            except ValueError:
                total = 0
        data = decode_json(resp)
        return (data if isinstance(data, list) else []), total, resp.status_code

    def count(self, fq: str) -> int:
        _, total, _ = self.search_page(fq, 0, 0)
        return total

    def iter_products(self, fq: Any, *, limit: Optional[int] = None, label: str = "",
                      path: str = "") -> Iterator[Dict[str, Any]]:
        """Paginate one fq filter (or category path) up to the VTEX cap."""
        from_ = 0
        seen = 0
        while from_ <= VTEX_MAX_FROM:
            page, total, status = self.search_page(fq, from_, from_ + PAGE_SIZE - 1, path=path)
            if not page:
                break
            for p in page:
                yield p
                seen += 1
                if limit and seen >= limit:
                    return
            if len(page) < PAGE_SIZE or (total and from_ + len(page) >= total):
                break
            from_ += PAGE_SIZE
            if self.delay:
                time.sleep(self.delay)
        if from_ > VTEX_MAX_FROM:
            print(f"{self.log}WARNING: {label or fq} exceeds the VTEX 2500 cap - split into sub-categories")

    def segments(self, depth: int = 3, *, min_split: int = VTEX_MAX_TOTAL) -> List[Tuple[str, str]]:
        """
        Build (fq, label) segments that each stay below the 2500 cap by
        descending into children only where needed (fewer requests than
        scraping every leaf).
        """
        out: List[Tuple[str, str]] = []

        def visit(node: Dict[str, Any], id_path: str, name_path: str) -> None:
            nid = node.get("id") or node.get("Id")
            if nid is None:
                return
            name = str(node.get("name") or node.get("Name") or str(nid)).strip()
            path = f"{id_path}/{nid}" if id_path else str(nid)
            label = f"{name_path}/{name}" if name_path else name
            fq = f"C:/{path}/"
            total = self.count(fq)
            children = node.get("children") or []
            if total <= min_split or not children:
                if total > 0:
                    out.append((fq, label))
                if total > min_split:
                    print(f"{self.log}WARNING: {label} has {total} products and no children - capped at {VTEX_MAX_TOTAL}")
                return
            for child in children:
                visit(child, path, label)

        for root in self.category_tree(depth):
            visit(root, "", "")
        return out

    def lookup_by_product_ids(self, product_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch products by productId (max ~50 per call). Returns {productId: product}."""
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(product_ids), 50):
            batch = [f"productId:{pid}" for pid in product_ids[i:i + 50]]
            page, _, _ = self.search_page(batch, 0, 49)
            for p in page:
                pid = str(p.get("productId") or "")
                if pid:
                    out[pid] = p
            if self.delay:
                time.sleep(self.delay)
        return out


# ---------------------------------------------------------------------------
# Standardise a VTEX product into the common offer dict
# ---------------------------------------------------------------------------

_MEMBERSHIP_WORDS = ("socio", "sócio", "clube", "club", "member", "app ")


def _teasers(co: Dict[str, Any]) -> List[Dict[str, Any]]:
    promos = co.get("PromotionTeasers") or []
    if promos:
        return [p for p in promos if isinstance(p, dict)]
    out = []
    for t in co.get("Teasers") or []:
        if not isinstance(t, dict):
            continue
        name = t.get("Name") or t.get("<Name>k__BackingField") or ""
        cond = t.get("Conditions") or t.get("<Conditions>k__BackingField") or {}
        min_qty = cond.get("MinimumQuantity") or cond.get("<MinimumQuantity>k__BackingField")
        out.append({"Name": name, "Conditions": {"MinimumQuantity": min_qty}})
    return out


def vtex_offer(product: Dict[str, Any], *, store_id: str, base_url: str,
               allow_gtin8: bool = True, category_label: Optional[str] = None,
               url_suffix: str = "/p") -> Optional[Dict[str, Any]]:
    """Convert one VTEX catalog product into the common offer dict."""
    name = product.get("productName") or product.get("name")
    pid = product.get("productId")
    items = product.get("items") or []
    item0 = items[0] if items and isinstance(items[0], dict) else {}
    sellers = item0.get("sellers") or []
    seller = next((s for s in sellers if isinstance(s, dict) and s.get("sellerDefault")), None) or \
        (sellers[0] if sellers and isinstance(sellers[0], dict) else {})
    co = seller.get("commertialOffer") or {}

    list_price = parse_brl(co.get("ListPrice"))
    price = parse_brl(co.get("Price"))
    regular, promo = (list_price, price) if list_price else (price, None)

    ref_ids = [r.get("Value") for r in (item0.get("referenceId") or []) if isinstance(r, dict)]
    barcode = first_gtin(item0.get("ean"), allow_gtin8=allow_gtin8) or \
        first_gtin(*ref_ids, allow_gtin8=False)
    barcode_source = "inline" if barcode else None

    teasers = _teasers(co)
    offer_name = None
    min_qty = None
    membership = False
    if teasers:
        offer_name = (teasers[0].get("Name") or "").strip() or None
        cond = teasers[0].get("Conditions") or {}
        try:
            mq = int(cond.get("MinimumQuantity") or 0)
        except (TypeError, ValueError):
            mq = 0
        min_qty = mq if mq > 1 else None
        joined = " ".join((t.get("Name") or "").lower() for t in teasers)
        membership = any(w in joined for w in _MEMBERSHIP_WORDS)
    clusters = product.get("productClusters") or {}
    offer_tag = " | ".join(str(v).strip() for v in clusters.values() if str(v).strip()) or None

    images = item0.get("images") or []
    image_url = images[0].get("imageUrl") if images and isinstance(images[0], dict) else None
    cats = product.get("categories") or []
    category = cats[0].strip("/") if cats else category_label
    link_text = product.get("linkText") or ""
    product_url = f"{base_url.rstrip('/')}/{link_text}{url_suffix}" if link_text else product.get("link")

    unit = item0.get("measurementUnit")
    mult = item0.get("unitMultiplier")
    if unit and mult not in (None, "", 1, 1.0):
        unit = f"{mult}{unit}"

    avail = co.get("IsAvailable")
    qty = co.get("AvailableQuantity")
    return make_offer(
        product_id=pid, store_id=store_id, product_name=name,
        regular_price=regular, promo_price=promo,
        barcode=barcode, barcode_source=barcode_source,
        brand=product.get("brand"), category_path=category,
        promo_min_quantity=min_qty, offer_name=offer_name, offer_tag=offer_tag,
        app_membership_required=membership if teasers else None,
        unit=unit, is_available=bool(avail) if avail is not None else None,
        stock=qty, product_url=product_url, image_url=image_url,
    )
