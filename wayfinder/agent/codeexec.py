"""Run model-written Playwright code against a *guarded* page (opt-in: --allow-code).

IMPORTANT: this is defence in depth, NOT a security boundary. Python cannot be sandboxed
reliably in-process, so the tool is disabled unless the person passes --allow-code, and it
should only be enabled for sites they trust. What the guards do:

* AST screening: no imports, no underscore/frame attributes, no eval/exec/open/getattr,
  no `while`, no str.format (attribute access through format strings).
* No modules in scope: `re` and `json` are small facades exposing a few functions only.
* The `page` the code receives is a proxy with a default-deny return filter: only plain data
  and (wrapped) locators come back, so the raw Page, BrowserContext, frames and element
  handles are unreachable. In-page JavaScript (`evaluate`, `dispatch_event`, ...) is blocked.
* Typing into / reading password, card and one-time-code fields is refused.
* Clicks on submit/pay/send-like elements, submit controls of POST forms, and Enter inside
  POST forms are blocked in dry-run, or need human approval.
* `goto` is limited to links the agent has seen on allowed domains; a time budget stops loops.
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
from types import SimpleNamespace
from typing import Callable

from playwright.sync_api import Locator, Page

from ..safety.pagechecks import (
    ACTIVATE_KEYS,
    ACTIVE_BUTTON_LABEL_JS,
    ACTIVE_IN_POST_FORM_JS,
    ACTIVE_SENSITIVE_JS,
    BUTTON_LABEL_JS,
    ENTER_KEYS,
    IN_POST_FORM_JS,
    IS_SUBMIT_CONTROL_JS,
    LABEL_JS,
    SENSITIVE_JS,
)
from ..safety.policy import RISKY_LABEL, domain_allowed, normalize_url

MAX_CODE_CHARS = 4000
MAX_OUTPUT_CHARS = 3000
MAX_SECONDS = 45

FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "memoryview", "type", "object", "super",
}
# Attribute names that reach interpreter internals or read attributes via strings.
FORBIDDEN_ATTR_PREFIXES = ("_", "gi_", "cr_", "ag_", "f_", "tb_", "co_")
FORBIDDEN_ATTRS = {"format", "format_map", "mro", "subclasses"}
# Never available on the page or on locators (in-page JS, raw handles, context, network, ...).
FORBIDDEN_MEMBERS = {
    "context", "request", "route", "unroute", "route_from_har", "expose_function", "expose_binding",
    "add_init_script", "set_extra_http_headers", "set_content", "frames", "main_frame", "frame",
    "frame_locator", "content_frame", "video", "pdf", "close", "on", "once", "remove_listener",
    "route_web_socket", "evaluate", "evaluate_all", "evaluate_handle", "dispatch_event", "add_script_tag",
    "add_style_tag", "wait_for_function", "set_input_files", "element_handle", "element_handles",
    "query_selector", "query_selector_all", "wait_for_selector", "page", "owner", "workers", "opener",
    "screenshot", "pause", "bring_to_front", "emulate_media", "set_viewport_size", "touchscreen",
    "request_gc", "console_messages", "page_errors", "requests", "clock",
}
# Navigation helpers whose return value (a Response object) is dropped.
RETURNS_NOTHING = {"reload", "go_back", "go_forward", "wait_for_url", "wait_for_load_state"}

SAFE_BUILTINS = {
    n: getattr(builtins, n)
    for n in (
        "range len enumerate zip min max sum sorted reversed abs round any all map filter "
        "str int float bool list dict set tuple repr isinstance print Exception TimeoutError "
        "ValueError KeyError IndexError"
    ).split()
}
PLAIN = (str, int, float, bool, type(None))

_POINT = "([x, y]) => { let e = document.elementFromPoint(x, y); if (!e) return %s; e = e.closest('button, a, input, [role=button], label') || e; return (%s)(e); }"
_POINT_LABEL_JS = _POINT % ("null", LABEL_JS)
_POINT_SUBMITS_POST_JS = _POINT % ("false", "e => (" + IS_SUBMIT_CONTROL_JS + ")(e) && (" + IN_POST_FORM_JS + ")(e)")


class GuardError(Exception):
    """An action the policy does not allow (raised inside generated code)."""


def screen(code: str) -> None:
    """Reject code that tries to escape the guards. Raises GuardError."""
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
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith(FORBIDDEN_ATTR_PREFIXES) or node.attr in FORBIDDEN_ATTRS
        ):
            raise GuardError(f"attribute '{node.attr}' is not allowed (use f-strings instead of .format)")


@dataclass
class Policy:
    allowed_domains: list[str]
    seen_urls: Callable[[], set[str]]
    dry_run: bool
    require_approval: bool
    approve: Callable[[str], bool]

    def gate(self, what: str) -> None:
        """Dry-run block / human approval for something that may not be reversible."""
        if self.dry_run:
            raise GuardError(f"blocked (dry run): {what} would submit/pay/send. Not done.")
        if self.require_approval and not self.approve(f"{what} (via generated code)"):
            raise GuardError(f"declined: the person did not approve: {what}.")


class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.time() + seconds

    def check(self) -> None:
        if time.time() > self.deadline:
            raise GuardError(f"code exceeded {MAX_SECONDS}s; do less per call")


class _Guarded:
    """Shared plumbing: member deny-list, time budget, default-deny filter on return values."""

    _policy: Policy
    _budget: _Budget

    def _enter(self, name: str) -> None:
        if name.startswith("_") or name in FORBIDDEN_MEMBERS:
            raise AttributeError(f"'{name}' is not available in generated code")
        self._budget.check()

    def _safe(self, value, name: str):
        if isinstance(value, Locator):
            return GuardedLocator(value, self._policy, self._budget)
        if isinstance(value, PLAIN):
            return value
        if isinstance(value, (list, tuple)):
            return [self._safe(v, name) for v in value]
        if isinstance(value, dict):
            return {k: self._safe(v, name) for k, v in value.items() if isinstance(k, PLAIN)}
        raise GuardError(f"'{name}' returns a {type(value).__name__}, which is not available in generated code")

    def _call(self, attr, name: str):
        if not callable(attr):
            return self._safe(attr, name)

        def call(*a, **k):
            result = attr(*a, **k)
            return None if name in RETURNS_NOTHING else self._safe(result, name)

        return call


class GuardedLocator(_Guarded):
    _TYPING = {"fill", "type", "press_sequentially", "clear"}
    _READING = {"input_value"}
    _CLICKING = {"click", "dblclick", "tap"}

    def __init__(self, loc: Locator, policy: Policy, budget: _Budget):
        self._loc, self._policy, self._budget = loc, policy, budget

    def _is(self, js: str) -> bool:
        return bool(self._loc.first.evaluate(js))

    def __getattr__(self, name):
        self._enter(name)
        attr = getattr(self._loc, name)
        if name in self._TYPING or name in self._READING:
            def guarded_io(*a, **k):
                if self._is(SENSITIVE_JS):
                    raise GuardError("refused: that is a password/card/one-time-code field; only the person may use it")
                return self._safe(attr(*a, **k), name)
            return guarded_io
        if name in self._CLICKING:
            def clicked(*a, **k):
                label = self._loc.first.evaluate(LABEL_JS) or ""
                if RISKY_LABEL.search(label) or (self._is(IS_SUBMIT_CONTROL_JS) and self._is(IN_POST_FORM_JS)):
                    self._policy.gate(f'Click "{label}"')
                return self._safe(attr(*a, **k), name)
            return clicked
        if name == "press":
            def pressed(key, *a, **k):
                if len(str(key)) == 1 and self._is(SENSITIVE_JS):
                    raise GuardError("refused: that is a password/card/one-time-code field; only the person may type there")
                k_ = str(key).lower()
                label = self._loc.first.evaluate(BUTTON_LABEL_JS) if k_ in ACTIVATE_KEYS else ""
                if label and RISKY_LABEL.search(label):
                    self._policy.gate(f'Activate "{label}" ({key})')
                elif k_ in ENTER_KEYS and self._is(IN_POST_FORM_JS):
                    self._policy.gate("Submit the form (Enter)")
                return self._safe(attr(key, *a, **k), name)
            return pressed
        return self._call(attr, name)


class GuardedKeyboard(_Guarded):
    def __init__(self, page: Page, policy: Policy, budget: _Budget):
        self._page, self._policy, self._budget = page, policy, budget

    def __getattr__(self, name):
        self._enter(name)
        attr = getattr(self._page.keyboard, name)
        if name in ("type", "insert_text"):
            def typed(*a, **k):
                if self._page.evaluate(ACTIVE_SENSITIVE_JS):
                    raise GuardError("refused: focus is in a password/card/one-time-code field")
                return attr(*a, **k)
            return typed
        if name in ("press", "down"):
            def pressed(key, *a, **k):
                if len(str(key)) == 1 and self._page.evaluate(ACTIVE_SENSITIVE_JS):
                    raise GuardError("refused: focus is in a password/card/one-time-code field")
                k_ = str(key).lower()
                label = self._page.evaluate(ACTIVE_BUTTON_LABEL_JS) if k_ in ACTIVATE_KEYS else ""
                if label and RISKY_LABEL.search(label):
                    self._policy.gate(f'Activate "{label}" ({key})')
                elif k_ in ENTER_KEYS and self._page.evaluate(ACTIVE_IN_POST_FORM_JS):
                    self._policy.gate("Submit the focused form (Enter)")
                return attr(key, *a, **k)
            return pressed
        return self._call(attr, name)


class GuardedMouse(_Guarded):
    """Coordinate mouse: the element under the pointer gets the same guards as locator clicks."""

    _PRESSING = {"click", "dblclick", "down"}

    def __init__(self, page: Page, policy: Policy, budget: _Budget):
        self._page, self._policy, self._budget = page, policy, budget
        self._pos = (0.0, 0.0)

    def __getattr__(self, name):
        self._enter(name)
        attr = getattr(self._page.mouse, name)
        if name == "move":
            def moved(x, y, **k):
                self._pos = (x, y)
                return attr(x, y, **k)
            return moved
        if name in self._PRESSING:
            def pressed(*a, **k):
                if name != "down" and len(a) >= 2:
                    self._pos = (a[0], a[1])
                pt = list(self._pos)
                label = self._page.evaluate(_POINT_LABEL_JS, pt) or ""
                if RISKY_LABEL.search(label) or self._page.evaluate(_POINT_SUBMITS_POST_JS, pt):
                    self._policy.gate(f'Mouse click on "{label}"')
                return attr(*a, **k)
            return pressed
        return self._call(attr, name)


class GuardedPage(_Guarded):
    # page.click("sel") etc. are routed through a guarded locator so the same checks apply.
    _SELECTOR_ACTIONS = {
        "fill", "type", "click", "dblclick", "tap", "check", "uncheck", "set_checked", "select_option", "hover",
        "press", "focus", "text_content", "inner_text", "inner_html", "input_value", "get_attribute",
        "is_visible", "is_hidden", "is_checked", "is_enabled", "is_disabled", "is_editable",
    }

    def __init__(self, page: Page, policy: Policy, budget: _Budget):
        self._page, self._policy, self._budget = page, policy, budget

    @property
    def mouse(self):
        return GuardedMouse(self._page, self._policy, self._budget)

    @property
    def keyboard(self):
        return GuardedKeyboard(self._page, self._policy, self._budget)

    def goto(self, url, **k):
        self._budget.check()
        if not domain_allowed(url, self._policy.allowed_domains):
            raise GuardError(f"blocked: {url} is outside the allowed site(s)")
        if normalize_url(url) not in self._policy.seen_urls():
            raise GuardError("blocked: goto only works for URLs of links seen in an observation; click the link instead")
        self._page.goto(url, **k)

    def __getattr__(self, name):
        self._enter(name)
        if name in self._SELECTOR_ACTIONS:
            def via_locator(selector, *a, **k):
                return getattr(GuardedLocator(self._page.locator(selector).first, self._policy, self._budget), name)(*a, **k)
            return via_locator
        return self._call(getattr(self._page, name), name)


def _facades() -> dict:
    """`re` and `json` without the modules themselves (modules lead back to sys/os/builtins)."""
    flags = {n: int(getattr(re, n)) for n in ("IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S")}
    re_ns = SimpleNamespace(
        search=re.search, match=re.match, fullmatch=re.fullmatch, findall=re.findall,
        sub=re.sub, split=re.split, escape=re.escape, **flags,
    )
    json_ns = SimpleNamespace(dumps=json.dumps, loads=json.loads)
    return {"re": re_ns, "json": json_ns}


def run_code(code: str, page: Page, policy: Policy) -> str:
    """Execute screened code; returns a short text outcome for the model. Never raises GuardError."""
    try:
        screen(code)
    except GuardError as e:
        return f"REJECTED: {e}"

    budget = _Budget(MAX_SECONDS)
    out = io.StringIO()

    def _print(*a, **k):
        k.pop("file", None)
        print(*a, file=out, **k)

    ns: dict = {
        "__builtins__": {**SAFE_BUILTINS, "print": _print},
        "page": GuardedPage(page, policy, budget),
        "nav": lambda i: GuardedLocator(page.locator(f'[data-nav-id="{int(i)}"]').first, policy, budget),
        **_facades(),
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
