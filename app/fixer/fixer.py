import base64
import io
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from axe_playwright_python.sync_playwright import Axe
from PIL import Image
from playwright.sync_api import Page, Route, sync_playwright

from app.fixer.generator import generate_attribute_fix, generate_fix
from app.fixer.llm import SUPPORTS_IMAGES, LLMUnavailable
from app.fixer.rules import get_rule
from app.fixer.wcag import extract_wcag_criterion
from app.security import SSRFBlocked, assert_public_host

logger = logging.getLogger("accesspilot")

CHROMIUM_ARGS = ["--no-sandbox"] if os.environ.get("CHROMIUM_NO_SANDBOX") == "1" else []

PAGE_LOAD_TIMEOUT_MS = 300_000
NODE_TIMEOUT_MS = 10_000
SCAN_TIME_BUDGET_S = int(os.environ.get("SCAN_TIME_BUDGET_S", "240"))
MAX_VIOLATIONS_PROCESSED = 20


class ScanTimeout(Exception):
    pass


def rescan_and_check(page: Page, selector: str) -> list[dict]:
    axe = Axe()
    violations = axe.run(page).response["violations"]

    still_broken = []
    for violation in violations:
        for node in violation["nodes"]:
            if selector in node["target"]:
                still_broken.append(violation["id"])
                break

    return still_broken


_REPLACE_JS = """
(el, html) => {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  if (tpl.content.children.length !== 1) {
    return { ok: false, reason: 'fix must be exactly one HTML element' };
  }
  const fresh = tpl.content.firstElementChild;
  el.replaceWith(fresh);
  window.__ap_undo = () => fresh.replaceWith(el);
  return { ok: true };
}
"""

_ATTR_JS = """
(el, [name, value]) => {
  const prev = el.getAttribute(name);
  el.setAttribute(name, value);
  window.__ap_undo = () => {
    if (prev === null) el.removeAttribute(name);
    else el.setAttribute(name, prev);
  };
  return { ok: true };
}
"""

_REVERT_JS = """
() => {
  if (window.__ap_undo) {
    window.__ap_undo();
    window.__ap_undo = null;
  }
}
"""

_DOCUMENT_LEVEL = {"html", "body"}


_SURROUNDING_JS = r"""
(el) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const p = el.parentElement;
  if (!p) return null;
  const cls = typeof p.className === 'string' && p.className.trim()
    ? '.' + p.className.trim().split(/\s+/).join('.') : '';
  const before = el.previousElementSibling ? clean(el.previousElementSibling.textContent) : '';
  const after = el.nextElementSibling ? clean(el.nextElementSibling.textContent) : '';
  let inside = '';
  if (!before && !after && !['MAIN', 'BODY', 'HTML'].includes(p.tagName)) {
    inside = clean(p.textContent);
  }
  return { parent: p.tagName.toLowerCase() + cls, before, after, inside };
}
"""


def surrounding_context(page: Page, selector: str) -> str | None:
    info = page.locator(selector).evaluate(_SURROUNDING_JS)
    if not info:
        return None
    parts = [f"inside <{info['parent']}>"]
    if info["before"]:
        parts.append(f'text before it: "{info["before"]}"')
    if info["after"]:
        parts.append(f'text after it: "{info["after"]}"')
    if info["inside"]:
        parts.append(f'text in its container: "{info["inside"]}"')
    return "; ".join(parts)


def element_screenshot(page: Page, selector: str) -> bytes | None:
    try:
        return page.locator(selector).screenshot(type="png", timeout=5000)
    except Exception:
        return None


def is_blank_image(png: bytes) -> bool:
    try:
        colors = Image.open(io.BytesIO(png)).convert("RGB").getcolors(maxcolors=16)
    except Exception:
        return False
    return colors is not None and len(colors) == 1


def build_violation_context(
    violation: dict, node: dict, surrounding: str | None = None
) -> dict:
    return {
        "issue_type": violation["id"],
        "rule_help": violation.get("help"),
        "wcag_criterion": extract_wcag_criterion(violation.get("tags", [])),
        "failure_summary": node.get("failureSummary") or violation["help"],
        "original_html": node["html"],
        "surrounding": surrounding,
    }


def apply_generated_fix(page: Page, selector: str, fix_code: str) -> tuple[bool, str]:
    result = page.locator(selector).evaluate(_REPLACE_JS, fix_code)
    return result["ok"], result.get("reason", "")


def revert_fix(page: Page) -> None:
    page.evaluate(_REVERT_JS)


def _normalize(html: str) -> str:
    return " ".join(html.split())


def _selector_of(node: dict) -> str:
    target = node["target"]
    return " >> ".join(str(t) for t in target)


def _is_simple_target(node: dict) -> bool:
    target = node["target"]
    return len(target) == 1 and isinstance(target[0], str)


def _new_result(violation: dict, node: dict, strategy: str) -> dict:
    return {
        "rule_id": violation["id"],
        "wcag_criterion": extract_wcag_criterion(violation.get("tags", [])),
        "severity": violation.get("impact"),
        "strategy": strategy,
        "selector": _selector_of(node),
        "original_html": node["html"],
        "fix_code": None,
        "explanation": None,
        "llm_validated": False,
        "rescan_validated": False,
        "needs_review": False,
        "status": None,
        "detail": "",
    }


def _decide(page: Page, selector: str, rule_id: str, rules_before: set, result: dict) -> dict:
    rules_after = set(rescan_and_check(page, selector))
    introduced = sorted(rules_after - rules_before)
    passed = rule_id not in rules_after and not introduced
    result["rescan_validated"] = passed

    if passed:
        result["status"] = "verified"
        return result

    revert_fix(page)
    result["status"] = "not_verified"
    notes = []
    if rule_id in rules_after:
        notes.append(f"'{rule_id}' still fires on the element")
    if introduced:
        notes.append(f"fix introduced: {introduced}")
    result["detail"] = "; ".join(notes)
    return result


def fix_and_verify_node(page: Page, violation: dict, node: dict) -> dict:
    rule_id = violation["id"]
    rule = get_rule(rule_id)
    strategy = rule["strategy"]
    result = _new_result(violation, node, strategy)

    if strategy != "manual" and not _is_simple_target(node):
        result["strategy"] = "manual"
        result["status"] = "manual_review"
        result["detail"] = (
            "element is inside an iframe or shadow DOM; not attempted "
            f"({violation['help']}; {violation.get('helpUrl', '')})"
        )
        return result

    if strategy == "manual":
        result["status"] = "manual_review"
        result["detail"] = f"{violation['help']} ({violation.get('helpUrl', '')})"
        return result

    selector = node["target"][0]
    rules_before = set(rescan_and_check(page, selector))

    if strategy == "deterministic":
        fix = rule["fix"](violation, node)
        if fix is None:
            result["status"] = "manual_review"
            result["detail"] = (
                "couldn't compute a safe automatic fix "
                f"({violation['help']}; {violation.get('helpUrl', '')})"
            )
            return result

        result.update(
            fix_code=fix.fix_code,
            explanation=fix.explanation,
            needs_review=fix.needs_review,
        )
        outcome = page.locator(selector).evaluate(fix.js, fix.arg)
        if not outcome.get("ok"):
            result.update(status="not_applicable", detail=outcome.get("reason", ""))
            return result
        return _decide(page, selector, rule_id, rules_before, result)

    if selector in _DOCUMENT_LEVEL:
        result["strategy"] = "manual"
        result["status"] = "manual_review"
        result["detail"] = (
            "violation is on the whole document; not attempted by the model "
            f"({violation['help']}; {violation.get('helpUrl', '')})"
        )
        return result

    context = build_violation_context(
        violation, node, surrounding=surrounding_context(page, selector)
    )

    if rule.get("mode") == "attribute":
        attribute = rule["attribute"]
        image_b64 = None
        if rule.get("vision"):
            png = element_screenshot(page, selector)
            if png is not None:
                if attribute == "alt" and is_blank_image(png):
                    result.update(
                        fix_code='alt=""',
                        explanation=(
                            "The image has no visible content (a single uniform "
                            "color), so it was marked decorative with empty alt text."
                        ),
                        needs_review=bool(rule.get("needs_review")),
                    )
                    page.locator(selector).evaluate(_ATTR_JS, [attribute, ""])
                    return _decide(page, selector, rule_id, rules_before, result)
                if SUPPORTS_IMAGES:
                    image_b64 = base64.b64encode(png).decode("ascii")

        fix = generate_attribute_fix(context, attribute, image_b64=image_b64)
        if fix.fix_generation_failed:
            result["status"] = "fix_generation_failed"
            return result

        result.update(
            fix_code=fix.fix_code,
            explanation=fix.explanation,
            llm_validated=fix.llm_validated,
            needs_review=bool(rule.get("needs_review")),
        )
        page.locator(selector).evaluate(_ATTR_JS, [attribute, fix.attribute_value])
        return _decide(page, selector, rule_id, rules_before, result)

    fix = generate_fix(context)
    if fix.fix_generation_failed:
        result["status"] = "fix_generation_failed"
        return result

    result.update(
        fix_code=fix.fix_code,
        explanation=fix.explanation,
        llm_validated=fix.llm_validated,
    )

    original_tag = page.locator(selector).evaluate("(el) => el.tagName")
    original_inner = _normalize(page.locator(selector).evaluate("(el) => el.innerHTML"))
    ok, reason = apply_generated_fix(page, selector, fix.fix_code)
    if not ok:
        result.update(status="not_applicable", detail=reason)
        return result

    matches = page.locator(selector).count()
    new_tag = None
    new_inner = None
    if matches:
        new_tag = page.locator(selector).first.evaluate("(el) => el.tagName")
        new_inner = _normalize(page.locator(selector).first.evaluate("(el) => el.innerHTML"))
    if matches != 1 or new_tag != original_tag:
        revert_fix(page)
        result.update(
            status="not_applicable",
            detail=(
                f"element identity changed (selector matches {matches}, "
                f"tag {original_tag} -> {new_tag})"
            ),
        )
        return result

    if new_inner != original_inner:
        revert_fix(page)
        result.update(
            status="not_applicable",
            detail="fix changed the element's inner content; not applied",
        )
        return result

    return _decide(page, selector, rule_id, rules_before, result)


def resolve_target(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target
    return Path(target).resolve().as_uri()


_SEVERITY_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


def _sort_key(pair: tuple[dict, dict]) -> tuple[int, tuple[int, ...]]:
    violation, _node = pair
    severity_rank = _SEVERITY_ORDER.get(violation.get("impact"), 4)
    wcag = extract_wcag_criterion(violation.get("tags", [])) or "9.9.9"
    return (severity_rank, tuple(int(part) for part in wcag.split(".")))


def _install_request_guard(page: Page, blocked: list[str]) -> None:
    host_ok: dict[str, bool] = {}

    def handle(route: Route) -> None:
        url = route.request.url
        scheme = urlparse(url).scheme
        if scheme in ("data", "blob", "about"):
            route.continue_()
            return
        host = urlparse(url).hostname or ""
        if host not in host_ok:
            try:
                assert_public_host(url)
                host_ok[host] = True
            except Exception:
                # Any failure (SSRFBlocked, UnicodeError, ...) blocks the
                # request; never let an exception escape a route handler.
                host_ok[host] = False
        if host_ok[host]:
            route.continue_()
        else:
            blocked.append(url)
            route.abort("blockedbyclient")

    page.route("**/*", handle)


def _error_result(violation: dict, node: dict) -> dict:
    result = _new_result(violation, node, get_rule(violation["id"])["strategy"])
    result["status"] = "error"
    result["detail"] = "this element could not be processed automatically"
    return result


def fix_page(fixture_path: str) -> dict:
    url = resolve_target(fixture_path)
    results = []
    blocked: list[str] = []
    started = time.monotonic()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            page = browser.new_page()
            if url.startswith(("http://", "https://")):
                _install_request_guard(page, blocked)
            try:
                page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as e:
                if blocked:
                    raise SSRFBlocked(
                        "The page redirected to, or required, a non-public "
                        "address; internal and private targets can't be scanned."
                    ) from e
                raise ScanTimeout(
                    "The page failed to load or didn't finish loading "
                    f"within {PAGE_LOAD_TIMEOUT_MS // 1000}s."
                ) from e

            page.set_default_timeout(NODE_TIMEOUT_MS)
            violations = Axe().run(page).response["violations"]

            all_pairs = [
                (violation, node)
                for violation in violations
                for node in violation["nodes"]
            ]
            all_pairs.sort(key=_sort_key)

            total_found = len(all_pairs)
            for violation, node in all_pairs[:MAX_VIOLATIONS_PROCESSED]:
                if time.monotonic() - started > SCAN_TIME_BUDGET_S:
                    logger.warning("Scan time budget reached for %s", url)
                    break
                try:
                    results.append(fix_and_verify_node(page, violation, node))
                except LLMUnavailable:
                    raise
                except Exception:
                    logger.exception("Failed processing %s", _selector_of(node))
                    try:
                        revert_fix(page)
                    except Exception:
                        pass
                    results.append(_error_result(violation, node))
        finally:
            browser.close()

    return {
        "total_violations_found": total_found,
        "violations_processed": len(results),
        "results": results,
    }
