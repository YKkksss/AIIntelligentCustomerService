import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aics.db.base import new_id
from aics.db.scoped import AppScopedRepository, registered_repositories, unscoped_methods


class _TBase(DeclarativeBase):
    pass


class Item(_TBase):
    __tablename__ = "items"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(Text)


class ItemRepository(AppScopedRepository[Item]):
    model = Item

    async def by_title(self, app_id: uuid.UUID, title: str) -> list[Item]:
        stmt = self._scoped(app_id).where(Item.title == title)
        return list((await self.session.execute(stmt)).scalars())

    async def count_all_apps(self) -> int:  # 显式命名的跨应用方法允许
        return 0


A, B = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_TBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def test_isolation(session: AsyncSession) -> None:
    repo = ItemRepository(session)
    a1 = await repo.add(A, Item(app_id=A, title="t"))
    await repo.add(B, Item(app_id=B, title="t"))

    assert await repo.get(A, a1.id) is not None
    assert await repo.get(B, a1.id) is None  # 别的应用拿不到
    assert [i.app_id for i in await repo.list(A)] == [A]
    assert [i.app_id for i in await repo.by_title(B, "t")] == [B]
    assert await repo.count(A) == 1

    assert await repo.update(B, a1.id, title="hack") == 0
    assert await repo.update(A, a1.id, title="new") == 1
    assert await repo.delete(B, a1.id) == 0
    assert await repo.delete(A, a1.id) == 1
    assert (await session.execute(select(Item).where(Item.app_id == B))).scalars().one()


async def test_list_filters_and_page(session: AsyncSession) -> None:
    repo = ItemRepository(session)
    for i in range(5):
        await repo.add(A, Item(app_id=A, title=f"t{i % 2}"))
    assert len(await repo.list(A, filters={"title": "t0"})) == 3
    assert len(await repo.list(A, limit=2)) == 2
    with pytest.raises(ValueError):
        await repo.list(A, filters={"app_id": B})


async def test_guards(session: AsyncSession) -> None:
    repo = ItemRepository(session)
    with pytest.raises(ValueError):
        await repo.add(A, Item(app_id=B, title="x"))
    with pytest.raises(ValueError):
        await repo.update(A, uuid.uuid4(), app_id=B)


def test_model_without_app_id_rejected() -> None:
    class NoApp(_TBase):
        __tablename__ = "noapp"
        id: Mapped[int] = mapped_column(primary_key=True)

    with pytest.raises(TypeError, match="app_id"):

        class Bad(AppScopedRepository[NoApp]):  # type: ignore[type-var]
            model = NoApp


def test_signature_check_detects_violation() -> None:
    class Leaky(AppScopedRepository[Item]):
        model = Item

        async def find(self, title: str) -> None: ...

    assert unscoped_methods(Leaky) == ["find"]


def test_all_registered_repositories_are_scoped() -> None:
    """所有 AppScopedRepository 子类（含测试外的真实 repository）方法签名第一个参数为 app_id。"""
    import aics.db.models  # noqa: F401

    for cls in registered_repositories():
        if cls.__qualname__.startswith("test_signature_check"):
            continue
        assert unscoped_methods(cls) == [], cls
