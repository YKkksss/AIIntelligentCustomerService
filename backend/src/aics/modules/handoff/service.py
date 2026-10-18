"""转人工状态机与坐席接口（M3.4 ②，会话生命周期与转人工 §3.3）。

本提交做坐席侧的四件事：读状态、抢单接入、坐席发言、交还 AI。队列超时（§3.6）、服务时间
离线（§3.7）与 `accepted` 空闲回收（§3.8）在提交 ③ 落到 `handoff.sweep` 与 `chat.service`。

三条硬规则：
1. 迁移都是**条件单行 UPDATE**，受影响 0 行 → 重读数据库里的当前状态 → 409
   `HANDOFF_STATE_CONFLICT`（`details.handoff_status`）。并发抢单因此只有一个能成功。
2. 坐席消息（`role=human_agent`）与系统提示（`handoff_accepted` / `handoff_released`）都经
   `chat.service.append_agent_message` 写入：`message_count`、`last_activity_at`、
   `idle_notice_sent_at` 的联动规则只有 `chat.repository` 一份，绕过它会让空闲扫描误判。
3. 会话已关闭 → 409 `CONVERSATION_CLOSED`，坐席接口不会自动开新会话（§3.3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import (
    ConversationNotFoundError,
    ForbiddenError,
    HandoffStateConflictError,
)
from aics.core.schedule import in_service_hours
from aics.db.models.conversation import Conversation, Message
from aics.modules.apps.service import AppService, HandoffConfig
from aics.modules.chat.repository import ConversationClosedError
from aics.modules.chat.service import append_agent_message
from aics.modules.handoff.repository import OPEN_STATUSES, HandoffRepository

log = structlog.get_logger()

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 交还 AI 的原因（`HANDOFF_END_REASONS`；坐席接口只用 `agent`，其余由提交 ③ 写）。
AGENT_END_REASON = "agent"


@dataclass(frozen=True, slots=True)
class HandoffState:
    """开放接口参考 §5 的转人工状态（GET 响应）。"""

    handoff_status: str
    handoff_reason: str | None
    requested_at: datetime | None
    accepted_at: datetime | None
    released_at: datetime | None
    handoff_end_reason: str | None
    needs_callback: bool
    agent_id: str | None
    agent_name: str | None
    handoff_ai_mode: str
    in_service_hours: bool


@dataclass(frozen=True, slots=True)
class HandoffItem:
    """`GET /handoffs` 的列表项：坐席平台拉队列用，够它决定接哪一通即可。"""

    conversation_id: UUID
    handoff_status: str
    handoff_reason: str | None
    requested_at: datetime | None
    accepted_at: datetime | None
    agent_id: str | None
    agent_name: str | None
    needs_callback: bool
    message_count: int
    last_activity_at: datetime
    external_user_id: str | None


class HandoffService:
    """坐席接口的业务规则；一个请求一个实例，session 由 runtime 注入。"""

    def __init__(self, session: AsyncSession, *, repo: HandoffRepository | None = None) -> None:
        self.session = session
        self.repo = HandoffRepository(session) if repo is None else repo

    # ------------------------------------------------------------ 读
    async def state(self, app_id: UUID, conversation_id: UUID) -> HandoffState:
        """状态查询：已关闭的会话也允许读（§3.1 关闭后 `handoff_status` 保留最后一个值）。"""
        conv = await self._load(app_id, conversation_id)
        return self._state_of(conv, await self._config(app_id))

    async def list_open(
        self,
        app_id: UUID,
        *,
        status: str = "pending",
        limit: int = DEFAULT_PAGE_SIZE,
        after: UUID | None = None,
    ) -> tuple[list[HandoffItem], bool]:
        """`pending` / `accepted` 的会话列表（游标 = 上一页最后一条的 `id`）。

        不读应用配置：Key 的 app 在鉴权阶段已确认存在且可用，这里再查一次只是多两条 SQL。
        """
        rows, more = await self.repo.list_open(app_id, status=status, limit=limit, after=after)
        return [self._item_of(c) for c in rows], more

    # ------------------------------------------------------------ 写
    async def accept(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        agent_name: str | None = None,
    ) -> HandoffState:
        """抢单接入：`pending → accepted`，随后写 `handoff_accepted` 系统提示（§3.3）。"""
        conv = await self._require_open(app_id, conversation_id)
        cfg = await self._config(app_id)
        changed = await self.repo.accept(
            app_id,
            conversation_id,
            agent_id=agent_id,
            agent_name=agent_name,
            at=datetime.now(UTC),
        )
        if not changed:
            raise HandoffStateConflictError(
                details={"handoff_status": await self.repo.status_of(app_id, conversation_id)}
            )
        if cfg.accepted_text:
            await append_agent_message(
                self.session,
                app_id,
                conversation_id,
                role="system",
                kind="handoff_accepted",
                content=cfg.accepted_text,
            )
        await self.session.commit()
        await self.session.refresh(conv)
        log.info(
            "handoff.accepted",
            app_id=str(app_id),
            conversation_id=str(conversation_id),
            agent_id=agent_id,
        )
        return self._state_of(conv, cfg)

    async def post_message(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        content: str,
    ) -> Message:
        """坐席发言：只有接入的坐席本人、且会话处于 `accepted` 时才允许（§3.3）。"""
        conv = await self._require_open(app_id, conversation_id)
        self._require_agent(conv, agent_id, expect="accepted")
        msg = await append_agent_message(
            self.session,
            app_id,
            conversation_id,
            role="human_agent",
            content=content,
            agent_id=agent_id,
        )
        await self.session.commit()
        log.info(
            "handoff.agent_message",
            app_id=str(app_id),
            conversation_id=str(conversation_id),
            agent_id=agent_id,
        )
        return msg

    async def release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        note: str | None = None,
    ) -> HandoffState:
        """交还 AI：`pending|accepted → released`，随后 AI 从下一轮开始接管（§3.3）。

        `pending`（还没人接）允许任一坐席 Key 释放——平台批量回退用得到；`accepted` 只允许
        接入的那个坐席释放，别人释放是 403。
        """
        conv = await self._require_open(app_id, conversation_id)
        cfg = await self._config(app_id)
        if conv.handoff_status == "accepted":
            self._require_agent(conv, agent_id, expect="accepted")
        elif conv.handoff_status not in OPEN_STATUSES:
            raise HandoffStateConflictError(details={"handoff_status": conv.handoff_status})
        changed = await self.repo.release(
            app_id,
            conversation_id,
            expected=OPEN_STATUSES,
            end_reason=AGENT_END_REASON,
            at=datetime.now(UTC),
        )
        if not changed:
            raise HandoffStateConflictError(
                details={"handoff_status": await self.repo.status_of(app_id, conversation_id)}
            )
        if cfg.released_text:
            await append_agent_message(
                self.session,
                app_id,
                conversation_id,
                role="system",
                kind="handoff_released",
                content=cfg.released_text,
            )
        await self.session.commit()
        await self.session.refresh(conv)
        # 交还备注只进日志：conversations 没有对应列，结构化审计留给 M4.1（ISSUE-53）。
        log.info(
            "handoff.released",
            app_id=str(app_id),
            conversation_id=str(conversation_id),
            agent_id=agent_id,
            end_reason=AGENT_END_REASON,
            note=note,
        )
        return self._state_of(conv, cfg)

    # ------------------------------------------------------------ 内部
    async def _load(self, app_id: UUID, conversation_id: UUID) -> Conversation:
        conv = await self.repo.get(app_id, conversation_id)
        if conv is None:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        return conv

    async def _require_open(self, app_id: UUID, conversation_id: UUID) -> Conversation:
        conv = await self._load(app_id, conversation_id)
        if conv.status != "active":
            raise ConversationClosedError(details={"id": str(conversation_id)})
        return conv

    async def _config(self, app_id: UUID) -> HandoffConfig:
        """读应用配置（不存在 / 已删除 → 404 `APP_NOT_FOUND`）。"""
        apps = await AppService.load(self.session)
        return (await apps.get_config(app_id)).handoff

    @staticmethod
    def _require_agent(conv: Conversation, agent_id: str, *, expect: str) -> None:
        if conv.handoff_status != expect:
            raise HandoffStateConflictError(details={"handoff_status": conv.handoff_status})
        if conv.handoff_agent_id != agent_id:
            raise ForbiddenError(details={"reason": "not_the_accepting_agent"})

    @staticmethod
    def _state_of(conv: Conversation, cfg: HandoffConfig) -> HandoffState:
        return HandoffState(
            handoff_status=conv.handoff_status,
            handoff_reason=conv.handoff_reason,
            requested_at=conv.handoff_requested_at,
            accepted_at=conv.handoff_accepted_at,
            released_at=conv.handoff_released_at,
            handoff_end_reason=conv.handoff_end_reason,
            needs_callback=conv.needs_callback,
            agent_id=conv.handoff_agent_id,
            agent_name=conv.handoff_agent_name,
            handoff_ai_mode=cfg.handoff_ai_mode,
            in_service_hours=in_service_hours(cfg.service_hours, datetime.now(UTC)),
        )

    @staticmethod
    def _item_of(conv: Conversation) -> HandoffItem:
        return HandoffItem(
            conversation_id=conv.id,
            handoff_status=conv.handoff_status,
            handoff_reason=conv.handoff_reason,
            requested_at=conv.handoff_requested_at,
            accepted_at=conv.handoff_accepted_at,
            agent_id=conv.handoff_agent_id,
            agent_name=conv.handoff_agent_name,
            needs_callback=conv.needs_callback,
            message_count=conv.message_count,
            last_activity_at=conv.last_activity_at,
            external_user_id=conv.external_user_id,
        )
