"""Fakes for rag graph tests: a scriptable LangChain chat model and an in-memory knowledge base."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from aics.rag.ports import ChunkText, KeywordHit, ParentText

Responder = Callable[[list[BaseMessage], dict[str, Any]], str]


def node_of(messages: list[BaseMessage]) -> str:
    """Which rag prompt produced these messages (by its fixed opening text)."""
    first = str(messages[0].content)
    if "对话理解模块" in first:
        return "classify"
    if first.startswith("你是对话改写器"):
        return "condense"
    if first.startswith("判断以下资料是否足以回答问题"):
        return "judge"
    if "用户刚才只是在寒暄" in first:
        return "chitchat"
    return "generate"


def tag(text: str, name: str) -> str:
    start = text.rfind(f"<{name}>")
    end = text.find(f"</{name}>", start)
    return text[start + len(name) + 2 : end] if start >= 0 and end >= 0 else ""


class FakeChatModel(BaseChatModel):
    responder: Any
    delay_s: float = 0.0
    stream_delay_s: float = 0.0  # pause between streamed chunks (disconnect tests)
    calls: list[dict[str, Any]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-aics"

    def _usage(self, messages: list[BaseMessage], text: str) -> dict[str, int]:
        inp = sum(len(str(m.content)) for m in messages)
        return {"input_tokens": inp, "output_tokens": len(text), "total_tokens": inp + len(text)}

    def _reply(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        self.calls.append({"node": node_of(messages), "kwargs": kwargs, "messages": messages})
        return self.responder(messages, kwargs)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        text = self._reply(messages, kwargs)
        msg = AIMessage(content=text, usage_metadata=self._usage(messages, text))  # type: ignore[arg-type]
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        text = self._reply(messages, kwargs)
        for i in range(0, len(text), 4):
            if self.stream_delay_s:
                await asyncio.sleep(self.stream_delay_s)
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[i : i + 4]))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", usage_metadata=self._usage(messages, text))  # type: ignore[arg-type]
        )


@dataclass
class FakeLLMProvider:
    responder: Responder
    default_model: str = "fake-chat"
    delay_s: float = 0.0
    stream_delay_s: float = 0.0
    overrides: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def chat_model(self, **overrides: Any) -> FakeChatModel:
        self.overrides.append(overrides)
        m = FakeChatModel(
            responder=self.responder, delay_s=self.delay_s, stream_delay_s=self.stream_delay_s
        )
        m.calls = self.calls
        return m

    def nodes(self) -> list[str]:
        return [c["node"] for c in self.calls]


def classify_json(question: str, intent: str = "kb_qa", confidence: float = 0.9, **kw: Any) -> str:
    data = {
        "rewritten_query": kw.pop("rewritten", question),
        "intent": intent,
        "confidence": confidence,
        "need_clarify": kw.pop("need_clarify", False),
        "clarify_question": kw.pop("clarify_question", None),
        "slots": kw.pop("slots", {}),
    }
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------- knowledge base
APP = uuid.UUID(int=0xA11)
DOC = uuid.UUID(int=0xD0C)


@dataclass
class KbChunk:
    chunk_id: UUID
    title: str
    heading: str
    text: str
    keywords: tuple[str, ...]  # a question mentioning any of these "matches" this chunk


class FakeKB:
    """Keyword-matching chunk store implementing ChunkSearch + VectorSearch + a reranker.

    `scores` overrides rerank scores per question: {question: {chunk index: score}}.
    """

    model = "fake-rerank"

    def __init__(self, chunks: Sequence[KbChunk]) -> None:
        self.chunks = {c.chunk_id: c for c in chunks}
        self.rerank_override: dict[str, float] = {}
        self.fail_rerank = False
        self.search_calls: list[str] = []
        self.rerank_calls: list[str] = []

    def _match(self, question: str) -> list[UUID]:
        return [c.chunk_id for c in self.chunks.values() if any(k in question for k in c.keywords)]

    async def keyword_search(
        self, app_id: UUID, question: str, k: int, *, tags: Sequence[str] | None = None
    ) -> list[KeywordHit]:
        return [KeywordHit(c, 1.0) for c in self._match(question)[:k]]

    async def search(
        self, app_id: UUID, question: str, k: int, *, tags: Sequence[str] | None = None
    ) -> list[tuple[UUID, float]]:
        self.search_calls.append(question)
        hits = self._match(question)
        rest = [c for c in self.chunks if c not in hits]
        return [(c, 0.8) for c in hits][:k] + [(c, 0.3) for c in rest][: max(0, k - len(hits))]

    async def fetch_chunks(
        self, app_id: UUID, chunk_ids: Sequence[UUID], *, tags: Sequence[str] | None = None
    ) -> dict[UUID, ChunkText]:
        return {
            c: ChunkText(
                c, DOC, self.chunks[c].title, self.chunks[c].heading, self.chunks[c].text, 1
            )
            for c in chunk_ids
            if c in self.chunks
        }

    async def fetch_parents(
        self, app_id: UUID, parent_ids: Sequence[UUID]
    ) -> dict[UUID, ParentText]:
        return {}

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        self.rerank_calls.append(query)
        if self.fail_rerank:
            raise RuntimeError("rerank down")
        hits = {self.chunks[c].text for c in self._match(query)}
        top = self.rerank_override.get(query, 0.92)
        scored = [(i, top if any(h in d for h in hits) else 0.05) for i, d in enumerate(docs)]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]
