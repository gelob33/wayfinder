"""Run state shared by the graph nodes, plus the dependency container."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..browser.session import BrowserSession
from ..config import Settings, Task
from ..hilt.human import Human
from ..perception.snapshot import Snapshot
from ..reporting.runlog import RunLog
from .llm import LLM


@dataclass
class RunResult:
    status: str  # success | incomplete | failed | blocked | aborted | budget_exceeded | error
    summary: str
    result: dict | None
    steps: int
    tokens: int
    run_dir: str
    checklist: list[dict] = field(default_factory=list)


@dataclass
class ChecklistItem:
    text: str
    done: bool = False


@dataclass
class Ctx:
    """Everything the nodes need that is not run state."""

    task: Task
    settings: Settings
    session: BrowserSession
    llm: LLM
    human: Human
    runlog: RunLog
    out: Callable[[str], None]
    allowed: list[str]


@dataclass
class RunState:
    messages: list[dict] = field(default_factory=list)
    snap: Snapshot | None = None
    prev_snap: Snapshot | None = None
    plan: dict | None = None
    checklist: list[ChecklistItem] = field(default_factory=list)
    step: int = 0
    tokens: int = 0
    nudges: int = 0
    done_rejections: int = 0
    started: float = field(default_factory=time.time)
    # Links the agent has been shown: `goto` is only allowed to these (no guessed URLs).
    seen_urls: set[str] = field(default_factory=set)
    login_pauses: int = 0
    challenge_pauses: int = 0
    # Off-site sign-in hosts (e.g. an identity provider) the person approved / declined this run.
    approved_hosts: set[str] = field(default_factory=set)
    declined_hosts: set[str] = field(default_factory=set)
    last_login_url: str | None = None
    # Set by `decide`, consumed by the next node:
    call: Any = None
    last_outcome: str = ""
    # Set when the run is over:
    result: RunResult | None = None
