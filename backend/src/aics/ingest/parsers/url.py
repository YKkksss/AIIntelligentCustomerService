"""URL 导入：SSRF 校验 + 受限抓取（文档处理流水线 §3.2、安全设计 §4）。

- 每一跳重定向（最多 5 跳）都重新做 `check_url`；连接建立后校验实际对端 IP。
- 总超时 15s，响应体 ≤ 10MB（流式读取，超限立即中止）。
- 按 Content-Type 选择解析器：HTML / 纯文本 / Markdown / PDF / DOCX；其他类型 `400 UNSUPPORTED_FILE_TYPE`。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from aics.core.netguard import (
    Allowlist,
    Resolver,
    UrlNotAllowedError,
    check_peer_ip,
    check_url,
    default_resolver,
)
from aics.ingest.parsers.base import ParsedDoc, ParseError, UnsupportedFileTypeError

FETCH_TIMEOUT_S = 15.0
FETCH_MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "AICS-Fetcher/1.0 (+knowledge-import)"

CONTENT_TYPES = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


@dataclass(frozen=True)
class FetchedUrl:
    url: str  # 最终地址（重定向后）
    content: bytes
    content_type: str
    file_type: str  # html / txt / md / pdf / docx


def _peer_ip(resp: httpx.Response) -> str | None:
    stream = resp.extensions.get("network_stream")
    if stream is None:
        return None
    addr = stream.get_extra_info("server_addr")
    return str(addr[0]) if addr else None


async def fetch_url(
    url: str,
    *,
    allowlist: Iterable[str] = (),
    resolver: Resolver = default_resolver,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = FETCH_TIMEOUT_S,
    max_bytes: int = FETCH_MAX_BYTES,
) -> FetchedUrl:
    allow = Allowlist.parse(allowlist)
    deadline = time.monotonic() + timeout_s
    own = client is None
    http = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current, allow, resolver)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParseError(message="抓取网页超时")
            try:
                async with http.stream(
                    "GET",
                    current,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/pdf,*/*;q=0.5",
                    },
                    timeout=remaining,
                    follow_redirects=False,
                ) as resp:
                    check_peer_ip(urlsplit(current).hostname or "", _peer_ip(resp), allow)
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise ParseError(message="网页重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise ParseError(message=f"网页返回 HTTP {resp.status_code}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ParseError(message="网页内容超过 10MB")
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise ParseError(message="网页内容超过 10MB")
                        if time.monotonic() > deadline:
                            raise ParseError(message="抓取网页超时")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    ftype = CONTENT_TYPES.get(ctype)
                    if ftype is None:
                        raise UnsupportedFileTypeError(
                            message=f"不支持的网页内容类型：{ctype or '未知'}"
                        )
                    return FetchedUrl(str(resp.url), bytes(buf), ctype, ftype)
            except httpx.TimeoutException as e:
                raise ParseError(message="抓取网页超时") from e
            except httpx.HTTPError as e:
                raise ParseError(message=f"抓取网页失败：{type(e).__name__}") from e
        raise ParseError(message="网页重定向次数过多")
    finally:
        if own:
            await http.aclose()


async def parse_url(url: str, **kwargs: object) -> ParsedDoc:
    from aics.ingest.parsers import parse_bytes
    from aics.ingest.parsers.html import parse_html_text
    from aics.ingest.parsers.text import decode_text

    fetched = await fetch_url(url, **kwargs)  # type: ignore[arg-type]
    if fetched.file_type == "html":
        return parse_html_text(decode_text(fetched.content), url=fetched.url)
    return parse_bytes(fetched.content, fetched.file_type)


__all__ = ["FetchedUrl", "UrlNotAllowedError", "fetch_url", "parse_url"]
