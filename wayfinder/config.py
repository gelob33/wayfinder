"""Task definition and run settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass
class Task:
    url: str
    goal: str
    inputs: dict[str, str] = field(default_factory=dict)
    success_criteria: str = ""


@dataclass
class Settings:
    model: str = field(default_factory=lambda: os.environ.get("WAYFINDER_MODEL") or os.environ.get("NAVIGATOR_MODEL") or DEFAULT_MODEL)
    max_steps: int = 40
    max_seconds: int = 900
    max_total_tokens: int = 600_000
    headless: bool = False
    # Chrome channel ("chrome", "msedge") or None for Playwright's bundled Chromium.
    browser_channel: str | None = None
    # Attach to a browser the person launched with --remote-debugging-port instead of launching one.
    cdp_url: str | None = None
    profile_dir: Path = Path("data/browser-profile")
    runs_dir: Path = Path("runs")
    allowed_domains: list[str] = field(default_factory=list)
    effort: str = "medium"
    # Block every click that looks like it submits, pays or deletes.
    dry_run: bool = False
    # Ask the human before submit/pay/delete-like clicks.
    require_approval: bool = True
    # Let the model write Playwright code (run_playwright). Off by default: it cannot be sandboxed safely.
    allow_code: bool = False
