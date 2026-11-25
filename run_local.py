"""Run the pipeline from a PC, e.g. to test the Blupath connection or to backfill.

    python run_local.py --local                     # write the lake to ./.lake instead of SharePoint
    python run_local.py                             # write to SharePoint (SP_* settings needed)
    python run_local.py --from 2026-01-01 --to 2026-11-25   # backfill a range (UTC)

Settings are read from environment variables or from local.settings.json if present.
"""
import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import BlupathConfig, SharePointConfig
from blupath_lake.pipeline import run


class LocalStore:
    def __init__(self, root: Path):
        self.root = root

    def read(self, path):
        p = self.root / path
        return p.read_bytes() if p.exists() else None

    def write(self, path, data):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="write to ./.lake instead of SharePoint")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Path(__file__).with_name("local.settings.json")
    if settings.exists():
        for k, v in json.loads(settings.read_text())["Values"].items():
            os.environ.setdefault(k, v)

    def parse(v):
        return datetime.fromisoformat(v).replace(tzinfo=timezone.utc) if v else None

    if args.local:
        store = LocalStore(Path(__file__).with_name(".lake"))
    else:
        from blupath_lake.sharepoint import SharePointStore
        store = SharePointStore(SharePointConfig.from_env())
    r = run(store, BlupathClient(BlupathConfig.from_env()), start=parse(args.start), end=parse(args.end))
    print(f"Fetched {r.fetched} loads; master now has {r.master_rows}. Files: {r.files}")


if __name__ == "__main__":
    main()
