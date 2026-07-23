#!/usr/bin/env python3
"""Issue fresh Supabase auth tokens for local UK AQ dashboard development."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or os.getenv("SB_SUPABASE_URL") or "").strip().rstrip("/")


def env_publishable_key() -> str:
    return (
        os.getenv("SB_PUBLISHABLE_DEFAULT_KEY")
        or ""
    ).strip()


def request_token(
    *,
    supabase_url: str,
    publishable_key: str,
    grant_type: str,
    payload: dict[str, str],
) -> dict[str, object]:
    req = urllib_request.Request(
        f"{supabase_url}/auth/v1/token?grant_type={grant_type}",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "apikey": publishable_key,
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        message = exc.reason
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body)
            message = parsed.get("msg") or parsed.get("message") or message
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            message = str(message)
        raise SystemExit(f"Auth request failed ({exc.code}): {message}") from exc


def upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    found = set()
    new_lines: list[str] = []
    for line in lines:
        replaced = False
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                found.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in found:
            new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Issue fresh UK_AQ_DEV_JWT and UK_AQ_DEV_REFRESH_TOKEN for local dashboard use."
    )
    parser.add_argument(
        "--email",
        default=os.getenv("UK_AQ_DEV_USER_EMAIL", ""),
        help="Dev user email (defaults to UK_AQ_DEV_USER_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("UK_AQ_DEV_USER_PASSWORD", ""),
        help="Dev user password (defaults to UK_AQ_DEV_USER_PASSWORD).",
    )
    parser.add_argument(
        "--refresh-token",
        default=os.getenv("UK_AQ_DEV_REFRESH_TOKEN", ""),
        help="Use refresh-token grant flow (defaults to UK_AQ_DEV_REFRESH_TOKEN).",
    )
    parser.add_argument(
        "--write-env-file",
        default="",
        help=(
            "Optional env file to upsert UK_AQ_DEV_JWT, "
            "UK_AQ_DEV_REFRESH_TOKEN, and UK_AQ_DEV_JWT_EXPIRES_AT."
        ),
    )
    parser.add_argument(
        "--output",
        choices=("shell", "json"),
        default="shell",
        help="Output format (default: shell).",
    )
    return parser.parse_args()


def main() -> None:
    load_env(Path(".env"))
    load_env(Path(".env.supabase"))
    args = parse_args()

    supabase_url = env_supabase_url()
    publishable_key = env_publishable_key()
    if not supabase_url:
        raise SystemExit("Missing SUPABASE_URL or SB_SUPABASE_URL.")
    if not publishable_key:
        raise SystemExit(
            "Missing SB_PUBLISHABLE_DEFAULT_KEY."
        )

    refresh_token = args.refresh_token.strip()
    email = args.email.strip()
    password = args.password
    prefer_password_flow = bool(email or password)

    if refresh_token and not prefer_password_flow:
        token_payload = request_token(
            supabase_url=supabase_url,
            publishable_key=publishable_key,
            grant_type="refresh_token",
            payload={"refresh_token": refresh_token},
        )
    else:
        if not email or not password:
            raise SystemExit(
                "Either provide --refresh-token (or UK_AQ_DEV_REFRESH_TOKEN), "
                "or provide --email/--password (or UK_AQ_DEV_USER_EMAIL/UK_AQ_DEV_USER_PASSWORD)."
            )
        token_payload = request_token(
            supabase_url=supabase_url,
            publishable_key=publishable_key,
            grant_type="password",
            payload={"email": email, "password": password},
        )

    access_token = str(token_payload.get("access_token") or "").strip()
    next_refresh_token = str(token_payload.get("refresh_token") or "").strip()
    token_type = str(token_payload.get("token_type") or "").strip()
    expires_in = int(token_payload.get("expires_in") or 0)
    expires_at_epoch = int(time.time()) + max(0, expires_in)

    if not access_token or not next_refresh_token:
        raise SystemExit("Auth response missing access_token or refresh_token.")

    if args.write_env_file:
        env_path = Path(args.write_env_file)
        upsert_env_file(
            env_path,
            {
                "UK_AQ_DEV_JWT": access_token,
                "UK_AQ_DEV_REFRESH_TOKEN": next_refresh_token,
                "UK_AQ_DEV_JWT_EXPIRES_AT": str(expires_at_epoch),
            },
        )

    if args.output == "json":
        print(
            json.dumps(
                {
                    "token_type": token_type,
                    "expires_in": expires_in,
                    "expires_at_epoch": expires_at_epoch,
                    "UK_AQ_DEV_JWT": access_token,
                    "UK_AQ_DEV_REFRESH_TOKEN": next_refresh_token,
                    "UK_AQ_DEV_JWT_EXPIRES_AT": str(expires_at_epoch),
                },
                indent=2,
            )
        )
        return

    print(f"export UK_AQ_DEV_JWT='{access_token}'")
    print(f"export UK_AQ_DEV_REFRESH_TOKEN='{next_refresh_token}'")
    print(f"export UK_AQ_DEV_JWT_EXPIRES_AT='{expires_at_epoch}'")
    print(f"# token_type={token_type or 'bearer'} expires_in={expires_in}s")
    if args.write_env_file:
        print(f"# updated {args.write_env_file}")


if __name__ == "__main__":
    main()
