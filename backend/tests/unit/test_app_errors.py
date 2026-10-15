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
