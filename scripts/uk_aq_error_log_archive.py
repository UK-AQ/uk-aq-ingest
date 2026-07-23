#!/usr/bin/env python3
"""
Archive per-error Dropbox logs into daily ZIPs.

Usage:
  python3 scripts/uk_aq_error_log_archive.py
  python3 scripts/uk_aq_error_log_archive.py --date 2026-01-07
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_LIST_FOLDER_URL = "https://api.dropboxapi.com/2/files/list_folder"
DROPBOX_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"
DROPBOX_DOWNLOAD_ZIP_URL = "https://content.dropboxapi.com/2/files/download_zip"
DEFAULT_ERROR_DROPBOX_FOLDER = "/error_log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive UK-AQ error logs in Dropbox.")
    parser.add_argument(
        "--date",
        help="Date to archive (YYYY-MM-DD). Defaults to yesterday (UTC).",
    )
    parser.add_argument(
        "--retain-days",
        type=int,
        default=365,
        help="Retention window for archived ZIPs in days (default: 365).",
    )
    return parser.parse_args()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_root_folder(folder: str) -> str:
    cleaned = (folder or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    cleaned = cleaned.rstrip("/")
    if cleaned.endswith("/error_log"):
        cleaned = cleaned[: -len("/error_log")]
    return cleaned


def error_log_root(folder: str) -> str:
    root = normalize_root_folder(folder or DEFAULT_ERROR_DROPBOX_FOLDER)
    if root:
        return f"{root}/error_log"
    return "/error_log"


def refresh_access_token() -> str:
    app_key = os.getenv("DROPBOX_APP_KEY", "").strip()
    app_secret = os.getenv("DROPBOX_APP_SECRET", "").strip()
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN", "").strip()
    if not (app_key and app_secret and refresh_token):
        raise RuntimeError("Dropbox credentials are required.")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app_key,
        "client_secret": app_secret,
    }
    resp = requests.post(DROPBOX_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox token request failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Dropbox token response missing access_token.")
    return token


def dropbox_download_zip(access_token: str, folder_path: str) -> Optional[bytes]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": json.dumps({"path": folder_path}),
    }
    resp = requests.post(DROPBOX_DOWNLOAD_ZIP_URL, headers=headers, timeout=120)
    if resp.status_code == 409:
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox download_zip failed ({resp.status_code}): {resp.text}")
    return resp.content


def dropbox_upload_bytes(access_token: str, payload: bytes, dropbox_path: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": json.dumps(
            {
                "path": dropbox_path,
                "mode": "overwrite",
                "autorename": False,
                "mute": False,
            }
        ),
        "Content-Type": "application/octet-stream",
    }
    resp = requests.post(DROPBOX_UPLOAD_URL, headers=headers, data=payload, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox upload failed ({resp.status_code}): {resp.text}")


def dropbox_delete(access_token: str, path: str) -> None:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    resp = requests.post(DROPBOX_DELETE_URL, headers=headers, json={"path": path}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox delete failed ({resp.status_code}): {resp.text}")


def dropbox_list_folder(access_token: str, path: str) -> List[Dict[str, Any]]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload: Dict[str, Any] = {"path": path}
    entries: List[Dict[str, Any]] = []
    while True:
        resp = requests.post(DROPBOX_LIST_FOLDER_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 409:
            return []
        if resp.status_code >= 400:
            raise RuntimeError(f"Dropbox list_folder failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        entries.extend(data.get("entries", []))
        if not data.get("has_more"):
            return entries
        payload = {"cursor": data.get("cursor")}


def error_dropbox_allowed() -> bool:
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    allowed_url = os.getenv("UK_AIR_ERROR_DROPBOX_ALLOWED_SUPABASE_URL", "").strip()
    if not allowed_url:
        return False
    return supabase_url == allowed_url


def main() -> None:
    args = parse_args()
    if not error_dropbox_allowed():
        print("Error Dropbox archive disabled (allowlist mismatch).")
        return

    target_date = utcnow().date() - timedelta(days=1)
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    folder = os.getenv("UK_AIR_ERROR_DROPBOX_FOLDER") or DEFAULT_ERROR_DROPBOX_FOLDER
    root = error_log_root(folder)
    date_folder = target_date.strftime("%Y-%m-%d")
    log_folder = f"{root}/{date_folder}"
    archive_folder = root
    archive_path = f"{archive_folder}/{date_folder}.zip"

    access_token = refresh_access_token()
    zipped = dropbox_download_zip(access_token, log_folder)
    if zipped is None:
        print(f"No error log folder found for {date_folder}.")
    else:
        dropbox_upload_bytes(access_token, zipped, archive_path)
        dropbox_delete(access_token, log_folder)
        print(f"Archived {date_folder} logs to {archive_path}.")

    cutoff_date = utcnow().date() - timedelta(days=args.retain_days)
    for entry in dropbox_list_folder(access_token, archive_folder):
        if entry.get(".tag") != "file":
            continue
        name = entry.get("name") or ""
        if not name.endswith(".zip"):
            continue
        date_part = name[:-4]
        try:
            archive_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if archive_date >= cutoff_date:
            continue
        path = entry.get("path_lower") or entry.get("path_display")
        if not path:
            continue
        dropbox_delete(access_token, path)
        print(f"Deleted archived log: {path}.")


if __name__ == "__main__":
    main()
