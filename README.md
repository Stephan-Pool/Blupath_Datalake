# Blupath Loads Data Lake

Pulls loads from the Blupath API every night at **23:00 South Africa time** and stores them in a
layered data lake in the **Pool Transport Data Lake** folder on the SNPool/IT SharePoint site. Runs as an **Azure Function**, so no PC
has to stay on.

```
Blupath API ──(23:00 SAST, Azure Function timer)──► SharePoint (SNPool/IT): "Pool Transport Data Lake/Blupath/"
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
4. A SharePoint admin grants the app write access to the IT site only (it reads the history workbook
   and writes the lake there):

```bash
# PnP PowerShell
Grant-PnPAzureADAppSitePermission -AppId <client-id> -DisplayName blupath-datalake -Site https://pooltransport.sharepoint.com/sites/SNPool/IT -Permissions Write
```

Settings: `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET`, `SP_HOSTNAME` (`pooltransport.sharepoint.com`),
`SP_SITE_PATH` (`/sites/SNPool/IT`), `SP_DRIVE_NAME` (`Documents`, which is "Shared Documents" in the
browser), and `SP_ROOT_FOLDER` (`Pool Transport Data Lake/Blupath`).

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
az functionapp config appsettings set -n func-blupath-lake -g rg-blupath-lake --settings "BLUPATH_ACCESS_TOKEN=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/blupath-access-token/)" "SP_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/sp-client-secret/)" "SP_TENANT_ID=..." "SP_CLIENT_ID=..." "SP_HOSTNAME=pooltransport.sharepoint.com" "SP_SITE_PATH=/sites/SNPool/IT"
```

Deploy, either automatically from GitHub (section 3b) or by hand with Azure Functions Core Tools:

```bash
func azure functionapp publish func-blupath-lake
```

## 3b. Automatic deployment from GitHub

`.github/workflows/deploy.yml` runs the tests on every push and pull request. When `main` changes
and the tests pass, it deploys to Azure, then checks that all three functions are registered.
Deployment stays switched off until the `AZURE_FUNCTIONAPP_NAME` variable below is set, so the
workflow can live in the repository before Azure exists.

It signs in to Azure with OpenID Connect, so no Azure password is stored in GitHub. One-time setup:

1. Create a deployment identity (separate from the SharePoint app) and note its `appId`:

```bash
az ad app create --display-name blupath-datalake-deploy --query appId -o tsv
```

```bash
az ad sp create --id <appId>
```

2. Trust GitHub's `main` branch of this repository:

```bash
az ad app federated-credential create --id <appId> --parameters '{"name":"github-main","issuer":"https://token.actions.githubusercontent.com","subject":"repo:Stephan-Pool/blupath_datalake:ref:refs/heads/main","audiences":["api://AzureADTokenExchange"]}'
```

3. Allow it to deploy to the function app, and nothing else:

```bash
az role assignment create --assignee <appId> --role "Website Contributor" --scope $(az functionapp show -n func-blupath-lake -g rg-blupath-lake --query id -o tsv)
```

4. In GitHub, go to the repository → *Settings → Secrets and variables → Actions*:
   - **Secrets:** `AZURE_CLIENT_ID` (the `appId`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   - **Variables:** `AZURE_FUNCTIONAPP_NAME` (`func-blupath-lake`), `AZURE_RESOURCE_GROUP` (`rg-blupath-lake`),
     and `AZURE_FUNCTIONAPP_PLAN` = `consumption` only if you used the Linux Consumption plan instead of Flex.

To deploy without changing code: *Actions → Test and deploy → Run workflow* on `main`.
The function's own settings (Blupath token, SharePoint IDs) stay in Azure and are not touched by deployments.

## 4. Load history (one-off)

History comes from the workbook **`Pool Transport Data Lake/BP Raw data.xlsx`**, using sheets
`011024-300925` (FY2025) and `011025-300926` (FY2026). The columns match the API, and the seed converts
Excel dates, times and numbers to the API's formats so everything lands in the same tables.
Rows are tagged in a `_source` column (`excel:<sheet>` or `api`).

Rules:
- If a load appears in both sheets, the later sheet wins.
- **API data always wins over the workbook.** The seed only adds loads that aren't already in the lake,
  and the nightly run overwrites any workbook row it fetches again. The seed is safe to run more than once.
- The workbook has no stop-level times, so `load_stops` only covers loads from the API.
- Both full sheets (about 53,000 loads) take about 45 seconds.

```bash
curl -X POST "https://func-blupath-lake.azurewebsites.net/api/blupath/seed-history?code=<function-key>"
```

Then close any gap between the end of the workbook (30 Sep 2026) and the start of the nightly window
(60 days ago). This is only needed if you deploy after about 29 Nov 2026, but it's harmless either way:

```bash
curl -X POST "https://func-blupath-lake.azurewebsites.net/api/blupath/run?from=2026-10-01&to=2026-11-30&code=<function-key>"
```

The same can be run from a PC with the SP_* settings filled in: `python run_local.py --seed-history`.
The settings are `HISTORY_FILE` (path from the library root) and `HISTORY_SHEETS` (comma-separated).
When next year's sheet is added, put its name in `HISTORY_SHEETS` and run the seed again.

Get the function key from the Azure portal (the function → *Function Keys*). Calling `/api/blupath/run`
without `from`/`to` runs the normal nightly window immediately.

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
