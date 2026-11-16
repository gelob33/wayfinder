"""The agent graph, as an explicit sequential state machine.

    observe -> decide --+--> plan ----------------------------+
                        +--> act -> observe -> verify --------+--> decide ...
                        +--> done (checklist gate) -> end

Nodes live in ``nodes.py``; they share one ``RunState`` so the same functions can later be
wrapped in a LangGraph ``StateGraph`` without changes.
"""

from __future__ import annotations

from typing import Callable

from ..browser.session import BrowserSession
from ..config import Settings, Task
from ..hilt.human import AbortRun, Human
from ..reporting.runlog import RunLog
from ..safety.policy import default_allowed_domains, normalize_url
from . import nodes
from .llm import LLM
from .prompts import initial_message
from .state import Ctx, RunResult, RunState


class Agent:
    def __init__(
        self,
        task: Task,
        settings: Settings,
        session: BrowserSession,
        llm: LLM,
        human: Human,
        runlog: RunLog,
        out: Callable[[str], None] = print,
    ):
        self.ctx = Ctx(
            task=task,
            settings=settings,
            session=session,
            llm=llm,
            human=human,
            runlog=runlog,
            out=out,
            allowed=settings.allowed_domains or default_allowed_domains(task.url),
        )

    def run(self) -> RunResult:
        ctx = self.ctx
        state = RunState(seen_urls={normalize_url(ctx.task.url)})
        try:
            ctx.session.page.goto(ctx.task.url)
            ctx.session.settle()
            nodes.observe(ctx, state)
            if state.result is not None:
                return state.result
            ctx.runlog.observation(0, state.snap.render())
            state.messages.append(
                {"role": "user", "content": initial_message(ctx.task, ctx.settings, state.snap.render())}
            )
            while state.result is None:
                if state.step >= ctx.settings.max_steps:
                    nodes.finish(ctx, state, "budget_exceeded", "Step budget exceeded.")
                    break
                state.step += 1
                nodes.decide(ctx, state)
                if state.result is not None or state.call is None:
                    continue  # finished, or nudged the model to call a tool
                name = state.call.name
                if name == "plan":
                    nodes.plan_node(ctx, state)
                elif name == "done":
                    nodes.done_node(ctx, state)
                else:
                    _, executed = nodes.act_node(ctx, state)
                    if executed:
                        nodes.observe(ctx, state)
                        if state.result is None:
                            nodes.verify_node(ctx, state)
        except AbortRun as e:
            nodes.finish(ctx, state, "aborted", str(e))
        except KeyboardInterrupt:
            nodes.finish(ctx, state, "aborted", "Interrupted.")
        except Exception as e:  # never lose the run log to a crash
            nodes.finish(ctx, state, "error", f"Unexpected error: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        return state.result
