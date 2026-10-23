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
    """Sum over all label sets of `name` that contain the given labels."""
    want = {(k, str(v)) for k, v in labels.items()}
    with _lock:
        return sum(v for (n, ls), v in _counters.items() if n == name and want <= set(ls))


def observations(name: str) -> list[float]:
    with _lock:
        return list(_observations.get(name, []))


def snapshot() -> list[tuple[str, dict[str, str], float]]:
    """Copy of every counter as `(name, labels, value)` (for `/metrics`)."""
    with _lock:
        return [(name, dict(ls), v) for (name, ls), v in _counters.items()]


def render() -> str:
    """Prometheus text exposition (M4.8 ③, ISSUE-06 的最小版).

    Counters whose name already ends with `_total` are declared `counter`; anything else is
    declared `gauge` so a partially-renamed counter still scrapes. Help lines are omitted on
    purpose — every metric is documented in 对话上下文管理 §12.1 / RAG 流程设计 §4.
    """
    lines: list[str] = []
    declared: set[str] = set()
    for name, labels, value in sorted(
        snapshot(), key=lambda item: (item[0], sorted(item[1].items()))
    ):
        if name not in declared:
            kind = "counter" if name.endswith("_total") else "gauge"
            lines.append(f"# TYPE {name} {kind}")
            declared.add(name)
        if labels:
            pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{pairs}}} {_number(value)}")
        else:
            lines.append(f"{name} {_number(value)}")
    with _lock:
        observations = {name: list(vals) for name, vals in _observations.items() if vals}
    for name in sorted(observations):
        vals = observations[name]
        lines.append(f"# TYPE {name}_count gauge")
        lines.append(f"{name}_count {len(vals)}")
        lines.append(f"# TYPE {name}_sum gauge")
        lines.append(f"{name}_sum {_number(sum(vals))}")
    return "\n".join(lines) + ("\n" if lines else "")


def _escape(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def reset() -> None:
    """Tests only."""
    with _lock:
        _counters.clear()
        _observations.clear()
