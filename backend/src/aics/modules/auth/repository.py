"""`modules.auth` 的数据访问：全部是全局表，没有 `app_id` 作用域，所以不用 `AppScopedRepository`。

为什么直接读 `db.models` 而不调 `modules.apps`：import-linter 的 independence 合同规定
`auth` 与 `apps` 互不依赖（ADR-0017），认证需要在登录响应里带上「这个用户能看到哪些应用」，
只能自己 join `user_app_roles` / `apps`。读表、不写别人的表：应用的创建与配置仍只走 `apps`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.models.app import App, UserAppRole
from aics.db.models.user import Role, User


class AuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def user_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def user(self, user_id: UUID) -> User | None:
        result = await self.session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def touch_login(self, user: User, *, at: datetime, reset: bool) -> None:
        user.last_login_at = at
        if reset:
            user.failed_logins = 0
            user.locked_until = None

    async def record_failure(self, user: User, *, at: datetime, lock_minutes: int) -> int:
        """Increment the failure counter; returns the new count (locks when it hits the cap)."""
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= 5:
            from datetime import timedelta

            user.locked_until = at + timedelta(minutes=lock_minutes)
        return user.failed_logins

    async def bump_token_version(self, user: User) -> int:
        user.token_version = (user.token_version or 0) + 1
        return user.token_version

    async def memberships(self, user_id: UUID) -> list[tuple[UserAppRole, App, Role]]:
        result = await self.session.execute(
            select(UserAppRole, App, Role)
            .join(App, App.id == UserAppRole.app_id)
            .join(Role, Role.id == UserAppRole.role_id)
            .where(UserAppRole.user_id == user_id, App.deleted_at.is_(None))
            .order_by(App.name)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def membership(self, user_id: UUID, app_id: UUID) -> UserAppRole | None:
        result = await self.session.execute(
            select(UserAppRole).where(UserAppRole.user_id == user_id, UserAppRole.app_id == app_id)
        )
        return result.scalar_one_or_none()

    async def role(self, role_id: str) -> Role | None:
        result = await self.session.execute(select(Role).where(Role.id == role_id))
        return result.scalar_one_or_none()

    async def app(self, app_id: UUID) -> App | None:
        result = await self.session.execute(
            select(App).where(App.id == app_id, App.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def create_user(self, *, email: str, display_name: str, password_hash: str) -> User:
        user = User(
            email=email.lower(),
            display_name=display_name,
            password_hash=password_hash,
            must_change_password=True,
        )
        self.session.add(user)
        return user

    async def users(self, *, q: str | None = None, limit: int = 100) -> list[User]:
        stmt = select(User).where(User.deleted_at.is_(None))
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(or_(User.email.ilike(like), User.display_name.ilike(like)))
        result = await self.session.execute(stmt.order_by(User.created_at.desc()).limit(limit))
        return list(result.scalars().all())

    async def members(self, app_id: UUID) -> list[tuple[UserAppRole, User, Role]]:
        result = await self.session.execute(
            select(UserAppRole, User, Role)
            .join(User, User.id == UserAppRole.user_id)
            .join(Role, Role.id == UserAppRole.role_id)
            .where(UserAppRole.app_id == app_id, User.deleted_at.is_(None))
            .order_by(User.email)
        )
        return [tuple(row) for row in result.all()]  # type: ignore[misc]

    async def set_user_values(self, user: User, values: dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(user, key, value)

    async def set_membership(self, *, user_id: UUID, app_id: UUID, role_id: str) -> UserAppRole:
        row = await self.membership(user_id, app_id)
        if row is None:
            row = UserAppRole(user_id=user_id, app_id=app_id, role_id=role_id)
            self.session.add(row)
        else:
            row.role_id = role_id
        return row

    async def delete_membership(self, *, user_id: UUID, app_id: UUID) -> bool:
        row = await self.membership(user_id, app_id)
        if row is None:
            return False
        await self.session.delete(row)
        return True
