"""Independent Starlette entry point for MindFlow Admin."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.admin_web.api import AdminAPI
from app.admin_web.admin_users import AdminUserRepository
from app.admin_web.repositories import AdminRepository
from app.bootstrap import build_business_services
from app.build_info import announce_build
from app.config import Settings
from app.db import Database, build_engine
from app.repositories import AgentRunRepository
from app.services.pressure_curve_service import PressureCurveService
from app.services.daily_review_service import DailyReviewService


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    database: Database,
    settings: Settings,
    pressure_curves: PressureCurveService | None = None,
    daily_reviews: DailyReviewService | None = None,
    dependency_refresh: Any = None,
) -> Starlette:
    settings.validate_admin()
    admin_users = AdminUserRepository(database)
    admin_users.ensure_environment_superadmin(
        settings.admin_username, settings.admin_password_hash
    )
    api = AdminAPI(
        AdminRepository(database), settings, pressure_curves,
        admin_users=admin_users, daily_reviews=daily_reviews,
    )

    async def root(_request: Request):
        return RedirectResponse("/admin/")

    async def frontend(_request: Request):
        return FileResponse(STATIC_DIR / "index.html")

    routes = [Route("/", root, methods=["GET"]), *api.routes()]
    routes.extend(
        [
            Mount(
                "/admin/static",
                app=StaticFiles(directory=STATIC_DIR),
                name="admin-static",
            ),
            Route("/admin", frontend, methods=["GET"]),
            Route("/admin/{path:path}", frontend, methods=["GET"]),
        ]
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        if dependency_refresh is not None:
            dependency_refresh.start()
        try:
            yield
        finally:
            if dependency_refresh is not None:
                await dependency_refresh.close()

    return Starlette(debug=False, routes=routes, lifespan=lifespan)


def main() -> None:
    import uvicorn

    announce_build("admin")
    settings = Settings.from_env()
    database = Database(build_engine(settings.database_url))
    services = build_business_services(
        database, settings, AgentRunRepository(database)
    )
    app = create_app(
        database,
        settings,
        services.pressure_curves,
        services.daily_reviews,
        services.dependency_refresh,
    )
    uvicorn.run(app, host=settings.admin_host, port=settings.admin_port)


if __name__ == "__main__":
    main()
