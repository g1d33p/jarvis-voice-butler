#!/usr/bin/env python3
"""Check who-sent-what in one WhatsApp chat, the way Yaadhamma reads it.

Opens the chat in Yaadhamma's own browser and prints two sections:

  FOR YOUR EYES ONLY  - the last few messages as Yaadhamma reads them
                        (text + "YOU" or "THEM"). Compare with your phone.
                        Do NOT paste this section anywhere.
  SAFE TO PASTE       - layout measurements only (positions, widths, flags),
                        no text, names or numbers.

Run on the Mac with Yaadhamma stopped:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/whatsapp_read_check.py "0413"
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import BrowserError, BrowserManager
from whatsapp import WhatsAppClient, WhatsAppError

LAYOUT_JS = r"""() => {
  const nodes = Array.from(document.querySelectorAll('#main [data-pre-plain-text]')).slice(-8);
  return nodes.map(el => {
    const panel = el.closest('[data-testid="conversation-panel-messages"]');
    const bubble = el.closest('[data-testid="msg-container"]');
    const p = panel ? panel.getBoundingClientRect() : null;
    const measure = (node) => {
      if (!node || !p) return null;
      const r = node.getBoundingClientRect();
      return {
        width_pct: Math.round(100 * r.width / p.width),
        gap_left: Math.round(r.left - p.left),
        gap_right: Math.round(p.right - r.right),
      };
    };
    const idHost = el.closest('[data-id]');
    const id = idHost ? idHost.getAttribute('data-id') || '' : '';
    let labelled = '';
    for (let a = el; a && a !== document.body; a = a.parentElement) {
      const l = (a.getAttribute('aria-label') || '').toLowerCase();
      if (l.startsWith('you')) { labelled = 'you'; break; }
    }
    return {
      text_block: measure(el),
      bubble: measure(bubble),
      data_id_starts: /^true_/.test(id) ? 'true_' : /^false_/.test(id) ? 'false_' : 'other',
      aria_label_starts_with_you: labelled === 'you',
      has_tail_in: !!(bubble && bubble.querySelector('[data-icon="tail-in"]')),
      has_tail_out: !!(bubble && bubble.querySelector('[data-icon="tail-out"]')),
      has_check_marks: !!(bubble && bubble.querySelector('[data-icon^="msg-check"], [data-icon^="msg-dblcheck"]')),
      text_nodes: el.querySelectorAll('span.selectable-text, .copyable-text').length,
      quote_like_blocks: Array.from(el.querySelectorAll('[data-testid], [aria-label], [role="button"]'))
        .filter(a => /quote/i.test(a.getAttribute('data-testid') || '') ||
                     /quot/i.test(a.getAttribute('aria-label') || '') ||
                     a.getAttribute('role') === 'button')
        .map(a => (a.getAttribute('data-testid') || '') + '|' + (a.getAttribute('role') || '') +
                  '|' + (a.getAttribute('aria-label') ? 'labelled' : '')).slice(0, 4),
    };
  });
}"""


async def main(name: str) -> int:
    try:
        browser = BrowserManager(headless=False)
        client = WhatsAppClient(browser=browser)
        await client.ensure_tab(browser)
        print("Waiting for WhatsApp Web to load (up to 60 s)...")
        await client._require_login(timeout_s=60)
    except BrowserError as exc:
        print(f"Could not start the browser: {exc}")
        print("Stop Yaadhamma first: the browser profile allows one window.")
        return 1
    try:
        try:
            result = await client.read_messages(name, 6)
        except WhatsAppError as exc:
            print(f"Could not read that chat: {exc}")
            return 1
        print("\n===== FOR YOUR EYES ONLY (do not paste) =====")
        print(f"Chat: {result['chat']}")
        for m in result["messages"]:
            who = "YOU " if m["outgoing"] else "THEM"
            reply = (
                f"  (replying to: {m['replying_to'][:40]})"
                if m.get("replying_to")
                else ""
            )
            print(f"  {who} | {m['time']} | {m['text'][:60]}{reply}")
        layout = await browser.evaluate(LAYOUT_JS)
        print("\n===== SAFE TO PASTE (layout only, oldest first) =====")
        for i, row in enumerate(layout, 1):
            print(f"{i}. {json.dumps(row)}")
    finally:
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "0413")))
