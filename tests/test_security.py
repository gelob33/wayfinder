"""Security regression tests: each one replays an attack that used to work."""

from pathlib import Path

from wayfinder.agent.codeexec import Policy, run_code
from wayfinder.agent.tools import tools_for
from wayfinder.config import Task
from wayfinder.perception.snapshot import take_snapshot

from .conftest import FakeHuman, find_id
from .test_wayfinder import _code_step, _log, make_agent


def _run_code(session, code, dry_run=True, approve=False):
    asked = []
    policy = Policy(["127.0.0.1"], lambda: set(), dry_run, True, lambda s: asked.append(s) or approve)
    return run_code(code, session.page, policy), asked


# ------------------------------------------------------------------ run_playwright
def test_run_playwright_is_off_by_default(tmp_path, session, site_url):
    assert "run_playwright" not in [t["name"] for t in tools_for(False)]
    assert "run_playwright" in [t["name"] for t in tools_for(True)]
    task = Task(url=site_url, goal="x")
    steps = [_code_step("result = 1"), lambda m: ("done", {"success": False, "summary": "n"})]
    agent, _ = make_agent(tmp_path, session, task, steps, allow_code=False)
    assert "run_playwright is disabled" in _log(agent.run())


def test_generated_code_cannot_escape_to_python_internals(session, site_url):
    session.page.goto(site_url)
    escapes = [
        "result = re.enum.sys.modules['os'].getcwd()",  # modules are facades now
        "result = json.codecs.builtins.eval('1+1')",
        "def g():\n    yield 1\nx = g()\nresult = x.gi_frame",  # generator frames
        "result = '{0._page}'.format(page)",  # attribute access through format strings
        "result = Exception.mro()",
    ]
    for code in escapes:
        out, _ = _run_code(session, code)
        assert out.startswith(("REJECTED", "ERROR", "GUARD")), (code, out)


def test_generated_code_still_does_ordinary_work(session, site_url):
    session.page.goto(site_url + "/search.html")
    out, _ = _run_code(
        session,
        "names = page.locator('li a').all_inner_texts()\n"
        "result = {'n': page.locator('li').count(), 'first': names[0], 'm': re.findall('[0-9]+', page.inner_text('main'))}",
    )
    assert "Code ran OK" in out and "Trail Running Shoes" in out and '"n": 2' in out


def test_generated_code_cannot_reach_raw_page_context_or_js(session, site_url):
    session.page.goto(site_url + "/login.html")
    blocked = [
        "result = str(page.locator('body').page.context.cookies())",
        "result = page.evaluate('1+1')",
        "page.locator('#p').evaluate(\"e => e.value = 'x'\")",
        "page.get_by_role('button', name='Log in').dispatch_event('click')",
        "h = page.query_selector('#p')",
        "h = page.locator('#p').element_handle()",
        "result = page.context",
    ]
    for code in blocked:
        out, _ = _run_code(session, code)
        assert "Code ran OK" not in out, (code, out)
    assert session.page.url.endswith("login.html")


def test_generated_code_cannot_type_or_read_password_any_way(session, site_url):
    session.page.goto(site_url + "/login.html")
    attempts = [
        "page.locator('#p').fill('hunter2')",
        "page.locator('#p').press('a')",
        "page.locator('#p').focus()\nfor c in 'ab':\n    page.keyboard.press(c)",
        "page.locator('#p').focus()\npage.keyboard.type('hunter2')",
        "result = page.locator('#p').input_value()",
    ]
    for code in attempts:
        out, _ = _run_code(session, code)
        assert "GUARD: refused" in out, (code, out)
    assert session.page.input_value("#p") == ""


def test_generated_code_post_submit_paths_are_gated(session, site_url):
    for code in [
        "page.get_by_role('button', name='Continue').click()",
        "page.get_by_label('Full name').press('Enter')",
        "page.get_by_label('Full name').focus()\npage.keyboard.press('Enter')",
        "b = page.get_by_role('button', name='Continue').bounding_box()\npage.mouse.click(b['x'] + 5, b['y'] + 5)",
    ]:
        session.page.goto(site_url + "/postform.html")
        out, asked = _run_code(session, code, dry_run=True)
        assert "blocked (dry run)" in out and not asked, (code, out)
        out, asked = _run_code(session, code, dry_run=False, approve=False)
        assert "declined" in out and len(asked) == 1, (code, out)
        session.page.wait_for_timeout(200)
        assert session.page.url.endswith("postform.html")


# ------------------------------------------------------------- simple tools: submit / password
def test_post_form_submission_is_gated_but_get_search_is_not(tmp_path, session, site_url):
    # POST form: Enter via `type(submit=True)`, the press tool, and the submit button are all gated.
    task = Task(url=site_url + "/postform.html", goal="x")
    steps = [
        lambda m: ("type", {"id": find_id(m, "Full name"), "text": "Bob", "submit": True}),
        lambda m: ("click", {"id": find_id(m, "Full name")}),
        lambda m: ("press", {"key": "Enter"}),
        lambda m: ("click", {"id": find_id(m, "Continue")}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    res = agent.run()
    assert _log(res).count("BLOCKED (dry run)") == 3 and human.approvals == []
    assert session.page.url.endswith("postform.html")  # never submitted

    agent, human = make_agent(tmp_path, session, task, list(steps), human=FakeHuman(approve=False))
    res = agent.run()
    assert len(human.approvals) == 3 and _log(res).count("DECLINED") == 3
    assert session.page.url.endswith("postform.html")

    # GET search form: Enter still works without any prompt, even in dry-run.
    task = Task(url=site_url, goal="search")
    steps = [
        lambda m: ("type", {"id": find_id(m, "Search products"), "text": "shoes", "submit": True}),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    agent.run()
    assert "search.html" in session.page.url and human.approvals == []


def test_simple_press_refuses_typing_into_password(tmp_path, session, site_url):
    task = Task(url=site_url + "/login.html", goal="x")
    steps = [
        lambda m: ("click", {"id": find_id(m, "Password")}),
        lambda m: ("press", {"key": "a"}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps)
    res = agent.run()
    assert "REFUSED: focus is in a SENSITIVE field" in _log(res)
    assert session.page.input_value("#p") == ""


# ------------------------------------------------------------------- off-site sign-in pages
def test_offsite_signin_page_needs_host_approval(tmp_path, session, site_url):
    # The same server under another host name is a different site as far as the allowlist goes.
    other = site_url.replace("127.0.0.1", "localhost")
    task = Task(url=site_url + "/offsite.html?to=" + other, goal="x")
    steps = [
        lambda m: ("click", {"id": find_id(m, "Member login")}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    human = FakeHuman(approve_domain=False)
    agent, _ = make_agent(tmp_path, session, task, steps, human=human)
    agent.run()
    assert human.domain_prompts == ["localhost"]
    assert human.logins == []  # no "please sign in" pause for a host the person declined
    assert "127.0.0.1" in session.page.url  # sent back to the real site

    human = FakeHuman(approve_domain=True)
    agent, _ = make_agent(tmp_path, session, task, list(steps), human=human)
    agent.run()
    assert human.domain_prompts == ["localhost"] and len(human.logins) == 1


# ------------------------------------------------------------------------ logs / injection
def test_logs_redact_typed_text_answers_and_url_queries(tmp_path, session, site_url):
    task = Task(url=site_url, goal="x")
    steps = [
        lambda m: ("ask_human", {"question": "What is your date of birth?"}),
        lambda m: ("type", {"id": find_id(m, "Search products"), "text": "secret-dob-1990", "submit": True}),
        lambda m: ("done", {"success": True, "summary": "ok"}),
    ]
    agent, _ = make_agent(tmp_path, session, task, steps, human=FakeHuman(answer="1990-01-02"))
    res = agent.run()
    log = _log(res)
    assert "secret-dob-1990" not in log and "1990-01-02" not in log
    assert "<redacted" in log
    everything = log + "".join(p.read_text(encoding="utf-8") for p in Path(res.run_dir).glob("step-*.txt"))
    assert "q=secret" not in everything and "[query removed]" in everything


def test_page_text_cannot_fake_wrapper_tags_or_terminal_escapes(session, site_url):
    session.page.goto(site_url + "/inject.html")
    rendered = take_snapshot(session.page).render()
    assert "</page>" not in rendered and "\x1b" not in rendered
    assert "IGNORE ALL PREVIOUS" in rendered  # still visible as data, just defused


def test_enter_on_a_risky_button_is_gated(tmp_path, session, site_url):
    # Enter/Space on a focused "Place order" button acts like a click, even in a non-POST form.
    session.page.goto(site_url + "/product.html")
    out, asked = _run_code(session, "page.get_by_role('button', name='Place order').press('Enter')")
    assert "blocked (dry run)" in out and session.page.url.endswith("product.html")
    out, _ = _run_code(session, "page.get_by_role('button', name='Place order').focus()\npage.keyboard.press('Space')")
    assert "blocked (dry run)" in out and session.page.url.endswith("product.html")

    task = Task(url=site_url + "/product.html", goal="x")
    steps = [
        _code_step("page.get_by_role('button', name='Place order').focus()"),
        lambda m: ("press", {"key": "Enter"}),
        lambda m: ("done", {"success": False, "summary": "n"}),
    ]
    agent, human = make_agent(tmp_path, session, task, steps, dry_run=True)
    res = agent.run()
    assert "BLOCKED (dry run)" in _log(res) and session.page.url.endswith("product.html")
