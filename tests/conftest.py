import functools
import http.server
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayfinder.browser.session import BrowserSession

SITE = Path(__file__).parent / "site"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def site_url():
    handler = functools.partial(_Quiet, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def session(tmp_path):
    with BrowserSession(tmp_path / "profile", headless=True) as s:
        yield s


class FakeHuman:
    def __init__(self, approve=True, answer="ok"):
        self.logins: list[str] = []
        self.challenges: list[str] = []
        self.asked: list[str] = []
        self.approvals: list[str] = []
        self._approve = approve
        self._answer = answer

    def wait_for_login(self, url):
        self.logins.append(url)

    def wait_for_challenge(self, url):
        self.challenges.append(url)

    def ask(self, question):
        self.asked.append(question)
        return self._answer

    def approve(self, summary):
        self.approvals.append(summary)
        return self._approve


def last_observation(messages) -> str:
    """The most recent message that contains a page observation (plan replies carry none)."""
    for msg in reversed(messages):
        if msg["role"] != "user":
            continue  # assistant turns hold response objects
        content = msg["content"]
        text = content[0]["content"] if isinstance(content, list) else content
        if isinstance(text, str) and "Interactive elements:" in text:
            return text
    return ""


def find_id(messages, label: str) -> int:
    """Find the [id] of the element whose label contains ``label`` in the latest observation."""
    m = re.search(r'\[(\d+)\] \S+ "[^"\n]*' + re.escape(label), last_observation(messages))
    assert m, f"no element labelled {label!r} in:\n{last_observation(messages)}"
    return int(m.group(1))


class FakeLLM:
    """Scripted model: each step is a callable(messages) -> (tool_name, args)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def create(self, system, tools, messages):
        name, args = self.steps[self.calls](messages)
        self.calls += 1
        block = SimpleNamespace(type="tool_use", id=f"toolu_{self.calls}", name=name, input=args)
        usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason="tool_use", content=[block], usage=usage)


@pytest.fixture
def external_browser(tmp_path):
    """A separately launched Chromium with a debugging port, like a browser the person started."""
    import socket
    import subprocess
    import time
    import urllib.request

    from playwright.sync_api import sync_playwright

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with sync_playwright() as pw:
        exe = pw.chromium.executable_path
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'ext'}", "--headless=new", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "/json/version", timeout=0.5)
            break
        except Exception:
            time.sleep(0.2)
    yield url
    proc.terminate()
