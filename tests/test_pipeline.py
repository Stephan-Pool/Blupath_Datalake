import io
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import BlupathConfig
from blupath_lake.pipeline import MASTER_CSV, MASTER_PARQUET, WATERMARK, run


class MemStore:
    def __init__(self):
        self.files = {}

    def read(self, p):
        return self.files.get(p)

    def write(self, p, d):
        self.files[p] = d


class FakeResp:
    def __init__(self, body, url="https://api.test/loads"):
        self.body, self.status_code, self.url, self.text = body, 200, url, ""

    def json(self):
        return self.body


class FakeSession:
    """Serves pages of loads; records every request."""
    def __init__(self, pages):
        self.pages, self.calls, self.headers, self.auth = pages, [], {}, None

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return FakeResp(self.pages[len(self.calls) - 1], url)


def cfg(**kw):
    base = dict(base_url="https://api.test", loads_path="/loads", api_key="k", page_size=2)
    base.update(kw)
    return BlupathConfig(**base)


def load(i, status="booked"):
    return {"id": i, "status": status, "origin": {"city": "Durban"}, "stops": [{"seq": 1}], "weight": 1.5 * i}


T0 = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)


def test_page_pagination_and_auth():
    s = FakeSession([{"data": [load(1), load(2)]}, {"data": [load(3)]}])
    c = BlupathClient(cfg(), session=s)
    out = list(c.iter_loads(T0, T0))
    assert [r["id"] for r in out] == [1, 2, 3]
    assert [p["page"] for _, p in s.calls] == [1, 2]
    assert s.headers["Authorization"] == "Bearer k"
    assert s.calls[0][1]["updatedFrom"] == "2026-11-25T21:00:00Z"


def test_cursor_and_next_link_pagination():
    s = FakeSession([{"items": [load(1)], "meta": {"cursor": "abc"}}, {"items": [load(2)], "meta": {}}])
    c = BlupathClient(cfg(pagination="cursor", records_key="items", cursor_key="meta.cursor"), session=s)
    assert len(list(c.iter_loads(T0, T0))) == 2 and s.calls[1][1]["cursor"] == "abc"

    s = FakeSession([{"data": [load(1)], "next": "/loads?page=2"}, {"data": [load(2)], "next": None}])
    c = BlupathClient(cfg(pagination="next_link"), session=s)
    assert len(list(c.iter_loads(T0, T0))) == 2 and s.calls[1][0] == "https://api.test/loads?page=2"


def test_bare_list_response():
    s = FakeSession([[load(1)]])
    c = BlupathClient(cfg(pagination="none", records_key=""), session=s)
    assert len(list(c.iter_loads(T0, T0))) == 1


def test_run_builds_layers_and_upserts():
    store = MemStore()
    src = BlupathClient(cfg(pagination="none"), session=FakeSession([{"data": [load(1), load(2)]}]))
    r1 = run(store, src, now=T0)
    assert r1.fetched == 2 and r1.master_rows == 2
    assert "curated/loads/2026/11/25/loads_2026-11-25.parquet" in store.files
    assert any(p.startswith("raw/loads/2026/11/25/") for p in store.files)

    # Next night: load 2 changes status, load 3 is new; window starts from watermark - lookback
    nxt = datetime(2026, 11, 26, 21, 0, tzinfo=timezone.utc)
    sess = FakeSession([{"data": [load(2, "delivered"), load(3)]}])
    r2 = run(store, BlupathClient(cfg(pagination="none"), session=sess), now=nxt)
    assert r2.window_start == datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc)
    master = pd.read_parquet(io.BytesIO(store.files[MASTER_PARQUET])).set_index("id")
    assert len(master) == 3 and master.loc["2", "status"] == "delivered"
    assert master.loc["1", "origin_city"] == "Durban"
    assert json.loads(master.loc["1", "stops"]) == [{"seq": 1}]
    assert store.files[MASTER_CSV].startswith(b"\xef\xbb\xbf")
    assert json.loads(store.files[WATERMARK])["window_end"] == nxt.isoformat()


def test_missing_id_field_fails_without_advancing_watermark():
    store = MemStore()
    src = BlupathClient(cfg(pagination="none", id_field="loadNumber"), session=FakeSession([{"data": [load(1)]}]))
    with pytest.raises(RuntimeError, match="BLUPATH_ID_FIELD"):
        run(store, src, now=T0)
    assert WATERMARK not in store.files


def test_empty_day():
    store = MemStore()
    r = run(store, BlupathClient(cfg(pagination="none"), session=FakeSession([{"data": []}])), now=T0)
    assert r.fetched == 0 and WATERMARK in store.files
