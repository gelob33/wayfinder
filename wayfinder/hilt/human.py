"""Human-in-the-loop (HILT): pause the run and wait for the person.

Credentials never pass through here or to the LLM: for a sign-in the person types
directly into the visible browser window, and the persistent profile keeps the session.
"""

from __future__ import annotations

from typing import Protocol


class AbortRun(Exception):
    """The human chose to stop the run."""


class Human(Protocol):
    def wait_for_login(self, url: str) -> None: ...
    def wait_for_challenge(self, url: str) -> None: ...
    def ask(self, question: str) -> str: ...
    def approve(self, summary: str) -> bool: ...


class ConsoleHuman:
    def _read(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            raise AbortRun("no interactive input available")

    def wait_for_login(self, url: str) -> None:
        print("\n" + "=" * 60)
        print("PAUSED: sign-in needed")
        print(f"  Page: {url}")
        print("  Sign in in the browser window yourself (the agent never sees your")
        print("  password). Complete any MFA/CAPTCHA, then come back here.")
        answer = self._read("  Press Enter when signed in, or type 'abort' to stop: ")
        if answer.lower() in {"abort", "quit", "q", "stop"}:
            raise AbortRun("user aborted at sign-in")
        print("=" * 60 + "\n")

    def wait_for_challenge(self, url: str) -> None:
        print("\n" + "=" * 60)
        print("PAUSED: the site is showing a bot-verification (CAPTCHA) page")
        print(f"  Page: {url}")
        print("  Complete the verification in the browser window yourself if it lets you.")
        answer = self._read("  Press Enter when the real page has loaded, or type 'abort' to stop: ")
        if answer.lower() in {"abort", "quit", "q", "stop"}:
            raise AbortRun("user aborted at bot verification")
        print("=" * 60 + "\n")

    def ask(self, question: str) -> str:
        print("\n" + "=" * 60)
        print("The agent needs your input (do not type passwords or card numbers):")
        print(f"  {question}")
        answer = self._read("  > ")
        if answer.lower() in {"abort", "quit"}:
            raise AbortRun("user aborted")
        print("=" * 60 + "\n")
        return answer

    def approve(self, summary: str) -> bool:
        print("\n" + "=" * 60)
        print("APPROVAL NEEDED (action may not be reversible):")
        print(f"  {summary}")
        answer = self._read("  Allow? [y/N]: ")
        print("=" * 60 + "\n")
        return answer.lower() in {"y", "yes"}
