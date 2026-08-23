"""Independent Starlette entry point for MindFlow Admin."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.admin_web.api import AdminAPI
from app.admin_web.repositories import AdminRepository
from app.bootstrap import build_business_services
from app.config import Settings
from app.db import Database, build_engine
from app.repositories import AgentRunRepository
from app.services.pressure_curve_service import PressureCurveService


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    database: Database,
    settings: Settings,
    pressure_curves: PressureCurveService | None = None,
) -> Starlette:
    api = AdminAPI(AdminRepository(database), settings, pressure_curves)

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
    return Starlette(debug=False, routes=routes)


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    database = Database(build_engine(settings.database_url))
    services = build_business_services(
        database, settings, AgentRunRepository(database)
    )
    app = create_app(database, settings, services.pressure_curves)
    uvicorn.run(app, host=settings.admin_host, port=settings.admin_port)


if __name__ == "__main__":
    main()
