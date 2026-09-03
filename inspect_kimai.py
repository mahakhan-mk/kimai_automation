from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

from import_timesheets import exact_by_name
from kimai_client import KimaiClient, KimaiConfig, KimaiError


TARGET_PROJECT_NAME = "KPMG-Agentic SAR workflow automation-Stage-1"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_inspection_config() -> tuple[KimaiConfig, str, str]:
    load_dotenv()
    base_url = os.getenv("KIMAI_BASE_URL", "").strip()
    token = os.getenv("KIMAI_API_TOKEN", "").strip()
    customer_name = os.getenv("KIMAI_CUSTOMER_NAME", "").strip()
    project_name = os.getenv("KIMAI_PROJECT_NAME", TARGET_PROJECT_NAME).strip() or TARGET_PROJECT_NAME
    if not base_url or not token or token == "PASTE_YOUR_TOKEN_HERE":
        raise ValueError("Set KIMAI_BASE_URL and KIMAI_API_TOKEN in .env")
    timeout = float(os.getenv("KIMAI_TIMEOUT_SECONDS", "30"))
    verify = _bool_env("KIMAI_VERIFY_SSL", True)
    return KimaiConfig(base_url, token, timeout, verify), customer_name, project_name


def _related_id(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def resolve_project_record(
    projects: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    project_name: str,
    customer_name: str = "",
    project_id: int | None = None,
) -> dict[str, Any]:
    if project_id is not None:
        matches = [p for p in projects if _related_id(p.get("id")) == project_id]
        if len(matches) != 1:
            raise KimaiError(f"Expected exactly one project with id {project_id}, found {len(matches)}")
        return matches[0]

    candidates = [p for p in projects if str(p.get("name", "")).strip() == project_name]
    if customer_name:
        customer = exact_by_name(customers, customer_name, "customer")
        customer_id = _related_id(customer.get("id"))
        candidates = [p for p in candidates if _related_id(p.get("customer")) == customer_id]
    if len(candidates) != 1:
        scope = f" under {customer_name!r}" if customer_name else ""
        raise KimaiError(f"Expected exactly one project {project_name!r}{scope}, found {len(candidates)}")
    return candidates[0]


def _label(value: Any) -> str:
    if isinstance(value, dict):
        identifier = value.get("id", "?")
        name = str(value.get("name", "")).strip()
        return f"{identifier} ({name})" if name else str(identifier)
    if value is None:
        return "?"
    return str(value)


def _version_label(version: Any) -> str:
    if isinstance(version, dict):
        for key in ("version", "kimai", "name"):
            if version.get(key):
                return str(version[key])
    return str(version)


def inspect_api(
    client: KimaiClient,
    project_name: str,
    customer_name: str = "",
    project_id: int | None = None,
    begin: str | None = None,
    end: str | None = None,
    all_projects: bool = False,
    raw_timesheet: bool = False,
) -> None:
    """Inspect Kimai using GET requests only; this function has no write path."""
    version = client.version()
    user = client.user_me()

    print(f"Kimai version: {_version_label(version)}")
    print(f"Current user: id={user.get('id', '?')} | username={user.get('username', '?')}")

    resolved_project_id: int | None = None
    if all_projects:
        print("Project filter: none (all projects)")
        print("\nActivities available for this project: skipped in --all-projects mode")
    else:
        customers = client.customers()
        projects = client.projects()
        project = resolve_project_record(projects, customers, project_name, customer_name, project_id)
        resolved_project_id = int(project["id"])
        activities = client.activities(project_id=resolved_project_id)
        print(f"Resolved project: {_label(project)}")
        print("\nActivities available for this project:")
        if activities:
            for activity in activities:
                print(f"  - id={activity.get('id', '?')} | name={activity.get('name', '')}")
        else:
            print("  (none returned)")

    try:
        tags = client.tags_find("")
    except KimaiError as exc:
        print(f"\nAvailable tags: unavailable ({exc})")
    else:
        print(f"\nAvailable tags: {len(tags)}")

    if all_projects:
        timesheets = client.timesheets(
            begin=begin,
            end=end,
            size=10,
            order_by="begin",
            order="DESC",
        )
    else:
        timesheets = client.timesheets(project_id=resolved_project_id, begin=begin, end=end)
    sample = timesheets[:10]
    filter_text = []
    if begin:
        filter_text.append(f"begin>={begin}")
    if end:
        filter_text.append(f"end<={end}")
    suffix = f" ({', '.join(filter_text)})" if filter_text else ""
    print(f"\nReadable timesheet sample{suffix}: {len(sample)} of {len(timesheets)} returned")
    if sample:
        for item in sample:
            print(
                "  - "
                f"id={item.get('id', '?')} | "
                f"begin={item.get('begin', '')} | "
                f"end={item.get('end', '')} | "
                f"project={_label(item.get('project', resolved_project_id))} | "
                f"activity={_label(item.get('activity'))} | "
                f"description={item.get('description', '')}"
            )
    else:
        print("  (none returned)")

    if raw_timesheet and timesheets:
        print("\nRaw first timesheet:")
        print(json.dumps(timesheets[0], indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only inspection of the configured Kimai API")
    parser.add_argument("--project-id", type=int, help="Inspect this project ID instead of resolving by name")
    parser.add_argument("--begin", help="Timesheet filter, YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--end", help="Timesheet filter, YYYY-MM-DDTHH:MM:SS")
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Inspect the latest current-user timesheets across all projects",
    )
    parser.add_argument(
        "--raw-timesheet",
        action="store_true",
        help="Pretty-print the complete first returned timesheet JSON",
    )
    args = parser.parse_args()

    try:
        config, customer_name, project_name = load_inspection_config()
        with KimaiClient(config) as client:
            inspect_api(
                client,
                project_name,
                customer_name,
                args.project_id,
                args.begin,
                args.end,
                args.all_projects,
                args.raw_timesheet,
            )
        return 0
    except (ValueError, KimaiError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
