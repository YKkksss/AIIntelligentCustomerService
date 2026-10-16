import httpx
import pytest
import respx

from aics.core.netguard import UrlNotAllowedError
from aics.ingest.parsers import ParseError, UnsupportedFileTypeError
from aics.ingest.parsers.url import fetch_url, parse_url

RES = {
    "example.com": ["93.184.215.14"],
    "cdn.example.com": ["93.184.215.15"],
    "internal.example.com": ["10.0.0.8"],
}


def resolver(host: str, port: int) -> list[str]:
    return RES[host]


HTML = (
    "<html><head><title>帮助</title></head><body><article><h1>退货</h1>"
    "<p>签收后七天内可申请无理由退货，质量问题运费由商家承担。</p></article></body></html>"
)


@respx.mock
async def test_fetch_and_parse_html() -> None:
    respx.get("https://example.com/help").mock(
        return_value=httpx.Response(
            200, text=HTML, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    doc = await parse_url("https://example.com/help", resolver=resolver)
    assert doc.title == "帮助" and doc.blocks[0].text == "退货"


@respx.mock
async def test_redirect_to_private_rejected() -> None:
    respx.get("https://example.com/r").mock(
        return_value=httpx.Response(302, headers={"location": "http://internal.example.com/admin"})
    )
    with pytest.raises(UrlNotAllowedError):
        await fetch_url("https://example.com/r", resolver=resolver)


@respx.mock
async def test_redirect_followed_when_public() -> None:
    respx.get("https://example.com/r").mock(
        return_value=httpx.Response(301, headers={"location": "https://cdn.example.com/x.txt"})
    )
    respx.get("https://cdn.example.com/x.txt").mock(
        return_value=httpx.Response(
            200, content="纯文本".encode(), headers={"content-type": "text/plain"}
        )
    )
    f = await fetch_url("https://example.com/r", resolver=resolver)
    assert f.url == "https://cdn.example.com/x.txt" and f.file_type == "txt"


@respx.mock
async def test_too_many_redirects() -> None:
    respx.get("https://example.com/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"})
    )
    with pytest.raises(ParseError, match="重定向"):
        await fetch_url("https://example.com/loop", resolver=resolver)


async def test_private_literal_rejected_without_request() -> None:
    with respx.mock(assert_all_called=False) as m:
        route = m.get(url__regex=".*")
        with pytest.raises(UrlNotAllowedError):
            await fetch_url("http://192.168.1.1/", resolver=resolver)
        assert not route.called


@respx.mock
async def test_size_limit() -> None:
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(
            200, content=b"a" * 2048, headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ParseError, match="10MB"):
        await fetch_url("https://example.com/big", resolver=resolver, max_bytes=1024)


@respx.mock
async def test_declared_size_limit() -> None:
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(
            200, content=b"a", headers={"content-type": "text/plain", "content-length": "99999999"}
        )
    )
    with pytest.raises(ParseError):
        await fetch_url("https://example.com/big", resolver=resolver)


@respx.mock
async def test_http_error_unsupported_type_and_timeout() -> None:
    respx.get("https://example.com/404").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/img").mock(
        return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/png"})
    )
    respx.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("t"))
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("c"))
    with pytest.raises(ParseError, match="404"):
        await fetch_url("https://example.com/404", resolver=resolver)
    with pytest.raises(UnsupportedFileTypeError):
        await fetch_url("https://example.com/img", resolver=resolver)
    with pytest.raises(ParseError, match="超时"):
        await fetch_url("https://example.com/slow", resolver=resolver)
    with pytest.raises(ParseError, match="ConnectError"):
        await fetch_url("https://example.com/down", resolver=resolver)
