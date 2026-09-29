/* Tests for src/whatsapp_extractors.js against fixture DOMs.
 * Run: node tests/js/wa_extractors.test.mjs  (exit 0 = pass)
 */

"use strict";

const assert = require("node:assert/strict");
const ex = require("../../src/whatsapp_extractors.js");
const { loggedInDoc, qrDoc, loadingDoc, chatRow, conversationDoc, voiceNoteDoc } = require("./fixtures.js");

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


// --- Conversation header title (wrong-chat protection) -------------------

test("waConversationTitle returns the active chat's header title", () => {
  const { title } = ex.waConversationTitle(conversationDoc({ title: "SC1-Confidants" }));
  assert.equal(title, "SC1-Confidants");
});

test("waConversationTitle falls back to a [title] attribute span", () => {
  const { title } = ex.waConversationTitle(
    conversationDoc({ title: "SC1-Executives", useTitleAttr: true })
  );
  assert.equal(title, "SC1-Executives");
});

test("waConversationTitle returns empty string when no conversation is open", () => {
  assert.equal(ex.waConversationTitle(qrDoc()).title, "");
  assert.equal(ex.waConversationTitle(loadingDoc()).title, "");
});

// --- Message-pane scroll-up pagination -----------------------------------

test("waScrollMessagesUp scrolls the message pane up one viewport", () => {
  const doc = conversationDoc();
  const scroller = doc.querySelector(".msg-scroller");
  const r = ex.waScrollMessagesUp(doc);
  assert.equal(r.ok, true);
  assert.equal(r.advanced, true);
  assert.equal(r.atTop, false);
  assert.equal(scroller.scrollTop, 2200 - 800);
});

test("waScrollMessagesUp reports atTop and no advance at the top", () => {
  const doc = conversationDoc();
  const scroller = doc.querySelector(".msg-scroller");
  scroller.scrollTop = 100;
  const r = ex.waScrollMessagesUp(doc);
  assert.equal(r.ok, true);
  assert.equal(r.advanced, true);
  assert.equal(r.atTop, true);
  assert.equal(scroller.scrollTop, 0);
  const r2 = ex.waScrollMessagesUp(doc);
  assert.equal(r2.advanced, false);
  assert.equal(r2.atTop, true);
});

test("waScrollMessagesUp fails cleanly when there is no message pane", () => {
  const r = ex.waScrollMessagesUp(qrDoc());
  assert.equal(r.ok, false);
});

// --- 2026-09-25 layout (probe run on Jeevan's Mac) ------------------------
const { El, docWith } = require("./dom_fake");

test("row title comes from the inner [title], not the badge-polluted text", () => {
  // Live: "SC1-Organization1" with 4 unread was read as "SC1-Organization14".
  const titleBox = new El("div", { "data-testid": "cell-frame-title" }, [
    new El("span", { title: "SC1-Organization1" }, ["SC1-Organization1"]),
    new El("span", {}, ["4"]),
    new El("span", { "aria-label": "4 unread messages" }, ["4 unread messages"]),
  ]);
  const row = new El("div", { "data-testid": "cell-frame-container" }, [titleBox]);
  const doc = docWith([new El("div", { id: "pane-side" }, [row])]);
  const { chats } = ex.waListChats(doc);
  assert.equal(chats[0].name, "SC1-Organization1");
  assert.equal(chats[0].unread, 4);
});

test("conversation title uses the 2026 chat-title testid in the conversation header", () => {
  const drawerHeader = new El("header", {}, [new El("span", { title: "Profile details" }, ["Profile details"])]);
  const convHeader = new El("header", { "data-testid": "conversation-header" }, [
    new El("div", { "data-testid": "conversation-info-header-chat-title" }, ["SC1-Executives"]),
  ]);
  const doc = docWith([new El("div", { id: "main" }, [drawerHeader, convHeader])]);
  assert.equal(ex.waConversationTitle(doc).title, "SC1-Executives");
});

test("message direction comes from data-id when the message-out class is gone", () => {
  const msg = (id, meta) =>
    new El("div", { "data-id": id }, [
      new El("div", { "data-testid": "msg-container" }, [
        new El("div", { "data-pre-plain-text": meta }, [new El("div", { class: "copyable-text" }, ["x"])]),
      ]),
    ]);
  const doc = docWith([
    new El("div", { id: "main" }, [
      msg("false_120363@g.us_ABC", "[10:30, 25/09/2026] Ravi: "),
      msg("true_120363@g.us_DEF", "[10:31, 25/09/2026] Jeevan: "),
    ]),
  ]);
  const { messages } = ex.waReadMessages(doc, 10);
  assert.deepEqual(messages.map((m) => m.outgoing), [false, true]);
});

test("a reply's own text is read, and the quoted message kept separately", () => {
  // Live 2026-09-27: Jeevan replied "Mm" to "yeahhhhhhh"; the reader
  // returned the quoted "yeahhhhhhh" as his message.
  const quote = new El("div", { role: "button", "aria-label": "Quoted message" }, [
    new El("span", { class: "quoted-mention" }, [
      new El("span", { class: "selectable-text" }, ["yeahhhhhhh"]),
    ]),
  ]);
  const own = new El("span", { class: "selectable-text copyable-text" }, ["Mm"]);
  const block = new El("div", { "data-pre-plain-text": "[23:24, 26/9/2026] Jeevan: " }, [quote, own]);
  const doc = docWith([new El("div", { id: "main" }, [block])]);
  const [msg] = ex.waReadMessages(doc, 5).messages;
  assert.equal(msg.text, "Mm");
  assert.equal(msg.quoted, "yeahhhhhhh");
});

test("a plain message has no quoted field", () => {
  const block = new El("div", { "data-pre-plain-text": "[1:00, 1/1/2026] A: " }, [
    new El("span", { class: "selectable-text copyable-text" }, ["hello"]),
  ]);
  const doc = docWith([new El("div", { id: "main" }, [block])]);
  const [msg] = ex.waReadMessages(doc, 5).messages;
  assert.equal(msg.text, "hello");
  assert.equal(msg.quoted, undefined);
});


console.log(`\n${passed} passed`);

test("read messages flags a voice note with its duration", () => {
  const { messages } = ex.waReadMessages(voiceNoteDoc(), 10);
  assert.equal(messages.length, 1);
  assert.ok(messages[0].voice, "voice note not detected");
  assert.equal(messages[0].voice.duration_s, 37);
  assert.equal(messages[0].outgoing, true);
});

test("read messages does not flag plain text as a voice note", () => {
  const { messages } = ex.waReadMessages(loggedInDoc(), 10);
  assert.ok(messages.length > 0);
  for (const m of messages) assert.equal(m.voice, undefined);
});

test("voice note download reports a missing message honestly", async () => {
  const r = await ex.waVoiceNoteAudio(voiceNoteDoc(), "[never] Nobody: ");
  assert.equal(r.ok, false);
  assert.equal(r.reason, "message-not-found");
});
