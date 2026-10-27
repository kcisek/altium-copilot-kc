/* Shared shell for the MCP Apps views (inlined into each page by ui_views.render).

   Host bridge: the view side of MCP Apps (spec 2026-01-26), JSON-RPC 2.0 over
   postMessage with window.parent: ui/initialize, ui/notifications/initialized, the
   tool-input, tool-input-partial, tool-result, tool-cancelled and host-context-changed
   notifications, ui/resource-teardown, tools/call with request ids and timeouts (only
   when the host offers serverTools), and ui/notifications/size-changed when the
   content's size changes, with the panes fitted to the host's containerDimensions.
   Theme: the host's theme and style variables (else prefers-color-scheme).
   Widgets: el/svg builders (text only, never HTML), a filter box, a sortable table with
   a column chooser, sticky header, collapsible groups with counts and keyboard focus that
   survives a redraw, level badges, filter chips, a one-of switch, fact lists, tabs,
   states and notices, a full-screen button.
   Paging and details: readPages follows a tool's next offsets and says why it stopped
   (teardown, no serverTools, the page budget, a failed page); toolCache asks for a
   selection's details once, never keeps a failure as the answer and forgets an answer
   that may be out of date when asked to.
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
  let tornDown = false;  // after ui/resource-teardown: no more requests to the host

  // ---------- bridge ----------

  function post(message) {
    if (host) host.postMessage(message, "*");
  }

  function request(method, params, timeoutMs = 30000) {
    if (!host) return Promise.reject(new Error("No host: open this view from a host that shows MCP Apps."));
    if (tornDown) return Promise.reject(new Error("The view was closed."));
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
      if (message.method === "ui/resource-teardown") {
        tornDown = true;
        await emit("teardown", message.params || {});
      } else if (message.method !== "ping") error = { code: -32601, message: `Method not found: ${message.method}` };
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

  // The CSS functions a host's style variable may hold: colours and arithmetic. Any other
  // function (an image, a resource), an escape that could spell one, an at-rule or a
  // block is refused, so no variable can make the page load anything.
  const STYLE_FUNCTIONS = new Set(["", "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch",
                                   "color", "color-mix", "light-dark", "calc", "min", "max", "clamp", "var"]);

  function safeStyleValue(value) {
    const text = String(value);
    if (/[\\@;{}<>]/.test(text)) return false;
    for (const match of text.matchAll(/([a-z0-9_-]*)\s*\(/gi)) {
      if (!STYLE_FUNCTIONS.has(match[1].toLowerCase())) return false;
    }
    return true;
  }

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
        else if (safeStyleValue(value)) root.style.setProperty(name, String(value));
        else root.style.removeProperty(name);
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

  const MIN_PANE = 64;  // reached only when the page does not fit otherwise (a short, narrow host)
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
    let unsized = 0;  // timed measures of a frame not laid out yet
    const measure = () => {
      if (!queued) return;
      queued = false;
      fitPanes();
      const height = contentHeight();
      const width = Math.ceil(window.innerWidth);
      if (!width || !height) {
        // A frame with no size yet (the host has not laid it out) may get no animation
        // frames, ResizeObserver callbacks or mutations later: measure it again on a
        // timer (backing off to a second, for about half a minute) until it has one.
        if (unsized < 40) {
          unsized += 1;
          setTimeout(queueMeasure, Math.min(1000, 80 * unsized));
        }
        return;
      }
      unsized = 0;
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

  const naturally = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });

  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

  /**
   * columns: [{key, label, value(row) for sorting (and filtering), text(row) for filtering
   *            when it differs, render(row) -> node|text, numeric, hidden, cls (the cells'
   *            class: "c-msg" for a message, "c-name" for names that may wrap)}]
   * group: null, or {key(row) -> a key or a list of keys (the row is drawn under each),
   *        label(key, rows) -> node|text, count(key, rows) -> node|text,
   *        order(a, b, buckets) -> number, open(key, rows, groups) -> bool (the first state)}:
   *        rows drawn under collapsible group headers (Enter or Space, Left and Right).
   * rowKey(row): a stable id, so keyboard focus and selection survive a redraw.
   * filter(row): a predicate beside the text query (chips, switches).
   * A table with rows to activate is a grid (a treegrid when grouped: group headers at
   * aria-level 1, their rows at 2) with one roving row in the tab order.
   * Returns {el, setRows(rows), setQuery(query), setFilter(predicate), setGroup(group),
   *          expandAll(), collapseAll(), select(predicate), focus(predicate), kept()}.
   */
  function table({ columns, rows = [], onActivate, empty = "Nothing to show.", ownFilter = true,
                   label = "Table", sort = null, group = null, rowKey = null, filter = null }) {
    const shown = new Set(columns.filter((column) => !column.hidden).map((column) => column.key));
    let sortKey = sort ? sort.key : null;
    let sortDir = sort ? sort.dir || 1 : 1;
    let query = "";
    let data = rows;
    let selected = null;
    let predicate = filter;
    let grouping = group;
    let open = new Map();  // group key -> expanded
    let groupKeys = [];
    let keptRows = [];

    const valueOf = (column, row) => (column.value ? column.value(row) : row[column.key]);
    const textOf = (column, row) => {
      const value = column.text ? column.text(row) : valueOf(column, row);
      return Array.isArray(value) ? value.join(" ") : value === null || value === undefined ? "" : String(value);
    };
    const identity = (row) => (rowKey ? rowKey(row) : row);
    const keysOf = (row) => {
      const key = grouping.key(row);
      const list = (Array.isArray(key) ? key : [key]).filter((item) => item !== null && item !== undefined);
      return list.length ? list.map(String) : [""];
    };

    let drawn = [];  // [tr, row] of the rows on screen
    const tableEl = el("table", { class: "tbl", "aria-label": label });
    const wrap = el("div", { class: "tbl-wrap" }, tableEl);
    const foot = el("div", { class: "tbl-foot", "aria-live": "polite" });
    const chooserSummary = el("summary", { class: "btn", text: "Columns" });
    const chooser = el("details", { class: "colchooser" },
      chooserSummary,
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
    // Esc closes the column chooser (and is not taken as "clear the selection").
    chooser.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !chooser.open) return;
      event.preventDefault();
      event.stopPropagation();
      chooser.open = false;
      chooserSummary.focus();
    });
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

    /** What has keyboard focus in the table, to give it back after a redraw. */
    function focusState() {
      const active = document.activeElement;
      if (!active || !tableEl.contains(active)) return null;
      if (active.dataset.col !== undefined) return { col: active.dataset.col };
      if (active.dataset.groupKey !== undefined) return { group: active.dataset.groupKey };
      const entry = drawn.find(([tr]) => tr === active);
      return entry ? { row: identity(entry[1]), group: active.dataset.group } : null;
    }

    function rove(body, target) {
      for (const item of body.querySelectorAll("[data-nav]")) item.tabIndex = item === target ? 0 : -1;
    }

    function onBodyKey(body, event) {
      const item = event.target.closest && event.target.closest("[data-nav]");
      if (!item) return;
      const items = [...body.querySelectorAll("[data-nav]")];
      const at = items.indexOf(item);
      const groupHead = item.dataset.groupKey !== undefined;
      let target = null;
      if (event.key === "ArrowDown") target = items[at + 1];
      else if (event.key === "ArrowUp") target = items[at - 1];
      else if (event.key === "Home") target = items[0];
      else if (event.key === "End") target = items[items.length - 1];
      else if (groupHead && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault();
        setOpen(item.dataset.groupKey, event.key === "ArrowRight");
        return;
      } else if (!groupHead && event.key === "ArrowLeft" && item.dataset.group !== undefined) {
        target = items.find((other) => other.dataset.groupKey === item.dataset.group);
      } else if (!groupHead && (event.key === "Enter" || event.key === " ") && onActivate && event.target === item) {
        event.preventDefault();
        const entry = drawn.find(([tr]) => tr === item);
        if (entry) onActivate(entry[1]);
        return;
      } else return;
      event.preventDefault();
      if (target) {
        rove(body, target);
        target.focus();
      }
    }

    function setOpen(key, value) {
      if ((open.get(key) !== false) === value) return;
      open.set(key, value);
      draw();
    }

    const cellClass = (column) => [column.numeric ? "num" : "", column.cls || ""].join(" ").trim() || null;

    function headCell(column) {
      return el("th", { scope: "col", class: cellClass(column),
                        "aria-sort": column.key === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none" },
        el("button", { type: "button", text: column.label, data: { col: column.key }, on: { click: () => {
          sortDir = column.key === sortKey ? -sortDir : 1;
          sortKey = column.key;
          draw();
        } } }));
    }

    function draw() {
      const restore = focusState();
      const visible = columns.filter((column) => shown.has(column.key));
      const kept = sorted(data.filter((row) => (!predicate || predicate(row)) &&
        (!query || matches(columns.map((column) => textOf(column, row)).join(" "), query))));
      keptRows = kept;
      clear(tableEl);
      const role = grouping ? "treegrid" : onActivate ? "grid" : null;
      if (role) tableEl.setAttribute("role", role);
      else tableEl.removeAttribute("role");
      const head = el("tr", {}, visible.map(headCell));
      const body = el("tbody");
      body.addEventListener("keydown", (event) => onBodyKey(body, event));
      drawn = [];
      const addRow = (row, groupKey) => {
        const tr = el("tr", { tabindex: "-1", class: onActivate ? "clickable" : null, "data-nav": "",
                              "aria-selected": onActivate ? (selected && selected(row) ? "true" : "false") : null,
                              "aria-level": grouping ? "2" : null,
                              data: groupKey === null ? null : { group: groupKey } },
          visible.map((column) => {
            const content = column.render ? column.render(row) : textOf(column, row);
            return el("td", { class: cellClass(column) }, content);
          }));
        tr.addEventListener("click", (event) => {
          rove(body, tr);
          if (!onActivate || event.target.closest("button, a, input")) return;
          onActivate(row);
        });
        body.appendChild(tr);
        drawn.push([tr, row]);
      };
      groupKeys = [];
      if (grouping) {
        const buckets = new Map();
        for (const row of kept) {
          for (const key of keysOf(row)) {
            if (!buckets.has(key)) buckets.set(key, []);
            buckets.get(key).push(row);
          }
        }
        groupKeys = [...buckets.keys()].sort((a, b) => (grouping.order ? grouping.order(a, b, buckets) : naturally(a, b)));
        for (const key of groupKeys) {
          const members = buckets.get(key);
          if (!open.has(key)) open.set(key, grouping.open ? Boolean(grouping.open(key, members, groupKeys.length)) : true);
          const expanded = open.get(key);
          const header = el("tr", { class: "grp", tabindex: "-1", "data-nav": "", "aria-expanded": expanded ? "true" : "false",
                                    "aria-level": "1",
                                    data: { groupKey: key }, title: expanded ? "Collapse this group" : "Expand this group" },
            el("th", { colspan: String(visible.length), scope: "colgroup" },
              el("span", { class: "grp-row" },
                el("span", { class: "grp-toggle", "aria-hidden": "true", text: expanded ? "▾" : "▸" }),
                el("span", { class: "grp-label" }, grouping.label ? grouping.label(key, members) : key || "(none)"),
                el("span", { class: "grp-count" }, grouping.count ? grouping.count(key, members)
                  : el("span", { class: "badge", text: String(members.length) })))));
          header.addEventListener("click", (event) => {
            rove(body, header);
            if (event.target.closest("button, a, input")) return;
            setOpen(key, !open.get(key));
          });
          header.addEventListener("keydown", (event) => {
            if ((event.key === "Enter" || event.key === " ") && event.target === header) {
              event.preventDefault();
              setOpen(key, !open.get(key));
            }
          });
          body.appendChild(header);
          if (expanded) for (const row of members) addRow(row, key);
        }
      } else {
        for (const row of kept) addRow(row, null);
      }
      append(tableEl, [el("thead", {}, head), body]);
      if (!kept.length) {
        body.appendChild(el("tr", {}, el("td", { colspan: String(visible.length), class: "muted", text: data.length ? "No row matches the filter." : empty })));
      }
      const groupsText = grouping && groupKeys.length ? ` in ${plural(groupKeys.length, "group")}` : "";
      foot.textContent = (kept.length === data.length ? plural(data.length, "row") : `${kept.length} of ${plural(data.length, "row")}`) + groupsText;
      // Keyboard focus goes back where it was: the same column header, group or row.
      let target = null;
      if (restore && restore.col !== undefined) {
        target = [...head.querySelectorAll("button")].find((button) => button.dataset.col === restore.col) || null;
      } else if (restore && restore.row !== undefined) {
        const same = drawn.filter(([, row]) => identity(row) === restore.row);
        const entry = same.find(([tr]) => tr.dataset.group === restore.group) || same[0];
        target = entry ? entry[0] : null;
      }
      if (!target && restore && restore.group !== undefined) {
        target = [...body.querySelectorAll("tr.grp")].find((tr) => tr.dataset.groupKey === restore.group) || null;
      }
      const first = body.querySelector("[data-nav]");
      if (target && body.contains(target)) rove(body, target);
      else if (first) first.tabIndex = 0;
      if (target) target.focus();
    }

    draw();
    return {
      el: root,
      setRows(next) { data = next || []; draw(); },
      setQuery(text) { query = text || ""; draw(); },
      setFilter(next) { predicate = next || null; draw(); },
      setGroup(next) { grouping = next || null; open = new Map(); draw(); },
      expandAll() { for (const key of groupKeys) open.set(key, true); draw(); },
      collapseAll() { for (const key of groupKeys) open.set(key, false); draw(); },
      kept() { return keptRows; },
      // Marks the selected rows in place: the rows are not rebuilt, so focus stays put.
      select(match) {
        selected = match;
        for (const [tr, row] of drawn) {
          if (selected && selected(row)) tr.setAttribute("aria-selected", "true");
          else if (onActivate) tr.setAttribute("aria-selected", "false");
          else tr.removeAttribute("aria-selected");
        }
      },
      // Keyboard focus to the first row drawn that matches, else to the table's roving
      // row (a details pane closing gives focus back to its row). False when neither is.
      focus(match) {
        const entry = match ? drawn.find(([, row]) => match(row)) : null;
        const body = tableEl.querySelector("tbody");
        const target = entry ? entry[0] : body && body.querySelector('[data-nav][tabindex="0"]');
        if (!target) return false;
        if (body) rove(body, target);
        target.focus();
        return true;
      },
    };
  }

  // ---------- badges, chips, switches, facts ----------

  const LEVEL_CLASS = {
    "fatal error": "fatal", fatal: "fatal", error: "error", warning: "warning", info: "info",
    "no report": "muted", ok: "ok",
  };

  /** A badge for a level or severity: Fatal Error, Error, Warning, info, No Report. */
  function levelBadge(level, text) {
    const kind = LEVEL_CLASS[String(level || "").toLowerCase()] || "";
    return el("span", { class: `badge ${kind}`.trim(), text: text || String(level || "") });
  }

  /** Toggle buttons that filter rows: items [{id, label, count}], every id on at first. */
  function chips({ label, items, onChange, selected = null }) {
    const on = new Set(selected || items.map((item) => item.id));
    const box = el("div", { class: "chips", role: "group", "aria-label": label },
      el("span", { class: "chips-label", text: label }));
    for (const item of items) {
      const button = el("button", { type: "button", class: "chip", "aria-pressed": on.has(item.id) ? "true" : "false",
                                    title: item.title || null },
        item.label, item.count === undefined ? null : el("span", { class: "chip-count", text: String(item.count) }));
      button.addEventListener("click", () => {
        if (on.has(item.id)) on.delete(item.id);
        else on.add(item.id);
        button.setAttribute("aria-pressed", on.has(item.id) ? "true" : "false");
        if (onChange) onChange(new Set(on));
      });
      box.appendChild(button);
    }
    return { el: box, selected: () => new Set(on) };
  }

  /** One-of buttons (a switch such as "group by"): items [{id, label, title, disabled}]. */
  function segmented({ label, items, current, onChange }) {
    const buttons = new Map();
    const box = el("div", { class: "seg", role: "group", "aria-label": label },
      el("span", { class: "chips-label", text: label }));
    const set = (id) => {
      for (const [key, button] of buttons) button.setAttribute("aria-pressed", key === id ? "true" : "false");
    };
    for (const item of items) {
      const button = el("button", { type: "button", class: "seg-btn", text: item.label, title: item.title || null,
                                    disabled: Boolean(item.disabled),
                                    on: { click: () => {
                                      set(item.id);
                                      if (onChange) onChange(item.id);
                                    } } });
      buttons.set(item.id, button);
      box.appendChild(button);
    }
    set(current);
    return { el: box, set };
  }

  /** A definition list of [label, value] pairs; empty values are left out. */
  function facts(pairs, className = "facts") {
    const list = el("dl", { class: className });
    for (const [label, value] of pairs) {
      if (value === null || value === undefined || value === "" || value === false ||
          (Array.isArray(value) && !value.length)) continue;
      append(list, [el("dt", { text: label }), el("dd", {}, value)]);
    }
    return list;
  }

  const markOf = (node) => `${node.tagName}|${node.textContent}|${node.getAttribute("title") || ""}|${node.getAttribute("aria-label") || ""}`;

  /** The control that has keyboard focus inside container, as a mark to find it again
   *  after the container is rebuilt (null when focus is elsewhere). */
  function focusMark(container) {
    const active = document.activeElement;
    if (!active || active === container || active === document.body || !container.contains(active)) return null;
    return markOf(active);
  }

  /** Focus the control in container with that mark, else fallback() (a node or null).
   *  True when something got focus. */
  function refocus(container, mark, fallback) {
    let target = mark ? [...container.querySelectorAll("button, input, select, summary")].find((node) => markOf(node) === mark) : null;
    if (!target && fallback) target = fallback();
    if (!target || typeof target.focus !== "function") return false;
    target.focus();
    return document.activeElement === target;
  }

  // The controls keepFocus counts to find a neighbour for a control that is gone.
  const CONTROLS = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex="0"]';

  /** Rebuild a container's content; a control that had keyboard focus in it gets it back
   *  (the one with the same text and title), so an answer arriving does not drop focus.
   *  When that control is not drawn again (a Retry or Ask again replaced by "Asking…"),
   *  focus goes to fallback() when given, else to the control now at its place in the
   *  container, so it stays where the user was rather than falling to the page. */
  function keepFocus(container, rebuild, fallback = null) {
    const had = focusMark(container);
    const at = had === null ? -1 : [...container.querySelectorAll(CONTROLS)].indexOf(document.activeElement);
    rebuild();
    if (had === null) return;
    refocus(container, had, () => {
      const chosen = fallback ? fallback() : null;
      if (chosen) return chosen;
      const shown = [...container.querySelectorAll(CONTROLS)].filter((node) => node.getClientRects().length);
      return at >= 0 && shown.length ? shown[Math.min(at, shown.length - 1)] : null;
    });
  }

  // One polite live region for the whole page: short news (a selection, a details answer)
  // without re-reading a pane that is rebuilt.
  let liveRegion = null;
  let liveTimer = null;

  function announce(text) {
    if (!liveRegion) {
      liveRegion = el("div", { class: "sr-only", role: "status", "aria-live": "polite" });
      document.body.appendChild(liveRegion);
    }
    clearTimeout(liveTimer);
    liveRegion.textContent = "";
    liveTimer = setTimeout(() => { liveRegion.textContent = String(text || ""); }, 60);
  }

  /** True when the host gives the page room for open filters and lists: no height limit
   *  under 520 px (or full screen) and wider than 600 px. */
  function roomy() {
    const dims = context.containerDimensions || {};
    const limit = Number(dims.height || dims.maxHeight) || 0;
    return (!limit || limit >= 520 || context.displayMode === "fullscreen") && window.innerWidth > 600;
  }

  /** query_net takes a Python regex: an anchored, escaped name matches that net only. */
  function exactPattern(name) {
    return "^" + String(name).replace(/[\\^$.|?*+()[\]{}]/g, "\\$&") + "$";
  }

  // ---------- paging and tool calls ----------

  /** A page budget shared by several readPages calls (one result's follow-up calls):
   *  at most total pages in all, used of them already read (the result's first page,
   *  which readPages does not count), so a stop names the number of pages really read. */
  function pageBudget(total, used = 0) {
    return { total, left: total - used };
  }

  /**
   * Reads every page after the first through tools/call, following the offsets the tool
   * gives: pageOffset(page) -> the next offset or null; getPage(offset) -> a promise of
   * the page (rejected on the tool's error payload); onPage(page) merges it; closed() is
   * true after teardown. Resolves {pages, stopped}: stopped is "" when every page was
   * read, else why the paging stopped, so the view draws what it has and says why.
   */
  async function readPages({ first, tool, pageOffset, getPage, onPage, budget = pageBudget(60),
                             closed = () => false, progress = null }) {
    let next = pageOffset(first);
    let pages = 1;
    let stopped = "";
    while (next !== null && next !== undefined) {
      if (closed() || tornDown) stopped = "the view was closed";
      else if (!canCallTools()) stopped = `this host does not let the view call ${tool} for the next pages`;
      else if (budget.left <= 0) stopped = `the view reads at most ${budget.total} pages`;
      if (stopped) break;
      budget.left -= 1;
      if (progress) progress(pages + 1, next);
      let page;
      try {
        page = await getPage(next);
      } catch (error) {
        stopped = `page ${pages + 1} of ${tool} could not be read: ${error.message}`;
        break;
      }
      onPage(page);
      next = pageOffset(page);
      pages += 1;
    }
    return { pages, stopped };
  }

  /**
   * Detail calls for a selection, asked once each: get(tool, args) -> undefined or
   * {loading} | {data: the tool's result} | {error}; load(tool, args) asks the server when
   * there is no entry or the last ask failed (a failure is never kept as the answer), and
   * never after teardown; forget(tool, args) drops an answer that may be out of date, so
   * the next load asks again. onChange(tool, args, entry) runs when an entry changes.
   */
  function toolCache(onChange) {
    const entries = new Map();
    const keyOf = (tool, args) => `${tool} ${JSON.stringify(args || {})}`;
    return {
      get: (tool, args) => entries.get(keyOf(tool, args)),
      forget(tool, args) {
        const entry = entries.get(keyOf(tool, args));
        if (entry && !entry.loading) entries.delete(keyOf(tool, args));
      },
      async load(tool, args) {
        const key = keyOf(tool, args);
        const entry = entries.get(key);
        if ((entry && !entry.error) || !canCallTools() || tornDown) return entry;
        entries.set(key, { loading: true });
        if (onChange) onChange(tool, args, entries.get(key));
        let next;
        try {
          next = { data: await callTool(tool, args) };
        } catch (error) {
          next = { error: error.message };
        }
        entries.set(key, next);
        if (onChange) onChange(tool, args, next);
        return next;
      },
    };
  }

  // ---------- display mode ----------

  /** A Full screen / Exit full screen button that asks only for a mode the host lists and
   *  follows the host's own changes of mode; hidden when the host offers neither. */
  function displayModeButton(onError) {
    const button = el("button", { type: "button", class: "btn" });
    const target = () => {
      const mode = context.displayMode === "fullscreen" ? "inline" : "fullscreen";
      return canDisplay(mode) ? mode : null;
    };
    const sync = () => {
      const mode = target();
      button.hidden = !mode;
      button.textContent = mode === "inline" ? "Exit full screen" : "Full screen";
    };
    button.addEventListener("click", async () => {
      const mode = target();
      if (!mode) return;
      try {
        await requestDisplayMode(mode);
      } catch (error) {
        if (onError) onError(error);
      }
      sync();
    });
    on("context", sync);
    sync();
    return button;
  }

  // ---------- tabs, states, notices ----------

  let tabsMade = 0;

  /** tabs([{id, label}], onChange, current, panel) -> {el, set(id)}; arrow keys move
   *  between tabs. panel (optional): the element the tabs switch, made their tabpanel. */
  function tabs(items, onChange, current, panel) {
    const buttons = new Map();
    const bar = el("div", { class: "tabs", role: "tablist" });
    const prefix = `tabs${++tabsMade}`;
    if (panel) {
      panel.setAttribute("role", "tabpanel");
      panel.id = panel.id || `${prefix}-panel`;
    }
    const label = (id) => {
      if (panel && buttons.has(id)) panel.setAttribute("aria-labelledby", buttons.get(id).id);
    };
    const set = (id) => {
      for (const [key, button] of buttons) {
        button.setAttribute("aria-selected", key === id ? "true" : "false");
        button.tabIndex = key === id ? 0 : -1;
      }
      label(id);
      onChange(id);
    };
    items.forEach((item, index) => {
      const button = el("button", { type: "button", class: "tab", role: "tab", text: item.label,
                                    id: `${prefix}-${index}`, "aria-controls": panel ? panel.id : null,
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
    label(current);
    const selected = () => [...buttons.values()].find((button) => button.getAttribute("aria-selected") === "true") || null;
    return { el: bar, set, selected, focus: () => (selected() || buttons.values().next().value || bar).focus() };
  }

  /** A loading, empty, error or info block. */
  function state(kind, title, detail) {
    return el("div", { class: `state ${kind}`, role: kind === "error" ? "alert" : "status" },
      el("h2", { text: title }), detail ? el("p", { text: detail }) : null);
  }

  const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), summary, [tabindex="0"]';

  /** The focusable control next to node in document order (after it, else before it),
   *  outside node and shown: where focus goes when node is removed. */
  function neighbour(node) {
    const shown = [...document.querySelectorAll(FOCUSABLE)]
      .filter((item) => !node.contains(item) && item.getClientRects().length);
    const after = shown.find((item) => node.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_FOLLOWING);
    return after || shown.filter((item) => node.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_PRECEDING).pop() || null;
  }

  /** A dismissible notice line (kind: info, warning, error); Dismiss gives keyboard focus
   *  to the next control. */
  function notice(kind, text) {
    const box = el("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" },
      el("span", { class: "grow", text }));
    box.appendChild(el("button", { type: "button", class: "btn-link", text: "Dismiss",
                                   on: { click: () => {
                                     const next = box.contains(document.activeElement) ? neighbour(box) : null;
                                     box.remove();
                                     if (next) next.focus();
                                   } } }));
    return box;
  }

  return {
    PROTOCOL_VERSION, on, connect, request, notify, canCallTools, callTool, resultText, resultJson, log,
    requestDisplayMode, canDisplay, context: () => context, capabilities: () => capabilities,
    el, svg, clear, append, matches, naturally, plural, filterBox, table, tabs, state, notice,
    levelBadge, chips, segmented, facts, keepFocus, focusMark, refocus, neighbour, announce, roomy,
    exactPattern, pageBudget, readPages, toolCache, displayModeButton, closed: () => tornDown,
  };
})();
