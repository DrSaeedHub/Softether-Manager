"""The stored SoftEther user credentials, for embedding into .vpn files.

SoftEther keeps a password-authenticated user's credential as
``SHA-0(password + UPPERCASE(username))`` -- on the server (``HashedKey`` in
vpn_server.config) and in the client's connection files (``HashedPassword``)
alike. The same base64 string in both places means the panel can hand out a
file that signs in without ever holding the plaintext.

Two sources, one cache:

* users created or re-passworded **through the panel** save their hash here
  the moment the plaintext passes through;
* anyone else's is recovered once from the server's own configuration file
  (the ``GetConfig`` RPC) and cached, so the next file needs no rediscovery.

Rows are keyed by hub + username and hold only the hash the server already
stores -- nothing here is more secret than vpn_server.config itself.
"""
from __future__ import annotations

import base64
from typing import Optional

from .db import get_db, utc_now


def save_credential(hub: str, username: str, password_hash: str) -> None:
    get_db().execute(
        'INSERT INTO "VpnUserCredential"("HubName", "UserName", "PasswordHash", "UpdatedDate") '
        "VALUES (:hub, :name, :hash, :now) "
        'ON CONFLICT("HubName", "UserName") DO UPDATE SET "PasswordHash" = :hash, "UpdatedDate" = :now',
        {"hub": hub, "name": username, "hash": password_hash, "now": utc_now()},
    )


def delete_credential(hub: str, username: str) -> None:
    get_db().execute(
        'DELETE FROM "VpnUserCredential" WHERE "HubName" = :hub AND "UserName" = :name',
        {"hub": hub, "name": username},
    )


def stored_credential(hub: str, username: str) -> Optional[str]:
    row = get_db().query_one(
        'SELECT "PasswordHash" FROM "VpnUserCredential" '
        'WHERE "HubName" = :hub AND "UserName" = :name',
        {"hub": hub, "name": username},
    )
    return row["PasswordHash"] if row else None


def credential_from_server_config(hub: str, username: str) -> Optional[str]:
    """Read the user's stored password hash out of vpn_server.config, via RPC.

    The config is SoftEther's own folder syntax; rather than a full parser,
    the walk tracks the ``declare`` stack until it stands inside
    root / VirtualHUB / <hub> / SecurityAccountDatabase / UserList /
    <username> and takes the ``byte AuthPassword`` line there (the 4.x
    server writes the hash flat on the user; older layouts nest it as
    ``HashedKey`` inside an ``AuthData`` folder, accepted too).
    """
    from .se import rpc

    result = rpc("GetConfig")
    try:
        text = base64.b64decode(result.get("FileData_bin", "")).decode("utf-8", "replace")
    except Exception:
        return None

    user_path = ["root", "VirtualHUB", hub, "SecurityAccountDatabase", "UserList", username]
    keys = ("byte AuthPassword ", "byte HashedKey ")
    stack: list[str] = []
    pending: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if line.startswith("declare "):
            pending = line[len("declare ") :].strip()
        elif line == "{":
            stack.append(pending if pending is not None else "?")
            pending = None
        elif line == "}":
            if stack:
                stack.pop()
        elif stack == user_path or stack == user_path + ["AuthData"]:
            for key in keys:
                if line.startswith(key):
                    value = line[len(key) :].strip()
                    if value and value != "$":
                        return value
    return None


def obtain_credential(hub: str, username: str) -> Optional[str]:
    """The stored hash, discovering and caching it from the server config
    when the panel has never seen this user's password."""
    found = stored_credential(hub, username)
    if found:
        return found
    found = credential_from_server_config(hub, username)
    if found:
        save_credential(hub, username, found)
    return found
