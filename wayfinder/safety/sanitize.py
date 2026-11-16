"""Neutralise untrusted text (page content, model output) before it reaches the model, terminal or logs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# Control characters except tab/newline (ANSI escapes can spoof terminal prompts).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# The observation is wrapped in <page>...</page>; page text must not be able to close it.
_PAGE_TAG = re.compile(r"<\s*(/?)\s*page\s*>", re.IGNORECASE)


def clean(text: str) -> str:
    """Strip control characters."""
    return _CONTROL.sub("", text or "")


def clean_page_text(text: str) -> str:
    """Strip control characters and defuse fake <page> wrapper tags."""
    return _PAGE_TAG.sub(lambda m: f"<{m.group(1)}page_>", clean(text))


def strip_query(url: str) -> str:
    """URL without query string or fragment (these often carry session tokens)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    marker = "?[query removed]" if p.query else ""
    return urlunsplit((p.scheme, p.netloc, p.path, "", "")) + marker


_URL_WITH_QUERY = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")


def scrub_urls(text: str) -> str:
    """Remove query strings/fragments from any URLs inside free text."""
    return _URL_WITH_QUERY.sub(r"\1?[query removed]", text or "")


def redact_args(action: str, args: dict) -> dict:
    """Copy of tool args that is safe to write to the run log."""
    out = dict(args)
    if action == "type" and "text" in out:
        out["text"] = f"<redacted {len(str(out['text']))} chars>"
    if "url" in out:
        out["url"] = strip_query(str(out["url"]))
    return out
