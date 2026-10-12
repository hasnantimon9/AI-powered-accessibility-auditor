import io
import unittest

try:
    from app.fixer import fixer
except ImportError:
    fixer = None


@unittest.skipIf(fixer is None, "axe_playwright_python not installed")
class FixerHelperTests(unittest.TestCase):
    def test_sort_key_orders_severity_then_numeric_wcag(self):
        def pair(impact, tag):
            return ({"impact": impact, "tags": [tag]}, {})

        pairs = [
            pair("minor", "wcag111"),
            pair("critical", "wcag1410"),
            pair("critical", "wcag143"),
            pair("serious", "wcag111"),
        ]
        ordered = sorted(pairs, key=fixer._sort_key)
        self.assertEqual(
            [(p[0]["impact"], p[0]["tags"][0]) for p in ordered],
            [("critical", "wcag143"), ("critical", "wcag1410"),
             ("serious", "wcag111"), ("minor", "wcag111")],
        )

    def test_nested_targets_are_not_simple(self):
        self.assertTrue(fixer._is_simple_target({"target": ["#a"]}))
        self.assertFalse(fixer._is_simple_target({"target": ["iframe", "#a"]}))
        self.assertFalse(fixer._is_simple_target({"target": [["#host", "#in"]]}))

    def test_blank_image_detection(self):
        from PIL import Image

        def png(color_fn):
            img = Image.new("RGB", (20, 20), "white")
            color_fn(img)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

        self.assertTrue(fixer.is_blank_image(png(lambda i: None)))
        self.assertFalse(
            fixer.is_blank_image(png(lambda i: i.putpixel((3, 3), (255, 0, 0))))
        )


if __name__ == "__main__":
    unittest.main()
