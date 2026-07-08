import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ADD_BUTTON_SELECTORS: List[str] = [
    "button:has-text('Adicionar ao carrinho')",
    "button:has-text('Adicionar')",
    "button:has-text('ADICIONAR')",
    "button:has-text('Comprar')",
]

PLUS_BUTTON_SELECTORS: List[str] = [
    "button[aria-label*='Aumentar']",
    "button[aria-label*='aumentar']",
    "button:has-text('+')",
]


def _load_actions(actions_path: Path) -> List[Dict[str, Any]]:
    if not actions_path.exists():
        raise FileNotFoundError(f"Swift actions file not found: {actions_path}")

    payload = json.loads(actions_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Swift actions file must contain a JSON array.")

    actions: List[Dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        url = str(row.get("product_url") or "").strip()
        if not url:
            continue
        quantity_raw = row.get("quantity", 1)
        try:
            quantity = max(1, int(float(quantity_raw)))
        except (TypeError, ValueError):
            quantity = 1

        actions.append(
            {
                "item_name": str(row.get("item_name") or "").strip() or "item",
                "product_url": url,
                "quantity": quantity,
            }
        )

    if not actions:
        raise ValueError("Swift actions file has no valid product_url entries.")
    return actions


def _click_first_visible(page, selectors: List[str], timeout_ms: int = 2500) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _add_quantity_for_item(page, quantity: int) -> Tuple[bool, str]:
    # First unit: add-to-cart action on product page.
    added = _click_first_visible(page, ADD_BUTTON_SELECTORS, timeout_ms=6000)
    if not added:
        return False, "add_button_not_found"

    if quantity <= 1:
        return True, "added_1"

    # Extra units: try to use + quantity control if present.
    plus_successes = 0
    for _ in range(quantity - 1):
        clicked_plus = _click_first_visible(page, PLUS_BUTTON_SELECTORS, timeout_ms=1200)
        if clicked_plus:
            plus_successes += 1
            time.sleep(0.2)
            continue

        # Fallback: repeat add-to-cart click when plus control is unavailable.
        clicked_add_again = _click_first_visible(page, ADD_BUTTON_SELECTORS, timeout_ms=1200)
        if clicked_add_again:
            plus_successes += 1
            time.sleep(0.2)
            continue

        return False, f"quantity_incomplete_{plus_successes + 1}_of_{quantity}"

    return True, f"added_{quantity}"


def run_swift_cart_automation(
    actions_path: str = "swift_cart_actions.json",
    headless: bool = False,
    slow_mo_ms: int = 120,
) -> Dict[str, Any]:
    actions_file = Path(actions_path)
    actions = _load_actions(actions_file)

    report: Dict[str, Any] = {
        "actions_file": str(actions_file),
        "processed": 0,
        "success": 0,
        "failed": 0,
        "items": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=max(0, slow_mo_ms))
        context = browser.new_context()

        # Login handoff: user logs in once, then automation continues.
        bootstrap = context.new_page()
        bootstrap.goto("https://www.swift.com.br", wait_until="domcontentloaded", timeout=45000)
        print("Swift cart automation: browser opened at swift.com.br")
        print("Please login (and confirm your CEP/store) in the opened browser tab.")
        input("After login is done, press Enter here to continue adding products... ")

        for action in actions:
            item_name = action["item_name"]
            product_url = action["product_url"]
            quantity = action["quantity"]
            report["processed"] += 1

            status = "failed"
            detail = "unknown_error"
            try:
                page = context.new_page()
                page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(0.6)
                ok, detail = _add_quantity_for_item(page, quantity)
                status = "ok" if ok else "failed"
                if ok:
                    report["success"] += 1
                else:
                    report["failed"] += 1
                page.close()
            except PlaywrightTimeoutError:
                detail = "navigation_timeout"
                report["failed"] += 1
            except Exception as exc:
                detail = f"exception:{exc}"
                report["failed"] += 1

            report["items"].append(
                {
                    "item_name": item_name,
                    "product_url": product_url,
                    "quantity": quantity,
                    "status": status,
                    "detail": detail,
                }
            )
            print(f"- {item_name}: {status} ({detail})")

        print("\nSwift automation finished.")
        print(f"Processed: {report['processed']} | Success: {report['success']} | Failed: {report['failed']}")
        print("Leave browser open to review cart, then close it manually when done.")
        input("Press Enter to close the automated browser session... ")
        browser.close()

    report_path = actions_file.with_name("swift_cart_automation_report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Automation report written to: {report_path}")
    return report
