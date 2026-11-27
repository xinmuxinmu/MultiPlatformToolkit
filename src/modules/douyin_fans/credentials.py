"""Read an existing, account-bound native circle session without copying it."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time


def _load_verified_metadata(path, *, account_id, expected_uid):
    """Read bounded identity metadata only when it matches the selection."""
    if not isinstance(path, str) or not path or not account_id or not expected_uid:
        return None
    try:
        source = Path(path)
        if source.stat().st_size > 1_048_576:
            return None
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("verified") is not True or data.get("identity_match") is not True
            or str(data.get("account_id") or "") != str(account_id)
            or str(data.get("uid") or "") != str(expected_uid)):
        return None
    return data


def load_circle_session(path, *, account_id, expected_uid):
    """Return only credentials scoped to the fixed fandom host, or None.

    The path is explicitly configured locally. Historical identity metadata
    must bind this session to the selected account; live GETs still determine
    whether the session is currently authenticated. No file is ever written.
    """
    data = _load_verified_metadata(path, account_id=account_id, expected_uid=expected_uid)
    if data is None:
        return None
    cookies = []
    rows = data.get("cookies")
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain") or "").lower().lstrip(".")
        if domain != "fandom.starplaylet.com":
            continue
        name, value = row.get("name"), row.get("value")
        if (not isinstance(name, str) or not name or not isinstance(value, str) or not value
                or any(c in name + value for c in "\r\n;")):
            continue
        expires = row.get("expires", row.get("expirationDate", -1))
        if (type(expires) not in (int, float)
                or (isinstance(expires, float) and not math.isfinite(expires))):
            continue
        if isinstance(expires, (int, float)) and 0 < expires <= time.time():
            continue
        cookie = {"name": name, "value": value, "domain": row["domain"],
                  "path": row.get("path") if isinstance(row.get("path"), str)
                  and row["path"].startswith("/") else "/"}
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = expires
        cookies.append(cookie)
    if not cookies:
        return None
    headers = {}
    raw_headers = data.get("headers")
    for key, value in raw_headers.items() if isinstance(raw_headers, dict) else ():
        if (isinstance(key, str) and key.lower() in {"user-agent", "accept", "x-scm-version"}
                and isinstance(value, str) and "\r" not in value and "\n" not in value):
            headers[key] = value
    params = {}
    raw_params = data.get("common_params")
    for key, value in raw_params.items() if isinstance(raw_params, dict) else ():
        if key in {"aid", "device_platform", "scm_version", "from_app"} and type(value) in (str, int):
            params[key] = value
    return {"cookies": cookies, "headers": headers, "common_params": params,
            "source": "account_bound_native_circle_session"}


def _load_store_session(auth_path, store_path, *, account_id, expected_uid,
                        allowed_domains, source):
    metadata = _load_verified_metadata(auth_path, account_id=account_id, expected_uid=expected_uid)
    if metadata is None or not isinstance(store_path, str) or not store_path:
        return None
    from .native_cookie_store import CookieFormatError, decode_store

    try:
        path = Path(store_path)
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        cookies = decode_store(path.read_bytes(), allowed_domains=allowed_domains)
    except (OSError, ValueError, CookieFormatError):
        return None
    if not cookies:
        return None
    return {"cookies": cookies, "headers": metadata["headers"],
            "common_params": metadata["common_params"],
            "source": source}


def load_aweme_session(auth_path, store_path, *, account_id, expected_uid):
    """Read original Aweme-domain cookies; never re-scope them."""
    return _load_store_session(
        auth_path, store_path, account_id=account_id, expected_uid=expected_uid,
        allowed_domains=("snssdk.com", "aweme.snssdk.com"),
        source="account_bound_native_aweme_session",
    )


def load_webcast_session(auth_path, store_path, *, account_id, expected_uid):
    """Read original Webcast-domain cookies; never re-scope them."""
    return _load_store_session(
        auth_path, store_path, account_id=account_id, expected_uid=expected_uid,
        allowed_domains=("webcast.amemv.com", "amemv.com"),
        source="account_bound_native_webcast_session",
    )
