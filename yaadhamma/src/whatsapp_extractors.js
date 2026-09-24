/* WhatsApp Web DOM extractors for Yaadhamma.
 *
 * Every WhatsApp-Web-specific selector lives in this file and nowhere else.
 * WhatsApp's DOM changes every few months; when it does, these functions
 * return empty results (they never throw), the Python side raises a clear
 * "layout changed" error, and only this file needs updating.
 *
 * Each function takes `doc` (the page's document) so it can be unit-tested
 * in Node with a fixture DOM. In the browser, `doc` is `document`.
 *
 * Conventions:
 * - Primary selector first, structural fallbacks after, each marked.
 * - Missing fields come back as "" or 0, never as an exception.
 */

"use strict";

function waLoginState(doc) {
  // Logged in: the chat list pane exists.
  if (doc.querySelector("#pane-side")) return { state: "logged_in" };
  // Not paired: the QR code is on screen.
  if (
    doc.querySelector('[data-testid="qrcode"]') ||
    doc.querySelector('canvas[aria-label^="Scan"]')
  ) {
    return { state: "qr" };
  }
  var bodyText = doc.body ? doc.body.innerText || "" : "";
  if (/scan/i.test(bodyText) && /whatsapp/i.test(bodyText)) return { state: "qr" };
  return { state: "loading" };
}

function waChatRows(doc) {
  var side = doc.querySelector("#pane-side");
  if (!side) return [];
  // Primary: stable testid used by WhatsApp Web for years.
  var rows = side.querySelectorAll('[data-testid="cell-frame-container"]');
  // Fallbacks: ARIA roles, if WhatsApp drops the testids.
  if (!rows.length) rows = side.querySelectorAll('div[role="row"]');
  if (!rows.length) rows = side.querySelectorAll('div[role="listitem"]');
  return Array.prototype.slice.call(rows);
}

function waRowTitle(row) {
  // The chat name comes ONLY from the dedicated title element -- never from
  // a generic [title] attribute. WhatsApp puts badge/ARIA text like
  // "1 unread message" on nearby elements, and that text once leaked into a
  // chat name ("No WhatsApp chat named '1 unread message+1 (972) 897-4377'
  // found"). The unread count comes ONLY from the badge (waRowUnread).
  var t = row.querySelector('[data-testid="cell-frame-title"]');
  if (!t) return "";
  var text = (t.textContent || "").trim();
  // Defensive: in some layouts the unread badge nests inside the title
  // container, so its text lands in textContent. Strip a leading
  // "N unread message(s)" prefix; the count itself still comes only from
  // the badge.
  return text.replace(/^\d+\s+unread\s+messages?\s*/i, "");
}

function waRowUnread(row) {
  // Primary: WhatsApp marks the unread badge with an aria-label like
  // "3 unread messages". Matched in JS (not in the selector) so the
  // comparison is case-insensitive everywhere.
  var spans = row.querySelectorAll("span");
  var i, label, m;
  for (i = 0; i < spans.length; i++) {
    label = (spans[i].getAttribute("aria-label") || "").toLowerCase();
    m = label.match(/(\d+)\s+unread/);
    if (m) return parseInt(m[1], 10);
  }
  // Fallback: a lone small number in a badge-like element.
  for (i = 0; i < spans.length; i++) {
    var txt = (spans[i].textContent || "").trim();
    if (/^\d{1,3}$/.test(txt) && /badge|unread/i.test(spans[i].className || "")) {
      return parseInt(txt, 10);
    }
  }
  return 0;
}

// Timestamps look like "10:30", "Yesterday", "Monday", "24/09/2026".
var WA_TIME_RE =
  /^\d{1,2}:\d{2}$|^yesterday$|^(mon|tue|wed|thu|fri|sat|sun)(day)?$|^\d{1,2}\/\d{1,2}(\/\d{2,4})?$/i;

function waRowDetails(row) {
  var title = waRowTitle(row);
  var unread = waRowUnread(row);
  var preview = "";
  var time = "";
  // Best-effort: gather leaf texts, drop the title and the unread badge,
  // treat a trailing time-like token as the timestamp and the longest
  // remaining text as the last-message preview.
  var spans = row.querySelectorAll("span");
  var texts = [];
  var i, t;
  for (i = 0; i < spans.length; i++) {
    t = (spans[i].textContent || "").trim();
    if (!t || t === title) continue;
    if (/^\d{1,3}$/.test(t)) continue; // unread badge
    texts.push(t);
  }
  for (i = texts.length - 1; i >= 0; i--) {
    if (WA_TIME_RE.test(texts[i])) {
      time = texts[i];
      texts.splice(i, 1);
      break;
    }
  }
  texts.sort(function (a, b) {
    return b.length - a.length;
  });
  if (texts.length) preview = texts[0];
  return { name: title, unread: unread, preview: preview, time: time };
}

function waListChats(doc) {
  var rows = waChatRows(doc);
  return {
    chats: rows
      .map(waRowDetails)
      .filter(function (c) {
        return c.name;
      }),
  };
}

function waClickChat(doc, name) {
  var rows = waChatRows(doc);
  var target = (name || "").trim().toLowerCase();
  for (var i = 0; i < rows.length; i++) {
    if (waRowTitle(rows[i]).toLowerCase() === target) {
      if (rows[i].scrollIntoView) rows[i].scrollIntoView();
      rows[i].click();
      return { opened: true, matched: waRowTitle(rows[i]) };
    }
  }
  return { opened: false, reason: "not-visible" };
}

function _chatScroller(doc) {
  // Find the scrollable ancestor of the chat list (the virtualized pane).
  var rows = waChatRows(doc);
  var anchor = rows.length ? rows[rows.length - 1] : doc.querySelector("#pane-side");
  if (!anchor) return null;
  var el = anchor;
  while (el && el !== doc.body) {
    if (el.scrollHeight > el.clientHeight + 4) break;
    el = el.parentElement;
  }
  return el && el !== doc.body ? el : null;
}

function waScrollTop(doc) {
  // Reset to the top of the chat list. The list is ordered newest-first,
  // so traversal must always start here: unread badges on the newest chats
  // are captured first.
  var el = _chatScroller(doc);
  if (!el) return { ok: false };
  el.scrollTop = 0;
  return { ok: true };
}

function waScrollChats(doc) {
  // Scroll DOWN by exactly one viewport so the next window of virtualized
  // rows renders. Returns whether the view actually advanced: once the
  // bottom is reached nothing moves and traversal stops. (A previous
  // version jumped straight to the bottom, which walked past the newest
  // chats and reported "no unread" while 3-4 unread chats sat at the top.)
  var rows = waChatRows(doc);
  var el = _chatScroller(doc);
  if (!el) return { before: rows.length, advanced: false };
  var max = Math.max(0, el.scrollHeight - el.clientHeight);
  var next = Math.min(el.scrollTop + el.clientHeight, max);
  var advanced = next > el.scrollTop;
  el.scrollTop = next;
  return { before: rows.length, advanced: advanced };
}

function waReadMessages(doc, limit) {
  var main = doc.querySelector("#main");
  if (!main) return { messages: [], error: "no-open-chat" };
  // data-pre-plain-text is the long-stable hook: "[10:30, 24/09/2026] Ravi: "
  var nodes = main.querySelectorAll("[data-pre-plain-text]");
  var start = Math.max(0, nodes.length - limit);
  var out = [];
  for (var i = start; i < nodes.length; i++) {
    var el = nodes[i];
    var meta = el.getAttribute("data-pre-plain-text") || "";
    var textEl = el.querySelector(".copyable-text");
    var text = textEl
      ? textEl.innerText || textEl.textContent || ""
      : "";
    var host = el.closest('[data-testid="msg-container"]') || el;
    var cls = String((host.className || "") + " " + (el.className || ""));
    out.push({ meta: meta, text: text, outgoing: /message-out/.test(cls) });
  }
  return { messages: out };
}

function waTypeAndSend(doc, text) {
  // The message box is a contenteditable div (WhatsApp moved it to a Lexical
  // editor in 2024; several selectors are tried in order).
  var box =
    doc.querySelector('div[contenteditable="true"][data-tab="10"]') ||
    doc.querySelector('[aria-label="Type a message"]') ||
    doc.querySelector('div[contenteditable="true"][data-lexical-editor="true"]') ||
    doc.querySelector("#main div[contenteditable='true']");
  if (!box) return { typed: false, reason: "no-message-box" };
  if (box.focus) box.focus();
  // insertText (not setting innerHTML) fires the input events WhatsApp's
  // editor listens for; without them the Send button stays disabled.
  var ok = false;
  try {
    ok = doc.execCommand("insertText", false, text);
  } catch (e) {
    ok = false;
  }
  if (!ok) return { typed: false, reason: "insert-failed" };
  var btn =
    doc.querySelector('[data-testid="send"]') ||
    doc.querySelector('button[aria-label="Send"]');
  if (btn) {
    btn.click();
    return { typed: true, clickedSend: true };
  }
  // No send button (e.g. layout change): the Python side presses Enter.
  return { typed: true, clickedSend: false };
}

function waLastMessage(doc) {
  var r = waReadMessages(doc, 1);
  return { message: r.messages.length ? r.messages[0] : null };
}

// Export for the Node test harness; guarded so page.evaluate is unaffected.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    waLoginState: waLoginState,
    waChatRows: waChatRows,
    waRowDetails: waRowDetails,
    waListChats: waListChats,
    waClickChat: waClickChat,
    waScrollChats: waScrollChats,
    waScrollTop: waScrollTop,
    waReadMessages: waReadMessages,
    waTypeAndSend: waTypeAndSend,
    waLastMessage: waLastMessage,
  };
}
