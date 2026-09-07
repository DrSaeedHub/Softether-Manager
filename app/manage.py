"""Maintenance commands for the ``sem`` CLI: the paths back in.

Everything here has a browser equivalent; these exist for the moments the
browser one cannot be reached -- nobody knows the password, the web path was
forgotten, the panel will not start. They talk straight to the database.

    python -m app.manage password --username admin --password ...
    python -m app.manage users
    python -m app.manage webpath <path|"">
    python -m app.manage connect --host 127.0.0.1 --port 5555 --password ...
    python -m app.manage domain [show | set <name> | clear | only on|off]

The ``domain`` commands exist for the day the domain-only restriction locks
the operator out -- a DNS record that moved, a certificate that could not be
renewed -- and the panel can only be reached from a shell on the machine.
"""
from __future__ import annotations

import argparse
import sys

from .db import get_db, utc_now
from .security import hash_password


def cmd_password(username: str, password: str) -> int:
    if not password:
        print("The password cannot be empty.", file=sys.stderr)
        return 1
    db = get_db()
    now = utc_now()
    row = db.query_one(
        'SELECT "UserID" FROM "PanelUser" WHERE "Username" = :u AND "IsDeleted" = 0',
        {"u": username},
    )
    if row:
        db.execute(
            'UPDATE "PanelUser" SET "PasswordHash" = :h, "UpdatedDate" = :now WHERE "UserID" = :id',
            {"h": hash_password(password), "now": now, "id": row["UserID"]},
        )
        print(f"Password updated for {username}.")
    else:
        db.execute(
            'INSERT INTO "PanelUser"("Username", "PasswordHash", "CreatedDate", "UpdatedDate") '
            "VALUES (:u, :h, :now, :now)",
            {"u": username, "h": hash_password(password), "now": now},
        )
        print(f"Account {username} created.")
    return 0


def cmd_users() -> int:
    rows = get_db().query_all(
        'SELECT "Username", "CreatedDate" FROM "PanelUser" WHERE "IsDeleted" = 0 ORDER BY "UserID"'
    )
    if not rows:
        print("No accounts exist yet; the first sign-in screen will create one.")
        return 0
    for row in rows:
        print(f"{row['Username']}\tsince {row['CreatedDate']}")
    return 0


def cmd_webpath(value: str) -> int:
    from .settings_store import set_setting

    cleaned = value.strip().strip("/")
    if cleaned and not all(c.isalnum() or c in "._~-" for c in cleaned):
        print("The web path may contain only letters, digits, dot, underscore, tilde and hyphen.", file=sys.stderr)
        return 1
    set_setting("web_path", cleaned)
    print(f"Web path set to '{cleaned or '(root)'}'. Restart the service to apply it.")
    return 0


def cmd_connect(host: str, port: int, password: str) -> int:
    """Point the panel at a SoftEther server without going through the UI.

    The installer uses this after it installs SoftEther on a fresh host: the
    password is encrypted with this installation's own key on the way in, the
    same as saving the connection from the Connect screen would do.
    """
    from .se import set_connection

    set_connection(host, port, password)
    print(f"Connection set to {host}:{port}.")
    return 0


def cmd_domain(action: str, value: str) -> int:
    """Show or change the panel's domain settings straight in the database.
    The running service adopts a change at its next tick (within half a
    minute) -- the restriction immediately, the listeners shortly after."""
    from .services.tls import https_ready, normalise_domain
    from .settings_store import get_setting, set_setting

    if action in ("", "show"):
        domain = str(get_setting("domain") or "")
        print(f"domain:      {domain or '(none)'}")
        if domain:
            print(f"https port:  {get_setting('https_port')}")
            print(f"certificate: {'ready' if https_ready(domain) else 'not issued yet, or expired'}")
        print(f"domain-only: {'on' if get_setting('domain_only') else 'off'}")
        return 0
    if action == "set":
        try:
            name = normalise_domain(value)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not name:
            print("A domain name is needed.", file=sys.stderr)
            return 1
        set_setting("domain", name)
        set_setting("domain_only", False)
        print(f"Domain set to {name}. The panel obtains a certificate for it on its own; domain-only access is off until you turn it on from the panel.")
        return 0
    if action == "clear":
        set_setting("domain", "")
        set_setting("domain_only", False)
        print("Domain cleared. The HTTPS listener stops and the panel answers on every name again.")
        return 0
    if action == "only":
        flag = value.strip().lower()
        if flag not in ("on", "off"):
            print("Usage: domain only on|off", file=sys.stderr)
            return 1
        if flag == "on" and not str(get_setting("domain") or ""):
            print("Set a domain first.", file=sys.stderr)
            return 1
        set_setting("domain_only", flag == "on")
        print(f"Domain-only access {'on' if flag == 'on' else 'off'}.")
        return 0
    print("Usage: domain [show | set <name> | clear | only on|off]", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)

    p_password = sub.add_parser("password")
    p_password.add_argument("--username", required=True)
    p_password.add_argument("--password", required=True)

    sub.add_parser("users")

    p_webpath = sub.add_parser("webpath")
    p_webpath.add_argument("value", nargs="?", default="")

    p_connect = sub.add_parser("connect")
    p_connect.add_argument("--host", default="127.0.0.1")
    p_connect.add_argument("--port", type=int, default=5555)
    p_connect.add_argument("--password", required=True)

    p_domain = sub.add_parser("domain")
    p_domain.add_argument("action", nargs="?", default="show")
    p_domain.add_argument("value", nargs="?", default="")

    args = parser.parse_args()
    if args.command == "password":
        return cmd_password(args.username, args.password)
    if args.command == "users":
        return cmd_users()
    if args.command == "webpath":
        return cmd_webpath(args.value)
    if args.command == "connect":
        return cmd_connect(args.host, args.port, args.password)
    if args.command == "domain":
        return cmd_domain(args.action, args.value)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
