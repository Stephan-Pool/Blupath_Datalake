"""One-off seed of historical loads from the "BP Raw data.xlsx" workbook.

Each sheet is one financial year (e.g. "011024-300925" = 1 Oct 2024 - 30 Sep 2025) with the same
column names as the Blupath API. Rows are converted to the API's formats so they sit in the same
tables as the nightly data. Loads already in the lake (from the API) are kept, never overwritten,
so the seed can safely be re-run at any time.
"""
from __future__ import annotations

import io
import json
import logging
import math
from datetime import date, datetime, time, timezone

import pandas as pd

from .pipeline import MASTER, RUN_LOG, SAST, Store, _parquet, _pickup_date, _read_parquet, _stabilise

log = logging.getLogger(__name__)

# Column -> the text format the Blupath API uses for it
DMY = ("firstPickPlanStartDate", "lastDropPlanDateStartDate", "lastDropApptDateStartDate")
YMD = ("invoiceDate", "lastDropPlanDateStart", "lastDropApptDateStart")
ISO = ("createdDate",)
TIMES = ("firstPickPlanStartTime", "lastDropPlanDateStartTime", "lastDropApptDateStartTime")
WHOLE_NUMBERS = ("blupathId", "TmsId", "cellNumber", "driverCellNumber")
LISTS = ("pickupReferenceNumbers",)


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def _as_date(v):
    if isinstance(v, (datetime, pd.Timestamp)):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


def _fmt(col: str, v):
    """Convert one Excel cell to the API's representation for that column."""
    if _blank(v):
        return None
    d = _as_date(v)
    if col in DMY and d is not None:
        return d.strftime("%d/%m/%Y")
    if col in YMD and d is not None:
        return d.strftime("%Y-%m-%d")
    if col in ISO and d is not None:
        return pd.Timestamp(d).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if col in TIMES:
        if isinstance(v, (time, datetime)):
            return v.strftime("%H:%M")
        if isinstance(v, float) and 0 <= v < 1:  # Excel fraction of a day
            mins = round(v * 24 * 60)
            return f"{mins // 60:02d}:{mins % 60:02d}"
    if col in WHOLE_NUMBERS and isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer():
        return str(int(v))
    if col in LISTS and not str(v).startswith("["):
        return json.dumps([str(v)])
    if isinstance(v, str) and v.upper() in ("TRUE", "FALSE"):
        return v.upper() == "TRUE"
    if d is not None:
        return d.isoformat()
    return v


def workbook_to_frame(xlsx: bytes, sheets: list[str], id_field: str, ingested_at: str) -> pd.DataFrame:
    frames = []
    book = pd.read_excel(io.BytesIO(xlsx), sheet_name=sheets, dtype=object, engine="openpyxl")
    for sheet in sheets:
        raw = book[sheet]
        raw.columns = [str(c).strip() for c in raw.columns]
        if id_field not in raw.columns:
            raise RuntimeError(f"Sheet '{sheet}' has no '{id_field}' column")
        df = pd.DataFrame({c: [_fmt(c, v) for v in raw[c]] for c in raw.columns})
        df = df[df[id_field].map(lambda v: not _blank(v))]
        df[id_field] = df[id_field].astype(str)
        df["_source"] = f"excel:{sheet}"
        log.info("Sheet %s: %d loads", sheet, len(df))
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    # A load in both sheets: keep the later financial year's row
    out = out.drop_duplicates(subset=[id_field], keep="last")
    out["_ingested_at"] = ingested_at
    out = _stabilise(out)
    out["pickup_month"] = pd.to_datetime(_pickup_date(out["firstPickPlanStartDate"])).dt.strftime("%Y-%m").fillna("unknown")
    return out


def _merge_keep_existing(existing: pd.DataFrame | None, hist: pd.DataFrame, id_field: str) -> tuple[pd.DataFrame, int]:
    if existing is None or existing.empty:
        return hist.reset_index(drop=True), len(hist)
    new = hist[~hist[id_field].isin(existing[id_field])]
    return _stabilise(pd.concat([existing, new], ignore_index=True)), len(new)


def seed(store: Store, xlsx: bytes, sheets: list[str], id_field: str = "blupathId",
         now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hist = workbook_to_frame(xlsx, sheets, id_field, now.isoformat())
    files = []

    for month, part in hist.groupby("pickup_month"):
        path = f"curated/loads/pickup_month={month}/loads.parquet"
        merged, added = _merge_keep_existing(_read_parquet(store, path), part, id_field)
        if added:
            store.write(path, _parquet(merged))
            files.append(path)

    master, added = _merge_keep_existing(_read_parquet(store, MASTER + ".parquet"), hist, id_field)
    store.write(MASTER + ".parquet", _parquet(master))
    store.write(MASTER + ".csv", master.to_csv(index=False).encode("utf-8-sig"))
    files += [MASTER + ".parquet", MASTER + ".csv"]

    local = now.astimezone(SAST)
    prior = store.read(RUN_LOG) or b"run_at_sast,pickup_from,pickup_to,loads_fetched,master_rows\n"
    store.write(RUN_LOG, prior + f"{local:%Y-%m-%d %H:%M:%S},history:{'|'.join(sheets)},,{len(hist)},{len(master)}\n".encode())
    log.info("History seed: %d loads in workbook, %d added, master now %d", len(hist), added, len(master))
    return {"workbook_loads": len(hist), "added": added, "already_in_lake": len(hist) - added,
            "master_rows": len(master), "files": files}
