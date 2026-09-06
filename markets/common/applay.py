"""
applay.py - Shared client for the "applay" e-commerce platform
(X Supermercados: api-xsupermercados.applay.tech, Barbosa: api-barbosa.applay.tech).

How the platform works (reverse engineered, see docs/MARKETS.md):
  1. Access token (x-access-token):
       Barbosa: POST {web}/api/auth {"url": "<api base>/"} -> {"token"}
       X:       Next.js server action on {web}/buscar?corredor=<id> with header
                Next-Action: <action id>. Action ids rotate on each deploy; we try the
                known ids, then scan the page HTML for $ACTION_ID_<40 hex>, then (last
                resort) use Playwright to discover it.
  2. Session: POST {api}/eauth/session with an encrypted body -> session object with the
     store ("loja") chosen from the device position (lat/lng of the CEP).
     Bodies are AES-CBC encrypted the CryptoJS way (EVP_BytesToKey, passphrase
     "BEWAREOBLIVIONISATHAND", "Salted__" prefix); responses come back the same way.
  3. Products: POST {api}/enav/produtos {session, query:{departamento?}, config:{skus:[...]}}
     The API has no page number: every call returns the next ~50 products NOT in
     config.skus, so we keep sending the ids already seen (the body grows, which is why
     these two markets take ~20-30 min). X also has enav/produtos_corredor.
  Product fields: sku, _id, descricao, marca, de (list price), por (sale price), img,
     uri, estoque, material (often the EAN), unidadeMedidaExibicao.
Barcode  : `material` or `sku` when it is a valid 12-14 digit GTIN (~87-90%).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from markets.common.geo import format_zip, normalize_zip, zip_coords
from markets.common.gtin import first_gtin
from markets.common.http import make_session
from markets.common.offer import make_offer

CRYPTO_PASS = b"BEWAREOBLIVIONISATHAND"


# ----------------------------------------------------------------------------- crypto
def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int) -> Tuple[bytes, bytes]:
    digest = b""
    block = b""
    while len(digest) < key_len + iv_len:
        block = hashlib.md5(block + password + salt).digest()
        digest += block
    return digest[:key_len], digest[key_len:key_len + iv_len]


def encrypt(plaintext: str) -> str:
    salt = secrets.token_bytes(8)
    key, iv = _evp_bytes_to_key(CRYPTO_PASS, salt, 32, 16)
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return base64.b64encode(b"Salted__" + salt + enc).decode("ascii")


def decrypt(cipher_b64: str) -> str:
    raw = base64.b64decode(cipher_b64)
    if raw[:8] != b"Salted__":
        raise ValueError("unsupported payload")
    key, iv = _evp_bytes_to_key(CRYPTO_PASS, raw[8:16], 32, 16)
    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:]), AES.block_size).decode("utf-8")


def protect(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"cipher": encrypt(json.dumps(payload, ensure_ascii=False)), "iv": secrets.token_hex(15), "protect": True}


def unprotect(payload: Any) -> Any:
    if not isinstance(payload, dict) or not payload.get("protect"):
        return payload
    cipher = payload.get("cipher")
    if not cipher:
        return payload
    try:
        return json.loads(decrypt(cipher))
    except Exception:
        return payload


# ----------------------------------------------------------------------------- client
class ApplayMarket:
    def __init__(self, *, store_key: str, api_base: str, web_base: str, corridor_id: str,
                 known_action_ids: List[str], default_position: Tuple[float, float], use_api_auth: bool):
        self.key = store_key
        self.api = api_base.rstrip("/")
        self.web = web_base.rstrip("/")
        self.corridor_id = corridor_id
        self.known_action_ids = [a for a in known_action_ids if a]
        self.default_position = default_position
        self.use_api_auth = use_api_auth
        self.log = f"[{store_key}] "
        self.session = make_session({"Origin": self.web, "Referer": self.web + "/"})
        self.token: Optional[str] = None
        self.session_obj: Dict[str, Any] = {}
        self.store_id: Optional[str] = None

    # ---------------------------------------------------------------- token
    def _api_headers(self) -> Dict[str, str]:
        return {"x-access-token": self.token or "", "Content-Type": "application/json"}

    def _token_via_api_route(self) -> Optional[str]:
        try:
            r = self.session.post(f"{self.web}/api/auth", json={"url": self.api.split("/api2")[0] + "/"},
                                  headers={"Accept": "application/json"}, timeout=40)
            if r.status_code == 200:
                return (r.json() or {}).get("token") or None
        except Exception:
            pass
        return None

    def _token_via_action(self, action_id: str) -> Optional[str]:
        url = f"{self.web}/buscar?corredor={self.corridor_id}"
        tree = ('["",{"children":["pages",{"children":["search",{"children":["__PAGE__?{\\"corredor\\":\\"'
                f'{self.corridor_id}' '\\"}",{}]}]}]},null,null,true]')
        headers = {"Accept": "text/x-component", "Next-Action": action_id, "Next-Router-State-Tree": tree,
                   "Next-Url": "/pages/search", "Content-Type": "text/plain;charset=UTF-8"}
        try:
            r = self.session.post(url, headers=headers, data=json.dumps([self.api.split("/api2")[0] + "/"]).encode(),
                                  timeout=40)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        m = re.search(r'"token":"([^"]+)"', r.text)
        return m.group(1) if m else None

    def _action_ids_from_pages(self) -> List[str]:
        ids: List[str] = []
        for url in (f"{self.web}/buscar?corredor={self.corridor_id}", self.web + "/"):
            try:
                r = self.session.get(url, timeout=25)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            for a in re.findall(r"\$ACTION_ID_([a-f0-9]{40})", r.text):
                if a not in ids:
                    ids.append(a)
            # also scan the first JS chunks referenced by the page
            for chunk in re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', r.text)[:8]:
                try:
                    js = self.session.get(self.web + chunk, timeout=25)
                    for a in re.findall(r'createServerReference\)\("([a-f0-9]{40})"', js.text):
                        if a not in ids:
                            ids.append(a)
                except Exception:
                    continue
            if ids:
                break
        return ids

    def _action_ids_via_browser(self) -> List[str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return []
        ids: List[str] = []
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                page = b.new_page()
                page.on("request", lambda req: ids.append(req.headers.get("next-action", ""))
                        if req.method == "POST" and req.headers.get("next-action") else None)
                page.goto(f"{self.web}/buscar?corredor={self.corridor_id}", wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                html = page.content()
                ids.extend(re.findall(r"\$ACTION_ID_([a-f0-9]{40})", html))
                b.close()
        except Exception as exc:
            print(f"{self.log}browser action discovery failed: {exc}")
        return [a for a in dict.fromkeys(ids) if a]

    def _validate(self, token: str) -> bool:
        payload = {"device": {"browser": "chrome", "platform": "web", "uuid": f"probe-{self.key}", "ip_address": "",
                              "position": {"lat": 0.0, "lng": 0.0, "default": True}}, "session": None, "firstLoad": True}
        try:
            r = self.session.post(f"{self.api}/eauth/session", headers={"x-access-token": token, "Content-Type": "application/json"},
                                  data=json.dumps(protect(payload), ensure_ascii=False), timeout=20)
            return r.status_code == 200
        except Exception:
            return False

    def get_token(self) -> str:
        env_token = os.getenv(f"{self.key.upper()}_ACCESS_TOKEN")
        candidates: List[Tuple[str, Optional[str]]] = []
        if env_token:
            candidates.append(("env", env_token))
        if self.use_api_auth:
            candidates.append(("api/auth", self._token_via_api_route()))
        for aid in self.known_action_ids:
            candidates.append((f"action {aid[:8]}", self._token_via_action(aid)))
        for label, tok in candidates:
            if tok and self._validate(tok):
                print(f"{self.log}token ok via {label}")
                self.token = tok
                return tok
        for aid in self._action_ids_from_pages():
            tok = self._token_via_action(aid)
            if tok and self._validate(tok):
                print(f"{self.log}token ok via discovered action {aid[:8]} (add it to known_action_ids)")
                self.token = tok
                return tok
        for aid in self._action_ids_via_browser():
            tok = self._token_via_action(aid)
            if tok and self._validate(tok):
                print(f"{self.log}token ok via browser-discovered action {aid[:8]}")
                self.token = tok
                return tok
        raise RuntimeError(f"{self.key}: could not obtain an API token (site deploy changed the server action id)")

    # ---------------------------------------------------------------- session
    def open_session(self, zip_code: str) -> Tuple[Dict[str, Any], Optional[str]]:
        zdigits = normalize_zip(zip_code)
        coords = zip_coords(zdigits, self.session) or self.default_position
        device = {"browser": "chrome", "platform": "web", "uuid": f"{self.key}-{zdigits or 'default'}", "ip_address": "",
                  "position": {"lat": coords[0], "lng": coords[1], "default": False}, "cep": zdigits or None, "zip_code": zdigits or None}
        payload = {"device": device, "session": None, "firstLoad": True}
        r = self.session.post(f"{self.api}/eauth/session", headers=self._api_headers(),
                              data=json.dumps(protect(payload), ensure_ascii=False), timeout=40)
        r.raise_for_status()
        session_obj = unprotect((r.json() or {}).get("data") or {})
        if not isinstance(session_obj, dict):
            raise RuntimeError(f"{self.key}: invalid session payload")
        # explicit position update so the backend re-resolves the nearest store
        try:
            sid = session_obj.get("session")
            if sid:
                pos = {"device": device, "session": sid, "newPosition": {"latitude": coords[0], "longitude": coords[1], "default": False}}
                r2 = self.session.post(f"{self.api}/eauth/device_position", headers=self._api_headers(),
                                       data=json.dumps(protect(pos), ensure_ascii=False), timeout=40)
                if r2.status_code == 200:
                    refreshed = unprotect((r2.json() or {}).get("data") or {})
                    if isinstance(refreshed, dict) and (refreshed.get("session") or refreshed.get("loja")):
                        session_obj.update(refreshed)
        except Exception:
            pass
        loja = session_obj.get("loja") or {}
        store_id = str(loja.get("id") or loja.get("numero") or loja.get("_id") or "") or None
        self.session_obj, self.store_id = session_obj, store_id
        return session_obj, store_id

    def save_store(self, db, zip_code: str) -> str:
        loja = self.session_obj.get("loja") or {}
        store_id = self.store_id or f"{self.key}:default"
        end = loja.get("end") if isinstance(loja.get("end"), dict) else {}
        db.save_store_info(
            store_id, query_zip=format_zip(zip_code), name=loja.get("nome") or loja.get("name"),
            address=", ".join(str(p) for p in [loja.get("endereco") or end.get("endereco"), loja.get("numero") or end.get("numero"),
                                               loja.get("bairro") or end.get("bairro")] if p) or None,
            city=loja.get("cidade") or end.get("cidade"), state=loja.get("uf") or end.get("uf"),
            store_zip=loja.get("cep") or end.get("cep"),
            latitude=loja.get("latitude") or loja.get("lat") or end.get("latitude"),
            longitude=loja.get("longitude") or loja.get("lng") or end.get("longitude"),
            payload={k: v for k, v in loja.items() if not isinstance(v, (list, dict))} if loja else None,
        )
        print(f"{self.log}store {store_id} {loja.get('nome') or loja.get('name')} ({loja.get('cidade') or end.get('cidade')})")
        return store_id

    # ---------------------------------------------------------------- products
    def offer(self, p: Dict[str, Any], store_id: str, category: Optional[str]) -> Optional[Dict[str, Any]]:
        pid = str(p.get("_id") or p.get("sku") or "").strip()
        barcode = first_gtin(p.get("material"), p.get("sku"), p.get("ean"), p.get("gtin"), allow_gtin8=False)
        img = p.get("img")
        if isinstance(img, str) and img.startswith("/"):
            img = f"{self.web}{img}"
        uri = str(p.get("uri") or "").strip()
        if uri.startswith("http"):
            url = uri
        elif uri and "/" not in uri.strip("/") and "?" not in uri:
            from urllib.parse import quote
            url = f"{self.web}/?produto={quote(uri.strip('/'), safe='_-~.')}"
        elif uri:
            url = f"{self.web}/{uri.lstrip('/')}"
        else:
            url = None
        de, por = p.get("de"), p.get("por")
        return make_offer(
            product_id=pid, store_id=store_id, product_name=p.get("descricao"),
            regular_price=de if de not in (None, 0, "0") else por, promo_price=por,
            barcode=barcode, barcode_source="inline",
            brand=p.get("marca") or p.get("fabricante") or p.get("brand"),
            category_path=category or p.get("departamento") or p.get("categoria"),
            unit=p.get("unidadeMedidaExibicao") or p.get("peso"), stock=p.get("estoque"),
            is_available=(int(float(p.get("estoque"))) > 0) if p.get("estoque") not in (None, "") else None,
            product_url=url, image_url=img,
        )

    def _post_products(self, endpoint: str, payload: Dict[str, Any], *, encrypted: bool) -> Optional[Dict[str, Any]]:
        body = json.dumps(protect(payload) if encrypted else payload, ensure_ascii=False)
        for attempt in range(3):
            try:
                r = self.session.post(f"{self.api}/{endpoint}", headers=self._api_headers(), data=body, timeout=90)
            except requests.RequestException:
                time.sleep(3)
                continue
            if r.status_code in (401, 403):
                print(f"{self.log}auth expired - refreshing token/session")
                self.get_token()
                self.open_session(self._zip)
                payload["session"] = self.session_obj if "session" in payload and isinstance(payload["session"], dict) else payload.get("session")
                body = json.dumps(protect(payload) if encrypted else payload, ensure_ascii=False)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            try:
                decoded = unprotect((r.json() or {}).get("data") or {})
            except ValueError:
                return None
            return decoded if isinstance(decoded, dict) else None
        return None

    def iter_products(self, *, endpoint: str = "enav/produtos", query: Optional[Dict[str, Any]] = None,
                      encrypted: bool = True, extra: Optional[Dict[str, Any]] = None, max_pages: int = 120):
        """Yield product dicts, paginating with the SKU-exclusion list."""
        seen_skus: List[str] = []
        seen_set: set = set()
        total = None
        for page in range(1, max_pages + 1):
            payload: Dict[str, Any] = {"session": self.session_obj, "query": query or {}, "config": {"skus": seen_skus or None}}
            if extra:
                payload.update(extra)
            decoded = self._post_products(endpoint, payload, encrypted=encrypted)
            if not decoded:
                return
            products = decoded.get("produtos") or []
            if not products:
                return
            if total is None:
                total = decoded.get("totalProdutos")
            new = 0
            for p in products:
                if not isinstance(p, dict):
                    continue
                sku = str(p.get("sku") or p.get("_id") or "")
                if sku and sku not in seen_set:
                    seen_set.add(sku)
                    seen_skus.append(sku)
                    new += 1
                    yield p
            if new == 0 or (isinstance(total, int) and len(seen_set) >= total):
                return
            time.sleep(0.1)

    # Department names are not listed by any endpoint any more (the old
    # `departamentos` key of enav/produtos is gone). We seed with the names seen
    # on the sites and learn new ones from the `departamento` field of every
    # product we fetch (see scrape(): a department discovered on the way is
    # queued too).
    SEED_DEPARTMENTS = ["Mercearia", "Bebidas", "Hortifruti", "Limpeza", "Higiene e Beleza", "Frios", "Açougue",
                        "Padaria", "Pet", "Congelados", "Utilidades", "Peixaria", "Bazar", "Bebê", "Laticínios",
                        "Frios e Laticínios", "Carnes", "Perfumaria", "Adega", "Eletro", "Automotivo", "Papelaria"]

    def departments(self) -> List[str]:
        decoded = self._post_products("enav/produtos", {"session": self.session_obj, "query": {}, "config": {"skus": None}},
                                      encrypted=True) or {}
        deps = [d.strip() for d in (decoded.get("departamentos") or []) if isinstance(d, str) and d.strip()]
        for p in decoded.get("produtos") or []:
            if isinstance(p, dict) and isinstance(p.get("departamento"), str) and p["departamento"].strip():
                deps.append(p["departamento"].strip())
        deps.extend(self.SEED_DEPARTMENTS)
        return list(dict.fromkeys(deps))

    # ---------------------------------------------------------------- scrape
    def scrape(self, db, zip_code: str, limit: Optional[int], *, strategy: str) -> Dict[str, int]:
        self._zip = zip_code
        self.get_token()
        self.open_session(zip_code)
        store_id = self.save_store(db, zip_code)
        seen: set = set()
        total = {"upserted": 0, "skipped": 0, "with_barcode": 0}

        discovered: List[str] = []

        def consume(products, category: Optional[str]) -> int:
            batch = []
            for p in products:
                dep = p.get("departamento") if isinstance(p, dict) else None
                if isinstance(dep, str) and dep.strip() and dep.strip() not in discovered:
                    discovered.append(dep.strip())
                offer = self.offer(p, store_id, category)
                if offer and offer["product_id"] not in seen:
                    seen.add(offer["product_id"])
                    batch.append(offer)
                    if len(batch) >= 200:
                        r = db.save(batch)
                        for k in total:
                            total[k] += r[k]
                        batch = []
                if limit and len(seen) >= limit:
                    break
            if batch:
                r = db.save(batch)
                for k in total:
                    total[k] += r[k]
            return len(seen)

        if strategy == "corridor" and self.corridor_id:
            # X: the "corredor" is a curated list (~800 items); the real catalogue
            # is only reachable per department, so we do both.
            before = len(seen)
            consume(self.iter_products(endpoint="enav/produtos_corredor", encrypted=False,
                                       extra={"id": self.corridor_id}, query={}), None)
            print(f"{self.log}corridor +{len(seen) - before} (total {len(seen)})")
            strategy = "departments"

        if strategy == "departments":
            queue = self.departments()
            done_deps: set = set()
            print(f"{self.log}{len(queue)} departments to try (new ones are discovered on the way)")
            while queue:
                dep = queue.pop(0)
                key = dep.casefold()
                if key in done_deps:
                    continue
                done_deps.add(key)
                before = len(seen)
                consume(self.iter_products(query={"departamento": dep}), dep)
                if len(seen) > before:
                    print(f"{self.log}{dep[:35]:<35} +{len(seen) - before:>5} (total {len(seen)})")
                for d in discovered:
                    if d.casefold() not in done_deps and d not in queue:
                        queue.append(d)
                if limit and len(seen) >= limit:
                    break
        return total
