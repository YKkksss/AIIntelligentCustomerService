"""全局配置（pydantic-settings，前缀 AICS_）。

- 优先级：环境变量 > .env > 默认值；启动时校验，缺必填项直接失败。
- 业务代码只通过 `get_settings()`（或 `aics.core.config.settings`）读取，不直接读 os.environ。
- 变量清单见 docs/06-development/05-配置与环境变量.md。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from aics.core.models import ModelRegistry

# 写死在代码中的约定（修改需同步文档与 ADR-0003）
CELERY_QUEUES: tuple[str, ...] = ("ingest", "context", "eval", "maintenance")
TOKEN_ENCODING = "cl100k_base"  # noqa: S105 - 编码名，不是密码


def _split_csv(v: Any) -> Any:
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AICS_",
        # 后面的文件优先：backend/.env 覆盖仓库根目录 .env
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 应用 ----
    env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False
    api_host: str = "0.0.0.0"  # noqa: S104 - 服务监听地址，生产由 Nginx 前置
    api_port: int = 8000
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # ---- 安全 ----
    jwt_secret: SecretStr
    jwt_access_ttl_min: int = Field(30, gt=0)
    jwt_refresh_ttl_days: int = Field(7, gt=0)
    api_key_pepper: SecretStr
    data_encryption_key: SecretStr
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # ---- 基础设施 ----
    db_url: str
    db_pool_size: int = Field(10, gt=0)
    db_max_overflow: int = Field(10, ge=0)
    redis_url: str
    redis_prefix: str = "aics:"
    celery_broker_url: str
    celery_result_backend: str | None = None
    qdrant_url: str
    qdrant_api_key: SecretStr
    # 服务器上共用 KnowledgeFlow 的 Qdrant：AICS 只用此前缀的集合（集成测试用 aics_test_）
    qdrant_collection_prefix: str = Field("aics_", pattern=r"^aics_[a-z0-9_]*$")

    # ---- 存储与导入 ----
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./storage"
    upload_max_mb: int = Field(50, gt=0)
    url_fetch_allowlist: Annotated[list[str], NoDecode] = []

    # ---- 保留 ----
    message_retention_days: int = Field(180, gt=0)
    trace_retention_days: int = Field(30, gt=0)
    message_embed_enabled: bool = False

    # ---- 模型 ----
    llm_provider: Literal["openai_compatible"] = "openai_compatible"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr
    llm_model: str = "deepseek-flash"
    llm_timeout_s: float = Field(60, gt=0)

    models: ModelRegistry

    embed_provider: Literal["dashscope", "openai_compatible"] = "dashscope"
    embed_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embed_api_key: SecretStr
    embed_model: str = "qwen3.7-text-embedding-flash"
    embed_dim: int = Field(1024, gt=0)
    embed_batch: int = Field(10, gt=0)

    rerank_provider: Literal["dashscope", "llamacpp", "none"] = "dashscope"
    rerank_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    rerank_api_key: SecretStr | None = None
    rerank_model: str = "qwen3.7-text-rerank"

    @field_validator("cors_origins", "url_fetch_allowlist", mode="before")
    @classmethod
    def _csv(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_len(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 64:
            raise ValueError("AICS_JWT_SECRET 至少 64 个字符")
        return v

    @field_validator("api_key_pepper")
    @classmethod
    def _pepper_len(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("AICS_API_KEY_PEPPER 至少 32 个字符")
        return v

    @field_validator("db_url")
    @classmethod
    def _db_async(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("AICS_DB_URL 必须使用 postgresql+asyncpg:// 驱动")
        return v

    @model_validator(mode="after")
    def _check_models(self) -> Settings:
        """默认模型必须在注册表中且类型匹配，否则启动失败。"""
        self.models.require(self.llm_model, "chat", field="AICS_LLM_MODEL")
        self.models.require(self.embed_model, "embedding", field="AICS_EMBED_MODEL")
        if self.rerank_provider != "none":
            self.models.require(self.rerank_model, "rerank", field="AICS_RERANK_MODEL")
            if self.rerank_api_key is None and self.rerank_provider == "dashscope":
                raise ValueError("AICS_RERANK_PROVIDER=dashscope 时必须设置 AICS_RERANK_API_KEY")
        return self

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # 必填项来自环境变量


def __getattr__(name: str) -> Any:
    # 允许 `from aics.core.config import settings`，同时保持惰性加载（测试可先设环境变量）
    if name == "settings":
        return get_settings()
    raise AttributeError(name)
