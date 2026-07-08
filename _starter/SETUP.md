# New Project Setup Guide

This folder is a clean template to start a new scraper project (e.g., drugstores)
with the same architecture as `markets_db`:

- One **NeonDB** (via Supabase) per store
- One **shared manager DB** for barcode catalog cross-matching
- One **GitHub repo** per store, each running its own scrape workflow
- A **master repo** (this one) that syncs code to all store repos

---

## Prerequisites

- Python 3.12+
- `pip install requests playwright`
- [GitHub CLI](https://cli.github.com) (`gh auth login`)
- A Supabase account at https://app.supabase.com

---

## Step 1 — Copy this folder to a new project

```bash
cp -r _starter/ /path/to/new-project
cd /path/to/new-project
git init && git add . && git commit -m "initial commit"
```

Create a new GitHub repo for the master project:
```bash
gh repo create yourorg/stores-db --private
git remote add origin https://github.com/yourorg/stores-db.git
git push -u origin main
```

---

## Step 2 — Copy shared infrastructure

Run the setup script to copy `db/`, `env_loader.py`, and other shared files
from the parent `markets_db` project:

```bash
python setup.py --source /path/to/markets_db
# or auto-detect if this project is inside markets_db:
python setup.py
```

This copies:
- `db/` — DatabaseManager, BarcodeAIMatcher, barcode_matcher, storage_controller
- `env_loader.py` — loads .env files
- `product_normalizer.py` — normalizes product names
- `location_detector.py` — detects store location from ZIP
- `requirements.txt` — Python dependencies
- `reset_data.py` — resets DB tables for a store

---

## Step 3 — Create environment file

```bash
cp .env.template .env
```

Fill in `.env` with your secrets (see comments inside the file):

| Variable | Where to get it |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | https://app.supabase.com/account/tokens |
| `DB_ARCHIVE_GITHUB_TOKEN` | GitHub → Settings → Developer settings → PATs (needs `repo` + `workflow` scopes) |
| `OPENROUTER_API_KEY` | https://openrouter.ai (optional) |
| `GEMINI_API_KEY` | https://aistudio.google.com/app/apikey (optional) |
| `XAI_API_KEY` | https://x.ai (optional) |
| `HF_TOKEN` | https://huggingface.co/settings/tokens (optional) |

---

## Step 4 — Create Supabase databases

This creates one NeonDB project per store (manager + each store):

```bash
python deploy/supabase_bootstrap.py bootstrap --write-env .env
```

Before running, add your stores to `ROLE_TO_ENV_KEY` in `deploy/supabase_bootstrap.py`:

```python
ROLE_TO_ENV_KEY = {
    "manager": "DATABASE_URL_MANAGER",
    "Drogaria Example": "DATABASE_URL_EXAMPLE",
    # add more stores...
}
```

The script will:
1. Create one Supabase project per role (each gets a free PostgreSQL DB)
2. Wait for each project to become active
3. Write `DATABASE_URL_*` connection strings into your `.env`
4. Save a disaster recovery manifest in `deploy/disaster_recovery/`

> **Tip**: The manager DB is the shared catalog for barcode cross-matching.
> All store DBs are independent — one per store.

---

## Step 5 — Add your first scraper

Copy the template and fill it in:

```bash
cp markets/example/scraper_template.py markets/my_store/scraper_my_store.py
```

Edit the new file:
1. Rename `ExampleStoreScraper` → `MyStoreScraper`
2. Set `BASE_URL` and `STORE_ID`
3. Implement `_fetch_products()` — calls the store's API and returns raw product dicts
4. Implement `_standardize()` — converts raw dicts to the standard offer schema

Then register the scraper in `main.py`:

```python
from markets.my_store.scraper_my_store import MyStoreScraper

STORE_REGISTRY = {
    "My Store Name": MyStoreScraper,
}
```

And add it to `config.py`:

```python
STORE_DATABASE_URLS = {
    "My Store Name": _secret("DATABASE_URL_MYSTORE") or DATABASE_URL,
}

STORE_TIER = {
    "My Store Name": 1,   # 1=has barcodes, 2=partial, 3=none
}

STORE_DB_SUFFIX = {
    "My Store Name": "MYSTORE",
}
```

---

## Step 6 — Test locally

```bash
SCRAPE_MARKET="My Store Name" SCRAPE_LIMIT=20 python main.py
```

If it prints offers at the end, the scraper works.

---

## Step 7 — Deploy to GitHub

First, configure the stores in `deploy/deploy.py`:

```python
REPO_PREFIX = "stores-db"   # repo names: stores-db-my-store

STORE_SCHEDULE = {
    "My Store Name": "0 17 * * *",   # daily 17:00 UTC
}

STORE_DB_SUFFIX = {
    "My Store Name": "MYSTORE",
}
```

Also add the suffix map to `deploy/store-scrape.yml` (the workflow secret section):

```python
suffix_map = {
    "My Store Name": "MYSTORE",
}
```

Then run:

```bash
python deploy/deploy.py --dry-run   # preview
python deploy/deploy.py             # create repos + set secrets
```

This will:
1. Create a private GitHub repo `yourorg/stores-db-my-store`
2. Push the codebase
3. Set all required secrets (DB URLs, API keys, etc.) on the repo

---

## Step 8 — Set master repo secrets

Add these secrets to the **master repo** (Settings → Secrets → Actions):

| Secret | Value |
|---|---|
| `DB_ARCHIVE_GITHUB_TOKEN` | Same PAT from Step 3 |
| `GITHUB_ORG` | Your GitHub username/org |

These are needed by the sync workflow to push code to store repos.

---

## Step 9 — Enable scheduled runs (when ready)

When you're ready to run stores on a schedule:

1. Edit `STORE_SCHEDULE` in the sync workflow (`.github/workflows/sync-to-store-repos.yml`)
   and change the cron from the "disabled" Dec-1st value to a daily schedule:
   ```python
   STORE_SCHEDULE = {
       "My Store Name": "0 17 * * *",   # daily 17:00 UTC
   }
   ```

2. Run the sync workflow manually to push the updated schedule to all store repos:
   - Go to GitHub → Actions → "Sync to Store Repos" → "Run workflow"

3. Also enable the push trigger in the sync workflow:
   ```yaml
   on:
     push:
       branches: [main]
   ```

---

## Architecture overview

```
master repo (stores-db)
    |
    |-- main.py              ← runs scrapers, calls db.save_offers()
    |-- config.py            ← all non-secret settings
    |-- markets/
    |   |-- my_store/
    |       |-- scraper_my_store.py
    |-- db/                  ← shared: DatabaseManager, BarcodeAIMatcher
    |-- deploy/
    |   |-- supabase_bootstrap.py  ← creates Supabase (NeonDB) projects
    |   |-- deploy.py              ← creates GitHub repos, sets secrets
    |   |-- store-scrape.yml       ← workflow template pushed to store repos
    |-- .github/workflows/
        |-- sync-to-store-repos.yml  ← syncs code to all store repos

each store repo (stores-db-my-store)
    |-- main.py              ← same code, runs only "My Store Name"
    |-- .github/workflows/
        |-- scrape.yml       ← injected by sync, has per-store schedule
    env vars / secrets: MARKET_NAME=My Store Name, DATABASE_URL_MYSTORE=...
```

---

## Key secrets reference

| Secret | Where set | Used by |
|---|---|---|
| `SUPABASE_ACCESS_TOKEN` | `.env` only (local) | `supabase_bootstrap.py` |
| `DB_ARCHIVE_GITHUB_TOKEN` | `.env` + all repos | `deploy.py`, sync workflow, storage controller |
| `DATABASE_URL_MANAGER` | `.env` + all repos | every scraper run (shared catalog) |
| `DATABASE_URL_*` | `.env` + each store repo | that store's scraper |
| `OPENROUTER_API_KEY` | `.env` + all repos | AI barcode inference (optional) |
| `MARKET_NAME` | each store repo only | identifies which store to run |

---

## Troubleshooting

**"No scraper registered for store"** — Add it to `STORE_REGISTRY` in `main.py`

**"DATABASE_URL_* not set"** — Run `supabase_bootstrap.py` or add the URL to `.env` manually

**"Could not clone — does it exist?"** — Run `deploy.py` first to create the store repo

**Scraper returns 0 offers** — Check the store's API: wrong URL, wrong headers, needs auth

**Barcode inference not working** — Set at least one of `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY` in `.env`
