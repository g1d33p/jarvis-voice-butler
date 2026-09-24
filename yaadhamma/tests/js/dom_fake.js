/* Minimal DOM fake for testing the WhatsApp extractors in Node.
 *
 * Supports exactly the selector surface the extractors use: tag, #id,
 * .class, [attr], [attr="v"], [attr='v'], [attr^="v"], descendant
 * combinators, and comma groups. Anything fancier is out of scope on
 * purpose: if an extractor starts needing it, extend this file first.
 */

"use strict";

function parseSimple(part) {
  // -> {tag, id, classes[], attrs: [{name, op, value}]}
  const out = { tag: null, id: null, classes: [], attrs: [] };
  let rest = part.trim();
  const tagMatch = rest.match(/^[a-zA-Z][a-zA-Z0-9]*/);
  if (tagMatch) {
    out.tag = tagMatch[0].toUpperCase();
    rest = rest.slice(tagMatch[0].length);
  }
  const re = /#([a-zA-Z0-9_-]+)|\.([a-zA-Z0-9_-]+)|\[([a-zA-Z0-9_-]+)(?:(\^?=)(?:"([^"]*)"|'([^']*)'))?\]/g;
  let m;
  while ((m = re.exec(rest)) !== null) {
    if (m[1]) out.id = m[1];
    else if (m[2]) out.classes.push(m[2]);
    else out.attrs.push({ name: m[3], op: m[4] || null, value: m[5] ?? m[6] ?? null });
  }
  return out;
}

function matchesSimple(el, part) {
  const p = parseSimple(part);
  if (p.tag && el.tagName !== p.tag) return false;
  if (p.id && el.getAttribute("id") !== p.id) return false;
  const classAttr = ` ${el.className || ""} `;
  for (const c of p.classes) {
    if (!classAttr.includes(` ${c} `)) return false;
  }
  for (const a of p.attrs) {
    const v = el.getAttribute(a.name);
    if (v === null) return false;
    if (a.op === "=" && v !== a.value) return false;
    if (a.op === "^=" && !v.startsWith(a.value)) return false;
  }
  return true;
}

function matchesFull(el, selector) {
  // Comma groups: any branch may match.
  for (const branch of selector.split(",")) {
    const parts = branch.trim().split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    if (!matchesSimple(el, parts[parts.length - 1])) continue;
    // Descendant combinators: walk ancestors for the earlier parts.
    let ancestor = el.parentElement;
    let ok = true;
    for (let i = parts.length - 2; i >= 0; i--) {
      let found = false;
      while (ancestor) {
        if (matchesSimple(ancestor, parts[i])) {
          found = true;
          ancestor = ancestor.parentElement;
          break;
        }
        ancestor = ancestor.parentElement;
      }
      if (!found) {
        ok = false;
        break;
      }
    }
    if (ok) return true;
  }
  return false;
}

class El {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = { ...attrs };
    this.children = [];
    this.parentElement = null;
    this.clicked = false;
    this.scrolledIntoViewCalled = false;
    this.focused = false;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.scrollTop = 0;
    for (const c of children) this.append(c);
  }

  append(child) {
    if (typeof child === "string") {
      const t = new El("#text");
      t._text = child;
      child = t;
    }
    child.parentElement = this;
    this.children.push(child);
    return this;
  }

  getAttribute(name) {
    const v = this.attrs[name];
    return v === undefined ? null : String(v);
  }

  get className() {
    return this.attrs["class"] || "";
  }

  get textContent() {
    if (this.tagName === "#TEXT") return this._text || "";
    return this.children.map((c) => c.textContent).join("");
  }

  get innerText() {
    return this.textContent.replace(/\s+/g, " ");
  }

  get dataset() {
    const out = {};
    for (const [k, v] of Object.entries(this.attrs)) {
      if (k.startsWith("data-")) {
        const camel = k
          .slice(5)
          .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        out[camel] = v;
      }
    }
    return out;
  }

  _walk(acc) {
    for (const c of this.children) {
      if (c.tagName !== "#TEXT") {
        acc.push(c);
        c._walk(acc);
      }
    }
    return acc;
  }

  querySelectorAll(selector) {
    return this._walk([]).filter((el) => matchesFull(el, selector));
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }

  closest(selector) {
    let el = this;
    while (el) {
      if (el.tagName !== "#TEXT" && matchesFull(el, selector)) return el;
      el = el.parentElement;
    }
    return null;
  }

  click() {
    this.clicked = true;
  }

  focus() {
    this.focused = true;
  }

  scrollIntoView() {
    this.scrolledIntoViewCalled = true;
  }
}

function docWith(bodyChildren) {
  const body = new El("body", {}, bodyChildren);
  const doc = {
    body,
    _execCommands: [],
    querySelector(sel) {
      return body.querySelector(sel);
    },
    querySelectorAll(sel) {
      return body.querySelectorAll(sel);
    },
    execCommand(cmd, ui, value) {
      doc._execCommands.push({ cmd, value });
      return true;
    },
  };
  return doc;
}

module.exports = { El, docWith };
