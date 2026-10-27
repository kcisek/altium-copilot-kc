/* Shared shell for the MCP Apps views (inlined into each page by ui_views.render).

   Host bridge: the view side of MCP Apps (spec 2026-01-26), JSON-RPC 2.0 over
   postMessage with window.parent: ui/initialize, ui/notifications/initialized, the
   tool-input, tool-input-partial, tool-result, tool-cancelled and host-context-changed
   notifications, ui/resource-teardown, tools/call with request ids and timeouts (only
   when the host offers serverTools), and ui/notifications/size-changed when the
   content's size changes, with the panes fitted to the host's containerDimensions.
   Theme: the host's theme and style variables (else prefers-color-scheme).
   Widgets: el/svg builders (text only, never HTML), a filter box, a sortable table with
   a column chooser, sticky header and keyboard focus, tabs, states and notices.
   Nothing here loads or sends anything except postMessage to the host. */

const Shell = (() => {
  "use strict";

  const PROTOCOL_VERSION = "2026-01-26";
  const host = window.parent !== window ? window.parent : null;
  const pending = new Map();
  const listeners = new Map();
  let nextId = 1;
  let context = {};
  let capabilities = {};
  let appName = "view";

  // ---------- bridge ----------

  function post(message) {
    if (host) host.postMessage(message, "*");
  }

  function request(method, params, timeoutMs = 30000) {
    if (!host) return Promise.reject(new Error("No host: open this view from a host that shows MCP Apps."));
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method}: no answer from the host in ${Math.round(timeoutMs / 1000)} s`));
      }, timeoutMs);
      pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      post({ jsonrpc: "2.0", id, method, params: params || {} });
    });
  }

  function notify(method, params) {
    post({ jsonrpc: "2.0", method, params: params || {} });
  }

  function on(event, handler) {
    if (!listeners.has(event)) listeners.set(event, []);
    listeners.get(event).push(handler);
  }

  async function emit(event, data) {
    for (const handler of listeners.get(event) || []) {
      try {
        await handler(data);
      } catch (error) {
        log("error", `${event} handler: ${error && error.message}`);
      }
    }
  }

  function log(level, data) {
    if (capabilities.logging) notify("notifications/message", { level, logger: appName, data });
  }

  async function answer(message) {
    let error = null;
    try {
      if (message.method === "ui/resource-teardown") await emit("teardown", message.params || {});
      else if (message.method !== "ping") error = { code: -32601, message: `Method not found: ${message.method}` };
    } catch (failure) {
      error = { code: -32000, message: String((failure && failure.message) || failure) };
    }
    post(error ? { jsonrpc: "2.0", id: message.id, error }
               : { jsonrpc: "2.0", id: message.id, result: {} });
  }

  window.addEventListener("message", (event) => {
    if (!host || event.source !== host) return;
    const message = event.data;
    if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") return;
    if (message.method === undefined) {
      const waiting = pending.get(message.id);
      if (!waiting) return;
      pending.delete(message.id);
      if (message.error) waiting.reject(new Error(message.error.message || "The host answered with an error."));
      else waiting.resolve(message.result);
      return;
    }
    if (message.id !== undefined && message.id !== null) {
      answer(message);
      return;
    }
    const params = message.params || {};
    switch (message.method) {
      case "ui/notifications/tool-input": emit("toolInput", params.arguments || {}); break;
      case "ui/notifications/tool-input-partial": emit("toolInputPartial", params.arguments || {}); break;
      case "ui/notifications/tool-result": emit("toolResult", params); break;
      case "ui/notifications/tool-cancelled": emit("toolCancelled", params.reason || ""); break;
      case "ui/notifications/host-context-changed": applyContext(params); emit("context", context); break;
      default: break;
    }
  });

  /** Handshake: register the on() handlers first, then connect. */
  async function connect(appInfo, appCapabilities) {
    appName = (appInfo && appInfo.name) || appName;
    const result = await request("ui/initialize", {
      protocolVersion: PROTOCOL_VERSION,
      appInfo: appInfo || { name: appName, version: "1" },
      appCapabilities: appCapabilities || { availableDisplayModes: ["inline", "fullscreen"] },
    }, 10000);
    capabilities = (result && result.hostCapabilities) || {};
    applyContext((result && result.hostContext) || {});
    notify("ui/notifications/initialized", {});
    startAutoResize();
    return result || {};
  }

  /** True when the host proxies tools/call to the server (hostCapabilities.serverTools). */
  function canCallTools() {
    return Boolean(capabilities.serverTools);
  }

  async function callTool(name, args, timeoutMs = 120000) {
    if (!canCallTools()) throw new Error("This host does not let the view call the server's tools.");
    const result = await request("tools/call", { name, arguments: args || {} }, timeoutMs);
    if (result && result.isError) throw new Error(resultText(result) || `${name} failed.`);
    return result || {};
  }

  /** A CallToolResult's text: its text blocks, else structuredContent.result. */
  function resultText(result) {
    const blocks = (result && result.content) || [];
    const text = blocks.filter((block) => block && block.type === "text").map((block) => block.text).join("");
    if (text) return text;
    const structured = result && result.structuredContent;
    return structured && typeof structured.result === "string" ? structured.result : "";
  }

  function resultJson(result) {
    const text = resultText(result);
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error("The tool's result is not JSON: " + text.slice(0, 160));
    }
  }

  async function requestDisplayMode(mode) {
    const result = await request("ui/request-display-mode", { mode });
    if (result && result.mode) applyContext({ displayMode: result.mode });
    return (result && result.mode) || context.displayMode;
  }

  function canDisplay(mode) {
    return (context.availableDisplayModes || []).includes(mode);
  }

  // ---------- theme and host context ----------

  function applyContext(update) {
    context = { ...context, ...update };
    const root = document.documentElement;
    if (update.theme === "light" || update.theme === "dark") {
      root.dataset.theme = update.theme;
      root.style.colorScheme = update.theme;
    }
    const variables = update.styles && update.styles.variables;
    if (variables) {
      for (const [name, value] of Object.entries(variables)) {
        if (!name.startsWith("--")) continue;
        // Values only: anything that could load a resource is left out (styles.css.fonts too).
        if (value === undefined || value === null || value === "") root.style.removeProperty(name);
        else if (!/url\(|@import|image-set\(/i.test(String(value))) root.style.setProperty(name, String(value));
      }
    }
    if (update.displayMode) root.dataset.display = update.displayMode;
    const insets = update.safeAreaInsets;
    if (insets) {
      for (const side of ["top", "right", "bottom", "left"]) root.style.setProperty(`--inset-${side}`, `${Number(insets[side]) || 0}px`);
    }
    if (update.containerDimensions || update.displayMode) queueMeasure();
  }

  // ---------- height: panes fitted to the host's container ----------

  const MIN_PANE = 100;
  const INLINE_PANE = 560;  // the tallest pane inline when the host gives only a maxHeight
  let fitted = "";

  function contentHeight() {
    const html = document.documentElement;
    const saved = html.style.height;
    html.style.height = "max-content";
    const height = Math.ceil(html.getBoundingClientRect().height);
    html.style.height = saved;
    return height;
  }

  /**
   * Panes (the elements sized by --pane-height) as tall as the host's container lets the
   * whole page be: hostContext.containerDimensions.height (fixed) or maxHeight. The page's
   * height grows with the pane height, so a binary search finds the tallest that fits;
   * it only moves up from the current value unless the page is already too tall, so a
   * pane's scroll position is kept. Without dimensions the stylesheet's default applies.
   */
  function fitPanes() {
    const root = document.documentElement;
    const dims = context.containerDimensions || {};
    const limit = Number(dims.height || dims.maxHeight) || 0;
    if (!limit) {
      root.style.removeProperty("--pane-height");
      fitted = "";
      return;
    }
    const fixed = Boolean(dims.height) || context.displayMode === "fullscreen";
    const top = Math.max(MIN_PANE, fixed ? limit : Math.min(limit, INLINE_PANE));
    const set = (px) => root.style.setProperty("--pane-height", `${px}px`);
    const state = () => `${limit}|${top}|${window.innerWidth}|${root.style.getPropertyValue("--pane-height")}|${contentHeight()}`;
    let current = parseInt(root.style.getPropertyValue("--pane-height"), 10);
    if (!Number.isFinite(current)) {
      current = top;
      set(current);
    }
    if (state() === fitted) return;
    let low = current;
    let high = top;
    if (current > top || contentHeight() > limit) {
      low = MIN_PANE;
      high = Math.min(current, top);
    }
    while (high - low > 4) {
      const middle = Math.floor((low + high) / 2);
      set(middle);
      if (contentHeight() <= limit) low = middle;
      else high = middle;
    }
    set(high);
    if (contentHeight() > limit) set(low);
    fitted = state();
  }

  // ---------- auto-resize ----------

  let queueMeasure = () => {};

  function startAutoResize() {
    let last = { width: 0, height: 0 };
    let queued = false;
    const measure = () => {
      if (!queued) return;
      queued = false;
      fitPanes();
      const height = contentHeight();
      const width = Math.ceil(window.innerWidth);
      if (height !== last.height || width !== last.width) {
        last = { width, height };
        notify("ui/notifications/size-changed", { width, height });
      }
    };
    queueMeasure = () => {
      if (queued) return;
      queued = true;
      // A frame the host has not shown yet (no height until the first size-changed) may get
      // no animation frames and no ResizeObserver callbacks: a timer measures it as well.
      requestAnimationFrame(measure);
      setTimeout(measure, 80);
    };
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(() => queueMeasure());
      observer.observe(document.documentElement);
      observer.observe(document.body);
    }
    // Content changes are seen even where layout callbacks are held back.
    new MutationObserver(() => queueMeasure()).observe(document.body, {
      childList: true, subtree: true, attributes: true, characterData: true,
    });
    window.addEventListener("load", () => queueMeasure());
    window.addEventListener("resize", () => queueMeasure());
    queueMeasure();
  }

  // ---------- DOM builders (text content only) ----------

  function append(parent, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return parent;
  }

  /** el("div", {class, text, title, on: {click}, data: {...}, ...attributes}, ...children) */
  function el(tag, props, ...children) {
    return build(document.createElement(tag), props, children);
  }

  function svg(tag, props, ...children) {
    return build(document.createElementNS("http://www.w3.org/2000/svg", tag), props, children);
  }

  function build(node, props, children) {
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.setAttribute("class", value);
      else if (key === "text") node.textContent = String(value);
      else if (key === "on") for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
      else if (key === "data") for (const [name, item] of Object.entries(value)) node.dataset[name] = item;
      else if (key === "style") throw new Error("Use classes or CSSOM, not style attributes.");
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    return append(node, children);
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  // ---------- filter ----------

  /** True when every word of the query is in the text (case-insensitive). */
  function matches(text, query) {
    const haystack = String(text || "").toLowerCase();
    return String(query || "").toLowerCase().split(/\s+/).filter(Boolean).every((word) => haystack.includes(word));
  }

  function filterBox({ placeholder = "Filter", label = "Filter", onInput, onEnter, value = "" } = {}) {
    let timer = null;
    const input = el("input", { type: "search", class: "filter", placeholder, "aria-label": label, value });
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => onInput && onInput(input.value), 120);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && input.value) {
        input.value = "";
        if (onInput) onInput("");
        event.stopPropagation();
      } else if (event.key === "Enter" && onEnter) {
        onEnter(input.value);
      }
    });
    return input;
  }

  // ---------- table ----------

  /**
   * columns: [{key, label, value(row) for sorting and filtering, render(row) -> node|text,
   *            numeric, hidden}]
   * Returns {el, setRows(rows), setQuery(query), select(predicate)}.
   */
  function table({ columns, rows = [], onActivate, empty = "Nothing to show.", ownFilter = true,
                   label = "Table", sort = null }) {
    const shown = new Set(columns.filter((column) => !column.hidden).map((column) => column.key));
    let sortKey = sort ? sort.key : null;
    let sortDir = sort ? sort.dir || 1 : 1;
    let query = "";
    let data = rows;
    let selected = null;

    const valueOf = (column, row) => (column.value ? column.value(row) : row[column.key]);
    const textOf = (column, row) => {
      const value = valueOf(column, row);
      return Array.isArray(value) ? value.join(" ") : value === null || value === undefined ? "" : String(value);
    };

    let drawn = [];  // [tr, row] of the rows on screen
    const tableEl = el("table", { class: "tbl", "aria-label": label });
    const wrap = el("div", { class: "tbl-wrap" }, tableEl);
    const foot = el("div", { class: "tbl-foot", "aria-live": "polite" });
    const chooser = el("details", { class: "colchooser" },
      el("summary", { class: "btn", text: "Columns" }),
      el("div", { class: "menu" }, columns.map((column) => {
        const box = el("input", { type: "checkbox" });
        box.checked = shown.has(column.key);
        box.addEventListener("change", () => {
          if (box.checked) shown.add(column.key);
          else if (shown.size > 1) shown.delete(column.key);
          else box.checked = true;
          draw();
        });
        return el("label", {}, box, column.label);
      })));
    const toolbar = el("div", { class: "toolbar" },
      ownFilter ? filterBox({ label: `Filter ${label}`, onInput: (text) => { query = text; draw(); } }) : null,
      el("span", { class: "grow" }), chooser);
    const root = el("div", { class: "tbl-block" }, toolbar, wrap, foot);

    function sorted(list) {
      const column = columns.find((item) => item.key === sortKey);
      if (!column) return list;
      return [...list].sort((a, b) => {
        const x = valueOf(column, a);
        const y = valueOf(column, b);
        if (x === y) return 0;
        if (x === null || x === undefined || x === "") return 1;
        if (y === null || y === undefined || y === "") return -1;
        if (typeof x === "number" && typeof y === "number") return (x - y) * sortDir;
        return String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" }) * sortDir;
      });
    }

    function draw() {
      const visible = columns.filter((column) => shown.has(column.key));
      const kept = sorted(data.filter((row) => !query || matches(columns.map((column) => textOf(column, row)).join(" "), query)));
      clear(tableEl);
      const head = el("tr", {}, visible.map((column) => {
        const th = el("th", { scope: "col", class: column.numeric ? "num" : null,
                              "aria-sort": column.key === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none" },
          el("button", { type: "button", text: column.label, on: { click: () => {
            sortDir = column.key === sortKey ? -sortDir : 1;
            sortKey = column.key;
            draw();
          } } }));
        return th;
      }));
      const body = el("tbody");
      drawn = [];
      kept.forEach((row, index) => {
        const tr = el("tr", { tabindex: index === 0 ? "0" : "-1", class: onActivate ? "clickable" : null,
                              "aria-selected": selected && selected(row) ? "true" : null },
          visible.map((column) => {
            const content = column.render ? column.render(row) : textOf(column, row);
            return el("td", { class: column.numeric ? "num" : null }, content);
          }));
        if (onActivate) tr.addEventListener("click", (event) => {
          if (event.target.closest("button, a, input")) return;
          onActivate(row);
        });
        tr.addEventListener("keydown", (event) => {
          const rowsEls = [...body.children];
          const at = rowsEls.indexOf(tr);
          let target = null;
          if (event.key === "ArrowDown") target = rowsEls[at + 1];
          else if (event.key === "ArrowUp") target = rowsEls[at - 1];
          else if (event.key === "Home") target = rowsEls[0];
          else if (event.key === "End") target = rowsEls[rowsEls.length - 1];
          else if ((event.key === "Enter" || event.key === " ") && onActivate && event.target === tr) {
            event.preventDefault();
            onActivate(row);
            return;
          } else return;
          event.preventDefault();
          if (target) {
            tr.tabIndex = -1;
            target.tabIndex = 0;
            target.focus();
          }
        });
        body.appendChild(tr);
        drawn.push([tr, row]);
      });
      append(tableEl, [el("thead", {}, head), body]);
      if (!kept.length) {
        body.appendChild(el("tr", {}, el("td", { colspan: String(visible.length), class: "muted", text: data.length ? "No row matches the filter." : empty })));
      }
      foot.textContent = kept.length === data.length ? `${data.length} rows` : `${kept.length} of ${data.length} rows`;
    }

    draw();
    return {
      el: root,
      setRows(next) { data = next || []; draw(); },
      setQuery(text) { query = text || ""; draw(); },
      // Marks the selected rows in place: the rows are not rebuilt, so focus stays put.
      select(predicate) {
        selected = predicate;
        for (const [tr, row] of drawn) {
          if (selected && selected(row)) tr.setAttribute("aria-selected", "true");
          else tr.removeAttribute("aria-selected");
        }
      },
    };
  }

  // ---------- tabs, states, notices ----------

  /** tabs([{id, label}], onChange) -> {el, set(id)}; arrow keys move between tabs. */
  function tabs(items, onChange, current) {
    const buttons = new Map();
    const bar = el("div", { class: "tabs", role: "tablist" });
    const set = (id) => {
      for (const [key, button] of buttons) {
        button.setAttribute("aria-selected", key === id ? "true" : "false");
        button.tabIndex = key === id ? 0 : -1;
      }
      onChange(id);
    };
    items.forEach((item, index) => {
      const button = el("button", { type: "button", class: "tab", role: "tab", text: item.label,
                                    on: { click: () => set(item.id) } });
      button.addEventListener("keydown", (event) => {
        const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        const next = items[(index + step + items.length) % items.length];
        set(next.id);
        buttons.get(next.id).focus();
      });
      buttons.set(item.id, button);
      bar.appendChild(button);
    });
    for (const [key, button] of buttons) {
      button.setAttribute("aria-selected", key === current ? "true" : "false");
      button.tabIndex = key === current ? 0 : -1;
    }
    return { el: bar, set };
  }

  /** A loading, empty, error or info block. */
  function state(kind, title, detail) {
    return el("div", { class: `state ${kind}`, role: kind === "error" ? "alert" : "status" },
      el("h2", { text: title }), detail ? el("p", { text: detail }) : null);
  }

  /** A dismissible notice line (kind: info, warning, error). */
  function notice(kind, text) {
    const box = el("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" },
      el("span", { class: "grow", text }));
    box.appendChild(el("button", { type: "button", class: "btn-link", text: "Dismiss",
                                   on: { click: () => box.remove() } }));
    return box;
  }

  return {
    PROTOCOL_VERSION, on, connect, request, notify, canCallTools, callTool, resultText, resultJson, log,
    requestDisplayMode, canDisplay, context: () => context, capabilities: () => capabilities,
    el, svg, clear, append, matches, filterBox, table, tabs, state, notice,
  };
})();
