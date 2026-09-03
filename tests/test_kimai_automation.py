from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

import httpx

from import_timesheets import InputRow, build_timesheet_payload
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

        self.assertNotIn("tags", payload)
        self.assertNotIn("customer", payload)

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


if __name__ == "__main__":
    unittest.main()
