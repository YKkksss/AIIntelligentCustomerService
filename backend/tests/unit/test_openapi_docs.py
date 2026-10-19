"""OpenAPI 文档口径（M3.6 ④）：每个开放路由都要有 summary 与错误响应声明。

`/docs` 是接入方唯一的自助入口（本机与测试环境开放、生产关闭）。路由缺 `summary`
时，页面里只显示 `/api/v1/apps/{app_id}/...` 这种模板路径，接入方无从判断用途；
缺 `responses` 时看不到统一错误体长什么样、也不知道会遇到哪些状态码。这两件都
靠约定维持不住，所以写成测试：新增路由忘了写，这里就失败。
"""

from __future__ import annotations

import pytest

from aics.main import create_app


@pytest.fixture(scope="module")
def spec() -> dict:
    return create_app().openapi()


def operations(spec: dict) -> list[tuple[str, str, dict]]:
    return [
        (method.upper(), path, operation)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
    ]


def test_every_route_has_a_summary(spec: dict) -> None:
    missing = [f"{method} {path}" for method, path, op in operations(spec) if not op.get("summary")]
    assert not missing, f"这些路由没有 summary：{missing}"


def test_open_routes_document_the_standard_error_body(spec: dict) -> None:
    schemas = spec["components"]["schemas"]
    assert "ErrorEnvelope" in schemas and "ErrorItem" in schemas
    # 探活接口没有业务错误，其余都该声明 4xx
    probes = {"/healthz", "/readyz"}
    missing = []
    for method, path, operation in operations(spec):
        if path in probes:
            continue
        responses = operation.get("responses", {})
        if path.startswith("/v1/"):
            # `/v1` 用 OpenAI 风格错误体（不是 ErrorEnvelope），只要求声明了 4xx
            if not any(str(status).startswith("4") for status in responses):
                missing.append(f"{method} {path}")
            continue
        documented = {
            status
            for status, body in responses.items()
            if str(status).startswith("4")
            and "$ref" in str(body.get("content", {}).get("application/json", {}).get("schema", {}))
        }
        if not documented:
            missing.append(f"{method} {path}")
    assert not missing, f"这些路由没有声明 4xx 错误响应：{missing}"


def test_error_responses_point_at_the_envelope(spec: dict) -> None:
    ref = "#/components/schemas/ErrorEnvelope"
    for method, path, operation in operations(spec):
        if path.startswith("/v1/"):
            continue
        for status, body in operation.get("responses", {}).items():
            if not str(status).startswith("4"):
                continue
            schema = body.get("content", {}).get("application/json", {}).get("schema", {})
            assert schema.get("$ref") == ref, (
                f"{method} {path} 的 {status} 没有指向 ErrorEnvelope：{schema}"
            )


def test_docs_are_disabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    import aics.main as main

    settings = main.get_settings().model_copy(update={"env": "prod"})
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    assert app.docs_url is None
    assert app.openapi_url is None
    assert app.redoc_url is None


def test_docs_are_available_outside_production() -> None:
    app = create_app()
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"


def test_every_documented_code_appears_in_some_route(spec: dict) -> None:
    """错误码一致性测试保证码存在；这里保证它们在文档里有落点（至少 422/401/403/404）。"""

    statuses: set[str] = set()
    for _, _, operation in operations(spec):
        statuses.update(str(status) for status in operation.get("responses", {}))
    assert {"401", "403", "404", "422"} <= statuses
