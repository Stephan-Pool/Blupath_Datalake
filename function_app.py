"""Azure Functions entry points (Python v2 programming model).

- blupath_daily: timer, every day at 23:00 South Africa time (21:00 UTC; SAST has no DST).
  Re-pulls loads with first pickup from LAKE_DAYS_BACK days ago to LAKE_DAYS_FORWARD days ahead.
- blupath_manual: HTTP POST for a manual run or a backfill, protected by a function key.
    POST /api/blupath/run                                    -> normal nightly window
    POST /api/blupath/run?from=2025-10-01&to=2025-12-31      -> backfill a pickup-date range
- blupath_seed_history: HTTP POST, one-off load of history from "BP Raw data.xlsx".
    POST /api/blupath/seed-history                           -> sheets from HISTORY_SHEETS
    POST /api/blupath/seed-history?sheets=011024-300925      -> specific sheet(s), comma-separated
"""
import json
import logging
from datetime import date

import azure.functions as func

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import HISTORY_FILE, HISTORY_SHEETS, BlupathConfig, SharePointConfig
from blupath_lake.history import seed
from blupath_lake.pipeline import run
from blupath_lake.sharepoint import SharePointStore

app = func.FunctionApp()


def _execute(date_from=None, date_to=None):
    store = SharePointStore(SharePointConfig.from_env())
    source = BlupathClient(BlupathConfig.from_env())
    return run(store, source, date_from=date_from, date_to=date_to)


@app.timer_trigger(schedule="0 0 21 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def blupath_daily(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is past due; running now")
    r = _execute()
    logging.info("Blupath nightly ingest: %d loads (%s..%s), master=%d", r.fetched, r.date_from, r.date_to, r.master_rows)


@app.route(route="blupath/run", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def blupath_manual(req: func.HttpRequest) -> func.HttpResponse:
    try:
        f, t = req.params.get("from"), req.params.get("to")
        if bool(f) != bool(t):
            return func.HttpResponse("Provide both 'from' and 'to' (YYYY-MM-DD), or neither", status_code=400)
        r = _execute(date.fromisoformat(f) if f else None, date.fromisoformat(t) if t else None)
        body = {"pickup_from": str(r.date_from), "pickup_to": str(r.date_to),
                "loads_fetched": r.fetched, "master_rows": r.master_rows, "files": r.files}
        return func.HttpResponse(json.dumps(body, indent=2), mimetype="application/json")
    except Exception as exc:  # reason to the caller; full trace goes to Application Insights
        logging.exception("Manual run failed")
        return func.HttpResponse(f"Run failed: {exc}", status_code=500)


@app.route(route="blupath/seed-history", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def blupath_seed_history(req: func.HttpRequest) -> func.HttpResponse:
    try:
        sheets = [x.strip() for x in (req.params.get("sheets") or "").split(",") if x.strip()] or HISTORY_SHEETS
        store = SharePointStore(SharePointConfig.from_env())
        xlsx = store.read_from_library(HISTORY_FILE)
        if xlsx is None:
            return func.HttpResponse(f"History workbook not found: {HISTORY_FILE}", status_code=404)
        result = seed(store, xlsx, sheets)
        return func.HttpResponse(json.dumps(result, indent=2), mimetype="application/json")
    except Exception as exc:
        logging.exception("History seed failed")
        return func.HttpResponse(f"History seed failed: {exc}", status_code=500)
