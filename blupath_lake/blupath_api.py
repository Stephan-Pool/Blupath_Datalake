"""Client for the Blupath loads API. Auth, filters and pagination are all config-driven."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import BlupathConfig

log = logging.getLogger(__name__)
MAX_PAGES = 10_000


def dig(obj: Any, dotted: str) -> Any:
    """Follow a dotted path ('result.items') into nested dicts. '' returns obj."""
    if not dotted:
        return obj
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",), respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


class BlupathClient:
    def __init__(self, cfg: BlupathConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or _session()
        self.session.headers["Accept"] = "application/json"
        if cfg.auth_type == "bearer" and cfg.api_key:
            self.session.headers["Authorization"] = f"Bearer {cfg.api_key}"
        elif cfg.auth_type == "header" and cfg.api_key:
            self.session.headers[cfg.api_key_header] = cfg.api_key
        elif cfg.auth_type == "basic":
            self.session.auth = (cfg.username or "", cfg.password or "")

    def _fmt(self, dt: datetime) -> str:
        dt = dt.astimezone(timezone.utc)
        if self.cfg.date_format == "date":
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _get(self, url: str, params: dict | None) -> Any:
        if self.cfg.auth_type == "query" and self.cfg.api_key:
            params = {**(params or {}), self.cfg.api_key_param: self.cfg.api_key}
        resp = self.session.get(url, params=params, timeout=self.cfg.timeout)
        if resp.status_code >= 400:
            # Never log params: they may contain the API key (query auth).
            raise RuntimeError(f"Blupath API {resp.status_code} on {resp.url.split('?')[0]}: {resp.text[:300]}")
        return resp.json()

    def _records(self, body: Any) -> list[dict]:
        recs = dig(body, self.cfg.records_key) if isinstance(body, dict) else body
        if recs is None:
            recs = []
        if not isinstance(recs, list):
            raise RuntimeError(
                f"Expected a list of loads at BLUPATH_RECORDS_KEY='{self.cfg.records_key}', got {type(recs).__name__}"
            )
        return recs

    def iter_loads(self, start: datetime, end: datetime) -> Iterator[dict]:
        cfg = self.cfg
        url = urljoin(cfg.base_url + "/", cfg.loads_path.lstrip("/"))
        base = dict(cfg.extra_params)
        if cfg.from_param:
            base[cfg.from_param] = self._fmt(start)
        if cfg.to_param:
            base[cfg.to_param] = self._fmt(end)

        mode = cfg.pagination
        page, offset, cursor = cfg.page_start, 0, None
        next_url: str | None = url
        for _ in range(MAX_PAGES):
            params = dict(base)
            if mode in ("page", "offset", "cursor"):
                params[cfg.page_size_param] = cfg.page_size
            if mode == "page":
                params[cfg.page_param] = page
            elif mode == "offset":
                params[cfg.offset_param] = offset
            elif mode == "cursor" and cursor:
                params[cfg.cursor_param] = cursor

            if mode == "next_link" and next_url != url:
                body = self._get(next_url, None)  # next link already carries the query
            else:
                body = self._get(url, params)
            recs = self._records(body)
            yield from recs

            if mode == "none":
                return
            if mode == "page":
                if len(recs) < cfg.page_size:
                    return
                page += 1
            elif mode == "offset":
                if len(recs) < cfg.page_size:
                    return
                offset += len(recs)
            elif mode == "cursor":
                cursor = dig(body, cfg.cursor_key) if isinstance(body, dict) else None
                if not cursor or not recs:
                    return
            elif mode == "next_link":
                link = dig(body, cfg.next_link_key) if isinstance(body, dict) else None
                if not link or not recs:
                    return
                next_url = urljoin(url, link)
            else:
                raise RuntimeError(f"Unknown BLUPATH_PAGINATION '{mode}'")
        raise RuntimeError("Pagination did not terminate; check BLUPATH_PAGINATION settings")
