#!/usr/bin/env python3
"""Seed admin API with data from mappings.yaml.

Usage:
    python scripts/seed_mappings.py [--base-url URL] [--token TOKEN] [--dry-run]

Env vars (fallback):
    BASE_URL    — default http://localhost:8000
    ADMIN_TOKEN — required if not passed via --token
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
import yaml


def load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def post(client: httpx.Client, url: str, payload: dict, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY] POST {url}  {payload}")
        return
    resp = client.post(url, json=payload)
    status = resp.status_code
    mark = "OK" if resp.is_success else "FAIL"
    print(f"  [{mark} {status}] POST {url}  {payload}")
    if not resp.is_success:
        print(f"         {resp.text[:200]}")


def seed(base_url: str, token: str, data: dict, dry_run: bool) -> None:
    plane_project_id = data["plane_project_id"]
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=base_url, headers=headers, timeout=10) as client:
        print("\n=== repo-modules ===")
        for entry in data.get("repo_module_map", []):
            payload = {
                "plane_module_id": entry["plane_module_id"],
                "plane_project_id": plane_project_id,
                "gh_repo": entry["gh_repo"],
            }
            post(client, "/admin/repo-modules", payload, dry_run)

        print("\n=== labels ===")
        for entry in data.get("label_map", []):
            payload = {
                "plane_project_id": plane_project_id,
                "plane_label_id": entry["plane_label_id"],
                "gh_repo": entry["gh_repo"],
                "gh_label": entry["gh_label"],
            }
            post(client, "/admin/labels", payload, dry_run)

        print("\n=== users ===")
        for entry in data.get("user_map", []):
            payload = {
                "plane_user_id": entry["plane_user_id"],
                "gh_login": entry["gh_login"],
                "discord_user_id": str(entry["discord_user_id"]),
            }
            post(client, "/admin/users", payload, dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed admin API from mappings.yaml")
    parser.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.getenv("ADMIN_TOKEN"))
    parser.add_argument("--dry-run", action="store_true", help="Print requests, do not send")
    parser.add_argument(
        "--file",
        default=Path(__file__).parent / "mappings.yaml",
        type=Path,
    )
    args = parser.parse_args()

    if not args.token and not args.dry_run:
        print("ERROR: ADMIN_TOKEN env var or --token required", file=sys.stderr)
        sys.exit(1)

    data = load_yaml(args.file)
    print(f"Base URL : {args.base_url}")
    print(f"File     : {args.file}")
    print(f"Dry run  : {args.dry_run}")

    seed(args.base_url, args.token or "dry-run", data, args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
