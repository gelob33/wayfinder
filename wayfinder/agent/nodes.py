"""Graph nodes: observe -> decide -> (plan | act -> verify | done).

Each node takes the shared ``RunState`` (and the ``Ctx`` of dependencies) and updates it.
Observe and verify are plain code, so the model cannot skip them.
"""

from __future__ import annotations

import time
from dataclasses import asdict

import anthropic
from playwright.sync_api import Error as PlaywrightError

from ..hilt.human import AbortRun
from ..perception.diff import describe_change
from ..perception.snapshot import Snapshot, take_snapshot
from ..safety.policy import domain_allowed, host_of, is_bot_challenge, is_sign_in_url, normalize_url
from . import actions
from .prompts import system_prompt
from .state import ChecklistItem, Ctx, RunResult, RunState
from .tools import tools_for

MAX_LOGIN_PAUSES = 5
MAX_NUDGES = 3
MAX_CHALLENGE_PAUSES = 2
MAX_DONE_REJECTIONS = 2
# Actions whose success should visibly change the page; "no change" is then worth flagging.
EXPECT_CHANGE = {"click", "type", "select", "press", "goto", "back", "run_playwright"}


# ------------------------------------------------------------------ helpers
def finish(ctx: Ctx, state: RunState, status: str, summary: str, result: dict | None = None) -> RunState:
    res = RunResult(
        status=status,
        summary=summary,
        result=result,
        steps=state.step,
        tokens=state.tokens,
        run_dir=str(ctx.runlog.dir),
        checklist=[asdict(i) for i in state.checklist],
    )
    ctx.runlog.result(asdict(res))
    state.result = res
    return state


def checklist_text(state: RunState) -> str:
    if not state.checklist:
        return ""
    return "Checklist:\n" + "\n".join(f"  {i}. [{'x' if c.done else ' '}] {c.text}" for i, c in enumerate(state.checklist, 1))


def reply(state: RunState, content: str) -> None:
    """Answer the pending tool call (every tool_use needs a tool_result)."""
    state.messages.append(
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": state.call.id, "content": content}]}
    )


def short(args: dict) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return s if len(s) < 70 else s[:67] + "..."


# ------------------------------------------------------------------ observe
def _snap(ctx: Ctx) -> Snapshot:
    """Snapshot the current tab; if it was closed meanwhile, retry on the tab the session falls back to."""
    try:
        return take_snapshot(ctx.session.page, allowed=ctx.allowed)
    except PlaywrightError as e:
        if "has been closed" in str(e) or "Target closed" in str(e):
            ctx.session.settle()
            return take_snapshot(ctx.session.page, allowed=ctx.allowed)
        raise


def observe(ctx: Ctx, state: RunState) -> RunState:
    """Take a fresh snapshot (handling off-site redirects and sign-in pauses)."""
    state.prev_snap = state.snap
    notes: list[str] = []
    page = ctx.session.page
    url = page.url
    if url != "about:blank" and not domain_allowed(url, ctx.allowed + sorted(state.approved_hosts)):
        if _signin_host_approved(ctx, state, url):
            notes.append(f"The person approved signing in at {host_of(url)}.")
        else:
            _leave_offsite(ctx, page)
            notes.append(f"Navigation to {url} was blocked (outside the allowed site); returned to the previous page.")
    snap = _snap(ctx)
    if is_bot_challenge(snap.title, snap.text):
        # Never fight bot protection: hand off to the person once or twice, then stop cleanly.
        if state.challenge_pauses < MAX_CHALLENGE_PAUSES:
            state.challenge_pauses += 1
            ctx.runlog.step(event="challenge_pause", url=snap.url)
            ctx.human.wait_for_challenge(snap.url)
            ctx.session.settle()
            snap = _snap(ctx)
        if is_bot_challenge(snap.title, snap.text):
            state.snap = snap
            ctx.runlog.observation(state.step, snap.render())
            ctx.session.screenshot(ctx.runlog.screenshot_path(state.step))
            return finish(
                ctx, state, "blocked",
                "The site's bot protection is blocking this automated browser (verification page keeps appearing). "
                "The agent does not try to bypass it. Options: complete the task manually, or use an official API/export "
                "if the site offers one.",
            )
    if (
        state.login_pauses < MAX_LOGIN_PAUSES
        and (snap.has_password or is_sign_in_url(snap.url))
        and snap.url != state.last_login_url
    ):
        state.last_login_url = snap.url
        state.login_pauses += 1
        ctx.runlog.step(event="login_pause", url=snap.url)
        ctx.human.wait_for_login(snap.url)
        ctx.session.settle()
        snap = _snap(ctx)
        notes.append("The person signed in manually in the browser. Continue with the goal.")
    notes += [f"Browser {d}" for d in ctx.session.pop_notes()]
    snap.notes = notes
    state.seen_urls |= snap.link_urls | {normalize_url(snap.url)}
    state.snap = snap
    return state


def _signin_host_approved(ctx: Ctx, state: RunState, url: str) -> bool:
    """An off-site page that looks like a sign-in page is only trusted if the person approves its host."""
    if not is_sign_in_url(url):
        return False
    host = host_of(url)
    if host in state.declined_hosts:
        return False
    ctx.runlog.step(event="offsite_signin_prompt", host=host)
    if ctx.human.approve_domain(host, url):
        state.approved_hosts.add(host)
        return True
    state.declined_hosts.add(host)
    return False


def _leave_offsite(ctx: Ctx, page) -> None:
    try:
        others = [p for p in ctx.session.context.pages if p is not page and not p.is_closed()]
        if ctx.session.cdp_url and others:
            ctx.session.page = others[-1]  # attached to the person's browser: never close their tabs
        elif len(ctx.session.context.pages) > 1:
            page.close()
            ctx.session.page = ctx.session.context.pages[-1]
        else:
            page.go_back()
    except Exception:
        ctx.session.page.goto(ctx.task.url)
    ctx.session.settle()


# ------------------------------------------------------------------- decide
def decide(ctx: Ctx, state: RunState) -> RunState:
    """Ask the model for the next tool call. Sets ``state.call`` (or finishes the run)."""
    state.call = None
    if time.time() - state.started > ctx.settings.max_seconds:
        return finish(ctx, state, "budget_exceeded", "Time budget exceeded.")
    if state.tokens > ctx.settings.max_total_tokens:
        return finish(ctx, state, "budget_exceeded", "Token budget exceeded.")
    try:
        allow = ctx.settings.allow_code
        response = ctx.llm.create(system_prompt(allow), tools_for(allow), state.messages)
    except anthropic.APIError as e:
        return finish(ctx, state, "error", f"LLM API error: {e}")
    u = response.usage
    state.tokens += (u.input_tokens or 0) + (u.output_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    if response.stop_reason == "refusal":
        return finish(ctx, state, "error", "The model declined this request.")
    state.messages.append({"role": "assistant", "content": response.content})
    call = next((b for b in response.content if b.type == "tool_use"), None)
    if call is None:
        state.nudges += 1
        if state.nudges > MAX_NUDGES:
            return finish(ctx, state, "failed", "The agent stopped without finishing.")
        state.messages.append({"role": "user", "content": "Continue by calling a tool. Call `done` when finished."})
        return state
    state.call = call
    return state


# --------------------------------------------------------------------- plan
def plan_node(ctx: Ctx, state: RunState) -> RunState:
    a = dict(state.call.input or {})
    incoming = [str(x).strip() for x in (a.get("checklist") or []) if str(x).strip()]
    if not incoming and not state.checklist:
        reply(state, "REJECTED: include a `checklist` of the concrete criteria the goal requires (what must be true when you are done).")
        ctx.runlog.step(step=state.step, action="plan", args=a, outcome="rejected: no checklist")
        return state
    if incoming:
        done_before = {c.text.lower() for c in state.checklist if c.done}
        state.checklist = [ChecklistItem(t, t.lower() in done_before) for t in incoming]
    for idx in a.get("completed") or []:
        try:
            state.checklist[int(idx) - 1].done = True
        except (ValueError, IndexError):
            pass
    state.plan = {
        "page_summary": a.get("page_summary", ""),
        "controls_to_use": a.get("controls_to_use", []),
        "steps": a.get("steps", []),
    }
    ctx.runlog.step(step=state.step, action="plan", args=a, checklist=[asdict(c) for c in state.checklist])
    ctx.out(f"[{state.step}] plan: {a.get('page_summary', '')[:90]}")
    for s in a.get("steps", [])[:8]:
        ctx.out(f"      - {s}")
    reply(state, "Plan recorded.\n" + checklist_text(state) + "\nNow act on the plan (page unchanged).")
    return state


# ---------------------------------------------------------------------- act
def act_node(ctx: Ctx, state: RunState) -> tuple[RunState, bool]:
    """Execute the call. Returns (state, executed); when not executed the reply is already sent."""
    name, args = state.call.name, dict(state.call.input or {})
    if state.plan is None and name != "ask_human":
        msg = "REJECTED: call `plan` first (describe the page's controls, map them to the goal, give a checklist)."
        ctx.runlog.step(step=state.step, action=name, args=args, outcome=msg)
        ctx.out(f"[{state.step}] {name} -> {msg[:100]}")
        reply(state, msg)
        return state, False
    state.last_outcome = actions.execute(ctx, state, name, args)
    return state, True


# ------------------------------------------------------------------- verify
def verify_node(ctx: Ctx, state: RunState) -> RunState:
    """Compare before/after observations and report the facts back to the model."""
    name, args = state.call.name, dict(state.call.input or {})
    outcome = state.last_outcome
    lines, changed = describe_change(state.prev_snap, state.snap)
    report = ""
    if lines:
        report += "Changes since the previous observation:\n" + "\n".join(f"  - {l}" for l in lines) + "\n"
    failed = outcome.startswith(("ERROR", "BLOCKED", "REFUSED", "DECLINED", "REJECTED", "GUARD"))
    if name in EXPECT_CHANGE and not changed and not failed:
        report += "WARNING: the page looks identical after this action - it may have had no effect. Verify before continuing.\n"
    ctx.out(f"[{state.step}] {name} {short(dict(state.call.input or {}))} -> {outcome[:110].splitlines()[0]}")
    for l in lines[:4]:
        ctx.out(f"      ~ {l[:110]}")
    ctx.runlog.observation(state.step, state.snap.render())
    ctx.session.screenshot(ctx.runlog.screenshot_path(state.step))
    ctx.runlog.step(step=state.step, action=name, args=args, outcome=outcome, url=state.snap.url, changes=lines)
    cl = checklist_text(state)
    reply(
        state,
        f"Result: {outcome}\n{report}{cl + chr(10) if cl else ''}\n<page>\n{state.snap.render()}\n</page>",
    )
    return state


# --------------------------------------------------------------------- done
def done_node(ctx: Ctx, state: RunState) -> RunState:
    a = dict(state.call.input or {})
    ok = bool(a.get("success"))
    unmet = [c.text for c in state.checklist if not c.done]
    if ok and unmet and state.done_rejections < MAX_DONE_REJECTIONS:
        state.done_rejections += 1
        ctx.runlog.step(step=state.step, event="done_rejected", unmet=unmet)
        ctx.out(f"[{state.step}] done rejected: unmet checklist items {unmet}")
        reply(
            state,
            "REJECTED: these checklist items are not marked complete: "
            + "; ".join(unmet)
            + ". Finish them (then mark via `plan` with `completed`), or call done with success=false and explain what is missing.",
        )
        return state
    ctx.runlog.step(step=state.step, action="done", args=a)
    ctx.out(f"[{state.step}] done success={ok}: {a.get('summary', '')}")
    summary = a.get("summary", "")
    if ok and unmet:
        return finish(ctx, state, "incomplete", f"{summary} [UNVERIFIED checklist items: {'; '.join(unmet)}]", a.get("result"))
    return finish(ctx, state, "success" if ok else "failed", summary, a.get("result"))


__all__ = ["observe", "decide", "plan_node", "act_node", "verify_node", "done_node", "finish", "AbortRun", "Snapshot"]
