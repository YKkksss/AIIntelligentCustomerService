"""In-process counters (对话上下文管理 §12.1, RAG 流程设计 §4).

M2.5 records the memory / guardrail counters here; M6.3 exports them on `/metrics`
(Prometheus). Labels are plain strings and must never carry PII (mask first).
Thread-safe: Celery thread pools and the API process both increment.
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()
_counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
_observations: dict[str, list[float]] = {}
_MAX_OBSERVATIONS = 1000


def inc(name: str, amount: float = 1, **labels: str) -> None:
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    with _lock:
        _counters[key] += amount


def observe(name: str, value: float) -> None:
    """Histogram-like observation (kept as the last 1000 values until M6.3)."""
    with _lock:
        vals = _observations.setdefault(name, [])
        vals.append(value)
        del vals[:-_MAX_OBSERVATIONS]


def value(name: str, **labels: str) -> float:
    """Counter value; with no labels, the sum over all label sets."""
    with _lock:
        if labels:
            return _counters[(name, tuple(sorted((k, str(v)) for k, v in labels.items())))]
        return sum(v for (n, _), v in _counters.items() if n == name)


def observations(name: str) -> list[float]:
    with _lock:
        return list(_observations.get(name, []))


def reset() -> None:
    """Tests only."""
    with _lock:
        _counters.clear()
        _observations.clear()
