"""Configuration, read from environment variables (Azure Function App settings).

Nothing secret is hard-coded. In Azure, point secret settings at Key Vault, e.g.
BLUPATH_ACCESS_TOKEN = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/blupath-access-token/)
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = (os.environ.get(name) or "").strip()
    if required and not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value or default


@dataclass
class BlupathConfig:
    access_token: str
    url: str = "https://portal.blupath.com/fireWorkFlow"
    service: str = "BlupathService"
    operation: str = "GatewayService.exportLoads"
    chunk_days: int = 31      # max pickup-date range per API request
    timeout: int = 300
    id_field: str = "blupathId"

    @classmethod
    def from_env(cls) -> "BlupathConfig":
        return cls(
            access_token=_env("BLUPATH_ACCESS_TOKEN", required=True),
            url=_env("BLUPATH_URL", cls.url),
            service=_env("BLUPATH_SERVICE", cls.service),
            operation=_env("BLUPATH_OPERATION", cls.operation),
            chunk_days=int(_env("BLUPATH_CHUNK_DAYS", "31")),
            timeout=int(_env("BLUPATH_TIMEOUT", "300")),
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


# Nightly refresh window, in pickup dates relative to today (SAST). Loads keep changing
# (status, POD, invoice) well after pickup, and Blupath has no "last updated" filter,
# so every night re-pulls this whole window and updates the lake.
DAYS_BACK = int(os.environ.get("LAKE_DAYS_BACK", "60"))
DAYS_FORWARD = int(os.environ.get("LAKE_DAYS_FORWARD", "30"))
