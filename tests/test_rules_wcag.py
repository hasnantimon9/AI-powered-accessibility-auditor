import unittest

from app.fixer.rules import contrast_ratio, get_rule, suggest_accessible_color
from app.fixer.wcag import extract_wcag_criterion


class ContrastTests(unittest.TestCase):
    def test_black_on_white_is_21(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=1)

    def test_known_low_contrast_pair(self):
        self.assertLess(contrast_ratio("#999999", "#ffffff"), 3.0)

    def test_suggestion_meets_required_ratio(self):
        for fg, bg, required in [
            ("#999999", "#ffffff", 4.5),
            ("#777777", "#eeeeee", 4.5),
            ("#aaaaaa", "#ffffff", 3.0),
            ("#888", "#fff", 4.5),
        ]:
            with self.subTest(fg=fg, bg=bg):
                new = suggest_accessible_color(fg, bg, required)
                self.assertIsNotNone(new)
                self.assertGreaterEqual(contrast_ratio(new, bg), required)

    def test_black_on_white_can_still_be_adjusted(self):
        new = suggest_accessible_color("#000000", "#ffffff", 4.5)
        self.assertIsNotNone(new)


class RuleRegistryTests(unittest.TestCase):
    def test_strategies(self):
        self.assertEqual(get_rule("color-contrast")["strategy"], "deterministic")
        self.assertEqual(get_rule("heading-order")["strategy"], "manual")
        self.assertEqual(get_rule("label")["strategy"], "llm")
        self.assertEqual(get_rule("some-unknown-rule")["strategy"], "llm")


class WcagTests(unittest.TestCase):
    def test_two_digit_criteria_are_not_confused(self):
        self.assertEqual(extract_wcag_criterion(["wcag2aa", "wcag143"]), "1.4.3")
        self.assertEqual(extract_wcag_criterion(["wcag1410"]), "1.4.10")

    def test_no_criterion(self):
        self.assertIsNone(extract_wcag_criterion(["best-practice", "cat.aria"]))


if __name__ == "__main__":
    unittest.main()
