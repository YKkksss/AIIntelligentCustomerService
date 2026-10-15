"""异步 SQLAlchemy 引擎与会话工厂（单进程单例，惰性创建）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aics.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        kwargs: dict[str, object] = {"pool_pre_ping": True}
        if s.db_url.startswith("postgresql"):
            # 共享 PG：控制总连接数（DB_POOL_SIZE + DB_MAX_OVERFLOW）
            kwargs.update(pool_size=s.db_pool_size, max_overflow=s.db_max_overflow)
        _engine = create_async_engine(s.db_url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _sessionmaker is None:  # pragma: no cover - get_engine 已创建
        raise RuntimeError("sessionmaker 未初始化")
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖 / 任务中使用：异常回滚，正常结束不自动提交（由 service 决定）。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine, _sessionmaker = None, None
