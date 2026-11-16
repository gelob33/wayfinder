# Wayfinder: Feature Map (LLM web agent with Playwright + Claude)

## 0. Core loop

```
Goal + URL → [Session Setup] → loop { Observe → Plan → Act → Verify } → Result
                                   ↑ pause for HITL (login, CAPTCHA, confirmation)
```

## 1. Feature areas

### A. Task intake
| Feature | Notes |
|---|---|
| Task definition: `url`, `goal` (free text), optional `inputs` (key/value data such as ZIP code, car model, DOB) | Structured inputs prevent the agent from inventing data |
| Success criteria and output schema (e.g. "return a JSON quote with premium and coverage") | Lets the agent know when it is done |
| Constraints: allowed domains, max steps, max time, max cost, forbidden actions | Safety rails |
| Interfaces: CLI first, then local web UI, then API | `main.py` is the CLI entry point to start with |

### B. Browser layer (Playwright)
- Persistent browser context with cookies, local storage and a per-site profile.
- Headed mode, so the user can see the agent and type credentials.
- Tab and popup handling, iframes, shadow DOM, file download/upload and dialogs (alert/confirm).
- Anti-flake behaviour: auto-wait, retry, handling of cookie banners and overlays.
- Screenshot and trace capture on every step (Playwright trace viewer).

### C. Page understanding (Observe)
- **DOM distillation:** the accessibility tree plus interactive elements, each tagged with a stable ID (`[12] button "Get quote"`). Far cheaper than raw HTML.
- **Visual grounding:** a screenshot with numbered element overlays (set-of-marks), used as a fallback or for vision-capable models.
- **Page classifier:** login, form, listing, detail, cart, confirmation, error, CAPTCHA.
- **Form extractor:** fields, types, required flags, validation errors, multi-step wizard progress.
- Change detection (did the page change after the action?) and loop/dead-end detection.

### D. Site mapping (navigation and menu understanding)
- Extract the nav menu, mega-menus, footer links, sitemap.xml and robots.txt.
- Build a site graph incrementally: nodes are pages with a summary, edges are navigation actions.
- Goal-directed exploration: rank links by relevance to the goal (e.g. "Insurance → Car → Get a quote").
- Persist the site map per domain, so repeat runs skip rediscovery.
- Optional "explore-only" mode that just produces a site map report.

### E. Agent brain (LLM)
- **Planner:** decomposes the goal into sub-goals and replans when blocked.
- **Actor:** picks one tool call per step from a constrained action set: `click(id)`, `type(id, text)`, `select(id, option)`, `press(key)`, `scroll`, `goto(url)`, `back`, `wait`, `extract(schema)`, `ask_human(reason)`, `done(result)`.
- **Memory:** short-term step history (compressed), a scratchpad for facts gathered so far, and a long-term per-site playbook.
- **Reflection:** after each action, check whether the expected change happened and recover if not.
- Model routing: a cheap model for simple steps, a stronger model for planning and stuck states.
- Prompt caching and context trimming to control cost.

### F. Human-in-the-loop (HILT)
| Trigger | Behavior |
|---|---|
| Login or auth wall detected (password field, 401/redirect to `/login`) | Pause, notify, let the user log in in the live browser window **or** enter credentials in the app |
| MFA/OTP, CAPTCHA | Pause and wait for the user to solve it, then resume |
| Missing information (agent needs a value not in `inputs`) | Ask the user a question and resume with the answer |
| Irreversible action (submit, pay, delete, send) | Approval gate with a summary of what will happen |
| Agent stuck or low confidence | Escalate to the human, who can take over, hint or abort |

Key design rule: **credentials never go to the LLM.** The user types them directly into the browser or a secure prompt, the app injects them via Playwright, and they are masked in logs and traces. After login, save the session state (`storage_state`) for reuse.

### G. Safety and guardrails
- Domain allowlist, and defence against prompt injection from page content (page text is treated as data, never as instructions).
- Dry-run mode that fills forms but never submits.
- Action policy: read-only, fill-only, or full.
- Redaction of PII and secrets in logs, screenshots and LLM context.
- Hard budget caps (steps, tokens, dollars, wall-clock time) and a kill switch.
- Respect robots.txt and terms of service, with rate limiting.

### H. Output and reporting
- Structured result (JSON validated against the schema from section A).
- Human-readable run report: steps taken, screenshots, final state and sources.
- Replayable run log and trace.
- Export of the site map (Mermaid or JSON).

### I. Observability and evaluation
- Per-step log: observation summary, LLM reasoning, action, result, latency and cost.
- Live view: current step, screenshot and agent thoughts.
- Eval harness: a suite of goal and site pairs (including a local mock site) with pass/fail, step count and cost.
- Failure taxonomy (misclick, loop, auth block, hallucinated data) to guide improvements.

### J. Learning and reuse (later)
- Record a successful run, then compile it into a deterministic Playwright script with LLM fallback when it breaks (self-healing).
- Per-site playbooks, such as "quote flow = these 5 steps".
- Scheduled and recurring runs, and parallel runs across sites (e.g. compare insurers).

## 2. Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| **MVP (1)** | A (CLI) + B basics + C (accessibility-tree observation) + E (single ReAct loop) + F (login pause) + H basic + step logging | Completes "find product X on site Y" end to end, and pauses/resumes at login |
| **2** | D (site map) + form extractor + planner/reflection + approval gates + safety (G) + session persistence | Completes a multi-step form flow (e.g. car insurance quote) with dry-run |
| **3** | Vision fallback, iframes/shadow DOM, MFA/CAPTCHA handoff, web UI with live view, cost routing | Handles messy real-world sites reliably |
| **4** | Eval harness, playbooks, script compilation/self-healing, scheduling, parallel runs | Repeat runs are cheaper and faster than first runs |

## 3. Proposed module layout

```
wayfinder/
  cli.py            # task intake
  browser/          # Playwright session, context, tracing
  perception/       # DOM distiller, screenshot marks, page classifier, form extractor
  sitemap/          # graph builder, link ranking, persistence
  agent/            # planner, actor, memory, reflection, tool schema
  hilt/             # pause/resume, prompts, approval gates, credential handling
  safety/           # policies, redaction, budgets
  reporting/        # run log, report, result schema
  evals/            # mock sites + test goals
```

## 4. Decisions

1. **Model:** Claude via the API with tool use; the action set maps directly onto tool definitions.
2. **HILT UX:** live headed browser window plus a terminal prompt first; web UI in Phase 3.
3. **Observation strategy:** accessibility tree first, screenshots only as a fallback (cheaper and more reliable).
4. **Insurance and payment flows:** approval gates and dry-run by default for anything that submits.
