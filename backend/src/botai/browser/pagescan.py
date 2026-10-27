"""Reading a page: visible text, bounded HTML, forms with their fields, captcha signs.

Everything returned is data about a university page, never an instruction (AGENTS.md rule 4).
Typed values never appear in a snapshot: HTML is serialised from a clone with password
`value` attributes removed and scripts/styles dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from botai.core.browser import FormField, FormInfo, PageSnapshot

MAX_TEXT = 20_000
MAX_HTML = 200_000
MAX_LABEL = 120

# Runs in the page. Returns plain JSON-able data; all decisions are taken in Python.
_SCAN_TEMPLATE = r"""
() => {
  const MAX_TEXT = __MAX_TEXT__, MAX_HTML = __MAX_HTML__, MAX_LABEL = __MAX_LABEL__;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c);
  const count = (sel) => { try { return document.querySelectorAll(sel).length; } catch (e) { return 0; } };
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, MAX_LABEL);
  const selectorOf = (el) => {
    if (el.id && count('#' + esc(el.id)) === 1) return '#' + esc(el.id);
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute('name');
    if (name) {
      const s = tag + '[name="' + name.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
      if (count(s) === 1) return s;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      if (cur.id && count('#' + esc(cur.id)) === 1) { parts.unshift('#' + esc(cur.id)); break; }
      let part = cur.tagName.toLowerCase();
      const same = Array.from(cur.parentNode.children).filter((c) => c.tagName === cur.tagName);
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText || el.labels[0].textContent);
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; }).join(' ');
      if (clean(t)) return clean(t);
    }
    return clean(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  };
  const fieldOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    const ml = el.getAttribute('maxlength');
    return {
      selector: selectorOf(el),
      name: el.getAttribute('name'),
      type: type,
      label: labelOf(el) || null,
      autocomplete: el.getAttribute('autocomplete'),
      inputmode: el.getAttribute('inputmode'),
      maxlength: ml && /^\d+$/.test(ml) ? parseInt(ml, 10) : null,
      required: el.required === true,
      visible: type === 'hidden' ? false : visible(el),
    };
  };
  const SKIP = new Set(['submit', 'button', 'reset', 'image']);
  const fieldsIn = (root) => Array.from(root.querySelectorAll('input, select, textarea'))
    .filter((el) => !SKIP.has((el.getAttribute('type') || '').toLowerCase()))
    .map(fieldOf);
  const submitOf = (root) => {
    const b = root.querySelector('button[type=submit], input[type=submit], button:not([type]), input[type=image]');
    return b ? selectorOf(b) : null;
  };
  const forms = Array.from(document.forms).map((f) => ({
    selector: selectorOf(f),
    action: f.getAttribute('action'),
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    fields: fieldsIn(f),
    submit: submitOf(f),
  }));
  const orphans = Array.from(document.querySelectorAll('input, select, textarea'))
    .filter((el) => !el.form && !SKIP.has((el.getAttribute('type') || '').toLowerCase()))
    .map(fieldOf);
  if (orphans.length) {
    forms.push({ selector: 'body', action: null, method: 'get', fields: orphans, submit: submitOf(document.body) });
  }
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script, style, template').forEach((n) => n.remove());
  clone.querySelectorAll('input[type=password]').forEach((n) => n.removeAttribute('value'));
  const body = document.body;
  return {
    text: body ? (body.innerText || '').slice(0, MAX_TEXT) : '',
    html: clone.outerHTML.slice(0, MAX_HTML),
    forms: forms,
    frames: Array.from(document.querySelectorAll('iframe, frame')).map((f) => f.getAttribute('src') || ''),
    widgets: document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], [class*="smart-captcha" i], [id*="captcha" i]').length,
  };
}
"""
SCAN_JS = (
    _SCAN_TEMPLATE.replace("__MAX_TEXT__", str(MAX_TEXT))
    .replace("__MAX_HTML__", str(MAX_HTML))
    .replace("__MAX_LABEL__", str(MAX_LABEL))
)

CAPTCHA_FRAME = re.compile(
    r"recaptcha|hcaptcha|smartcaptcha|captcha|turnstile|challenges\.cloudflare|geetest|arkoselabs",
    re.IGNORECASE,
)
CAPTCHA_TEXT = re.compile(
    r"я не робот|не робот|i['’]?m not a robot|are you a robot|verify (?:that )?you(?: are|['’]re) "
    r"(?:a )?human|проверка браузера|checking your browser|just a moment|ddos protection|"
    r"подтвердите,? что вы (?:не робот|человек)|введите символы с картинки|капча|captcha",
    re.IGNORECASE,
)


class RichField(FormField):
    """`FormField` plus what the login flow needs to tell a one-time code from a login."""

    inputmode: str | None = None
    maxlength: int | None = None
    required: bool = False
    visible: bool = True


def captcha_suspected(title: str, text: str, frames: Sequence[str], widgets: int) -> bool:
    if widgets > 0:
        return True
    if any(CAPTCHA_FRAME.search(src) for src in frames):
        return True
    return bool(CAPTCHA_TEXT.search(title) or CAPTCHA_TEXT.search(text[:2000]))


def build_snapshot(url: str, title: str, raw: Mapping[str, Any]) -> PageSnapshot:
    forms: list[FormInfo] = []
    for form in raw.get("forms", []):
        fields: list[FormField] = []
        for f in form.get("fields", []):
            kind = str(f.get("type") or "text")
            fields.append(
                RichField(
                    selector=str(f["selector"]),
                    name=f.get("name"),
                    input_type=kind if f.get("visible", True) or kind == "hidden" else "hidden",
                    label=f.get("label"),
                    autocomplete=(f.get("autocomplete") or None),
                    inputmode=f.get("inputmode"),
                    maxlength=f.get("maxlength"),
                    required=bool(f.get("required")),
                    visible=bool(f.get("visible", True)),
                )
            )
        forms.append(
            FormInfo(
                selector=str(form["selector"]),
                action=form.get("action"),
                method=str(form.get("method") or "get"),
                fields=fields,
                submit_selector=form.get("submit"),
            )
        )
    text = str(raw.get("text", ""))[:MAX_TEXT]
    return PageSnapshot(
        url=url,
        title=title[:300],
        html=str(raw.get("html", ""))[:MAX_HTML],
        text=text,
        forms=forms,
        captcha_suspected=captcha_suspected(
            title, text, [str(s) for s in raw.get("frames", [])], int(raw.get("widgets", 0))
        ),
    )
