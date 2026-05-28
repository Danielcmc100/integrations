import os

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
def _fetch(token: str, base: str, path: str) -> list[dict]:  # type: ignore[type-arg]
    r = requests.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def plane_projects() -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, "/admin/plane/projects")


def plane_labels(project_id: str) -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/labels")


def plane_modules(project_id: str) -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/modules")


def plane_members(project_id: str) -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, f"/admin/plane/projects/{project_id}/members")


def github_repos() -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, "/admin/github/repos")


def github_labels(owner: str, repo: str) -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, f"/admin/github/repos/{owner}/{repo}/labels")


def github_collaborators(owner: str, repo: str) -> list[dict]:  # type: ignore[type-arg]
    return _fetch(TOKEN, BASE, f"/admin/github/repos/{owner}/{repo}/collaborators")


# ── Non-cached admin CRUD ─────────────────────────────────────────────────────


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def admin_get(path: str) -> list[dict]:  # type: ignore[type-arg]
    r = requests.get(f"{BASE}{path}", headers=_headers(), timeout=15)
    if r.status_code == 401:
        st.error("Invalid token.")
        st.stop()
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def admin_post(path: str, body: dict) -> None:  # type: ignore[type-arg]
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


def _project_opts(projects: list[dict]) -> dict[str, str]:  # type: ignore[type-arg]
    """name → id"""
    return {p.get("name", p.get("id", "?")): p["id"] for p in projects if "id" in p}


def _label_opts(labels: list[dict]) -> dict[str, str]:  # type: ignore[type-arg]
    """name → id"""
    return {lb.get("name", lb.get("id", "?")): lb["id"] for lb in labels if "id" in lb}


def _module_opts(modules: list[dict]) -> dict[str, str]:  # type: ignore[type-arg]
    """name → id"""
    return {m.get("name", m.get("id", "?")): m["id"] for m in modules if "id" in m}


def _member_opts(members: list[dict]) -> dict[str, str]:  # type: ignore[type-arg]
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


def _gh_label_names(labels: list[dict]) -> list[str]:  # type: ignore[type-arg]
    return [lb["name"] for lb in labels if "name" in lb]


def _collab_logins(collabs: list[dict]) -> list[str]:  # type: ignore[type-arg]
    return [c["login"] for c in collabs if "login" in c]


def _repo_full_names(repos: list[dict]) -> list[str]:  # type: ignore[type-arg]
    return [r["full_name"] for r in repos if "full_name" in r]


# ── Shared loaders ────────────────────────────────────────────────────────────


def _load_projects() -> list[dict]:  # type: ignore[type-arg]
    try:
        return plane_projects()
    except Exception as exc:
        st.error(f"Failed to load Plane projects: {exc}")
        return []


def _load_gh_repos() -> list[dict]:  # type: ignore[type-arg]
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
    cols: list,  # type: ignore[type-arg]
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
        gh_repo = (
            st.selectbox("Repository", options=["", *repo_names], key="lbl_gh_repo") or ""
        )

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
            _table_header("Plane project", "Plane label ID", "GH repo", "GH label")
            for row in existing:
                cols = st.columns([3, 3, 3, 3, 1])
                cols[0].text(row.get("plane_project_id", "")[:16])
                cols[1].text(row.get("plane_label_id", "")[:16])
                cols[2].text(row.get("gh_repo", ""))
                cols[3].text(row.get("gh_label", ""))
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
        gh_repo = (
            st.selectbox("Repository", options=["", *repo_names], key="usr_gh_repo") or ""
        )

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
            _table_header("Plane user ID", "GitHub login", "Discord user ID")
            for row in existing:
                cols = st.columns([3, 3, 3, 1])
                cols[0].text(row.get("plane_user_id", "")[:16])
                cols[1].text(row.get("gh_login", ""))
                cols[2].text(row.get("discord_user_id") or "—")
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
            mod_name = st.selectbox(
                "Module", options=["", *mod_opts.keys()], key="mod_plane_mod"
            )
            plane_module_id = mod_opts.get(mod_name, "") if mod_name else ""
        else:
            st.selectbox("Module", options=[""], key="mod_plane_mod_off", disabled=True)

    with col2:
        st.markdown("**GitHub**")
        repo_names = _repo_full_names(repos)
        gh_repo_m = (
            st.selectbox("Repository", options=["", *repo_names], key="mod_gh_repo") or ""
        )

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
            _table_header("Plane module ID", "Plane project ID", "GitHub repo")
            for row in existing:
                cols = st.columns([3, 3, 3, 1])
                cols[0].text(row.get("plane_module_id", "")[:16])
                cols[1].text(row.get("plane_project_id", "")[:16])
                cols[2].text(row.get("gh_repo", ""))
                _delete_row(
                    cols,
                    f"/admin/repo-modules/{row['plane_module_id']}",
                    f"mod_{row['plane_module_id']}",
                )
        else:
            st.info("No module mappings yet.")
    except Exception as exc:
        st.error(f"Failed to load mappings: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

tab_labels, tab_users, tab_modules = st.tabs(["Labels", "Users", "Modules"])

with tab_labels:
    render_labels_tab()

with tab_users:
    render_users_tab()

with tab_modules:
    render_modules_tab()
