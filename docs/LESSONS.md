# Lessons learned

Things we paid for in hours of debugging. Read before writing or fixing a scraper.

## Reconnaissance recipe (how every market here was reverse engineered)

1. `curl` the category page with a browser User-Agent. If the HTML already has
   the products (Sonda, Carrefour, Nagumo), parse it; it is the most stable route.
2. Look for a platform signature and use its public API:
   * `/api/catalog_system/pub/...` -> **VTEX** (Swift, Oba, Sam's, Giga, Atacadão `/io/api`)
   * `services.vipcommerce.com.br` in the JS -> **VipCommerce** (Rossi, Davo)
   * `api-<store>.applay.tech` -> **applay** (X, Barbosa)
   * `api.vendas.gpa.digital` -> **GPA/Linx** (Extra, PdA)
   * `demandware.store` -> **Salesforce Commerce Cloud** (Nagumo)
   * `api.ibecom.com.br` / `instabuy` -> **Instabuy** (Higas)
3. When the API is not obvious, intercept the browser once with Playwright
   (`page.on("request"/"response")`, filter by host) and copy the exact URL +
   headers + POST body. Keep this to reconnaissance; never ship it in the daily path.
4. Check where the **barcode** is: listing field, image file name, PDP JSON-LD,
   a lookup API. Then check its precision with the check digit.
5. Save the findings in docs/MARKETS.md immediately (sites change; the note is
   what lets you fix it in minutes next time).

## Platform-specific gotchas

* **VTEX** hard-caps `_from` at 2500 per filter. Split large categories into
  their children with the FULL path (`C:/6/42/`, not `C:/42/`). `resources`
  header gives the total. Several `fq=productId:X` in one call batch-fetch 50
  products - the cheapest EAN lookup there is. Prices in the catalog API are the
  default seller's; FastStore GraphQL gives per-store prices (Atacadão) but its
  `gtin` field is an internal id.
* **VipCommerce**: the storefront JWT comes from `POST org/{org}/auth/loja/login`
  with `username: "loja"` and a shared key (same on Rossi and Davo). Pickup
  distribution centres have an empty catalogue; use CD 1. 20 products per page.
* **applay**: bodies/responses are AES-CBC encrypted the CryptoJS way with the
  passphrase `BEWAREOBLIVIONISATHAND`. Pagination is "send me products not in
  this SKU list", so the request grows; that is why X/Barbosa take 20-30 min.
  Barbosa's `enav/produtos` now requires a query (department); department names
  are only visible on products, so we seed a list and learn new names from the
  `departamento` field while scraping. X token = Next.js server action whose
  id rotates on deploys; Barbosa has a plain `/api/auth` route.
* **GPA/Linx**: the list API has no barcode and `/products/{id}` neither. The
  PDP HTML contains `"ean":"..."`. A wrong `storeId` returns an empty (not
  error) result - always try the fallbacks.
* **Carrefour**: GraphQL/catalog APIs answer 503/403 captcha to non-browser
  clients; the category HTML (React Router SSR) is open and has 15 cards per page
  with the price and an image named `<GTIN>_...`. Regionalisation is three POST
  `/action/...` calls that set cookies.
* **Instabuy (Higas)**: `api.instabuy.com.br/apiv3/{offers,search,category}` are
  gone (404). The storefront is `<sub>.instabuy.app.br` and uses
  `api.ibecom.com.br/api_ecommerce/v5` with header `x-store-id`; `items?limit=30`
  paginates (30 max). No barcode field any more. The API bans the IP
  (`403 "Acesso bloqueado"`, hours) after ~8 fast requests: pace at 2.5 s, never
  retry a 403 in a loop, prefer `curl_cffi` (Chrome TLS fingerprint).
* **Carrefour** serves at most 50 pages per listing: always scrape the deepest
  sitemap categories, never the top-level ones.
* **GPA/Linx** relevance sort repeats products across pages: an all-duplicates
  page is not the end of the category (stop at `totalPages`).
* **Nagumo**: `StoreLocator-GetNearestStores?postalCode=` stopped returning a
  list; `Stores-FindStores?lat=&long=&radius=100` works. Category HTML accepts
  `?sz=<total>` to return everything in one page. The `upc` field disappeared in 2026.
* **Sonda**: WebForms pagination via `ctl00_conteudo_linkPaginaProxima`; EAN in
  the image path for ~41% of products and in the PDP JSON-LD for all.
* **Tenda**: no category listing; search queries only (20/page, 25 pages max).
  Coverage comes from many queries (departments + sub-categories + keywords +
  a-z) deduplicated by id.

## General

* Always send a real UA and `Accept-Language: pt-BR`; several WAFs 403 the
  default `python-requests` UA.
* Back off on 429/5xx (`request_with_retry`); a burst of retries gets the IP
  banned for hours (Instabuy, Carrefour).
* A 200 with HTML where JSON was expected is a challenge page, not data.
* One `requests.Session` per thread.
* Save per category / per page, never hold a whole market in memory.
* Barcodes never change: cache them (DB) and enrich only NULL rows. This turned
  Atacadão from 1h40 into minutes.
* Fail loudly: `run_cli` exits 1 when a full run saves nothing, so a silently
  broken site is visible in the Actions UI.
* GitHub datacenter IPs are blocked by some WAFs (Convertiez in the pharmacy
  project). `main.py --only-stale` exists to refresh those from a home IP.

## Data lessons

* The old `market_storehash_barcode` offer id caused duplicates whenever a
  barcode was learned after the first insert. Use the native product id as PK
  and keep the barcode as an indexed column.
* Never overwrite a known barcode with NULL on a later scrape (COALESCE in the
  upsert).
* Some sites send `promo_end_at` in 2099 (cache TTL) - drop dates > 2 years out.
* Weighed items (fruit, meat, bakery) have PLU codes, not GTINs. Expect NULL.
* 8-digit "barcodes" are usually internal ids unless the field is an explicit
  EAN field; validate with the check digit and allow GTIN-8 per source.
