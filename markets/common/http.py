"""
http.py - requests.Session factory with sane defaults, retry/backoff helpers
and thread-local sessions for parallel fetches.

Learned the hard way (see docs/LESSONS.md):
  * Always send a real browser User-Agent + pt-BR Accept-Language.
  * 429 / 5xx must back off exponentially; hammering just gets the IP banned.
  * Cloudflare "Just a moment..." pages come back as HTTP 200/403 with HTML -
    treat a non-JSON body as a soft failure, not a crash.
  * Use one Session per thread (requests.Session is not thread-safe).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_session(headers: Optional[Dict[str, str]] = None, *, json_accept: bool = True) -> requests.Session:
    s = requests.Session()
    base = {
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": (
            "application/json, text/plain, */*"
            if json_accept
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
    }
    if headers:
        base.update(headers)
    s.headers.update({k: v for k, v in base.items() if v is not None})
    for k, v in base.items():
        if v is None:
            s.headers.pop(k, None)
    return s


class ThreadSessions:
    """Thread-local session pool: `pool.get()` returns this thread's Session."""

    def __init__(self, headers: Optional[Dict[str, str]] = None, json_accept: bool = True,
                 cookies: Optional[Dict[str, str]] = None):
        self._local = threading.local()
        self._headers = dict(headers or {})
        self._json_accept = json_accept
        self._cookies = dict(cookies or {})

    def get(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = make_session(self._headers, json_accept=self._json_accept)
            for k, v in self._cookies.items():
                s.cookies.set(k, v)
            self._local.session = s
        return s


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    timeout: float = 30.0,
    log_prefix: str = "",
    **kwargs: Any,
) -> Optional[requests.Response]:
    """
    Perform an HTTP request, retrying on network errors and RETRY_STATUSES with
    exponential backoff (honours Retry-After). Returns the final Response, or
    None when every attempt failed with a network error.
    """
    delay = base_delay
    last: Optional[requests.Response] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt >= max_attempts:
                print(f"{log_prefix}request error ({exc.__class__.__name__}) after {attempt} attempts: {url[:120]}")
                return last
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
            continue
        last = resp
        if resp.status_code in RETRY_STATUSES:
            if attempt >= max_attempts:
                return resp
            retry_after = resp.headers.get("Retry-After")
            wait = delay
            if retry_after and retry_after.isdigit():
                wait = max(wait, float(retry_after))
            print(f"{log_prefix}HTTP {resp.status_code} (attempt {attempt}/{max_attempts}) - waiting {wait:.0f}s")
            time.sleep(wait)
            delay = min(delay * 2, max_delay)
            continue
        return resp
    return last


def get_json(session: requests.Session, url: str, *, params: Any = None, headers: Any = None,
             timeout: float = 30.0, max_attempts: int = 5, log_prefix: str = "") -> Optional[Any]:
    """GET and decode JSON. Returns None on HTTP error or non-JSON body."""
    resp = request_with_retry(session, "GET", url, params=params, headers=headers,
                              timeout=timeout, max_attempts=max_attempts, log_prefix=log_prefix)
    return decode_json(resp)


def post_json(session: requests.Session, url: str, *, json_body: Any = None, data: Any = None,
              headers: Any = None, timeout: float = 30.0, max_attempts: int = 5,
              log_prefix: str = "") -> Optional[Any]:
    resp = request_with_retry(session, "POST", url, json=json_body, data=data, headers=headers,
                              timeout=timeout, max_attempts=max_attempts, log_prefix=log_prefix)
    return decode_json(resp)


def decode_json(resp: Optional[requests.Response]) -> Optional[Any]:
    if resp is None or resp.status_code not in (200, 206):
        return None
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def looks_like_challenge(text: str) -> bool:
    """Detect Cloudflare / captcha interstitial pages."""
    low = (text or "")[:4000].lower()
    return "just a moment" in low or "cf-challenge" in low or "<title>captcha</title>" in low
