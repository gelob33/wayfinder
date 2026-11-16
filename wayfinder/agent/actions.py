"""Act: execute one browser action. Guards (sensitive fields, risky clicks, domains) live here."""

from __future__ import annotations

from ..hilt.human import AbortRun
from ..safety.policy import domain_allowed, is_risky_click, normalize_url
from .codeexec import Policy, run_code
from .state import Ctx, RunState


def execute(ctx: Ctx, state: RunState, name: str, args: dict) -> str:
    try:
        return _do(ctx, state, name, args)
    except AbortRun:
        raise
    except Exception as e:  # Playwright timeouts, detached elements, ...
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return f"ERROR: {first[:200]}"


def _locator(ctx: Ctx, nav_id: int):
    return ctx.session.page.locator(f'[data-nav-id="{int(nav_id)}"]').first


def _click(ctx: Ctx, el: dict) -> str:
    page = ctx.session.page
    loc = _locator(ctx, el["id"])
    before_url = page.url
    is_toggle = el["tag"] == "input" and el.get("type") in ("checkbox", "radio")
    try:
        loc.click(timeout=4000)
    except Exception:
        # Styled checkboxes are often 1x1px behind an overlay that intercepts the click.
        if is_toggle:
            if el.get("type") == "checkbox" and el.get("checked"):
                loc.uncheck(force=True, timeout=4000)
            else:
                loc.check(force=True, timeout=4000)
        else:
            loc.click(force=True, timeout=4000)
    page.wait_for_timeout(600)
    msg = f'Clicked [{el["id"]}] "{el["label"]}".'
    if is_toggle:
        try:
            msg += " It is now " + ("checked." if loc.is_checked() else "unchecked.")
        except Exception:
            pass
    ctx.session.settle()
    return msg


def _do(ctx: Ctx, state: RunState, name: str, a: dict) -> str:
    page = ctx.session.page
    snap = state.snap
    el = None
    if name in ("click", "type", "select"):
        el = snap.element(int(a.get("id", -1)))
        if el is None:
            return f"ERROR: no element with id {a.get('id')} in the latest observation."
        if el.get("disabled"):
            return f"ERROR: element [{el['id']}] is disabled."

    if name == "click":
        if is_risky_click(el):
            if ctx.settings.dry_run:
                return f'BLOCKED (dry run): "{el["label"]}" would submit/pay/send. Not clicked; report what would happen.'
            if ctx.settings.require_approval and not ctx.human.approve(
                f'Click "{el["label"]}" on {snap.url}\n  (task: {ctx.task.goal})'
            ):
                return f'DECLINED: the person did not approve clicking "{el["label"]}".'
        return _click(ctx, el)

    if name == "type":
        if el.get("sensitive"):
            return "REFUSED: that is a SENSITIVE field (password/card/code). The person must enter it themselves."
        loc = _locator(ctx, el["id"])
        loc.fill(str(a.get("text", "")))
        if a.get("submit"):
            loc.press("Enter")
            page.wait_for_timeout(400)
        return f'Typed into [{el["id"]}]' + (" and pressed Enter." if a.get("submit") else ".")

    if name == "select":
        loc = _locator(ctx, el["id"])
        try:
            loc.select_option(label=str(a["option"]))
        except Exception:
            loc.select_option(value=str(a["option"]))
        return f'Selected "{a["option"]}" in [{el["id"]}].'

    if name == "press":
        page.keyboard.press(str(a["key"]))
        page.wait_for_timeout(300)
        return f'Pressed {a["key"]}.'

    if name == "scroll":
        sign = -1 if a.get("direction") == "up" else 1
        page.evaluate(f"window.scrollBy(0, {sign} * innerHeight * 0.8)")
        page.wait_for_timeout(250)
        return f'Scrolled {a.get("direction", "down")}.'

    if name == "goto":
        url = str(a["url"])
        if not domain_allowed(url, ctx.allowed):
            return f"BLOCKED: {url} is outside the allowed site(s): {', '.join(ctx.allowed)}."
        if normalize_url(url) not in state.seen_urls:
            return (
                "BLOCKED: goto only works for URLs of links you have seen in an observation. "
                "Do not guess URLs: use the navigation menu, links or the page's own controls instead."
            )
        page.goto(url)
        return f"Navigated to {url}."

    if name == "back":
        page.go_back()
        return "Went back."

    if name == "wait":
        page.wait_for_timeout(int(min(max(float(a.get("seconds", 1)), 0), 10) * 1000))
        return "Waited."

    if name == "run_playwright":
        policy = Policy(
            allowed_domains=ctx.allowed,
            seen_urls=lambda: state.seen_urls,
            dry_run=ctx.settings.dry_run,
            require_approval=ctx.settings.require_approval,
            approve=ctx.human.approve,
        )
        outcome = run_code(str(a.get("code", "")), ctx.session.page, policy)
        ctx.session.page.wait_for_timeout(500)
        return outcome

    if name == "ask_human":
        answer = ctx.human.ask(str(a["question"]))
        return f"The person answered: {answer}"

    return f"ERROR: unknown tool {name!r}."
