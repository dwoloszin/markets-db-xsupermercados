import os
import re
import json
import unicodedata
from math import asin, cos, radians, sin, sqrt
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from db.db_manager import DatabaseManager


class RossiDepartamentosScraper:
    DEFAULT_FILIAL = "1"
    DEFAULT_DEPARTMENT_URLS = [
        "https://www.rossidelivery.com.br/departamentos/biscoitos-e-chocolates",
        "https://www.rossidelivery.com.br/departamentos/bazar-e-utilidades",
        "https://www.rossidelivery.com.br/departamentos/laticinios-e-frios",
        "https://www.rossidelivery.com.br/departamentos/bebidas",
        "https://www.rossidelivery.com.br/departamentos/hortifruti",
        "https://www.rossidelivery.com.br/departamentos/carnes-e-aves",
        "https://www.rossidelivery.com.br/departamentos/padaria-e-confeitaria",
        "https://www.rossidelivery.com.br/departamentos/mercearia",
        "https://www.rossidelivery.com.br/departamentos/limpeza",
        "https://www.rossidelivery.com.br/departamentos/higiene-e-beleza",
        "https://www.rossidelivery.com.br/departamentos/congelados",
        "https://www.rossidelivery.com.br/departamentos/pet-shop",
    ]

    def __init__(self):
        self.db = DatabaseManager()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
        )
        self.market_name = "Rossi"
        self._resolved_store_metadata: Optional[Dict[str, Any]] = None
        _default_token = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJ2aXBjb21tZXJjZSIsImF1ZCI6ImFwaS1hZG1pbiIs"
            "InN1YiI6IjZiYzQ4NjdlLWRjYTktMTFlOS04NzQyLTAyMGQ3OTM1OWNhMCIsInZpcGNvbW1lcmNlQ2xpZW50ZUlkIjp"
            "udWxsLCJpYXQiOjE3NzA1OTUzMDksInZlciI6MSwiY2xpZW50IjpudWxsLCJvcGVyYXRvciI6bnVsbCwib3JnIjoiNjMi"
            "fQ.T0BNUA61yW47v9hrMEZlWbUveCMY4yS0VuhKKtkcTwM9LGbrOTppCwwVXGFcAZA8DeFM3lhVfEBq645bUAqk9A"
        )
        self.api_token = os.getenv("ROSSI_API_TOKEN", _default_token)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_zip(value: Any) -> str:
        return "".join(ch for ch in str(value or "") if ch.isdigit())

    @staticmethod
    def _normalize_text_key(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        d_lat = radians(lat2 - lat1)
        d_lon = radians(lon2 - lon1)
        a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
        return R * 2 * asin(sqrt(max(0.0, min(1.0, a))))

    @staticmethod
    def _infer_app_membership_required(*texts: Optional[str]) -> bool:
        joined = " ".join(str(t or "") for t in texts).lower()
        return any(tag in joined for tag in ("meu rossi", "clube", "app", "cadastro"))

    @staticmethod
    def _append_page_query(url: str, page: int) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["page"] = [str(page)]
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(qs, doseq=True), parsed.fragment)
        )

    @staticmethod
    def _normalize_department_urls(urls: Iterable[str]) -> List[str]:
        unique: List[str] = []
        seen: set = set()
        for url in urls:
            if not url:
                continue
            parsed = urlparse(url.strip())
            path = (parsed.path or "").rstrip("/")
            if not path.startswith("/departamentos/") or path == "/departamentos":
                continue
            normalized = f"https://www.rossidelivery.com.br{path}"
            if normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    # --------------------------------------------------------- zip / geo helpers

    def _resolve_zip_metadata(self, zip_code: str) -> Dict[str, Any]:
        normalized = self._normalize_zip(zip_code)
        if len(normalized) != 8:
            return {}
        try:
            r = self.session.get(f"https://viacep.com.br/ws/{normalized}/json/", timeout=8)
            if r.status_code != 200:
                return {}
            data = r.json() or {}
            if data.get("erro"):
                return {}
            return {"city": data.get("localidade"), "state": data.get("uf")}
        except Exception:
            return {}

    def _resolve_zip_coordinates(self, zip_code: str) -> Optional[tuple]:
        normalized = self._normalize_zip(zip_code)
        if len(normalized) != 8:
            return None
        for query in [f"{normalized[:5]}-{normalized[5:]}", f"{normalized}, Brasil"]:
            try:
                r = self.session.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": query, "format": "json", "limit": 1, "countrycodes": "br"},
                    headers={"User-Agent": "markets-db-rossi-scraper/1.0"},
                    timeout=8,
                )
                if r.status_code != 200:
                    continue
                data = r.json() or []
                if not data:
                    continue
                lat = self._to_float(data[0].get("lat"))
                lon = self._to_float(data[0].get("lon"))
                if lat is not None and lon is not None:
                    return lat, lon
            except Exception:
                continue
        return None

    # --------------------------------------------------------- store resolution

    def _store_coordinates(self, store: Dict[str, Any]) -> Optional[tuple]:
        coords = store.get("coordenada_geografica") or store.get("coordinates") or {}
        lat = self._to_float(coords.get("latitude") or store.get("latitude"))
        lon = self._to_float(coords.get("longitude") or store.get("longitude"))
        if lat is None or lon is None:
            return None
        return lat, lon

    def _store_zip_distance(self, user_zip: str, store: Dict[str, Any]) -> Optional[int]:
        addr = store.get("endereco") or store.get("address") or {}
        store_zip = self._normalize_zip(
            addr.get("cep") or addr.get("zipcode") or addr.get("postalCode")
        )
        if len(user_zip) != 8 or len(store_zip) != 8:
            return None
        try:
            return abs(int(user_zip) - int(store_zip))
        except ValueError:
            return None

    def _store_distance_score(self, store: Dict[str, Any]) -> float:
        for key in ("distancia", "distance", "distance_km", "km"):
            v = self._to_float(store.get(key))
            if v is not None:
                return v
        return float("inf")

    def _store_sort_key(
        self, store: Dict[str, Any], normalized_zip: str, zip_coords: Optional[tuple]
    ) -> tuple:
        api_km = self._store_distance_score(store)
        coords = self._store_coordinates(store)
        haversine = (
            self._haversine_km(zip_coords[0], zip_coords[1], coords[0], coords[1])
            if zip_coords and coords
            else float("inf")
        )
        zip_score = float(self._store_zip_distance(normalized_zip, store) or float("inf"))
        stable_id = int(self._to_int(store.get("id")) or 999999)
        return api_km, haversine, zip_score, stable_id

    def _filter_by_region(
        self, candidates: List[Dict], zip_city: Optional[str], zip_state: Optional[str]
    ) -> List[Dict]:
        if not candidates or (not zip_state and not zip_city):
            return candidates
        nstate = self._normalize_text_key(zip_state)
        ncity = self._normalize_text_key(zip_city)
        same_state, same_city = [], []
        for item in candidates:
            addr = item.get("endereco") or item.get("address") or {}
            istate = self._normalize_text_key(addr.get("uf") or addr.get("state"))
            icity = self._normalize_text_key(addr.get("cidade") or addr.get("city"))
            if nstate and istate == nstate:
                same_state.append(item)
                if ncity and icity == ncity:
                    same_city.append(item)
        return same_city or same_state or candidates

    def _build_store_metadata(self, store: Dict[str, Any]) -> Dict[str, Any]:
        addr = store.get("endereco") or store.get("address") or {}
        coords = self._store_coordinates(store)
        parts = [
            str(addr.get("logradouro") or addr.get("street") or "").strip(),
            str(addr.get("numero") or addr.get("number") or "").strip(),
            str(addr.get("bairro") or addr.get("district") or "").strip(),
        ]
        return {
            "store_name": store.get("nome") or store.get("name"),
            "store_address": ", ".join(p for p in parts if p) or None,
            "store_city": addr.get("cidade") or addr.get("city"),
            "store_state": addr.get("uf") or addr.get("state"),
            "latitude": coords[0] if coords else store.get("latitude"),
            "longitude": coords[1] if coords else store.get("longitude"),
            "store_cd_id": store.get("vipcommerce_centro_distribuicao_id"),
            "store_payload": store,
        }

    def resolve_store(self, zip_code: str) -> Optional[str]:
        """Return the best filial ID for the given ZIP and populate _resolved_store_metadata."""
        self._resolved_store_metadata = None
        normalized_zip = self._normalize_zip(zip_code)
        if not normalized_zip:
            return None

        zip_meta = self._resolve_zip_metadata(normalized_zip)
        zip_city = zip_meta.get("city")
        zip_state = zip_meta.get("state")
        zip_coords = self._resolve_zip_coordinates(normalized_zip)

        api_headers = {
            "DomainKey": "rossidelivery.com.br",
            "OrganizationId": "63",
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        formatted_zip = f"{normalized_zip[:5]}-{normalized_zip[5:]}"
        endpoints = [
            "https://services.vipcommerce.com.br/api-admin/v1/org/63/filial/1/loja/centros_distribuicoes/retiradas",
            f"https://services.vipcommerce.com.br/api-admin/v1/org/63/filial?cep={formatted_zip}",
            f"https://services.vipcommerce.com.br/api-admin/v1/org/63/filial?cep={normalized_zip}",
            f"https://services.vipcommerce.com.br/api-admin/v1/org/63/filial?zip_code={formatted_zip}",
        ]

        for url in endpoints:
            try:
                r = self.session.get(url, headers=api_headers, timeout=12)
                label = url.split("filial")[-1][:50]
                print(f"Rossi resolve_store [{label}]: HTTP {r.status_code}")
                if r.status_code != 200:
                    print(f"  body: {r.text[:200]}")
                    continue

                data = r.json() or {}
                raw = data.get("data") or data
                if isinstance(raw, dict):
                    candidates = [raw] if raw.get("id") else []
                elif isinstance(raw, list):
                    candidates = [c for c in raw if isinstance(c, dict) and c.get("id")]
                else:
                    candidates = []

                if not candidates:
                    print("  → no candidates in response")
                    continue

                sample = candidates[0]
                print(
                    f"  → {len(candidates)} candidates | "
                    f"sample: id={sample.get('id')} nome={sample.get('nome')} "
                    f"cd={sample.get('vipcommerce_centro_distribuicao_id')}"
                )

                filtered = self._filter_by_region(candidates, zip_city, zip_state)
                best = min(filtered, key=lambda s: self._store_sort_key(s, normalized_zip, zip_coords))
                self._resolved_store_metadata = self._build_store_metadata(best)
                print(
                    f"Rossi store resolved: id={best.get('id')} "
                    f"nome={best.get('nome')} "
                    f"cd={best.get('vipcommerce_centro_distribuicao_id')}"
                )
                return str(best["id"])

            except Exception as exc:
                print(f"  → exception: {exc}")
                continue

        print("Rossi resolve_store: all endpoints failed, using DEFAULT_FILIAL")
        return self.DEFAULT_FILIAL

    # ------------------------------------------------- department URL discovery

    def discover_department_urls(self) -> List[str]:
        """Discover all department URLs. Tries plain HTTP first, then Playwright."""
        page_url = "https://www.rossidelivery.com.br/departamentos"

        # Fast path: plain HTTP (works if server pre-renders links)
        try:
            r = self.session.get(page_url, timeout=20)
            if r.status_code == 200 and r.text:
                links = re.findall(
                    r"href=[\"']((?:https?://www\.rossidelivery\.com\.br)?/departamentos/[^\"'#?]+)",
                    r.text,
                    flags=re.IGNORECASE,
                )
                normalized = self._normalize_department_urls(links)
                if normalized:
                    print(f"Rossi: discovered {len(normalized)} departments via HTML.")
                    return normalized
        except Exception:
            pass

        # Playwright path: needed for SPA rendering
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(
                "Rossi: Playwright not installed — using DEFAULT_DEPARTMENT_URLS.\n"
                "To enable auto-discovery run: pip install playwright && playwright install chromium"
            )
            return []

        try:
            with sync_playwright() as pw:
                browser = self._launch_browser(pw)
                ctx = self._new_stealth_context(browser)
                page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page.set_default_timeout(45_000)
                page.goto(page_url, wait_until="networkidle")
                page.wait_for_timeout(2_500)
                links = page.eval_on_selector_all(
                    "a[href*='/departamentos/']",
                    "els => els.map(e => e.href)",
                )
                ctx.close()
                browser.close()
                normalized = self._normalize_department_urls([str(lk) for lk in links])
            if normalized:
                print(f"Rossi: discovered {len(normalized)} departments via Playwright.")
            return normalized
        except Exception as exc:
            print(f"Rossi: Playwright department discovery failed: {exc}")
            return []

    # ---------------------------------------- shared Playwright browser factory

    @staticmethod
    def _launch_browser(pw):
        """Launch Chromium with anti-bot args needed for CI (GitHub Actions Linux)."""
        return pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-gpu",
            ],
        )

    @staticmethod
    def _new_stealth_context(browser):
        """Create a browser context that mimics a real Windows Chrome session."""
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        return ctx

    @staticmethod
    def _is_product_like_response_url(url: str) -> bool:
        u = str(url or "").lower()
        if not u:
            return False
        return (
            "vipcommerce" in u
            or "rossidelivery.com.br" in u
            or "/produto" in u
            or "/produtos" in u
            or "/departamento" in u
            or "prateleira" in u
            or "shelf" in u
            or "search" in u
        )

    @staticmethod
    def _is_product_payload(body: Any) -> bool:
        def _looks_like_product_dict(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            has_identity = any(k in item for k in ("produto_id", "id", "descricao", "nome", "link"))
            has_commerce = any(k in item for k in ("preco", "oferta", "codigo_barras", "marca", "imagem"))
            return has_identity and has_commerce

        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data:
                if any(_looks_like_product_dict(item) for item in data):
                    return True
            items = body.get("items")
            if isinstance(items, list) and items:
                if any(_looks_like_product_dict(item) for item in items):
                    return True
        if isinstance(body, list) and body:
            return any(_looks_like_product_dict(item) for item in body)
        return False

    @staticmethod
    def _raw_product_key(item: Dict[str, Any]) -> Optional[str]:
        pid = item.get("produto_id") or item.get("id")
        if pid:
            return f"pid:{pid}"
        link = item.get("link") or item.get("href")
        if link:
            return f"link:{str(link).strip().lower()}"
        name = str(item.get("descricao") or item.get("nome") or "").strip().lower()
        if not name:
            return None
        price = item.get("preco") or item.get("preco_oferta") or ""
        unit = item.get("unidade_sigla") or item.get("unidade") or ""
        return f"name:{name}|price:{price}|unit:{unit}"

    @staticmethod
    def _ssr_extract_products(page):
        """
        Fallback: extract products from server-rendered vip-card-produto elements.
        Used when the VipCommerce API interception returns nothing (bot-blocked on CI).
        Returns dicts shaped like the API response so _standardize_product works unchanged.
        """
        js = r"""
        () => Array.from(document.querySelectorAll('vip-card-produto')).map(el => {
            const a = (n) => el.getAttribute(n) || null;
            const linkEl = el.querySelector('a[href*="/produto/"]');
            const href = a('href') || (linkEl ? linkEl.getAttribute('href') : null);
            let pid = null;
            if (href) { const m = href.match(/\/produto\/(\d+)/); if (m) pid = parseInt(m[1]); }
            const rawPrice = a('preco') || a('price') || a('preco-venda') || '0';
            const price = parseFloat(rawPrice.replace(',', '.')) || null;
            const rawPromo = a('preco-oferta') || a('preco_oferta') || null;
            const promoPrice = rawPromo ? (parseFloat(rawPromo.replace(',', '.')) || null) : null;
            return {
                produto_id: pid || a('produto-id') || a('produto_id') || a('id'),
                descricao:  a('nome') || a('name') || a('descricao') || '',
                codigo_barras: a('codigo-barras') || a('codigo_barras') || a('barcode') || null,
                marca:      a('marca') || a('brand') || null,
                preco:      price,
                imagem:     a('imagem') || a('image') || a('src') || null,
                unidade_sigla: a('unidade-sigla') || a('unidade_sigla') || null,
                link:       href,
                oferta: promoPrice ? {preco_oferta: promoPrice} : null,
            };
        }).filter(p => p.descricao || p.produto_id)
        """
        try:
            items = page.evaluate(js)
            return items if isinstance(items, list) else []
        except Exception:
            return []

    # -------------------------------------------- Playwright product collection

    def _browser_collect_products(
        self,
        department_url: str,
        *,
        max_pages: int,
        wait_ms: int,
        filial_id: str,
        cd_id: str,
        max_items: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Open each department page in a headless browser and collect products.
        Primary path: intercept VipCommerce API responses (works when not bot-blocked).
        Fallback path: extract from server-rendered vip-card-produto DOM elements.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required. "
                "Run: pip install playwright && playwright install chromium"
            ) from exc

        all_products: List[Dict[str, Any]] = []
        seen_ids: set = set()
        current_batch: List[Dict[str, Any]] = []

        def _on_response(response) -> None:
            if not self._is_product_like_response_url(response.url):
                return
            if response.status != 200:
                return
            try:
                body = response.json()
            except Exception:
                return
            if not self._is_product_payload(body):
                return
            if isinstance(body, dict):
                if isinstance(body.get("data"), list):
                    items = body.get("data") or []
                else:
                    items = body.get("items") or []
            else:
                items = body if isinstance(body, list) else []
            if not items or not isinstance(items[0], dict):
                return
            filtered_items = [
                item
                for item in items
                if isinstance(item, dict)
                and any(k in item for k in ("produto_id", "id", "descricao", "nome", "link"))
                and any(k in item for k in ("preco", "oferta", "codigo_barras", "marca", "imagem"))
            ]
            if filtered_items:
                current_batch.extend(filtered_items)

        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            ctx = self._new_stealth_context(browser)
            # Hint selected store context for CI sessions where store selection
            # is not persisted and product APIs return empty datasets.
            try:
                ctx.add_cookies(
                    [
                        {
                            "name": "filial_id",
                            "value": str(filial_id),
                            "domain": ".rossidelivery.com.br",
                            "path": "/",
                        },
                        {
                            "name": "loja_id",
                            "value": str(filial_id),
                            "domain": ".rossidelivery.com.br",
                            "path": "/",
                        },
                        {
                            "name": "centro_distribuicao_id",
                            "value": str(cd_id),
                            "domain": ".rossidelivery.com.br",
                            "path": "/",
                        },
                    ]
                )
            except Exception:
                pass
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            storage_payload = json.dumps({"filialId": str(filial_id), "cdId": str(cd_id)})
            storage_script = """
                (() => {
                    const payload = __PAYLOAD__;
                    const filialId = payload.filialId;
                    const cdId = payload.cdId;
                    const pairs = [
                        ['filial_id', filialId],
                        ['filialId', filialId],
                        ['loja_id', filialId],
                        ['store_id', filialId],
                        ['vipcommerce_filial_id', filialId],
                        ['vipcommerce_centro_distribuicao_id', cdId],
                        ['centro_distribuicao_id', cdId],
                        ['cd_id', cdId],
                    ];
                    for (const [k, v] of pairs) {
                        try { localStorage.setItem(k, String(v)); } catch (_) {}
                        try { sessionStorage.setItem(k, String(v)); } catch (_) {}
                    }
                })()
                """.replace("__PAYLOAD__", storage_payload)
            page.add_init_script(
                storage_script,
            )
            page.set_default_timeout(30_000)
            page.on("response", _on_response)

            for page_num in range(1, max_pages + 1):
                current_batch.clear()
                target = self._append_page_query(department_url, page_num)

                # Navigate and wait for DOM ready
                page.goto(target, wait_until="domcontentloaded", timeout=30_000)

                # Wait for the product API call to fire and return
                try:
                    page.wait_for_response(
                        lambda r: (
                            self._is_product_like_response_url(r.url)
                            and r.status == 200
                        ),
                        timeout=7_000,
                    )
                except Exception:
                    # API timed out — probably bot-blocked on CI. Try SSR DOM extraction.
                    pass

                # Small extra wait for any follow-up requests to settle
                page.wait_for_timeout(wait_ms)

                # If API interception yielded nothing, fall back to SSR DOM extraction
                if not current_batch:
                    ssr = self._ssr_extract_products(page)
                    if ssr:
                        print(f"  page={page_num} api=0 ssr={len(ssr)} (SSR fallback active)")
                        current_batch.extend(ssr)

                new_count = 0
                for p in current_batch:
                    key = self._raw_product_key(p)
                    if not key or key in seen_ids:
                        continue
                    seen_ids.add(key)
                    all_products.append(p)
                    new_count += 1

                print(
                    f"  page={page_num} intercepted={len(current_batch)} "
                    f"new={new_count} total={len(all_products)}"
                )

                if not current_batch or new_count == 0:
                    print(f"  → no new products at page {page_num}, stopping department.")
                    break

                if max_items is not None and len(all_products) >= max_items:
                    break

            ctx.close()
            browser.close()

        return all_products

    # --------------------------------------------------------------- standardize

    def _standardize_product(
        self,
        p: Dict[str, Any],
        zip_code: str,
        filial: str,
    ) -> Optional[Dict[str, Any]]:
        description = p.get("descricao")
        if not description:
            return None

        native_id = p.get("produto_id") or p.get("id")
        raw_barcode = p.get("codigo_barras")
        gtin_text = str(raw_barcode) if raw_barcode else None
        barcode = self.db.normalize_barcode(gtin_text) if gtin_text else None

        offer_id = self.db.build_offer_id("rossi", filial, barcode, gtin_text, description)
        if not offer_id:
            return None

        oferta = p.get("oferta") or {}
        regular_price = self._to_float(p.get("preco"))
        promo_price = self._to_float(oferta.get("preco_oferta")) or regular_price

        image_path = p.get("imagem") or ""
        if image_path.startswith("http"):
            image_url = image_path
        elif image_path:
            image_url = f"https://produto-assets-vipcommerce-com-br.br-se1.magaluobjects.com/250x250/{image_path}"
        else:
            image_url = None

        product_slug = p.get("link")
        if native_id and product_slug:
            product_url = f"https://www.rossidelivery.com.br/produto/{native_id}/{product_slug}"
        elif native_id:
            product_url = f"https://www.rossidelivery.com.br/produto/{native_id}/"
        else:
            product_url = None

        offer_name = oferta.get("nome")
        offer_tag = oferta.get("tag")

        return {
            "id": offer_id,
            "product_name": description[:200],
            "brand": p.get("marca") or None,
            "description": p.get("observacao") or None,
            "regular_price": regular_price,
            "promo_price": promo_price,
            "promo_min_quantity": self._to_int(oferta.get("quantidade_minima")),
            "unit": p.get("unidade_sigla") or None,
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": None,
            "stock_general": None,
            "sold_quantity": self._to_int(p.get("quantidade_vendida")),
            "offer_name": offer_name,
            "offer_tag": offer_tag,
            "app_membership_required": self._infer_app_membership_required(offer_name, offer_tag),
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": filial,
            "zip_code": zip_code,
        }

    # --------------------------------------------------------- main entry point

    def fetch_offers(
        self,
        zip_code: str,
        department_urls: Optional[Iterable[str]] = None,
        max_pages_per_department: int = 80,
        render_wait_ms: int = 2500,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        print(f"Fetching Rossi departamentos offers for {zip_code}...")
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        # ── resolve store ──────────────────────────────────────────────────────
        cached_filial = self.db.get_store_id(zip_code, self.market_name)
        filial = self.resolve_store(zip_code)
        if filial:
            metadata = self._resolved_store_metadata or {}
            self.db.cache_store_id(
                zip_code,
                self.market_name,
                filial,
                store_name=metadata.get("store_name"),
                store_address=metadata.get("store_address"),
                store_city=metadata.get("store_city"),
                store_state=metadata.get("store_state"),
                latitude=metadata.get("latitude"),
                longitude=metadata.get("longitude"),
                store_payload=metadata.get("store_payload"),
            )
        else:
            filial = cached_filial
        if not filial:
            print("Rossi: could not resolve store ID.")
            return []

        metadata = self._resolved_store_metadata or {}
        selected_store_name = metadata.get("store_name") or "N/A"
        selected_cd_id = str(
            metadata.get("store_cd_id")
            or (metadata.get("store_payload") or {}).get("vipcommerce_centro_distribuicao_id")
            or "1"
        )
        print(
            f"Rossi departamentos: store id={filial} cd={selected_cd_id} "
            f"name={selected_store_name} cached_previous={cached_filial or 'N/A'}"
        )

        # ── department URLs ────────────────────────────────────────────────────
        if department_urls is not None:
            urls = [u for u in department_urls if u]
        else:
            discovered = self.discover_department_urls()
            urls = discovered if discovered else self.DEFAULT_DEPARTMENT_URLS

        if not urls:
            print("Rossi: no department URLs available.")
            return []

        print(f"Rossi departamentos: scraping {len(urls)} departments")
        for i, u in enumerate(urls, 1):
            print(f"  [{i}] {u}")

        # ── scrape each department ─────────────────────────────────────────────
        products_by_id: Dict[str, Dict[str, Any]] = {}

        for dept_url in urls:
            if max_items is not None and len(products_by_id) >= max_items:
                break

            remaining = (max_items - len(products_by_id)) if max_items is not None else None
            print(f"Rossi departamentos: scraping {dept_url}")

            raw = self._browser_collect_products(
                dept_url,
                max_pages=max_pages_per_department,
                wait_ms=render_wait_ms,
                filial_id=str(filial),
                cd_id=selected_cd_id,
                max_items=remaining,
            )

            for p in raw:
                std = self._standardize_product(p, zip_code, filial)
                if std is None:
                    continue
                products_by_id[std["id"]] = std
                if max_items is not None and len(products_by_id) >= max_items:
                    break

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        print(f"Rossi departamentos: {len(all_products)} products collected.")
        return all_products


if __name__ == "__main__":
    scraper = RossiDepartamentosScraper()
    scraper.fetch_offers("08032-230")
