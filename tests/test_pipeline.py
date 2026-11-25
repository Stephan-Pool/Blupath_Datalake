import gzip
import io
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import BlupathConfig
from blupath_lake.pipeline import MASTER, RUN_LOG, STOPS, default_window, run


class MemStore:
    def __init__(self):
        self.files = {}

    def read(self, p):
        return self.files.get(p)

    def write(self, p, d):
        self.files[p] = d


class FakeResp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body


class FakeSession:
    """Returns the given loads, filtered by first-pickup date like the real export."""
    def __init__(self, loads, status=200, body=None):
        self.loads, self.status, self.body, self.calls, self.headers = loads, status, body, [], {}

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        if self.body is not None:
            return FakeResp(self.body, self.status)
        lo, hi = date.fromisoformat(json["dateFrom"]), date.fromisoformat(json["dateTo"])
        out = [l for l in self.loads
               if lo <= datetime.strptime(l["firstPickPlanStartDate"], "%d/%m/%Y").date() <= hi]
        return FakeResp({"response": "ok", "result": out}, self.status)


def load(n, pickup="20/11/2026", status="planned", lat=-29.1):
    return {
        "blupathId": 1000 + n, "TmsId": str(600000 + n), "id": f"CUSTOMER.{600000 + n}",
        "loadStatus": status, "firstPickPlanStartDate": pickup, "firstPickLatitude": lat,
        "billableRate": 100.5 * n, "hazmat": False, "pickupReferenceNumbers": ["REF1"],
        "stopActualTimes": [
            {"stopNumber": 1, "stopType": "pickup", "stopName": "A", "actualArrival": ""},
            {"stopNumber": 2, "stopType": "delivery", "stopName": "B", "actualArrival": ""},
        ],
    }


def client(loads, **kw):
    s = FakeSession(loads, **kw)
    return BlupathClient(BlupathConfig(access_token="tok", chunk_days=7), session=s), s


NOW = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)  # 23:00 SAST


def test_request_shape_and_chunking():
    c, s = client([load(1, "02/11/2026"), load(2, "20/11/2026")])
    out = list(c.iter_loads(date(2026, 11, 1), date(2026, 11, 20)))
    assert [r["blupathId"] for r in out] == [1001, 1002]
    assert s.calls == [
        {"dateFrom": "2026-11-01", "dateTo": "2026-11-07"},
        {"dateFrom": "2026-11-08", "dateTo": "2026-11-14"},
        {"dateFrom": "2026-11-15", "dateTo": "2026-11-20"},
    ]
    assert s.headers["accessToken"] == "tok"
    assert s.headers["operation"] == "GatewayService.exportLoads"


def test_errors():
    c, _ = client([], status=401, body={"error": "bad token"})
    with pytest.raises(RuntimeError, match="access token"):
        c.fetch(date(2026, 1, 1), date(2026, 1, 1))
    c, _ = client([], body={"response": "error", "message": "x"})
    with pytest.raises(RuntimeError, match="Unexpected"):
        c.fetch(date(2026, 1, 1), date(2026, 1, 1))


def test_default_window_is_sast_based():
    # 22:30 UTC on 25 Nov is already 26 Nov in South Africa
    f, t = default_window(datetime(2026, 11, 25, 22, 30, tzinfo=timezone.utc))
    assert (f, t) == (date(2026, 9, 27), date(2026, 12, 26))


def test_nightly_runs_upsert_and_partition():
    store = MemStore()
    src, _ = client([load(1, "15/10/2026"), load(2)])
    r1 = run(store, src, now=NOW)
    assert r1.fetched == 2 and r1.master_rows == 2
    assert "curated/loads/pickup_month=2026-10/loads.parquet" in store.files
    assert "curated/loads/pickup_month=2026-11/loads.parquet" in store.files
    raw = [p for p in store.files if p.startswith("raw/loads/2026/11/25/")]
    assert len(raw) == 1 and json.loads(gzip.decompress(store.files[raw[0]]))["count"] == 2

    # Next night: load 2 completed, latitude now arrives as a string; load 3 is new
    src, _ = client([load(1, "15/10/2026"), load(2, status="completed", lat="-29.1"), load(3)])
    r2 = run(store, src, now=datetime(2026, 11, 26, 21, 0, tzinfo=timezone.utc))
    master = pd.read_parquet(io.BytesIO(store.files[MASTER + ".parquet"])).set_index("blupathId")
    assert r2.master_rows == 3 and len(master) == 3
    assert master.loc["1002", "loadStatus"] == "completed"
    assert json.loads(master.loc["1001", "pickupReferenceNumbers"]) == ["REF1"]
    nov = pd.read_parquet(io.BytesIO(store.files["curated/loads/pickup_month=2026-11/loads.parquet"]))
    assert sorted(nov["blupathId"]) == ["1002", "1003"]
    stops = pd.read_parquet(io.BytesIO(store.files[STOPS + ".parquet"]))
    assert len(stops) == 6 and set(stops.columns) >= {"blupathId", "stopNumber", "stopType", "actualArrival"}
    assert store.files[MASTER + ".csv"].startswith(b"\xef\xbb\xbf")
    assert store.files[RUN_LOG].decode().count("\n") == 3


def test_backfill_range_and_empty():
    store = MemStore()
    src, s = client([load(1, "01/10/2025")])
    r = run(store, src, date_from=date(2025, 10, 1), date_to=date(2025, 10, 3), now=NOW)
    assert r.fetched == 1 and s.calls[0] == {"dateFrom": "2025-10-01", "dateTo": "2025-10-03"}
    src, _ = client([])
    assert run(MemStore(), src, now=NOW).fetched == 0
