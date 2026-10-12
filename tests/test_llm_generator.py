import unittest
from unittest import mock

import requests

from app.fixer import generator, llm

CTX = {
    "issue_type": "link-name",
    "rule_help": "Links must have discernible text",
    "wcag_criterion": "2.4.4",
    "failure_summary": "x",
    "original_html": '<a href="/cart"></a>',
    "surrounding": None,
}


def _resp(status, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = (body or "{}").encode()
    return r


OK_BODY = '{"choices":[{"message":{"content":"{\\"a\\":1}"}}]}'


class LLMRetryTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(llm, "LLM_API_KEY", "test-key")
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch("time.sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)

    def test_retries_429_then_succeeds(self):
        with mock.patch(
            "requests.post", side_effect=[_resp(429), _resp(200, OK_BODY)]
        ) as post:
            self.assertEqual(llm.llm_json("p", {}), {"a": 1})
        self.assertEqual(post.call_count, 2)
        self.sleep.assert_called_once()

    def test_gives_up_after_max_attempts(self):
        with mock.patch("requests.post", return_value=_resp(503)) as post:
            with self.assertRaises(llm.LLMError):
                llm.llm_chat("p")
        self.assertEqual(post.call_count, llm.LLM_MAX_ATTEMPTS)

    def test_bad_key_is_unavailable_and_not_retried(self):
        with mock.patch("requests.post", return_value=_resp(401)) as post:
            with self.assertRaises(llm.LLMUnavailable):
                llm.llm_chat("p")
        self.assertEqual(post.call_count, 1)

    def test_retry_after_is_capped(self):
        wait = llm._backoff_seconds(_resp(429, headers={"Retry-After": "999"}), 1)
        self.assertEqual(wait, llm._MAX_BACKOFF_S)

    def test_missing_key(self):
        with mock.patch.object(llm, "LLM_API_KEY", None):
            with self.assertRaises(llm.LLMUnavailable):
                llm.llm_chat("p")


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(generator, "SANITY_CHECK", False)
        p.start()
        self.addCleanup(p.stop)

    def _fix(self, fix_code):
        with mock.patch.object(
            generator, "llm_json", return_value={"fix_code": fix_code, "explanation": "e"}
        ):
            return generator.generate_fix(CTX)

    def test_safe_fix_accepted(self):
        r = self._fix('<a href="/cart" aria-label="Cart"></a>')
        self.assertFalse(r.fix_generation_failed)

    def test_unsafe_fixes_rejected(self):
        for bad in [
            '<a href="/cart" onclick="x()"></a>',
            '<img src=x onerror="x()">',
            '<a href="javascript:alert(1)">Cart</a>',
            "<script>alert(1)</script>",
            '<iframe src="/x"></iframe>',
        ]:
            with self.subTest(bad=bad):
                self.assertTrue(self._fix(bad).fix_generation_failed)

    def test_attribute_fix_cleans_value(self):
        with mock.patch.object(
            generator, "llm_json", return_value={"value": ' "Cart" ', "explanation": "e"}
        ):
            r = generator.generate_attribute_fix(CTX, "aria-label")
        self.assertEqual(r.attribute_value, "Cart")

    def test_attribute_value_with_markup_rejected(self):
        with mock.patch.object(
            generator, "llm_json", return_value={"value": "<b>x</b>", "explanation": "e"}
        ):
            self.assertTrue(
                generator.generate_attribute_fix(CTX, "aria-label").fix_generation_failed
            )

    def test_decorative_alt_becomes_empty(self):
        with mock.patch.object(
            generator, "llm_json", return_value={"value": "decorative", "explanation": "e"}
        ):
            r = generator.generate_attribute_fix(CTX, "alt")
        self.assertEqual(r.attribute_value, "")

    def test_unavailable_propagates(self):
        with mock.patch.object(generator, "llm_json", side_effect=llm.LLMUnavailable("x")):
            with self.assertRaises(llm.LLMUnavailable):
                generator.generate_fix(CTX)


if __name__ == "__main__":
    unittest.main()
