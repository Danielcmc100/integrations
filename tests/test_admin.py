from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from integration.config import settings
from integration.deps import get_config_service, get_github_client, get_plane_client, get_session
from integration.models import LabelMap, RepoModuleMap, UserMap
from main import app

TOKEN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSession:
    def __init__(
        self,
        rows: list[Any] | None = None,
        get_result: Any = None,
    ) -> None:
        self.rows = rows or []
        self.get_result = get_result
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0

    async def execute(self, _stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value = self.rows
        return result

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self.get_result

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        for obj in self.added:
            if hasattr(type(obj), "id") and getattr(obj, "id", None) is None:  # pyright: ignore[reportUnknownArgumentType]
                obj.id = 1


class FakeConfigService:
    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


def _make_client(
    session: FakeSession, config_svc: FakeConfigService
) -> Iterator[TestClient]:
    async def _session_override() -> AsyncIterator[FakeSession]:
        yield session

    def _config_svc_override() -> FakeConfigService:
        return config_svc

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_config_service] = _config_svc_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _set_token() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    prev = settings.admin_token
    settings.admin_token = TOKEN
    yield
    settings.admin_token = prev


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def fake_config_svc() -> FakeConfigService:
    return FakeConfigService()


@pytest.fixture
def client(fake_session: FakeSession, fake_config_svc: FakeConfigService) -> Iterator[TestClient]:
    yield from _make_client(fake_session, fake_config_svc)


# --- Auth ---


def test_missing_token_returns_401(client: TestClient) -> None:
    resp = client.get("/admin/repo-modules")
    assert resp.status_code == 401


def test_wrong_token_returns_401(client: TestClient) -> None:
    resp = client.get("/admin/repo-modules", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_no_bearer_prefix_returns_401(client: TestClient) -> None:
    resp = client.get("/admin/repo-modules", headers={"Authorization": TOKEN})
    assert resp.status_code == 401


def test_valid_token_allows_access(client: TestClient) -> None:
    resp = client.get("/admin/repo-modules", headers=AUTH)
    assert resp.status_code == 200


def test_empty_admin_token_always_rejects(
    fake_session: FakeSession, fake_config_svc: FakeConfigService
) -> None:
    prev = settings.admin_token
    settings.admin_token = ""
    try:
        for c in _make_client(fake_session, fake_config_svc):
            resp = c.get("/admin/repo-modules", headers=AUTH)
            assert resp.status_code == 401
    finally:
        settings.admin_token = prev


# --- Repo Modules ---


def test_list_repo_modules_empty(client: TestClient) -> None:
    resp = client.get("/admin/repo-modules", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_repo_modules_returns_rows(
    fake_config_svc: FakeConfigService,
) -> None:
    rm = RepoModuleMap()
    rm.plane_module_id = "mod-1"
    rm.plane_project_id = "proj-1"
    rm.gh_repo = "owner/repo"
    session = FakeSession(rows=[rm])
    for client in _make_client(session, fake_config_svc):
        resp = client.get("/admin/repo-modules", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["plane_module_id"] == "mod-1"
        assert data[0]["gh_repo"] == "owner/repo"


def test_create_repo_module(
    fake_session: FakeSession, fake_config_svc: FakeConfigService, client: TestClient
) -> None:
    payload = {
        "plane_module_id": "mod-new",
        "plane_project_id": "proj-1",
        "gh_repo": "owner/repo",
    }
    resp = client.post("/admin/repo-modules", json=payload, headers=AUTH)
    assert resp.status_code == 201
    assert len(fake_session.added) == 1
    row = fake_session.added[0]
    assert isinstance(row, RepoModuleMap)
    assert row.plane_module_id == "mod-new"
    assert fake_session.commits == 1
    assert fake_config_svc.invalidated == 1


def test_create_repo_module_bad_payload_422(client: TestClient) -> None:
    resp = client.post("/admin/repo-modules", json={"plane_module_id": "x"}, headers=AUTH)
    assert resp.status_code == 422


def test_update_repo_module(fake_config_svc: FakeConfigService) -> None:
    existing = RepoModuleMap()
    existing.plane_module_id = "mod-1"
    existing.plane_project_id = "proj-old"
    existing.gh_repo = "owner/old"
    session = FakeSession(get_result=existing)
    for client in _make_client(session, fake_config_svc):
        payload = {"plane_project_id": "proj-new", "gh_repo": "owner/new"}
        resp = client.put("/admin/repo-modules/mod-1", json=payload, headers=AUTH)
        assert resp.status_code == 200
        assert existing.plane_project_id == "proj-new"
        assert existing.gh_repo == "owner/new"
        assert session.commits == 1
        assert fake_config_svc.invalidated == 1


def test_update_repo_module_not_found(client: TestClient) -> None:
    resp = client.put(
        "/admin/repo-modules/missing",
        json={"plane_project_id": "p", "gh_repo": "o/r"},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_delete_repo_module(fake_config_svc: FakeConfigService) -> None:
    existing = RepoModuleMap()
    existing.plane_module_id = "mod-1"
    existing.plane_project_id = "proj-1"
    existing.gh_repo = "owner/repo"
    session = FakeSession(get_result=existing)
    for client in _make_client(session, fake_config_svc):
        resp = client.delete("/admin/repo-modules/mod-1", headers=AUTH)
        assert resp.status_code == 204
        assert existing in session.deleted
        assert session.commits == 1
        assert fake_config_svc.invalidated == 1


def test_delete_repo_module_not_found(client: TestClient) -> None:
    resp = client.delete("/admin/repo-modules/missing", headers=AUTH)
    assert resp.status_code == 404


# --- Labels ---


def test_list_labels(fake_config_svc: FakeConfigService) -> None:
    lm = LabelMap()
    lm.id = 1
    lm.plane_project_id = "proj-1"
    lm.plane_label_id = "lbl-1"
    lm.gh_repo = "owner/repo"
    lm.gh_label = "bug"
    session = FakeSession(rows=[lm])
    for client in _make_client(session, fake_config_svc):
        resp = client.get("/admin/labels", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["gh_label"] == "bug"


def test_create_label_invalidates_cache(
    fake_session: FakeSession, fake_config_svc: FakeConfigService, client: TestClient
) -> None:
    payload = {
        "plane_project_id": "proj-1",
        "plane_label_id": "lbl-1",
        "gh_repo": "owner/repo",
        "gh_label": "bug",
    }
    resp = client.post("/admin/labels", json=payload, headers=AUTH)
    assert resp.status_code == 201
    assert fake_config_svc.invalidated == 1
    assert isinstance(fake_session.added[0], LabelMap)


def test_update_label_not_found(client: TestClient) -> None:
    payload = {
        "plane_project_id": "p",
        "plane_label_id": "l",
        "gh_repo": "o/r",
        "gh_label": "x",
    }
    resp = client.put("/admin/labels/999", json=payload, headers=AUTH)
    assert resp.status_code == 404


def test_delete_label(fake_config_svc: FakeConfigService) -> None:
    lm = LabelMap()
    lm.id = 5
    lm.plane_project_id = "proj-1"
    lm.plane_label_id = "lbl-1"
    lm.gh_repo = "owner/repo"
    lm.gh_label = "bug"
    session = FakeSession(get_result=lm)
    for client in _make_client(session, fake_config_svc):
        resp = client.delete("/admin/labels/5", headers=AUTH)
        assert resp.status_code == 204
        assert lm in session.deleted
        assert fake_config_svc.invalidated == 1


# --- Users ---


def test_list_users(fake_config_svc: FakeConfigService) -> None:
    um = UserMap()
    um.id = 1
    um.plane_user_id = "user-1"
    um.gh_login = "gh-user"
    um.discord_user_id = None
    session = FakeSession(rows=[um])
    for client in _make_client(session, fake_config_svc):
        resp = client.get("/admin/users", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["gh_login"] == "gh-user"
        assert data[0]["discord_user_id"] is None


def test_create_user_with_discord(
    fake_session: FakeSession, fake_config_svc: FakeConfigService, client: TestClient
) -> None:
    payload = {
        "plane_user_id": "user-1",
        "gh_login": "gh-user",
        "discord_user_id": "discord-123",
    }
    resp = client.post("/admin/users", json=payload, headers=AUTH)
    assert resp.status_code == 201
    row = fake_session.added[0]
    assert isinstance(row, UserMap)
    assert row.discord_user_id == "discord-123"
    assert fake_config_svc.invalidated == 1


def test_create_user_without_discord(
    fake_session: FakeSession, fake_config_svc: FakeConfigService, client: TestClient
) -> None:
    payload = {"plane_user_id": "user-2", "gh_login": "gh-user2"}
    resp = client.post("/admin/users", json=payload, headers=AUTH)
    assert resp.status_code == 201
    row = fake_session.added[0]
    assert row.discord_user_id is None


def test_update_user(fake_config_svc: FakeConfigService) -> None:
    um = UserMap()
    um.id = 3
    um.plane_user_id = "user-1"
    um.gh_login = "old-gh"
    um.discord_user_id = None
    session = FakeSession(get_result=um)
    for client in _make_client(session, fake_config_svc):
        payload = {"plane_user_id": "user-1", "gh_login": "new-gh", "discord_user_id": "d-999"}
        resp = client.put("/admin/users/3", json=payload, headers=AUTH)
        assert resp.status_code == 200
        assert um.gh_login == "new-gh"
        assert um.discord_user_id == "d-999"
        assert fake_config_svc.invalidated == 1


def test_delete_user_not_found(client: TestClient) -> None:
    resp = client.delete("/admin/users/999", headers=AUTH)
    assert resp.status_code == 404


# --- Proxy endpoints ---


class FakePlaneClient:
    async def list_projects(self) -> list[dict[str, Any]]:
        return [{"id": "proj-1", "name": "Test Project"}]

    async def list_labels(self, project_id: str) -> list[dict[str, Any]]:
        return [{"id": "lbl-1", "name": "bug"}]

    async def list_modules(self, project_id: str) -> list[dict[str, Any]]:
        return [{"id": "mod-1", "name": "Sprint 1"}]

    async def list_project_members(self, project_id: str) -> list[dict[str, Any]]:
        return [{"member": {"id": "usr-1", "display_name": "Alice"}}]


class FakeGitHubClient:
    async def list_repos(self) -> list[dict[str, Any]]:
        return [{"full_name": "owner/repo"}]

    async def list_repo_labels(self, owner: str, repo: str) -> list[dict[str, Any]]:
        return [{"name": "bug", "color": "d73a4a"}]

    async def list_collaborators(self, owner: str, repo: str) -> list[dict[str, Any]]:
        return [{"login": "alice"}]


@pytest.fixture
def proxy_client(
    fake_session: FakeSession, fake_config_svc: FakeConfigService
) -> Iterator[TestClient]:
    fake_plane = FakePlaneClient()
    fake_github = FakeGitHubClient()

    async def _session_override() -> AsyncIterator[FakeSession]:
        yield fake_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_config_service] = lambda: fake_config_svc
    app.dependency_overrides[get_plane_client] = lambda: fake_plane
    app.dependency_overrides[get_github_client] = lambda: fake_github
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_proxy_plane_projects(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/plane/projects", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["id"] == "proj-1"


def test_proxy_plane_projects_requires_auth(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/plane/projects")
    assert resp.status_code == 401


def test_proxy_plane_labels(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/plane/projects/proj-1/labels", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "bug"


def test_proxy_plane_modules(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/plane/projects/proj-1/modules", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "mod-1"


def test_proxy_plane_members(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/plane/projects/proj-1/members", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["member"]["display_name"] == "Alice"


def test_proxy_github_repos(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/github/repos", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["full_name"] == "owner/repo"


def test_proxy_github_labels(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/github/repos/owner/repo/labels", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "bug"


def test_proxy_github_collaborators(proxy_client: TestClient) -> None:
    resp = proxy_client.get("/admin/github/repos/owner/repo/collaborators", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["login"] == "alice"
