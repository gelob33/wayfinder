"""Run model-written Playwright code against a *guarded* page.

The model sees the page and writes short Playwright (sync Python) snippets. Safety is
enforced here, not by the prompt:

* AST screening: no imports, no dunder access, no eval/exec/open/getattr, no `while`.
* The `page` the code receives is a proxy: it never exposes the browser context, network
  routing or init scripts; `goto` is limited to links the agent has seen on allowed domains.
* Typing into password/card/one-time-code fields is refused.
* Clicking submit/pay/send/delete-like elements is blocked in dry-run, or needs human approval.
* A wall-clock budget stops runaway loops.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import Locator, Page

from ..safety.policy import RISKY_LABEL, domain_allowed, normalize_url

MAX_CODE_CHARS = 4000
MAX_OUTPUT_CHARS = 3000
MAX_SECONDS = 45

FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "memoryview", "type", "object", "super",
}
FORBIDDEN_PAGE_ATTRS = {
    "context", "request", "route", "unroute", "route_from_har", "expose_function", "expose_binding",
    "add_init_script", "set_extra_http_headers", "set_content", "frames", "main_frame", "frame",
    "frame_locator", "video", "pdf", "close", "on", "once", "remove_listener", "route_web_socket",
}
SAFE_BUILTINS = {
    n: getattr(builtins, n)
    for n in (
        "range len enumerate zip min max sum sorted reversed abs round any all map filter "
        "str int float bool list dict set tuple repr isinstance print Exception TimeoutError "
        "ValueError KeyError IndexError"
    ).split()
}

_SENSITIVE_JS = """e => {
  const t = (e.getAttribute('type') || '').toLowerCase();
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
  return t === 'password' || /^cc-|one-time-code|current-password|new-password/.test(ac) ||
    (e.tagName === 'INPUT' && /card|cvv|cvc|iban|ssn|otp|one.?time|security.?code|account.?number|routing/i
      .test((e.name || '') + ' ' + (e.id || '')));
}"""
_LABEL_JS = "e => (e.getAttribute('aria-label') || e.innerText || e.value || '').trim().slice(0, 100)"
_ACTIVE_SENSITIVE_JS = "(() => { const e = document.activeElement; return !!e && (" + _SENSITIVE_JS + ")(e); })()"


class GuardError(Exception):
    """An action the policy does not allow (raised inside generated code)."""


def screen(code: str) -> None:
    """Reject code that tries to escape the sandbox. Raises GuardError."""
    if len(code) > MAX_CODE_CHARS:
        raise GuardError(f"code too long (>{MAX_CODE_CHARS} chars); split it into smaller steps")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise GuardError(f"syntax error: {e.msg} (line {e.lineno})")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise GuardError("imports are not allowed; `page`, `nav`, `re`, `json` are already available")
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.While, ast.ClassDef, ast.AsyncFunctionDef, ast.Await)):
            raise GuardError(f"{type(node).__name__} is not allowed (use for-loops over finite lists)")
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            raise GuardError(f"name '{node.id}' is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise GuardError(f"attribute '{node.attr}' is not allowed")


@dataclass
class Policy:
    allowed_domains: list[str]
    seen_urls: Callable[[], set[str]]
    dry_run: bool
    require_approval: bool
    approve: Callable[[str], bool]


class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.time() + seconds

    def check(self) -> None:
        if time.time() > self.deadline:
            raise GuardError(f"code exceeded {MAX_SECONDS}s; do less per call")


class GuardedLocator:
    _TYPING = {"fill", "type", "press_sequentially"}
    _CLICKING = {"click", "dblclick", "tap"}

    def __init__(self, loc: Locator, policy: Policy, budget: _Budget):
        self._loc, self._policy, self._budget = loc, policy, budget

    def _wrap(self, value):
        if isinstance(value, Locator):
            return GuardedLocator(value, self._policy, self._budget)
        return value

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._budget.check()
        attr = getattr(self._loc, name)
        if name in self._TYPING:
            def typed(*a, **k):
                if self._loc.first.evaluate(_SENSITIVE_JS):
                    raise GuardError("refused: that is a password/card/one-time-code field; the person must type it")
                return attr(*a, **k)
            return typed
        if name in self._CLICKING:
            def clicked(*a, **k):
                label = self._loc.first.evaluate(_LABEL_JS)
                if RISKY_LABEL.search(label or ""):
                    if self._policy.dry_run:
                        raise GuardError(f'blocked (dry run): "{label}" would submit/pay/send. Not clicked.')
                    if self._policy.require_approval and not self._policy.approve(f'Click "{label}" (via generated code)'):
                        raise GuardError(f'declined: the person did not approve clicking "{label}".')
                return attr(*a, **k)
            return clicked
        if callable(attr):
            return lambda *a, **k: self._wrap(attr(*a, **k))
        return self._wrap(attr)


class GuardedKeyboard:
    def __init__(self, page: Page, budget: _Budget):
        self._page, self._budget = page, budget

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._budget.check()
        attr = getattr(self._page.keyboard, name)
        if name in ("type", "insert_text"):
            def typed(*a, **k):
                if self._page.evaluate(_ACTIVE_SENSITIVE_JS):
                    raise GuardError("refused: focus is in a password/card/one-time-code field")
                return attr(*a, **k)
            return typed
        return attr


_POINT_LABEL_JS = """([x, y]) => {
  const e = document.elementFromPoint(x, y);
  if (!e) return null;
  const t = e.closest('button, a, input, [role=button], label') || e;
  return (t.getAttribute('aria-label') || t.innerText || t.value || '').trim().slice(0, 100);
}"""
_POINT_SENSITIVE_JS = "([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && (" + _SENSITIVE_JS + ")(e); }"


class GuardedMouse:
    """Coordinate mouse: the element under the pointer gets the same risky-click guard as locators."""

    _PRESSING = {"click", "dblclick", "down"}

    def __init__(self, page: Page, policy: Policy, budget: _Budget):
        self._page, self._policy, self._budget = page, policy, budget
        self._pos = (0.0, 0.0)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._budget.check()
        attr = getattr(self._page.mouse, name)
        if name == "move":
            def moved(x, y, **k):
                self._pos = (x, y)
                return attr(x, y, **k)
            return moved
        if name in self._PRESSING:
            def pressed(*a, **k):
                pt = [a[0], a[1]] if name != "down" and len(a) >= 2 else list(self._pos)
                if name != "down" and len(a) >= 2:
                    self._pos = (a[0], a[1])
                label = self._page.evaluate(_POINT_LABEL_JS, pt)
                if RISKY_LABEL.search(label or ""):
                    if self._policy.dry_run:
                        raise GuardError(f'blocked (dry run): "{label}" would submit/pay/send. Not clicked.')
                    if self._policy.require_approval and not self._policy.approve(f'Mouse click on "{label}" (via generated code)'):
                        raise GuardError(f'declined: the person did not approve clicking "{label}".')
                return attr(*a, **k)
            return pressed
        return attr


class GuardedPage:
    _SELECTOR_ACTIONS = {"fill", "type", "click", "dblclick", "check", "uncheck", "select_option", "hover", "press", "text_content", "inner_text", "is_visible", "is_checked"}

    def __init__(self, page: Page, policy: Policy, budget: _Budget):
        self._page, self._policy, self._budget = page, policy, budget

    def _wrap(self, value):
        return GuardedLocator(value, self._policy, self._budget) if isinstance(value, Locator) else value

    @property
    def mouse(self):
        return GuardedMouse(self._page, self._policy, self._budget)

    @property
    def keyboard(self):
        return GuardedKeyboard(self._page, self._budget)

    def goto(self, url, **k):
        self._budget.check()
        if not domain_allowed(url, self._policy.allowed_domains):
            raise GuardError(f"blocked: {url} is outside the allowed site(s)")
        if normalize_url(url) not in self._policy.seen_urls():
            raise GuardError("blocked: goto only works for URLs of links seen in an observation; click the link instead")
        return self._page.goto(url, **k)

    def __getattr__(self, name):
        if name.startswith("_") or name in FORBIDDEN_PAGE_ATTRS:
            raise AttributeError(f"'{name}' is not available")
        self._budget.check()
        if name in self._SELECTOR_ACTIONS:  # page.click("sel") -> same guards as locator.click()
            def via_locator(selector, *a, **k):
                return getattr(GuardedLocator(self._page.locator(selector).first, self._policy, self._budget), name)(*a, **k)
            return via_locator
        attr = getattr(self._page, name)
        if callable(attr):
            return lambda *a, **k: self._wrap(attr(*a, **k))
        return self._wrap(attr)


def run_code(code: str, page: Page, policy: Policy) -> str:
    """Execute screened code; returns a short text outcome for the model. Never raises GuardError."""
    try:
        screen(code)
    except GuardError as e:
        return f"REJECTED: {e}"

    budget = _Budget(MAX_SECONDS)
    guarded = GuardedPage(page, policy, budget)
    out = io.StringIO()

    def _print(*a, **k):
        k.pop("file", None)
        print(*a, file=out, **k)

    ns: dict = {
        "__builtins__": {**SAFE_BUILTINS, "print": _print},
        "page": guarded,
        "nav": lambda i: GuardedLocator(page.locator(f'[data-nav-id="{int(i)}"]').first, policy, budget),
        "re": re,
        "json": json,
    }
    try:
        exec(compile(code, "<agent>", "exec"), ns)
    except GuardError as e:
        return f"GUARD: {e}" + _output(out)
    except Exception as e:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return f"ERROR: {type(e).__name__}: {first[:240]}" + _output(out)
    result = ns.get("result")
    extra = ""
    if result is not None:
        with contextlib.suppress(Exception):
            extra = "\nresult = " + json.dumps(result, default=str, ensure_ascii=False)[:MAX_OUTPUT_CHARS]
    return "Code ran OK." + _output(out) + extra


def _output(buf: io.StringIO) -> str:
    text = buf.getvalue().strip()
    return ("\nOutput:\n" + text[:MAX_OUTPUT_CHARS]) if text else ""
