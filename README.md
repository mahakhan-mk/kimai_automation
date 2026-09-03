# Kimai Timesheet Importer

A small, intentionally conservative importer for the Octans Kimai instance.

## Safety model

- Default execution is **dry-run**. It performs lookups and validation but does not create timesheets.
- Writes require an explicit `--commit` flag.
- Use `--limit 1 --commit` for the first real API test.
- The token is read only from `.env` and `.env` is gitignored.
- The script only uses Kimai JSON API endpoints. It does not automate browser clicks and does not alter Kimai infrastructure/configuration.

## 1. Configure

Open `.env` and replace only:

```text
KIMAI_API_TOKEN=PASTE_YOUR_TOKEN_HERE
```

The current defaults are already set to:

```text
KIMAI_BASE_URL=https://octans-digital.kimai.cloud
KIMAI_CUSTOMER_NAME=KPMG-Canada
KIMAI_PROJECT_NAME=KPMG-Agentic SAR workflow automation-Stage-1
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

## 5. Dry-run

```powershell
python import_timesheets.py sample_timesheets.csv
```

This creates `import_report.json`, but writes nothing to Kimai.

## 6. First real test

After reviewing the dry-run:

```powershell
python import_timesheets.py sample_timesheets.csv --limit 1 --commit
```

Verify the resulting single entry in the Kimai UI before any bulk import.

## 7. Bulk import

Only after the one-entry test succeeds:

```powershell
python import_timesheets.py your_timesheets.csv --commit
```

## API notes

Kimai documents Bearer authentication as `Authorization: Bearer <token>` and uses `/api/timesheets` to create timesheets. The authenticated installation-specific Swagger page is authoritative for the exact version deployed by Octans.

The company `/api/doc` URL redirects unauthenticated requests to the Kimai login screen, so this package intentionally avoids assuming installation-specific custom fields. If your deployed Swagger schema contains required custom fields, add them before bulk import.
