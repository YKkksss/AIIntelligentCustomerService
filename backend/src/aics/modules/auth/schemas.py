"""管理后台认证的请求 / 响应模型（管理接口参考 §认证，M4.1 ②）。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Email = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=320)
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=200)]
Token = Annotated[str, StringConstraints(min_length=20, max_length=4096)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    email: Email
    password: Password


class RefreshBody(_Body):
    refresh_token: Token


class LogoutBody(_Body):
    refresh_token: Token | None = None


class ChangePasswordBody(_Body):
    current_password: Password
    new_password: Password


class UserBody(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_super_admin: bool
    must_change_password: bool = False
    status: Literal["active", "disabled"] = "active"


class UserListBody(BaseModel):
    items: list[UserBody] = Field(default_factory=list)


class UserCreateBody(_Body):
    email: Email
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    password: Password
    is_super_admin: bool = False


class UserPatchBody(_Body):
    display_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None
    ) = None
    status: Literal["active", "disabled"] | None = None
    is_super_admin: bool | None = None
    password: Password | None = None


class MemberBody(_Body):
    role_id: Literal["app_admin", "viewer"]


class MemberRow(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    role_id: Literal["app_admin", "viewer"]
    status: Literal["active", "disabled"] = "active"


class MemberListBody(BaseModel):
    items: list[MemberRow] = Field(default_factory=list)


class AppMembership(BaseModel):
    app_id: UUID
    app_name: str
    role_id: Literal["app_admin", "viewer"]
    permissions: list[str] = Field(default_factory=list)


class SessionBody(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int
    user: UserBody
    apps: list[AppMembership] = Field(default_factory=list)


class MeBody(BaseModel):
    user: UserBody
    apps: list[AppMembership] = Field(default_factory=list)


class TokenBody(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int
