import os
from dataclasses import dataclass

DEFAULT_PAGE_LANG = os.environ.get("DEFAULT_PAGE_LANG", "en")


@dataclass
class DeterministicFix:
    js: str
    arg: object
    fix_code: str
    explanation: str
    needs_review: bool = False


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _luminance(_hex_to_rgb(fg_hex))
    l2 = _luminance(_hex_to_rgb(bg_hex))
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def suggest_accessible_color(fg_hex: str, bg_hex: str, required: float) -> str | None:
    fg = _hex_to_rgb(fg_hex)
    best: tuple[float, str] | None = None

    for target in ((0, 0, 0), (255, 255, 255)):
        for step in range(1, 51):
            t = step / 50
            mixed = tuple(f + (g - f) * t for f, g in zip(fg, target))
            candidate = _rgb_to_hex(mixed)
            if contrast_ratio(candidate, bg_hex) >= required + 0.01:
                if best is None or t < best[0]:
                    best = (t, candidate)
                break

    return best[1] if best else None


_COLOR_JS = """
(el, color) => {
  const prevValue = el.style.getPropertyValue('color');
  const prevPriority = el.style.getPropertyPriority('color');
  el.style.setProperty('color', color, 'important');
  window.__ap_undo = () => {
    if (prevValue) el.style.setProperty('color', prevValue, prevPriority);
    else el.style.removeProperty('color');
  };
  return { ok: true };
}
"""


def fix_color_contrast(violation: dict, node: dict) -> DeterministicFix | None:
    check = next(
        (c for c in node.get("any", []) if c.get("id") == "color-contrast"), None
    )
    if not check:
        return None
    data = check.get("data") or {}
    try:
        fg, bg = data["fgColor"], data["bgColor"]
        required = float(str(data["expectedContrastRatio"]).split(":")[0])
        current = float(data["contrastRatio"])
        new_color = suggest_accessible_color(fg, bg, required)
    except (KeyError, ValueError, TypeError):
        return None
    if new_color is None:
        return None

    new_ratio = contrast_ratio(new_color, bg)
    return DeterministicFix(
        js=_COLOR_JS,
        arg=new_color,
        fix_code=f'style="color: {new_color} !important"',
        explanation=(
            f"Changed text color from {fg} to {new_color} on background {bg}: "
            f"contrast {current:.2f}:1 -> {new_ratio:.2f}:1 (needs {required}:1)."
        ),
    )


_LANG_JS = """
(el, lang) => {
  const prev = el.getAttribute('lang');
  el.setAttribute('lang', lang);
  window.__ap_undo = () => {
    if (prev === null) el.removeAttribute('lang');
    else el.setAttribute('lang', prev);
  };
  return { ok: true };
}
"""


def fix_html_lang(violation: dict, node: dict) -> DeterministicFix:
    return DeterministicFix(
        js=_LANG_JS,
        arg=DEFAULT_PAGE_LANG,
        fix_code=f'<html lang="{DEFAULT_PAGE_LANG}">',
        explanation=(
            f"Set lang=\"{DEFAULT_PAGE_LANG}\" on <html>. The language is an "
            "assumption; confirm it matches the page's real language."
        ),
        needs_review=True,
    )


RULES = {
    "html-has-lang": {"strategy": "deterministic", "fix": fix_html_lang},
    "color-contrast": {"strategy": "deterministic", "fix": fix_color_contrast},
    "label": {
        "strategy": "llm",
        "mode": "attribute",
        "attribute": "aria-label",
        "hint": (
            "This form control has no accessible name. Write 2-4 words saying "
            'what the user should enter, like "Email address" or "Search".'
        ),
    },
    "select-name": {
        "strategy": "llm",
        "mode": "attribute",
        "attribute": "aria-label",
        "hint": (
            "This dropdown has no accessible name. Write 2-4 words saying what "
            'the user is choosing, like "Country" or "Sort by".'
        ),
    },
    "button-name": {
        "strategy": "llm",
        "mode": "attribute",
        "attribute": "aria-label",
        "hint": (
            "This button shows only an icon. Write 1-3 words for the action it "
            'performs, like "Close" or "Open menu". Use the surrounding context.'
        ),
    },
    "link-name": {
        "strategy": "llm",
        "mode": "attribute",
        "attribute": "aria-label",
        "hint": (
            "This link shows only an icon. Write 1-3 words saying where it goes, "
            'like "Cart" or "Home". Use the href and the surrounding context.'
        ),
    },
    "image-alt": {
        "strategy": "llm",
        "mode": "attribute",
        "attribute": "alt",
        "vision": True,
        "needs_review": True,
        "hint": (
            "Write alt text, under 12 words, for someone who cannot see this "
            "image, based on the surrounding page context (you cannot see "
            "the image itself). Do not start with 'image of' or 'picture of'. "
            "Do not guess or invent specific details you can't infer from "
            "context. If nothing in the context suggests this image carries "
            "meaning, answer with exactly the single word: decorative"
        ),
    },
}

MANUAL_RULES = {
    "heading-order",
    "page-has-heading-one",
    "empty-heading",
    "document-title",
    "meta-viewport",
    "bypass",
    "region",
    "landmark-one-main",
    "landmark-unique",
    "landmark-no-duplicate-main",
    "landmark-no-duplicate-banner",
    "landmark-no-duplicate-contentinfo",
    "landmark-banner-is-top-level",
    "landmark-contentinfo-is-top-level",
    "landmark-main-is-top-level",
    "landmark-complementary-is-top-level",
    "scrollable-region-focusable",
    "tabindex",
    "focus-order-semantics",
    "frame-title",
}


def get_rule(rule_id: str) -> dict:
    if rule_id in RULES:
        return RULES[rule_id]
    if rule_id in MANUAL_RULES:
        return {"strategy": "manual"}
    return {"strategy": "llm"}


def get_hint(rule_id: str) -> str | None:
    return get_rule(rule_id).get("hint")
