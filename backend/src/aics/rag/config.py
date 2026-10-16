"""应用配置结构的唯一定义（模块划分 §3.7）。

- `AppRagConfig`（含 `IntentConfig`）→ `apps.rag_config`；`HandoffConfig` → `apps.handoff_config`；
  `SessionConfig` → `apps.session_config`；`ChunkConfig` → `apps.chunk_config`；`WebhookConfig` → `apps.webhook_config`。
- 读出时用这些 schema 校验并补齐默认值；未知键（如已移除的 `history_turns`）忽略。
- 单个对象内的规则由 Pydantic 校验；依赖模型注册表、平台默认值或多个对象的规则在 `validate_app_config()`。
- 本模块是纯数据定义，不访问数据库与配置单例：注册表等由调用方（apps.service）传入。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from aics.core.models import ModelRegistry

INTENTS: tuple[str, ...] = ("kb_qa", "chitchat", "human", "out_of_scope", "action", "clarify")
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 窗口校验常量（对话上下文管理 §9）
WINDOW_RATIO = 0.8
PROMPT_RESERVE_TOKENS = 1500  # 系统提示与问题预留
SUMMARY_BATCH_TOKENS = 8000  # 压缩单批溢出上限
SUMMARY_PROMPT_TOKENS = 1000

Text200 = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------------------------------------------------------------- intent
class ChitchatReplies(_Cfg):
    greeting: Annotated[str, StringConstraints(max_length=200)]
    thanks: Annotated[str, StringConstraints(max_length=200)]
    ack: Annotated[str, StringConstraints(max_length=200)]
    bye: Annotated[str, StringConstraints(max_length=200)]


class IntentConfig(_Cfg):
    """`rag_config.intent`（意图识别 §7）。"""

    enabled: bool = True
    enabled_intents: list[str] = Field(default_factory=lambda: list(INTENTS))
    chitchat_keywords: list[Annotated[str, StringConstraints(min_length=1, max_length=8)]] = Field(
        default_factory=lambda: ["你好", "您好", "在吗", "谢谢", "好的", "再见"], max_length=200
    )
    chitchat_replies: ChitchatReplies | None = None
    chitchat_max_chars: int = Field(20, ge=5, le=100)
    sensitive_keywords: list[Annotated[str, StringConstraints(min_length=1, max_length=20)]] = (
        Field(default_factory=list, max_length=1000)
    )
    sensitive_reply: Text200 = "抱歉，这个问题我无法回答。"
    out_of_scope_reply: Annotated[str, StringConstraints(max_length=200)] | None = None
    action_reply: Annotated[str, StringConstraints(max_length=200)] = (
        "这个需要人工客服帮您办理，正在为您转接。"
    )
    min_confidence: float = Field(0.6, ge=0, le=1)
    clarify_threshold: float = Field(0.7, ge=0, le=1)
    classify_timeout_ms: int = Field(2000, ge=500, le=10000)
    vector_fast_path: bool = False
    vector_threshold: float = Field(0.9, ge=0.8, le=0.99)

    @field_validator("enabled_intents")
    @classmethod
    def _intents(cls, v: list[str]) -> list[str]:
        bad = [i for i in v if i not in INTENTS]
        if bad:
            raise ValueError(f"非法意图：{', '.join(bad)}")
        if "kb_qa" not in v:
            raise ValueError("enabled_intents 必须包含 kb_qa")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _thresholds(self) -> IntentConfig:
        if self.clarify_threshold < self.min_confidence:
            raise ValueError("clarify_threshold 不能小于 min_confidence")
        return self


# ---------------------------------------------------------------- rag
class AppRagConfig(_Cfg):
    """`apps.rag_config`（数据库设计 §3.4、对话上下文管理 §9、RAG 流程设计）。"""

    vector_top_k: int = Field(20, ge=1, le=100)
    keyword_top_k: int = Field(20, ge=1, le=100)
    fuse_top_k: int = Field(30, ge=1, le=200)
    rerank_top_n: int = Field(5, ge=1, le=20)
    enable_rerank: bool = True
    # Retrieval settings (M2.2b, RAG 流程设计 §4.2): routes, fusion and vector score floor.
    retrieval_mode: Literal["hybrid", "vector", "fulltext"] = "hybrid"
    fusion: Literal["rrf", "weighted"] = "rrf"
    vector_weight: float = Field(0.7, ge=0, le=1)  # weighted only; keyword weight = 1 - this
    vector_score_threshold: float | None = Field(None, ge=-1, le=1)  # cosine; None = no floor
    # 检索 / 判定默认值的依据见 docs/06-development/09-参数默认值与依据.md
    rerank_threshold: float = Field(0.5, ge=0, le=1)
    judge_with_llm: bool = True
    judge_gray_upper: float = Field(0.8, ge=0, le=1)
    history_token_budget: int = Field(3000, ge=500, le=16000)
    compact_trigger_tokens: int = Field(6000, le=64000)
    keep_recent_turns: int = Field(4, ge=1, le=20)
    enable_facts: bool = True
    enable_history_recall: bool = False
    summary_model: str | None = None
    openai_temp_summary: bool = True
    temperature: float = Field(0.2, ge=0, le=2)
    max_tokens: int = Field(800, ge=64, le=8192)
    llm_model: str | None = None
    intent: IntentConfig = Field(default_factory=IntentConfig)

    @model_validator(mode="after")
    def _cross(self) -> AppRagConfig:
        if self.compact_trigger_tokens < self.history_token_budget:
            raise ValueError("compact_trigger_tokens 必须 ≥ history_token_budget")
        if self.judge_gray_upper < self.rerank_threshold:
            raise ValueError("judge_gray_upper 不能小于 rerank_threshold")
        return self


# ---------------------------------------------------------------- chunk
ChunkStrategy = Literal["heading", "article", "section", "faq", "clause"]
ChunkMode = Literal["general", "parent_child"]
PRESETS_FILE = Path(__file__).with_name("chunk_presets.yaml")
CUSTOM_PRESET = "custom"


@dataclass(frozen=True)
class ChunkPreset:
    name: str
    label: str
    description: str
    config: dict[str, Any]


@lru_cache(maxsize=1)
def _load_presets() -> tuple[str, dict[str, ChunkPreset]]:
    raw = yaml.safe_load(PRESETS_FILE.read_text(encoding="utf-8"))
    presets = {
        name: ChunkPreset(name, item["label"], item["description"], dict(item["config"]))
        for name, item in raw["presets"].items()
    }
    default = raw["default"]
    if default not in presets:
        raise ValueError(f"chunk_presets.yaml: default={default!r} 不存在")
    return default, presets


def chunk_presets() -> list[ChunkPreset]:
    """全部切块预设（顺序同配置文件），供管理接口 / 前端一键填充。"""
    return list(_load_presets()[1].values())


def default_chunk_preset() -> str:
    return _load_presets()[0]


def get_chunk_preset(name: str) -> ChunkPreset:
    presets = _load_presets()[1]
    if name not in presets:
        raise ValueError(f"未知切块预设：{name}（可选 {', '.join(presets)}）")
    return presets[name]


_SEP_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ChunkConfig(_Cfg):
    """`apps.chunk_config`（文档处理流水线 §3.3）；token 数按 `aics.core.tokens.count_tokens` 计。

    缺省的键按 `preset`（默认 `chunk_presets.yaml` 的 default）补齐，再由显式给出的键覆盖；
    `preset` 只是记录来源（`custom` 表示不基于预设），切块逻辑只看展开后的字段。
    """

    preset: str = Field(default_factory=lambda: default_chunk_preset())
    strategy: ChunkStrategy = "heading"
    chunk_size: int = Field(500, ge=100, le=2000)
    chunk_overlap: int = Field(80, ge=0)
    separators: list[str] = Field(default_factory=list, max_length=10)
    split_by_heading: bool = True
    keep_tables: bool = True
    mode: ChunkMode = "general"
    parent_mode: Literal["paragraph", "full_doc"] = "paragraph"
    parent_size: int = Field(1024, ge=300, le=4000)
    full_doc_max_tokens: int = Field(10000, ge=1000, le=10000)
    child_size: int = Field(200, ge=50, le=1000)
    child_overlap: int = Field(20, ge=0)
    child_separators: list[str] = Field(default_factory=list, max_length=10)
    # 预处理（设计讨论 0001 / 文档处理流水线 §3 第 2.5 步）
    normalize_whitespace: bool = True
    remove_urls_emails: bool = False
    # 向量化文本加"文档标题 + 标题路径"前缀（上下文增强，§3 第 6 步）
    contextual_prefix: bool = True

    @model_validator(mode="before")
    @classmethod
    def _fill_from_preset(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        name = data.get("preset") or default_chunk_preset()
        if name == CUSTOM_PRESET:
            return data
        try:
            preset = get_chunk_preset(name)
        except ValueError as e:
            raise ValueError(str(e)) from e
        return {**preset.config, **data, "preset": name}

    @field_validator("separators", "child_separators")
    @classmethod
    def _separators(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for sep in v:
            if not sep or len(sep) > 16:
                raise ValueError("分隔符长度须为 1–16 个字符")
            if _SEP_FORBIDDEN.search(sep):
                raise ValueError("分隔符不能包含除换行、制表符以外的控制字符")
            if not sep.strip() and "\n" not in sep:
                raise ValueError('分隔符不能只由空格组成（空行请用 "\\n\\n"）')
            if sep not in out:
                out.append(sep)
        return out  # 按字面量匹配（re.escape），不支持正则，避免 ReDoS

    @model_validator(mode="after")
    def _rules(self) -> ChunkConfig:
        if self.chunk_overlap * 2 > self.chunk_size:
            raise ValueError("chunk_overlap 不能超过 chunk_size 的一半")
        if self.preset != CUSTOM_PRESET:
            get_chunk_preset(self.preset)
        if self.mode == "parent_child":
            if self.parent_mode == "paragraph" and self.child_size >= self.parent_size:
                raise ValueError("child_size 必须小于 parent_size")
            if self.child_overlap * 2 > self.child_size:
                raise ValueError("child_overlap 不能超过 child_size 的一半")
        return self

    @property
    def context_chunk_tokens(self) -> int:
        """送入 LLM 的单个检索结果上限：父子模式为父块大小，否则为切块大小（窗口校验用）。"""
        if self.mode != "parent_child":
            return self.chunk_size
        return self.full_doc_max_tokens if self.parent_mode == "full_doc" else self.parent_size


def merge_chunk_config(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """chunk_config 部分更新（应用级与文档级共用）：patch 指定了 preset 时以该预设为底（丢弃旧值），
    否则在 current 上逐键覆盖。结果须再经 `ChunkConfig` 校验。"""
    if "preset" in patch:
        return dict(patch)
    return {**current, **patch}


# ---------------------------------------------------------------- session
IdleClose = Annotated[int, Field(ge=1, le=1440)]
NoticeBefore = Annotated[int, Field(ge=0, le=1439)]


def _check_notice(close: int, notice: int) -> None:
    if notice >= close:
        raise ValueError("idle_notice_before_minutes 必须小于 idle_close_minutes")


class SessionConfig(_Cfg):
    """`apps.session_config`：键为 null 时取平台默认（会话生命周期与转人工 §2.1）。"""

    idle_close_minutes: IdleClose | None = None
    idle_notice_before_minutes: NoticeBefore | None = None
    idle_close_notice: Text200 | None = None

    @model_validator(mode="after")
    def _pair(self) -> SessionConfig:
        if self.idle_close_minutes is not None and self.idle_notice_before_minutes is not None:
            _check_notice(self.idle_close_minutes, self.idle_notice_before_minutes)
        return self

    def resolve(self, default: EffectiveSessionConfig) -> EffectiveSessionConfig:
        """与平台默认合并得到生效值（合并后再整体校验）。"""
        merged = default.model_dump() | self.model_dump(exclude_none=True)
        return EffectiveSessionConfig.model_validate(merged)


class EffectiveSessionConfig(_Cfg):
    """生效的会话配置；也是 `system_settings.default_session_config` 的结构。"""

    idle_close_minutes: IdleClose
    idle_notice_before_minutes: NoticeBefore
    idle_close_notice: Text200

    @model_validator(mode="after")
    def _pair(self) -> EffectiveSessionConfig:
        _check_notice(self.idle_close_minutes, self.idle_notice_before_minutes)
        return self


PLATFORM_DEFAULT_SESSION = EffectiveSessionConfig(
    idle_close_minutes=10,
    idle_notice_before_minutes=1,
    idle_close_notice="如果没有其他问题，当前服务就结束咯~",
)


# ---------------------------------------------------------------- handoff
class ServiceHours(_Cfg):
    """服务时间周计划（会话生命周期与转人工 §3.7）：每天 0–N 个 [开始, 结束) 区间，缺省的星期不服务。"""

    timezone: str
    weekly: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"非法 IANA 时区：{v}") from e
        return v

    @field_validator("weekly")
    @classmethod
    def _weekly(cls, v: dict[str, list[tuple[str, str]]]) -> dict[str, list[tuple[str, str]]]:
        for day, ranges in v.items():
            if day not in WEEKDAYS:
                raise ValueError(f"非法星期键：{day}（应为 {'/'.join(WEEKDAYS)}）")
            for start, end in ranges:
                if not (_HHMM.match(start) and _HHMM.match(end)):
                    raise ValueError(f"{day} 时间须为 HH:MM：{start}-{end}")
                if start >= end:
                    raise ValueError(f"{day} 区间开始须早于结束且不跨零点：{start}-{end}")
            ordered = sorted(ranges)
            for (_, prev_end), (next_start, _) in zip(ordered, ordered[1:], strict=False):
                if next_start < prev_end:
                    raise ValueError(f"{day} 区间重叠")
        return v


class HandoffConfig(_Cfg):
    """`apps.handoff_config`（会话生命周期与转人工 §3.5；无平台默认层）。"""

    enabled: bool = True
    keywords: list[Annotated[str, StringConstraints(min_length=1, max_length=20)]] = Field(
        default_factory=lambda: ["人工", "投诉"], max_length=200
    )
    max_refusals: int = Field(2, ge=1, le=10)
    on_unanswerable: bool = False
    text: Text200 = "已为您转接人工客服"
    handoff_ai_mode: Literal["continue", "wait"] = "continue"
    wait_text: Text200 = "已为您转接人工客服，请稍候。"
    accepted_text: Text200 | None = None
    released_text: Text200 | None = None
    handoff_queue_timeout_minutes: int = Field(5, ge=1, le=120)
    queue_timeout_text: Text200 = "当前客服繁忙，您可以留下问题和联系方式，我们会尽快回复您"
    service_hours: ServiceHours | None = None
    offline_text: Text200 = (
        "当前为非服务时间，您可以留下问题和联系方式，我们会在服务时间内尽快回复您"
    )
    handoff_accepted_idle_minutes: int = Field(30, ge=1, le=1440)


# ---------------------------------------------------------------- webhook
class WebhookConfig(_Cfg):
    """`apps.webhook_config`。URL 只做格式校验；SSRF 校验（`400 URL_NOT_ALLOWED`）由 webhooks 模块在保存与投递时执行。"""

    url: Annotated[str, StringConstraints(max_length=2048, pattern=r"^https?://\S+$")] | None = None
    secret_enc: str | None = None
    secret_prefix: str | None = None

    def public(self) -> dict[str, Any]:
        """接口返回：不含加密后的密钥。"""
        return {"url": self.url, "secret_prefix": self.secret_prefix}


# ---------------------------------------------------------------- 跨对象校验
@dataclass
class ConfigIssue:
    loc: tuple[str, ...]
    msg: str


@dataclass
class ConfigValidationError(Exception):
    issues: list[ConfigIssue] = field(default_factory=list)

    def __str__(self) -> str:
        return "; ".join(f"{'.'.join(i.loc)}: {i.msg}" for i in self.issues)


def _window_issue(
    rag: AppRagConfig,
    chunk: ChunkConfig,
    doc_chunks: Sequence[ChunkConfig],
    llm_name: str,
    llm_window: int,
) -> ConfigIssue | None:
    """上下文窗口规则：按应用与各文档级覆盖中单个检索结果最大的那个计算（最坏情况）。"""
    worst = max([chunk, *doc_chunks], key=lambda c: c.context_chunk_tokens)
    need = (
        rag.history_token_budget
        + rag.max_tokens
        + rag.rerank_top_n * worst.context_chunk_tokens
        + PROMPT_RESERVE_TOKENS
    )
    limit = int(WINDOW_RATIO * llm_window)
    if need <= limit:
        return None
    unit = (
        ("full_doc_max_tokens" if worst.parent_mode == "full_doc" else "parent_size")
        if worst.mode == "parent_child"
        else "chunk_size"
    )
    where = "" if worst is chunk else "（文档级 chunk_config）"
    return ConfigIssue(
        ("rag_config",),
        f"上下文超出模型 {llm_name} 窗口 80%：history_token_budget + max_tokens + "
        f"rerank_top_n × {unit}{where} + {PROMPT_RESERVE_TOKENS} = {need} > {limit}",
    )


def check_chunk_window(
    *, rag: AppRagConfig, chunk: ChunkConfig, models: ModelRegistry, default_llm_model: str
) -> None:
    """文档级 chunk_config 保存时的窗口校验（与应用级同一规则）；不满足抛 `ConfigValidationError`。"""
    llm_name = rag.llm_model or default_llm_model
    spec = models.get(llm_name)
    if spec is None or spec.kind != "chat":
        return  # 模型问题由应用配置校验报告
    issue = _window_issue(rag, chunk, (), llm_name, spec.context_window)
    if issue is not None:
        raise ConfigValidationError([ConfigIssue(("chunk_config",), issue.msg)])


def validate_app_config(
    *,
    rag: AppRagConfig,
    chunk: ChunkConfig,
    handoff: HandoffConfig,
    session: EffectiveSessionConfig,
    models: ModelRegistry,
    default_llm_model: str,
    message_embed_enabled: bool,
    doc_chunks: Sequence[ChunkConfig] = (),
) -> None:
    """依赖模型注册表 / 平台配置 / 多个配置对象的规则；全部问题一次列出，抛 `ConfigValidationError`。"""
    issues: list[ConfigIssue] = []

    def chat_window(name: str, loc: tuple[str, ...]) -> int | None:
        spec = models.get(name)
        if spec is None or spec.kind != "chat":
            issues.append(ConfigIssue(loc, f"{name!r} 不是 AICS_MODELS 中 kind=chat 的模型"))
            return None
        return spec.context_window

    llm_name = rag.llm_model or default_llm_model
    llm_window = chat_window(llm_name, ("rag_config", "llm_model"))
    if llm_window is not None:
        issue = _window_issue(rag, chunk, doc_chunks, llm_name, llm_window)
        if issue is not None:
            issues.append(issue)

    summary_name = rag.summary_model or llm_name
    summary_window = (
        chat_window(summary_name, ("rag_config", "summary_model"))
        if rag.summary_model
        else llm_window
    )
    need_summary = SUMMARY_BATCH_TOKENS + SUMMARY_PROMPT_TOKENS
    if summary_window is not None and need_summary > WINDOW_RATIO * summary_window:
        issues.append(
            ConfigIssue(
                ("rag_config", "summary_model"),
                f"压缩模型 {summary_name} 窗口不足：需 {need_summary} ≤ 80% × {summary_window}",
            )
        )

    if rag.enable_history_recall and not message_embed_enabled:
        issues.append(
            ConfigIssue(
                ("rag_config", "enable_history_recall"),
                "需要系统开启 AICS_MESSAGE_EMBED_ENABLED",
            )
        )

    if handoff.handoff_queue_timeout_minutes >= session.idle_close_minutes:
        issues.append(
            ConfigIssue(
                ("handoff_config", "handoff_queue_timeout_minutes"),
                f"必须小于生效的 idle_close_minutes（{session.idle_close_minutes}）",
            )
        )

    if issues:
        raise ConfigValidationError(issues)
