"""ORM 模型；Alembic 通过导入本包拿到完整 metadata。"""

from aics.db.models.user import Role, User

__all__ = ["Role", "User"]
