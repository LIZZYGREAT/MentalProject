"""Flask request authentication for browser sessions and server API keys."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional

from flask import g, jsonify, redirect, request, session, url_for

from auth.database import AppDatabase


def get_database() -> AppDatabase:
    database = getattr(g, "_app_database", None)
    if database is None:
        database = AppDatabase()
        g._app_database = database
    return database


def get_identity() -> Optional[dict]:
    """Resolve and cache the current session user or Bearer API-key owner."""
    if hasattr(g, "auth_identity"):
        return g.auth_identity

    database = get_database()
    identity = None
    session_user_id = session.get("user_id")
    if session_user_id is not None:
        identity = database.get_user(int(session_user_id))
        if identity and identity.get("is_active"):
            identity["auth_type"] = "session"
        else:
            session.clear()
            identity = None

    if identity is None:
        auth_header = request.headers.get("Authorization", "")
        raw_key = ""
        if auth_header.lower().startswith("bearer "):
            raw_key = auth_header[7:].strip()
        elif request.headers.get("X-API-Key"):
            raw_key = request.headers["X-API-Key"].strip()
        if raw_key:
            identity = database.authenticate_api_key(raw_key)

    g.auth_identity = identity
    return identity


def auth_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        identity = get_identity()
        if identity:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify(
                {
                    "status": "error",
                    "code": "authentication_required",
                    "message": "请登录或提供有效的 Bearer API Key",
                }
            ), 401
        return redirect(url_for("login_page", next=request.full_path))

    return wrapped


def session_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        identity = get_identity()
        if identity and identity.get("auth_type") == "session":
            return view(*args, **kwargs)
        return jsonify(
            {
                "status": "error",
                "code": "browser_session_required",
                "message": "此操作需要浏览器登录会话",
            }
        ), 401

    return wrapped


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        identity = get_identity()
        if not identity:
            return jsonify(
                {
                    "status": "error",
                    "code": "authentication_required",
                    "message": "请先登录",
                }
            ), 401
        if identity.get("role") != "admin":
            return jsonify(
                {
                    "status": "error",
                    "code": "admin_required",
                    "message": "需要管理员权限",
                }
            ), 403
        return view(*args, **kwargs)

    return wrapped
