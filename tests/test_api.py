import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient

    from app import main
    from app.fixer.fixer import ScanTimeout
    from app.fixer.llm import LLMUnavailable
    from app.security import SSRFBlocked
except ImportError:
    main = None

REPORT = {"total_violations_found": 0, "violations_processed": 0, "results": []}


@unittest.skipIf(main is None, "fastapi, httpx or axe_playwright_python not installed")
class ScanEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        p = mock.patch.object(main, "assert_public_host")
        self.assert_host = p.start()
        self.addCleanup(p.stop)

    def post(self, url="https://example.com"):
        return self.client.post("/scan", json={"url": url})

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_success(self):
        with mock.patch.object(main, "fix_page", return_value=REPORT):
            r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["violations"], [])

    def test_bad_urls_422(self):
        for url in ["not a url", "ftp://example.com", "https://x.com/" + "a" * 3000]:
            with self.subTest(url=url[:30]):
                self.assertEqual(self.post(url).status_code, 422)

    def test_ssrf_blocked_at_api_and_in_pipeline(self):
        self.assert_host.side_effect = SSRFBlocked("blocked")
        self.assertEqual(self.post().status_code, 422)
        self.assert_host.side_effect = None
        with mock.patch.object(main, "fix_page", side_effect=SSRFBlocked("redirect")):
            self.assertEqual(self.post().status_code, 422)

    def test_error_mapping_and_no_detail_leak(self):
        cases = [
            (LLMUnavailable("secret-detail"), 502),
            (ScanTimeout("slow page"), 504),
            (ValueError("secret-detail"), 502),
        ]
        for exc, status in cases:
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(main, "fix_page", side_effect=exc):
                    r = self.post()
                self.assertEqual(r.status_code, status)
                self.assertNotIn("secret-detail", r.text)

    def test_busy_returns_429_and_slot_is_released(self):
        sems = [main._scan_slots.acquire() for _ in range(main.MAX_CONCURRENT_SCANS)]
        try:
            self.assertEqual(self.post().status_code, 429)
        finally:
            for _ in sems:
                main._scan_slots.release()
        with mock.patch.object(main, "fix_page", side_effect=ValueError("x")):
            self.post()
        with mock.patch.object(main, "fix_page", return_value=REPORT):
            self.assertEqual(self.post().status_code, 200)

    def test_no_cors_headers(self):
        r = self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)


if __name__ == "__main__":
    unittest.main()
