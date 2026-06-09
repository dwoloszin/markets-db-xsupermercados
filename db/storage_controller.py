"""
db/storage_controller.py — Database storage monitor and archival controller.

Triggered automatically by main.py after every scrape run, and also available
as a standalone GitHub Actions step.

Flow:
  1. Check size of every market + manager Postgres database
  2. If any DB ≥ DB_ARCHIVE_THRESHOLD_BYTES (default 420 MB):
       a. For each table in DB_ARCHIVE_TABLES:
            - Export oldest rows (beyond DB_ARCHIVE_KEEP_ROWS) to Parquet
            - DELETE those rows from Postgres
       b. Zip all Parquet files
       c. Push the zip to GitHub (DB_ARCHIVE_GITHUB_REPO, DB_ARCHIVE_BRANCH)
  3. Log results to process_timing

Usage (standalone):
    python -m db.storage_controller

GitHub Actions secret required:
    DB_ARCHIVE_GITHUB_TOKEN   — PAT with contents:write on the archive repo
"""

import io
import json
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg  # type: ignore

# ---------------------------------------------------------------------------
# Optional heavy imports — only needed when archiving
# ---------------------------------------------------------------------------
try:
    import pandas as pd  # type: ignore
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import requests as _requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


def _cfg():
    """Lazy import config to avoid circular deps at module load time."""
    import config
    return config


def _db():
    from db.db_manager import DatabaseManager
    return DatabaseManager()


# ---------------------------------------------------------------------------
# Size helpers
# ---------------------------------------------------------------------------

def get_db_size_bytes(database_url: str) -> int:
    """Return the total size of a Postgres database in bytes."""
    try:
        conn = psycopg.connect(database_url)
        cursor = conn.cursor()
        cursor.execute("SELECT pg_database_size(current_database())")
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as exc:
        print(f"  StorageController: could not get DB size: {exc}")
        return 0


def check_all_db_sizes(db) -> Dict[str, int]:
    """Return {label: size_bytes} for the manager DB and every market DB."""
    sizes: Dict[str, int] = {}
    cfg = _cfg()

    manager_url = cfg.DATABASE_URL_MANAGER
    if manager_url:
        sizes["manager"] = get_db_size_bytes(manager_url)

    for market_name, url in cfg.MARKET_DATABASE_URLS.items():
        if url and url != manager_url:
            label = market_name.lower().replace(" ", "_").replace("ã", "a").replace("ã", "a")
            sizes[label] = get_db_size_bytes(url)
        elif url == manager_url:
            # Shared DB — already counted under manager
            pass

    return sizes


def needs_archival(sizes: Dict[str, int]) -> bool:
    threshold = _cfg().DB_ARCHIVE_THRESHOLD_BYTES
    return any(v >= threshold for v in sizes.values())


# ---------------------------------------------------------------------------
# Parquet export helpers
# ---------------------------------------------------------------------------

def _get_db_url_for_table(table_name: str) -> str:
    """Determine which DB URL to use for a given table name."""
    cfg = _cfg()
    # price_history and offers are per-market — use the largest DB
    # For simplicity, archive from the manager DB (has barcode/catalog tables)
    # and from each market DB for offers/price_history
    return cfg.DATABASE_URL_MANAGER or cfg.DATABASE_URL


def export_table_to_parquet(
    database_url: str,
    table_name: str,
    keep_rows: int,
    output_dir: Path,
) -> Optional[Path]:
    """
    Export the oldest rows of `table_name` (beyond keep_rows) to a Parquet file.
    Returns the path to the created file, or None if nothing to export.
    """
    if not _HAS_PANDAS:
        print(f"  StorageController: pandas not installed — skipping Parquet export for {table_name}")
        return None

    try:
        conn = psycopg.connect(database_url)
        cursor = conn.cursor()

        # Count total rows
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        total = int(cursor.fetchone()[0])
        rows_to_archive = total - keep_rows

        if rows_to_archive <= 0:
            conn.close()
            print(f"  StorageController: {table_name} has {total} rows — under keep_rows={keep_rows}, skipping")
            return None

        print(f"  StorageController: {table_name} has {total} rows — archiving {rows_to_archive} oldest rows")

        # Detect ordering column (prefer recorded_at / last_updated / started_at / id)
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        cols = [r[0] for r in cursor.fetchall()]
        order_col = next(
            (c for c in ["recorded_at", "last_updated", "started_at", "last_attempted_at", "id"] if c in cols),
            cols[0],
        )

        # Export the oldest rows
        cursor.execute(
            f"""
            SELECT * FROM {table_name}
            ORDER BY {order_col} ASC
            LIMIT %s
            """,
            (rows_to_archive,),
        )
        rows = cursor.fetchall()
        col_names = [desc[0] for desc in cursor.description]
        conn.close()

        df = pd.DataFrame(rows, columns=col_names)
        # Convert any UUID / custom types to string
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = output_dir / f"{table_name}_{timestamp}.parquet"
        df.to_parquet(out_path, index=False, compression="snappy")
        print(f"  StorageController: wrote {len(df)} rows → {out_path.name} ({out_path.stat().st_size // 1024} KB)")
        return out_path

    except Exception as exc:
        print(f"  StorageController: Parquet export failed for {table_name}: {exc}")
        return None


def delete_archived_rows(
    database_url: str,
    table_name: str,
    keep_rows: int,
) -> int:
    """Delete the oldest rows beyond keep_rows. Returns number of rows deleted."""
    try:
        conn = psycopg.connect(database_url)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        cols = [r[0] for r in cursor.fetchall()]
        order_col = next(
            (c for c in ["recorded_at", "last_updated", "started_at", "last_attempted_at", "id"] if c in cols),
            cols[0],
        )

        # Get PK column(s)
        cursor.execute(
            """
            SELECT a.attname FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            ORDER BY a.attnum
            """,
            (table_name,),
        )
        pk_cols = [r[0] for r in cursor.fetchall()]
        if not pk_cols:
            # Fallback: use ctid
            cursor.execute(
                f"""
                DELETE FROM {table_name}
                WHERE ctid IN (
                    SELECT ctid FROM {table_name}
                    ORDER BY {order_col} ASC
                    LIMIT (SELECT GREATEST(0, COUNT(*) - %s) FROM {table_name})
                )
                """,
                (keep_rows,),
            )
        else:
            pk = pk_cols[0]
            cursor.execute(
                f"""
                DELETE FROM {table_name}
                WHERE {pk} IN (
                    SELECT {pk} FROM {table_name}
                    ORDER BY {order_col} ASC
                    LIMIT (SELECT GREATEST(0, COUNT(*) - %s) FROM {table_name})
                )
                """,
                (keep_rows,),
            )

        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"  StorageController: deleted {deleted} rows from {table_name}")
        return deleted

    except Exception as exc:
        print(f"  StorageController: delete failed for {table_name}: {exc}")
        return 0


# ---------------------------------------------------------------------------
# GitHub push helper
# ---------------------------------------------------------------------------

def push_archive_to_github(zip_path: Path, repo: str, token: str, branch: str) -> bool:
    """Push a zip file to a GitHub repository using the Contents API."""
    if not _HAS_REQUESTS:
        print("  StorageController: 'requests' not installed — cannot push to GitHub")
        return False
    if not repo or not token:
        print("  StorageController: DB_ARCHIVE_GITHUB_REPO or DB_ARCHIVE_GITHUB_TOKEN not set")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y/%m")
    remote_path = f"archives/{timestamp}/{zip_path.name}"
    api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Read and encode file
    import base64
    with open(zip_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    # Check if file already exists (get its SHA if so)
    sha = None
    try:
        r = _requests.get(api_url, headers=headers, params={"ref": branch}, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "message": f"db archive {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = _requests.put(api_url, headers=headers, json=payload, timeout=60)
        if r.status_code in (200, 201):
            print(f"  StorageController: pushed {zip_path.name} → github:{repo}/{remote_path}")
            return True
        else:
            print(f"  StorageController: GitHub push failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as exc:
        print(f"  StorageController: GitHub push error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------

def run(db=None, force: bool = False) -> Dict[str, Any]:
    """
    Main entry point.  Called by main.py after every scrape run.

    Args:
        db:    DatabaseManager instance (created if None)
        force: Run archival even if DB is below threshold

    Returns dict with: checked, threshold_bytes, sizes, archived_tables,
                        rows_deleted, parquet_files, github_pushed
    """
    cfg = _cfg()
    if db is None:
        db = _db()

    result: Dict[str, Any] = {
        "checked": False,
        "threshold_bytes": cfg.DB_ARCHIVE_THRESHOLD_BYTES,
        "sizes": {},
        "needs_archival": False,
        "archived_tables": [],
        "rows_deleted": 0,
        "parquet_files": [],
        "github_pushed": False,
    }

    print("\n=== DB Storage Controller ===")

    # 1. Check sizes — each DB is independent, threshold applies per DB
    sizes = check_all_db_sizes(db)
    result["sizes"] = sizes
    result["checked"] = True

    threshold_mb = cfg.DB_ARCHIVE_THRESHOLD_BYTES // (1024 * 1024)
    dbs_above: Dict[str, int] = {}
    for label, size in sorted(sizes.items(), key=lambda x: -x[1]):
        size_mb = size / (1024 * 1024)
        if size >= cfg.DB_ARCHIVE_THRESHOLD_BYTES:
            flag = f" ⚠ ABOVE {threshold_mb} MB THRESHOLD"
            dbs_above[label] = size
        else:
            flag = ""
        print(f"  {label:<25} {size_mb:>7.1f} MB{flag}")

    do_archive = force or bool(dbs_above)
    result["needs_archival"] = do_archive

    if not do_archive:
        print(f"  All individual DBs are under {threshold_mb} MB — no archival needed")
        print("=== DB Storage Controller done ===\n")
        return result

    print(f"\n  ⚠ {len(dbs_above)} DB(s) above {threshold_mb} MB — starting per-DB archival...")

    # 2. Prepare temp directory
    temp_dir = Path(cfg.DB_ARCHIVE_TEMP_DIR)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    parquet_files: List[Path] = []
    total_deleted = 0

    # 3. Archive each DB that is above the threshold
    # Build a reverse map: label → database_url
    label_to_url: Dict[str, str] = {}
    if cfg.DATABASE_URL_MANAGER:
        label_to_url["manager"] = cfg.DATABASE_URL_MANAGER
    for market_name, url in cfg.MARKET_DATABASE_URLS.items():
        if url and url != cfg.DATABASE_URL_MANAGER:
            label = market_name.lower().replace(" ", "_").replace("ã", "a").replace("ä", "a")
            label_to_url[label] = url

    for db_label in dbs_above:
        db_url = label_to_url.get(db_label) or cfg.DATABASE_URL_MANAGER or cfg.DATABASE_URL
        if not db_url:
            print(f"  Skipping {db_label}: no DB URL found")
            continue
        print(f"\n  Archiving {db_label} ({dbs_above[db_label]/(1024*1024):.1f} MB)...")
        for table_name in cfg.DB_ARCHIVE_TABLES:
            parquet_path = export_table_to_parquet(
                db_url, table_name, cfg.DB_ARCHIVE_KEEP_ROWS, temp_dir
            )
            if parquet_path:
                parquet_files.append(parquet_path)
                deleted = delete_archived_rows(db_url, table_name, cfg.DB_ARCHIVE_KEEP_ROWS)
                total_deleted += deleted
                if table_name not in result["archived_tables"]:
                    result["archived_tables"].append(f"{db_label}.{table_name}")

    result["rows_deleted"] = total_deleted
    result["parquet_files"] = [str(p) for p in parquet_files]

    if not parquet_files:
        print("  No data exported — nothing to push")
        print("=== DB Storage Controller done ===\n")
        return result

    # 4. Zip all Parquet files
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_path = temp_dir / f"db_archive_{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for pf in parquet_files:
            zf.write(pf, pf.name)
    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  Archive zip: {zip_path.name} ({zip_size_mb:.1f} MB)")

    # 5. Push to GitHub
    pushed = push_archive_to_github(
        zip_path,
        repo=cfg.DB_ARCHIVE_GITHUB_REPO,
        token=cfg.DB_ARCHIVE_GITHUB_TOKEN,
        branch=cfg.DB_ARCHIVE_BRANCH,
    )
    result["github_pushed"] = pushed

    # 6. Log to process_timing
    try:
        db.log_process_timing(
            process_name="storage_controller",
            step_name="archive_and_push",
            status="success" if pushed else "archived_no_push",
            duration_seconds=0,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            run_type="individual",
            details=json.dumps({
                "tables": result["archived_tables"],
                "rows_deleted": total_deleted,
                "zip_size_mb": round(zip_size_mb, 2),
                "github_pushed": pushed,
            }),
        )
    except Exception as exc:
        print(f"  StorageController: timing log failed: {exc}")

    # 7. Cleanup temp
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass

    print(f"  Archived {len(result['archived_tables'])} tables, deleted {total_deleted} rows")
    print("=== DB Storage Controller done ===\n")
    return result


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from env_loader import load_env_file
    load_env_file()

    force = "--force" in sys.argv
    results = run(force=force)

    print("\nSummary:")
    print(f"  Checked:        {results['checked']}")
    print(f"  Needs archival: {results['needs_archival']}")
    print(f"  Tables archived:{results['archived_tables']}")
    print(f"  Rows deleted:   {results['rows_deleted']}")
    print(f"  GitHub pushed:  {results['github_pushed']}")
