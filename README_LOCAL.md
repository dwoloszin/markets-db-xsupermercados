
# to do list



etapa de barcodematch levando muito tempo

atacadão tem leitor de brcode no app verificar como pegar endpoint

higas não esta incluindo os intens nas ofertas com a função enrrich


extra include oofer tag because says 10% off for pay with extra card


ROSSI not working in git Actions



# for date format use
Recommendation: Use YYYY-MM-DDTHH:MM:SSZ (ISO 8601)


corrigir link higas

=================================================================
TIMING BREAKDOWN
=================================================================
  Step                                      Time      %
  -------------------------------------------------------
  ✓ Swift                                  1m 15s     0%
  ✓ Carrefour                              9m 03s     2%
  ✓ XSupermercados                        30m 30s     7%  █
  ✓ Barbosa                               20m 50s     4%
  ✓ Rossi                                  6m 15s     1%
  ✓ Extra                                  6m 55s     1%
  ✓ Pão de Açúcar                          3m 55s     1%
  ✓ Oba Hortifruti                        37m 20s     8%  █
  ✓ Sam's Club                             6m 44s     1%
  ✓ Tenda Atacado                         18m 27s     4%
  ✓ Davo                                      16s     0%
  ✓ Giga                                   1m 40s     0%
  ✓ Sonda Delivery                        17m 46s     4%
  ✓ Catalog sync                               1s     0%
  ✓ Atacadão                           1h 39m 56s    22%  ████
  ✓ Nagumo                                 4m 25s     1%
  ✓ Higas                                  2m 44s     1%
  ✓ Barcode sync + inference           3h 14m 36s    42%  ████████
  ✓ DB optimize                               45s     0%
  ───────────────────────────────────────────────────────
  TOTAL                               7h 43m 24s  100%
=================================================================



== DB Storage Controller ===
  All individual DBs are under 420 MB — no archival needed
=== DB Storage Controller done ===


Syncing app_offers to Supabase manager2...
  Loaded 31 store mappings from manager
  app_offers sync: Atacadao              11,771 rows
  app_offers sync: Barbosa                8,923 rows
  app_offers sync: Carrefour              3,892 rows
  app_offers sync: Davo                   1,637 rows
  app_offers sync: Extra                 11,799 rows
  app_offers sync: Giga                   3,211 rows
  app_offers sync: Higas                  8,363 rows
  app_offers sync: Nagumo                   914 rows
  app_offers sync: Oba                    2,888 rows
  app_offers sync: Paodeacucar           10,961 rows
  app_offers sync: Rossi                 12,486 rows
  app_offers sync: Samsclub               4,051 rows
  app_offers sync: Sonda                  4,690 rows
  app_offers sync: Swift                    888 rows
  app_offers sync: Tenda                  8,715 rows
  app_offers sync: Xsupermercados         4,901 rows
  app_offers full sync complete: 100,090 rows in 55.5s | manager2: 87.4 MB

=================================================================
TIMING BREAKDOWN
=================================================================
  Step                                      Time      %
  -------------------------------------------------------
  ✓ Swift                                  1m 21s     0%  
  ✓ Carrefour                              8m 32s     2%  
  ✓ XSupermercados                         9m 10s     2%  
  ✓ Barbosa                               13m 47s     3%  
  ✓ Rossi                                 17m 11s     4%  
  ✓ Extra                                  5m 32s     1%  
  ✓ Pão de Açúcar                          3m 02s     1%  
  ✓ Oba Hortifruti                        37m 15s     9%  █
  ✓ Sam's Club                             6m 06s     2%  
  ✓ Tenda Atacado                         17m 22s     4%  
  ✓ Davo                                      14s     0%  
  ✓ Giga                                   1m 35s     0%  
  ✓ Sonda Delivery                        13m 19s     3%  
  ✓ Catalog sync                               1s     0%  
  ✓ Atacadão                           1h 26m 48s    21%  ████
  ✓ Nagumo                                 4m 26s     1%  
  ✓ Higas                                  1m 38s     0%  
  ✓ app_offers sync                           56s     0%  
  ✓ Barcode sync + inference           2h 54m 43s    43%  ████████
  ✓ DB optimize                               53s     0%  
  ───────────────────────────────────────────────────────
  TOTAL                               6h 43m 52s  100%
=================================================================

--- ALL departamentos run complete ---
(.venv) PS C:\Users\dwolo\Documents\DARIO\PYTHON\markets_db> 

Historical pricing insights:
- Every `save_offers()` run now refreshes store-level and product-level history analysis inside each market Neon DB.
- New market tables:
  - `store_pricing_insights`: best weekday to buy for each store based on observed promo/toggle history.
  - `product_price_patterns`: recurring price points, best weekday price, and predicted next toggle when `promo_end_at` is missing.
- Quick inspection commands:
  - `python query_db.py store_pricing "Rossi" --limit 10`
  - `python query_db.py product_pattern "Rossi" --store-id <store_id> --text "coca" --limit 20`



  parallel starts 8h30

