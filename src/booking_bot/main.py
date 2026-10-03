import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from booking_bot.api.router import api_router
from booking_bot.api.routes.health import router as health_router
from booking_bot.bot import dispatcher
from booking_bot.config import get_settings
from booking_bot.db.session import engine
from booking_bot.logging_config import configure_logging
from booking_bot.specialist_config import load_specialist_template

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        configure_logging(settings)
        load_specialist_template(settings.specialist_config_path)
        yield
    finally:
        try:
            await dispatcher.fsm.close()
        finally:
            await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(health_router, include_in_schema=False)

    @app.middleware("http")
    async def basic_api_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled HTTP error", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
