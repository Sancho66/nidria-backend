from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    # `pre_ping` validates the connection at checkout; `recycle=300`
    # forces a refresh after 5 min idle. Supabase (and the network
    # hops in front of it) silently kill idle TCP connections after a
    # while — without recycling, the pool hands out a dead socket and
    # the request hangs until the kernel notices. 5 min is well under
    # the typical kill threshold; the validation cost is one SELECT 1
    # per checkout. `pool_size`/`max_overflow` are SQLAlchemy defaults
    # made explicit so a future bump is a one-line change.
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=10,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
