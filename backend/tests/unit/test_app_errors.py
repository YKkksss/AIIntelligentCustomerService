from fastapi.testclient import TestClient

from aics.core.errors import AppError, NotFoundError
from aics.main import create_app


def _client() -> TestClient:
    app = create_app()

    @app.get("/boom/app")
    async def boom_app() -> None:
        raise NotFoundError(details={"id": "1"})

    @app.get("/boom/custom")
    async def boom_custom() -> None:
        raise AppError("CONFLICT", "冲突", 409)

    @app.get("/boom/crash")
    async def boom_crash() -> None:
        raise RuntimeError("secret detail")

    @app.get("/v")
    async def v(n: int) -> int:
        return n

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_envelope() -> None:
    r = _client().get("/boom/app", headers={"X-Request-ID": "rid-1"})
    assert r.status_code == 404
    assert r.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "资源不存在",
            "details": {"id": "1"},
            "request_id": "rid-1",
        }
    }


def test_custom_and_crash() -> None:
    c = _client()
    assert c.get("/boom/custom").json()["error"]["code"] == "CONFLICT"
    r = c.get("/boom/crash")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret detail" not in r.text


def test_validation_422() -> None:
    r = _client().get("/v?n=abc")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_route_uses_the_error_envelope() -> None:
    r = _client().get("/nope", headers={"X-Request-ID": "rid-2"})
    assert r.status_code == 404
    assert r.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "资源不存在",
            "details": {},
            "request_id": "rid-2",
        }
    }


def test_wrong_method_uses_the_error_envelope() -> None:
    r = _client().post("/v")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert r.headers["allow"] == "GET"


def test_unknown_v1_route_uses_the_openai_envelope() -> None:
    r = _client().get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "invalid_request_error"
