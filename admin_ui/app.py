import os
from typing import Any, cast

import requests
import streamlit as st

API_BASE_DEFAULT = os.environ.get("API_BASE", "http://localhost:8000")

st.set_page_config(page_title="PSTG Integrations Admin", layout="wide")
st.title("PSTG Integrations — Admin")

# ── Sidebar: connection ───────────────────────────────────────────────────────

with st.sidebar:
    st.header("Connection")
    api_base_input = st.text_input(
        "API URL",
        value=st.session_state.get("api_base", API_BASE_DEFAULT),
    )
    token_input = st.text_input(
        "Admin token",
        type="password",
        value=st.session_state.get("api_token", ""),
    )
    if st.button("Connect"):
        st.session_state["api_base"] = api_base_input.rstrip("/")
        st.session_state["api_token"] = token_input
        st.cache_data.clear()
        st.rerun()

TOKEN: str = st.session_state.get("api_token", "")
BASE: str = st.session_state.get("api_base", API_BASE_DEFAULT).rstrip("/")

if not TOKEN:
    st.info("Enter your admin token in the sidebar to continue.")
    st.stop()

# ── Cached fetchers (keyed by token + base so cache invalidates on reconnect) ─


@st.cache_data(ttl=60)
def _fetch(token: str, base: str, path: str) -> list[dict[str, Any]]:
    r = requests.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    r.raise_for_status()
    return cast(list[dict[str, Any]], r.json())


def plane_projects() -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, "/admin/plane/projects")


def plane_labels(project_id: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/labels")


def plane_states(project_id: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/states")


def plane_modules(project_id: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/modules")


def plane_members(project_id: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/members")


def github_repos() -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, "/admin/github/repos")


def github_labels(owner: str, repo: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/github/repos/{owner}/{repo}/labels")


def github_collaborators(owner: str, repo: str) -> list[dict[str, Any]]:
    return _fetch(TOKEN, BASE, f"/admin/github/repos/{owner}/{repo}/collaborators")


# ── Non-cached admin CRUD ─────────────────────────────────────────────────────


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def admin_get(path: str) -> list[dict[str, Any]]:
    r = requests.get(f"{BASE}{path}", headers=_headers(), timeout=15)
    if r.status_code == 401:
        st.error("Invalid token.")
        st.stop()
    r.raise_for_status()
    return cast(list[dict[str, Any]], r.json())


def admin_post(path: str, body: dict[str, Any]) -> None:
    r = requests.post(f"{BASE}{path}", json=body, headers=_headers(), timeout=15)
    if r.status_code == 401:
        st.error("Invalid token.")
        st.stop()
    r.raise_for_status()


def admin_delete(path: str) -> None:
    r = requests.delete(f"{BASE}{path}", headers=_headers(), timeout=15)
    if r.status_code == 401:
        st.error("Invalid token.")
        st.stop()
    r.raise_for_status()


# ── Option-list helpers ───────────────────────────────────────────────────────


def _project_opts(projects: list[dict[str, Any]]) -> dict[str, str]:
    """name → id"""
    return {p.get("name", p.get("id", "?")): p["id"] for p in projects if "id" in p}


def _label_opts(labels: list[dict[str, Any]]) -> dict[str, str]:
    """name → id"""
    return {lb.get("name", lb.get("id", "?")): lb["id"] for lb in labels if "id" in lb}


def _module_opts(modules: list[dict[str, Any]]) -> dict[str, str]:
    """name → id"""
    return {m.get("name", m.get("id", "?")): m["id"] for m in modules if "id" in m}


def _member_opts(members: list[dict[str, Any]]) -> dict[str, str]:
    """display → user_id

    Plane /members/ may return [{member: {id, display_name}}, ...]
    or [{member__id, member__display_name}, ...]
    """
    result: dict[str, str] = {}
    for m in members:
        nested = m.get("member")
        if isinstance(nested, dict):
            uid = str(nested.get("id", ""))
            name = str(nested.get("display_name") or nested.get("email") or uid)
        else:
            uid = str(m.get("member__id") or m.get("id", ""))
            name = str(
                m.get("member__display_name")
                or m.get("display_name")
                or m.get("member__email")
                or uid
            )
        if uid:
            result[f"{name} ({uid[:8]})"] = uid
    return result


def _gh_label_names(labels: list[dict[str, Any]]) -> list[str]:
    return [lb["name"] for lb in labels if "name" in lb]


def _state_name_opts(states: list[dict[str, Any]]) -> list[str]:
    return [str(s["name"]) for s in states if "name" in s]


def _collab_logins(collabs: list[dict[str, Any]]) -> list[str]:
    return [c["login"] for c in collabs if "login" in c]


def _repo_full_names(repos: list[dict[str, Any]]) -> list[str]:
    return [r["full_name"] for r in repos if "full_name" in r]


# ── Name-lookup builders (use cached fetchers, so no extra API calls) ─────────


def _label_name_map(existing: list[dict[str, Any]]) -> dict[str, str]:
    """plane_label_id → label name, fetching per unique project in existing rows."""
    result: dict[str, str] = {}
    seen: set[str] = set()
    for row in existing:
        proj_id = row.get("plane_project_id", "")
        if not proj_id or proj_id in seen:
            continue
        seen.add(proj_id)
        try:
            for lb in plane_labels(proj_id):
                if "id" in lb and "name" in lb:
                    result[str(lb["id"])] = str(lb["name"])
        except Exception:
            pass
    return result


def _user_name_map() -> dict[str, str]:
    """plane_user_id → display name, searching across all projects."""
    result: dict[str, str] = {}
    try:
        for proj in plane_projects():
            proj_id = proj.get("id", "")
            if not proj_id:
                continue
            try:
                for m in plane_members(str(proj_id)):
                    nested = m.get("member")
                    if isinstance(nested, dict):
                        uid = str(nested.get("id", ""))
                        name = str(nested.get("display_name") or nested.get("email") or uid)
                    else:
                        uid = str(m.get("member__id") or m.get("id", ""))
                        name = str(
                            m.get("member__display_name")
                            or m.get("display_name")
                            or m.get("member__email")
                            or uid
                        )
                    if uid and uid not in result:
                        result[uid] = name
            except Exception:
                pass
    except Exception:
        pass
    return result


def _module_name_map(existing: list[dict[str, Any]]) -> dict[str, str]:
    """plane_module_id → module name, fetching per unique project in existing rows."""
    result: dict[str, str] = {}
    seen: set[str] = set()
    for row in existing:
        proj_id = row.get("plane_project_id", "")
        if not proj_id or proj_id in seen:
            continue
        seen.add(proj_id)
        try:
            for m in plane_modules(str(proj_id)):
                if "id" in m and "name" in m:
                    result[str(m["id"])] = str(m["name"])
        except Exception:
            pass
    return result


def _project_name_map() -> dict[str, str]:
    """plane_project_id → project name."""
    try:
        return {str(p["id"]): str(p.get("name", p["id"])) for p in plane_projects() if "id" in p}
    except Exception:
        return {}


# ── Shared loaders ────────────────────────────────────────────────────────────


def _load_projects() -> list[dict[str, Any]]:
    try:
        return plane_projects()
    except Exception as exc:
        st.error(f"Failed to load Plane projects: {exc}")
        return []


def _load_gh_repos() -> list[dict[str, Any]]:
    try:
        return github_repos()
    except Exception as exc:
        st.error(f"Failed to load GitHub repos: {exc}")
        return []


# ── Existing-mappings table helpers ──────────────────────────────────────────


def _table_header(*labels: str) -> None:
    cols = st.columns([*[3] * len(labels), 1])
    for col, lbl in zip(cols, labels, strict=False):
        col.markdown(f"**{lbl}**")
    cols[-1].markdown("**Action**")


def _delete_row(
    cols: list[Any],
    delete_path: str,
    row_key: str,
) -> None:
    if cols[-1].button("Delete", key=f"del_{row_key}"):
        try:
            admin_delete(delete_path)
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            st.error(f"Failed: {exc}")


# ── LABELS TAB ────────────────────────────────────────────────────────────────


def render_labels_tab() -> None:
    st.subheader("Map Plane label → GitHub label")

    projects = _load_projects()
    repos = _load_gh_repos()

    col1, col2 = st.columns(2)
    plane_proj_id = ""
    plane_label_id = ""
    gh_repo = ""
    gh_label_name = ""

    with col1:
        st.markdown("**Plane**")
        proj_opts = _project_opts(projects)
        plane_proj_name = st.selectbox(
            "Project", options=["", *proj_opts.keys()], key="lbl_plane_proj"
        )
        plane_proj_id = proj_opts.get(plane_proj_name, "") if plane_proj_name else ""

        if plane_proj_id:
            try:
                pl_labels = plane_labels(plane_proj_id)
            except Exception as exc:
                st.error(f"Failed to load Plane labels: {exc}")
                pl_labels = []
            lbl_opts = _label_opts(pl_labels)
            plane_lbl_name = st.selectbox(
                "Label", options=["", *lbl_opts.keys()], key="lbl_plane_lbl"
            )
            plane_label_id = lbl_opts.get(plane_lbl_name, "") if plane_lbl_name else ""
        else:
            st.selectbox("Label", options=[""], key="lbl_plane_lbl_off", disabled=True)

    with col2:
        st.markdown("**GitHub**")
        repo_names = _repo_full_names(repos)
        gh_repo = st.selectbox("Repository", options=["", *repo_names], key="lbl_gh_repo") or ""

        if gh_repo:
            owner, repo_name = gh_repo.split("/", 1)
            try:
                gh_lbls = github_labels(owner, repo_name)
            except Exception as exc:
                st.error(f"Failed to load GitHub labels: {exc}")
                gh_lbls = []
            gh_label_name = (
                st.selectbox(
                    "Label",
                    options=["", *_gh_label_names(gh_lbls)],
                    key="lbl_gh_lbl",
                )
                or ""
            )
        else:
            st.selectbox("Label", options=[""], key="lbl_gh_lbl_off", disabled=True)

    if st.button("Map labels", type="primary", key="lbl_map_btn"):
        if not all([plane_proj_id, plane_label_id, gh_repo, gh_label_name]):
            st.warning("Select all four fields before mapping.")
        else:
            try:
                admin_post(
                    "/admin/labels",
                    {
                        "plane_project_id": plane_proj_id,
                        "plane_label_id": plane_label_id,
                        "gh_repo": gh_repo,
                        "gh_label": gh_label_name,
                    },
                )
                st.success("Mapping created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    st.divider()
    st.subheader("Existing label mappings")
    try:
        existing = admin_get("/admin/labels")
        if existing:
            names = _label_name_map(existing)
            _table_header("Plane project", "Plane label ID", "Plane label", "GH repo", "GH label")
            for row in existing:
                cols = st.columns([3, 3, 3, 3, 3, 1])
                cols[0].text(row.get("plane_project_id", "")[:16])
                cols[1].text(row.get("plane_label_id", "")[:16])
                cols[2].text(names.get(row.get("plane_label_id", ""), "—"))
                cols[3].text(row.get("gh_repo", ""))
                cols[4].text(row.get("gh_label", ""))
                _delete_row(cols, f"/admin/labels/{row['id']}", f"lbl_{row['id']}")
        else:
            st.info("No label mappings yet.")
    except Exception as exc:
        st.error(f"Failed to load mappings: {exc}")


# ── USERS TAB ─────────────────────────────────────────────────────────────────


def render_users_tab() -> None:
    st.subheader("Map Plane user → GitHub collaborator → Discord")

    projects = _load_projects()
    repos = _load_gh_repos()

    col1, col2 = st.columns(2)
    plane_user_id = ""
    gh_login = ""

    with col1:
        st.markdown("**Plane**")
        proj_opts = _project_opts(projects)
        plane_proj_name = st.selectbox(
            "Project", options=["", *proj_opts.keys()], key="usr_plane_proj"
        )
        plane_proj_id = proj_opts.get(plane_proj_name, "") if plane_proj_name else ""

        if plane_proj_id:
            try:
                members = plane_members(plane_proj_id)
            except Exception as exc:
                st.error(f"Failed to load Plane members: {exc}")
                members = []
            member_opts = _member_opts(members)
            member_name = st.selectbox(
                "Member", options=["", *member_opts.keys()], key="usr_plane_member"
            )
            plane_user_id = member_opts.get(member_name, "") if member_name else ""
        else:
            st.selectbox("Member", options=[""], key="usr_plane_member_off", disabled=True)

    with col2:
        st.markdown("**GitHub**")
        repo_names = _repo_full_names(repos)
        gh_repo = st.selectbox("Repository", options=["", *repo_names], key="usr_gh_repo") or ""

        if gh_repo:
            owner, repo_name = gh_repo.split("/", 1)
            try:
                collabs = github_collaborators(owner, repo_name)
            except Exception as exc:
                st.error(f"Failed to load collaborators: {exc}")
                collabs = []
            gh_login = (
                st.selectbox(
                    "Collaborator",
                    options=["", *_collab_logins(collabs)],
                    key="usr_gh_collab",
                )
                or ""
            )
        else:
            st.selectbox("Collaborator", options=[""], key="usr_gh_collab_off", disabled=True)

    discord_id = st.text_input("Discord user ID (optional)", key="usr_discord_id")

    if st.button("Map user", type="primary", key="usr_map_btn"):
        if not all([plane_user_id, gh_login]):
            st.warning("Select Plane member and GitHub collaborator before mapping.")
        else:
            try:
                admin_post(
                    "/admin/users",
                    {
                        "plane_user_id": plane_user_id,
                        "gh_login": gh_login.lower(),
                        "discord_user_id": discord_id or None,
                    },
                )
                st.success("Mapping created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    st.divider()
    st.subheader("Existing user mappings")
    try:
        existing = admin_get("/admin/users")
        if existing:
            names = _user_name_map()
            _table_header("Plane user ID", "Plane user", "GitHub login", "Discord user ID")
            for row in existing:
                cols = st.columns([3, 3, 3, 3, 1])
                cols[0].text(row.get("plane_user_id", "")[:16])
                cols[1].text(names.get(row.get("plane_user_id", ""), "—"))
                cols[2].text(row.get("gh_login", ""))
                cols[3].text(row.get("discord_user_id") or "—")
                _delete_row(cols, f"/admin/users/{row['id']}", f"usr_{row['id']}")
        else:
            st.info("No user mappings yet.")
    except Exception as exc:
        st.error(f"Failed to load mappings: {exc}")


# ── MODULES TAB ───────────────────────────────────────────────────────────────


def render_modules_tab() -> None:
    st.subheader("Map Plane module → GitHub repository")

    projects = _load_projects()
    repos = _load_gh_repos()

    col1, col2 = st.columns(2)
    plane_module_id = ""
    plane_proj_id_m = ""
    gh_repo_m = ""

    with col1:
        st.markdown("**Plane**")
        proj_opts = _project_opts(projects)
        plane_proj_name = st.selectbox(
            "Project", options=["", *proj_opts.keys()], key="mod_plane_proj"
        )
        plane_proj_id_m = proj_opts.get(plane_proj_name, "") if plane_proj_name else ""

        if plane_proj_id_m:
            try:
                mods = plane_modules(plane_proj_id_m)
            except Exception as exc:
                st.error(f"Failed to load Plane modules: {exc}")
                mods = []
            mod_opts = _module_opts(mods)
            mod_name = st.selectbox("Module", options=["", *mod_opts.keys()], key="mod_plane_mod")
            plane_module_id = mod_opts.get(mod_name, "") if mod_name else ""
        else:
            st.selectbox("Module", options=[""], key="mod_plane_mod_off", disabled=True)

    with col2:
        st.markdown("**GitHub**")
        repo_names = _repo_full_names(repos)
        gh_repo_m = st.selectbox("Repository", options=["", *repo_names], key="mod_gh_repo") or ""

    if st.button("Map module", type="primary", key="mod_map_btn"):
        if not all([plane_module_id, plane_proj_id_m, gh_repo_m]):
            st.warning("Select Plane module and GitHub repository before mapping.")
        else:
            try:
                admin_post(
                    "/admin/repo-modules",
                    {
                        "plane_module_id": plane_module_id,
                        "plane_project_id": plane_proj_id_m,
                        "gh_repo": gh_repo_m,
                    },
                )
                st.success("Mapping created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    st.divider()
    st.subheader("Existing module mappings")
    try:
        existing = admin_get("/admin/repo-modules")
        if existing:
            mod_names = _module_name_map(existing)
            proj_names = _project_name_map()
            _table_header("Plane module ID", "Plane module", "Plane project", "GitHub repo")
            for row in existing:
                cols = st.columns([3, 3, 3, 3, 1])
                cols[0].text(row.get("plane_module_id", "")[:16])
                cols[1].text(mod_names.get(row.get("plane_module_id", ""), "—"))
                cols[2].text(proj_names.get(row.get("plane_project_id", ""), "—"))
                cols[3].text(row.get("gh_repo", ""))
                _delete_row(
                    cols,
                    f"/admin/repo-modules/{row['plane_module_id']}",
                    f"mod_{row['plane_module_id']}",
                )
        else:
            st.info("No module mappings yet.")
    except Exception as exc:
        st.error(f"Failed to load mappings: {exc}")


# ── STAGE MAPS TAB ───────────────────────────────────────────────────────────

_TRIGGER_LABELS: dict[str, str] = {
    "branch_created": "Branch created",
    "pr_opened": "PR opened / ready / reopened",
    "ci_passed": "CI passed (PR ready)",
    "changes_requested": "Changes requested (review)",
    "pr_approved": "PR approved (review)",
    "pr_closed": "PR closed without merge",
}


def render_stage_maps_tab() -> None:
    st.subheader("Map GitHub trigger → Plane stage")

    projects = _load_projects()
    proj_opts = _project_opts(projects)

    col1, col2 = st.columns(2)
    plane_proj_id = ""
    selected_trigger = ""
    selected_state_name = ""

    with col1:
        st.markdown("**Plane project & trigger**")
        plane_proj_name = st.selectbox(
            "Project", options=["", *proj_opts.keys()], key="sm_plane_proj"
        )
        plane_proj_id = proj_opts.get(plane_proj_name, "") if plane_proj_name else ""

        trigger_label = st.selectbox(
            "Trigger",
            options=["", *_TRIGGER_LABELS.values()],
            key="sm_trigger",
        )
        selected_trigger = (
            next((k for k, v in _TRIGGER_LABELS.items() if v == trigger_label), "")
            if trigger_label
            else ""
        )

    with col2:
        st.markdown("**Target Plane state**")
        if plane_proj_id:
            try:
                states = plane_states(plane_proj_id)
                state_names = _state_name_opts(states)
            except Exception as exc:
                st.error(f"Failed to load Plane states: {exc}")
                state_names = []
            selected_state_name = (
                st.selectbox("State", options=["", *state_names], key="sm_state") or ""
            )
        else:
            st.selectbox("State", options=[""], key="sm_state_off", disabled=True)

    if st.button("Map stage", type="primary", key="sm_map_btn"):
        if not all([plane_proj_id, selected_trigger, selected_state_name]):
            st.warning("Select project, trigger, and target state before mapping.")
        else:
            try:
                admin_post(
                    "/admin/stage-maps",
                    {
                        "plane_project_id": plane_proj_id,
                        "trigger": selected_trigger,
                        "plane_state_name": selected_state_name,
                    },
                )
                st.success("Mapping created.")
                st.rerun()
            except Exception as exc:
                if "409" in str(exc):
                    st.error(
                        "Mapping already exists for this project/trigger. "
                        "Delete the existing one first."
                    )
                else:
                    st.error(f"Failed: {exc}")

    st.divider()
    st.subheader("Existing stage mappings")
    try:
        existing = admin_get("/admin/stage-maps")
        if existing:
            proj_names = _project_name_map()
            _table_header("Project", "Trigger", "Target state")
            for row in existing:
                cols = st.columns([3, 3, 3, 1])
                proj_id_val = row.get("plane_project_id", "")
                cols[0].text(proj_names.get(proj_id_val, proj_id_val[:16]))
                cols[1].text(_TRIGGER_LABELS.get(row.get("trigger", ""), row.get("trigger", "")))
                cols[2].text(row.get("plane_state_name", ""))
                _delete_row(cols, f"/admin/stage-maps/{row['id']}", f"sm_{row['id']}")
        else:
            st.info("No stage mappings yet.")
    except Exception as exc:
        st.error(f"Failed to load mappings: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

tab_labels, tab_users, tab_modules, tab_stage_maps = st.tabs(
    ["Labels", "Users", "Modules", "Stage Maps"]
)

with tab_labels:
    render_labels_tab()

with tab_users:
    render_users_tab()

with tab_modules:
    render_modules_tab()

with tab_stage_maps:
    render_stage_maps_tab()
