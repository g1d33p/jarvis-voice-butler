/* Tests for src/whatsapp_extractors.js against fixture DOMs.
 * Run: node tests/js/wa_extractors.test.mjs  (exit 0 = pass)
 */

"use strict";

const assert = require("node:assert/strict");
const ex = require("../../src/whatsapp_extractors.js");
const { loggedInDoc, qrDoc, loadingDoc } = require("./fixtures.js");

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`ok - ${name}`);
  } catch (e) {
    console.error(`FAIL - ${name}\n  ${e.message}`);
    process.exitCode = 1;
  }
}

test("login state: logged in", () => {
  assert.equal(ex.waLoginState(loggedInDoc()).state, "logged_in");
});

test("login state: QR shown", () => {
  assert.equal(ex.waLoginState(qrDoc()).state, "qr");
});

test("login state: still loading", () => {
  assert.equal(ex.waLoginState(loadingDoc()).state, "loading");
});

test("list chats parses names, unread, preview, time", () => {
  const { chats } = ex.waListChats(loggedInDoc());
  assert.equal(chats.length, 3);
  const ravi = chats[0];
  assert.equal(ravi.name, "Ravi Kumar");
  assert.equal(ravi.unread, 2);
  assert.equal(ravi.preview, "see you at 6");
  assert.equal(ravi.time, "10:30");
  assert.equal(chats[1].unread, 0);
  assert.equal(chats[1].time, "Yesterday");
});

test("list chats falls back to [title] when the title testid is missing", () => {
  const { chats } = ex.waListChats(loggedInDoc());
  assert.equal(chats[2].name, "Priya");
  assert.equal(chats[2].unread, 1);
});

test("list chats with no pane returns empty", () => {
  assert.deepEqual(ex.waListChats(qrDoc()).chats, []);
});

test("click chat matches case-insensitively and clicks the row", () => {
  const doc = loggedInDoc();
  const r = ex.waClickChat(doc, "ravi kumar");
  assert.equal(r.opened, true);
  assert.equal(r.matched, "Ravi Kumar");
  const rows = doc.querySelectorAll('[data-testid="cell-frame-container"]');
  assert.equal(rows[0].clicked, true);
  assert.equal(rows[0].scrolledIntoViewCalled, true);
  assert.equal(rows[1].clicked, false);
});

test("click chat misses unknown names without clicking", () => {
  const doc = loggedInDoc();
  const r = ex.waClickChat(doc, "Nobody Here");
  assert.equal(r.opened, false);
  for (const row of doc.querySelectorAll('[data-testid="cell-frame-container"]')) {
    assert.equal(row.clicked, false);
  }
});

test("scroll chats reports row count and scrolls the list", () => {
  const doc = loggedInDoc();
  const r = ex.waScrollChats(doc);
  assert.equal(r.before, 3);
  const scroller = doc.querySelector(".chat-scroller");
  assert.equal(scroller.scrollTop, scroller.scrollHeight);
});

test("read messages parses sender, text and direction", () => {
  const { messages, error } = ex.waReadMessages(loggedInDoc(), 10);
  assert.equal(error, undefined);
  assert.equal(messages.length, 3);
  assert.equal(messages[0].meta, "[10:30, 24/09/2026] Ravi Kumar: ");
  assert.equal(messages[0].text, "Are we still on for 6?");
  assert.equal(messages[0].outgoing, false);
  assert.equal(messages[1].outgoing, true);
  assert.equal(messages[1].text, "Yes, see you then");
  // Media-only message: no text, still a message.
  assert.equal(messages[2].text, "");
});

test("read messages honors the limit", () => {
  const { messages } = ex.waReadMessages(loggedInDoc(), 2);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].text, "Yes, see you then");
});

test("read messages with no open chat reports it", () => {
  const { messages, error } = ex.waReadMessages(qrDoc(), 10);
  assert.deepEqual(messages, []);
  assert.equal(error, "no-open-chat");
});

test("type and send inserts text and clicks send", () => {
  const doc = loggedInDoc();
  const r = ex.waTypeAndSend(doc, "hello there");
  assert.equal(r.typed, true);
  assert.equal(r.clickedSend, true);
  assert.deepEqual(doc._execCommands, [{ cmd: "insertText", value: "hello there" }]);
  assert.equal(doc.querySelector('[data-testid="send"]').clicked, true);
});

test("type and send without a message box fails cleanly", () => {
  const r = ex.waTypeAndSend(qrDoc(), "hi");
  assert.equal(r.typed, false);
  assert.equal(r.reason, "no-message-box");
});

test("last message returns the newest message", () => {
  const { message } = ex.waLastMessage(loggedInDoc());
  assert.equal(message.text, "");
  assert.equal(message.outgoing, false);
  assert.equal(message.meta, "[10:33, 24/09/2026] Ravi Kumar: ");
});

console.log(`\n${passed} passed`);
