"""Fakes for memory compaction tests: a scriptable LLM provider (chat_model().ainvoke)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage

Responder = Callable[[str, dict[str, Any]], str]


def default_responder(prompt: str, overrides: dict[str, Any]) -> str:
    """Summary: echo the number of new lines; facts: pick order numbers from user lines."""
    if "<new_turns>" in prompt:
        body = prompt.split("<new_turns>")[1].split("</new_turns>")[0]
        n = len([x for x in body.splitlines() if x.strip()])
        return json.dumps(
            {
                "user_goal": "售后咨询",
                "confirmed_info": [f"新增 {n} 条消息"],
                "solutions_given": [],
                "open_issues": [],
            },
            ensure_ascii=False,
        )
    body = prompt.split("<turns>")[1].split("</turns>")[0]
    facts = []
    for line in body.splitlines():
        m = re.match(r"\[(\d+)\]\[user\].*?([A-Z]\d{8,})", line)
        if m:
            facts.append(
                {
                    "key": "order_no",
                    "value": m.group(2),
                    "source_index": int(m.group(1)),
                    "confidence": 0.9,
                }
            )
    return json.dumps({"facts": facts[-1:]})


class _Chat:
    def __init__(self, owner: FakeMemoryLLM, overrides: dict[str, Any]) -> None:
        self.owner, self.overrides = owner, overrides

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        prompt = str(messages[-1].content)
        self.owner.calls.append({"prompt": prompt, "overrides": self.overrides, "kwargs": kwargs})
        if self.owner.before_reply is not None:
            await self.owner.before_reply(prompt)
        out = self.owner.responder(prompt, self.overrides)
        return AIMessage(
            content=out,
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )


class FakeMemoryLLM:
    default_model = "fake-chat"

    def __init__(self, responder: Responder = default_responder) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []
        self.before_reply: Callable[[str], Any] | None = None

    def chat_model(self, **overrides: Any) -> _Chat:
        return _Chat(self, overrides)
