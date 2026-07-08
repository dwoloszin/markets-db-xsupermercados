import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from db.db_manager import DatabaseManager


DEFAULT_MARKETS: List[str] = [
    "Rossi",
    "Atacadão",
    "Nagumo",
    "Higas",
    "Swift",
    "Sonda Delivery",
    "XSupermercados",
    "Barbosa",
    "Carrefour",
    "Oba Hortifruti",
    "Extra",
    "Pão de Açúcar",
    "Tenda Atacado",
]


@dataclass
class ShoppingItem:
    name: str
    quantity: float = 1.0
    brand: Optional[str] = None
    max_price: Optional[float] = None


def _normalize_text(text: Optional[str]) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    no_accents = unicodedata.normalize("NFKD", raw)
    no_accents = "".join(ch for ch in no_accents if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", no_accents).strip()


def _token_set(text: Optional[str]) -> set:
    return {tok for tok in _normalize_text(text).split() if len(tok) >= 2}


def _safe_price(offer: Dict[str, Any]) -> Optional[float]:
    promo = offer.get("promo_price")
    regular = offer.get("regular_price")
    for value in (promo, regular):
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _score_offer(item: ShoppingItem, offer: Dict[str, Any]) -> float:
    target_tokens = _token_set(item.name)
    candidate_name = f"{offer.get('product_name') or ''} {offer.get('description') or ''}"
    offer_tokens = _token_set(candidate_name)

    if not target_tokens or not offer_tokens:
        base = 0.0
    else:
        overlap = len(target_tokens.intersection(offer_tokens))
        base = overlap / max(len(target_tokens), 1)

    brand_bonus = 0.0
    if item.brand:
        target_brand = _normalize_text(item.brand)
        offer_brand = _normalize_text(offer.get("brand"))
        if target_brand and offer_brand and target_brand in offer_brand:
            brand_bonus = 0.2

    price_bonus = 0.0
    price = _safe_price(offer)
    if price is not None:
        if item.max_price and item.max_price > 0:
            if price <= item.max_price:
                # Better bonus for lower price under threshold.
                price_bonus = min((item.max_price - price) / item.max_price, 0.15)
            else:
                price_bonus = -0.25
        else:
            price_bonus = 0.05

    return base + brand_bonus + price_bonus


def _parse_item(raw: Dict[str, Any], index: int) -> ShoppingItem:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"shopping_list item {index} is missing required field 'name'.")

    quantity = raw.get("quantity", 1)
    try:
        quantity_value = float(quantity)
    except (TypeError, ValueError):
        quantity_value = 1.0
    if quantity_value <= 0:
        quantity_value = 1.0

    brand = raw.get("brand")
    max_price = raw.get("max_price")
    max_price_value = None
    if max_price is not None and str(max_price).strip() != "":
        try:
            max_price_value = float(max_price)
        except (TypeError, ValueError):
            max_price_value = None

    return ShoppingItem(
        name=name,
        quantity=quantity_value,
        brand=str(brand).strip() if brand else None,
        max_price=max_price_value,
    )


def load_shopping_list(input_path: Path) -> List[ShoppingItem]:
    if not input_path.exists():
        raise FileNotFoundError(f"Shopping list file not found: {input_path}")

    payload = json.loads(input_path.read_text(encoding="utf-8"))

    if isinstance(payload, dict):
        items = payload.get("items", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Shopping list JSON must be an array or an object with 'items'.")

    parsed_items: List[ShoppingItem] = []
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"shopping_list item {idx} must be an object.")
        parsed_items.append(_parse_item(raw, idx))

    if not parsed_items:
        raise ValueError("Shopping list is empty.")
    return parsed_items


def _match_item_in_market(
    db: DatabaseManager,
    item: ShoppingItem,
    market_name: str,
    min_score: float,
    search_limit: int,
    item_position: Optional[int] = None,
    total_items: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if item_position is not None and total_items is not None:
        print(
            f"[{market_name}] loading... waitingggg (item {item_position}/{total_items}) "
            f"fetching '{item.name}' from DB"
        )
    else:
        print(f"[{market_name}] loading... waitingggg fetching '{item.name}' from DB")

    candidates = db.query_offers(
        market_name=market_name,
        search_text=item.name,
        only_with_barcode=False,
        limit=search_limit,
    )

    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    for offer in candidates:
        score = _score_offer(item, offer)
        if score > best_score:
            best_score = score
            best = offer

    if not best or best_score < min_score:
        return None

    price = _safe_price(best)
    return {
        "score": round(best_score, 4),
        "offer_id": best.get("id"),
        "product_name": best.get("product_name"),
        "brand": best.get("brand"),
        "unit": best.get("unit"),
        "product_url": best.get("product_url"),
        "store_id": best.get("store_id"),
        "price": price,
    }


def _write_market_csv(output_file: Path, rows: Sequence[Dict[str, Any]]) -> None:
    fieldnames = [
        "item_name",
        "quantity",
        "brand_preference",
        "max_price",
        "matched",
        "matched_product_name",
        "price",
        "estimated_line_total",
        "product_url",
        "offer_id",
        "score",
    ]
    with output_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _safe_market_slug(market_name: str) -> str:
    return _normalize_text(market_name).replace(" ", "_") or "market"


def _write_swift_quick_buy_files(output_dir: Path, market_rows: Sequence[Dict[str, Any]]) -> List[str]:
    swift_actions: List[Dict[str, Any]] = []
    for row in market_rows:
        if row.get("matched") != "yes":
            continue
        url = str(row.get("product_url") or "").strip()
        if not url:
            continue
        quantity_raw = row.get("quantity")
        try:
            quantity = max(1, int(float(quantity_raw)))
        except (TypeError, ValueError):
            quantity = 1
        swift_actions.append(
            {
                "item_name": row.get("item_name"),
                "matched_product_name": row.get("matched_product_name"),
                "quantity": quantity,
                "product_url": url,
            }
        )

    if not swift_actions:
        return []

    artifacts: List[str] = []

    actions_path = output_dir / "swift_cart_actions.json"
    actions_path.write_text(json.dumps(swift_actions, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(actions_path.name)

    links_path = output_dir / "swift_quick_buy_links.txt"
    lines = ["# Open each product and click 'Adicionar' quantity times", "# item_name | quantity | product_url"]
    for action in swift_actions:
        lines.append(f"{action['item_name']} | {action['quantity']} | {action['product_url']}")
    links_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts.append(links_path.name)

    script_path = output_dir / "swift_open_tabs.ps1"
    script_lines = [
        "$items = Get-Content -Raw -Path \"swift_cart_actions.json\" | ConvertFrom-Json",
        "foreach ($item in $items) {",
        "  Start-Process $item.product_url",
        "  Start-Sleep -Milliseconds 350",
        "}",
    ]
    script_path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    artifacts.append(script_path.name)

    auto_script_path = output_dir / "swift_auto_cart.ps1"
    auto_script_lines = [
        "$actionsPath = Join-Path $PSScriptRoot 'swift_cart_actions.json'",
        "python main.py swift_cart_auto $actionsPath",
    ]
    auto_script_path.write_text("\n".join(auto_script_lines) + "\n", encoding="utf-8")
    artifacts.append(auto_script_path.name)

    return artifacts


def build_store_carts(
    db: DatabaseManager,
    items: Sequence[ShoppingItem],
    output_dir: Path,
    markets: Optional[Sequence[str]] = None,
    min_score: float = 0.55,
    search_limit: int = 120,
) -> Dict[str, Any]:
    selected_markets = [m for m in (markets or DEFAULT_MARKETS) if str(m or "").strip()]
    if not selected_markets:
        raise ValueError("No markets selected for cart build.")

    output_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "markets": {},
        "unmatched_items": [],
    }
    matched_by_item: Dict[str, bool] = {item.name: False for item in items}

    for market_name in selected_markets:
        market_rows: List[Dict[str, Any]] = []
        matched_count = 0
        market_total = 0.0

        print(f"[{market_name}] loading... waitingggg preparing market cart from DB data")

        for idx, item in enumerate(items, start=1):
            match = _match_item_in_market(
                db=db,
                item=item,
                market_name=market_name,
                min_score=min_score,
                search_limit=search_limit,
                item_position=idx,
                total_items=len(items),
            )
            matched = bool(match)
            if matched:
                matched_count += 1
                matched_by_item[item.name] = True
            price = match.get("price") if match else None
            line_total = (price * item.quantity) if (price is not None) else None
            if line_total is not None:
                market_total += line_total

            market_rows.append(
                {
                    "item_name": item.name,
                    "quantity": item.quantity,
                    "brand_preference": item.brand or "",
                    "max_price": item.max_price if item.max_price is not None else "",
                    "matched": "yes" if matched else "no",
                    "matched_product_name": match.get("product_name") if match else "",
                    "price": round(price, 2) if isinstance(price, (int, float)) else "",
                    "estimated_line_total": round(line_total, 2) if isinstance(line_total, (int, float)) else "",
                    "product_url": match.get("product_url") if match else "",
                    "offer_id": match.get("offer_id") if match else "",
                    "score": match.get("score") if match else "",
                }
            )

        safe_market = _safe_market_slug(market_name)
        csv_path = output_dir / f"cart_{safe_market}.csv"
        _write_market_csv(csv_path, market_rows)

        extra_artifacts: List[str] = []
        if market_name == "Swift":
            extra_artifacts = _write_swift_quick_buy_files(output_dir, market_rows)

        summary["markets"][market_name] = {
            "items_requested": len(items),
            "items_matched": matched_count,
            "coverage": round((matched_count / len(items)) * 100, 2) if items else 0.0,
            "estimated_total": round(market_total, 2),
            "cart_csv": str(csv_path.name),
            "extra_artifacts": extra_artifacts,
        }

    for item in items:
        if not matched_by_item.get(item.name):
            summary["unmatched_items"].append(item.name)

    summary_path = output_dir / "cart_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
