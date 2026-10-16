"""出站 URL 安全校验（防 SSRF，安全设计 §4）：URL 导入与出站 Webhook 共用。

规则：
- 只允许 http/https；拒绝带用户名密码的 URL；端口只允许 80/443/8080/8443（命中白名单的目标不限端口）。
- 解析 DNS 后**所有**地址都必须是公网地址：拒绝私网、回环、链路本地（含 169.254.169.254 云元数据）、
  组播、保留、未指定地址，以及 IPv4 映射 / 6to4 / NAT64 等内嵌私网 IPv4 的 IPv6 地址。
- 白名单 `AICS_URL_FETCH_ALLOWLIST`：CIDR 或主机名（精确匹配，或 `.corp.local` 形式的后缀匹配）。
- 调用方在每一跳重定向前重新调用 `check_url`，并在连接建立后用 `check_peer_ip` 校验实际对端地址
  （缓解 DNS rebinding）。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from aics.core.errors import AppError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443, 8080, 8443)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SHARED = ipaddress.ip_network("100.64.0.0/10")  # 运营商级 NAT，Python 不算 private


class UrlNotAllowedError(AppError):
    code, status, message = "URL_NOT_ALLOWED", 400, "URL 不允许访问"


def default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UrlNotAllowedError(message=f"无法解析主机：{host}") from e
    return sorted({str(info[4][0]) for info in infos})


@dataclass(frozen=True)
class Allowlist:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    hosts: tuple[str, ...] = ()

    @classmethod
    def parse(cls, items: Iterable[str]) -> Allowlist:
        nets, hosts = [], []
        for raw in items:
            item = raw.strip().lower()
            if not item:
                continue
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                hosts.append(item)
        return cls(tuple(nets), tuple(hosts))

    def host_allowed(self, host: str) -> bool:
        h = host.lower().rstrip(".")
        return any(h == a or (a.startswith(".") and h.endswith(a)) for a in self.hosts)

    def ip_allowed(self, ip: IPAddress) -> bool:
        return any(ip in n for n in self.networks)


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        v4 = _embedded_v4(ip)
        if v4 is not None:
            return is_public_ip(v4)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED:
        return False
    return ip.is_global and not ip.is_multicast


def check_ip(ip: IPAddress, allow: Allowlist) -> None:
    if allow.ip_allowed(ip):
        return
    if not is_public_ip(ip):
        raise UrlNotAllowedError(message=f"目标地址 {ip} 属于内网 / 保留地址，不允许访问")


def check_url(url: str, allow: Allowlist, resolver: Resolver = default_resolver) -> list[str]:
    """校验 URL，返回解析出的 IP 列表；不通过抛 `400 URL_NOT_ALLOWED`。"""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise UrlNotAllowedError(message="URL 格式不合法") from e
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlNotAllowedError(message="只允许 http / https")
    if parts.username or parts.password:
        raise UrlNotAllowedError(message="URL 不能包含用户名或密码")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UrlNotAllowedError(message="URL 缺少主机名")
    port = port or (443 if parts.scheme.lower() == "https" else 80)

    host_ok = allow.host_allowed(host)
    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and (host.isdigit() or host.lower().startswith("0x")):
        # 十进制 / 十六进制整数形式的 IPv4（如 2130706433）：一律拒绝，避免解析歧义
        raise UrlNotAllowedError(message="不支持整数形式的 IP 地址")

    ips = [str(literal)] if literal is not None else resolver(host, port)
    if not ips:
        raise UrlNotAllowedError(message=f"无法解析主机：{host}")
    all_allowlisted = host_ok or all(allow.ip_allowed(ipaddress.ip_address(i)) for i in ips)
    if port not in ALLOWED_PORTS and not all_allowlisted:
        raise UrlNotAllowedError(message=f"不允许的端口：{port}")
    if not host_ok:
        for i in ips:
            check_ip(ipaddress.ip_address(i), allow)
    return ips


def check_peer_ip(host: str, peer_ip: str | None, allow: Allowlist) -> None:
    """连接建立后校验实际对端地址（DNS rebinding 防护）。"""
    if peer_ip is None or allow.host_allowed(host):
        return
    check_ip(ipaddress.ip_address(peer_ip), allow)
