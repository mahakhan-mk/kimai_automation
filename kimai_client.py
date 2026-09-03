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
        response = self.client.request(method, path, **kwargs)
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

    def activities(self, project_id: int | None = None) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = []
        if project_id is not None:
            # Kimai v2 collection filters accept arrays. Repeated query keys are portable.
            params.append(("projects[]", str(project_id)))
        data = self._request("GET", "/activities", params=params or None)
        return list(data or [])

    def timesheets(self, begin: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        data = self._request("GET", "/timesheets", params=params or None)
        return list(data or [])

    def create_timesheet(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/timesheets", json=payload)
        if not isinstance(data, dict):
            raise KimaiError("POST /timesheets did not return a JSON object")
        return data
