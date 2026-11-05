"""Report exported actual usage costs, retaining credits and currency boundaries."""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re


PROJECT = "pbmc-okf-portfolio"


def query_for(table):
    if not re.fullmatch(r"[a-z][a-z0-9-]+\.[a-zA-Z_][a-zA-Z0-9_]*\.gcp_billing_export_v1_[a-zA-Z0-9_]+", table):
        raise ValueError("Select an explicit standard billing export table: project.dataset.gcp_billing_export_v1_...")
    return f"""SELECT DATE(usage_start_time) AS usage_date,
      service.description AS service, currency,
      SUM(cost) AS gross_cost,
      SUM(IFNULL((SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit), 0)) AS credits,
      SUM(cost) + SUM(IFNULL((SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit), 0)) AS net_cost
    FROM `{table}`
    WHERE project.id = @project
      AND usage_start_time >= TIMESTAMP(@start_date)
      AND usage_start_time < TIMESTAMP(@end_date)
    GROUP BY usage_date, service, currency
    ORDER BY usage_date, service, currency"""


def summarize(rows):
    totals = {}
    output = []
    for row in rows:
        row = dict(row)
        currency = row["currency"]
        values = {key: Decimal(str(row[key])) for key in ("gross_cost", "credits", "net_cost")}
        if not all(value.is_finite() for value in values.values()):
            raise ValueError("Billing totals must be finite")
        if abs(values["gross_cost"] + values["credits"] - values["net_cost"]) > Decimal("0.000001"):
            raise ValueError("Billing costs and credits do not reconcile")
        output.append({**row, "usage_date": str(row["usage_date"]), **{key: str(value) for key, value in values.items()}})
        currency_totals = totals.setdefault(currency, {key: Decimal(0) for key in values})
        for key, value in values.items():
            currency_totals[key] += value
    return output, {currency: {key: str(value) for key, value in values.items()} for currency, values in totals.items()}


def run_report(table, start, end, maximum_bytes_billed=104857600, client=None):
    if start >= end or maximum_bytes_billed <= 0:
        raise ValueError("Choose a nonempty UTC date range and positive query byte cap")
    report = {"project": PROJECT, "start_date_inclusive_utc": str(start), "end_date_exclusive_utc": str(end),
              "generated_at": datetime.now(timezone.utc).isoformat(), "maximum_bytes_billed": maximum_bytes_billed,
              "scope": "Exported usage costs including credits, by currency; delayed/incomplete exports are not final invoices."}
    if not table:
        return {**report, "status": "billing_export_not_configured", "rows": [], "totals_by_currency": None,
                "next_action": "Enable standard billing export to pbmc-okf-portfolio.pbmc_billing_export, then pass its full table ID. No zero-cost claim is made."}
    query = query_for(table)
    from google.cloud import bigquery
    if client is None:
        client = bigquery.Client(project=PROJECT)
    params = [bigquery.ScalarQueryParameter("project", "STRING", PROJECT),
              bigquery.ScalarQueryParameter("start_date", "DATE", start),
              bigquery.ScalarQueryParameter("end_date", "DATE", end)]
    dry = client.query(query, location="EU", job_config=bigquery.QueryJobConfig(
        query_parameters=params, dry_run=True, use_query_cache=False, maximum_bytes_billed=maximum_bytes_billed))
    if dry.total_bytes_processed > maximum_bytes_billed:
        raise ValueError("Dry-run query estimate exceeds the byte cap; reduce the date range")
    job = client.query(query, location="EU", job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=maximum_bytes_billed))
    rows, totals = summarize(job.result())
    return {**report, "status": "reported" if rows else "no_export_rows_in_period", "table": table,
            "query_job_id": job.job_id, "bytes_processed": job.total_bytes_processed,
            "rows": rows, "totals_by_currency": totals if rows else None}


def main():
    today = datetime.now(timezone.utc).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table")
    parser.add_argument("--start", type=date.fromisoformat, default=today.replace(day=1))
    parser.add_argument("--end", type=date.fromisoformat, default=today + timedelta(days=1))
    parser.add_argument("--maximum-bytes-billed", type=int, default=104857600)
    parser.add_argument("--output", type=Path, default=Path("artifacts/cost-report.json"))
    args = parser.parse_args()
    try:
        report = run_report(args.table, args.start, args.end, args.maximum_bytes_billed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
    except Exception as error:
        parser.exit(1, f"Cost report failed: {error}\n")


if __name__ == "__main__":
    main()
