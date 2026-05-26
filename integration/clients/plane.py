import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx

JsonDict = dict[str, Any]
SleepFn = Callable[[float], Awaitable[None]]


class PlaneClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        workspace: str,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        max_retries: int = 3,
    ) -> None:
        self._workspace: str = workspace
        self._sleep: SleepFn = sleep
        self._max_retries: int = max_retries
        self._owns_client: bool = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_token, "Accept": "application/json"},
            timeout=30.0,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _ws_prefix(self, project_id: str) -> str:
        return f"/workspaces/{self._workspace}/projects/{project_id}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            response = await self._client.request(method, path, **kwargs)
            if response.status_code == 429 and attempt < self._max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                await self._sleep(retry_after)
                attempt += 1
                continue
            response.raise_for_status()
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset_in = response.headers.get("X-RateLimit-Reset-In")
            if remaining == "0" and reset_in is not None:
                await self._sleep(_safe_float(reset_in, 1.0))
            return response

    async def get_card(self, project_id: str, card_id: str) -> JsonDict:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/issues/{card_id}/")
        return _as_json_dict(response)

    async def create_card(self, project_id: str, payload: JsonDict) -> JsonDict:
        response = await self._request(
            "POST", f"{self._ws_prefix(project_id)}/issues/", json=payload
        )
        return _as_json_dict(response)

    async def update_card(
        self, project_id: str, card_id: str, payload: JsonDict
    ) -> JsonDict:
        response = await self._request(
            "PATCH", f"{self._ws_prefix(project_id)}/issues/{card_id}/", json=payload
        )
        return _as_json_dict(response)

    async def add_comment(
        self, project_id: str, card_id: str, comment_html: str
    ) -> JsonDict:
        response = await self._request(
            "POST",
            f"{self._ws_prefix(project_id)}/issues/{card_id}/comments/",
            json={"comment_html": comment_html},
        )
        return _as_json_dict(response)

    async def list_states(self, project_id: str) -> list[JsonDict]:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/states/")
        return _as_json_list(response)

    async def list_modules(self, project_id: str) -> list[JsonDict]:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/modules/")
        return _as_json_list(response)

    async def list_labels(self, project_id: str) -> list[JsonDict]:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/labels/")
        return _as_json_list(response)

    async def list_cycles(self, project_id: str) -> list[JsonDict]:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/cycles/")
        return _as_json_list(response)

    async def add_issue_to_cycle(
        self, project_id: str, cycle_id: str, issue_id: str
    ) -> JsonDict:
        response = await self._request(
            "POST",
            f"{self._ws_prefix(project_id)}/cycles/{cycle_id}/cycle-issues/",
            json={"issues": [issue_id]},
        )
        return _as_json_dict(response)

    async def get_card_by_sequence(
        self, project_id: str, sequence_id: int
    ) -> JsonDict | None:
        response = await self._request(
            "GET",
            f"{self._ws_prefix(project_id)}/issues/",
            params={"sequence_id": sequence_id},
        )
        items = _as_json_list(response)
        return items[0] if items else None

    async def list_cards(self, project_id: str) -> list[JsonDict]:
        response = await self._request("GET", f"{self._ws_prefix(project_id)}/issues/")
        return _as_json_list(response)


def _parse_retry_after(value: str | None) -> float:
    return _safe_float(value, 1.0)


def _safe_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _as_json_dict(response: httpx.Response) -> JsonDict:
    data: Any = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"expected json object, got {type(data).__name__}")
    return cast(JsonDict, data)


def _as_json_list(response: httpx.Response) -> list[JsonDict]:
    data: Any = response.json()
    if isinstance(data, dict):
        data_dict = cast("dict[str, Any]", data)
        raw_results = data_dict.get("results")
        if isinstance(raw_results, list):
            return [
                cast(JsonDict, r)
                for r in cast("list[Any]", raw_results)
                if isinstance(r, dict)
            ]
    if isinstance(data, list):
        return [cast(JsonDict, r) for r in cast("list[Any]", data) if isinstance(r, dict)]
    raise ValueError("expected json array or paginated object with 'results' key")
