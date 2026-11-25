# Blupath Loads Data Lake

Pulls loads from the Blupath API every night at **23:00 South Africa time** and stores them in a
layered data lake in a **SharePoint** document library. Runs as an **Azure Function**, so no PC
has to stay on.

```
Blupath API ──(23:00 SAST, Azure Function timer)──► SharePoint: "Blupath Data Lake/"
                                                     ├─ raw/loads/YYYY/MM/DD/loads_<time>.json.gz       exact API reply per run
                                                     ├─ curated/loads/pickup_month=YYYY-MM/loads.parquet loads by first-pickup month
                                                     ├─ gold/loads_master.parquet / .csv               one row per load (latest version)
                                                     ├─ gold/load_stops.parquet / .csv                 one row per stop, actual arrival/departure
                                                     └─ _state/run_log.csv                              loads fetched per night
```

**The Blupath API** (same call as the Power BI query):
`POST https://portal.blupath.com/fireWorkFlow` with headers `service: BlupathService`,
`operation: GatewayService.exportLoads` and `accessToken`, and body `{"dateFrom": "YYYY-MM-DD", "dateTo": "YYYY-MM-DD"}`.
The dates filter on the **first pickup planned date**. The reply is `{"response": "ok", "result": [...]}`.
`blupathId` is the unique load key.

**How the nightly run works**
- Blupath has no "last updated" filter, and loads keep changing after pickup (status, POD,
  invoice). So every night re-pulls all loads with first pickup from **60 days ago to 30 days ahead**
  and updates the lake. A load that moves from planned to completed is updated, not duplicated.
- Requests are split into 31-day chunks. A 30-day chunk takes about 17 seconds.
- Nested fields: `stopActualTimes` becomes the `load_stops` table. Lists such as
  `pickupReferenceNumbers` are kept as JSON text in the master.
- Loads that have been deleted in Blupath remain in the lake with their last known values.
- The schedule is `0 0 21 * * *` (21:00 UTC). South Africa has no daylight saving, so this is always 23:00 SAST.

## 1. Settings

| Setting | Meaning | Default |
|---|---|---|
| `BLUPATH_ACCESS_TOKEN` | Blupath access token (store in Key Vault) | *required* |
| `BLUPATH_URL` | gateway URL | `https://portal.blupath.com/fireWorkFlow` |
| `BLUPATH_SERVICE` / `BLUPATH_OPERATION` | gateway headers | `BlupathService` / `GatewayService.exportLoads` |
| `BLUPATH_CHUNK_DAYS` | max pickup-date range per request | `31` |
| `LAKE_DAYS_BACK` / `LAKE_DAYS_FORWARD` | nightly refresh window around today | `60` / `30` |

Test from a PC first (writes to a local `.lake/` folder, not SharePoint):

```bash
cp local.settings.json.example local.settings.json   # then fill in BLUPATH_ACCESS_TOKEN
pip install -r requirements.txt
python run_local.py --local
```

## 2. Give the function access to SharePoint

1. **Entra ID → App registrations → New registration**, named `blupath-datalake`. Note the tenant ID and client ID.
2. **Certificates & secrets → New client secret.** Save the value; it goes into Key Vault.
3. **API permissions → Microsoft Graph → Application → `Sites.Selected`**, then **Grant admin consent**.
   This lets the app reach only the sites you give it, not all of SharePoint.
4. Create or choose a site, e.g. `https://yourcompany.sharepoint.com/sites/DataLake`. A SharePoint
   admin grants the app write access to that site only:

```bash
# PnP PowerShell
Grant-PnPAzureADAppSitePermission -AppId <client-id> -DisplayName blupath-datalake -Site https://yourcompany.sharepoint.com/sites/DataLake -Permissions Write
```

Settings: `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET`, `SP_HOSTNAME` (`yourcompany.sharepoint.com`),
`SP_SITE_PATH` (`/sites/DataLake`), `SP_DRIVE_NAME` (library name, default `Documents`, which is
"Shared Documents" in the browser), and `SP_ROOT_FOLDER` (default `Blupath Data Lake`).

## 3. Deploy the Azure Function

The values below are examples. South Africa North keeps the data in-country.

```bash
az group create -n rg-blupath-lake -l southafricanorth
```

```bash
az storage account create -n stblupathlake -g rg-blupath-lake -l southafricanorth --sku Standard_LRS
```

```bash
az functionapp create -n func-blupath-lake -g rg-blupath-lake --storage-account stblupathlake --flexconsumption-location southafricanorth --runtime python --runtime-version 3.11
```

If Flex Consumption isn't available in your region, use `--consumption-plan-location southafricanorth --os-type Linux --functions-version 4` instead.

Store the secrets in Key Vault, enable the function's managed identity, give it the
"Key Vault Secrets User" role on the vault, then add the settings:

```bash
az functionapp config appsettings set -n func-blupath-lake -g rg-blupath-lake --settings "BLUPATH_ACCESS_TOKEN=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/blupath-access-token/)" "SP_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/sp-client-secret/)" "SP_TENANT_ID=..." "SP_CLIENT_ID=..." "SP_HOSTNAME=yourcompany.sharepoint.com" "SP_SITE_PATH=/sites/DataLake"
```

Deploy (requires Azure Functions Core Tools):

```bash
func azure functionapp publish func-blupath-lake
```

## 4. Load history (one-off backfill)

The nightly run only covers the last 60 days. To load the full financial year (as in Power BI),
run a backfill by pickup date. Each call is one quarter, to stay within the 10-minute function timeout.
Get the function key from the Azure portal (the function → *Function Keys*):

```bash
curl -X POST "https://func-blupath-lake.azurewebsites.net/api/blupath/run?from=2025-10-01&to=2025-12-31&code=<function-key>"
```

Repeat for `2026-01-01`–`2026-03-31`, `2026-04-01`–`2026-06-30`, and so on. Alternatively, run it from a PC
with the SP_* settings filled in:

```bash
python run_local.py --from 2025-10-01 --to 2026-09-30
```

Calling the endpoint without `from`/`to` runs the normal nightly window immediately.

## 5. Use the data

- **Excel:** open `gold/loads_master.csv` from SharePoint.
- **Power BI:** *Get Data → SharePoint folder*, filter to `gold/loads_master.parquet` (or the CSV) and
  `gold/load_stops.parquet`, and relate them on `blupathId`. This replaces the direct API call in the current report.
- **Monitoring:** failed runs are logged in Application Insights. Add an alert on function failures.
  `_state/run_log.csv` shows the load count for each night.

## Development

```bash
pip install -r requirements.txt pytest
```

```bash
python -m pytest -q
```
