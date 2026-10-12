import ipaddress
import socket
from urllib.parse import urlparse


class SSRFBlocked(Exception):
    pass


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def assert_public_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlocked("Only http:// and https:// URLs can be scanned.")
    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlocked("The URL has no host.")
    # Empty DNS labels (".example.com", "a..b.com") make the idna codec raise
    # UnicodeError; reject them up front as invalid hosts.
    if hostname.startswith(".") or ".." in hostname:
        raise SSRFBlocked("The URL has an invalid host name.")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError) as e:
        # UnicodeError (e.g. idna "label empty"/"label too long") must be
        # converted here: Playwright can't re-wrap it and crashes with a
        # confusing TypeError.
        raise SSRFBlocked("The host could not be resolved.") from None

    for *_, sockaddr in addr_infos:
        if not is_public_ip(ipaddress.ip_address(sockaddr[0].split("%")[0])):
            raise SSRFBlocked(
                "The URL points to a non-public address; "
                "internal and private targets can't be scanned."
            )
