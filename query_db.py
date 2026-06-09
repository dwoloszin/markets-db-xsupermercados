import argparse
import json

from db.db_manager import DatabaseManager


def main():
    parser = argparse.ArgumentParser(description="Query offers data from configured DB (Postgres or SQLite)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary", help="Show summary by market")
    p_summary.add_argument("--json", action="store_true", help="Output as JSON")

    p_market = sub.add_parser("market", help="List latest offers from one market")
    p_market.add_argument("market_name", help="Market name, e.g. Rossi or Atacadão")
    p_market.add_argument("--limit", type=int, default=20)
    p_market.add_argument("--json", action="store_true", help="Output as JSON")

    p_search = sub.add_parser("search", help="Search offers by product or brand")
    p_search.add_argument("text", help="Search text")
    p_search.add_argument("--market", dest="market_name", default=None)
    p_search.add_argument("--barcode-only", action="store_true")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true", help="Output as JSON")

    p_offer = sub.add_parser("offer", help="Get one offer by id")
    p_offer.add_argument("offer_id", help="Offer id, e.g. rossi_123")
    p_offer.add_argument("--json", action="store_true", help="Output as JSON")

    p_store_pricing = sub.add_parser("store_pricing", help="Show store-level historical pricing insights")
    p_store_pricing.add_argument("market_name", help="Market name, e.g. Rossi or Atacadão")
    p_store_pricing.add_argument("--store-id", default=None)
    p_store_pricing.add_argument("--limit", type=int, default=20)
    p_store_pricing.add_argument("--json", action="store_true", help="Output as JSON")

    p_product_pattern = sub.add_parser("product_pattern", help="Show product price patterns and predictions")
    p_product_pattern.add_argument("market_name", help="Market name, e.g. Rossi or Atacadão")
    p_product_pattern.add_argument("--store-id", default=None)
    p_product_pattern.add_argument("--text", default=None, help="Filter by product name")
    p_product_pattern.add_argument("--limit", type=int, default=20)
    p_product_pattern.add_argument("--json", action="store_true", help="Output as JSON")

    p_sql = sub.add_parser("sql", help="Run SELECT SQL query")
    p_sql.add_argument("query", help="SELECT query only")
    p_sql.add_argument("--limit", type=int, default=100)
    p_sql.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    db = DatabaseManager()

    if args.command == "summary":
        print("loading... waitingggg getting summary from DB")
        result = db.get_summary()
    elif args.command == "market":
        print(f"loading... waitingggg fetching market '{args.market_name}' from DB")
        result = db.query_offers(market_name=args.market_name, limit=args.limit)
    elif args.command == "search":
        print("loading... waitingggg searching offers in DB")
        result = db.query_offers(
            market_name=args.market_name,
            search_text=args.text,
            only_with_barcode=args.barcode_only,
            limit=args.limit,
        )
    elif args.command == "offer":
        print(f"loading... waitingggg fetching offer '{args.offer_id}' from DB")
        result = db.get_offer_by_id(args.offer_id)
    elif args.command == "store_pricing":
        print(f"loading... waitingggg fetching store pricing insights for '{args.market_name}'")
        result = db.get_store_pricing_insights(
            market_name=args.market_name,
            store_id=args.store_id,
            limit=args.limit,
        )
    elif args.command == "product_pattern":
        print(f"loading... waitingggg fetching product price patterns for '{args.market_name}'")
        result = db.get_product_price_patterns(
            market_name=args.market_name,
            store_id=args.store_id,
            search_text=args.text,
            limit=args.limit,
        )
    elif args.command == "sql":
        print("loading... waitingggg running SQL on DB")
        query = args.query.strip().rstrip(";") + f" LIMIT {max(1, args.limit)}"
        result = db.run_select_query(query)
    else:
        raise RuntimeError("Invalid command")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return

    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return

    if result is None:
        print("No result")
        return

    if isinstance(result, list):
        if not result:
            print("No rows")
            return
        for idx, row in enumerate(result, start=1):
            print(f"[{idx}] {json.dumps(row, ensure_ascii=False, default=str)}")
        print(f"Total rows: {len(result)}")
        return

    print(result)


if __name__ == "__main__":
    main()
