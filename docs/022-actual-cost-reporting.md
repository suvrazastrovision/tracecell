# Actual cost reporting: export setup and bounded reports

The protected BigQuery dataset **`pbmc-okf-portfolio.pbmc_billing_export`** is
created in **EU**. The reporter is implemented and tested, but the project has
no billing-export table yet. Missing data is reported as **not configured**,
not as zero cost. The available computer/browser connection cannot open the
Cloud Console, so the export configuration requires the following console step.

## Enable the data source once

1. Open [Billing in the Cloud Console](https://console.cloud.google.com/billing?project=pbmc-okf-portfolio)
   and choose the billing account linked to this project.
2. Open **Billing export**, then **BigQuery export**.
3. Under **Standard usage cost**, select **Edit settings**.
4. Choose project **pbmc-okf-portfolio** and dataset **pbmc_billing_export**.
5. Save the configuration. Do not change the project's billing account or payment
   settings; this task only enables a reporting export.
6. When the export table appears in BigQuery, copy its full table ID. It starts
   with `pbmc-okf-portfolio.pbmc_billing_export.gcp_billing_export_v1_`.

Google requires Billing Account Costs Manager or Billing Account Administrator
permissions, plus the relevant BigQuery permissions, to configure usage exports.
Exports are not instantaneous; initial backfill can take up to five days. See
[Google's export setup guide](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup).
Do not treat a newly created empty table as evidence that the project costs nothing.

## Run the report

From the repository root in the project Python environment:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.report_costs `
  --table "pbmc-okf-portfolio.pbmc_billing_export.gcp_billing_export_v1_REPLACE_WITH_REAL_SUFFIX" `
  --start 2026-11-01 --end 2026-12-01
```

Use the real exported table ID, not the placeholder. Dates are UTC; the start is
inclusive and the end exclusive. The default period is the current UTC month
through today, including the incomplete current day. The output is saved to the
ignored `artifacts/cost-report.json`.

The query filters `project.id` to this portfolio project and groups by date,
service and currency. It includes credits separately and in net cost. Currency
totals remain separate. A dry run checks the estimated scan before executing;
the actual query also enforces the byte cap, which defaults to 100 MiB. Reduce
the date range or explicitly review a higher cap if the report refuses it.

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.report_costs
```

Without `--table`, the command produces a readiness report and does not query
BigQuery. A configured export with no rows in the requested period produces
`no_export_rows_in_period`, with unknown totals rather than invented zeros.

Usage-cost exports are delayed billing records and can receive adjustments.
They are not guaranteed complete final invoices. The report covers project usage
by service, not exact per-execution cost allocation or proven cost savings.
Billing exports can include other projects on the same billing account; the
report filters this project, and the underlying export remains private. Keep
raw billing data and reports out of GitHub. Only publish a deliberately reviewed
and appropriately scoped summary if needed for the portfolio.
