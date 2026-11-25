"""Client for the Blupath gateway export (GatewayService.exportLoads).

POST https://portal.blupath.com/fireWorkFlow
  headers: service, operation, accessToken
  body:    {"dateFrom": "YYYY-MM-DD", "dateTo": "YYYY-MM-DD"}   (filters on first pickup planned date)
  reply:   {"response": "ok", "result": [ {load}, ... ]}
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import BlupathConfig

log = logging.getLogger(__name__)


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=5, status_forcelist=(429, 502, 503, 504),
                  allowed_methods=None, respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class BlupathClient:
    def __init__(self, cfg: BlupathConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or _session()
        self.session.headers.update({
            "service": cfg.service,
            "operation": cfg.operation,
            "accessToken": cfg.access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def fetch(self, date_from: date, date_to: date) -> list[dict]:
        """One API call for an inclusive pickup-date range."""
        resp = self.session.post(
            self.cfg.url,
            json={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
            timeout=self.cfg.timeout,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Blupath rejected the access token ({resp.status_code}). Check BLUPATH_ACCESS_TOKEN.")
        if resp.status_code >= 400:
            raise RuntimeError(f"Blupath API error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if not isinstance(body, dict) or body.get("response") != "ok" or not isinstance(body.get("result"), list):
            raise RuntimeError(f"Unexpected Blupath response: {str(body)[:300]}")
        return body["result"]

    def iter_loads(self, date_from: date, date_to: date) -> Iterator[dict]:
        """All loads with first pickup between date_from and date_to (inclusive), in chunks."""
        start = date_from
        while start <= date_to:
            end = min(start + timedelta(days=self.cfg.chunk_days - 1), date_to)
            recs = self.fetch(start, end)
            log.info("Blupath %s..%s: %d loads", start, end, len(recs))
            yield from recs
            start = end + timedelta(days=1)
