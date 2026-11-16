"""Guardrails: domain allowlist, sign-in detection, risky-action detection."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Sign-in pages: identity hosts (id., login., sso., ...) or sign-in paths.
SIGN_IN_URL = re.compile(
    r"^https?://(id|login|auth|sso|signin|accounts?)\.|"
    r"/(log-?in|sign-?in|sso|saml|oauth2?|authorize|join|passwordless-auth)(/|\?|$)",
    re.IGNORECASE,
)

# Clicks that probably cannot be undone: need human approval (or are blocked in dry-run).
RISKY_LABEL = re.compile(
    r"\b(submit|pay|purchase|buy now|place order|confirm (order|purchase|payment)|"
    r"checkout|complete order|delete|remove account|cancel (policy|subscription)|"
    r"send|book now|subscribe|sign up|register|apply now)\b",
    re.IGNORECASE,
)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def default_allowed_domains(start_url: str) -> list[str]:
    return [_strip_www(host_of(start_url))]


def domain_allowed(url: str, allowed: list[str]) -> bool:
    if urlsplit(url).scheme not in ("http", "https"):
        return False
    host = _strip_www(host_of(url))
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_sign_in_url(url: str) -> bool:
    return bool(SIGN_IN_URL.search(url))


def is_risky_click(element: dict) -> bool:
    # Label-based on purpose: plain "Search" / "Get quote" submits are normal navigation.
    return bool(RISKY_LABEL.search(element.get("label", "")))


def normalize_url(url: str) -> str:
    """scheme + host + path, no query/fragment/trailing slash (for comparing links)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{_strip_www(parts.netloc.lower())}{parts.path.rstrip('/')}"


_BOT_TITLES = re.compile(r"^(just a moment|attention required|access denied|security check|verify you are human)", re.IGNORECASE)
_BOT_TEXT = re.compile(
    r"performing security verification|verify you are human|verifying you are human|checking your browser|"
    r"checking if the site connection is secure|enable javascript and cookies to continue|"
    r"protect against malicious bots|unusual traffic from your computer",
    re.IGNORECASE,
)


def is_bot_challenge(title: str, text: str) -> bool:
    """A bot-verification / CAPTCHA interstitial (e.g. Cloudflare) instead of the real page."""
    return bool(_BOT_TITLES.search(title or "")) or (len(text or "") < 1200 and bool(_BOT_TEXT.search(text or "")))
