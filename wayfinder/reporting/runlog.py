"""Per-run log: steps.jsonl, step screenshots, result.json."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..safety.sanitize import redact_args, scrub_urls


class RunLog:
    def __init__(self, runs_dir: Path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = Path(runs_dir) / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self._steps = self.dir / "steps.jsonl"

    def screenshot_path(self, step: int) -> Path:
        return self.dir / f"step-{step:03d}.png"

    def observation(self, step: int, text: str) -> None:
        """What the agent saw after this step (for auditing its claims)."""
        (self.dir / f"step-{step:03d}.txt").write_text(scrub_urls(text), encoding="utf-8")

    def step(self, **entry) -> None:
        # Keep secrets out of the log: typed text, the person's answers, URL query strings (tokens).
        action = entry.get("action")
        if isinstance(entry.get("args"), dict):
            entry["args"] = redact_args(action, entry["args"])
        if action == "ask_human" and "outcome" in entry:
            entry["outcome"] = "The person answered: <redacted>"
        for key in ("url", "outcome"):
            if isinstance(entry.get(key), str):
                entry[key] = scrub_urls(entry[key])
        if isinstance(entry.get("changes"), list):
            entry["changes"] = [scrub_urls(str(c)) for c in entry["changes"]]
        entry["t"] = round(time.time(), 2)
        with self._steps.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def result(self, result: dict) -> Path:
        path = self.dir / "result.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path
