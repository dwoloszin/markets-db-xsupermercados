# Markets - endpoints, store resolution, barcode route, quirks

All notes verified on 2026-09-06. Keep this file current: it is what makes a
broken scraper a 10-minute fix instead of a day of reverse engineering.

Conventions: `CEP` = 8-digit ZIP; "inline" = barcode present in the listing;
"enrich" = extra fetch only for rows whose barcode is NULL.

---

## Atacadão - `markets/atacadao` (VTEX FastStore)

* Store: `GET https://www.atacadao.com.br/api/checkout/pub/regions?postalCode=<CEP>&country=BRA`
  -> `[].sellers[].id` like `atacadaobr120`; first candidate is the nearest (API order),
  coordinates via `/io/api/catalog_system/pub/seller/list` when available.
  `regionId = base64("SW#" + seller_id)`.
* Listing: `GET /api/graphql?operationName=ProductsQuery&variables={"first":100,"after":"<offset>",
  "sort":"score_desc","term":"","selectedFacets":[{"key":"c","value":"<slug>"},
  {"key":"channel","value":"{\"salesChannel\":\"1\",\"seller\":\"<seller>\",\"regionId\":\"<regionId>\"}"},
  {"key":"locale","value":"pt-BR"}]}` -> `data.search.products.edges[].node`.
  Slugs from `/sitemap/category-0.xml` (top level). Node: `isVariantOf.productGroupID`
  (= VTEX productId, our `product_id`), `offers.offers[]` (price, listPrice, minQuantity per
  seller), `image[0].url`, `slug`, `breadcrumbList`.
* Barcode: node `gtin` is an internal RefId -> ignored. Enrich:
  `GET /io/api/catalog_system/pub/products/search?fq=productId:A&fq=productId:B...` (50 per
  call) -> `items[].ean`. `barcode_source = catalog`.
* GraphQL quirk: one product with a broken offer makes the WHOLE page fail with
  `Cannot read properties of undefined (reading 'price')`. The scraper re-fetches such a page
  in slices of 10 and skips only the broken slice (~60 slices per run) - never the category.
* Runtime: ~8 min listing (18 slugs, 100/page, ~10.6k products) + enrichment only for new
  products. Old version: 1h40 (one catalog call per product).

## Barbosa - `markets/barbosa` (applay)

* API `https://api-barbosa.applay.tech/api2/ecommerce`, web `https://www.barbosasupermercados.com.br`.
* Token: `POST {web}/api/auth {"url":"https://api-barbosa.applay.tech/"}` -> `{token}`.
* Session: `POST eauth/session` (encrypted) with device position from the CEP coordinates
  -> `loja` = always the chain's single e-commerce store (Taboão for Barbosa, Pimentas for X),
  whatever the CEP. `enav/listar_lojas` lists the physical stores with `distancia`, but pinning
  one on the session changes nothing (same totals/prices/stock) and the web app has no
  store-switch endpoint -> national prices, `per_store: False`.
* Listing: `POST enav/produtos {session, query:{departamento:<name>}, config:{skus:[seen...]}}`
  (encrypted) -> `produtos[]`, `totalProdutos`. A query is mandatory now. Department names:
  seed list + learned from `produtos[].departamento`.
* Product: `sku` (often the EAN), `material` (internal), `descricao`, `marca`, `de`/`por`,
  `img`, `uri`, `estoque`, `departamento`, `categoria`.
* Barcode: `material` or `sku` when 12-14 valid digits (`barcode_source = inline`).
* Runtime: ~15-25 min (SKU-exclusion pagination grows the request body).

## Hiper Bergamini - `markets/bergamini` (VipCommerce, org 282)

* Same API and shared login key as Rossi; domain `hiperbergamini.com.br`, org 282.
* CD 1 = Hipermercado Bergamini (Jacana); CD 2 = "Supermercado Ourinhos" (Tremembe), a different
  banner with its own catalogue (8/160 products in common, same prices). CD 1 only,
  store_id `bergamini:1:1`, national prices.
* 16 departments, ~14.2k products, `codigo_barras` on every product, ~99% valid GTIN.
  ~10 min. Added 2026-09-22.

## Carrefour Mercado - `markets/carrefour` (VTEX FastStore, HTML route)

* Region: `POST https://mercado.carrefour.com.br/action/cep {CEP}` -> city;
  `POST /action/stores-from-pickups {city}` -> stores (`cep_clique_retire`);
  `POST /action/set-regionalization {CEP,name,city,state,postal_code,store(json)}` -> cookies.
* Listing: `GET /categoria/<path>?page=N` (15 cards/page). Card regex in `parse_cards()`:
  `<a href="/produto/<slug>-<id>" data-testid="search-product-card">`, `<img src alt>`,
  `<h2>name</h2>`, `<span>R$ 9,99</span>` (regular) then `<span>R$ 8,49</span>` (promo),
  `style="background-color..."><span>15% OFF</span>` (tag). **A listing never serves
  `?page=51`** (750 products max), so categories must be the deepest paths: `/sitemap.xml`
  -> `/sitemap/category-N.xml` (currently `category-1.xml`, ~470 paths WITHOUT the
  `/categoria/` prefix, e.g. `bebidas/cervejas`) -> ~385 leaves. 4 categories in parallel.
* Barcode: image file name prefix (`78936683_1_2_...png`, `7891095605118.png`) validated with
  the check digit (`image`) - only a minority of images are named that way (~4%); the rest
  comes from the PDP JSON-LD `gtin` (`pdp`, ~16 min for the first 5k pages, then only new
  products). Produce has PLU codes (`"gtin":"2062"`) -> stays NULL (~5%).
* GraphQL `/api/graphql` and `/api/catalog_system` answer 503/403 captcha outside a browser.
* Runtime: ~12 min listing (385 leaves, 4 threads) + PDP enrichment for new products.

## Coop - `markets/coop` (VTEX IO, per-store prices)

* Catalog API on `www.coopsupermercado.com.br` (33 departments, 31 segments, ~7.2k products,
  `items[0].ean` on every product, ~87% valid GTIN - produce has PLU codes). **The catalog price
  is not the selling price.**
* Stores: 11 white-label sellers `coopsp<xx>`; `/api/checkout/pub/regions` returns all 11 for
  every CEP, so the nearest store is chosen from `/api/checkout/pub/pickup-points` coordinates
  (6 sellers have a pickup point AND price a basket; pickup-only points answer an empty
  simulation). store_id = seller id (`coopspbm` Barao de Maua, `coopspid` Industrial, ...).
* Prices: `POST /api/checkout/pub/orderForms/simulation?sc=1` with up to 50 `{id, seller}` items
  (more are silently truncated to 50) -> `listPrice`, `sellingPrice`, `availability`,
  `unitMultiplier`. Measured 2026-09-22: the same product costs 6.79-7.79 across sellers (~15%).
  Weighed items: listPrice is per kg, sellingPrice includes the multiplier - compare
  `sellingPrice` with `listPrice * unitMultiplier` or every weighed item looks like a 90% promo.
* ~5 min per store (145 catalog calls + 145 simulations). Added 2026-09-22.

## Davo - `markets/davo` (VipCommerce, org 399)

* Migrated in 2026 from its own API (`davo.com.br/api/products`, now a redirect to the SPA)
  to VipCommerce. See Rossi; domain `davo.com.br`, filial 448, org 399.
* 14 departments, ~2.5k products, < 1 min.

## Extra - `markets/extra` and Pão de Açúcar - `markets/paodeacucar` (GPA / Linx)

* `POST https://api.vendas.gpa.digital/{ex|pa}/search/category-page`
  `{"partner":"linx","page":N,"resultsPerPage":48,"multiCategory":"<slug>","sortBy":"relevance",
  "department":"ecom","storeId":<id>,"customerPlus":true}` -> `products[]`, `totalPages`.
  storeIds: Extra 483 (fallback 532, 1, 101), PdA 101 (fallback 532, 483, 1). Slugs from
  `{web}/sitemap/mapa-de-categorias` (`/categoria/<slug>`).
* Product: `id`, `name`, `price`, `brand`, `stock` (bool), `productPromotion{unitPrice,endDate,
  appExclusive,tagLabel,...}`, `productImages[]`, `urlDetails`.
* Pagination: `totalPages` is real (Extra `alimentos` = 238 pages / 8.5k products) but the
  relevance sort repeats products across pages, so a page with nothing new is NOT the end -
  stop only at `totalPages` or after 8 empty pages in a row. Sub-category slugs
  (`alimentos/mercearia`) answer 404; only the top-level slugs of `/sitemap/mapa-de-categorias`
  work (some of them 404 too - skipped).
* Barcode: not in the API (`/{prefix}/products/{id}?storeId=` has no ean either). Enrich from the
  PDP HTML: `"ean":"7891991308243"` (`pdp`). ~13k products each. The storefront WAF answers
  **403 when the request carries a `Referer` header** together with Accept/Accept-Language
  (same URL + UA is 200 without Referer) - the enricher sends no Referer, 4 threads + 0.25 s.
  Blocked fetches are recorded as `error` and retried next run, never as `not_found`.
* Runtime: listing ~5 min each. They share the API host -> run one at a time (`group: gpa`).

## Giga - `markets/giga` (VTEX)

* `https://www.giga.com.vc/api/catalog_system/pub/category/tree/3` + `products/search?fq=C:/path/`.
  Falls back to a plain `_from/_to` sweep if the tree is empty. ~5.5k products, ~2 min.

## Higas - `markets/higas` (Instabuy -> ibecom v5)

* Store: `GET https://api.instabuy.com.br/apiv3/store?partner_id=replicarhigas&zip_code=<CEP>`
  -> branches (`id`, `subdomain`, `address`, `spatial_position`); nearest by API distance /
  coordinates / CEP gap.
* API base `https://api.ibecom.com.br/api_ecommerce/v5`, header `x-store-id: <store id>`,
  browser UA + `Origin/Referer: https://<subdomain>.instabuy.app.br`. Do NOT send the site's
  `ibsessionid` (not needed).
* Listing: `GET /menu` -> `data.items[]` with `link.type == "department"` (18 departments,
  `link.department_id`); then per department
  `GET /items?category_id=<department id>&limit=30&page=N` -> `data[]`,
  `pagination.total_pages/total_count` (~300-900 items per department, ~15k total).
  Item: `id`, `name`, `brand`, `slug`, `image` (-> `https://assets.ibecom.com.br/ib.item.image.medium/m-<image>`),
  `price_config.price`, `price_config.price_discount{promo_price,end_date}`, `stock`.
* **Traps (found with `--probe` on GitHub runners, 2026-09-06)**:
  * `items` without a filter refuses `page > 7`: HTTP 400
    `"Paginação profunda sem filtro não é permitida; filtre por categoria/departamento"`.
  * `department_id`, `department`, `category`, `*_slug` parameters are silently IGNORED
    (same 15k total); only `category_id` (= the menu *department* id) and `subcategory_id`
    (= the menu *category* id) filter.
  * `limit` max 30; `search?search=<text>&N<=50` needs a non-empty text;
    `recommendations/departments/{id}?N=30&page=N` also paginates (no totals).
  * a handful of 400/429 answers in a row -> `403 "Acesso bloqueado"` for the whole IP for
    hours. The scraper waits `HIGAS_DELAY` (2.5 s) between calls, cools down `HIGAS_BACKOFF`
    (300 s) on 4xx/5xx and aborts on the ban message.
* Barcode: none in v5 (`/products/{id}` neither; `apiv3/offers|search` are 404). Coverage
  comes from `offers_legacy` (name/url match) and `tools/crossfill_barcodes.py`.
* Runtime: ~25 min (about 520 calls at 2.5 s).

## Hirota - `markets/hirota` (VipCommerce, org 171)

* Same API and shared login key as Rossi; domain `lojahirota.com.br`, org 171.
* 4 delivery hubs (CD 1 Nazaré/Ipiranga 9.8k items, 2 Granja Viana 8.8k, 3 São Bernardo 9.4k,
  4 Morumbi 9.6k) with identical prices (0-1 differing product in 130+ common, 2026-09-22);
  CDs 5-12 are "Encomenda" pickup stores with ~16 order-only items. Scraped from CD 1,
  store_id `hirota:1:1`, national prices.
* 21 departments, `codigo_barras` on every product, ~97.6% valid GTIN. ~5 min.

## Mambo - `markets/mambo` (VTEX IO)

* Catalog API on `www.mambo.com.br` (account `mambodelivery`): one root category "Mambo",
  14 departments, 33 segments, ~18k products. `items[0].ean` on every product, ~92% valid GTIN
  (produce/butcher/bakery carry `2000xxx` PLU codes).
* 9 stores = sellers `mambodelivery2xx` (pickup-points lists them); regions maps a CEP to one
  store and some CEPs are unserved. Checkout simulation of 150 SKUs on 3 stores: 1 difference ->
  national prices, store_id `mambo`, catalog seller `1`. Added 2026-10-27.

## Master Supermercados - `markets/master` (VipCommerce, org 193)

* Same API and shared login key as Rossi; domain `lojaonline.mastersupermercados.com.br`, org 193.
* One distribution centre (Master Matriz, Erechim RS) -> store_id `master:1:1`, national prices.
* 15 departments, ~22.3k products, `codigo_barras` on every product, ~99% valid GTIN.
  ~15 min. Added 2026-10-20.

## Nagumo - `markets/nagumo` (Salesforce Commerce Cloud)

* Store: `GET /on/demandware.store/Sites-Nagumo-Site/pt_BR/Stores-FindStores?lat=&long=&radius=100`
  (JSON, `stores[]` with coordinates) -> nearest -> `M_<ID>`. (`StoreLocator-GetNearestStores?postalCode=`
  no longer returns a list.)
* Listing: `GET /categoria/<slug>/?sz=<total>&start=0&srule=Relevance` -> products JSON in
  `<search-card-grid products="...">`; total from the probe page (`?sz=1`). Fallback JSON grid:
  `Search-UpdateGrid?cgid=<slug>&sz=120&start=N` -> `productsSearchResult`. Categories: homepage
  `/categoria/...` links + defaults. Catalogue is shared across stores, fetched without `pmid`.
* Product: `id`, `productName`, `brand`, `price{sales}` (`price.list` is always null),
  `flagtypes[]` = the promotions: `{"flagType":"NGM_26_M","valueFlag":7.59}` -> promo_price
  (flagType ending in `_M` = "Meu Nagumo" loyalty-app price, ~30% of the catalogue),
  `promotionDiscount` (null), `images.medium[0].absURL`, `productShowFullUrl`, `ATSInCurrentStore`.
* Barcode: NONE anywhere on the site (checked 2026-09-06): the old `upc` field is gone from the
  listing JSON (`customAttributes: {}`), the PDP JSON-LD carries only mpn/sku, the search index
  does not answer to a barcode query, and the Salesforce OCAPI (`/s/Nagumo/dw/shop/v21_3/...`)
  rejects the client id found in the site JS. Coverage = legacy table (~2k) + the two crossfill
  passes (exact name, then size-aware token set). Only the Nagumo mobile app could expose more
  (its OCAPI client id would have to be captured with a proxy).
* Runtime: ~4-5 min.

## Oba Hortifruti - `markets/oba` (VTEX)

* `https://www.obahortifruti.com.br/api/catalog_system/pub/category/tree/3` + `products/search?fq=`.
  Store id `obahortifruti` (national). Barcode inline `items[0].ean` or `referenceId` (~60%;
  fruit/vegetables have none). ~5.7k products, ~3 min.

## Rossi - `markets/rossi` (VipCommerce, org 63)

* API root `https://services.vipcommerce.com.br/api-admin/v1`, headers `DomainKey: rossidelivery.com.br`,
  `OrganizationId: 63`, `Authorization: Bearer <jwt>`.
* Token: `POST /org/63/auth/loja/login {"domain":"rossidelivery.com.br","username":"loja","key":"<shared key>"}`
  -> `data` = JWT (key in `vipcommerce.py`, override `VIPCOMMERCE_LOGIN_KEY`; or set `ROSSI_API_TOKEN`).
* Store: `GET /org/63/loja/centros_distribuicoes/1` (delivery hub; pickup CDs have no catalogue);
  `GET /org/63/filial/1/loja/tipo_entregas/realiza_entrega?cep=` says if the CEP is served.
* Listing: `GET /org/63/filial/1/centro_distribuicao/1/loja/classificacoes_mercadologicas/departamentos/arvore`
  -> departments; `.../departamentos/<id>/produtos?page=N` (20/page, `paginator.total_pages`).
* Product: `produto_id`, `descricao`, `codigo_barras`, `preco`, `preco_original`/`exibe_preco_original`,
  `oferta{preco_oferta,quantidade_minima,nome,tag}`, `marca`, `imagem` (-> magaluobjects CDN), `link`,
  `unidade_sigla`, `disponivel`, `quantidade_maxima`.
* Barcode inline (~92%). Runtime ~3 min (was Playwright, 6+ min and CI-hostile).

## Sam's Club - `markets/samsclub` (VTEX)

* Region: `GET https://www.samsclub.com.br/api/checkout/pub/regions/?country=BRA&postalCode=<CEP>`
  -> `id` (regionId) + `sellers[]` (nearest store for store_info). Prices are uniform; regionId
  only affects availability. `products/search?fq=C:/..&sc=1&regionId=`. The catalogue indexes
  ~19k products but only ~4.2k carry a price (the rest have Price=0/IsAvailable=false with or
  without regionId = not sold any more); rows without price are not offers and are skipped.
  ~7 min.

## Casa Santa Luzia - `markets/santaluzia` (VTEX IO)

* Catalog API on `www.santaluzia.com.br` (25 departments, 44 segments, ~15.8k products).
* Single online store: regions returns no sellers for any CEP; catalog seller `1` ->
  store_id `santaluzia`, national prices.
* `items[0].ean` on ~95%. Imported goods carry genuine EAN-8 codes, so GTIN-8 is accepted
  (89.8% valid without it, 95.3% with it). The host sometimes resets TLS handshakes;
  request_with_retry absorbs it, pace 0.2 s. ~10 min. Added 2026-10-27.

## Sonda Delivery - `markets/sonda` (ASP.NET HTML)

* Categories: `GET https://www.sondadelivery.com.br/delivery` -> `/delivery/categoria/<slug>` "Ver Todos".
* Page: `ViewItemAnalytics(price,'sku','name')` per product = the price you pay; discounted
  items only show `<div class="product--discount">17% OFF</div>` (the old price is printed
  nowhere, PDP included) so `regular_price` is derived as `price / (1 - pct)`; EAN in
  `/sku/<sku>/<size>/<EAN>.png`; next page `#ctl00_conteudo_linkPaginaProxima`.
* Barcode: image path (~41%, `image`) + PDP JSON-LD `gtin` (`pdp`). ~18k products, ~15 min listing.

## Spani - `markets/spani` (VipCommerce, org 67)

* Same API and shared login key as Rossi; domain `spanionline.com.br`, filial 1, org 67
  (resolved by `organizacoes/filiais/dominio/spanionline.com.br`).
* 14 departments, ~4k products, 20/page. `codigo_barras` on every product, ~95% valid GTIN
  (weighed items carry 4-digit PLU codes). Prices: `preco`, `preco_original`, `oferta.preco_oferta`.
* store_id `spani:1:1` (delivery hub, national prices). Added 2026-09-22.

## St. Marche - `markets/marche` (Shopify)

* `marche.com.br` is a Hydrogen/Oxygen front-end without `products.json`; the Shopify domain
  `checkout.marche.com.br` serves `products.json?limit=250&page=N` (61 pages, ~15.2k) with
  price / compare_at_price / available but NO barcode key in the bulk variants.
* Barcode: `products/<handle>.json` -> `variants[0].barcode`, a `;`-separated list (supplier
  codes, UPC-12, EAN-13s); `pick_gtin` prefers a valid EAN-13. 95% valid GTIN, 77% EAN-13.
  Enrichment step, 6 threads, ~14 s per 120 products, no throttling seen.
* "Selecione uma loja" only changes fulfilment (Shopify has one price per variant) ->
  national prices, store_id `marche`. Added 2026-09-22.

## Swift - `markets/swift` (VTEX behind Remix)

* `GET https://www.swift.com.br/api/categories` -> `categoryList[].linkId`;
  `GET /api/catalog_system/pub/products/search/<slug>?_from&_to`. Cookie `postalcode` for
  serviceability; prices national -> `store_id = swift:<uf>:<city>`. ~1k products, ~1 min.

## Tenda Atacado - `markets/tenda` (Stoom API)

* Store: `GET https://api.tendaatacado.com.br/api/public/branch/zip/<CEP>` -> all 41 branches sorted
  by distance; cookie `_Tendaatacado-branchID=<id>`. Prices are NATIONAL (0 differences across
  4 branches, 2026-09-09); each product carries `inventory[]` with the stock of every branch,
  so the nearest branch only feeds the stock column. `store_id` = constant `tenda`.
* Listing: `GET /api/public/store/search?query=<q>&page=N` (20/page, <= 25 pages). Queries =
  department links + `/public/store/all-categories` sub-links + keywords + a-z, 4 threads,
  dedup by `id`. Headers `Origin/Referer: https://www.tendaatacado.com.br`, `Web-Platform: web-desktop`.
* Product: `id`, `name`, `barcode`, `price`, `wholesalePrices[]{price,minQuantity}`,
  `promotions[]{price,type}`, `photos[]`, `brand`, `department`, `url`, `inventory[]{branchId,totalAvailable}`.
* Barcode inline (~98%). Runtime ~8-10 min.

## Zaffari - `markets/zaffari` (VTEX IO)

* Standard VTEX catalog API on `www.zaffari.com.br` (12 departments, 21 segments, ~15.2k products).
* Store picker = VTEX regionalization: `/api/checkout/pub/regions?postalCode=` returns the same
  5 Porto Alegre store sellers (`zaffari1110/1240/1380/1410/1510`) for every CEP; the catalog
  answers with placeholder seller `1`. Checkout simulation on the same SKU: identical price on
  all sellers, only stock differs -> national prices, store_id `zaffari`. Only `sc=1` exists.
* Barcode inline `items[0].ean` on every product, ~97.5% valid GTIN (weighed items carry
  `0000020...` PLU codes). ~5 min. Added 2026-09-22.

## Violeta Express - `markets/violeta` (VipCommerce, org 121)

* Same API and shared login key as Rossi; domain `violetaexpress.com.br`, org 121.
* 3 stores (CD 1 Loja 01 Vila Gomes, CD 2 Loja 06 Vila Palmeiras, CD 11 Loja 10 Jardim Iris)
  plus "CD Express" hubs, identical prices (0-4 differing products in 75-137 common,
  2026-09-22) -> CD 1, store_id `violeta:1:1`, national prices.
* 13 departments, ~10.4k products, `codigo_barras` on every product but only ~71% valid GTIN:
  butcher/produce/bakery carry 1-5 digit internal codes. ~5 min. Added 2026-09-22.

## X Supermercados - `markets/xsupermercados` (applay)

* Like Barbosa with api `https://api-xsupermercados.applay.tech/api2/ecommerce`, web
  `https://www.xsupermercados.com.br`. Token: `/api/auth` route first; fallback Next.js server
  action on `/buscar?corredor=<id>` (`Next-Action: <40-hex id>`, ids in `known_action_ids`, new
  ones discovered from `$ACTION_ID_` in the page/chunks or via Playwright).
* Listing: `POST enav/produtos_corredor {id:<corridor>, session, query:{}, config:{skus,ordem}}`
  (plain JSON) then `enav/produtos` (encrypted) as a safety net. Corridor `63335f603aa29725e0119211`.
* Barcode inline (~90%). Runtime ~20-30 min.
