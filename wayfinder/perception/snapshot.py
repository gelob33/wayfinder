"""Observe: distil the page into numbered interactive elements plus main text.

Every interactive element gets a ``data-nav-id`` attribute that the agent uses to
refer to it (``click(7)``). Ids are reassigned on every snapshot, so the agent must
use the ids from the latest observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..safety.policy import domain_allowed, is_sign_in_url, normalize_url
from ..safety.sanitize import clean_page_text

MAX_ELEMENTS = 300
MAX_TEXT = 8000

_JS = r"""
([maxEls, maxText]) => {
  document.querySelectorAll('[data-nav-id]').forEach(e => e.removeAttribute('data-nav-id'));
  const SEL = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],' +
    '[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],' +
    '[role=combobox],[contenteditable=""],[contenteditable=true],[onclick]';
  const vis = e => {
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0;
  };
  const clip = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const labelOf = e => {
    const aria = e.getAttribute('aria-label');
    if (aria) return clip(aria, 80);
    const by = e.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean)
        .map(n => n.innerText).join(' ');
      if (t.trim()) return clip(t, 80);
    }
    if (e.id) {
      const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
      if (l && l.innerText.trim()) return clip(l.innerText, 80);
    }
    const wrap = e.closest('label');
    if (wrap && wrap.innerText.trim()) return clip(wrap.innerText, 80);
    const tag = e.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      const t = (e.type || '').toLowerCase();
      if (t === 'submit' || t === 'button') return clip(e.value, 80);
      return clip(e.placeholder || e.title || e.name, 80);
    }
    return clip(e.innerText || e.title || (e.querySelector('img') || {}).alt || e.getAttribute('value'), 80);
  };
  const SENSITIVE_NAME = /card|cvv|cvc|iban|ssn|otp|one.?time|security.?code|account.?number|routing/i;
  const out = [];
  let hasPassword = false;
  let n = 0;
  for (const e of document.querySelectorAll(SEL)) {
    if (n >= maxEls) break;
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    if (!vis(e)) continue;
    if (e.closest('[aria-hidden=true]') && tag !== 'input') continue;
    const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
    const sensitive = type === 'password' ||
      /^cc-|one-time-code|current-password|new-password/.test(ac) ||
      (tag === 'input' && SENSITIVE_NAME.test((e.name || '') + ' ' + (e.id || '')));
    if (type === 'password') hasPassword = true;
    const r = e.getBoundingClientRect();
    const item = {
      id: ++n, tag, type, label: labelOf(e),
      inView: r.bottom > 0 && r.top < innerHeight,
    };
    e.setAttribute('data-nav-id', String(item.id));
    if (tag === 'a') item.href = e.getAttribute('href');
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (type === 'checkbox' || type === 'radio') item.checked = e.checked;
      else item.value = sensitive ? '' : clip(e.value, 60);
      if (e.required) item.required = true;
    }
    if (tag === 'select') item.options = [...e.options].slice(0, 40).map(o => clip(o.text, 40));
    if (tag === 'button' || (tag === 'input' && type === 'submit')) {
      item.submits = type === 'submit' || (tag === 'button' && type === '' && !!e.form);
    }
    if (e.disabled) item.disabled = true;
    if (sensitive) item.sensitive = true;
    const form = e.form || e.closest('form');
    if (form && (form.getAttribute('method') || 'get').toLowerCase() === 'post') item.post = true;
    out.push(item);
  }
  const body = document.body;
  const bodyText = (body.innerText || '').trim();
  const cand = document.querySelector('main, [role=main], #main-content, article');
  // Use the main region only if it holds most of the page text (some sites mark up a tiny <main>).
  const candText = cand ? (cand.innerText || '').trim() : '';
  const raw = candText.length >= 0.4 * bodyText.length ? candText : bodyText;
  const text = raw.replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').slice(0, maxText);
  return {
    url: location.href, title: document.title, text, elements: out, hasPassword,
    scrollY: Math.round(scrollY), scrollHeight: document.documentElement.scrollHeight,
    viewportHeight: innerHeight,
  };
}
"""


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    elements: list[dict]
    has_password: bool
    scroll_y: int
    scroll_height: int
    viewport_height: int
    notes: list[str] = field(default_factory=list)
    # Normalised absolute URLs of the links shown to the agent (used to vet `goto`).
    link_urls: set[str] = field(default_factory=set)
    truncated: bool = False

    def element(self, nav_id: int) -> dict | None:
        return next((e for e in self.elements if e["id"] == nav_id), None)

    def render(self) -> str:
        more_below = self.scroll_y + self.viewport_height < self.scroll_height - 4
        lines = [
            f"URL: {self.url}",
            f"Title: {self.title}",
            f"Scroll: {self.scroll_y}/{self.scroll_height}px"
            + (" (more content below)" if more_below else " (at bottom)"),
        ]
        lines += [f"Note: {n}" for n in self.notes]
        if self.truncated:
            lines.append("Note: element list was truncated; scroll or use the page's search/filters to reach the rest.")
        lines.append("Interactive elements:")
        lines += ["  " + _fmt(e) for e in self.elements] or ["  (none)"]
        lines.append("Page text:")
        lines.append(self.text or "(empty)")
        return "\n".join(lines)


def _short_href(href: str) -> str:
    """Drop query/fragment (tracking junk) and clip long paths."""
    parts = urlsplit(href)
    out = parts.path or "/"
    if parts.netloc:
        out = f"{parts.netloc}{out}"
    if parts.query:
        out += "?…"
    return out if len(out) <= 110 else out[:107] + "..."


def _fmt(e: dict) -> str:
    kind = e["tag"] + (f":{e['type']}" if e.get("type") and e["tag"] == "input" else "")
    parts = [f"[{e['id']}] {kind} \"{e.get('label', '')}\""]
    if e.get("href"):
        parts.append(f"-> {_short_href(e['href'])}")
    if "checked" in e:
        parts.append("checked" if e["checked"] else "unchecked")
    elif e.get("value"):
        parts.append(f"value=\"{e['value']}\"")
    if e.get("options"):
        parts.append("options=" + "|".join(e["options"]))
    for flag in ("required", "disabled", "submits"):
        if e.get(flag):
            parts.append(flag)
    if e.get("post") and (e.get("submits") or e["tag"] in ("input", "textarea")):
        parts.append("POST-form(submitting needs approval)")
    if e.get("sensitive"):
        parts.append("SENSITIVE(never fill)")
    if not e.get("inView"):
        parts.append("offscreen")
    return " ".join(parts)


def _clean_links(elements: list[dict], base_url: str, allowed: list[str] | None) -> tuple[list[dict], set[str]]:
    """Resolve hrefs, drop off-site/tracking links and duplicate image+title links."""
    kept: list[dict] = []
    first_by_href: dict[str, int] = {}
    urls: set[str] = set()
    for e in elements:
        if e["tag"] != "a" or not e.get("href"):
            kept.append(e)
            continue
        absolute = urljoin(base_url, e["href"])
        e["href"] = absolute
        if urlsplit(absolute).scheme not in ("http", "https"):
            kept.append(e)  # mailto:, tel:, javascript: ...
            continue
        if allowed is not None and not domain_allowed(absolute, allowed) and not is_sign_in_url(absolute):
            continue  # external / affiliate redirect: the agent cannot follow it anyway
        if "/redirect/" in absolute:
            continue
        key = normalize_url(absolute)
        label = e.get("label", "")
        if key in first_by_href:
            prev = kept[first_by_href[key]]
            if prev.get("label", "").endswith(" image") and not label.endswith(" image"):
                prev["label"] = label  # prefer the readable title over "... image"
            continue
        if key != normalize_url(base_url):
            first_by_href[key] = len(kept)
        kept.append(e)
        urls.add(key)
    return kept, urls


def _evaluate_with_retry(page: Page, max_elements: int, max_text: int, attempts: int = 5) -> dict:
    """Run the extraction script, riding out navigations (e.g. the redirect right after a sign-in)."""
    for attempt in range(attempts):
        try:
            page.wait_for_load_state("domcontentloaded")
            return page.evaluate(_JS, [max_elements, max_text])
        except PlaywrightError as e:
            msg = str(e)
            transient = "context was destroyed" in msg or "navigat" in msg.lower() or "Target closed" in msg
            if not transient or attempt == attempts - 1:
                raise
            page.wait_for_timeout(700)
            try:
                page.wait_for_load_state("load", timeout=10000)
            except PlaywrightError:
                pass
    raise RuntimeError("unreachable")


def take_snapshot(
    page: Page,
    max_elements: int = MAX_ELEMENTS,
    max_text: int = MAX_TEXT,
    allowed: list[str] | None = None,
) -> Snapshot:
    data = _evaluate_with_retry(page, max_elements, max_text)
    for e in data["elements"]:  # page-controlled strings: strip control chars / fake wrapper tags
        e["label"] = clean_page_text(e.get("label", ""))
        if "value" in e:
            e["value"] = clean_page_text(e["value"])
        if "options" in e:
            e["options"] = [clean_page_text(o) for o in e["options"]]
    data["title"] = clean_page_text(data["title"])
    data["text"] = clean_page_text(data["text"])
    elements, urls = _clean_links(data["elements"], data["url"], allowed)
    return Snapshot(
        url=data["url"],
        title=data["title"],
        text=data["text"],
        elements=elements,
        has_password=data["hasPassword"],
        scroll_y=data["scrollY"],
        scroll_height=data["scrollHeight"],
        viewport_height=data["viewportHeight"],
        link_urls=urls,
        truncated=len(data["elements"]) >= max_elements,
    )
