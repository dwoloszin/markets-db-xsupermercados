import argparse
import os
from pathlib import Path
from typing import List

from db.db_manager import DatabaseManager
from env_loader import load_env_file


POSTGRES_RESET_TABLES = [
    "offers",
    "barcode_reference_market_map",
    "store_mappings",
    "match_audit",
    "model_inference_audit",
    "price_history",
    "store_pricing_insights",
    "product_price_patterns",
    "barcode_inference_state",
    "process_timing",
    "product_catalog",
    "higas_barcode_enrich_state",
]


def _truncate_existing_tables(conn, table_names: List[str], dry_run: bool, db_label: str) -> List[str]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        ORDER BY table_name
        """,
        (table_names,),
    )
    existing_tables = [row[0] for row in cursor.fetchall()]

    if not existing_tables:
        return [f"skip {db_label} truncate (no matching tables found)"]

    truncate_sql = "TRUNCATE TABLE " + ", ".join(existing_tables) + " RESTART IDENTITY CASCADE"
    actions = [f"{db_label}: {truncate_sql}"]
    if dry_run:
        return actions

    cursor.execute(truncate_sql)
    conn.commit()
    return actions


def _truncate_postgres_tables(dry_run: bool) -> List[str]:
    db = DatabaseManager()
    if not db.use_postgres:
        raise RuntimeError("DATABASE_URL_MANAGER (or DATABASE_URL) is not configured. Cannot reset Postgres.")

    common_tables = [name for name in POSTGRES_RESET_TABLES if name in db.COMMON_TABLES]
    market_tables = [name for name in POSTGRES_RESET_TABLES if name in db.MARKET_TABLES]

    actions: List[str] = []

    manager_conn = db._get_pg()
    actions.extend(_truncate_existing_tables(manager_conn, common_tables, dry_run, "manager_db"))
    manager_conn.close()

    market_db_urls = db._iter_market_database_urls()
    for index, market_db_url in enumerate(market_db_urls, start=1):
        market_conn = db._connect_pg(market_db_url)
        actions.extend(
            _truncate_existing_tables(
                market_conn,
                market_tables,
                dry_run,
                f"market_db_{index}",
            )
        )
        market_conn.close()

    return actions


def _delete_local_files(dry_run: bool) -> List[str]:
    # Only delete analysis CSV files - SQLite databases are no longer used
    targets = [
        Path("analysis_offers.csv"),
            Path("analysis_store_mappings.csv"),
        Path("analysis_price_history.csv"),
    ]

    actions = []
    for target in targets:
        if target.exists():
            actions.append(f"delete {target}")
            if not dry_run:
                target.unlink()
        else:
            actions.append(f"skip missing {target}")
    return actions


def _validate_mode(mode: str):
    allowed = {"postgres", "local", "all"}
    if mode not in allowed:
        raise ValueError(f"Invalid mode '{mode}'. Choose one of {sorted(allowed)}")


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="Reset project data safely")
    parser.add_argument(
        "mode",
        nargs="?",
        default="postgres",
        choices=["postgres", "local", "all"],
        help="What to reset: postgres, local, or all",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted/reset without doing it",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    _validate_mode(args.mode)

    if not args.yes and not args.dry_run:
        print("WARNING: this will permanently delete/reset data.")
        confirm = input("Type RESET to continue: ").strip()
        if confirm != "RESET":
            print("Cancelled.")
            return

    actions: List[str] = []

    if args.mode in {"postgres", "all"}:
        actions.extend(_truncate_postgres_tables(args.dry_run))

    if args.mode in {"local", "all"}:
        actions.extend(_delete_local_files(args.dry_run))

    if args.dry_run:
        print("Dry run actions:")
    else:
        print("Completed actions:")
    for action in actions:
        print(f"- {action}")


if __name__ == "__main__":
    main()
