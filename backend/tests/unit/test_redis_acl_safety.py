"""共享 Redis 的 ACL 不允许 @admin / @dangerous 命令（见服务器准备 §3）：源码中不得出现这些调用。"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "aics"
FORBIDDEN = re.compile(
    r"\.(keys|flushdb|flushall|info|config_get|config_set|debug_object|monitor|shutdown|"
    r"slaveof|replicaof|save|bgsave|client_kill|client_list)\s*\(",
    re.IGNORECASE,
)
LOGGING = re.compile(r"\b(log|logger)\.info\s*\(")


def test_no_forbidden_redis_commands() -> None:
    offenders = []
    for p in SRC.rglob("*.py"):
        for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
            # structlog's `log.info(` is not the Redis INFO command
            if LOGGING.search(line):
                continue
            if FORBIDDEN.search(line) and "redis" in p.read_text("utf-8").lower():
                offenders.append(f"{p.relative_to(SRC)}:{i}: {line.strip()}")
    assert offenders == []


def test_redis_prefix_setting() -> None:
    from aics.core.config import get_settings

    assert get_settings().redis_prefix == "aics:"
