from __future__ import annotations

import asyncio

from sqlalchemy import select

from aics.core.config import get_settings
from aics.db.engine import get_sessionmaker
from aics.db.models.app import App
from aics.db.models.user import User
from aics.modules.apikeys.service import ApiKeyService
from aics.modules.apps.service import AppService

APP_NAME = "演示智能客服"


async def main() -> None:
    s = get_settings()
    sessions = get_sessionmaker()
    async with sessions() as session:
        admin = (
            await session.execute(select(User).where(User.email == s.bootstrap_admin_email))
        ).scalar_one_or_none()
        if admin is None:
            raise SystemExit("先运行 bootstrap-admin 创建管理员")
        app = (
            await session.execute(select(App).where(App.name == APP_NAME))
        ).scalar_one_or_none()
        if app is None:
            app = await AppService(session).create_app(
                name=APP_NAME,
                description="M4.8 一键演示创建；可直接在后台改配置。",
                created_by=admin.id,
            )
            await session.commit()
        existing = await ApiKeyService(session).repo.list_for_app(app.id)
        if not existing:
            _row, plain = await ApiKeyService(session).create(
                app.id,
                name="demo",
                scopes=["chat", "kb:read", "kb:write", "handoff:write"],
                created_by=admin.id,
            )
            await session.commit()
            print(f"演示应用 {app.id} 的 API Key（只显示一次）：{plain}")
        else:
            print(f"演示应用已存在：{app.id}")


if __name__ == "__main__":
    asyncio.run(main())