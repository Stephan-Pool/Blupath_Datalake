"""Nightly Blupath loads ingestion into a layered data lake.

Lake layout (inside the SharePoint root folder):

  raw/loads/YYYY/MM/DD/loads_<run time>.json.gz       exact API reply for each run (bronze, immutable)
  curated/loads/pickup_month=YYYY-MM/loads.parquet    loads by first-pickup month, latest version (silver)
  gold/loads_master.parquet / .csv                    every load, latest version, one row each (gold)
  gold/load_stops.parquet / .csv                      one row per stop with actual arrival/departure (gold)
  _state/run_log.csv                                  one line per run
"""
from __future__ import annotations

import gzip
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .config import DAYS_BACK, DAYS_FORWARD

log = logging.getLogger(__name__)
SAST = ZoneInfo("Africa/Johannesburg")
RUN_LOG = "_state/run_log.csv"
MASTER = "gold/loads_master"
STOPS = "gold/load_stops"
NESTED_LIST_COLS = ("stopActualTimes",)


class Store(Protocol):
    def read(self, path: str) -> bytes | None: ...
    def write(self, path: str, data: bytes) -> None: ...


class LoadSource(Protocol):
    cfg: object
    def iter_loads(self, date_from: date, date_to: date) -> Iterator[dict]: ...


@dataclass
class RunResult:
    date_from: date
    date_to: date
    fetched: int
    master_rows: int
    files: list[str]


def _stabilise(df: pd.DataFrame) -> pd.DataFrame:
    """Text-ify object columns (nested -> JSON) so Parquet writes don't break when Blupath
    sends a field as a number one day and a string the next (e.g. latitude, odometer)."""
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(
                lambda v: None if v is None or (isinstance(v, float) and pd.isna(v))
                else json.dumps(v, default=str) if isinstance(v, (list, dict))
                else str(v)
            )
    return df


def _pickup_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%d/%m/%Y", errors="coerce").dt.date


def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _read_parquet(store: Store, path: str) -> pd.DataFrame | None:
    data = store.read(path)
    return pd.read_parquet(io.BytesIO(data)) if data else None


def _upsert(old: pd.DataFrame | None, new: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    if old is None or old.empty:
        out = new
    else:
        out = pd.concat([old, new], ignore_index=True)
    return _stabilise(out.drop_duplicates(subset=key, keep="last").reset_index(drop=True))


def _stops(records: list[dict], id_field: str, ingested_at: str) -> pd.DataFrame:
    rows = []
    for r in records:
        for s in r.get("stopActualTimes") or []:
            rows.append({id_field: str(r.get(id_field)), "TmsId": r.get("TmsId"), **s, "_ingested_at": ingested_at})
    return _stabilise(pd.DataFrame(rows))


def default_window(now: datetime) -> tuple[date, date]:
    today = now.astimezone(SAST).date()
    return today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_FORWARD)


def run(store: Store, source: LoadSource, date_from: date | None = None,
        date_to: date | None = None, now: datetime | None = None) -> RunResult:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if date_from is None or date_to is None:
        date_from, date_to = default_window(now)
    id_field = source.cfg.id_field
    log.info("Fetching Blupath loads with first pickup %s..%s", date_from, date_to)

    records = list(source.iter_loads(date_from, date_to))
    local = now.astimezone(SAST)
    stamp = now.isoformat()
    files: list[str] = []

    # Bronze: the untouched API reply, gzipped
    raw_path = f"raw/loads/{local:%Y/%m/%d}/loads_{local:%Y%m%dT%H%M%S}.json.gz"
    store.write(raw_path, gzip.compress(json.dumps({
        "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(),
        "fetched_at": stamp, "count": len(records), "result": records,
    }, default=str).encode()))
    files.append(raw_path)

    master_rows = 0
    if records:
        df = pd.json_normalize(records, sep="_")
        if id_field not in df.columns:
            raise RuntimeError(f"Load ID field '{id_field}' missing from Blupath data")
        df[id_field] = df[id_field].astype(str)
        df["_source"] = "api"
        df["_ingested_at"] = stamp
        df = _stabilise(df)
        df["pickup_month"] = pd.to_datetime(_pickup_date(df["firstPickPlanStartDate"])).dt.strftime("%Y-%m").fillna("unknown")

        # Silver: one file per first-pickup month
        for month, part in df.groupby("pickup_month"):
            path = f"curated/loads/pickup_month={month}/loads.parquet"
            store.write(path, _parquet(_upsert(_read_parquet(store, path), part, [id_field])))
            files.append(path)

        # Gold: all loads, latest version
        master = _upsert(_read_parquet(store, MASTER + ".parquet"), df, [id_field])
        master_rows = len(master)
        store.write(MASTER + ".parquet", _parquet(master))
        store.write(MASTER + ".csv", master.to_csv(index=False).encode("utf-8-sig"))
        files += [MASTER + ".parquet", MASTER + ".csv"]

        # Gold: stops (replace all stops of the loads just fetched, so removed stops disappear)
        new_stops = _stops(records, id_field, stamp)
        old_stops = _read_parquet(store, STOPS + ".parquet")
        if old_stops is not None and not old_stops.empty:
            old_stops = old_stops[~old_stops[id_field].isin(df[id_field])]
        parts = [s for s in (old_stops, new_stops) if s is not None and not s.empty]
        if parts or old_stops is not None:
            stops = _stabilise(pd.concat(parts, ignore_index=True)) if parts else new_stops
            store.write(STOPS + ".parquet", _parquet(stops))
            store.write(STOPS + ".csv", stops.to_csv(index=False).encode("utf-8-sig"))
            files += [STOPS + ".parquet", STOPS + ".csv"]
    else:
        log.info("No loads in window")

    prior = store.read(RUN_LOG) or b"run_at_sast,pickup_from,pickup_to,loads_fetched,master_rows\n"
    store.write(RUN_LOG, prior + f"{local:%Y-%m-%d %H:%M:%S},{date_from},{date_to},{len(records)},{master_rows}\n".encode())
    log.info("Done: %d loads fetched, master has %d loads", len(records), master_rows)
    return RunResult(date_from, date_to, len(records), master_rows, files)
