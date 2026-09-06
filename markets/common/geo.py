"""
geo.py - ZIP (CEP) helpers shared by store-resolution code.

  * normalize_zip("08032-230") -> "08032230"
  * format_zip("08032230")     -> "08032-230"
  * zip_info(zip)              -> {"city","state","street","neighborhood"} via ViaCEP
  * zip_coords(zip)            -> (lat, lon) via BrasilAPI (fast) then ViaCEP+Nominatim
  * haversine_km(lat1, lon1, lat2, lon2)
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import requests

from .http import make_session


def normalize_zip(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def format_zip(value: object) -> str:
    d = normalize_zip(value)
    return f"{d[:5]}-{d[5:]}" if len(d) == 8 else str(value or "")


def zip_info(zip_code: str, session: Optional[requests.Session] = None) -> Dict[str, str]:
    d = normalize_zip(zip_code)
    if len(d) != 8:
        return {}
    s = session or make_session()
    try:
        r = s.get(f"https://viacep.com.br/ws/{d}/json/", timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json() or {}
        if data.get("erro"):
            return {}
        return {
            "city": (data.get("localidade") or "").strip(),
            "state": (data.get("uf") or "").strip().upper(),
            "street": (data.get("logradouro") or "").strip(),
            "neighborhood": (data.get("bairro") or "").strip(),
        }
    except Exception:
        return {}


def zip_coords(zip_code: str, session: Optional[requests.Session] = None) -> Optional[Tuple[float, float]]:
    d = normalize_zip(zip_code)
    if len(d) != 8:
        return None
    s = session or make_session()
    # 1. BrasilAPI has coordinates for most ZIPs
    try:
        r = s.get(f"https://brasilapi.com.br/api/cep/v2/{d}", timeout=10)
        if r.status_code == 200:
            coords = ((r.json() or {}).get("location") or {}).get("coordinates") or {}
            lat, lon = coords.get("latitude"), coords.get("longitude")
            if lat is not None and lon is not None:
                return float(lat), float(lon)
    except Exception:
        pass
    # 2. ViaCEP address -> Nominatim
    info = zip_info(d, s)
    queries = []
    if info:
        queries.append(", ".join(p for p in [info.get("street"), info.get("neighborhood"),
                                             info.get("city"), info.get("state"), "Brasil"] if p))
        queries.append(", ".join(p for p in [info.get("city"), info.get("state"), "Brasil"] if p))
    queries.append(f"{d[:5]}-{d[5:]}, Brasil")
    for q in queries:
        try:
            r = s.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1, "countrycodes": "br"},
                headers={"User-Agent": "markets-db-geocoder/2.0"},
                timeout=10,
            )
            if r.status_code == 200:
                rows = r.json() or []
                if rows:
                    return float(rows[0]["lat"]), float(rows[0]["lon"])
        except Exception:
            continue
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def to_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
