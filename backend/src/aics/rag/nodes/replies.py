"""Non-kb_qa branches and handoff (意图识别 §6.3; 会话生命周期与转人工 §3).

M2.3 uses fixed replies; M2.4 adds the rule-layer templates (greeting / thanks / ack / bye)
and the optional LLM chitchat reply. None of these emit citations or go through postprocess.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from aics.rag.nodes.answer import emit_trace
from aics.rag.schemas import DeltaEvent
from aics.rag.state import RagDeps, RagState, merged

DEFAULT_CHITCHAT_REPLY = "您好，请问有什么可以帮您？"


def _reply(
    state: RagState,
    runtime: Runtime[RagDeps],
    text: str,
    answer_type: str,
    reason: str,
    handoff_reason: str | None = None,
) -> dict[str, Any]:
    emit_trace(state, runtime)
    runtime.stream_writer(DeltaEvent(text=text))
    return merged(
        state,
        answer=text,
        answer_type=answer_type,
        decision_reason=reason,
        handoff_reason=handoff_reason,
        citations=[],
    )


async def chitchat_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    replies = state["input"].config.intent.chitchat_replies
    text = (replies.greeting if replies and replies.greeting else "") or DEFAULT_CHITCHAT_REPLY
    return _reply(state, runtime, text, "answer", "intent:chitchat")


def out_of_scope_text(state: RagState) -> str:
    inp = state["input"]
    if inp.config.intent.out_of_scope_reply:
        return inp.config.intent.out_of_scope_reply
    return f"抱歉，我是{inp.profile.name}的客服助手，只能回答与本业务相关的问题。"


async def out_of_scope_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    return _reply(state, runtime, out_of_scope_text(state), "refused", "intent:out_of_scope")


async def clarify_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    q = state["intent"].clarify_question or ""
    return _reply(state, runtime, q, "answer", "intent:clarify")


async def handoff(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    intent = state["intent"].intent
    if state.get("decision") == "handoff":  # judge: unanswerable + on_unanswerable
        return _reply(
            state, runtime, inp.handoff.text, "handoff", state["decision_reason"], "unanswerable"
        )
    if intent == "action":
        return _reply(
            state,
            runtime,
            inp.config.intent.action_reply,
            "handoff",
            "intent:action",
            "intent_action",
        )
    return _reply(state, runtime, inp.handoff.text, "handoff", "intent:human", "intent_human")
