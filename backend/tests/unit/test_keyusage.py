"""M3.1：Key 最近使用时间——Redis 分桶标记与批量落库（接口规范 §2、数据库设计 §3.5）。

设计要点：每次成功鉴权只写“当前 UTC 分钟”这一个桶的一个字段，因此过去的桶不可变、可以安全删除，
不需要 Lua，也不存在 HGETALL/HDEL 竞争；落库用 greatest(last_used_at, :ts)，迟到的桶不会把时间
往回写。Redis 不可用时只记日志，绝不让一个已鉴权的请求失败。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from aics.modules.apikeys import keyusage
from tests.fake_redis import FakeRedis

PREFIX = "aics:"
KEY_ID = "018f2b9c-0000-7000-8000-000000000001"
OTHER_ID = "018f2b9c-0000-7000-8000-000000000002"
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z，整分钟


class RecordingSession:
    """假的 AsyncSession：记录语句、统计提交，不真的执行 SQL。"""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    def __call__(self) -> RecordingSession:
        return self

    async def __aenter__(self) -> RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (NOW, "aics:keyuse:202701150800"),
        (NOW + 59, "aics:keyuse:202701150800"),  # 同一分钟的两次调用共用一个桶
        (NOW + 60, "aics:keyuse:202701150801"),
    ],
)
def test_bucket_key_is_the_utc_minute(ts: float, expected: str) -> None:
    assert keyusage.bucket_key(PREFIX, ts) == expected


async def test_record_use_writes_one_field_with_ttl() -> None:
    redis = FakeRedis()

    await keyusage.record_use(redis, PREFIX, UUID(KEY_ID), now=NOW)

    bucket = "aics:keyuse:202701150800"
    assert redis.data[bucket] == {KEY_ID: str(int(NOW))}
    assert redis.ttl[bucket] == keyusage.KEYUSE_BUCKET_TTL_S


async def test_record_use_swallows_redis_failure() -> None:
    # Redis 挂了也只记日志：鉴权已经成功，不能让请求失败。
    await keyusage.record_use(FakeRedis(fail=True), PREFIX, UUID(KEY_ID), now=NOW)


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        ([{KEY_ID: "100"}], {UUID(KEY_ID): 100}),
        ([{KEY_ID: "100"}, {KEY_ID: "160"}], {UUID(KEY_ID): 160}),
        ([{KEY_ID: "160"}, {KEY_ID: "100"}], {UUID(KEY_ID): 160}),  # 顺序无关，取最大
        ([{KEY_ID: "100", OTHER_ID: "90"}], {UUID(KEY_ID): 100, UUID(OTHER_ID): 90}),
        ([{"not-a-uuid": "100", KEY_ID: "abc", OTHER_ID: "5"}], {UUID(OTHER_ID): 5}),
        ([], {}),
    ],
)
def test_merge_buckets_keeps_the_newest_and_drops_garbage(
    buckets: list[dict[str, Any]], expected: dict[UUID, int]
) -> None:
    assert keyusage.merge_buckets(buckets) == expected


async def test_flush_folds_buckets_and_deletes_only_past_minutes() -> None:
    redis = FakeRedis()
    past = keyusage.bucket_key(PREFIX, NOW - 60)
    current = keyusage.bucket_key(PREFIX, NOW)
    await redis.hset(past, KEY_ID, str(int(NOW) - 30))
    await redis.hset(current, KEY_ID, str(int(NOW) + 5))
    session = RecordingSession()

    result = await keyusage.flush_key_usage(session, redis, prefix=PREFIX, now=NOW)

    assert result == {"keys": 1, "buckets": 2}
    assert current in redis.data  # 当前分钟还在写入，不能删
    assert past not in redis.data
    assert session.commits == 1
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "update api_keys" in sql
    assert "greatest(" in sql  # 迟到的时间戳不会把 last_used_at 往回写
    assert "to_timestamp(" in sql


async def test_flush_without_buckets_never_opens_a_session() -> None:
    def explode() -> Any:  # pragma: no cover - 只在回归时执行
        raise AssertionError("没有桶时不应该打开数据库会话")

    result = await keyusage.flush_key_usage(explode, FakeRedis(), prefix=PREFIX, now=NOW)

    assert result == {"keys": 0, "buckets": 0}


async def test_flush_ignores_a_broken_redis() -> None:
    def explode() -> Any:  # pragma: no cover - 只在回归时执行
        raise AssertionError("Redis 读失败时不应该打开数据库会话")

    result = await keyusage.flush_key_usage(explode, FakeRedis(fail=True), prefix=PREFIX, now=NOW)

    assert result == {"keys": 0, "buckets": 0}


async def test_flush_reads_only_the_configured_key_prefix() -> None:
    redis = FakeRedis()
    await redis.hset(keyusage.bucket_key("other:", NOW), KEY_ID, str(int(NOW)))
    session = RecordingSession()

    result = await keyusage.flush_key_usage(session, redis, prefix=PREFIX, now=NOW)

    assert result == {"keys": 0, "buckets": 0}
    assert session.statements == []
