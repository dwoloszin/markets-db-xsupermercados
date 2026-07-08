import json
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from markets.tier1_inline_barcodes.market_scrap_xsupermercados import XSupermercadosScraper


class XSupermercadosDepartamentosScraper(XSupermercadosScraper):
    """Departamento-oriented wrapper for X Supermercados.

    Current backend behavior: departamento filtering is not exposed/accepted by
    the same internal endpoint used by the storefront pagination. We still
    discover and log available department labels from Home components, then use
    reliable full-catalog pagination from the parent scraper.
    """

    def _extract_departamento_from_link(self, raw_link: str) -> Optional[str]:
        link = str(raw_link or "").strip()
        if not link:
            return None

        parsed = urlparse(link if "://" in link else f"https://www.xsupermercados.com.br/{link.lstrip('/')}")
        params = parse_qs(parsed.query)
        dep_values = params.get("departamento") or []
        for value in dep_values:
            text = str(value or "").strip()
            if text:
                return text
        return None

    def _extract_lista_from_link(self, raw_link: str) -> Optional[str]:
        link = str(raw_link or "").strip().strip("/")
        if not link:
            return None

        parsed = urlparse(link if "://" in link else f"https://www.xsupermercados.com.br/{link.lstrip('/')}")
        params = parse_qs(parsed.query)
        lista_values = params.get("listas-prontas") or []
        for value in lista_values:
            text = str(value or "").strip().strip("/")
            if text:
                return text

        if parsed.query:
            return None

        # Home payload sometimes stores lista-pronta entries as a plain slug path.
        # Only accept paths that look like lista slugs (no slashes, not a known nav path).
        path = parsed.path.strip("/")
        _NAV_PATHS = {"", "home", "login", "cadastro", "conta", "carrinho", "busca", "buscar"}
        if not path or "/" in path or path.lower() in _NAV_PATHS:
            return None
        # Must look like a slug: only alphanumerics, hyphens, underscores
        if not re.match(r"^[a-zA-Z0-9_-]+$", path):
            return None
        return path

    def _collect_candidate_departamento_names(self, node: object) -> List[str]:
        candidates: List[str] = []

        def walk(value: object, parent_key: Optional[str] = None) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    walk(inner, str(key).lower())
                return

            if isinstance(value, list):
                for inner in value:
                    walk(inner, parent_key)
                return

            if not isinstance(value, str):
                return

            text = value.strip()
            if not text:
                return
            if text.startswith("http") or "/" in text or "?" in text:
                return
            if len(text) > 64:
                return

            key = (parent_key or "").lower()
            if key in {"titulo", "title", "nome", "name", "departamento", "categoria"}:
                candidates.append(text)

        walk(node)
        return candidates

    def discover_catalog_targets(self, zip_code: str) -> Dict[str, List[str]]:
        corridor_id = self.DEFAULT_CORRIDOR_ID
        token = self._get_access_token(corridor_id)
        session_obj, _ = self._open_session(zip_code, token)

        produtos_departamentos: List[str] = []
        produtos_headers = {
            "x-access-token": token,
            "Content-Type": "application/json",
        }
        produtos_payload = {
            "session": session_obj,
            "query": {},
            "config": {"skus": None},
        }
        try:
            produtos_response = self.session.post(
                f"{self.API_BASE}/enav/produtos",
                headers=produtos_headers,
                data=json.dumps(self._protect_payload(produtos_payload), ensure_ascii=False),
                timeout=60,
            )
            if produtos_response.status_code != 200:
                print(f"XSupermercados discover: produtos HTTP {produtos_response.status_code}")
            else:
                content_type = produtos_response.headers.get("Content-Type", "")
                if "json" not in content_type:
                    print(f"XSupermercados discover: produtos unexpected Content-Type={content_type!r}")
                else:
                    produtos_body = produtos_response.json() or {}
                    produtos_decoded = self._extract_protected_payload((produtos_body.get("data") or {}))
                    if isinstance(produtos_decoded, dict):
                        departamentos_raw = produtos_decoded.get("departamentos") or []
                        if isinstance(departamentos_raw, list):
                            for name in departamentos_raw:
                                if isinstance(name, str) and name.strip():
                                    produtos_departamentos.append(name.strip())
                        print(f"XSupermercados discover: {len(produtos_departamentos)} depts from produtos")
        except Exception as exc:
            print(f"XSupermercados discover: produtos error {exc}")

        headers = {
            "x-access-token": token,
            "Content-Type": "application/json",
        }
        decoded: dict = {}
        try:
            response = self.session.post(
                f"{self.API_BASE}/enav/home",
                headers=headers,
                data=json.dumps({"session": session_obj}, ensure_ascii=False),
                timeout=60,
            )
            if response.status_code != 200:
                print(f"XSupermercados discover: home HTTP {response.status_code}")
            else:
                body = response.json() or {}
                result = self._extract_protected_payload((body.get("data") or {}))
                if isinstance(result, dict):
                    decoded = result
        except Exception as exc:
            print(f"XSupermercados discover: home error {exc}")

        componentes = decoded.get("componentes") or []
        departamento_names: List[str] = []
        lista_links: List[str] = []

        for comp in componentes:
            if not isinstance(comp, dict):
                continue

            # Also collect names from component titles/labels via the walk helper
            for candidate in self._collect_candidate_departamento_names(comp):
                departamento_names.append(candidate)

            banners = comp.get("banners") or []
            for banner in banners:
                if not isinstance(banner, dict):
                    continue
                titulo = banner.get("titulo")
                link = banner.get("link")

                if isinstance(link, str) and link.strip():
                    dep = self._extract_departamento_from_link(link)
                    if dep:
                        departamento_names.append(dep)

                    lista = self._extract_lista_from_link(link)
                    if lista:
                        lista_links.append(lista)

        departamento_names.extend(produtos_departamentos)

        dedup_departamentos: List[str] = []
        seen_departamentos = set()
        for name in departamento_names:
            cleaned = str(name or "").strip()
            key = cleaned.casefold()
            if not cleaned or key in seen_departamentos:
                continue
            seen_departamentos.add(key)
            dedup_departamentos.append(cleaned)

        dedup_listas: List[str] = []
        seen_listas = set()
        for slug in lista_links:
            cleaned = str(slug or "").strip().strip("/")
            if not cleaned or cleaned in seen_listas:
                continue
            seen_listas.add(cleaned)
            dedup_listas.append(cleaned)

        return {
            "departamentos": dedup_departamentos,
            "listas": dedup_listas,
        }

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict]:
        print(f"Fetching X Supermercados departamentos offers for {zip_code}...")
        try:
            targets = self.discover_catalog_targets(zip_code)
            listas = targets.get("listas") or []
            departamentos = targets.get("departamentos") or []

            print(
                "X Supermercados departamentos: discovered "
                f"{len(listas)} lista links and {len(departamentos)} departamento labels"
            )

            max_items = limit if isinstance(limit, int) and limit > 0 else None
            combined_by_id: Dict[str, Dict] = {}

            if listas:
                print(
                    "X Supermercados departamentos: collecting from listas-prontas endpoint."
                )
                lista_rows = super().fetch_offers_from_listas(
                    zip_code,
                    lista_links=listas,
                    limit=max_items,
                )
                for row in lista_rows:
                    combined_by_id[row["id"]] = row

            remaining = None
            if max_items is not None:
                remaining = max(0, max_items - len(combined_by_id))

            if departamentos and (remaining is None or remaining > 0):
                print(
                    "X Supermercados departamentos: collecting from departamento endpoint."
                )
                departamento_rows = super().fetch_offers_from_departamentos(
                    zip_code,
                    departamentos=departamentos,
                    limit=remaining,
                )
                for row in departamento_rows:
                    combined_by_id[row["id"]] = row

            if combined_by_id:
                final_rows = list(combined_by_id.values())
                if max_items is not None:
                    final_rows = final_rows[:max_items]
                print(
                    "X Supermercados departamentos: combined endpoint total "
                    f"{len(final_rows)} offers."
                )
                return final_rows

            print("X Supermercados departamentos: no labels discovered from home payload")
            print("X Supermercados departamentos: falling back to corridor pagination.")
        except Exception as exc:
            print(
                f"X Supermercados departamentos: discovery failed ({exc}), "
                "using full-catalog pagination."
            )

        return super().fetch_offers(zip_code, limit=limit)


if __name__ == "__main__":
    scraper = XSupermercadosDepartamentosScraper()
    scraper.fetch_offers("08032-230")
