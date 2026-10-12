import ipaddress
import socket
import unittest
from unittest import mock

from app.security import SSRFBlocked, assert_public_host, is_public_ip


def _addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class IsPublicIpTests(unittest.TestCase):
    def test_blocks_non_public_ranges(self):
        for ip in [
            "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1",
            "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
            "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
        ]:
            with self.subTest(ip=ip):
                self.assertFalse(is_public_ip(ipaddress.ip_address(ip)))

    def test_allows_public_addresses(self):
        for ip in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]:
            with self.subTest(ip=ip):
                self.assertTrue(is_public_ip(ipaddress.ip_address(ip)))


class AssertPublicHostTests(unittest.TestCase):
    def test_public_host_passes(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            assert_public_host("https://example.com/")

    def test_any_private_answer_blocks(self):
        with mock.patch(
            "socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "10.0.0.1")
        ):
            with self.assertRaises(SSRFBlocked):
                assert_public_host("https://mixed.example/")

    def test_message_does_not_leak_resolved_ip(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("10.1.2.3")):
            with self.assertRaises(SSRFBlocked) as ctx:
                assert_public_host("https://internal.example/")
        self.assertNotIn("10.1.2.3", str(ctx.exception))

    def test_unresolvable_host_blocked(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(SSRFBlocked):
                assert_public_host("https://nonexistent.invalid/")

    def test_non_http_scheme_blocked(self):
        for url in ["file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)"]:
            with self.subTest(url=url):
                with self.assertRaises(SSRFBlocked):
                    assert_public_host(url)

    def test_literal_metadata_ip_blocked(self):
        with self.assertRaises(SSRFBlocked):
            assert_public_host("http://169.254.169.254/latest/meta-data/")

    def test_literal_loopback_blocked(self):
        with self.assertRaises(SSRFBlocked):
            assert_public_host("http://127.0.0.1:8000/")


if __name__ == "__main__":
    unittest.main()
