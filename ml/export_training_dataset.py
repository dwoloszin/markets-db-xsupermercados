import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

from db.db_manager import DatabaseManager


def _fetch_positive_rows(db: DatabaseManager, limit: int) -> List[Tuple[Any, ...]]:
    if db.use_postgres:
        conn = db._get_pg()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT target_offer_id, target_market, inferred_barcode, source_market, source_market_id, confidence, reasoning
            FROM match_audit
            ORDER BY last_updated DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    import sqlite3

    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT target_offer_id, target_market, inferred_barcode, source_market, source_market_id, confidence, reasoning
        FROM match_audit
        ORDER BY last_updated DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def export_dataset(output_csv: Path, limit: int) -> Dict[str, int]:
    db = DatabaseManager()
    rows = _fetch_positive_rows(db, limit)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "target_offer_id",
                "target_market",
                "inferred_barcode",
                "source_market",
                "source_market_id",
                "confidence",
                "reasoning",
                "label",
            ]
        )
        for row in rows:
            writer.writerow(list(row) + [1])

    return {"rows": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export positive training pairs from match_audit")
    parser.add_argument("--output", default="ml/data/train_pairs_positive.csv")
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args()

    stats = export_dataset(Path(args.output), max(1, int(args.limit)))
    print(f"Training dataset exported: {args.output} rows={stats['rows']}")


if __name__ == "__main__":
    main()
