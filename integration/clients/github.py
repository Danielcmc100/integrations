import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import jwt

JsonDict = dict[str, Any]
SleepFn = Callable[[float], Awaitable[None]]
TimeFn = Callable[[], float]

JWT_TTL_SECONDS = 540
TOKEN_REFRESH_SKEW_SECONDS = 60


class GitHubClient:
    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        base_url: str = "https://api.github.com",
        installation_id: int | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        time_fn: TimeFn = time.time,
        max_retries: int = 3,
    ) -> None:
        self._app_id: str = app_id
        self._private_key: str = private_key_pem
        self._installation_id: int | None = installation_id
        self._sleep: SleepFn = sleep
        self._time: TimeFn = time_fn
        self._max_retries: int = max_retries
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._owns_client: bool = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _build_jwt(self) -> str:
        now = int(self._time())
        payload: dict[str, Any] = {
            "iat": now - 60,
            "exp": now + JWT_TTL_SECONDS,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _resolve_installation_id(self) -> int:
        if self._installation_id is not None:
            return self._installation_id
        app_jwt = self._build_jwt()
        response = await self._client.get(
            "/app/installations", headers={"Authorization": f"Bearer {app_jwt}"}
        )
        response.raise_for_status()
        raw: Any = response.json()
        if not isinstance(raw, list) or not raw:
            raise RuntimeError("no GitHub App installations found")
        raw_list = cast("list[Any]", raw)
        raw_first = raw_list[0]
        if not isinstance(raw_first, dict):
            raise RuntimeError("invalid installation payload")
        first_item = cast("dict[str, Any]", raw_first)
        installation_id_raw = first_item.get("id")
        if not isinstance(installation_id_raw, int):
            raise RuntimeError("installation id missing")
        self._installation_id = installation_id_raw
        return installation_id_raw

    async def _get_installation_token(self) -> str:
        if self._token is not None and self._time() < (
            self._token_expires_at - TOKEN_REFRESH_SKEW_SECONDS
        ):
            return self._token
        installation_id = await self._resolve_installation_id()
        app_jwt = self._build_jwt()
        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        response.raise_for_status()
        raw_body: Any = response.json()
        if not isinstance(raw_body, dict):
            raise RuntimeError("invalid access_token response")
        body = cast("dict[str, Any]", raw_body)
        token_raw = body.get("token")
        expires_at_raw = body.get("expires_at")
        if not isinstance(token_raw, str) or not isinstance(expires_at_raw, str):
            raise RuntimeError("missing token/expires_at")
        self._token = token_raw
        self._token_expires_at = _parse_iso8601_to_epoch(expires_at_raw)
        return token_raw

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            token = await self._get_installation_token()
            headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"token {token}"
            response = await self._client.request(method, path, headers=headers, **kwargs)
            if response.status_code in (429, 403) and _is_rate_limited(response):
                if attempt >= self._max_retries:
                    response.raise_for_status()
                await self._sleep(_rate_limit_delay(response, self._time))
                attempt += 1
                continue
            response.raise_for_status()
            if response.headers.get("X-RateLimit-Remaining") == "0":
                await self._sleep(_rate_limit_delay(response, self._time))
            return response

    async def get_issue(self, owner: str, repo: str, number: int) -> JsonDict:
        response = await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        return _as_json_dict(response)

    async def create_issue(self, owner: str, repo: str, payload: JsonDict) -> JsonDict:
        response = await self._request(
            "POST", f"/repos/{owner}/{repo}/issues", json=payload
        )
        return _as_json_dict(response)

    async def update_issue(
        self, owner: str, repo: str, number: int, payload: JsonDict
    ) -> JsonDict:
        response = await self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=payload
        )
        return _as_json_dict(response)

    async def close_issue(
        self, owner: str, repo: str, number: int, state_reason: str | None = None
    ) -> JsonDict:
        payload: JsonDict = {"state": "closed"}
        if state_reason is not None:
            payload["state_reason"] = state_reason
        return await self.update_issue(owner, repo, number, payload)

    async def create_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> JsonDict:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return _as_json_dict(response)

    async def add_labels(
        self, owner: str, repo: str, number: int, labels: list[str]
    ) -> list[JsonDict]:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": labels},
        )
        raw: Any = response.json()
        if not isinstance(raw, list):
            raise ValueError("expected array from add_labels")
        return [cast(JsonDict, r) for r in cast("list[Any]", raw) if isinstance(r, dict)]

    async def get_pr(self, owner: str, repo: str, number: int) -> JsonDict:
        response = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return _as_json_dict(response)

    async def list_check_runs(
        self, owner: str, repo: str, ref: str
    ) -> list[JsonDict]:
        response = await self._request(
            "GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs"
        )
        raw: Any = response.json()
        if not isinstance(raw, dict):
            raise ValueError("expected object with check_runs")
        data = cast("dict[str, Any]", raw)
        runs = data.get("check_runs", [])
        if not isinstance(runs, list):
            raise ValueError("check_runs not an array")
        return [cast(JsonDict, r) for r in cast("list[Any]", runs) if isinstance(r, dict)]

    async def list_reviews(
        self, owner: str, repo: str, number: int
    ) -> list[JsonDict]:
        response = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/reviews"
        )
        raw: Any = response.json()
        if not isinstance(raw, list):
            raise ValueError("expected array from list_reviews")
        return [cast(JsonDict, r) for r in cast("list[Any]", raw) if isinstance(r, dict)]

    async def get_branch_protection(
        self, owner: str, repo: str, branch: str
    ) -> JsonDict:
        response = await self._request(
            "GET", f"/repos/{owner}/{repo}/branches/{branch}/protection"
        )
        return _as_json_dict(response)


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return (
        response.status_code == 403
        and response.headers.get("X-RateLimit-Remaining") == "0"
    )


def _rate_limit_delay(response: httpx.Response, time_fn: TimeFn) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    reset = response.headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            return max(0.0, float(reset) - time_fn())
        except ValueError:
            pass
    return 1.0


def _parse_iso8601_to_epoch(value: str) -> float:
    from datetime import datetime

    cleaned = value.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).timestamp()


def _as_json_dict(response: httpx.Response) -> JsonDict:
    data: Any = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"expected json object, got {type(data).__name__}")
    return cast(JsonDict, data)
