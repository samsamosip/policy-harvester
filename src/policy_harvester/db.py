from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(schema="inha_policy", naming_convention=NAMING_CONVENTION)

settings = get_settings()
async_database_url = settings.database_url.replace(
    "postgresql+psycopg://", "postgresql+psycopg://", 1
)
engine = create_async_engine(async_database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session
