"""Command-line database management.

Examples:
    python -m auth.manage init-db
    python -m auth.manage create-user --login-id admin@school.edu.cn --role admin
    python -m auth.manage create-api-key --login-id 20260001 --name production
"""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import sys

from auth.database import AppDatabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the application SQLite database")
    parser.add_argument(
        "--database",
        help="Override APP_DATABASE_PATH for this command",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or migrate the database schema")

    create_user = sub.add_parser("create-user", help="Create a login user")
    create_user.add_argument("--login-id", required=True)
    create_user.add_argument("--role", choices=("admin", "user"), default="user")
    create_user.add_argument("--password")

    sub.add_parser("list-users", help="List users without password hashes")

    reset_password = sub.add_parser("reset-password", help="Reset a user's password")
    reset_password.add_argument("--login-id", required=True)
    reset_password.add_argument("--password")

    user_state = sub.add_parser("set-user-active", help="Activate or deactivate a user")
    user_state.add_argument("--login-id", required=True)
    user_state.add_argument("--active", choices=("true", "false"), required=True)

    create_key = sub.add_parser("create-api-key", help="Create an API key")
    create_key.add_argument("--login-id", required=True)
    create_key.add_argument("--name", required=True)
    create_key.add_argument("--expires-days", type=int)

    list_keys = sub.add_parser("list-api-keys", help="List API key metadata")
    list_keys.add_argument("--login-id", required=True)

    revoke_key = sub.add_parser("revoke-api-key", help="Revoke an API key by id")
    revoke_key.add_argument("--id", type=int, required=True)

    sub.add_parser("db-stats", help="Show database path, size, journal and row counts")

    backup = sub.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("--output", required=True)
    return parser


def _password_from_args(value: str = None) -> str:
    if value:
        return value
    first = getpass("Password: ")
    second = getpass("Confirm password: ")
    if first != second:
        raise ValueError("password confirmation does not match")
    return first


def _require_user(database: AppDatabase, login_id: str) -> dict:
    user = database.get_user_by_login_id(login_id)
    if not user:
        raise ValueError("user not found")
    return user


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    database = AppDatabase(args.database)
    database.init_schema()

    try:
        if args.command == "init-db":
            print(json.dumps(database.stats(), ensure_ascii=False, indent=2))
        elif args.command == "create-user":
            user = database.create_user(
                args.login_id,
                _password_from_args(args.password),
                role=args.role,
            )
            print(json.dumps(user, ensure_ascii=False, indent=2))
        elif args.command == "list-users":
            print(json.dumps(database.list_users(), ensure_ascii=False, indent=2))
        elif args.command == "reset-password":
            user = _require_user(database, args.login_id)
            database.reset_password(user["id"], _password_from_args(args.password))
            print(f"Password updated for {user['login_id']}")
        elif args.command == "set-user-active":
            user = _require_user(database, args.login_id)
            updated = database.set_user_active(user["id"], args.active == "true")
            print(json.dumps(updated, ensure_ascii=False, indent=2))
        elif args.command == "create-api-key":
            user = _require_user(database, args.login_id)
            result = database.create_api_key(
                user["id"],
                args.name,
                expires_days=args.expires_days,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("Store this key now. It cannot be displayed again.", file=sys.stderr)
        elif args.command == "list-api-keys":
            user = _require_user(database, args.login_id)
            print(
                json.dumps(
                    database.list_api_keys(user["id"]),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "revoke-api-key":
            if not database.revoke_api_key(args.id):
                raise ValueError("active API key not found")
            print(f"API key {args.id} revoked")
        elif args.command == "db-stats":
            print(json.dumps(database.stats(), ensure_ascii=False, indent=2))
        elif args.command == "backup":
            print(database.backup(args.output))
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
