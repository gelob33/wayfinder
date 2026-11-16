from pathlib import Path

from wayfinder.agent.loop import Agent
from wayfinder.config import Settings, Task
from wayfinder.perception.snapshot import take_snapshot
from wayfinder.reporting.runlog import RunLog
from wayfinder.safety.policy import domain_allowed, is_risky_click, is_sign_in_url

from .conftest import FakeHuman, FakeLLM, find_id


PLAN = lambda m: ("plan", {"page_summary": "test page", "steps": ["do it"], "checklist": ["test goal"], "completed": [1]})  # noqa: E731


def make_agent(tmp_path, session, task, steps, human=None, plan=True, **settings):
    """Build an Agent over scripted steps; a plan step is prepended unless plan=False."""
    settings.setdefault("allow_code", True)  # most tests exercise run_playwright; the default (off) has its own test
    s = Settings(runs_dir=tmp_path / "runs", headless=True, **settings)
    human = human or FakeHuman()
    if plan:
        steps = [PLAN, *steps]
    return Agent(task, s, session, FakeLLM(steps), human, RunLog(s.runs_dir), out=lambda *_: None), human


# ---------------------------------------------------------------- perception
def test_snapshot_lists_elements_and_flags_sensitive(session, site_url):
    session.page.goto(site_url + "/login.html")
    snap = take_snapshot(session.page)
    assert snap.has_password
    pw = next(e for e in snap.elements if e["type"] == "password")
    assert pw["sensitive"] and pw["label"] == "Password"
    assert "SENSITIVE" in snap.render()


def test_snapshot_home(session, site_url):
    session.page.goto(site_url)
    snap = take_snapshot(session.page)
    labels = [e["label"] for e in snap.elements]
    assert "Search products" in labels and "Search" in labels and "Products" in labels
    assert "Welcome to Shop" in snap.text


# ------------------------------------------------------------------- safety
def test_policy():
    assert domain_allowed("https://www.example.com/x", ["example.com"])
    assert domain_allowed("https://shop.example.com", ["example.com"])
    assert not domain_allowed("https://evil.com", ["example.com"])
    assert not domain_allowed("file:///etc/passwd", ["example.com"])
    assert is_sign_in_url("https://login.example.com/") and is_sign_in_url("https://x.com/signin?x=1")
    assert not is_sign_in_url("https://x.com/products")
    assert is_risky_click({"label": "Place order"}) and not is_risky_click({"label": "Search"})


# --------------------------------------------------------------- agent loop
def test_search_flow_end_to_end(tmp_path, session, site_url):
    task = Task(url=site_url, goal="find trail shoes price")
    steps = [
        lambda m: ("type", {"id": find_id(m, "Search products"), "text": "shoes", "submit": True}),
        lambda m: ("click", {"id": find_id(m, "Trail Running Shoes")}),
        lambda m: ("done", {"success": True, "summary": "found", "result": {"price": 89}}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert res.status == "success" and res.result == {"price": 89} and res.steps == 4
    assert "product.html" in session.page.url
    assert (Path(res.run_dir) / "result.json").exists()
    assert (Path(res.run_dir) / "steps.jsonl").exists()


def test_login_pauses_for_human_and_never_fills_password(tmp_path, session, site_url):
    task = Task(url=site_url + "/login.html", goal="check account")
    steps = [
        lambda m: ("type", {"id": find_id(m, "Password"), "text": "hunter2"}),
        lambda m: ("done", {"success": False, "summary": "stopped"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps)
    # Observation happens before the first LLM call, so the pause occurs first.
    res = agent.run()
    assert human.logins == [site_url + "/login.html"]
    assert res.status == "failed"
    log = (Path(res.run_dir) / "steps.jsonl").read_text()
    assert "hunter2" not in log.split('"outcome"')[1].split("\n")[0]  # outcome is a refusal, not the secret
    assert "REFUSED" in log


def test_risky_click_needs_approval(tmp_path, session, site_url):
    task = Task(url=site_url + "/product.html", goal="buy")
    steps = [
        lambda m: ("click", {"id": find_id(m, "Place order")}),
        lambda m: ("done", {"success": False, "summary": "declined"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, human=FakeHuman(approve=False))
    res = agent.run()
    assert len(human.approvals) == 1
    assert "DECLINED" in (Path(res.run_dir) / "steps.jsonl").read_text()
    assert session.page.url.endswith("product.html")  # not submitted


def test_dry_run_blocks_without_asking(tmp_path, session, site_url):
    task = Task(url=site_url + "/product.html", goal="buy")
    steps = [
        lambda m: ("click", {"id": find_id(m, "Place order")}),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    agent.run()
    assert human.approvals == []
    assert session.page.url.endswith("product.html")


def test_offsite_goto_blocked(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        lambda m: ("goto", {"url": "https://example.org/"}),
        lambda m: ("done", {"success": False, "summary": "blocked"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert "BLOCKED" in (Path(res.run_dir) / "steps.jsonl").read_text()
    assert session.page.url.startswith(site_url)


def test_step_budget(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [lambda m: ("scroll", {"direction": "down"})] * 5
    agent, _ = make_agent(tmp_path, session, task, steps, max_steps=4)
    res = agent.run()
    assert res.status == "budget_exceeded" and res.steps == 4


# ------------------------------------------------- filters / links / goto (Finder lessons)
def test_snapshot_text_uses_body_when_main_is_tiny_and_cleans_links(session, site_url):
    session.page.goto(site_url + "/filters.html")
    snap = take_snapshot(session.page, allowed=["127.0.0.1"])
    assert "Alpha Card score 9.5" in snap.text  # <main> only holds the nav; body text is used
    rendered = snap.render()
    elements = rendered.split("Page text:")[0]  # the element list, not the page text
    assert "other.example" not in elements and "GO TO SITE" not in elements  # off-site redirect dropped
    assert "utm=1" not in rendered  # tracking query stripped
    assert rendered.count("card.html") == 1  # image+title duplicate links collapsed
    assert "Alpha Card image" not in rendered


def test_overlaid_checkbox_can_be_toggled(tmp_path, session, site_url):
    task = Task(url=site_url + "/filters.html", goal="filter")
    steps = [
        lambda m: ("click", {"id": find_id(m, "No annual fee")}),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    log = (Path(res.run_dir) / "steps.jsonl").read_text()
    assert "now checked" in log
    assert "1 of 2 results" in session.page.inner_text("#r")


def test_goto_only_to_seen_links(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        lambda m: ("goto", {"url": site_url + "/product.html?x=1"}),  # not linked from home
        lambda m: ("goto", {"url": site_url + "/search.html"}),  # linked ("Products" -> search.html?q=)
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    log = (Path(res.run_dir) / "steps.jsonl").read_text()
    assert "BLOCKED: goto only works for URLs of links you have seen" in log
    assert session.page.url.endswith("search.html")
    assert (Path(res.run_dir) / "step-000.txt").exists()  # observations are saved for auditing


# ------------------------------------------------------- generated Playwright code
def _code_step(code):
    return lambda m: ("run_playwright", {"code": code, "reason": "test"})


def _log(res):
    return (Path(res.run_dir) / "steps.jsonl").read_text()


def test_generated_code_can_use_page_semantics(tmp_path, session, site_url):
    task = Task(url=site_url + "/filters.html", goal="filter")
    steps = [
        _code_step("page.get_by_label('No annual fee').check(force=True)\nresult = page.locator('#r').inner_text()"),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert 'result = \\"1 of 2 results\\"' in _log(res) or "1 of 2 results" in _log(res)


def test_generated_code_is_screened(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    bad = ["import os", "x = ().__class__", "while True:\n    pass", "open('f')", "page.context.cookies()"]
    steps = [_code_step(c) for c in bad] + [lambda m: ("done", {"success": False, "summary": "n"})]
    agent, _ = make_agent(tmp_path, session, task, steps, max_steps=12)
    res = agent.run()
    log = _log(res)
    assert log.count("REJECTED") == 4  # four screened statically
    assert "'context' is not available" in log  # the proxy hides the browser context


def test_generated_code_cannot_fill_password(tmp_path, session, site_url):
    task = Task(url=site_url + "/login.html", goal="x")
    steps = [
        _code_step("page.get_by_label('Password').fill('hunter2')"),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert "GUARD: refused" in _log(res)
    assert session.page.input_value("#p") == ""


def test_generated_code_risky_click_blocked_in_dry_run_and_approved_otherwise(tmp_path, session, site_url):
    task = Task(url=site_url + "/product.html", goal="buy")
    steps = [
        _code_step("page.get_by_role('button', name='Place order').click()"),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    res = agent.run()
    assert "blocked (dry run)" in _log(res) and session.page.url.endswith("product.html")

    agent, human = make_agent(tmp_path, session, task, list(steps), human=FakeHuman(approve=False))
    res = agent.run()
    assert len(human.approvals) == 1 and "declined" in _log(res)
    assert session.page.url.endswith("product.html")


def test_generated_code_goto_limited_to_seen_links(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        _code_step(f"page.goto('{site_url}/product.html')"),
        _code_step(f"page.goto('{site_url}/search.html')"),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert "goto only works for URLs of links seen" in _log(res)
    assert session.page.url.endswith("search.html")


# ------------------------------------------------ plan / verify / checklist (graph nodes)
from wayfinder.perception.diff import describe_change  # noqa: E402


def test_first_action_requires_plan(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        lambda m: ("scroll", {"direction": "down"}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps, plan=False)
    res = agent.run()
    assert "REJECTED: call `plan` first" in _log(res)


def test_plan_requires_checklist_then_done_gated_on_it(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        lambda m: ("plan", {"page_summary": "home", "steps": ["a"]}),  # no checklist -> rejected
        lambda m: ("plan", {"page_summary": "home", "steps": ["a"], "checklist": ["Filter applied", "Price read"]}),
        lambda m: ("done", {"success": True, "summary": "early"}),  # rejected: unmet items
        lambda m: ("plan", {"page_summary": "home", "steps": ["b"], "completed": [1, 2]}),
        lambda m: ("done", {"success": True, "summary": "all good", "result": {"ok": 1}}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps, plan=False)
    res = agent.run()
    log = _log(res)
    assert "rejected: no checklist" in log and "done_rejected" in log
    assert res.status == "success" and all(c["done"] for c in res.checklist)


def test_done_accepted_as_incomplete_after_repeated_rejections(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [lambda m: ("plan", {"page_summary": "h", "steps": ["a"], "checklist": ["Never done"]})]
    steps += [lambda m: ("done", {"success": True, "summary": "claims done"})] * 3
    agent, _ = make_agent(tmp_path, session, task, steps, plan=False)
    res = agent.run()
    assert res.status == "incomplete" and "UNVERIFIED" in res.summary


def test_verify_reports_checkbox_and_result_count_changes(tmp_path, session, site_url):
    task = Task(url=site_url + "/filters.html", goal="filter")
    steps = [
        lambda m: ("click", {"id": find_id(m, "No annual fee")}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    log = _log(res)
    assert 'unchecked -> checked' in log
    assert 'Result count text' in log and "1 of 2 results" in log


def test_verify_warns_when_nothing_changed(session, site_url):
    session.page.goto(site_url)
    a = take_snapshot(session.page)
    b = take_snapshot(session.page)
    lines, changed = describe_change(a, b)
    assert not changed and lines == []
    session.page.goto(site_url + "/search.html")
    _, changed = describe_change(a, take_snapshot(session.page))
    assert changed


# ------------------------------------------------------------ bot-verification pages
def test_bot_challenge_pauses_then_stops_without_acting(tmp_path, session, site_url):
    task = Task(url=site_url + "/challenge.html", goal="x")
    agent, human = make_agent(tmp_path, session, task, [], plan=False)
    res = agent.run()  # the scripted LLM has no steps: it must never be called
    assert res.status == "blocked" and "bot protection" in res.summary
    assert len(human.challenges) == 1  # asked the person once, then gave up instead of looping
    assert agent.ctx.llm.calls == 0


def test_bot_challenge_detector():
    from wayfinder.safety.policy import is_bot_challenge

    assert is_bot_challenge("Just a moment...", "")
    assert is_bot_challenge("udemy.com", "Performing security verification This website uses a security service")
    assert not is_bot_challenge("Shop Home", "Welcome to Shop " * 5)
    assert not is_bot_challenge("Blog", "How to verify you are human " + "x" * 2000)


# ------------------------------------------------------ attach to the person's own browser
def test_attach_to_external_browser_keeps_it_open(tmp_path, site_url, external_browser):
    from playwright.sync_api import sync_playwright

    from wayfinder.browser.session import BrowserSession

    with sync_playwright() as pw:  # the "person" signs in / sets state in their own browser first
        b = pw.chromium.connect_over_cdp(external_browser)
        page = b.contexts[0].pages[0]
        page.goto(site_url + "/account.html")
        page.evaluate("document.cookie = 'signedin=1; path=/'")
        b.close()

    task = Task(url=site_url + "/account.html", goal="read")
    with BrowserSession(tmp_path / "unused", cdp_url=external_browser) as s:
        assert s.page.evaluate("document.cookie").startswith("signedin=1")  # same session, not a fresh profile
        agent, _ = make_agent(
            tmp_path, s, task, [lambda m: ("done", {"success": True, "summary": "ok"})]
        )
        assert agent.run().status == "success"

    with sync_playwright() as pw:  # still running after our session disconnected
        b = pw.chromium.connect_over_cdp(external_browser)
        assert b.contexts[0].pages
        b.close()


def test_generated_code_mouse_click_cannot_bypass_risky_guard(tmp_path, session, site_url):
    task = Task(url=site_url + "/product.html", goal="buy")
    code = (
        "b = page.get_by_role('button', name='Place order').bounding_box()\n"
        "page.mouse.click(b['x'] + b['width'] / 2, b['y'] + b['height'] / 2)"
    )
    steps = [_code_step(code), lambda m: ("done", {"success": True, "summary": "ok"})]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    res = agent.run()
    assert "blocked (dry run)" in _log(res)
    assert session.page.url.endswith("product.html")  # the form was not submitted

    agent, human = make_agent(tmp_path, session, task, list(steps), human=FakeHuman(approve=False))
    res = agent.run()
    assert len(human.approvals) == 1 and "declined" in _log(res)
    assert session.page.url.endswith("product.html")


# ------------------------------------------------ robustness: navigation during snapshot / crashes
def test_snapshot_retries_when_page_navigates_mid_evaluate(session, site_url):
    from playwright.sync_api import Error as PlaywrightError

    session.page.goto(site_url)

    class Flaky:
        def __init__(self, page):
            self._p, self.fails = page, 2

        def evaluate(self, *a, **k):
            if self.fails:
                self.fails -= 1
                raise PlaywrightError("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
            return self._p.evaluate(*a, **k)

        def __getattr__(self, n):
            return getattr(self._p, n)

    snap = take_snapshot(Flaky(session.page))
    assert snap.title == "Shop Home"


def test_unexpected_crash_still_writes_result(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")

    def boom(m):
        raise ValueError("kaboom")

    agent, _ = make_agent(tmp_path, session, task, [boom], plan=False)
    res = agent.run()
    assert res.status == "error" and "kaboom" in res.summary
    assert (Path(res.run_dir) / "result.json").exists()


def test_passwordless_login_url_is_a_sign_in_page():
    assert is_sign_in_url("https://www.udemy.com/join/passwordless-auth/?locale=en_US&next=x")


def test_session_falls_back_when_current_tab_closes(session, site_url):
    first = session.page
    first.goto(site_url)
    popup = session.context.new_page()  # adopted as the current tab (like a login popup)
    popup.goto(site_url + "/login.html")
    assert session.page is popup
    popup.close()  # popup closes itself after signing in
    assert session.page is first and session.page.url.startswith(site_url)


def test_agent_survives_login_popup_closing(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    popup_holder = {}

    def open_and_close_popup(m):
        popup = session.context.new_page()
        popup.goto(site_url + "/account.html")
        popup.close()
        popup_holder["done"] = True
        return ("scroll", {"direction": "down"})

    steps = [open_and_close_popup, lambda m: ("done", {"success": True, "summary": "ok"})]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert popup_holder and res.status == "success"


# ------------------------------------------- custom (non-button) option controls, e.g. styled pills
def test_snapshot_lists_custom_clickable_options_and_they_can_be_clicked(tmp_path, session, site_url):
    session.page.goto(site_url + "/options.html")
    snap = take_snapshot(session.page)
    labels = {e["label"]: e for e in snap.elements}
    for name in ("1", "2", "3", "Yes", "No", "Next"):
        assert name in labels, f"{name!r} missing from {sorted(labels)}"
    assert labels["2"].get("custom") and labels["Yes"].get("custom")  # plain div / label wrapping a hidden radio
    assert not any("explanatory text" in e["label"] for e in snap.elements)  # long clickable blocks are not options
    assert "(custom clickable option)" in snap.render()

    task = Task(url=site_url + "/options.html", goal="x")
    steps = [
        lambda m: ("click", {"id": find_id(m, "2")}),
        lambda m: ("click", {"id": find_id(m, "Yes")}),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    agent.run()
    assert session.page.inner_text("#out") == "storeys=2 raised=y"


def test_snapshot_elements_stay_in_page_order(session, site_url):
    session.page.goto(site_url + "/options.html")
    labels = [e["label"] for e in take_snapshot(session.page).elements]
    assert labels.index("1") < labels.index("Yes") < labels.index("Next")
