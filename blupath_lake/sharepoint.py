"""Minimal SharePoint (Microsoft Graph) file store used as the data lake backend."""
from __future__ import annotations

import logging
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import SharePointConfig

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
CHUNK = 5 * 1024 * 1024  # multiple of 320 KiB as Graph requires


class SharePointStore:
    def __init__(self, cfg: SharePointConfig):
        self.cfg = cfg
        self.s = requests.Session()
        retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=None, respect_retry_after_header=True)
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self.s.headers["Authorization"] = f"Bearer {self._token()}"
        self.drive_id = self._drive_id()

    def _token(self) -> str:
        r = requests.post(
            f"https://login.microsoftonline.com/{self.cfg.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Azure AD token request failed ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]

    def _check(self, r: requests.Response) -> requests.Response:
        if r.status_code >= 400:
            raise RuntimeError(f"Graph {r.request.method} {r.status_code}: {r.text[:300]}")
        return r

    def _drive_id(self) -> str:
        site = self._check(self.s.get(f"{GRAPH}/sites/{self.cfg.hostname}:{self.cfg.site_path}", timeout=30)).json()
        drives = self._check(self.s.get(f"{GRAPH}/sites/{site['id']}/drives", timeout=30)).json()["value"]
        for d in drives:
            if d["name"] == self.cfg.drive_name:
                return d["id"]
        raise RuntimeError(f"Library '{self.cfg.drive_name}' not found. Available: {[d['name'] for d in drives]}")

    def _item(self, path: str) -> str:
        full = f"{self.cfg.root_folder}/{path.lstrip('/')}"
        return f"{GRAPH}/drives/{self.drive_id}/root:/{quote(full)}"

    def read(self, path: str) -> bytes | None:
        """Read a file inside the lake root folder."""
        return self._get_content(self._item(path))

    def read_from_library(self, path: str) -> bytes | None:
        """Read a file by its path from the document library root (outside the lake folder)."""
        return self._get_content(f"{GRAPH}/drives/{self.drive_id}/root:/{quote(path.strip('/'))}")

    def _get_content(self, item_url: str) -> bytes | None:
        r = self.s.get(item_url + ":/content", timeout=300)
        if r.status_code == 404:
            return None
        return self._check(r).content

    def write(self, path: str, data: bytes) -> None:
        """Upload, creating intermediate folders automatically. Overwrites existing files."""
        if len(data) <= SIMPLE_UPLOAD_LIMIT:
            self._check(self.s.put(self._item(path) + ":/content", data=data, timeout=120))
        else:
            sess = self._check(self.s.post(
                self._item(path) + ":/createUploadSession",
                json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}, timeout=30,
            )).json()
            upload_url, total = sess["uploadUrl"], len(data)
            for start in range(0, total, CHUNK):
                chunk = data[start:start + CHUNK]
                end = start + len(chunk) - 1
                # Upload URL is pre-authorised; must NOT send the Graph bearer token.
                r = requests.put(upload_url, data=chunk, timeout=300, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                })
                self._check(r)
        log.info("Wrote %s (%d bytes)", path, len(data))
