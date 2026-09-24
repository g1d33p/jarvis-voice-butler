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

module.exports = { loggedInDoc, qrDoc, loadingDoc, chatRow };
