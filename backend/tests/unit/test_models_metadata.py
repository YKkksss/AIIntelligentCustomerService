from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from aics.db.models import Role, User


def test_users_ddl_matches_design() -> None:
    ddl = str(CreateTable(User.__table__).compile(dialect=postgresql.dialect()))
    for col in [
        "id UUID NOT NULL",
        "email TEXT NOT NULL",
        "display_name TEXT NOT NULL",
        "password_hash TEXT NOT NULL",
        "is_super_admin BOOLEAN DEFAULT false NOT NULL",
        "status TEXT DEFAULT 'active' NOT NULL",
        "failed_logins INTEGER DEFAULT 0 NOT NULL",
        "locked_until TIMESTAMP WITH TIME ZONE",
        "last_login_at TIMESTAMP WITH TIME ZONE",
        "deleted_at TIMESTAMP WITH TIME ZONE",
    ]:
        assert col in ddl, col
    assert "status IN ('active', 'disabled')" in ddl


def test_roles_ddl() -> None:
    ddl = str(CreateTable(Role.__table__).compile(dialect=postgresql.dialect()))
    assert "id TEXT NOT NULL" in ddl and "permissions JSONB DEFAULT '[]' NOT NULL" in ddl


def test_new_id_is_uuid7() -> None:
    from aics.db.base import new_id

    a, b = new_id(), new_id()
    assert a.version == 7 and a < b
