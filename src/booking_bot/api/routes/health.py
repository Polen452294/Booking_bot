import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot import dispatcher
from booking_bot.config import Settings, get_settings
from booking_bot.db.session import get_session
from booking_bot.services.specialist_context import get_specialist_context
from booking_bot.specialist_config import load_specialist_template

logger = logging.getLogger(__name__)
router = APIRouter()


class HealthResponse(BaseModel):
    status: str


def get_readiness_redis() -> Redis:
    return dispatcher.storage.redis


@router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_readiness_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    dependency = "specialist_configuration"
    try:
        template = load_specialist_template(settings.specialist_config_path)
        async with asyncio.timeout(3):
            dependency = "postgres"
            await session.execute(text("SELECT 1"))
            dependency = "specialist_context"
            context = await get_specialist_context(session)
            if (
                context.business.slug != template.profile.slug
                or context.master.business_id != context.business_id
            ):
                raise ValueError("Specialist context does not match configuration")
            dependency = "redis"
            if not await redis.ping():
                raise ConnectionError("Redis ping failed")
    except Exception:
        logger.warning("Readiness check failed: %s", dependency)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not ready",
        ) from None
    return HealthResponse(status="ready")
