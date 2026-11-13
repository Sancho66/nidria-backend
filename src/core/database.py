from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import Settings, get_settings
from src.core.database_pool import API_POOL_SIZE, POOL_RECYCLE_SECONDS

settings = get_settings()


def make_api_engine(config: Settings) -> AsyncEngine:
    return create_async_engine(
        config.database_url,
        echo=config.environment == "development",
        # Validate each checkout and renew aged sockets. Recycle is based on
        # connection age, not idle time; it does not release idle pool slots.
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_size=API_POOL_SIZE,
        # Queue locally instead of exhausting the shared session pool.
        max_overflow=0,
    )


engine = make_api_engine(settings)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
