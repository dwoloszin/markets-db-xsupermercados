#!/usr/bin/env python3
"""
Cleanup legacy tables from manager DB.
Drops per-market tables that now live in dedicated market databases.
"""
import os
import sys

# Load env manually
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(), val.strip())

import psycopg

manager_url = os.getenv('DATABASE_URL_MANAGER') or os.getenv('DATABASE_URL')
if not manager_url:
    print("ERROR: DATABASE_URL_MANAGER not set")
    sys.exit(1)

DRY_RUN = '--dry-run' in sys.argv

print(f"Connecting to manager DB...")
print(f"Mode: {'DRY RUN (no changes)' if DRY_RUN else 'LIVE - will DROP tables'}")
print()

with psycopg.connect(manager_url, connect_timeout=10) as conn:
    with conn.cursor() as cur:
        # List all tables with sizes
        cur.execute("""
            SELECT 
                t.table_name,
                pg_size_pretty(pg_total_relation_size(quote_ident(t.table_name))) AS size,
                pg_total_relation_size(quote_ident(t.table_name)) AS raw_size
            FROM information_schema.tables t
            WHERE t.table_schema = 'public'
            ORDER BY pg_total_relation_size(quote_ident(t.table_name)) DESC
        """)
        rows = cur.fetchall()
        
        COMMON_TABLES = {
            "barcode_references", "barcode_reference_market_map",
            "store_mappings", "match_audit",
            "model_inference_audit", "barcode_inference_state",
            "process_timing", "product_catalog", "higas_barcode_enrich_state",
        }
        MARKET_TABLES = {
            "offers",
            "price_history",
            "store_pricing_insights",
            "product_price_patterns",
        }
        
        print(f"{'Table':<40} {'Size':>12}  Status")
        print("-" * 72)
        legacy = []
        total_legacy_bytes = 0
        for table_name, size, raw_size in rows:
            if table_name in MARKET_TABLES:
                status = "LEGACY - will DROP"
                legacy.append(table_name)
                total_legacy_bytes += raw_size
            elif table_name in COMMON_TABLES:
                status = "KEEP"
            else:
                status = "UNKNOWN - keep as-is"
            print(f"  {table_name:<38} {size:>12}  {status}")
        
        print()
        
        if not legacy:
            print("No legacy tables found. Manager DB is already clean.")
            sys.exit(0)
        
        # Human-readable size
        mb = total_legacy_bytes / (1024 * 1024)
        print(f"Legacy tables to drop: {legacy}")
        print(f"Space to reclaim: ~{mb:.1f} MB")
        print()
        
        if DRY_RUN:
            print("DRY RUN - no changes made. Run without --dry-run to drop tables.")
        else:
            confirm = input("Proceed with DROP? Type 'yes' to confirm: ").strip().lower()
            if confirm != 'yes':
                print("Aborted.")
                sys.exit(0)
            
            for table in legacy:
                print(f"  Dropping '{table}'...")
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            conn.commit()
            print()
            print(f"Done. Dropped {len(legacy)} legacy tables from manager DB.")
            print("Run VACUUM to reclaim disk space (or wait for Neon autovacuum).")
