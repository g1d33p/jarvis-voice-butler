/* Tests for src/whatsapp_extractors.js against fixture DOMs.
 * Run: node tests/js/wa_extractors.test.mjs  (exit 0 = pass)
 */

"use strict";

const assert = require("node:assert/strict");
const ex = require("../../src/whatsapp_extractors.js");
const { loggedInDoc, qrDoc, loadingDoc, chatRow } = require("./fixtures.js");

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
  // Priya's row has no title element and is excluded rather than guessed.
  assert.equal(chats.length, 2);
  const ravi = chats[0];
  assert.equal(ravi.name, "Ravi Kumar");
  assert.equal(ravi.unread, 2);
  assert.equal(ravi.preview, "see you at 6");
  assert.equal(ravi.time, "10:30");
  assert.equal(chats[1].unread, 0);
  assert.equal(chats[1].time, "Yesterday");
});

test("list chats ignores rows without a title element instead of guessing", () => {
  const { chats } = ex.waListChats(loggedInDoc());
  assert.deepEqual(
    chats.map((c) => c.name),
    ["Ravi Kumar", "Family Group"]
  );
});

test("unread badge text never leaks into the chat name (live regression)", () => {
  // 2026-09-24: the live run produced "No WhatsApp chat named
  // '1 unread message+1 (972) 897-4377' found." The title must come only
  // from the title element; the count only from the badge.
  const { El, docWith } = require("./dom_fake");
  const rows = [
    chatRow({
      name: "+1 (972) 897-4377",
      unread: 1,
      titleText: "1 unread message+1 (972) 897-4377", // badge nested in title container
    }),
    chatRow({
      name: "Ravi Kumar",
      unread: 2,
      titleText: "2 unread messages Ravi Kumar",
    }),
  ];
  const scroller = new El("div", { class: "chat-scroller" }, rows);
  scroller.scrollHeight = 600;
  scroller.clientHeight = 600;
  const doc = docWith([new El("div", { id: "pane-side" }, [scroller])]);
  const { chats } = ex.waListChats(doc);
  assert.equal(chats.length, 2);
  assert.equal(chats[0].name, "+1 (972) 897-4377");
  assert.equal(chats[0].unread, 1);
  assert.equal(chats[1].name, "Ravi Kumar");
  assert.equal(chats[1].unread, 2);
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

test("waScrollTop resets the list to the top (newest chats first)", () => {
  const doc = loggedInDoc();
  const scroller = doc.querySelector(".chat-scroller");
  scroller.scrollTop = 1400; // simulate a list left sitting at the bottom
  const r = ex.waScrollTop(doc);
  assert.equal(r.ok, true);
  assert.equal(scroller.scrollTop, 0);
});

test("waScrollChats walks down one viewport and reports when it stops", () => {
  const doc = loggedInDoc();
  const scroller = doc.querySelector(".chat-scroller"); // 2000 tall, 600 viewport
  let r = ex.waScrollChats(doc);
  assert.equal(r.before, 3);
  assert.equal(r.advanced, true);
  assert.equal(scroller.scrollTop, 600);
  r = ex.waScrollChats(doc);
  assert.equal(r.advanced, true);
  assert.equal(scroller.scrollTop, 1200);
  r = ex.waScrollChats(doc);
  assert.equal(r.advanced, true);
  assert.equal(scroller.scrollTop, 1400); // clamped: scrollHeight - clientHeight
  r = ex.waScrollChats(doc);
  assert.equal(r.advanced, false); // bottom reached: traversal stops
  assert.equal(scroller.scrollTop, 1400);
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
