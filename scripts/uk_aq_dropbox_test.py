#!/usr/bin/env python3
"""Test Dropbox OAuth refresh token and optional upload."""

import argparse
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DROPBOX_ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"
DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DEFAULT_FOLDER = "/raw_data"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test Dropbox refresh token and optional upload.")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload a small JSON payload to the Dropbox folder for verification.",
    )
    parser.add_argument(
        "--folder",
        default=None,
        help="Dropbox folder path override (default: env UK_AIR_RAW_DROPBOX_FOLDER).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_key = os.getenv("DROPBOX_APP_KEY", "").strip()
    app_secret = os.getenv("DROPBOX_APP_SECRET", "").strip()
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN", "").strip()
    folder = (args.folder or os.getenv("UK_AIR_RAW_DROPBOX_FOLDER") or DEFAULT_FOLDER).strip()

    if not (app_key and app_secret and refresh_token):
        raise SystemExit("Missing Dropbox env vars: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN")

    access_token = refresh_access_token(app_key, app_secret, refresh_token)
    account = get_account(access_token)
    print("Dropbox account:", json.dumps(account, indent=2))

    if args.upload:
        upload_test_file(access_token, folder)
        print("Upload OK")


def refresh_access_token(app_key: str, app_secret: str, refresh_token: str) -> str:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app_key,
        "client_secret": app_secret,
    }
    resp = requests.post(DROPBOX_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise SystemExit(f"Dropbox token request failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise SystemExit("Dropbox token response missing access_token.")
    return token


def get_account(access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.post(DROPBOX_ACCOUNT_URL, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise SystemExit(f"Dropbox account request failed ({resp.status_code}): {resp.text}")
    return resp.json()


def upload_test_file(access_token: str, folder: str) -> None:
    filename = f"uk_aq_dropbox_test_{utcnow().replace(':', '').replace('+', '')}.json"
    path = build_path(folder, filename)
    payload = json.dumps({"tested_at": utcnow()}, ensure_ascii=True).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add", "autorename": True}),
        "Content-Type": "application/octet-stream",
    }
    resp = requests.post(DROPBOX_UPLOAD_URL, headers=headers, data=payload, timeout=60)
    if resp.status_code >= 400:
        raise SystemExit(f"Dropbox upload failed ({resp.status_code}): {resp.text}")


def build_path(folder: str, filename: str) -> str:
    cleaned = folder.strip() or DEFAULT_FOLDER
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return f"{cleaned.rstrip('/')}/{filename}"


if __name__ == "__main__":
    main()
