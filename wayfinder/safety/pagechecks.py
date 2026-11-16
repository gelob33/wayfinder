"""Small in-page checks (JavaScript) shared by the simple tools and the generated-code guards."""

# Element is a password / card / one-time-code style field.
SENSITIVE_JS = """e => {
  const t = (e.getAttribute('type') || '').toLowerCase();
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
  return t === 'password' || /^cc-|one-time-code|current-password|new-password/.test(ac) ||
    (e.tagName === 'INPUT' && /card|cvv|cvc|iban|ssn|otp|one.?time|security.?code|account.?number|routing/i
      .test((e.name || '') + ' ' + (e.id || '')));
}"""

# Element sits in a form that submits with POST (i.e. submitting can change something).
IN_POST_FORM_JS = """e => {
  const f = e.form || (e.closest && e.closest('form'));
  return !!f && (f.getAttribute('method') || 'get').toLowerCase() === 'post';
}"""

# Element is a control whose click submits its form.
IS_SUBMIT_CONTROL_JS = """e => {
  const tag = e.tagName, t = (e.getAttribute('type') || '').toLowerCase();
  if (tag === 'INPUT') return t === 'submit' || t === 'image';
  const b = tag === 'BUTTON' ? e : (e.closest && e.closest('button'));
  if (!b) return false;
  const bt = (b.getAttribute('type') || '').toLowerCase();
  return (bt === '' || bt === 'submit') && !!b.form;
}"""

LABEL_JS = "e => (e.getAttribute('aria-label') || e.innerText || e.value || '').trim().slice(0, 100)"

# Label of a button-like element (Enter/Space on it acts like a click); '' for anything else.
BUTTON_LABEL_JS = """e => {
  const t = (e.getAttribute('type') || '').toLowerCase();
  const buttonish = e.tagName === 'BUTTON' || e.tagName === 'A' || e.getAttribute('role') === 'button' ||
    (e.tagName === 'INPUT' && ['submit', 'button', 'image'].includes(t));
  return buttonish ? (e.getAttribute('aria-label') || e.innerText || e.value || '').trim().slice(0, 100) : '';
}"""


def on_active(js: str) -> str:
    """Wrap an element predicate so it runs on document.activeElement."""
    return "(() => { const e = document.activeElement; return !!e && (" + js + ")(e); })()"


ACTIVE_SENSITIVE_JS = on_active(SENSITIVE_JS)
ACTIVE_IN_POST_FORM_JS = on_active(IN_POST_FORM_JS)
ACTIVE_BUTTON_LABEL_JS = "(() => { const e = document.activeElement; return e ? (" + BUTTON_LABEL_JS + ")(e) : ''; })()"

ENTER_KEYS = ("enter", "numpadenter")
ACTIVATE_KEYS = ENTER_KEYS + (" ", "space")  # keys that "click" a focused button
