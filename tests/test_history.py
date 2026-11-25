import io
import json
from datetime import datetime, time, timezone

import pandas as pd
from openpyxl import Workbook

from blupath_lake.history import seed
from blupath_lake.pipeline import MASTER, run
from test_pipeline import NOW, MemStore, client, load

COLS = ["TmsId", "batchNumber", "billableRate", "blupathId", "createdDate", "firstPickPlanStartDate",
        "firstPickPlanStartTime", "hazmat", "invoiceDate", "loadStatus", "pickupReferenceNumbers", "cellNumber"]


def row(n, pickup, status="completed"):
    return [600000 + n, "TB-1", 100.0 * n, float(1000 + n), datetime(2024, 9, 30, 7, 49, 38, 983000),
            pickup, time(7, 0), True, datetime(2025, 9, 18), status, "PB110", 27766538654.0]


def workbook(sheets: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(COLS)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def master(store):
    return pd.read_parquet(io.BytesIO(store.files[MASTER + ".parquet"])).set_index("blupathId")


def test_seed_formats_match_api_and_partitions():
    xlsx = workbook({
        "011024-300925": [row(1, datetime(2024, 10, 7)), row(2, datetime(2025, 9, 30), "delivered"),
                          [None] * len(COLS)],
        "011025-300926": [row(2, datetime(2025, 9, 30), "completed"), row(3, datetime(2025, 10, 2))],
    })
    store = MemStore()
    out = seed(store, xlsx, ["011024-300925", "011025-300926"], now=NOW)
    assert out["workbook_loads"] == 3 and out["added"] == 3

    m = master(store)
    r = m.loc["1001"]
    assert r["firstPickPlanStartDate"] == "07/10/2024"          # API uses dd/mm/yyyy
    assert r["createdDate"] == "2024-09-30T07:49:38.983Z"
    assert r["invoiceDate"] == "2025-09-18"
    assert r["firstPickPlanStartTime"] == "07:00"
    assert r["TmsId"] == "600001" and r["cellNumber"] == "27766538654"
    assert json.loads(r["pickupReferenceNumbers"]) == ["PB110"]
    assert m.loc["1002", "loadStatus"] == "completed"            # later sheet wins
    assert m.loc["1002", "_source"] == "excel:011025-300926"
    for month in ("2024-10", "2025-09", "2025-10"):
        assert f"curated/loads/pickup_month={month}/loads.parquet" in store.files


def test_api_data_wins_over_history_in_either_order():
    xlsx = workbook({"011025-300926": [row(2, datetime(2026, 11, 20), "planned"), row(9, datetime(2025, 10, 1))]})

    # Order 1: history first, then nightly API run updates load 1002
    store = MemStore()
    seed(store, xlsx, ["011025-300926"], now=NOW)
    src, _ = client([load(2, status="completed")])
    run(store, src, now=NOW)
    m = master(store)
    assert m.loc["1002", "loadStatus"] == "completed" and m.loc["1002", "_source"] == "api"
    assert "1009" in m.index

    # Order 2: API first, then history seed must not overwrite it
    store = MemStore()
    src, _ = client([load(2, status="completed")])
    run(store, src, now=NOW)
    out = seed(store, xlsx, ["011025-300926"], now=NOW)
    m = master(store)
    assert out["added"] == 1 and out["already_in_lake"] == 1
    assert m.loc["1002", "loadStatus"] == "completed" and len(m) == 2

    # Re-running the seed is harmless
    assert seed(store, xlsx, ["011025-300926"], now=NOW)["added"] == 0
