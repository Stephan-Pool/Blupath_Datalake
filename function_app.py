"""Azure Functions entry points (Python v2 programming model).

- blupath_daily: timer, every day at 23:00 South Africa time (21:00 UTC; SAST has no DST).
  Re-pulls loads with first pickup from LAKE_DAYS_BACK days ago to LAKE_DAYS_FORWARD days ahead.
- blupath_manual: HTTP POST for a manual run or a backfill, protected by a function key.
    POST /api/blupath/run                                    -> normal nightly window
    POST /api/blupath/run?from=2025-10-01&to=2025-12-31      -> backfill a pickup-date range
"""
import json
import logging
from datetime import date

import azure.functions as func

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import BlupathConfig, SharePointConfig
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
