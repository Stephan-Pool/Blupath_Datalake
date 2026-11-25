# Blupath Loads Data Lake

Pulls loads from the Blupath API every night at **23:00 South Africa time** and stores them in a
layered data lake in a **SharePoint** document library. Runs as an **Azure Function**, so no PC
has to stay on.

```
Blupath API ──(23:00 SAST, Azure Function timer)──► SharePoint: "Blupath Data Lake/"
                                                     ├─ raw/loads/YYYY/MM/DD/loads_<time>.json      untouched API data
                                                     ├─ curated/loads/YYYY/MM/DD/loads_<date>.parquet  loads changed that day
                                                     ├─ gold/loads_master.parquet                    one row per load (latest)
                                                     ├─ gold/loads_master.csv                        same, for Excel / Power BI
                                                     └─ _state/watermark.json, run_log.csv           run tracking
```

**How the daily run works**
- It fetches loads *updated* since the last successful run, with a 24-hour overlap so late changes
  aren't missed. The first run pulls the last 30 days. Use a backfill for older history.
- Each load is flattened (`origin.city` becomes `origin_city`; lists become JSON text) and upserted
  into the master by its load ID, so a load that changes status is updated rather than duplicated.
- The watermark only moves forward after all files are written, so a failed night is picked up the next night.
- The schedule is `0 0 21 * * *` (21:00 UTC). South Africa has no daylight saving, so this is always 23:00 SAST.

## 1. Connect to the Blupath API

All API details are settings, so no code changes are needed:

| Setting | Meaning | Default |
|---|---|---|
| `BLUPATH_BASE_URL` | e.g. `https://api.blupath.co.za` | *required* |
| `BLUPATH_LOADS_PATH` | loads endpoint | `/loads` |
| `BLUPATH_AUTH_TYPE` | `bearer`, `header`, `query`, `basic` or `none` | `bearer` |
| `BLUPATH_API_KEY` | API key or token (store in Key Vault) | |
| `BLUPATH_API_KEY_HEADER` / `BLUPATH_API_KEY_PARAM` | header or query name for `header`/`query` auth | `x-api-key` / `api_key` |
| `BLUPATH_USERNAME` / `BLUPATH_PASSWORD` | for `basic` auth | |
| `BLUPATH_FROM_PARAM` / `BLUPATH_TO_PARAM` | date-filter query params (blank = not sent) | `updatedFrom` / `updatedTo` |
| `BLUPATH_DATE_FORMAT` | `iso` or `date` | `iso` |
| `BLUPATH_PAGINATION` | `page`, `offset`, `cursor`, `next_link` or `none` | `page` |
| `BLUPATH_PAGE_PARAM`, `BLUPATH_PAGE_START`, `BLUPATH_PAGE_SIZE_PARAM`, `BLUPATH_PAGE_SIZE` | paging params | `page`, `1`, `pageSize`, `500` |
| `BLUPATH_CURSOR_PARAM` / `BLUPATH_CURSOR_KEY` / `BLUPATH_NEXT_LINK_KEY` | for cursor or next-link paging (keys are dotted paths, e.g. `meta.next`) | |
| `BLUPATH_RECORDS_KEY` | where the list of loads sits in the response (dotted path; blank if the response is a list) | `data` |
| `BLUPATH_ID_FIELD` | unique load ID column, after flattening | `id` |
| `BLUPATH_EXTRA_PARAMS` | any fixed params as JSON, e.g. `{"status":"all"}` | `{}` |
| `LAKE_LOOKBACK_HOURS` / `LAKE_INITIAL_LOOKBACK_DAYS` | overlap per run / first-run history | `24` / `30` |

Test it on a PC before deploying (writes to a local `.lake/` folder):

```bash
cp local.settings.json.example local.settings.json   # then fill in the BLUPATH_* values
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
az functionapp config appsettings set -n func-blupath-lake -g rg-blupath-lake --settings "BLUPATH_BASE_URL=https://..." "BLUPATH_API_KEY=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/blupath-api-key/)" "SP_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/sp-client-secret/)" "SP_TENANT_ID=..." "SP_CLIENT_ID=..." "SP_HOSTNAME=yourcompany.sharepoint.com" "SP_SITE_PATH=/sites/DataLake"
```

Deploy (requires Azure Functions Core Tools):

```bash
func azure functionapp publish func-blupath-lake
```

## 4. Run it manually or backfill history

The function also exposes an HTTP endpoint protected by a function key (Azure portal → the function → *Function Keys*):

```bash
curl -X POST "https://func-blupath-lake.azurewebsites.net/api/blupath/run?code=<function-key>"
```

To backfill history, run month by month to stay within the 10-minute function timeout:

```bash
curl -X POST "https://func-blupath-lake.azurewebsites.net/api/blupath/run?from=2026-01-01&to=2026-02-01&code=<function-key>"
```

For a large backfill you can also run `python run_local.py --from 2025-01-01 --to 2026-11-25` from a PC with the SP_* settings.

## 5. Use the data

- **Excel:** open `gold/loads_master.csv` from SharePoint.
- **Power BI:** *Get Data → SharePoint folder*, filter to `gold/loads_master.parquet` (or the CSV).
  Use the `curated/` daily files for history and trends.
- **Monitoring:** failed runs are logged in Application Insights. Add an alert on function failures.
  `_state/run_log.csv` shows the load count for each night.

## Development

```bash
pip install -r requirements.txt pytest
```

```bash
python -m pytest -q
```
