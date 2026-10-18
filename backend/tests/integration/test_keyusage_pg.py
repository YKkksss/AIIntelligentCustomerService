"""M3.1 ① (integration): 鉴权只写 Redis 分桶，`aics.flush_key_usage` 批量落 last_used_at.

覆盖：成功鉴权不动数据库、beat 任务把最近分钟的桶折进 `api_keys.last_used_at`、迟到的时间戳不会把
last_used_at 往回写（greatest）、过去的桶被清理、当前分钟的桶保留（只读不删，避免与写入竞争）。
每个用例用独立前缀：桶名按分钟分片，同一分钟里相邻用例会互相污染；前缀必须以 `aics:` 开头，
否则真实 Redis 的 ACL（`~aics:*`）会拒绝，这也是本文件最初跑集成测试时暴露的问题。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from aics.db.models import ApiKey
from aics.modules.apikeys import keyusage
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService

pytestmark = pytest.mark.integration
PEPPER = "x" * 40


@pytest.fixture
def prefix() -> str:
    """`aics:` 开头（ACL 放行范围），后半段每个用例独立。"""
    return f"aics:it:{uuid4().hex[:8]}:"


async def create_key(
    pg: dict[str, Any], redis: Any, app_id: UUID, prefix: str, **kw: Any
) -> tuple[UUID, str]:
    async with pg["sessions"]() as s:
        svc = ApiKeyService(s, pepper=PEPPER, redis=redis, redis_prefix=prefix)
        row, plain = await svc.create(app_id, name="it", scopes=["chat"], **kw)
        await s.commit()
        return row.id, plain


async def read_last_used_at(pg: dict[str, Any], key_id: UUID) -> datetime | None:
    async with pg["sessions"]() as s:
        stmt = select(ApiKey.last_used_at).where(ApiKey.id == key_id)
        return (await s.execute(stmt)).scalar_one()


async def test_authenticated_call_marks_the_bucket_and_flush_persists_it(
    pg: dict[str, Any], redis_it: Any, prefix: str
) -> None:
    a = pg["app_a"]
    key_id, key = await create_key(pg, redis_it, a, prefix)
    auth = ApiKeyAuthenticator(pg["sessions"], pepper=PEPPER, redis=redis_it, redis_prefix=prefix)

    p = await auth.authenticate(f"Bearer {key}", app_id=a, any_scope=("chat",))

    assert p.key_id == key_id
    bucket = keyusage.bucket_key(prefix)
    assert str(key_id).encode() in await redis_it.hgetall(bucket)  # 只写 Redis（客户端不开 decode）
    assert await read_last_used_at(pg, key_id) is None  # 鉴权路径不写库

    out = await keyusage.flush_key_usage(pg["sessions"], redis_it, prefix=prefix)

    assert out["keys"] == 1
    ts = await read_last_used_at(pg, key_id)
    assert ts is not None and abs(time.time() - ts.timestamp()) < 120


async def test_failed_authentication_leaves_no_marker(
    pg: dict[str, Any], redis_it: Any, prefix: str
) -> None:
    from aics.core.errors import AppError

    a = pg["app_a"]
    key_id, key = await create_key(pg, redis_it, a, prefix)
    auth = ApiKeyAuthenticator(pg["sessions"], pepper=PEPPER, redis=redis_it, redis_prefix=prefix)
    wrong = key[:12] + ("A" if key[12] != "A" else "B") + key[13:]

    for bad in (wrong, "aics_" + "Z" * 32, None):
        with pytest.raises(AppError):
            await auth.authenticate(bad, app_id=a, any_scope=("chat",))  # type: ignore[arg-type]

    assert await redis_it.hgetall(keyusage.bucket_key(prefix)) == {}
    assert await read_last_used_at(pg, key_id) is None


async def test_flush_never_moves_last_used_at_backwards(
    pg: dict[str, Any], redis_it: Any, prefix: str
) -> None:
    a = pg["app_a"]
    key_id, _ = await create_key(pg, redis_it, a, prefix)
    now = time.time()
    newer = datetime.fromtimestamp(now + 3600, UTC)  # 库里已有更新的时间戳
    async with pg["sessions"]() as s:
        await s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=newer))
        await s.commit()
    past, current = keyusage.bucket_key(prefix, now - 60), keyusage.bucket_key(prefix, now)
    await redis_it.hset(past, str(key_id), str(int(now) - 30))
    await redis_it.hset(current, str(key_id), str(int(now) - 5))

    out = await keyusage.flush_key_usage(pg["sessions"], redis_it, prefix=prefix, now=now)

    assert out == {"keys": 1, "buckets": 2}
    assert await read_last_used_at(pg, key_id) == newer
    assert not await redis_it.exists(past)  # 过去的桶已清理
    assert await redis_it.exists(current)  # 当前分钟还在写入，保留
    # 再跑一次：只剩当前桶，值取最大，因此是幂等的（不会把时间戳改成更早的）
    again = await keyusage.flush_key_usage(pg["sessions"], redis_it, prefix=prefix, now=now)
    assert again == {"keys": 1, "buckets": 1}
    assert await read_last_used_at(pg, key_id) == newer
