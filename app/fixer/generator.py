import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from app.fixer.llm import LLMError, LLMUnavailable, llm_json
from app.fixer.rules import get_hint

SANITY_CHECK = os.environ.get("SANITY_CHECK", "1") == "1"

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "fix_code": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["fix_code", "explanation"],
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["valid", "reason"],
}

ATTR_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["value", "explanation"],
}

MAX_PROMPT_HTML = 800


@dataclass
class FixResult:
    fix_code: str | None
    explanation: str | None
    llm_validated: bool
    fix_generation_failed: bool = False
    attribute_value: str | None = None


_FORBIDDEN_TAGS = {"script", "style", "iframe", "object", "embed", "link", "meta", "base"}
_URL_ATTRS = {"href", "src", "action", "formaction", "xlink:href"}


class _UnsafeHTML(Exception):
    pass


class _SafetyParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag in _FORBIDDEN_TAGS:
            raise _UnsafeHTML(f"<{tag}> is not allowed in a fix")
        for name, value in attrs:
            if name.startswith("on"):
                raise _UnsafeHTML(f"event handler {name!r} is not allowed in a fix")
            if name in _URL_ATTRS and (value or "").strip().lower().startswith(
                ("javascript:", "data:text/html")
            ):
                raise _UnsafeHTML(f"unsafe URL in {name!r}")

    handle_startendtag = handle_starttag


def _check_fix_html_is_safe(html: str) -> None:
    try:
        _SafetyParser().feed(html)
    except _UnsafeHTML as e:
        raise LLMError(f"model returned unsafe HTML: {e}") from e


def _shorten_html(html: str) -> str:
    html = re.sub(r"(data:[^\"'\s]{0,30})[^\"'\s]{40,}", r"\1...[truncated]", html)
    if len(html) > MAX_PROMPT_HTML:
        html = html[:MAX_PROMPT_HTML] + "...[truncated]"
    return html


def _build_fix_prompt(violation_context: dict) -> str:
    hint = get_hint(violation_context["issue_type"])
    hint_block = f"\nHow to fix this kind of issue:\n{hint}\n" if hint else ""
    rule_help = violation_context.get("rule_help")
    rule_line = f"Rule: {rule_help}\n" if rule_help else ""
    surrounding = violation_context.get("surrounding")
    surrounding_line = f"Surrounding context: {surrounding}\n" if surrounding else ""
    return f"""You are fixing a web accessibility violation.

Issue type: {violation_context['issue_type']}
{rule_line}WCAG criterion: {violation_context.get('wcag_criterion') or 'unknown'}
Failure summary: {violation_context['failure_summary']}
Original HTML:
{_shorten_html(violation_context['original_html'])}
{surrounding_line}{hint_block}
Return the corrected HTML as a SINGLE element: keep the same tag, keep its
existing id/class/name attributes, and only add or change what is needed to
fix the issue. Do not add wrapper elements or extra sibling elements.

Respond as JSON with two fields:
  fix_code: the corrected HTML element
  explanation: one sentence on what changed and why
"""


def _build_sanity_check_prompt(violation_context: dict, fix_code: str) -> str:
    return f"""You are sanity-checking a proposed accessibility fix.

Original HTML:
{_shorten_html(violation_context['original_html'])}

Proposed fix:
{fix_code}

Issue type being fixed: {violation_context['issue_type']}
WCAG criterion: {violation_context.get('wcag_criterion') or 'unknown'}

Does this fix plausibly resolve the stated issue without introducing an
obvious new problem (broken markup, removed functionality, wrong element)?

Respond as JSON with two fields:
  valid: true or false
  reason: one sentence
"""


def _build_attribute_prompt(
    violation_context: dict, attribute: str, has_image: bool = False
) -> str:
    hint = get_hint(violation_context["issue_type"])
    hint_block = f"\nHow to write it:\n{hint}\n" if hint else ""
    rule_help = violation_context.get("rule_help")
    rule_line = f"Rule: {rule_help}\n" if rule_help else ""
    surrounding = violation_context.get("surrounding")
    surrounding_line = (
        f"Surrounding context: {surrounding}\n" if surrounding and not has_image else ""
    )
    image_line = (
        "A picture of this element, as rendered on the page, is attached. "
        "Base your answer on what the picture actually shows.\n"
        if has_image
        else ""
    )
    return f"""You are fixing a web accessibility violation by writing one attribute value.
{image_line}
Issue type: {violation_context['issue_type']}
{rule_line}WCAG criterion: {violation_context.get('wcag_criterion') or 'unknown'}
Failure summary: {violation_context['failure_summary']}
Element:
{_shorten_html(violation_context['original_html'])}
{surrounding_line}{hint_block}
Write the value for the "{attribute}" attribute of this element. Keep it short
and specific, with no surrounding quotes and no HTML.

Respond as JSON with two fields:
  value: the text for the {attribute} attribute
  explanation: one sentence on why this value fits
"""


def _clean_attribute_value(raw: str) -> str:
    value = " ".join(raw.split()).strip().strip("\"'`")
    if not value:
        raise LLMError("model returned an empty attribute value")
    if len(value) > 150 or "<" in value or ">" in value:
        raise LLMError("model returned an unusable attribute value")
    return value


def generate_attribute_fix(
    violation_context: dict, attribute: str, image_b64: str | None = None
) -> FixResult:
    try:
        data = llm_json(
            _build_attribute_prompt(violation_context, attribute, has_image=bool(image_b64)),
            ATTR_SCHEMA,
            images=[image_b64] if image_b64 else None,
        )
        raw_value = data["value"]
        if attribute == "alt" and raw_value.strip().strip("\"'`.").lower() == "decorative":
            value = ""
        else:
            value = _clean_attribute_value(raw_value)
        explanation = data["explanation"].strip()
    except LLMUnavailable:
        raise
    except (LLMError, KeyError, AttributeError):
        return FixResult(
            fix_code=None,
            explanation=None,
            llm_validated=False,
            fix_generation_failed=True,
        )

    fix_code = f'{attribute}="{value}"'
    llm_validated = False
    if SANITY_CHECK:
        try:
            check_data = llm_json(
                _build_sanity_check_prompt(violation_context, fix_code), CHECK_SCHEMA
            )
            llm_validated = bool(check_data.get("valid", False))
        except LLMUnavailable:
            raise
        except LLMError:
            llm_validated = False

    return FixResult(
        fix_code=fix_code,
        explanation=explanation,
        llm_validated=llm_validated,
        fix_generation_failed=False,
        attribute_value=value,
    )


def generate_fix(violation_context: dict) -> FixResult:
    try:
        fix_data = llm_json(_build_fix_prompt(violation_context), FIX_SCHEMA)
        fix_code = fix_data["fix_code"].strip()
        explanation = fix_data["explanation"].strip()
        if not fix_code:
            raise LLMError("model returned an empty fix")
        _check_fix_html_is_safe(fix_code)
    except LLMUnavailable:
        raise
    except (LLMError, KeyError, AttributeError):
        return FixResult(
            fix_code=None,
            explanation=None,
            llm_validated=False,
            fix_generation_failed=True,
        )

    llm_validated = False
    if SANITY_CHECK:
        try:
            check_data = llm_json(
                _build_sanity_check_prompt(violation_context, fix_code),
                CHECK_SCHEMA,
            )
            llm_validated = bool(check_data.get("valid", False))
        except LLMUnavailable:
            raise
        except LLMError:
            llm_validated = False

    return FixResult(
        fix_code=fix_code,
        explanation=explanation,
        llm_validated=llm_validated,
        fix_generation_failed=False,
    )
