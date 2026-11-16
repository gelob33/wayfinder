"""Verify support: describe, in plain facts, what changed between two observations."""

from __future__ import annotations

import re
from collections import defaultdict

from .snapshot import Snapshot

_COUNT = re.compile(
    r"\b\d[\d,]*(?: of \d[\d,]*)? (?:results?|items?|products?|matches|cards?|offers?|listings?)\b", re.IGNORECASE
)
MAX_LINES = 14


def _key(e: dict) -> tuple:
    return (e["tag"], e.get("type", ""), e.get("label", ""))


def _index(snap: Snapshot) -> dict[tuple, list[dict]]:
    idx: dict[tuple, list[dict]] = defaultdict(list)
    for e in snap.elements:
        idx[_key(e)].append(e)
    return idx


def _state(e: dict) -> str | None:
    if "checked" in e:
        return "checked" if e["checked"] else "unchecked"
    if e["tag"] in ("input", "textarea", "select") and e.get("value"):
        return f'value "{e["value"]}"'
    return None


def count_text(snap: Snapshot) -> str | None:
    m = _COUNT.search(snap.text)
    return m.group(0) if m else None


def describe_change(prev: Snapshot | None, new: Snapshot) -> tuple[list[str], bool]:
    """Return (report lines, changed). ``changed`` is False when the page looks identical."""
    if prev is None:
        return [], True
    lines: list[str] = []
    changed = False

    if prev.url != new.url:
        changed = True
        lines.append(f"URL changed: {prev.url} -> {new.url}")
    if prev.title != new.title:
        changed = True
        lines.append(f'Title changed: "{prev.title}" -> "{new.title}"')

    old_idx, new_idx = _index(prev), _index(new)
    for key, news in new_idx.items():
        olds = old_idx.get(key)
        if not olds:
            continue
        before, after = _state(olds[0]), _state(news[0])
        if before != after and (before or after):
            changed = True
            lines.append(f'{key[0]}{":" + key[1] if key[1] else ""} "{key[2]}": {before or "empty"} -> {after or "empty"}')

    added = [k for k in new_idx if k not in old_idx]
    removed = [k for k in old_idx if k not in new_idx]
    if added or removed:
        changed = True
        if added:
            sample = ", ".join(f'"{k[2]}"' for k in added[:5] if k[2])
            lines.append(f"{len(added)} new element(s) appeared" + (f", e.g. {sample}" if sample else ""))
        if removed:
            sample = ", ".join(f'"{k[2]}"' for k in removed[:5] if k[2])
            lines.append(f"{len(removed)} element(s) disappeared" + (f", e.g. {sample}" if sample else ""))

    c_old, c_new = count_text(prev), count_text(new)
    if c_old != c_new and (c_old or c_new):
        changed = True
        lines.append(f'Result count text: "{c_old or "none"}" -> "{c_new or "none"}"')

    if prev.text != new.text:
        changed = True
        if not lines:
            lines.append("Page text changed (content updated).")
    if prev.scroll_y != new.scroll_y:
        lines.append(f"Scroll position: {prev.scroll_y} -> {new.scroll_y}px")
        changed = True

    return lines[:MAX_LINES], changed
