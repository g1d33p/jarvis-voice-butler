/* Fixture DOMs mirroring WhatsApp Web's structure (best-effort, from the
 * documented selectors in src/whatsapp_extractors.js). The real validation
 * is the Mac acceptance run; these fixtures guard the extraction logic
 * against regressions when selectors are edited.
 */

"use strict";

const { El, docWith } = require("./dom_fake");

function chatRow({ name, nameTestid = true, unread = 0, preview = "", time = "", titleText = null }) {
  const kids = [];
  if (nameTestid) {
    // titleText lets a test reproduce the live bug where the unread badge
    // nested inside the title container, so textContent included it.
    kids.push(
      new El("span", { "data-testid": "cell-frame-title" }, [titleText !== null ? titleText : name])
    );
  } else {
    kids.push(new El("span", { title: name }, [name]));
  }
  if (preview) kids.push(new El("span", {}, [preview]));
  if (time) kids.push(new El("span", {}, [time]));
  if (unread) {
    kids.push(
      new El(
        "span",
        { "aria-label": `${unread} unread messages`, class: "unread-badge" },
        [String(unread)]
      )
    );
  }
  return new El("div", { "data-testid": "cell-frame-container" }, kids);
}

function loggedInDoc() {
  const rows = [
    chatRow({ name: "Ravi Kumar", unread: 2, preview: "see you at 6", time: "10:30" }),
    chatRow({ name: "Family Group", unread: 0, preview: "Mum: call me", time: "Yesterday" }),
    chatRow({ name: "Priya", nameTestid: false, unread: 1, preview: "thanks!", time: "Monday" }),
  ];
  const scroller = new El("div", { class: "chat-scroller" }, rows);
  scroller.scrollHeight = 2000;
  scroller.clientHeight = 600;
  const pane = new El("div", { id: "pane-side" }, [scroller]);

  const messages = [
    new El(
      "div",
      { "data-testid": "msg-container", class: "message-in" },
      [
        new El("div", { "data-pre-plain-text": "[10:30, 24/09/2026] Ravi Kumar: " }, [
          new El("div", { class: "copyable-text" }, ["Are we still on for 6?"]),
        ]),
      ]
    ),
    new El(
      "div",
      { "data-testid": "msg-container", class: "message-out" },
      [
        new El("div", { "data-pre-plain-text": "[10:32, 24/09/2026] Jeevan: " }, [
          new El("div", { class: "copyable-text" }, ["Yes, see you then"]),
        ]),
      ]
    ),
    new El(
      "div",
      { "data-testid": "msg-container", class: "message-in" },
      [
        new El("div", { "data-pre-plain-text": "[10:33, 24/09/2026] Ravi Kumar: " }, [
          new El("div", { class: "copyable-text" }, [""]),
        ]),
      ]
    ),
  ];
  const main = new El("div", { id: "main" }, [
    ...messages,
    new El("div", { contenteditable: "true", "data-tab": "10", "aria-label": "Type a message" }, []),
    new El("button", { "data-testid": "send", "aria-label": "Send" }, []),
  ]);

  return docWith([pane, main]);
}

function qrDoc() {
  return docWith([
    new El("div", {}, [
      new El("div", { "data-testid": "qrcode" }, []),
      new El("h1", {}, ["Scan to log in to WhatsApp Web"]),
    ]),
  ]);
}

function loadingDoc() {
  return docWith([new El("div", {}, ["Loading WhatsApp…"])]);
}

function _msgEl(meta, text, outgoing) {
  return new El(
    "div",
    { "data-testid": "msg-container", class: outgoing ? "message-out" : "message-in" },
    [
      new El("div", { "data-pre-plain-text": meta }, [
        new El("div", { class: "copyable-text" }, [text]),
      ]),
    ]
  );
}

/* A conversation pane as it looks right after a chat is opened: the #main
 * column has a header naming the active chat and a tall scrollable message
 * pane sitting near the bottom (newest messages visible).
 */
function conversationDoc({ title = "SC1-Confidants", useTitleAttr = false, messageCount = 20 } = {}) {
  const titleEl = useTitleAttr
    ? new El("span", { title: title }, [title])
    : new El("span", { "data-testid": "conversation-title" }, [title]);
  const header = new El("header", {}, [titleEl]);
  const scroller = new El("div", { class: "msg-scroller" }, []);
  for (let i = 1; i <= messageCount; i++) {
    scroller.append(
      _msgEl(`[10:${String(i).padStart(2, "0")}, 24/09/2026] Sender: `, `message ${i}`, i % 2 === 0)
    );
  }
  scroller.scrollHeight = 3000;
  scroller.clientHeight = 800;
  scroller.scrollTop = 2200; // = scrollHeight - clientHeight
  const main = new El("div", { id: "main" }, [header, scroller]);
  return docWith([main]);
}

module.exports = { loggedInDoc, qrDoc, loadingDoc, chatRow, conversationDoc, _msgEl };
