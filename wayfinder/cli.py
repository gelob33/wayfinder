"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent.llm import AnthropicLLM
from .agent.loop import Agent
from .browser.session import BrowserSession
from .config import Settings, Task
from .hilt.human import ConsoleHuman
from .reporting.runlog import RunLog


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="wayfinder", description="LLM agent that navigates a website to achieve a goal.")
    p.add_argument("--url", required=True, help="Start URL of the website.")
    p.add_argument("--goal", required=True, help="What the agent should achieve.")
    p.add_argument("--input", "-i", action="append", default=[], metavar="KEY=VALUE",
                   help="Task input the agent may use when filling forms (repeatable).")
    p.add_argument("--success", default="", help="Success criteria / expected output description.")
    p.add_argument("--model", default=None, help="Model id (default: claude-sonnet-5-5 or $WAYFINDER_MODEL).")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--max-seconds", type=int, default=900)
    p.add_argument("--headless", action="store_true", help="Hide the browser (sign-in pauses need it visible).")
    p.add_argument("--channel", default=None, help='Use installed browser, e.g. "chrome" or "msedge".')
    p.add_argument("--profile", default="data/browser-profile", help="Persistent browser profile (keeps sign-ins).")
    p.add_argument("--cdp-url", default=None, metavar="URL",
                   help="Attach to a Chrome you launched yourself with --remote-debugging-port (e.g. http://localhost:9222).")
    p.add_argument("--allow-domain", action="append", default=[], help="Extra allowed domain (repeatable).")
    p.add_argument("--dry-run", action="store_true", help="Never click submit/pay/send-like buttons.")
    p.add_argument("--allow-code", action="store_true",
                   help="Let the agent write and run Playwright code (run_playwright). Only for sites you trust.")
    p.add_argument("--no-approval", action="store_true", help="Do not ask before submit/pay/send-like clicks.")
    return p.parse_args(argv)


def build(args: argparse.Namespace) -> tuple[Task, Settings]:
    inputs: dict[str, str] = {}
    for item in args.input:
        if "=" not in item:
            raise SystemExit(f"--input must be KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        inputs[k.strip()] = v.strip()
    task = Task(url=args.url, goal=args.goal, inputs=inputs, success_criteria=args.success)
    settings = Settings(
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        headless=args.headless,
        browser_channel=args.channel,
        profile_dir=Path(args.profile),
        cdp_url=args.cdp_url,
        dry_run=args.dry_run,
        require_approval=not args.no_approval,
        allow_code=args.allow_code,
    )
    if args.model:
        settings.model = args.model
    if args.allow_domain:
        from .safety.policy import default_allowed_domains

        settings.allowed_domains = default_allowed_domains(args.url) + [d.lower() for d in args.allow_domain]
    return task, settings


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    task, settings = build(args)
    runlog = RunLog(settings.runs_dir)
    print(f"Run folder: {runlog.dir}")
    llm = AnthropicLLM(settings.model, settings.effort)
    with BrowserSession(settings.profile_dir, settings.headless, settings.browser_channel, settings.cdp_url) as session:
        result = Agent(task, settings, session, llm, ConsoleHuman(), runlog).run()
    print("\n=== RESULT ===")
    print(f"status : {result.status}")
    print(f"steps  : {result.steps}   tokens: {result.tokens}")
    print(f"summary: {result.summary}")
    for i, item in enumerate(result.checklist, 1):
        print(f"  {i}. [{'x' if item['done'] else ' '}] {item['text']}")
    if result.result is not None:
        print(json.dumps(result.result, indent=2, ensure_ascii=False))
    print(f"log    : {result.run_dir}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
