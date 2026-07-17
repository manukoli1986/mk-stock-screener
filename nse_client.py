"""Polite NSE India API client.

NSE's public JSON endpoints sit behind bot protection: they need the cookies a
browser picks up on the homepage, and they rate-limit aggressive callers. This
client manages that: one shared session warmed up on nseindia.com, browser-like
headers, a minimum gap between requests, retry with session refresh on
401/403/empty bodies, and a per-endpoint cache so the app never hammers NSE.
Stale cache is served if NSE is temporarily unreachable.
"""

import threading
import time

import requests

BASE = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NSEClient:
    def __init__(self, min_interval=1.0):
        self._lock = threading.Lock()
        self._session = None
        self._session_born = 0.0
        self._last_request = 0.0
        self._min_interval = min_interval
        self._cache = {}  # path -> (ts, data)

    def _fresh_session(self):
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get(BASE, timeout=10)  # homepage visit sets the anti-bot cookies
        self._session_born = time.time()
        return s

    def get(self, path, ttl=60):
        """GET a /api/... path, JSON-decoded. Returns None only if NSE is
        unreachable and nothing is cached."""
        with self._lock:
            cached = self._cache.get(path)
            if cached and time.time() - cached[0] < ttl:
                return cached[1]

            for attempt in range(3):
                wait = self._min_interval - (time.time() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                try:
                    # NSE cookies go stale after a few minutes idle
                    if self._session is None or time.time() - self._session_born > 300:
                        self._session = self._fresh_session()
                    r = self._session.get(BASE + path, timeout=15)
                    self._last_request = time.time()
                    if r.status_code in (401, 403) or not r.text.strip():
                        self._session = None
                        time.sleep(1 + attempt)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    self._cache[path] = (time.time(), data)
                    return data
                except (requests.RequestException, ValueError):
                    self._session = None
                    self._last_request = time.time()
                    time.sleep(1 + attempt)

            return cached[1] if cached else None


nse = NSEClient()
