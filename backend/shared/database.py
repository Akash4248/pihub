import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from shared.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
db_url = settings.database_url

# Normalize SQLite async driver prefix if sqlite URL is passed
if db_url.startswith("sqlite://") and not db_url.startswith("sqlite+aiosqlite://"):
    db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://")

engine = create_async_engine(
    db_url,
    echo=False,
    future=True,
    pool_pre_ping=True if not db_url.startswith("sqlite") else False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db_health() -> bool:
    """Verify DB connectivity on startup."""
    try:
        async with engine.connect() as conn:
            logger.info("[DATABASE] Connection verified successfully for %s", engine.url.drivername)
            return True
    except Exception as e:
        logger.warning("[DATABASE] DB Connection warning: %s", e)
        return False
