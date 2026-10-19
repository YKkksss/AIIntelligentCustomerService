"""apps.service：应用创建、读取与配置校验（M1.2）。

配置结构只来自 `aics.rag.config`；本模块负责持久化、默认值补齐、跨对象校验，
以及 `config_version` / `chunk_config_version` 的维护（数据库设计 §3.4）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core import netguard, secretbox
from aics.core.config import Settings, get_settings
from aics.core.errors import AppNotFoundError, ValidationAppError
from aics.db.models import App, Document
from aics.modules.apps.repository import AppRepository
from aics.modules.apps.system_settings import SystemSettingsService
from aics.rag.config import (
    PLATFORM_DEFAULT_SESSION,
    AppRagConfig,
    ChunkConfig,
    ConfigValidationError,
    EffectiveSessionConfig,
    HandoffConfig,
    SessionConfig,
    WebhookConfig,
    chunk_presets,
    merge_chunk_config,
    validate_app_config,
)

CONFIG_FIELDS = ("rag_config", "handoff_config", "session_config", "chunk_config")
BASIC_FIELDS = (
    "name",
    "description",
    "greeting",
    "system_prompt",
    "answer_style",
    "fallback_text",
    "language",
)
ANSWER_STYLES = ("concise", "detailed", "custom")
WEBHOOK_EDITABLE = {"url"}  # 签名密钥只能经 /webhook/secret 生成


def list_chunk_presets() -> list[dict[str, Any]]:
    """切块预设列表（`GET /admin/chunk-presets`，供前端一键填充 chunk_config）。

    `config` 为展开后的完整 chunk_config（含未在预设中出现、取默认值的键），前端可直接回填后再让用户修改。
    """
    return [
        {
            "name": p.name,
            "label": p.label,
            "description": p.description,
            "config": ChunkConfig.model_validate({"preset": p.name}).model_dump(mode="json"),
        }
        for p in chunk_presets()
    ]


_merge_chunk = merge_chunk_config


@dataclass(frozen=True)
class AppConfig:
    """读出并补齐默认值后的应用配置。"""

    rag: AppRagConfig
    handoff: HandoffConfig
    session: SessionConfig
    effective_session: EffectiveSessionConfig
    chunk: ChunkConfig
    webhook: WebhookConfig


@dataclass(frozen=True)
class UpdateResult:
    app: App
    config: AppConfig
    changed: bool
    reindex_required: bool  # chunk_config 变化：chunk_config_version +1，需手动重建
    custom_dict_changed: bool  # 调用方投递 retokenize_app（M1.5/M1.6）


def _dump(m: BaseModel) -> dict[str, Any]:
    return m.model_dump(mode="json")


def _errors_from_pydantic(prefix: str, e: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": [prefix, *map(str, err["loc"])], "msg": err["msg"]} for err in e.errors()]


def _merge(current: Mapping[str, Any], patch: Mapping[str, Any], nested: tuple[str, ...]) -> dict:
    """部分更新：顶层逐键覆盖；`nested` 中的键（如 rag_config.intent）再合并一层。"""
    out = dict(current)
    for k, v in patch.items():
        if k in nested and isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


class AppService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        platform_session: EffectiveSessionConfig = PLATFORM_DEFAULT_SESSION,
    ) -> None:
        # platform_session: from system_settings.default_session_config since M2.1;
        # use `AppService.load(session)` to get an instance with the stored platform default.
        self.session = session
        self.repo = AppRepository(session)
        self.settings = settings or get_settings()
        self.platform_session = platform_session

    @classmethod
    async def load(cls, session: AsyncSession, *, settings: Settings | None = None) -> AppService:
        """Instance whose platform session default comes from `system_settings` (M2.1)."""
        platform = await SystemSettingsService(session).get_default_session_config()
        return cls(session, settings=settings, platform_session=platform)

    # ------------------------------------------------------------ 校验
    def build_config(
        self,
        *,
        rag_config: Mapping[str, Any] | None,
        handoff_config: Mapping[str, Any] | None,
        session_config: Mapping[str, Any] | None,
        chunk_config: Mapping[str, Any] | None,
        webhook_config: Mapping[str, Any] | None = None,
        doc_chunks: tuple[ChunkConfig, ...] = (),
    ) -> AppConfig:
        """校验并补齐默认值；任何问题抛 `422 VALIDATION_ERROR`（`details.errors` 一次列全）。"""
        errors: list[dict[str, Any]] = []
        parsed: dict[str, Any] = {}
        for name, schema, raw in (
            ("rag_config", AppRagConfig, rag_config),
            ("handoff_config", HandoffConfig, handoff_config),
            ("session_config", SessionConfig, session_config),
            ("chunk_config", ChunkConfig, chunk_config),
            ("webhook_config", WebhookConfig, webhook_config),
        ):
            try:
                parsed[name] = schema.model_validate(dict(raw or {}))
            except ValidationError as e:
                errors.extend(_errors_from_pydantic(name, e))
        effective: EffectiveSessionConfig | None = None
        if "session_config" in parsed:
            try:
                effective = parsed["session_config"].resolve(self.platform_session)
            except ValidationError as e:
                errors.extend(_errors_from_pydantic("session_config", e))
        if errors:
            raise ValidationAppError(message="应用配置校验失败", details={"errors": errors})
        if effective is None:  # pragma: no cover - 无错误时必然已解析
            raise RuntimeError("session_config 未解析")
        try:
            validate_app_config(
                rag=parsed["rag_config"],
                chunk=parsed["chunk_config"],
                handoff=parsed["handoff_config"],
                session=effective,
                models=self.settings.models,
                default_llm_model=self.settings.llm_model,
                message_embed_enabled=self.settings.message_embed_enabled,
                doc_chunks=doc_chunks,
            )
        except ConfigValidationError as e:
            raise ValidationAppError(
                message="应用配置校验失败",
                details={"errors": [{"loc": list(i.loc), "msg": i.msg} for i in e.issues]},
            ) from e
        return AppConfig(
            rag=parsed["rag_config"],
            handoff=parsed["handoff_config"],
            session=parsed["session_config"],
            effective_session=effective,
            chunk=parsed["chunk_config"],
            webhook=parsed["webhook_config"],
        )

    def config_of(self, app: App) -> AppConfig:
        return self.build_config(
            rag_config=app.rag_config,
            handoff_config=app.handoff_config,
            session_config=app.session_config,
            chunk_config=app.chunk_config,
            webhook_config=app.webhook_config,
        )

    @staticmethod
    def _check_basic(values: Mapping[str, Any]) -> None:
        errors = []
        if "name" in values and not (isinstance(values["name"], str) and values["name"].strip()):
            errors.append({"loc": ["name"], "msg": "名称不能为空"})
        if "answer_style" in values and values["answer_style"] not in ANSWER_STYLES:
            errors.append({"loc": ["answer_style"], "msg": f"应为 {'/'.join(ANSWER_STYLES)}"})
        if "fallback_text" in values and not values["fallback_text"]:
            errors.append({"loc": ["fallback_text"], "msg": "兜底话术不能为空"})
        if errors:
            raise ValidationAppError(details={"errors": errors})

    # ------------------------------------------------------------ 创建 / 读取
    async def create_app(
        self,
        *,
        name: str,
        created_by: UUID | None,
        rag_config: Mapping[str, Any] | None = None,
        handoff_config: Mapping[str, Any] | None = None,
        session_config: Mapping[str, Any] | None = None,
        chunk_config: Mapping[str, Any] | None = None,
        custom_dict: list[str] | None = None,
        **basic: Any,
    ) -> App:
        unknown = set(basic) - set(BASIC_FIELDS)
        if unknown:
            raise ValidationAppError(
                details={"errors": [{"loc": [k], "msg": "未知字段"} for k in sorted(unknown)]}
            )
        self._check_basic({"name": name, **basic})
        cfg = self.build_config(
            rag_config=rag_config,
            handoff_config=handoff_config,
            session_config=session_config,
            chunk_config=chunk_config,
        )
        app = App(
            name=name.strip(),
            created_by=created_by,
            rag_config=_dump(cfg.rag),
            handoff_config=_dump(cfg.handoff),
            session_config=_dump(cfg.session),
            chunk_config=_dump(cfg.chunk),
            webhook_config=_dump(cfg.webhook),
            custom_dict=list(dict.fromkeys(custom_dict or [])),
            **{k: v for k, v in basic.items() if v is not None},
        )
        await self.repo.add(app.id, app)
        await self.session.refresh(app)
        return app

    async def get_app(self, app_id: UUID, *, include_deleting: bool = False) -> App:
        """不存在、已软删除或（默认）删除中 → `404 APP_NOT_FOUND`。"""
        app = await self.repo.get_app(app_id)
        if app is None or (app.status == "deleting" and not include_deleting):
            raise AppNotFoundError()
        return app

    async def get_config(self, app_id: UUID) -> AppConfig:
        return self.config_of(await self.get_app(app_id))

    async def list_apps(
        self, *, ids: list[UUID] | None, include_deleting: bool = False
    ) -> list[App]:
        return await self.repo.list_all_apps(ids=ids, include_deleting=include_deleting)

    # ------------------------------------------------------------ 更新
    async def update_app(self, app_id: UUID, patch: Mapping[str, Any]) -> UpdateResult:
        """部分更新（PATCH /apps/{id}）。配置合并后整体校验；有变化时 config_version +1。"""
        allowed = set(BASIC_FIELDS) | set(CONFIG_FIELDS) | {"custom_dict", "webhook_config"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValidationAppError(
                details={"errors": [{"loc": [k], "msg": "不可修改的字段"} for k in sorted(unknown)]}
            )
        webhook_patch = patch.get("webhook_config") or {}
        if set(webhook_patch) - WEBHOOK_EDITABLE:
            raise ValidationAppError(
                details={
                    "errors": [
                        {
                            "loc": ["webhook_config"],
                            "msg": "只能修改 url；签名密钥请使用生成密钥接口",
                        }
                    ]
                }
            )
        basic = {k: patch[k] for k in BASIC_FIELDS if k in patch}
        self._check_basic(basic)

        app = await self.get_app(app_id)
        merged = {
            "rag_config": _merge(app.rag_config, patch.get("rag_config") or {}, ("intent",)),
            "handoff_config": _merge(app.handoff_config, patch.get("handoff_config") or {}, ()),
            "session_config": _merge(app.session_config, patch.get("session_config") or {}, ()),
            "chunk_config": _merge_chunk(app.chunk_config, patch.get("chunk_config") or {}),
            "webhook_config": _merge(app.webhook_config, webhook_patch, ()),
        }
        doc_chunks: tuple[ChunkConfig, ...] = ()
        if patch.get("rag_config"):  # 窗口规则要覆盖文档级 chunk_config（最坏情况）
            rows = await self.session.execute(
                select(Document.chunk_config).where(
                    Document.app_id == app_id,
                    Document.chunk_config.is_not(None),
                    Document.deleted_at.is_(None),
                )
            )
            doc_chunks = tuple(ChunkConfig.model_validate(c) for c in rows.scalars())
        cfg = self.build_config(**merged, doc_chunks=doc_chunks)
        if "url" in webhook_patch and cfg.webhook.url:
            # 保存期 SSRF 校验（安全设计 §4.1：与 URL 导入同规则）；投递前 webhooks 会再校验一次
            netguard.check_url(
                cfg.webhook.url, netguard.Allowlist.parse(self.settings.url_fetch_allowlist)
            )
        new_values: dict[str, Any] = {
            "rag_config": _dump(cfg.rag),
            "handoff_config": _dump(cfg.handoff),
            "session_config": _dump(cfg.session),
            "chunk_config": _dump(cfg.chunk),
            "webhook_config": _dump(cfg.webhook),
            **basic,
        }
        if "custom_dict" in patch:
            new_values["custom_dict"] = list(dict.fromkeys(patch["custom_dict"] or []))

        old_chunk = ChunkConfig.model_validate(app.chunk_config)
        changes = {k: v for k, v in new_values.items() if getattr(app, k) != v}
        reindex = cfg.chunk != old_chunk
        dict_changed = "custom_dict" in changes
        if changes:
            changes["config_version"] = app.config_version + 1
            if reindex:
                changes["chunk_config_version"] = app.chunk_config_version + 1
            await self.repo.save_values(app_id, changes)
            await self.session.refresh(app)
        return UpdateResult(
            app=app,
            config=cfg,
            changed=bool(changes),
            reindex_required=reindex,
            custom_dict_changed=dict_changed,
        )

    # ------------------------------------------------------------ Webhook 密钥
    async def rotate_webhook_secret(self, app_id: UUID) -> str:
        """生成（或轮换）Webhook 签名密钥，**明文只在这里返回一次**（安全设计 §4.1）。

        库里只留 `secret_enc`（Fernet 密文，主密钥 `AICS_DATA_ENCRYPTION_KEY`）与
        `secret_prefix`（日志 / 接口只出现前缀）。加密工具在 `core.secretbox`：`apps` 与
        `webhooks` 是互不依赖的独立模块，谁也不能 import 谁。
        """
        app = await self.get_app(app_id)
        plain = secretbox.new_secret()
        encrypted = secretbox.encrypt(plain, self.settings.data_encryption_key.get_secret_value())
        cfg = WebhookConfig.model_validate(dict(app.webhook_config or {}))
        values = cfg.model_dump()
        values.update(secret_enc=encrypted, secret_prefix=secretbox.prefix_of(plain))
        await self.repo.save_values(
            app_id, {"webhook_config": _dump(WebhookConfig.model_validate(values))}
        )
        await self.session.refresh(app)
        return plain
