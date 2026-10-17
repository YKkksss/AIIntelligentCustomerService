import pytest

from aics.core.netguard import (
    Allowlist,
    UrlNotAllowedError,
    check_peer_ip,
    check_url,
    is_public_ip,
    resolve_client_ip,
)

NONE = Allowlist.parse([])
PROXY = Allowlist.parse(["172.17.0.0/16", "2001:db8::/32"])


def res(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    return lambda host, port: mapping[host]


PUBLIC = res(
    {
        "example.com": ["93.184.215.14"],
        "evil.test": ["10.0.0.5"],
        "mixed.test": ["93.184.215.14", "192.168.1.9"],
        "wiki.corp.local": ["192.168.110.20"],
        "a.corp.local": ["192.168.110.21"],
    }
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost.localdomain.invalid:80/",  # 解析失败也拒绝（见下方 resolver）
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.110.86:6333/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::a00:1]/",
        "http://[2002:a00:1::]/",
        "http://224.0.0.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://evil.test/",
        "http://mixed.test/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "http://user:pw@example.com/",
        "http://example.com:22/",
        "http:///nohost",
    ],
)
def test_rejected(url: str) -> None:
    def resolver(host: str, port: int) -> list[str]:
        if host not in {"example.com", "evil.test", "mixed.test"}:
            raise UrlNotAllowedError(message="无法解析")
        return PUBLIC(host, port)

    with pytest.raises(UrlNotAllowedError) as e:
        check_url(url, NONE, resolver)
    assert e.value.code == "URL_NOT_ALLOWED" and e.value.status == 400


def test_public_allowed() -> None:
    assert check_url("https://example.com/a?b=1", NONE, PUBLIC) == ["93.184.215.14"]
    assert check_url("http://example.com:8080/", NONE, PUBLIC)
    assert check_url("http://93.184.215.14/", NONE, PUBLIC)


def test_allowlist_cidr_and_host() -> None:
    allow = Allowlist.parse(["192.168.110.0/24", "wiki.corp.local", " .corp.local ", ""])
    assert check_url("http://192.168.110.86:6333/", allow, PUBLIC)  # 白名单不限端口
    assert check_url("http://wiki.corp.local/page", allow, PUBLIC)
    assert check_url("http://a.corp.local/", allow, PUBLIC)
    with pytest.raises(UrlNotAllowedError):
        check_url("http://10.0.0.1/", allow, PUBLIC)


def test_peer_ip_check() -> None:
    check_peer_ip("example.com", "93.184.215.14", NONE)
    check_peer_ip("example.com", None, NONE)
    with pytest.raises(UrlNotAllowedError):
        check_peer_ip("example.com", "127.0.0.1", NONE)
    check_peer_ip("wiki.corp.local", "192.168.110.20", Allowlist.parse(["wiki.corp.local"]))


def test_is_public_ip() -> None:
    import ipaddress

    assert is_public_ip(ipaddress.ip_address("8.8.8.8"))
    assert is_public_ip(ipaddress.ip_address("2001:4860:4860::8888"))
    assert not is_public_ip(ipaddress.ip_address("::ffff:10.0.0.1"))


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        # 直连：一律以对端为准，绝不采信请求头
        ("203.0.113.9", None, NONE, "203.0.113.9"),
        ("203.0.113.9", "1.2.3.4", NONE, "203.0.113.9"),
        ("203.0.113.9", "1.2.3.4", PROXY, "203.0.113.9"),
        # 可信代理：取最右侧的不可信地址
        ("172.17.0.5", "203.0.113.9", PROXY, "203.0.113.9"),
        ("172.17.0.5", "1.2.3.4, 203.0.113.9", PROXY, "203.0.113.9"),
        ("172.17.0.5", "203.0.113.9, 172.17.0.6", PROXY, "203.0.113.9"),  # 多级代理
        ("172.17.0.5", "172.17.0.7, 172.17.0.6", PROXY, "172.17.0.5"),  # 链全可信 → 退回对端
        ("172.17.0.5", None, PROXY, "172.17.0.5"),
        ("172.17.0.5", "  ", PROXY, "172.17.0.5"),
        ("[2001:db8::1]", "203.0.113.9", PROXY, None),  # 对端带方括号 → 解析失败
        ("172.17.0.5", "8.8.8.8, ::ffff:203.0.113.9", PROXY, "::ffff:203.0.113.9"),
        ("172.17.0.5", "203.0.113.9, garbage", PROXY, None),  # 链里有非法地址 → 不判定
        ("not-an-ip", "203.0.113.9", PROXY, None),
        (None, "203.0.113.9", PROXY, None),
        ("2001:db8::5", "203.0.113.9", PROXY, "203.0.113.9"),
    ],
)
def test_resolve_client_ip(
    peer: str | None, xff: str | None, trusted: Allowlist, expected: str | None
) -> None:
    ip = resolve_client_ip(peer, xff, trusted)
    assert (str(ip) if ip is not None else None) == expected
