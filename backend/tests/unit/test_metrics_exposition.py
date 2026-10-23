"""M4.8 ③：`/metrics` 的 Prometheus 文本暴露（ISSUE-06 的最小版）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aics.core import metrics
from aics.main import create_app


@pytest.fixture(autouse=True)
def _clean() -> None:
    metrics.reset()
    yield
    metrics.reset()


def test_render_declares_counter_type_for_total_names() -> None:
    metrics.inc("aics_idle_notice_total", app_id="a1")
    metrics.inc("aics_idle_notice_total", app_id="a1")

    text = metrics.render()

    assert "# TYPE aics_idle_notice_total counter" in text
    assert 'aics_idle_notice_total{app_id="a1"} 2' in text


def test_render_falls_back_to_gauge_and_escapes_labels() -> None:
    metrics.inc("aics_custom", label='a"b\\c')
    metrics.observe("aics_latency_ms", 12.5)

    text = metrics.render()

    assert "# TYPE aics_custom gauge" in text
    assert 'aics_custom{label="a\\"b\\\\c"} 1' in text
    # 观测值只以 `_count` / `_sum` 形式出现在最小版里（直方图分桶留 M6.3）
    assert "aics_latency_ms_count 1" in text


def test_render_is_empty_when_nothing_was_recorded() -> None:
    assert metrics.render() == ""


def test_metrics_endpoint_is_plain_text_and_outside_openapi() -> None:
    metrics.inc("aics_idle_notice_total")
    client = TestClient(create_app())

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "aics_idle_notice_total" in response.text
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
