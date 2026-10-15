"""模型注册表（AICS_MODELS）。

应用可选模型、80% 上下文窗口校验、/admin/models 都以此为准；见 docs/02-architecture/05-模型供应商抽象.md §1.1。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

ModelKind = Literal["chat", "embedding", "rerank"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    kind: ModelKind
    context_window: int = Field(gt=0, description="上下文窗口（token）")
    price_in: float = Field(ge=0, description="元 / 百万 token，仅用于估算展示")
    price_out: float = Field(ge=0, description="元 / 百万 token，仅用于估算展示")


class ModelRegistry(RootModel[list[ModelSpec]]):
    """注册表：名称唯一；按名称查询。"""

    @model_validator(mode="after")
    def _unique_names(self) -> ModelRegistry:
        names = [m.name for m in self.root]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"AICS_MODELS 中模型名重复：{', '.join(dup)}")
        if not self.root:
            raise ValueError("AICS_MODELS 不能为空")
        return self

    def get(self, name: str) -> ModelSpec | None:
        return next((m for m in self.root if m.name == name), None)

    def by_kind(self, kind: ModelKind) -> list[ModelSpec]:
        return [m for m in self.root if m.kind == kind]

    def require(self, name: str, kind: ModelKind, *, field: str) -> ModelSpec:
        """校验 `name` 已注册且类型为 `kind`，否则抛 ValueError（启动失败）。"""
        spec = self.get(name)
        if spec is None:
            raise ValueError(f"{field}={name!r} 不在 AICS_MODELS 注册表中")
        if spec.kind != kind:
            raise ValueError(f"{field}={name!r} 在注册表中的 kind 为 {spec.kind}，应为 {kind}")
        return spec

    def public(self) -> list[dict[str, object]]:
        """对外展示用（不含任何密钥，本身也不含密钥）。"""
        return [m.model_dump() for m in self.root]
