from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
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

    @property
    def begin(self) -> str:
        return f"{self.date}T{self.start}:00" if len(self.start) == 5 else f"{self.date}T{self.start}"

    @property
    def finish(self) -> str:
        return f"{self.date}T{self.end}:00" if len(self.end) == 5 else f"{self.date}T{self.end}"


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
            raise ValueError(f"Row {i}: expected an object")
        tags_raw = item.get("tags", [])
        if isinstance(tags_raw, str):
            tags = [x.strip() for x in tags_raw.split(",") if x.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(x).strip() for x in tags_raw if str(x).strip()]
        else:
            raise ValueError(f"Row {i}: tags must be text or a list")
        row = InputRow(
            date=str(item.get("date", "")).strip(),
            start=str(item.get("start", "")).strip(),
            end=str(item.get("end", "")).strip(),
            activity=str(item.get("activity", "")).strip(),
            description=str(item.get("description", "")).strip(),
            tags=tags,
        )
        validate_row(row, i)
        rows.append(row)
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


def exact_by_name(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any]:
    matches = [x for x in items if str(x.get("name", "")).strip() == name]
    if len(matches) != 1:
        raise KimaiError(f"Expected exactly one {kind} named {name!r}, found {len(matches)}")
    return matches[0]


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely import Kimai timesheets from CSV or JSON")
    parser.add_argument("input", type=Path)
    parser.add_argument("--commit", action="store_true", help="Actually create entries. Without this flag nothing is written.")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N rows, useful for a one-entry test")
    args = parser.parse_args()

    try:
        config, customer_name, project_name = load_config()
        rows = load_rows(args.input)
        if args.limit is not None:
            rows = rows[: args.limit]

        with KimaiClient(config) as client:
            _, project_id = resolve_project(client, customer_name, project_name)
            activities = client.activities(project_id)
            activity_ids: dict[str, int] = {}
            for row in rows:
                activity = exact_by_name(activities, row.activity, "activity")
                activity_ids[row.activity] = int(activity["id"])

            print(f"Mode: {'COMMIT' if args.commit else 'DRY RUN'}")
            print(f"Project: {project_name} (id={project_id})")
            print(f"Rows: {len(rows)}")

            report: list[dict[str, Any]] = []
            for number, row in enumerate(rows, start=1):
                payload: dict[str, Any] = {
                    "begin": row.begin,
                    "end": row.finish,
                    "project": project_id,
                    "activity": activity_ids[row.activity],
                    "description": row.description,
                    "tags": row.tags,
                }
                print(f"[{number}] {row.begin} -> {row.finish} | {row.activity} | {row.description}")
                if args.commit:
                    created = client.create_timesheet(payload)
                    report.append({"row": number, "status": "created", "id": created.get("id"), "payload": payload})
                else:
                    report.append({"row": number, "status": "dry-run", "payload": payload})

            Path("import_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print("Wrote import_report.json")
            if not args.commit:
                print("No Kimai data was changed. Add --commit only after reviewing the dry run.")
        return 0
    except (ValueError, KimaiError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
