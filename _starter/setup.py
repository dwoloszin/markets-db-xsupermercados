#!/usr/bin/env python3
"""
setup.py — Bootstrap a new scraper project from this template.

Copies shared infrastructure files from the parent markets_db project
into this project directory. Run this once when starting a new project.

Usage:
    python setup.py                          # copy from auto-detected parent
    python setup.py --source /path/to/markets_db

Files copied:
    db/                    (DatabaseManager, BarcodeAIMatcher, etc.)
    env_loader.py          (loads .env files)
    product_normalizer.py  (normalizes product names)
    location_detector.py   (detects store location from ZIP)
    requirements.txt       (Python dependencies)
    reset_data.py          (resets DB tables for a market)
"""

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()

# Files/folders to copy from the parent project
SHARED_FILES = [
    "db/",
    "env_loader.py",
    "product_normalizer.py",
    "location_detector.py",
    "requirements.txt",
    "reset_data.py",
]


def find_parent_project() -> Path:
    """Try to find the parent markets_db project by walking up the directory tree."""
    candidate = HERE.parent
    while candidate != candidate.parent:
        if (candidate / "db" / "db_manager.py").exists():
            return candidate
        candidate = candidate.parent
    return None


def copy_files(source: Path, dest: Path, dry_run: bool) -> None:
    for rel in SHARED_FILES:
        src = source / rel
        dst = dest / rel

        if not src.exists():
            print(f"  [SKIP] {rel} — not found in source")
            continue

        if dry_run:
            print(f"  [dry-run] Would copy: {rel}")
            continue

        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            print(f"  [copy dir]  {rel}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  [copy file] {rel}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap new project from shared infrastructure")
    parser.add_argument("--source", metavar="DIR", help="Path to parent markets_db project")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied")
    args = parser.parse_args()

    if args.source:
        source = Path(args.source).resolve()
    else:
        source = find_parent_project()
        if source is None:
            print("ERROR: Could not find parent markets_db project.")
            print("  Run: python setup.py --source /path/to/markets_db")
            sys.exit(1)

    if not source.exists():
        print(f"ERROR: Source directory not found: {source}")
        sys.exit(1)

    print(f"Source: {source}")
    print(f"Dest:   {HERE}")
    if args.dry_run:
        print("[DRY RUN]\n")

    copy_files(source, HERE, args.dry_run)

    if not args.dry_run:
        print(f"\nDone. Next steps:")
        print(f"  1. Copy .env.template to .env and fill in your secrets")
        print(f"  2. Run: python deploy/supabase_bootstrap.py bootstrap --write-env .env")
        print(f"  3. Add your first scraper to markets/ (copy scraper_template.py)")
        print(f"  4. Register it in main.py STORE_REGISTRY")
        print(f"  5. Test locally: python main.py")
        print(f"  6. Deploy to GitHub: python deploy/deploy.py")


if __name__ == "__main__":
    main()
