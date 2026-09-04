# Kimai Timesheet Importer

A small, intentionally conservative importer for the Octans Kimai instance.

## Safety model

- Default execution is **dry-run**. It performs lookups and validation but does not create timesheets.
- Writes require an explicit `--commit` flag.
- Every run preflights the input against existing timesheets; `--commit` refuses the entire batch if any row is invalid, duplicated, or overlapping.
- Inspect the live API before considering any write; the inspection workflow is strictly read-only.
- The token is read only from `.env` and `.env` is gitignored.
- The script only uses Kimai JSON API endpoints. It does not automate browser clicks and does not alter Kimai infrastructure/configuration.

## 1. Configure

Open `.env` and replace only:

```text
KIMAI_API_TOKEN=PASTE_YOUR_TOKEN_HERE
```

The current defaults are already set to:

```text
KIMAI_BASE_URL=https://example.kimai.cloud
KIMAI_CUSTOMER_NAME=ABC
KIMAI_PROJECT_NAME=workflow automation-Stage-1
```

Do not share or commit `.env`.

## 2. Install

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Verify API access

```powershell
python check_connection.py
```

This performs read-only calls.

## 4. Input format

Both CSV and JSON are supported. We can change the schema later when the real five-month dataset is prepared.

CSV:

```csv
date,start,end,activity,description,tags
2026-09-03,09:00,17:00,Developer - Coding,Example only,
```

JSON:

```json
[
  {
    "date": "2026-09-03",
    "start": "09:00",
    "end": "17:00",
    "activity": "Developer - Coding",
    "description": "Example only",
    "tags": []
  }
]
```

Times are sent as Kimai HTML5 local datetime strings such as `2026-09-03T09:00:00`. No timezone offset is added.

## 5. Inspect the live API

Run this before preparing or committing any import:

```powershell
python inspect_kimai.py
```

Optional timesheet filters can narrow the inspection:

```powershell
python inspect_kimai.py --project-id 59 --begin 2026-01-01T00:00:00 --end 2026-01-31T23:59:59
```

The script reads the Kimai version, resolves the configured project, lists its activities and available tags, and prints a small sample of readable timesheets. It performs GET requests only.

## 6. Dry-run

```powershell
python import_timesheets.py data/2026-08.csv --expected-month 2026-08
```

This creates `import_report.json`, but writes nothing to Kimai.

The dry-run prints the minimal POST payload for each row and classifies it as `READY`, `DUPLICATE`, `CONFLICT`, or `INVALID`. Existing timesheets are checked across all projects, with complete pagination, so overlapping work in another project also blocks the batch. `--expected-month` is optional but recommended for month-by-month imports.

## 7. Explicit commit

Only after reviewing the inspection output and dry-run:

```powershell
python import_timesheets.py data/2026-08.csv --expected-month 2026-08 --commit
```

`--commit` is the only normal write path. It refuses the entire batch if any row is invalid, duplicated, or overlapping. The durable report is updated after each successful entry.

## 8. Resume after a partial failure

Use `--resume` only after resolving the reported write failure:

```powershell
python import_timesheets.py data/2026-08.csv --expected-month 2026-08 --commit --resume
```

The importer verifies that `import_report.json` matches the same input and payloads, confirms previously created IDs through GET, skips only those proven importer-created rows, and stops again on any later write failure. Do not use `--resume` with an arbitrary or edited report.

## API notes

Kimai documents Bearer authentication as `Authorization: Bearer <token>` and uses `/api/timesheets` to create timesheets. The authenticated installation-specific Swagger page is authoritative for the exact version deployed by Octans.

The company `/api/doc` URL redirects unauthenticated requests to the Kimai login screen, so this package intentionally avoids assuming installation-specific custom fields. If your deployed Swagger schema contains required custom fields, add them before bulk import.
