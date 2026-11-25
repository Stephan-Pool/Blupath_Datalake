"""Daily Blupath loads ingestion into a layered data lake.

Lake layout (inside the SharePoint root folder):

  raw/loads/YYYY/MM/DD/loads_<run-timestamp>.json     exact API records (bronze, immutable)
  curated/loads/YYYY/MM/DD/loads_YYYY-MM-DD.parquet    flattened loads changed that day (silver)
  gold/loads_master.parquet                            one row per load, latest version (gold)
  gold/loads_master.csv                                same, for Excel / Power BI
  _state/watermark.json                                last successful window end
  _state/run_log.csv                                   one line per run
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .config import INITIAL_LOOKBACK_DAYS, LOOKBACK_HOURS

log = logging.getLogger(__name__)
SAST = ZoneInfo("Africa/Johannesburg")
WATERMARK = "_state/watermark.json"
RUN_LOG = "_state/run_log.csv"
MASTER_PARQUET = "gold/loads_master.parquet"
MASTER_CSV = "gold/loads_master.csv"


class Store(Protocol):
    def read(self, path: str) -> bytes | None: ...
    def write(self, path: str, data: bytes) -> None: ...


class LoadSource(Protocol):
    cfg: object
    def iter_loads(self, start: datetime, end: datetime): ...


@dataclass
class RunResult:
    window_start: datetime
    window_end: datetime
    fetched: int
    master_rows: int
    files: list[str]


def _normalise(records: list[dict], ingested_at: datetime) -> pd.DataFrame:
    df = pd.json_normalize(records, sep="_")
    for col in df.columns:
        if df[col].dtype == object:
            # Nested lists/dicts -> JSON text; everything else -> string. Keeps Parquet
            # writes stable when the API returns mixed types across days.
            df[col] = df[col].map(
                lambda v: None if v is None or (isinstance(v, float) and pd.isna(v))
                else json.dumps(v, default=str) if isinstance(v, (list, dict))
                else str(v)
            )
    df["_ingested_at"] = ingested_at.isoformat()
    return df


def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _window(store: Store, now: datetime) -> tuple[datetime, datetime]:
    raw = store.read(WATERMARK)
    if raw:
        last = datetime.fromisoformat(json.loads(raw)["window_end"])
        start = last - timedelta(hours=LOOKBACK_HOURS)
    else:
        start = now - timedelta(days=INITIAL_LOOKBACK_DAYS)
    return start, now


def run(store: Store, source: LoadSource, start: datetime | None = None,
        end: datetime | None = None, now: datetime | None = None) -> RunResult:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if start is None or end is None:
        start, end = _window(store, now)
    id_field = source.cfg.id_field
    log.info("Fetching Blupath loads updated %s -> %s", start.isoformat(), end.isoformat())

    records = list(source.iter_loads(start, end))
    day = now.astimezone(SAST)
    part = day.strftime("%Y/%m/%d")
    stamp = day.strftime("%Y%m%dT%H%M%S")
    files: list[str] = []

    # Bronze: always keep the untouched API payload
    raw_path = f"raw/loads/{part}/loads_{stamp}.json"
    store.write(raw_path, json.dumps({
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "fetched_at": now.isoformat(), "count": len(records), "records": records,
    }, default=str).encode())
    files.append(raw_path)

    master_rows = 0
    if records:
        df = _normalise(records, now)
        if id_field not in df.columns:
            raise RuntimeError(
                f"Load ID field '{id_field}' not in API data. Columns: {list(df.columns)[:30]}. "
                "Set BLUPATH_ID_FIELD."
            )
        df[id_field] = df[id_field].astype(str)

        # Silver: daily partition (merge with any earlier run the same day)
        silver_path = f"curated/loads/{part}/loads_{day:%Y-%m-%d}.parquet"
        existing = store.read(silver_path)
        silver = df if not existing else pd.concat([pd.read_parquet(io.BytesIO(existing)), df], ignore_index=True)
        silver = silver.drop_duplicates(subset=[id_field], keep="last")
        store.write(silver_path, _parquet(silver))
        files.append(silver_path)

        # Gold: latest version of every load ever seen
        existing = store.read(MASTER_PARQUET)
        master = df if not existing else pd.concat([pd.read_parquet(io.BytesIO(existing)), df], ignore_index=True)
        master = master.drop_duplicates(subset=[id_field], keep="last").reset_index(drop=True)
        master_rows = len(master)
        store.write(MASTER_PARQUET, _parquet(master))
        store.write(MASTER_CSV, master.to_csv(index=False).encode("utf-8-sig"))
        files += [MASTER_PARQUET, MASTER_CSV]
    else:
        log.info("No loads changed in window")

    # Only advance the watermark after every write above has succeeded
    store.write(WATERMARK, json.dumps({"window_end": end.isoformat(), "updated_at": now.isoformat()}).encode())
    prior = store.read(RUN_LOG) or b"run_at_sast,window_start_utc,window_end_utc,loads_fetched,master_rows\n"
    line = f"{day:%Y-%m-%d %H:%M:%S},{start.isoformat()},{end.isoformat()},{len(records)},{master_rows}\n"
    store.write(RUN_LOG, prior + line.encode())

    log.info("Done: %d loads fetched, master has %d loads", len(records), master_rows)
    return RunResult(start, end, len(records), master_rows, files)
