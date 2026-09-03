from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from kimai_client import KimaiClient, KimaiConfig, KimaiError


@dataclass(frozen=True)
class InputRow:
    date: str
    start: str
    end: str
    activity: str
    description: str
    tags: list[str]
    source_index: int = 0
    parse_error: str | None = None

    @property
    def begin(self) -> str:
        return f"{self.date}T{self.start}:00" if len(self.start) == 5 else f"{self.date}T{self.start}"

    @property
    def finish(self) -> str:
        return f"{self.date}T{self.end}:00" if len(self.end) == 5 else f"{self.date}T{self.end}"


@dataclass(frozen=True)
class PreflightResult:
    row_number: int
    row: InputRow
    status: str
    reason: str = ""
    payload: dict[str, Any] | None = None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> tuple[KimaiConfig, str, str]:
    load_dotenv()
    base_url = os.getenv("KIMAI_BASE_URL", "").strip()
    token = os.getenv("KIMAI_API_TOKEN", "").strip()
    customer = os.getenv("KIMAI_CUSTOMER_NAME", "").strip()
    project = os.getenv("KIMAI_PROJECT_NAME", "").strip()
    if not base_url or not token or token == "PASTE_YOUR_TOKEN_HERE":
        raise ValueError("Set KIMAI_BASE_URL and KIMAI_API_TOKEN in .env")
    if not customer or not project:
        raise ValueError("Set KIMAI_CUSTOMER_NAME and KIMAI_PROJECT_NAME in .env")
    timeout = float(os.getenv("KIMAI_TIMEOUT_SECONDS", "30"))
    verify = _bool_env("KIMAI_VERIFY_SSL", True)
    return KimaiConfig(base_url, token, timeout, verify), customer, project


def load_rows(path: Path) -> list[InputRow]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            raw = list(csv.DictReader(handle))
    elif path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("JSON input must be an array of objects")
    else:
        raise ValueError("Input must be .csv or .json")

    rows: list[InputRow] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            rows.append(InputRow("", "", "", "", "", [], i, "expected an object"))
            continue
        tags_raw = item.get("tags", [])
        parse_error: str | None = None
        if isinstance(tags_raw, str):
            tags = [x.strip() for x in tags_raw.split(",") if x.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(x).strip() for x in tags_raw if str(x).strip()]
        else:
            tags = []
            parse_error = "tags must be text or a list"
        rows.append(
            InputRow(
                date=str(item.get("date", "")).strip(),
                start=str(item.get("start", "")).strip(),
                end=str(item.get("end", "")).strip(),
                activity=str(item.get("activity", "")).strip(),
                description=str(item.get("description", "")).strip(),
                tags=tags,
                source_index=i,
                parse_error=parse_error,
            )
        )
    if not rows:
        raise ValueError("Input contains no timesheet rows")
    return rows


def validate_row(row: InputRow, index: int) -> None:
    if not row.activity:
        raise ValueError(f"Row {index}: activity is required")
    try:
        begin = datetime.fromisoformat(row.begin)
        end = datetime.fromisoformat(row.finish)
    except ValueError as exc:
        raise ValueError(f"Row {index}: invalid date/time") from exc
    if end <= begin:
        raise ValueError(f"Row {index}: end must be later than start")


def normalize_local_datetime(value: str) -> datetime:
    """Parse local input and Kimai's offset-bearing local response safely."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        # Kimai already renders the timestamp in the user's configured
        # timezone, so compare its wall-clock fields with local input fields.
        return parsed.replace(tzinfo=None)
    return parsed


def exact_by_name(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any]:
    matches = [x for x in items if str(x.get("name", "")).strip() == name]
    if len(matches) != 1:
        raise KimaiError(f"Expected exactly one {kind} named {name!r}, found {len(matches)}")
    return matches[0]


def build_timesheet_payload(row: InputRow, project_id: int, activity_id: int) -> dict[str, Any]:
    """Build only fields accepted for a new Kimai timesheet."""
    payload: dict[str, Any] = {
        "begin": row.begin,
        "end": row.finish,
        "project": project_id,
        "activity": activity_id,
        "description": row.description,
    }
    tags = [str(tag).strip() for tag in row.tags if str(tag).strip()]
    if tags:
        payload["tags"] = ",".join(tags)
    return payload


def resolve_project(client: KimaiClient, customer_name: str, project_name: str) -> tuple[int, int]:
    customer = exact_by_name(client.customers(), customer_name, "customer")
    customer_id = int(customer["id"])
    projects = [p for p in client.projects() if str(p.get("name", "")).strip() == project_name]
    projects = [p for p in projects if int(p.get("customer", customer_id)) == customer_id]
    if len(projects) != 1:
        raise KimaiError(
            f"Expected exactly one project {project_name!r} under {customer_name!r}, found {len(projects)}"
        )
    return customer_id, int(projects[0]["id"])


def _time_range(item: dict[str, Any]) -> tuple[datetime, datetime | None] | None:
    begin = item.get("begin")
    if not isinstance(begin, str) or not begin:
        return None
    try:
        parsed_begin = normalize_local_datetime(begin)
        end = item.get("end")
        parsed_end = normalize_local_datetime(end) if isinstance(end, str) and end else None
    except ValueError:
        return None
    if parsed_end is not None and parsed_end <= parsed_begin:
        return None
    return parsed_begin, parsed_end


def _overlaps(
    first_begin: datetime,
    first_end: datetime,
    second_begin: datetime,
    second_end: datetime | None,
) -> bool:
    second_end_value = second_end or datetime.max
    return first_begin < second_end_value and second_begin < first_end


def classify_rows(
    rows: list[InputRow],
    project_id: int,
    activity_ids: dict[str, int],
    existing_timesheets: list[dict[str, Any]],
) -> list[PreflightResult]:
    """Classify every candidate before a commit can create anything."""
    results: list[PreflightResult] = []
    parsed_rows: dict[int, tuple[datetime, datetime]] = {}

    for number, row in enumerate(rows, start=1):
        try:
            if row.parse_error:
                raise ValueError(row.parse_error)
            validate_row(row, number)
            begin = normalize_local_datetime(row.begin)
            end = normalize_local_datetime(row.finish)
            if row.activity not in activity_ids:
                raise ValueError(f"activity {row.activity!r} was not found for the project")
            parsed_rows[number] = (begin, end)
            results.append(
                PreflightResult(
                    number,
                    row,
                    "READY",
                    payload=build_timesheet_payload(row, project_id, activity_ids[row.activity]),
                )
            )
        except ValueError as exc:
            results.append(PreflightResult(number, row, "INVALID", str(exc)))

    exact_input_counts: dict[tuple[datetime, datetime], int] = {}
    for begin, end in parsed_rows.values():
        key = (begin, end)
        exact_input_counts[key] = exact_input_counts.get(key, 0) + 1

    existing_ranges = [
        time_range
        for item in existing_timesheets
        if (time_range := _time_range(item)) is not None
    ]
    existing_exact = {
        (begin, end)
        for begin, end in existing_ranges
        if end is not None
    }

    classified: list[PreflightResult] = []
    for result in results:
        if result.status != "READY":
            classified.append(result)
            continue
        begin, end = parsed_rows[result.row_number]
        key = (begin, end)
        if exact_input_counts[key] > 1:
            classified.append(
                PreflightResult(
                    result.row_number,
                    result.row,
                    "DUPLICATE",
                    "duplicate candidate row in input",
                    result.payload,
                )
            )
        elif any(
            other_number != result.row_number
            and _overlaps(begin, end, other_begin, other_end)
            for other_number, (other_begin, other_end) in parsed_rows.items()
        ):
            classified.append(
                PreflightResult(
                    result.row_number,
                    result.row,
                    "CONFLICT",
                    "overlaps another input row",
                    result.payload,
                )
            )
        elif key in existing_exact:
            classified.append(
                PreflightResult(
                    result.row_number,
                    result.row,
                    "DUPLICATE",
                    "exact begin/end already exists",
                    result.payload,
                )
            )
        elif any(
            _overlaps(begin, end, existing_begin, existing_end)
            for existing_begin, existing_end in existing_ranges
        ):
            classified.append(
                PreflightResult(
                    result.row_number,
                    result.row,
                    "CONFLICT",
                    "overlaps an existing timesheet",
                    result.payload,
                )
            )
        else:
            classified.append(result)
    return classified


def existing_timesheet_query_range(rows: list[InputRow]) -> tuple[str, str] | None:
    """Return a local query range, including earlier same-day entries."""
    valid_ranges: list[tuple[datetime, datetime]] = []
    for row in rows:
        try:
            if row.parse_error:
                continue
            validate_row(row, row.source_index or 0)
            valid_ranges.append((normalize_local_datetime(row.begin), normalize_local_datetime(row.finish)))
        except ValueError:
            continue
    if not valid_ranges:
        return None
    earliest = min(begin for begin, _ in valid_ranges)
    latest = max(end for _, end in valid_ranges)
    query_begin = earliest.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return query_begin.strftime("%Y-%m-%dT%H:%M:%S"), latest.strftime("%Y-%m-%dT%H:%M:%S")


def summarize(results: list[PreflightResult]) -> dict[str, int]:
    counts = {status: 0 for status in ("READY", "DUPLICATE", "CONFLICT", "INVALID")}
    for result in results:
        counts[result.status] += 1
    return counts


def _report_rows(results: list[PreflightResult], created: dict[int, Any] | None = None) -> list[dict[str, Any]]:
    created = created or {}
    report: list[dict[str, Any]] = []
    for result in results:
        entry: dict[str, Any] = {
            "row": result.row_number,
            "status": "created" if result.row_number in created else result.status,
            "preflight": result.status,
            "payload": result.payload,
        }
        if result.reason:
            entry["reason"] = result.reason
        if result.row_number in created:
            entry["id"] = created[result.row_number]
        report.append(entry)
    return report


def _write_report(results: list[PreflightResult], created: dict[int, Any] | None = None) -> None:
    Path("import_report.json").write_text(
        json.dumps(_report_rows(results, created), indent=2),
        encoding="utf-8",
    )


def _print_preflight(results: list[PreflightResult]) -> None:
    for result in results:
        row = result.row
        print(f"[{result.row_number}] {result.status} | {row.begin} -> {row.finish} | {row.activity} | {row.description}")
        print("  POST payload:")
        print(json.dumps(result.payload, indent=2) if result.payload is not None else "  unavailable")
        if result.reason:
            print(f"  Reason: {result.reason}")


def _print_summary(results: list[PreflightResult], writes_performed: int) -> None:
    counts = summarize(results)
    print(f"Rows: {len(results)}")
    print(f"READY: {counts['READY']}")
    print(f"DUPLICATE: {counts['DUPLICATE']}")
    print(f"CONFLICT: {counts['CONFLICT']}")
    print(f"INVALID: {counts['INVALID']}")
    print(f"Writes performed: {writes_performed}")


def run_import(
    client: KimaiClient,
    rows: list[InputRow],
    customer_name: str,
    project_name: str,
    commit: bool = False,
) -> int:
    """Run lookup, preflight, and optionally the already-authorized writes."""
    _, project_id = resolve_project(client, customer_name, project_name)
    activities = client.activities(project_id)
    activity_ids = {
        str(activity.get("name", "")).strip(): int(activity["id"])
        for activity in activities
        if activity.get("name") is not None and activity.get("id") is not None
    }

    query_range = existing_timesheet_query_range(rows)
    existing_timesheets = (
        client.all_timesheets(begin=query_range[0], end=query_range[1])
        if query_range is not None
        else []
    )
    results = classify_rows(rows, project_id, activity_ids, existing_timesheets)

    print(f"Mode: {'COMMIT' if commit else 'DRY RUN'}")
    print(f"Project: {project_name} (id={project_id})")
    _print_preflight(results)
    _write_report(results)

    counts = summarize(results)
    blocked = counts["DUPLICATE"] + counts["CONFLICT"] + counts["INVALID"]
    if commit and blocked:
        print("ERROR: --commit refused because preflight found blocked rows; no entries were created.", file=sys.stderr)
        _print_summary(results, 0)
        print("Wrote import_report.json")
        return 1

    created: dict[int, Any] = {}
    if commit:
        print(f"Preflight passed: {counts['READY']} entries ready to create.")
        for result in results:
            if result.status != "READY" or result.payload is None:
                continue
            created_result = client.create_timesheet(result.payload)
            created[result.row_number] = created_result.get("id")
        _write_report(results, created)
    else:
        print("No Kimai data was changed.")

    _print_summary(results, len(created))
    print("Wrote import_report.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely import Kimai timesheets from CSV or JSON")
    parser.add_argument("input", type=Path)
    parser.add_argument("--commit", action="store_true", help="Actually create entries. Without this flag nothing is written.")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N rows")
    args = parser.parse_args()

    try:
        config, customer_name, project_name = load_config()
        rows = load_rows(args.input)
        if args.limit is not None:
            rows = rows[: args.limit]

        with KimaiClient(config) as client:
            return run_import(client, rows, customer_name, project_name, args.commit)
    except (ValueError, KimaiError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
