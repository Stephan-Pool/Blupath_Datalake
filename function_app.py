"""Azure Functions entry points (Python v2 programming model).

- blupath_daily: timer, every day at 23:00 South Africa time (21:00 UTC; SAST has no DST).
- blupath_manual: HTTP POST for a manual run or a backfill, protected by a function key.
    POST /api/blupath/run                                    -> normal incremental run
    POST /api/blupath/run?from=2026-01-01&to=2026-02-01      -> backfill a date range (UTC)
"""
import json
import logging
from datetime import datetime, timezone

import azure.functions as func

from blupath_lake.blupath_api import BlupathClient
from blupath_lake.config import BlupathConfig, SharePointConfig
from blupath_lake.pipeline import run
from blupath_lake.sharepoint import SharePointStore

app = func.FunctionApp()


def _execute(start=None, end=None):
    store = SharePointStore(SharePointConfig.from_env())
    source = BlupathClient(BlupathConfig.from_env())
    return run(store, source, start=start, end=end)


@app.timer_trigger(schedule="0 0 21 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def blupath_daily(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is past due; running now")
    result = _execute()
    logging.info("Blupath daily ingest: %d loads, master=%d", result.fetched, result.master_rows)


@app.route(route="blupath/run", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def blupath_manual(req: func.HttpRequest) -> func.HttpResponse:
    def parse(v):
        if not v:
            return None
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    try:
        start, end = parse(req.params.get("from")), parse(req.params.get("to"))
        if bool(start) != bool(end):
            return func.HttpResponse("Provide both 'from' and 'to', or neither", status_code=400)
        r = _execute(start, end)
        body = {"window_start": r.window_start.isoformat(), "window_end": r.window_end.isoformat(),
                "loads_fetched": r.fetched, "master_rows": r.master_rows, "files": r.files}
        return func.HttpResponse(json.dumps(body, indent=2), mimetype="application/json")
    except Exception as exc:  # surface the reason to the caller; full trace goes to App Insights
        logging.exception("Manual run failed")
        return func.HttpResponse(f"Run failed: {exc}", status_code=500)
