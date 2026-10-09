/* domshim.mjs — the minimal DOM this project's pages need, for driving
 * the real ES modules in node against a live server. Not a browser:
 * no layout, no CSS, no rendering. It can prove wiring, data flow and
 * DOM shape; it cannot see colour or overlap. */

class ClassList {
  constructor(owner) { this.owner = owner; }
  _list() { return this.owner.className.split(/\s+/).filter(Boolean); }
  add(...names) {
    const list = this._list();
    for (const name of names) if (!list.includes(name)) list.push(name);
    this.owner.className = list.join(" ");
  }
  remove(...names) {
    this.owner.className =
      this._list().filter((n) => !names.includes(n)).join(" ");
  }
  toggle(name, force) {
    const has = this._list().includes(name);
    const want = force === undefined ? !has : force;
    if (want) this.add(name); else this.remove(name);
    return want;
  }
  contains(name) { return this._list().includes(name); }
}

export class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.classList = new ClassList(this);
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this._text = "";
    this.value = "";
    this.dataset = {};
  }
  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent).join("") + this._text;
  }
  set textContent(text) {
    this.children = [];
    this._text = String(text);
  }
  appendChild(node) {
    // A DocumentFragment is never itself inserted -- its OWN children
    // move into the real parent, then the fragment empties (matches
    // real DOM behaviour, which api.js's buildOwnerControls-style
    // fragment-then-append idiom relies on).
    if (node instanceof DocumentFragment) {
      for (const child of node.children) {
        child.parentNode = this;
        this.children.push(child);
      }
      node.children = [];
      this._text = "";
      return node;
    }
    node.parentNode = this;
    this.children.push(node);
    // NET FIX (transcribe_lib.mjs's discovery): a plain `el.textContent =
    // "x"` assignment stores its value in `_text` rather than as a real
    // child node -- an optimisation this shim takes that a real DOM does
    // not (`.textContent = "x"` creates one actual Text child there).
    // The divergence is invisible UNTIL code later does
    // `clearNode(el); el.appendChild(...)` (removeChild only touches
    // `.children`, never `._text`) -- in a real browser that sequence
    // fully replaces the content; here, without this line, the stale
    // `_text` from the earlier plain assignment keeps leaking back into
    // `.textContent`'s getter, DOUBLING what a real page shows. Exactly
    // this pattern: test.js's optimistic identity-line render
    // (`.textContent = "env · script"`, shown before the API answers)
    // followed by the real render's `clearNode()` + two `appendChild()`
    // calls once data arrives.
    this._text = "";
    return node;
  }
  removeChild(node) {
    this.children = this.children.filter((c) => c !== node);
    return node;
  }
  insertBefore(node, refNode) {
    node.parentNode = this;
    const at = refNode === null ? -1 : this.children.indexOf(refNode);
    if (at === -1) {
      this.children.push(node);
    } else {
      this.children.splice(at, 0, node);
    }
    this._text = "";
    return node;
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const at = this.parentNode.children.indexOf(this);
    return this.parentNode.children[at + 1] || null;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  get firstChild() { return this.children[0] || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  hasAttribute(name) { return name in this.attributes; }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  async dispatch(type) {
    for (const handler of this.listeners[type] || []) {
      await handler({ target: this });
    }
    // Browsers also invoke the on<type> property; pages here rebind
    // onclick per render precisely so handlers cannot stack.
    const property = this["on" + type];
    if (typeof property === "function") {
      await property({ target: this });
    }
  }
  async click() { await this.dispatch("click"); }
  scrollIntoView() { /* no layout here */ }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const wanted = selector.replace(/^\./, "");
    const found = [];
    const walk = (node) => {
      if (!node.children) return;   // a TextNode leaf, not an Element
      for (const child of node.children) {
        if (child.classList && child.classList.contains(wanted)) {
          found.push(child);
        }
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  /** Test helper: all descendants matching a predicate. */
  find(test) {
    const found = [];
    const walk = (node) => {
      if (!node.children) return;   // a TextNode leaf, not an Element
      for (const child of node.children) {
        if (test(child)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

class TextNode {
  constructor(text) { this.textContent = String(text); }
}

/** Minimal stand-in for a real DocumentFragment: a bare child list.
 * Element.appendChild special-cases it (see above) so
 * `fragment.appendChild(x); parent.appendChild(fragment)` moves x into
 * parent, exactly like a browser. */
class DocumentFragment {
  constructor() {
    this.children = [];
  }
  appendChild(node) {
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
}

export function installDom(ids, pageUrl) {
  const byId = {};
  for (const id of ids) byId[id] = new Element("div");
  // The real pages ship their <table><thead><tr> in static HTML, which
  // this shim never parses — selection.js (2026-08-10) mounts its
  // checkbox column into `#<id> thead tr`, unguarded because a browser
  // guarantees the markup. Seed that skeleton for every table-named id
  // (this project's convention: dashboard-table, delta-table,
  // actions-table, queue-table) so the mount finds what the shipped
  // markup would give it.
  for (const id of ids) {
    if (id.endsWith("-table")) {
      const table = new Element("table");
      const thead = new Element("thead");
      const row = new Element("tr");
      thead.appendChild(row);
      table.appendChild(thead);
      byId[id].appendChild(table);
    }
  }

  globalThis.document = {
    // A real browser guarantees document.body exists by the time a
    // deferred module script runs; the shim never parses HTML, so it
    // has to be seeded (2026-08-10: selection.js appends its sticky
    // action bar to document.body at mount time and crashed both
    // walks — same shim-wrinkle family as the `hidden` default the
    // handover documents).
    body: new Element("body"),
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new Element(tag),
    createElementNS: (_ns, tag) => new Element(tag),
    createTextNode: (text) => new TextNode(text),
    createDocumentFragment: () => new DocumentFragment(),
    // Descendant-combinator resolver (2026-08-10, replacing the earlier
    // "#id ... .class"-only shape): selection.js queries document-wide
    // compound selectors ("input.row-select-checkbox") and the tables'
    // header mounts use tag paths ("#delta-table thead tr"). Supports
    // a leading "#id", then any chain of "tag", ".class" or
    // "tag.class" parts — the shapes this project's pages actually
    // write, nothing more. The old shape returned [] for anything
    // else, which made new call sites silently INERT in walks — a
    // crash is honest, silence is not, hence the widening.
    querySelectorAll: (selector) => {
      const parts = selector.trim().split(/\s+/);
      if (!parts.length) return [];
      const matchesPart = (elt, part) => {
        if (!elt.classList) return false;   // TextNode leaf
        const dot = part.indexOf(".");
        const tag = dot === -1 ? part : part.slice(0, dot);
        const cls = dot === -1 ? null : part.slice(dot + 1);
        if (tag && elt.tagName !== tag.toUpperCase()) return false;
        if (cls && !elt.classList.contains(cls)) return false;
        return Boolean(tag || cls);
      };
      const descendants = (root) => {
        const out = [];
        const walk = (node) => {
          if (!node.children) return;
          for (const child of node.children) {
            out.push(child);
            walk(child);
          }
        };
        walk(root);
        return out;
      };
      let current;
      let rest;
      if (parts[0].startsWith("#")) {
        const root = byId[parts[0].slice(1)];
        if (!root) return [];
        current = [root];
        rest = parts.slice(1);
      } else {
        current = [globalThis.document.body].concat(Object.values(byId));
        rest = parts;
      }
      for (const part of rest) {
        const next = [];
        for (const root of current) {
          for (const elt of descendants(root)) {
            if (matchesPart(elt, part) && next.indexOf(elt) === -1) {
              next.push(elt);
            }
          }
        }
        current = next;
      }
      return rest.length ? current : [];
    },
    querySelector: (selector) =>
      globalThis.document.querySelectorAll(selector)[0] || null,
  };

  const storage = {};
  const windowListeners = {};
  const parsedUrl = new URL(pageUrl);
  globalThis.window = {
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
      removeItem: (k) => { delete storage[k]; },
    },
    // search/pathname are a SNAPSHOT taken at install time (from the
    // pageUrl a driver script passes in) -- pages that navigate via
    // `window.location.href = ...` (streams.js's picker) are exercised
    // by installing a SECOND dom with the target URL, not by mutating
    // this one; nothing in this project's frontend reads .search after
    // assigning .href within the same page load.
    location: {
      href: pageUrl, search: parsedUrl.search, pathname: parsedUrl.pathname,
    },
    history: {
      replaceState: (_s, _t, url) => {
        globalThis.window.location.href = String(url);
      },
    },
    prompt: () => null,
    scrollTo: () => { /* no viewport here */ },
    addEventListener: (type, handler) => {
      (windowListeners[type] = windowListeners[type] || []).push(handler);
    },
    /* Test helper: deliver an event to window-level listeners. */
    fire: async (type, event) => {
      for (const handler of windowListeners[type] || []) {
        await handler(event);
      }
    },
  };

  return byId;
}
