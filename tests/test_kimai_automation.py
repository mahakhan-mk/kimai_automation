from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import httpx

from import_timesheets import (
    InputRow,
    build_timesheet_payload,
    classify_rows,
    run_import,
    validate_expected_month,
)
from inspect_kimai import inspect_api
from kimai_client import KimaiClient, KimaiConfig, KimaiError


class KimaiClientReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = KimaiClient(KimaiConfig("https://example.test", "token"))
        self.addCleanup(self.client.close)

    def test_activity_lookup_by_project(self) -> None:
        self.client._request = Mock(return_value=[])

        self.client.activities(project_id=59)

        self.client._request.assert_called_once_with(
            "GET", "/activities", params={"project": "59"}
        )

    def test_timesheet_get_filters(self) -> None:
        self.client._request = Mock(return_value=[])

        self.client.timesheets(
            begin="2026-01-01T00:00:00",
            end="2026-01-31T23:59:59",
            project_id=59,
        )

        self.client._request.assert_called_once_with(
            "GET",
            "/timesheets",
            params={
                "project": "59",
                "begin": "2026-01-01T00:00:00",
                "end": "2026-01-31T23:59:59",
            },
        )

    def test_user_me_uses_get(self) -> None:
        self.client._request = Mock(return_value={"id": 3, "username": "alice"})

        self.assertEqual(self.client.user_me(), {"id": 3, "username": "alice"})

        self.client._request.assert_called_once_with("GET", "/users/me")

    def test_all_project_timesheets_have_no_project_filter(self) -> None:
        self.client._request = Mock(return_value=[])

        self.client.timesheets(size=10, order_by="begin", order="DESC")

        self.client._request.assert_called_once_with(
            "GET",
            "/timesheets",
            params={"size": "10", "orderBy": "begin", "order": "DESC"},
        )

    def test_all_timesheets_stops_after_short_page(self) -> None:
        self.client.timesheets = Mock(return_value=[{"id": 1}] * 499)

        records = self.client.all_timesheets(begin="2026-01-01T00:00:00", end="2026-01-31T23:59:59")

        self.assertEqual(len(records), 499)
        self.client.timesheets.assert_called_once_with(
            begin="2026-01-01T00:00:00",
            end="2026-01-31T23:59:59",
            page=1,
            size=500,
        )

    def test_all_timesheets_retrieves_page_two_after_exactly_500(self) -> None:
        self.client.timesheets = Mock(side_effect=[[{"id": 1}] * 500, [{"id": 501}]])

        records = self.client.all_timesheets()

        self.assertEqual(len(records), 501)
        self.assertEqual(self.client.timesheets.call_args_list, [
            call(begin=None, end=None, page=1, size=500),
            call(begin=None, end=None, page=2, size=500),
        ])

    def test_all_timesheets_retrieves_multiple_full_pages(self) -> None:
        self.client.timesheets = Mock(side_effect=[
            [{"id": 1}] * 500,
            [{"id": 501}] * 500,
            [{"id": 1001}] * 2,
        ])

        records = self.client.all_timesheets()

        self.assertEqual(len(records), 1002)
        self.assertEqual(self.client.timesheets.call_count, 3)
        for page_number, call in enumerate(self.client.timesheets.call_args_list, start=1):
            self.assertEqual(call.kwargs, {"begin": None, "end": None, "page": page_number, "size": 500})

    def test_all_timesheets_preserves_filters_on_every_page_and_has_no_project(self) -> None:
        self.client.timesheets = Mock(side_effect=[[{"id": 1}] * 500, [{"id": 501}] * 2])

        self.client.all_timesheets(begin="2026-02-01T00:00:00", end="2026-02-28T23:59:59")

        for call in self.client.timesheets.call_args_list:
            self.assertEqual(call.kwargs["begin"], "2026-02-01T00:00:00")
            self.assertEqual(call.kwargs["end"], "2026-02-28T23:59:59")
            self.assertNotIn("project_id", call.kwargs)
            self.assertNotIn("project", call.kwargs)

    def test_all_timesheets_page_guard_raises(self) -> None:
        self.client.timesheets = Mock(return_value=[{"id": 1}] * 500)

        with self.assertRaisesRegex(KimaiError, "pagination exceeded"):
            self.client.all_timesheets(max_pages=2)

        self.assertEqual(self.client.timesheets.call_count, 2)

    def test_tags_find_uses_get(self) -> None:
        self.client._request = Mock(return_value=[])

        self.client.tags_find("")

        self.client._request.assert_called_once_with("GET", "/tags/find", params={"name": ""})

    def test_api_failure_contains_method_status_and_body(self) -> None:
        response = httpx.Response(403, text="Access denied")
        self.client.client.request = Mock(return_value=response)

        with self.assertRaisesRegex(KimaiError, r"GET /version failed: HTTP 403: Access denied"):
            self.client.version()

    def test_transport_failure_is_wrapped(self) -> None:
        self.client.client.request = Mock(side_effect=httpx.ConnectError("connection refused"))

        with self.assertRaisesRegex(KimaiError, r"GET /version failed: connection refused"):
            self.client.version()


class ImportPayloadTests(unittest.TestCase):
    def test_empty_tags_are_omitted(self) -> None:
        row = InputRow("2026-09-03", "09:00", "17:00", "Coding", "Work", [])

        payload = build_timesheet_payload(row, project_id=59, activity_id=7)

        self.assertEqual(
            set(payload),
            {"begin", "end", "project", "activity", "description"},
        )
        self.assertNotIn("tags", payload)
        self.assertNotIn("customer", payload)
        self.assertNotIn("user", payload)
        self.assertNotIn("id", payload)
        self.assertNotIn("duration", payload)
        self.assertNotIn("break", payload)
        self.assertNotIn("rate", payload)
        self.assertNotIn("internalRate", payload)
        self.assertNotIn("exported", payload)
        self.assertNotIn("billable", payload)
        self.assertNotIn("metaFields", payload)

    def test_tags_are_comma_separated_string_not_json_array(self) -> None:
        row = InputRow("2026-09-03", "09:00", "17:00", "Coding", "Work", ["client", "remote"])

        payload = build_timesheet_payload(row, project_id=59, activity_id=7)

        self.assertEqual(payload["tags"], "client,remote")
        self.assertIsInstance(payload["tags"], str)
        self.assertNotIsInstance(payload["tags"], list)


class ReadOnlyInspectionTests(unittest.TestCase):
    def test_inspection_workflow_performs_get_backed_calls_only(self) -> None:
        class FakeReadClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def version(self):
                self.calls.append("version")
                return {"version": "2.65.0"}

            def user_me(self):
                self.calls.append("user_me")
                return {"id": 3, "username": "alice"}

            def customers(self):
                self.calls.append("customers")
                return [{"id": 4, "name": "KPMG-Canada"}]

            def projects(self):
                self.calls.append("projects")
                return [{"id": 59, "name": "Target", "customer": 4}]

            def activities(self, project_id=None):
                self.calls.append(f"activities:{project_id}")
                return [{"id": 7, "name": "Coding"}]

            def tags_find(self, name=""):
                self.calls.append(f"tags_find:{name}")
                return [{"id": 2, "name": "remote"}]

            def timesheets(self, begin=None, end=None, project_id=None):
                self.calls.append(f"timesheets:{project_id}:{begin}:{end}")
                return [{
                    "id": 100,
                    "begin": "2026-09-03T09:00:00+00:00",
                    "end": "2026-09-03T17:00:00+00:00",
                    "project": {"id": 59, "name": "Target"},
                    "activity": {"id": 7, "name": "Coding"},
                    "description": "Work",
                }]

        client = FakeReadClient()
        output = io.StringIO()
        with redirect_stdout(output):
            inspect_api(client, "Target", "KPMG-Canada", begin="2026-09-01T00:00:00")

        self.assertEqual(
            client.calls,
            [
                "version",
                "user_me",
                "customers",
                "projects",
                "activities:59",
                "tags_find:",
                "timesheets:59:2026-09-01T00:00:00:None",
            ],
        )
        rendered = output.getvalue()
        self.assertIn("Kimai version: 2.65.0", rendered)
        self.assertIn("Current user: id=3 | username=alice", rendered)
        self.assertIn("id=7 | name=Coding", rendered)
        self.assertIn("id=100", rendered)
        self.assertNotIn("POST", rendered)

    def test_all_projects_uses_latest_current_user_timesheets_and_raw_is_first_only(self) -> None:
        class FakeAllProjectsClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            def version(self):
                self.calls.append(("version", None))
                return {"version": "2.65.0"}

            def user_me(self):
                self.calls.append(("user_me", None))
                return {"id": 3, "username": "alice"}

            def tags_find(self, name=""):
                self.calls.append(("tags_find", name))
                return [{"id": 2, "name": "remote"}, {"id": 4, "name": "client"}]

            def timesheets(self, begin=None, end=None, project_id=None, size=None, order_by=None, order=None):
                self.calls.append(("timesheets", {
                    "begin": begin,
                    "end": end,
                    "project_id": project_id,
                    "size": size,
                    "order_by": order_by,
                    "order": order,
                }))
                return [
                    {"id": 10, "begin": "2026-09-03T09:00:00+00:00", "description": "latest"},
                    {"id": 9, "begin": "2026-09-02T09:00:00+00:00", "description": "older"},
                ]

        client = FakeAllProjectsClient()
        output = io.StringIO()
        with redirect_stdout(output):
            inspect_api(client, "Configured project", "KPMG-Canada", all_projects=True, raw_timesheet=True)

        self.assertEqual(
            client.calls,
            [
                ("version", None),
                ("user_me", None),
                ("tags_find", ""),
                ("timesheets", {
                    "begin": None,
                    "end": None,
                    "project_id": None,
                    "size": 10,
                    "order_by": "begin",
                    "order": "DESC",
                }),
            ],
        )
        rendered = output.getvalue()
        self.assertIn("Project filter: none (all projects)", rendered)
        self.assertIn("Available tags: 2", rendered)
        self.assertNotIn("remote", rendered.split("Available tags:", 1)[1].split("Readable timesheet", 1)[0])
        raw = json.loads(rendered.split("Raw first timesheet:\n", 1)[1])
        self.assertEqual(raw, {"id": 10, "begin": "2026-09-03T09:00:00+00:00", "description": "latest"})
        self.assertNotIn("POST", rendered)
        self.assertNotIn("PATCH", rendered)
        self.assertNotIn("DELETE", rendered)


class PreflightTests(unittest.TestCase):
    def row(self, start: str, end: str, description: str = "Work") -> InputRow:
        return InputRow("2026-09-03", start, end, "Coding", description, [])

    def statuses(self, rows, existing=None):
        results = classify_rows(rows, 59, {"Coding": 7}, existing or [])
        return [result.status for result in results]

    def test_exact_duplicate_detection(self) -> None:
        rows = [self.row("09:00", "10:00")]
        existing = [{"begin": "2026-09-03T09:00:00+0500", "end": "2026-09-03T10:00:00+0500"}]

        self.assertEqual(self.statuses(rows, existing), ["DUPLICATE"])

    def test_overlap_detection_is_independent_of_project(self) -> None:
        rows = [self.row("09:00", "10:00")]
        existing = [{
            "begin": "2026-09-03T09:30:00+0500",
            "end": "2026-09-03T11:00:00+0500",
            "project": {"id": 999, "name": "Other project"},
        }]

        self.assertEqual(self.statuses(rows, existing), ["CONFLICT"])

    def test_non_overlapping_entry_is_ready(self) -> None:
        rows = [self.row("11:00", "12:00")]
        existing = [{"begin": "2026-09-03T09:00:00+0500", "end": "2026-09-03T10:00:00+0500"}]

        self.assertEqual(self.statuses(rows, existing), ["READY"])

    def test_duplicate_rows_inside_input_are_rejected(self) -> None:
        rows = [self.row("09:00", "10:00", "first"), self.row("09:00", "10:00", "second")]

        self.assertEqual(self.statuses(rows), ["DUPLICATE", "DUPLICATE"])

    def test_overlapping_rows_inside_input_are_conflicts(self) -> None:
        rows = [self.row("09:00", "10:00", "first"), self.row("09:30", "10:30", "second")]

        self.assertEqual(self.statuses(rows), ["CONFLICT", "CONFLICT"])

    def test_timezone_aware_get_timestamps_match_local_input(self) -> None:
        rows = [self.row("09:00", "10:00")]
        existing = [{"begin": "2026-09-03T09:00:00+05:00", "end": "2026-09-03T10:00:00+05:00"}]

        self.assertEqual(self.statuses(rows, existing), ["DUPLICATE"])

    def test_invalid_row_is_classified(self) -> None:
        rows = [InputRow("2026-09-03", "bad", "10:00", "Coding", "Work", [])]

        self.assertEqual(self.statuses(rows), ["INVALID"])

    def test_expected_month_accepts_all_rows_in_month(self) -> None:
        rows = [
            InputRow("2026-08-01", "09:00", "10:00", "Coding", "first", []),
            InputRow("2026-08-31", "11:00", "12:00", "Coding", "last", []),
        ]

        results = classify_rows(rows, 59, {"Coding": 7}, [], expected_month="2026-08")

        self.assertEqual([result.status for result in results], ["READY", "READY"])

    def test_expected_month_outside_row_is_invalid(self) -> None:
        rows = [
            InputRow("2026-08-31", "09:00", "10:00", "Coding", "valid", []),
            InputRow("2026-09-01", "09:00", "10:00", "Coding", "outside", []),
        ]

        results = classify_rows(rows, 59, {"Coding": 7}, [], expected_month="2026-08")

        self.assertEqual([result.status for result in results], ["READY", "INVALID"])

    def test_invalid_expected_month_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid YYYY-MM"):
            validate_expected_month("2026-13")

        with self.assertRaisesRegex(ValueError, "YYYY-MM"):
            validate_expected_month("2026-8")


class ImportSafetyFlowTests(unittest.TestCase):
    class FakeImportClient:
        def __init__(self, existing=None):
            self.existing = existing or []
            self.calls = []

        def customers(self):
            self.calls.append("customers")
            return [{"id": 4, "name": "KPMG-Canada"}]

        def projects(self):
            self.calls.append("projects")
            return [{"id": 59, "name": "Target", "customer": 4}]

        def activities(self, project_id):
            self.calls.append(("activities", project_id))
            return [{"id": 7, "name": "Coding"}]

        def all_timesheets(self, begin=None, end=None):
            self.calls.append(("timesheets", begin, end, None))
            return self.existing

        def create_timesheet(self, payload):
            self.calls.append(("create_timesheet", payload))
            return {"id": 123}

    def row(self, start="09:00", end="10:00"):
        return InputRow("2026-09-03", start, end, "Coding", "Work", [])

    def execute(self, client, rows, commit=False, expected_month=None, resume=False):
        with patch("import_timesheets._write_report_atomic"):
            return run_import(client, rows, "KPMG-Canada", "Target", commit, expected_month, resume)

    def test_commit_refuses_entire_batch_if_one_row_conflicts(self) -> None:
        client = self.FakeImportClient([{
            "begin": "2026-09-03T09:30:00+0500",
            "end": "2026-09-03T11:00:00+0500",
        }])

        result = self.execute(client, [self.row(), self.row("11:00", "12:00")], commit=True)

        self.assertEqual(result, 1)
        self.assertFalse(any(call[0] == "create_timesheet" for call in client.calls if isinstance(call, tuple)))

    def test_expected_month_blocks_entire_commit_batch(self) -> None:
        client = self.FakeImportClient()

        result = self.execute(
            client,
            [self.row(), InputRow("2026-10-01", "09:00", "10:00", "Coding", "outside", [])],
            commit=True,
            expected_month="2026-09",
        )

        self.assertEqual(result, 1)
        self.assertFalse(any(
            isinstance(call, tuple) and call[0] == "create_timesheet"
            for call in client.calls
        ))

    def test_dry_run_performs_no_write_methods(self) -> None:
        client = self.FakeImportClient()
        output = io.StringIO()

        with redirect_stdout(output):
            result = self.execute(client, [self.row()])

        self.assertEqual(result, 0)
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "create_timesheet" for call in client.calls))
        self.assertIn('"project": 59', output.getvalue())
        self.assertIn("READY: 1", output.getvalue())
        self.assertIn("Writes performed: 0", output.getvalue())

    def test_valid_commit_creates_only_after_complete_preflight(self) -> None:
        client = self.FakeImportClient()

        result = self.execute(client, [self.row(), self.row("11:00", "12:00")], commit=True)

        self.assertEqual(result, 0)
        create_positions = [i for i, call in enumerate(client.calls) if isinstance(call, tuple) and call[0] == "create_timesheet"]
        self.assertEqual(len(create_positions), 2)
        self.assertGreater(create_positions[0], client.calls.index(("timesheets", "2026-09-02T00:00:00", "2026-09-03T12:00:00", None)))

    def test_report_is_written_before_and_after_each_successful_post(self) -> None:
        client = self.FakeImportClient()
        reports = []

        with patch("import_timesheets._write_report_atomic", side_effect=reports.append):
            result = run_import(
                client,
                [self.row(), self.row("11:00", "12:00")],
                "KPMG-Canada",
                "Target",
                commit=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0]["rows"][0]["status"], "READY")
        self.assertEqual(reports[1]["rows"][0]["status"], "created")
        self.assertEqual(reports[1]["rows"][0]["id"], 123)
        self.assertEqual(reports[2]["rows"][1]["status"], "created")

    def test_write_failure_persists_failure_and_stops_later_rows(self) -> None:
        class FailingClient(self.FakeImportClient):
            def create_timesheet(self, payload):
                self.calls.append(("create_timesheet", payload))
                if len([call for call in self.calls if isinstance(call, tuple) and call[0] == "create_timesheet"]) == 2:
                    raise RuntimeError("simulated POST failure")
                return {"id": 123}

        client = FailingClient()
        reports = []
        with patch("import_timesheets._write_report_atomic", side_effect=reports.append):
            result = run_import(
                client,
                [self.row(), self.row("11:00", "12:00"), self.row("13:00", "14:00")],
                "KPMG-Canada",
                "Target",
                commit=True,
            )

        self.assertEqual(result, 1)
        self.assertEqual(len([call for call in client.calls if isinstance(call, tuple) and call[0] == "create_timesheet"]), 2)
        failure_report = reports[-1]
        self.assertEqual(failure_report["rows"][0]["status"], "created")
        self.assertEqual(failure_report["rows"][0]["id"], 123)
        self.assertEqual(failure_report["rows"][1]["status"], "FAILED")
        self.assertIn("simulated POST failure", failure_report["rows"][1]["error"])
        self.assertEqual(failure_report["rows"][2]["status"], "READY")

    def test_resume_skips_confirmed_created_rows_and_continues_ready_rows(self) -> None:
        class FirstRunClient(self.FakeImportClient):
            def create_timesheet(self, payload):
                self.calls.append(("create_timesheet", payload))
                if len([call for call in self.calls if isinstance(call, tuple) and call[0] == "create_timesheet"]) == 2:
                    raise RuntimeError("temporary failure")
                return {"id": 123}

        rows = [self.row(), self.row("11:00", "12:00")]
        with TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                first_client = FirstRunClient()
                self.assertEqual(
                    run_import(first_client, rows, "KPMG-Canada", "Target", commit=True),
                    1,
                )
                first_report = json.loads(Path("import_report.json").read_text(encoding="utf-8"))
                self.assertEqual(first_report["rows"][0]["status"], "created")

                resume_client = self.FakeImportClient([{
                    "id": 123,
                    "begin": "2026-09-03T09:00:00+0500",
                    "end": "2026-09-03T10:00:00+0500",
                }])
                self.assertEqual(
                    run_import(
                        resume_client,
                        rows,
                        "KPMG-Canada",
                        "Target",
                        commit=True,
                        resume=True,
                    ),
                    0,
                )
                create_calls = [
                    call for call in resume_client.calls
                    if isinstance(call, tuple) and call[0] == "create_timesheet"
                ]
                self.assertEqual(len(create_calls), 1)
                self.assertEqual(create_calls[0][1]["begin"], "2026-09-03T11:00:00")
            finally:
                os.chdir(previous_directory)

    def test_resume_report_mismatch_blocks_without_post(self) -> None:
        with TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                rows = [self.row()]
                self.assertEqual(run_import(self.FakeImportClient(), rows, "KPMG-Canada", "Target"), 0)
                report = json.loads(Path("import_report.json").read_text(encoding="utf-8"))
                report["input_fingerprint"] = "mismatch"
                Path("import_report.json").write_text(json.dumps(report), encoding="utf-8")

                resume_client = self.FakeImportClient()
                with self.assertRaisesRegex(KimaiError, "does not match"):
                    run_import(resume_client, rows, "KPMG-Canada", "Target", commit=True, resume=True)
                self.assertFalse(any(
                    isinstance(call, tuple) and call[0] == "create_timesheet"
                    for call in resume_client.calls
                ))
            finally:
                os.chdir(previous_directory)

    def test_arbitrary_existing_duplicate_is_not_already_created(self) -> None:
        with TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                rows = [self.row()]
                self.assertEqual(run_import(self.FakeImportClient(), rows, "KPMG-Canada", "Target"), 0)
                duplicate_client = self.FakeImportClient([{
                    "id": 456,
                    "begin": "2026-09-03T09:00:00+0500",
                    "end": "2026-09-03T10:00:00+0500",
                }])
                self.assertEqual(
                    run_import(duplicate_client, rows, "KPMG-Canada", "Target", commit=True, resume=True),
                    1,
                )
                self.assertFalse(any(
                    isinstance(call, tuple) and call[0] == "create_timesheet"
                    for call in duplicate_client.calls
                ))
            finally:
                os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
