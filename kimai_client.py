from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class KimaiError(RuntimeError):
    pass


@dataclass(frozen=True)
class KimaiConfig:
    base_url: str
    token: str
    timeout_seconds: float = 30.0
    verify_ssl: bool = True


class KimaiClient:
    def __init__(self, config: KimaiConfig) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "KimaiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KimaiError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            body = response.text[:1000]
            raise KimaiError(f"{method} {path} failed: HTTP {response.status_code}: {body}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KimaiError(f"{method} {path} returned non-JSON content") from exc

    def ping(self) -> Any:
        return self._request("GET", "/ping")

    def version(self) -> Any:
        return self._request("GET", "/version")

    def customers(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/customers")
        return list(data or [])

    def projects(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/projects")
        return list(data or [])

    def user_me(self) -> dict[str, Any]:
        data = self._request("GET", "/users/me")
        if not isinstance(data, dict):
            raise KimaiError("GET /users/me did not return a JSON object")
        return data

    def activities(self, project_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if project_id is not None:
            params["project"] = str(project_id)
        data = self._request("GET", "/activities", params=params or None)
        return list(data or [])

    def tags_find(self, name: str | None = "") -> list[dict[str, Any]]:
        """Return full tag entities from GET /tags/find.

        Kimai returns no results when the optional search term is omitted on
        some 2.x versions, so the default empty search term requests all tags.
        """
        params: dict[str, str] = {}
        if name is not None:
            params["name"] = name
        data = self._request("GET", "/tags/find", params=params or None)
        return list(data or [])

    def tags(self, name: str | None = "") -> list[dict[str, Any]]:
        """Compatibility alias for the full tag lookup endpoint."""
        return self.tags_find(name)

    def timesheets(
        self,
        begin: str | None = None,
        end: str | None = None,
        project_id: int | None = None,
        page: int | None = None,
        size: int | None = None,
        order_by: str | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if project_id is not None:
            params["project"] = str(project_id)
        if page is not None:
            params["page"] = str(page)
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        if size is not None:
            params["size"] = str(size)
        if order_by:
            params["orderBy"] = order_by
        if order:
            params["order"] = order
        data = self._request("GET", "/timesheets", params=params or None)
        return list(data or [])

    def all_timesheets(
        self,
        begin: str | None = None,
        end: str | None = None,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch every current-user timesheet in a date range.

        The endpoint defaults to 50 records, so preflight uses its maximum
        page size and walks pages without a project filter. The page guard
        prevents an unexpectedly repeating API response from looping forever.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")

        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            current_page = self.timesheets(
                begin=begin,
                end=end,
                page=page,
                size=500,
            )
            records.extend(current_page)
            if len(current_page) < 500:
                return records
        raise KimaiError(
            f"GET /timesheets pagination exceeded the maximum of {max_pages} pages"
        )

    def create_timesheet(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/timesheets", json=payload)
        if not isinstance(data, dict):
            raise KimaiError("POST /timesheets did not return a JSON object")
        return data
