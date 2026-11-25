"""Configuration, read entirely from environment variables (Azure Function App settings).

Nothing secret is hard-coded. In Azure, point the secret settings at Key Vault, e.g.
BLUPATH_API_KEY = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/blupath-api-key/)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if isinstance(value, str):
        value = value.strip()
    if required and not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value or default


@dataclass
class BlupathConfig:
    base_url: str
    loads_path: str = "/loads"
    # Authentication: bearer | header | query | basic | none
    auth_type: str = "bearer"
    api_key: str | None = None
    api_key_header: str = "x-api-key"
    api_key_param: str = "api_key"
    username: str | None = None
    password: str | None = None
    # Date-window filter sent to the API (names of the query params)
    from_param: str | None = "updatedFrom"
    to_param: str | None = "updatedTo"
    date_format: str = "iso"  # iso (2026-11-25T21:00:00Z) | date (2026-11-25)
    # Pagination: none | page | offset | cursor | next_link
    pagination: str = "page"
    page_param: str = "page"
    page_start: int = 1
    page_size_param: str = "pageSize"
    page_size: int = 500
    offset_param: str = "offset"
    cursor_param: str = "cursor"
    cursor_key: str = "nextCursor"       # dotted path in the response body
    next_link_key: str = "next"          # dotted path in the response body
    records_key: str = "data"            # dotted path to the list of loads; "" = body is a list
    id_field: str = "id"                 # unique load identifier
    extra_params: dict = field(default_factory=dict)
    timeout: int = 60

    @classmethod
    def from_env(cls) -> "BlupathConfig":
        return cls(
            base_url=_env("BLUPATH_BASE_URL", required=True).rstrip("/"),
            loads_path=_env("BLUPATH_LOADS_PATH", "/loads"),
            auth_type=_env("BLUPATH_AUTH_TYPE", "bearer").lower(),
            api_key=_env("BLUPATH_API_KEY"),
            api_key_header=_env("BLUPATH_API_KEY_HEADER", "x-api-key"),
            api_key_param=_env("BLUPATH_API_KEY_PARAM", "api_key"),
            username=_env("BLUPATH_USERNAME"),
            password=_env("BLUPATH_PASSWORD"),
            from_param=_env("BLUPATH_FROM_PARAM", "updatedFrom") or None,
            to_param=_env("BLUPATH_TO_PARAM", "updatedTo") or None,
            date_format=_env("BLUPATH_DATE_FORMAT", "iso").lower(),
            pagination=_env("BLUPATH_PAGINATION", "page").lower(),
            page_param=_env("BLUPATH_PAGE_PARAM", "page"),
            page_start=int(_env("BLUPATH_PAGE_START", "1")),
            page_size_param=_env("BLUPATH_PAGE_SIZE_PARAM", "pageSize"),
            page_size=int(_env("BLUPATH_PAGE_SIZE", "500")),
            offset_param=_env("BLUPATH_OFFSET_PARAM", "offset"),
            cursor_param=_env("BLUPATH_CURSOR_PARAM", "cursor"),
            cursor_key=_env("BLUPATH_CURSOR_KEY", "nextCursor"),
            next_link_key=_env("BLUPATH_NEXT_LINK_KEY", "next"),
            records_key=os.environ.get("BLUPATH_RECORDS_KEY", "data").strip(),
            id_field=_env("BLUPATH_ID_FIELD", "id"),
            extra_params=json.loads(_env("BLUPATH_EXTRA_PARAMS", "{}")),
            timeout=int(_env("BLUPATH_TIMEOUT", "60")),
        )


@dataclass
class SharePointConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    hostname: str          # e.g. yourcompany.sharepoint.com
    site_path: str         # e.g. /sites/DataLake
    drive_name: str = "Documents"
    root_folder: str = "Blupath Data Lake"

    @classmethod
    def from_env(cls) -> "SharePointConfig":
        return cls(
            tenant_id=_env("SP_TENANT_ID", required=True),
            client_id=_env("SP_CLIENT_ID", required=True),
            client_secret=_env("SP_CLIENT_SECRET", required=True),
            hostname=_env("SP_HOSTNAME", required=True),
            site_path=_env("SP_SITE_PATH", required=True),
            drive_name=_env("SP_DRIVE_NAME", "Documents"),
            root_folder=_env("SP_ROOT_FOLDER", "Blupath Data Lake").strip("/"),
        )


# Overlap re-fetched on every run so late-arriving updates aren't missed.
LOOKBACK_HOURS = int(os.environ.get("LAKE_LOOKBACK_HOURS", "24"))
# Window used on the very first run (no watermark yet).
INITIAL_LOOKBACK_DAYS = int(os.environ.get("LAKE_INITIAL_LOOKBACK_DAYS", "30"))
