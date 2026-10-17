"""M3.2 ③：写接口幂等键（`core.idempotency`）——占位、回放与故障放行（接口规范 §7 / 设计讨论 0009 D9）。

纯 Redis 行为，用 `tests.fake_redis.FakeRedis` 跑；路由层接线由 `tests/unit/test_knowledge_api.py`
覆盖，真实 Redis 的并发行为在服务器复验清单里。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from aics.core.idempotency import (
    DONE,
    MAX_KEY_CHARS,
    MAX_REPLAY_BYTES,
    PROCESSING,
    TTL_S,
    Idempotency,
    IdempotencyBusyError,
    IdempotencyConflictError,
    digest_bytes,
    digest_json,
    digest_upload,
)
from tests.fake_redis import FakeRedis


def _idem(redis: object, *, prefix: str = "aics:") -> Idempotency:
    return Idempotency(redis, prefix=prefix)


def _circular() -> dict:
    body: dict = {}
    body["self"] = body  # json.dumps 会报 ValueError("Circular reference detected")
    return body


def test_cache_key_is_namespaced_by_key_id_and_truncated() -> None:
    key_id = uuid4()
    idem = _idem(FakeRedis())

    assert idem.cache_key(key_id, "  abc  ") == f"aics:idem:{key_id}:abc"
    long = idem.cache_key(key_id, "x" * 500)
    assert long == f"aics:idem:{key_id}:" + "x" * MAX_KEY_CHARS


async def test_without_a_key_nothing_is_written() -> None:
    redis = FakeRedis()
    async with _idem(redis).lock(key_id=uuid4(), idem=None, digest="d1") as guard:
        assert guard.replay is None
        await guard.record(202, {"id": "x"})
    assert redis.data == {}


async def test_placeholder_carries_sha_and_ttl() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        assert guard.replay is None
        key = f"aics:idem:{key_id}:k1"
        assert PROCESSING in redis.data[key] and '"sha":"d1"' in redis.data[key]
        assert redis.ttl[key] == TTL_S


async def test_second_request_while_processing_is_conflict() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    first = _idem(redis)
    other = _idem(redis)  # 另一个进程 / 另一个连接

    async with first.lock(key_id=key_id, idem="k1", digest="d1"):
        with pytest.raises(IdempotencyBusyError) as err:
            async with other.lock(key_id=key_id, idem="k1", digest="d1"):
                pass
    assert (err.value.code, err.value.status) == ("CONFLICT", 409)


async def test_done_record_replays_status_and_body() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        await guard.record(202, {"id": "doc-1"})

    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        assert guard.replay is not None
        assert (guard.replay.status, guard.replay.body) == (202, {"id": "doc-1"})


async def test_same_key_with_other_body_is_conflict() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        await guard.record(200, {"unchanged": True})

    with pytest.raises(IdempotencyConflictError) as err:
        async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d2"):
            pass
    assert (err.value.code, err.value.status) == ("IDEMPOTENCY_CONFLICT", 409)


async def test_failure_releases_the_placeholder() -> None:
    redis = FakeRedis()
    key_id = uuid4()

    with pytest.raises(RuntimeError):
        async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1"):
            raise RuntimeError("上传失败")

    assert redis.data == {}
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        assert guard.replay is None


async def test_redis_failure_fails_open() -> None:
    redis = FakeRedis(fail=True)
    async with _idem(redis).lock(key_id=uuid4(), idem="k1", digest="d1") as guard:
        assert guard.replay is None
        await guard.record(202, {"id": "x"})  # 放行：不抛异常
    assert redis.data == {}


@pytest.mark.parametrize("body", [{"blob": "x" * MAX_REPLAY_BYTES}, _circular()])
async def test_uncacheable_body_releases_the_placeholder(body: dict) -> None:
    """落库失败（过大 / 不可序列化）→ 占位删除，客户端可重试；`default=str` 兜不住的只有循环引用。"""
    redis = FakeRedis()
    key_id = uuid4()
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        await guard.record(202, body)
    assert redis.data == {}


async def test_unreadable_placeholder_is_busy() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    redis.data[f"aics:idem:{key_id}:k1"] = "not json"

    with pytest.raises(IdempotencyBusyError):
        async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1"):
            pass


@pytest.mark.parametrize(
    "record",
    [
        '{"sha":"d1","state":"done","status":"202","body":{}}',
        '{"sha":"d1","state":"done","status":202,"body":[]}',
    ],
)
async def test_done_record_with_bad_shape_is_busy(record: str) -> None:
    redis = FakeRedis()
    key_id = uuid4()
    redis.data[f"aics:idem:{key_id}:k1"] = record

    with pytest.raises(IdempotencyBusyError):
        async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1"):
            pass


async def test_record_is_idempotent_when_replaying() -> None:
    redis = FakeRedis()
    key_id = uuid4()
    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        await guard.record(202, {"id": "doc-1"})
    key = f"aics:idem:{key_id}:k1"
    stored = redis.data[key]

    async with _idem(redis).lock(key_id=key_id, idem="k1", digest="d1") as guard:
        assert guard.replay is not None
        await guard.record(999, {"id": "other"})  # 回放分支不落库

    assert redis.data[key] == stored
    assert DONE in stored


def test_digest_bytes_matches_sha256() -> None:
    assert digest_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_digest_json_is_canonical() -> None:
    assert digest_json({"b": 1, "a": [2, 3]}) == digest_json({"a": [2, 3], "b": 1})
    assert digest_json({"a": 1}) != digest_json({"a": 2})


def test_digest_upload_covers_file_and_fields() -> None:
    data = "# 退货\n7 天内可退。".encode()
    base = digest_upload(data, {"title": "政策"})
    assert base == digest_upload(data, {"title": "政策"})
    assert base != digest_upload(data, {"title": "别的"})
    assert base != digest_upload(data + b"x", {"title": "政策"})
