#!/usr/bin/env python3
"""Diagnose why WhatsApp chats "would not load" in Yaadhamma's browser (v2).

Opens WhatsApp Web in Yaadhamma's own browser and, for each chat you name:
  1. describes how the chat-list row is built (to find the stray digit that
     turned "SC1-Organization1" into "SC1-Organization14"),
  2. clicks it the way Yaadhamma does (a script click) and checks whether a
     conversation actually opened (is there a message box?),
  3. if not, clicks it like a real mouse and checks again,
  4. describes how the open conversation is built: which elements hold the
     messages and which attribute carries the sender/time.

It prints STRUCTURE ONLY: element kinds, counts, attribute NAMES, text
lengths and yes/no flags. Never message text, names or numbers.

Run on the Mac with Yaadhamma stopped:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/whatsapp_probe.py "SC1-Organization1" "0413"
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import BrowserError, BrowserManager
from whatsapp import WhatsAppClient

# Shared helpers, injected into each probe.
HELPERS = r"""
const describe = (el) => {
  if (!el) return null;
  const attrs = Array.from(el.attributes || []).map(a => a.name)
    .filter(n => n !== 'class' && n !== 'style');
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id ? 'yes' : '',
    role: el.getAttribute('role') || '',
    testid: el.getAttribute('data-testid') || '',
    attrs: attrs,
    children: el.children.length,
    text_len: (el.innerText || '').length,
  };
};
const digitsOf = (s) => (s || '').replace(/\D/g, '');
const rowsOf = () => {
  const side = document.querySelector('#pane-side');
  const scope = side || document;
  let rows = scope.querySelectorAll('[data-testid="cell-frame-container"]');
  if (!rows.length) rows = scope.querySelectorAll('div[role="row"]');
  if (!rows.length) rows = scope.querySelectorAll('div[role="listitem"]');
  return Array.from(rows);
};
const rowName = (row) => {
  const t = row.querySelector('[data-testid="cell-frame-title"]');
  const titled = t ? t.querySelector('[title]') : row.querySelector('span[title]');
  return (titled && titled.getAttribute('title')) || (t ? t.textContent : '') || '';
};
const findRow = (query) => {
  const q = query.trim().toLowerCase();
  const qd = digitsOf(q);
  const onlyDigits = qd.length >= 3 && !/[a-z]/.test(q);
  return rowsOf().findIndex(r => {
    const n = rowName(r).trim().toLowerCase();
    return onlyDigits ? digitsOf(n).endsWith(qd) : (n === q || n.startsWith(q));
  });
};
"""

ROW_JS = HELPERS + r"""
(query) => {
  const rows = rowsOf();
  const i = findRow(query);
  if (i < 0) return { found: false, rows_seen: rows.length };
  const row = rows[i];
  const titleBox = row.querySelector('[data-testid="cell-frame-title"]');
  const titled = titleBox ? titleBox.querySelector('[title]') : null;
  const titleText = titleBox ? (titleBox.textContent || '').trim() : '';
  const titleAttr = titled ? titled.getAttribute('title') : '';
  return {
    found: true,
    row: describe(row),
    title_box: describe(titleBox),
    title_box_text_len: titleText.length,
    inner_title_attr_len: (titleAttr || '').length,
    // Extra characters in the title box beyond the real name (the badge?).
    extra_chars_after_name: titleAttr && titleText.startsWith(titleAttr)
      ? titleText.slice(titleAttr.length).replace(/\d/g, '9') : 'n/a',
    badge_labels: Array.from(row.querySelectorAll('[aria-label]'))
      .map(e => (e.getAttribute('aria-label') || '').replace(/\d+/g, 'N'))
      .filter(l => /unread/i.test(l)),
  };
}
"""

CLICK_JS = HELPERS + r"""
(query) => {
  const rows = rowsOf();
  const i = findRow(query);
  if (i < 0) return { clicked: false };
  rows[i].scrollIntoView();
  rows[i].click();
  return { clicked: true, index: i };
}
"""

ROW_BOX_JS = HELPERS + r"""
(query) => {
  const rows = rowsOf();
  const i = findRow(query);
  if (i < 0) return null;
  rows[i].scrollIntoView();
  const r = rows[i].getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}
"""

OPEN_JS = HELPERS + r"""
() => {
  const composer = document.querySelector(
    'footer [contenteditable="true"], [contenteditable="true"][data-tab="10"], ' +
    'div[contenteditable="true"][data-lexical-editor="true"]');
  const main = document.querySelector('#main');
  // Any attribute whose VALUE looks like WhatsApp's "[10:30, 24/09/2026] Name: "
  const stampAttrs = {};
  let stamped = [];
  document.querySelectorAll('*').forEach(el => {
    for (const a of el.attributes || []) {
      if (/^\[\d{1,2}:\d{2}/.test(a.value || '')) {
        stampAttrs[a.name] = (stampAttrs[a.name] || 0) + 1;
        if (stamped.length < 400) stamped.push(el);
      }
    }
  });
  // The message list: nearest scrollable ancestor of the stamped elements.
  let list = null;
  if (stamped.length) {
    let el = stamped[stamped.length - 1];
    while (el && el !== document.body) {
      if (el.scrollHeight > el.clientHeight + 4) { list = el; break; }
      el = el.parentElement;
    }
  }
  // Path from a stamped message up to the page, to see what replaced #main.
  const path = [];
  let el = stamped.length ? stamped[stamped.length - 1] : null;
  while (el && el !== document.body && path.length < 14) {
    path.push(describe(el));
    el = el.parentElement;
  }
  const composerDesc = describe(composer);
  const composerLabel = composer
    ? ((composer.getAttribute('aria-label') || '') + ' ' +
       (composer.getAttribute('aria-placeholder') || '')).trim().length : 0;
  return {
    conversation_open: !!composer,
    composer: composerDesc,
    composer_label_len: composerLabel,
    main_exists: !!main,
    timestamp_attributes: stampAttrs,
    stamped_elements: stamped.length,
    message_list: describe(list),
    path_up_from_last_message: path,
    header_candidates: Array.from(document.querySelectorAll('header'))
      .map(h => ({ ...describe(h), testids: Array.from(h.querySelectorAll('[data-testid]'))
        .map(e => e.getAttribute('data-testid')).slice(0, 12) })),
    copyable_text_on_page: document.querySelectorAll('.copyable-text').length,
    message_in_out: [
      document.querySelectorAll('.message-in').length,
      document.querySelectorAll('.message-out').length,
    ],
  };
}
"""


async def probe_chat(browser: BrowserManager, page, name: str) -> None:
    print(f"\n===== {name!r} =====")
    row = await page.evaluate(ROW_JS, name)
    print("chat-list row:", json.dumps(row, indent=1))
    if not row.get("found"):
        return

    await page.evaluate(CLICK_JS, name)
    await asyncio.sleep(5)
    after_script = await page.evaluate(OPEN_JS)
    print("after SCRIPT click:", json.dumps(after_script, indent=1))
    if after_script.get("conversation_open"):
        return

    await page.keyboard.press("Escape")
    await asyncio.sleep(1)
    box = await page.evaluate(ROW_BOX_JS, name)
    if box:
        await page.mouse.click(box["x"], box["y"])
        await asyncio.sleep(5)
        print("after MOUSE click:", json.dumps(await page.evaluate(OPEN_JS), indent=1))


async def main(names: list[str]) -> int:
    try:
        browser = BrowserManager(headless=False)
        client = WhatsAppClient(browser=browser)
        await client.ensure_tab(browser)
        print("Waiting for WhatsApp Web to load (up to 60 s)...")
        await client._require_login(timeout_s=60)
        page = await browser._get_page()
    except BrowserError as exc:
        print(f"Could not start the browser: {exc}")
        print("Stop Yaadhamma first: the browser profile allows one window.")
        return 1
    try:
        for name in names:
            await probe_chat(browser, page, name)
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
    finally:
        await browser.close()
    return 0


if __name__ == "__main__":
    targets = sys.argv[1:] or ["SC1"]
    raise SystemExit(asyncio.run(main(targets)))
