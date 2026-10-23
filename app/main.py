"""FastAPI application: JSON API under /api, static UI at /."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, auth, chat, documents, health
from app.api.middleware import RequestContextMiddleware
from app.config import get_settings
from app.log import configure_logging

WEB = Path(__file__).resolve().parent / "web"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Jigsaw Policy Assistant",
        version="1.0.0",
        description="Answers staff questions from Jigsaw Insurance policy documents, with page citations. "
        "Answers are reference information, not coverage decisions.",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware, secure=settings.secure_cookies)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values back (they may contain passwords).
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {"code": "invalid_request", "message": "Please check the form.", "errors": errors}
            },
        )

    for module in (health, auth, documents, chat, admin):
        app.include_router(module.router)
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
