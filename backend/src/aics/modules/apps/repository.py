"""apps、user_app_roles 的 repository（均继承 AppScopedRepository）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from aics.db.models import App, UserAppRole
from aics.db.scoped import MAX_PAGE_SIZE, AppScopedRepository


class AppRepository(AppScopedRepository[App]):
    """apps 自身即租户：`App.app_id` 是 `id` 的别名。"""

    model = App

    async def get_app(self, app_id: UUID, *, include_deleted: bool = False) -> App | None:
        stmt = self._scoped(app_id)
        if not include_deleted:
            stmt = stmt.where(App.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def save_values(self, app_id: UUID, values: dict[str, Any]) -> int:
        """按 app_id 更新列（service 负责计算 config_version 等）。"""
        return await self.update(app_id, app_id, **values)

    async def list_all_apps(
        self,
        *,
        ids: list[UUID] | None = None,
        include_deleting: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[App]:
        """跨应用列表：仅供 apps.service 按调用者权限传入 `ids`（super_admin 传 None）。"""
        stmt = select(App).where(App.deleted_at.is_(None))
        if ids is not None:
            stmt = stmt.where(App.id.in_(ids))
        if not include_deleting:
            stmt = stmt.where(App.status != "deleting")
        stmt = (
            stmt.order_by(App.created_at.desc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        return list((await self.session.execute(stmt)).scalars())


class MemberRepository(AppScopedRepository[UserAppRole]):
    """user_app_roles：复合主键 (user_id, app_id)，通用方法中的 `id` 即 user_id。"""

    model = UserAppRole

    def _where_id(self, app_id: UUID, id: UUID) -> list[Any]:
        return [UserAppRole.app_id == app_id, UserAppRole.user_id == id]

    async def get_role(self, app_id: UUID, user_id: UUID) -> str | None:
        row = await self.get(app_id, user_id)
        return row.role_id if row else None

    async def set_role(self, app_id: UUID, user_id: UUID, role_id: str) -> None:
        row = await self.get(app_id, user_id)
        if row is None:
            await self.add(app_id, UserAppRole(app_id=app_id, user_id=user_id, role_id=role_id))
        else:
            row.role_id = role_id
            await self.session.flush()

    async def count_role(self, app_id: UUID, role_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(UserAppRole)
            .where(UserAppRole.app_id == app_id, UserAppRole.role_id == role_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def roles_for_user_all_apps(self, user_id: UUID) -> dict[UUID, str]:
        """某用户在各应用的角色（登录后计算可见应用）。"""
        stmt = select(UserAppRole.app_id, UserAppRole.role_id).where(UserAppRole.user_id == user_id)
        return {a: r for a, r in (await self.session.execute(stmt)).all()}
