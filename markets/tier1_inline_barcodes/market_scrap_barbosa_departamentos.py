import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from db.db_manager import DatabaseManager


class BarbosaScraper:
    API_BASE = "https://api-barbosa.applay.tech/api2/ecommerce"
    WEB_BASE = "https://www.barbosasupermercados.com.br"
    CRYPTO_PASS = "BEWAREOBLIVIONISATHAND"

    TOKEN_ACTION_ID = os.getenv(
        "BARBOSA_TOKEN_ACTION_ID",
        "bfb8781927026ab6b741b817d0c1ebd49281b720",
    )
    PREVIOUS_TOKEN_ACTION_ID = "cdd7f568183fa8c8873f4ad115a43ed1ef0a473a"
    LEGACY_TOKEN_ACTION_ID = "b5240a22b66e2990db00381bcd0e987be41e7f34"
    DEFAULT_CORRIDOR_ID = os.getenv("BARBOSA_DEFAULT_CORRIDOR_ID", "")

    DEFAULT_POSITION = (-23.506567, -46.601181)

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
        self.market_name = "Barbosa"
        self._working_token_action_id: Optional[str] = None

    @staticmethod
    def _is_truthy(raw: Optional[str]) -> bool:
        value = str(raw or "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _candidate_token_action_ids(self) -> List[str]:
        candidates: List[str] = []
        env_list = os.getenv("BARBOSA_TOKEN_ACTION_IDS", "")
        for raw in str(env_list).split(","):
            value = raw.strip()
            if value:
                candidates.append(value)

        candidates.extend(
            [
                self._working_token_action_id,
                self.TOKEN_ACTION_ID,
                self.PREVIOUS_TOKEN_ACTION_ID,
                self.LEGACY_TOKEN_ACTION_ID,
            ]
        )

        deduped: List[str] = []
        seen = set()
        for action_id in candidates:
            cleaned = str(action_id or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return deduped

    @staticmethod
    def _normalize_zip(zip_code: str) -> str:
        return "".join(ch for ch in str(zip_code or "") if ch.isdigit())

    def _resolve_lat_lng_from_cep_api(self, normalized_zip: str) -> Optional[Tuple[float, float]]:
        if len(normalized_zip) != 8:
            return None
        try:
            url = f"https://cep.awesomeapi.com.br/json/{normalized_zip}"
            response = self.session.get(url, timeout=12)
            if response.status_code != 200:
                return None
            payload = response.json() or {}
            lat = self._to_float(payload.get("lat"))
            lng = self._to_float(payload.get("lng"))
            if lat is None or lng is None:
                return None
            return lat, lng
        except Exception:
            return None

    @staticmethod
    def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int) -> Tuple[bytes, bytes]:
        digest = b""
        block = b""
        while len(digest) < key_len + iv_len:
            block = hashlib.md5(block + password + salt).digest()
            digest += block
        return digest[:key_len], digest[key_len : key_len + iv_len]

    @classmethod
    def _cryptojs_encrypt(cls, plaintext: str) -> str:
        salt = secrets.token_bytes(8)
        key, iv = cls._evp_bytes_to_key(cls.CRYPTO_PASS.encode("utf-8"), salt, 32, 16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
        raw = b"Salted__" + salt + encrypted
        return base64.b64encode(raw).decode("ascii")

    @classmethod
    def _cryptojs_decrypt(cls, ciphertext_b64: str) -> str:
        raw = base64.b64decode(ciphertext_b64)
        if raw[:8] != b"Salted__":
            raise ValueError("Unsupported encrypted payload format")
        salt = raw[8:16]
        encrypted = raw[16:]
        key, iv = cls._evp_bytes_to_key(cls.CRYPTO_PASS.encode("utf-8"), salt, 32, 16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = unpad(cipher.decrypt(encrypted), AES.block_size)
        return plaintext.decode("utf-8")

    @classmethod
    def _protect_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "cipher": cls._cryptojs_encrypt(json.dumps(payload, ensure_ascii=False)),
            "iv": secrets.token_hex(15),
            "protect": True,
        }

    @classmethod
    def _extract_protected_payload(cls, payload: Any) -> Any:
        if not isinstance(payload, dict) or not payload.get("protect"):
            return payload
        cipher_text = payload.get("cipher")
        if not cipher_text:
            return payload
        decrypted_text = cls._cryptojs_decrypt(cipher_text)
        return json.loads(decrypted_text)

    def _resolve_lat_lng(self, zip_code: str) -> Tuple[float, float]:
        normalized_zip = self._normalize_zip(zip_code)
        if len(normalized_zip) != 8:
            return self.DEFAULT_POSITION

        try:
            cep_coords = self._resolve_lat_lng_from_cep_api(normalized_zip)
            if cep_coords:
                return cep_coords

            via_cep_url = f"https://viacep.com.br/ws/{normalized_zip}/json/"
            via_cep = self.session.get(via_cep_url, timeout=12)
            if via_cep.status_code != 200:
                return self.DEFAULT_POSITION

            address = via_cep.json()
            if address.get("erro"):
                return self.DEFAULT_POSITION

            street = address.get("logradouro") or ""
            neighborhood = address.get("bairro") or ""
            city = address.get("localidade") or ""
            state = address.get("uf") or ""
            query_parts = [part for part in [street, neighborhood, city, state, "Brasil"] if part]
            query = ", ".join(query_parts)
            fallback_query = ", ".join(part for part in [city, state, "Brasil"] if part)

            nominatim_headers = {
                "User-Agent": "markets-db-barbosa-scraper/1.0",
                "Accept": "application/json",
            }

            for candidate in [query, fallback_query]:
                if not candidate:
                    continue
                geo = self.session.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": candidate, "format": "json", "limit": 1},
                    headers=nominatim_headers,
                    timeout=15,
                )
                if geo.status_code != 200:
                    continue
                rows = geo.json() or []
                if not rows:
                    continue
                first = rows[0]
                lat = float(first.get("lat"))
                lng = float(first.get("lon"))
                return lat, lng
        except Exception:
            return self.DEFAULT_POSITION

        return self.DEFAULT_POSITION

    @staticmethod
    def _extract_token_from_action_response(content: str) -> Optional[str]:
        marker = '"token":"'
        start = str(content or "").find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = content.find('"', start)
        if end < 0:
            return None
        return content[start:end]

    def _build_token_request_headers(self, corridor_id: str, action_id: str) -> Dict[str, str]:
        tree = (
            "[\"\",{\"children\":[\"pages\",{\"children\":[\"search\","
            "{\"children\":[\"__PAGE__?{\\\"corredor\\\":\\\""
            f"{corridor_id}"
            "\\\"}\",{}]}]}]},null,null,true]"
        )
        url = f"{self.WEB_BASE}/buscar?corredor={corridor_id}"
        return {
            "Accept": "text/x-component",
            "Next-Action": action_id,
            "Next-Router-State-Tree": tree,
            "Next-Url": "/pages/search",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": self.WEB_BASE,
            "Referer": url,
        }

    def _fetch_token_from_api_route(self) -> Optional[str]:
        """Primary token method: POST /api/auth (Next.js API route, no server action needed)."""
        response = self.session.post(
            f"{self.WEB_BASE}/api/auth",
            headers={"Accept": "application/json, text/plain, */*", "Content-Type": "application/json"},
            json={"url": "https://api-barbosa.applay.tech/"},
            timeout=40,
        )
        response.raise_for_status()
        body = response.json() or {}
        return body.get("token") or None

    def _request_access_token(self, corridor_id: str, action_id: str) -> Optional[str]:
        """Legacy server-action POST (kept as fallback; primary is _fetch_token_from_api_route)."""
        url = f"{self.WEB_BASE}/buscar?corredor={corridor_id}"
        headers = self._build_token_request_headers(corridor_id, action_id)
        response = self.session.post(
            url,
            headers=headers,
            data='["https://api-barbosa.applay.tech/"]'.encode("utf-8"),
            timeout=40,
        )
        response.raise_for_status()
        return self._extract_token_from_action_response(response.text)

    def _discover_action_ids_from_page(self, corridor_id: str) -> List[str]:
        """Scan the page HTML and linked JS chunks for Next.js server-action IDs."""
        _ACTION_PATTERNS = [
            r'\$ACTION_ID_([a-f0-9]{40})',
            r'\$ACTION_REF_([a-f0-9]{40})',
            r'"next-action"\s*:\s*"([a-f0-9]{40})"',
            r'Next-Action["\s:,]+([a-f0-9]{40})',
        ]

        def _extract_ids(text: str) -> List[str]:
            found: List[str] = []
            for pattern in _ACTION_PATTERNS:
                found.extend(re.findall(pattern, text, re.IGNORECASE))
            seen: set = set()
            return [x for x in found if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

        page_urls = []
        if corridor_id:
            page_urls.append(f"{self.WEB_BASE}/buscar?corredor={corridor_id}")
        page_urls.append(f"{self.WEB_BASE}/")

        for page_url in page_urls:
            try:
                r = self.session.get(page_url, timeout=20)
                if r.status_code != 200:
                    continue
                html = r.text

                # Check inline HTML first
                ids = _extract_ids(html)
                if ids:
                    return ids

                # Also scan the first few _next/static JS chunk URLs referenced in the page
                chunk_paths = re.findall(r'/_next/static/chunks/[^"\'>\s]+\.js', html)
                for chunk_path in chunk_paths[:8]:
                    try:
                        chunk_url = f"{self.WEB_BASE}{chunk_path}"
                        cr = self.session.get(chunk_url, timeout=15)
                        if cr.status_code == 200:
                            ids = _extract_ids(cr.text)
                            if ids:
                                return ids
                    except Exception:
                        continue
            except Exception:
                continue
        return []

    def _validate_token(self, token: str) -> bool:
        """Probe the session endpoint to verify the token is accepted by the API."""
        try:
            device = {
                "browser": "chrome",
                "platform": "web",
                "uuid": "probe-token-barbosa",
                "ip_address": "",
                "position": {"lat": 0.0, "lng": 0.0, "default": True},
            }
            payload = {"device": device, "session": None, "firstLoad": True}
            headers = {
                "x-access-token": token,
                "Content-Type": "application/json",
            }
            response = self.session.post(
                f"{self.API_BASE}/eauth/session",
                headers=headers,
                data=json.dumps(self._protect_payload(payload), ensure_ascii=False),
                timeout=15,
            )
            return response.status_code == 200
        except Exception:
            return False

    def _get_access_token_via_browser(self, corridor_id: str) -> Optional[str]:
        """Use Playwright to discover the current Next-Action ID, then replay the POST.

        Strategy 1: intercept a server-action POST if the page triggers one automatically.
        Strategy 2: if no POST fires, scan the fully-rendered page HTML for $ACTION_ID_ patterns.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(f"Playwright unavailable for token recovery: {exc}") from exc

        target_url = (
            f"{self.WEB_BASE}/buscar?corredor={corridor_id}"
            if corridor_id
            else f"{self.WEB_BASE}/"
        )
        discovered_action_id: Optional[str] = None
        last_error: Optional[str] = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                _action_patterns = [
                    r'\$ACTION_ID_([a-f0-9]{40})',
                    r'\$ACTION_REF_([a-f0-9]{40})',
                    r'"next-action"\s*:\s*"([a-f0-9]{40})"',
                ]

                def _find_in_text(text: str) -> Optional[str]:
                    for pat in _action_patterns:
                        m = re.search(pat, text, re.IGNORECASE)
                        if m:
                            return m.group(1)
                    return None

                # Strategy 1: intercept any server-action POST to the domain
                try:
                    with page.expect_request(
                        lambda req: req.method == "POST"
                            and req.url.startswith(self.WEB_BASE)
                            and bool((req.headers or {}).get("next-action")),
                        timeout=20000,
                    ) as req_info:
                        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    discovered_action_id = (req_info.value.headers or {}).get("next-action")
                except Exception:
                    # Strategy 2: scan the rendered HTML for action ID patterns
                    try:
                        if page.url in ("", "about:blank"):
                            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                        discovered_action_id = _find_in_text(page.content())
                        if not discovered_action_id:
                            last_error = "no action ID pattern found in rendered HTML"
                    except Exception as exc2:
                        last_error = str(exc2)
            except Exception as exc:
                last_error = str(exc)
            finally:
                browser.close()

        if not discovered_action_id:
            raise RuntimeError(
                f"Browser token capture failed: could not discover action ID. "
                f"Last error: {last_error or 'unknown'}"
            )

        # Replay the POST via our own HTTP session now that we have the valid action ID
        try:
            token = self._request_access_token(corridor_id, discovered_action_id)
        except Exception as exc:
            raise RuntimeError(
                f"Browser captured action_id={discovered_action_id[:12]}… "
                f"but token request failed: {exc}"
            ) from exc

        if token:
            self._working_token_action_id = discovered_action_id
            return token
        raise RuntimeError(
            f"Browser captured action_id={discovered_action_id[:12]}… "
            f"but token endpoint returned no token"
        )

    def _get_access_token(self, corridor_id: str) -> str:
        last_error: Optional[str] = None
        already_tried: set = set()

        def _try_action_id(action_id: str) -> Optional[str]:
            nonlocal last_error
            if not action_id or action_id in already_tried:
                return None
            already_tried.add(action_id)
            for attempt in range(1, 4):
                try:
                    token = self._request_access_token(corridor_id, action_id)
                    if not token and attempt < 3:
                        time.sleep(0.6)
                        continue
                    if not token:
                        last_error = f"action {action_id}: token marker not found"
                        return None
                    if not self._validate_token(token):
                        last_error = f"action {action_id}: token obtained but API validation failed"
                        return None
                    self._working_token_action_id = action_id
                    return token
                except Exception as exc:
                    last_error = f"action {action_id}: {exc}"
                    if attempt < 3:
                        time.sleep(0.6)
                    continue
            return None

        # 1. Primary: /api/auth route (no server actions, no corridor ID needed)
        for attempt in range(1, 4):
            try:
                token = self._fetch_token_from_api_route()
                if token and self._validate_token(token):
                    return token
                if token:
                    last_error = "/api/auth returned token but API validation failed"
                else:
                    last_error = "/api/auth returned no token"
                if attempt < 3:
                    time.sleep(0.6)
            except Exception as exc:
                last_error = f"/api/auth attempt {attempt}: {exc}"
                if attempt < 3:
                    time.sleep(0.6)

        # 2. Known/cached server-action IDs
        for action_id in self._candidate_token_action_ids():
            token = _try_action_id(action_id)
            if token:
                return token

        # 3. Discover server-action IDs from page/chunks (fast HTTP-only fallback)
        try:
            for action_id in self._discover_action_ids_from_page(corridor_id):
                token = _try_action_id(action_id)
                if token:
                    return token
        except Exception as exc:
            last_error = f"page/chunk action discovery: {exc}"

        # 4. Browser fallback: intercept action request or scan rendered DOM
        try:
            browser_token = self._get_access_token_via_browser(corridor_id)
            if browser_token:
                if self._validate_token(browser_token):
                    return browser_token
                last_error = "browser token obtained but API validation failed"
        except Exception as exc:
            last_error = str(exc)

        raise RuntimeError(
            "Could not resolve Barbosa API token. "
            f"Last attempt error: {last_error or 'unknown'}"
        )

    def _open_session(self, zip_code: str, access_token: str) -> Tuple[Dict[str, Any], Optional[str]]:
        lat, lng = self._resolve_lat_lng(zip_code)
        normalized_zip = self._normalize_zip(zip_code)
        device = {
            "browser": "chrome",
            "platform": "web",
            "uuid": f"barbosa-{normalized_zip or 'default'}",
            "ip_address": "",
            # Keep position non-default so backend can resolve nearest store by CEP/coords.
            "position": {"lat": lat, "lng": lng, "default": False},
            "cep": normalized_zip or None,
            "zip_code": normalized_zip or None,
        }

        payload = {"device": device, "session": None, "firstLoad": True}
        headers = {
            "x-access-token": access_token,
            "Content-Type": "application/json",
        }
        response = self.session.post(
            f"{self.API_BASE}/eauth/session",
            headers=headers,
            data=json.dumps(self._protect_payload(payload), ensure_ascii=False),
            timeout=40,
        )
        response.raise_for_status()

        body = response.json()
        session_obj = self._extract_protected_payload((body or {}).get("data") or {})
        if not isinstance(session_obj, dict):
            raise RuntimeError("Barbosa returned an invalid session payload")

        loja = session_obj.get("loja") or {}
        store_id = None
        if isinstance(loja, dict):
            store_id = loja.get("id") or loja.get("numero")
            if store_id is not None:
                store_id = str(store_id)

        # Mirror storefront behavior: after creating a session, send an explicit
        # device position update so backend can re-resolve nearest store.
        try:
            refreshed = self._update_device_position(
                session_obj=session_obj,
                device=device,
                lat=lat,
                lng=lng,
                access_token=access_token,
            )
            if isinstance(refreshed, dict) and (
                refreshed.get("session") is not None or refreshed.get("loja") is not None
            ):
                merged = dict(session_obj)
                merged.update(refreshed)
                session_obj = merged
                loja = (session_obj or {}).get("loja") or {}
                if isinstance(loja, dict):
                    updated_store_id = loja.get("id") or loja.get("numero")
                    if updated_store_id is not None:
                        store_id = str(updated_store_id)
        except Exception:
            pass

        # Pickup flow: list stores and force nearest store to CEP position.
        # This mirrors the "retirada" user journey where a store is chosen.
        try:
            if self._is_truthy(os.getenv("BARBOSA_PICKUP_NEAREST_ENABLED", "1")):
                stores = self._list_pickup_stores(access_token, session_obj)
                selected = self._select_nearest_store(stores, lat=lat, lng=lng)
                if selected:
                    selected_store, selected_distance_km = selected
                    session_obj = self._apply_selected_store_to_session(session_obj, selected_store)
                    selected_store_id = selected_store.get("_id") or selected_store.get("id")
                    if selected_store_id is not None:
                        store_id = str(selected_store_id)
                    self._log_selected_pickup_store(
                        zip_code=zip_code,
                        store=selected_store,
                        distance_km=selected_distance_km,
                    )
        except Exception:
            pass

        return session_obj, store_id

    def _list_pickup_stores(self, access_token: str, session_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        session_id = (session_obj or {}).get("session")
        if not session_id:
            return []

        payload = {"session": session_id}
        headers = {
            "x-access-token": access_token,
            "Content-Type": "application/json",
        }
        response = self.session.post(
            f"{self.API_BASE}/enav/listar_lojas",
            headers=headers,
            data=json.dumps(self._protect_payload(payload), ensure_ascii=False),
            timeout=40,
        )
        if response.status_code != 200:
            return []

        body = response.json() or {}
        if not body.get("status"):
            return []

        decoded = self._extract_protected_payload((body.get("data") or {}))
        if isinstance(decoded, list):
            return [row for row in decoded if isinstance(row, dict)]
        if isinstance(decoded, dict):
            lojas = decoded.get("lojas")
            if isinstance(lojas, list):
                return [row for row in lojas if isinstance(row, dict)]
        return []

    @staticmethod
    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        r = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lng2 - lng1)
        a = (
            math.sin(d_phi / 2.0) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
        )
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return r * c

    def _store_lat_lng(self, store: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        def _to_float(value: Any) -> Optional[float]:
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        end = store.get("end") or {}
        lat = _to_float(store.get("latitude") or store.get("lat") or end.get("latitude") or end.get("lat"))
        lng = _to_float(
            store.get("longitude") or store.get("lng") or end.get("longitude") or end.get("lng")
        )
        return lat, lng

    def _select_nearest_store(
        self,
        stores: List[Dict[str, Any]],
        *,
        lat: float,
        lng: float,
    ) -> Optional[Tuple[Dict[str, Any], Optional[float]]]:
        if not stores:
            return None

        ranked: List[Tuple[float, Dict[str, Any]]] = []
        for store in stores:
            store_lat, store_lng = self._store_lat_lng(store)
            if store_lat is None or store_lng is None:
                distancia = self._to_float(store.get("distancia") or store.get("distance"))
                if distancia is not None:
                    ranked.append((distancia, store))
                continue
            ranked.append((self._haversine_km(lat, lng, store_lat, store_lng), store))

        if not ranked:
            return stores[0], None

        ranked.sort(key=lambda item: item[0])
        nearest_distance, nearest_store = ranked[0]
        return nearest_store, nearest_distance

    def _log_selected_pickup_store(
        self,
        *,
        zip_code: str,
        store: Dict[str, Any],
        distance_km: Optional[float],
    ) -> None:
        store_id = store.get("_id") or store.get("id") or store.get("numero")
        name = store.get("nome") or store.get("name") or "N/A"
        end = store.get("end") or {}
        city = store.get("cidade") or (end.get("cidade") if isinstance(end, dict) else None) or "N/A"
        state = store.get("uf") or (end.get("uf") if isinstance(end, dict) else None) or "N/A"
        distance_label = "unknown" if distance_km is None else f"{distance_km:.2f} km"
        print(
            "Barbosa pickup store selected: "
            f"cep={zip_code} store_id={store_id} name={name} city={city}/{state} distance={distance_label}"
        )

    @staticmethod
    def _apply_selected_store_to_session(
        session_obj: Dict[str, Any],
        selected_store: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(session_obj or {})
        merged["loja"] = selected_store
        merged["modality"] = "retirada"
        return merged

    def _update_device_position(
        self,
        *,
        session_obj: Dict[str, Any],
        device: Dict[str, Any],
        lat: float,
        lng: float,
        access_token: str,
    ) -> Optional[Dict[str, Any]]:
        session_id = (session_obj or {}).get("session")
        if not session_id:
            return None

        payload = {
            "device": device,
            "session": session_id,
            "newPosition": {
                "latitude": lat,
                "longitude": lng,
                "default": False,
            },
        }
        headers = {
            "x-access-token": access_token,
            "Content-Type": "application/json",
        }
        response = self.session.post(
            f"{self.API_BASE}/eauth/device_position",
            headers=headers,
            data=json.dumps(self._protect_payload(payload), ensure_ascii=False),
            timeout=40,
        )
        if response.status_code != 200:
            return None

        body = response.json() or {}
        decoded = self._extract_protected_payload((body.get("data") or {}))
        if isinstance(decoded, dict):
            return decoded
        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_product_url(self, product_uri: Any) -> Optional[str]:
        uri = str(product_uri or "").strip()
        if not uri:
            return None

        if uri.startswith("http"):
            return uri

        if "?" in uri:
            if uri.startswith("/"):
                return f"{self.WEB_BASE}{uri}"
            return f"{self.WEB_BASE}/{uri}"

        # Barbosa PDP resolves by query-string slug (?produto=<slug>).
        slug = uri.strip("/")
        if slug and "/" not in slug:
            return f"{self.WEB_BASE}/?produto={quote(slug, safe='_-~.')}"

        if uri.startswith("/"):
            return f"{self.WEB_BASE}{uri}"
        return f"{self.WEB_BASE}/{uri}"

    def _standardize_product(
        self,
        product: Dict[str, Any],
        zip_code: str,
        store_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        sku = product.get("sku")
        doc_id = product.get("_id")
        product_id = str(doc_id or sku or "").strip()
        if not product_id:
            return None

        material = str(product.get("material") or "").strip()
        sku_text = str(sku or "").strip()

        gtin_text = None
        if material.isdigit() and len(material) in (8, 12, 13, 14):
            gtin_text = material
        elif sku_text.isdigit() and len(sku_text) in (8, 12, 13, 14):
            gtin_text = sku_text

        barcode = self.db.normalize_barcode(gtin_text)

        de = self._to_float(product.get("de"))
        por = self._to_float(product.get("por"))
        if de is None:
            de = por
        if por is None:
            por = de

        # Barbosa can return regular and current price equal; keep promo only when
        # there is an actual discount so downstream analytics are cleaner.
        promo_price = por if (de is not None and por is not None and por < de) else None

        clube_flag = bool(product.get("clube"))
        offer_tag = "CLUBE BARBOSA" if clube_flag else None
        offer_name = "Clube Barbosa" if clube_flag else None

        image_url = product.get("img")
        if image_url and isinstance(image_url, str) and image_url.startswith("/"):
            image_url = f"{self.WEB_BASE}{image_url}"

        product_uri = product.get("uri")
        product_url = self._build_product_url(product_uri)

        offer_id = self.db.build_offer_id("barbosa", store_id, barcode, gtin_text, product.get("descricao"))
        if not offer_id:
            return None

        return {
            "id": offer_id,
            "product_name": product.get("descricao"),
            "brand": product.get("marca") or product.get("fabricante"),
            "description": (product.get("descricaoComplementar") or product.get("descricaoSEO") or "").strip() or None,
            "regular_price": de,
            "promo_price": promo_price,
            "promo_min_quantity": None,
            "unit": product.get("unidadeMedidaExibicao") or product.get("peso"),
            "gtin": gtin_text,
            "barcode": barcode,
            "product_url": product_url,
            "image_url": image_url,
            "stock_balance": product.get("estoque"),
            "stock_general": None,
            "sold_quantity": None,
            "promo_end_at": None,
            "last_updated": datetime.now().isoformat(),
            "store_id": store_id,
            "zip_code": zip_code,
            "offer_name": offer_name,
            "offer_tag": offer_tag,
            "app_membership_required": clube_flag,
        }

    def _cache_store_metadata(self, zip_code: str, session_obj: Dict[str, Any], store_id: Optional[str]) -> None:
        if not store_id:
            return

        loja = (session_obj or {}).get("loja") if isinstance(session_obj, dict) else {}
        if not isinstance(loja, dict):
            loja = {}
        loja_end = loja.get("end") or {}
        if not isinstance(loja_end, dict):
            loja_end = {}

        address_parts = [
            str(loja.get("endereco") or loja_end.get("endereco") or "").strip(),
            str(loja.get("numero") or loja_end.get("numero") or "").strip(),
            str(loja.get("bairro") or loja_end.get("bairro") or "").strip(),
        ]
        self.db.cache_store_id(
            zip_code,
            self.market_name,
            str(store_id),
            store_name=loja.get("nome") or loja.get("name"),
            store_address=", ".join(part for part in address_parts if part) or None,
            store_city=loja.get("cidade") or loja_end.get("cidade"),
            store_state=loja.get("uf") or loja_end.get("uf"),
            latitude=loja.get("latitude") or loja.get("lat"),
            longitude=loja.get("longitude") or loja.get("lng"),
            store_payload=loja,
        )

    def _bootstrap_session(self, zip_code: str, corridor_id: str) -> Tuple[str, Dict[str, Any], Optional[str]]:
        access_token = self._get_access_token(corridor_id)
        session_obj, resolved_store_id = self._open_session(zip_code, access_token)
        return access_token, session_obj, resolved_store_id

    def _fetch_departamento_products(
        self,
        *,
        zip_code: str,
        departamento: str,
        session_obj: Dict[str, Any],
        store_id: Optional[str],
        access_token: str,
        limit: Optional[int] = None,
        max_pages: int = 80,
    ) -> List[Dict[str, Any]]:
        max_items = limit if isinstance(limit, int) and limit > 0 else None
        headers = {
            "x-access-token": access_token,
            "Content-Type": "application/json",
        }

        products_by_id: Dict[str, Dict[str, Any]] = {}
        seen_skus: List[str] = []
        seen_skus_set = set()
        total_products = None
        page = 1
        # Cap the seen_skus cursor sent to the API to avoid request payload bloat.
        # The API uses this list to skip already-seen products; keeping only the
        # most recent window is enough for the pagination cursor to advance.
        _SEEN_SKUS_WINDOW = 500

        while page <= max_pages:
            payload = {
                "session": session_obj,
                "query": {"departamento": departamento},
                "config": {
                    "skus": seen_skus[-_SEEN_SKUS_WINDOW:] if seen_skus else None,
                },
            }
            try:
                response = self.session.post(
                    f"{self.API_BASE}/enav/produtos",
                    headers=headers,
                    data=json.dumps(self._protect_payload(payload), ensure_ascii=False),
                    timeout=60,
                )

                if response.status_code in (401, 403):
                    if not hasattr(self, "_auth_refresh_count"):
                        self._auth_refresh_count = 0
                    self._auth_refresh_count += 1
                    if self._auth_refresh_count > 3:
                        print(
                            f"Barbosa departamento='{departamento}' page {page}: "
                            "auth refresh limit reached, stopping."
                        )
                        break
                    print(
                        f"Barbosa departamento='{departamento}' page {page}: "
                        f"auth expired, refreshing token/session (attempt {self._auth_refresh_count})..."
                    )
                    access_token, session_obj, resolved_store_id = self._bootstrap_session(
                        zip_code,
                        self.DEFAULT_CORRIDOR_ID,
                    )
                    if resolved_store_id:
                        store_id = resolved_store_id
                        self._cache_store_metadata(zip_code, session_obj, store_id)
                    headers["x-access-token"] = access_token
                    continue

                if response.status_code != 200:
                    print(
                        f"Barbosa departamento='{departamento}' page {page}: "
                        f"HTTP {response.status_code}, stopping"
                    )
                    break

                body = response.json() or {}
                decoded = self._extract_protected_payload(body.get("data") or {})
                if not isinstance(decoded, dict):
                    print(
                        f"Barbosa departamento='{departamento}' page {page}: "
                        "invalid payload, stopping"
                    )
                    break

                products = decoded.get("produtos") or []
                if not isinstance(products, list) or not products:
                    if page == 1:
                        print(f"Barbosa departamento='{departamento}': no products")
                    else:
                        print(f"Barbosa departamento='{departamento}' page {page}: no more products")
                    break

                if total_products is None:
                    total_products = decoded.get("totalProdutos")

                added = 0
                page_skus: List[str] = []
                for raw_product in products:
                    if not isinstance(raw_product, dict):
                        continue

                    normalized = self._standardize_product(raw_product, zip_code, store_id)
                    if not normalized:
                        continue

                    products_by_id[normalized["id"]] = normalized
                    added += 1
                    if max_items is not None and len(products_by_id) >= max_items:
                        break

                    sku = raw_product.get("sku")
                    if sku is not None:
                        sku_text = str(sku)
                        if sku_text and sku_text not in seen_skus_set:
                            seen_skus_set.add(sku_text)
                            seen_skus.append(sku_text)
                            page_skus.append(sku_text)

                print(
                    f"Barbosa departamento='{departamento}' page {page}: {added} products "
                    f"(unique_total={len(products_by_id)})"
                )

                if not page_skus:
                    break
                if isinstance(total_products, int) and len(products_by_id) >= total_products:
                    break
                if max_items is not None and len(products_by_id) >= max_items:
                    break

                page += 1
                time.sleep(0.15)
            except Exception as exc:
                print(f"Barbosa departamento='{departamento}' page {page}: error {exc}")
                break

        rows = list(products_by_id.values())
        if max_items is not None:
            rows = rows[:max_items]
        return rows

    def fetch_offers_from_departamentos(
        self,
        zip_code: str,
        departamentos: List[str],
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        normalized_zip = self._normalize_zip(zip_code)
        if len(normalized_zip) == 8:
            zip_code = f"{normalized_zip[:5]}-{normalized_zip[5:]}"
        max_items = limit if isinstance(limit, int) and limit > 0 else None

        if not departamentos:
            return []

        cached_store_id = self.db.get_store_id(zip_code, self.market_name)
        try:
            access_token, session_obj, resolved_store_id = self._bootstrap_session(
                zip_code,
                self.DEFAULT_CORRIDOR_ID,
            )
        except Exception as exc:
            print(f"Barbosa: departamento bootstrap failed: {exc}")
            return []

        store_id = resolved_store_id or cached_store_id
        self._cache_store_metadata(zip_code, session_obj, store_id)

        dedup_departamentos: List[str] = []
        seen_departamentos = set()
        for departamento in departamentos:
            cleaned = str(departamento or "").strip()
            key = cleaned.casefold()
            if not cleaned or key in seen_departamentos:
                continue
            seen_departamentos.add(key)
            dedup_departamentos.append(cleaned)

        products_by_id: Dict[str, Dict[str, Any]] = {}
        for departamento in dedup_departamentos:
            remaining = None
            if max_items is not None:
                remaining = max(0, max_items - len(products_by_id))
                if remaining <= 0:
                    break

            rows = self._fetch_departamento_products(
                zip_code=zip_code,
                departamento=departamento,
                session_obj=session_obj,
                store_id=store_id,
                access_token=access_token,
                limit=remaining,
            )
            for row in rows:
                products_by_id[row["id"]] = row
                if max_items is not None and len(products_by_id) >= max_items:
                    break

            print(
                f"Barbosa departamento='{departamento}': "
                f"fetched={len(rows)} global_total={len(products_by_id)}"
            )
            if max_items is not None and len(products_by_id) >= max_items:
                break

        all_products = list(products_by_id.values())
        if max_items is not None:
            all_products = all_products[:max_items]
        print(f"Barbosa departamentos: {len(all_products)} offers collected.")
        return all_products


class BarbosaDepartamentosScraper(BarbosaScraper):
    """Departamento-oriented scraper for Barbosa Supermercados."""

    def resolve_store(self, zip_code: str) -> Optional[str]:
        """Return the store_id for the given ZIP — used by the skip-recent-market check."""
        try:
            _, resolved_store_id = self._open_session(
                zip_code, self._get_access_token(self.DEFAULT_CORRIDOR_ID)
            )
            return resolved_store_id
        except Exception:
            return None

    def discover_catalog_targets(self, zip_code: str) -> List[str]:
        corridor_id = self.DEFAULT_CORRIDOR_ID
        token = self._get_access_token(corridor_id)
        session_obj, _ = self._open_session(zip_code, token)

        departamentos: List[str] = []
        headers = {
            "x-access-token": token,
            "Content-Type": "application/json",
        }

        # Get departamentos list from empty products query
        produtos_payload = {
            "session": session_obj,
            "query": {},
            "config": {"skus": None},
        }
        try:
            produtos_response = self.session.post(
                f"{self.API_BASE}/enav/produtos",
                headers=headers,
                data=json.dumps(self._protect_payload(produtos_payload), ensure_ascii=False),
                timeout=60,
            )
            if produtos_response.status_code != 200:
                print(f"Barbosa discover_catalog_targets: produtos HTTP {produtos_response.status_code}")
            else:
                produtos_body = produtos_response.json() or {}
                produtos_decoded = self._extract_protected_payload((produtos_body.get("data") or {}))
                if isinstance(produtos_decoded, dict):
                    departamentos_raw = produtos_decoded.get("departamentos") or []
                    if isinstance(departamentos_raw, list):
                        for name in departamentos_raw:
                            if isinstance(name, str) and name.strip():
                                departamentos.append(name.strip())
                    print(f"Barbosa discover_catalog_targets: {len(departamentos)} depts from produtos endpoint")
        except Exception as exc:
            print(f"Barbosa discover_catalog_targets produtos error: {exc}")

        # Also scan home component banners for departamento links
        try:
            home_response = self.session.post(
                f"{self.API_BASE}/enav/home",
                headers=headers,
                data=json.dumps({"session": session_obj}, ensure_ascii=False),
                timeout=60,
            )
            if home_response.status_code == 200:
                home_body = home_response.json() or {}
                home_decoded = self._extract_protected_payload((home_body.get("data") or {}))
                if isinstance(home_decoded, dict):
                    for comp in (home_decoded.get("componentes") or []):
                        if not isinstance(comp, dict):
                            continue
                        for banner in (comp.get("banners") or []):
                            if not isinstance(banner, dict):
                                continue
                            link = banner.get("link") or ""
                            if "departamento=" not in link:
                                continue
                            parsed = urlparse(
                                link if "://" in link else f"{self.WEB_BASE}/{link.lstrip('/')}"
                            )
                            params = parse_qs(parsed.query)
                            for val in (params.get("departamento") or []):
                                text = str(val or "").strip()
                                if text:
                                    departamentos.append(text)
        except Exception:
            pass

        # Deduplicate preserving order
        dedup: List[str] = []
        seen: set = set()
        for name in departamentos:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                dedup.append(name)

        return dedup

    def fetch_offers(self, zip_code: str, limit: Optional[int] = None) -> List[Dict]:
        print(f"Fetching Barbosa departamentos offers for {zip_code}...")
        try:
            departamentos = self.discover_catalog_targets(zip_code)
            print(f"Barbosa departamentos: discovered {len(departamentos)} departamento labels")

            if departamentos:
                return self.fetch_offers_from_departamentos(
                    zip_code, departamentos=departamentos, limit=limit
                )

            print("Barbosa departamentos: no departamentos discovered from API")
        except Exception as exc:
            print(f"Barbosa departamentos: discovery failed ({exc})")

        return []


if __name__ == "__main__":
    scraper = BarbosaDepartamentosScraper()
    scraper.fetch_offers("08032-230")
