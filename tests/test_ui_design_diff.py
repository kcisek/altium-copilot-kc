"""The design diff view (roadmap #76, server/ui/design_diff.html): its ui:// resource and
link, the self-contained page, the read-only tools it calls (one at a time, through one
guard), the messages it may send (fixed words; designators, net names and pads only under
strict patterns; commit hashes), the lessons of the earlier views it keeps, and its data
contract against diff_design on Git repositories built in tmp_path (the diff tests' JSON
sheets and boards, and Altium's public Bluetooth Sentinel example with edited revisions),
a synthetic comparison with every section, two folders, several pages and a later page,
paging as the view pages (every change once per section), and the unchanged text."""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.ui_views as uv
import services.design_diff as sdd  # the module server.main uses (its _CACHE, its parsers)
from tests.test_design_diff import (  # noqa: F401 - fixtures used by name
    BOARD_1, BOARD_2, LITE, PRJ, PROD_A, PROD_B, SHEET_1, SHEET_2, board_a, board_b, clone,
    commit_all, git, isolated_git, json_parsers, make_folder, needs_git, sheets_a, sheets_b, source,
)
from tests.test_ui_views import _example, _handle, _tools

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
VIEW = uv.view_for("diff_design")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
CALLED = {"diff_design", "get_component", "query_net"}
MESSAGED = {"get_board_view", "get_component", "get_sheet_context", "query_net", "diff_design"}
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
SENTINEL = EXAMPLES / "Bluetooth Sentinel"


@pytest.fixture(autouse=True)
def fresh_cache():
    # The cache diff_design reads: the module server.main imported, not another copy of it.
    assert main_mod.diff_revisions is sdd.diff_revisions and main_mod.diff_folders is sdd.diff_folders
    sdd._CACHE.clear()
    yield
    sdd._CACHE.clear()


def _page() -> str:
    return (UI / VIEW.file).read_text(encoding="utf-8")


def _script() -> str:
    return _page().split(uv.JS_MARKER, 1)[1]


def _contract() -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>', _page(), re.S)
    return json.loads(block.group(1))


def _function(name: str) -> str:
    """A function of the page's script, up to its closing brace at two spaces."""
    script = _script()
    start = re.search(rf"\n  (?:async )?function {name}\(", script).start()
    return script[start:script.index("\n  }\n", start) + 4]


def _rows_at(data, path: str) -> list:
    """The rows a contract path names, read as the page's rowsAt does."""
    if path == "result":
        return [data]
    rows = [data]
    for part in path.split("."):
        key = part.removesuffix("[]")
        out = []
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if part.endswith("[]"):
                out.extend(value if isinstance(value, list) else [])
            elif value is not None:
                out.append(value)
        rows = out
    return rows


def _check(data: dict) -> list[str]:
    """The page's checkContract: the required keys of every shape."""
    problems = []
    for path, shape in _contract()["shapes"].items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


# ---------- the view's reading, in Python ----------

def _sig(page: dict) -> str:
    """The page's pageSig: what makes a page one of this comparison."""
    return json.dumps([page.get("a"), page.get("b"), page.get("units"), page.get("section"), page["total"]])


def _view_read(**args) -> dict:
    """diff_design read as the view reads it: from offset 0 (again, when the call's result
    is a later page), then each next_offset with the same arguments; every page checked
    against the contract and against the first page's sides, units, section and total.
    Each change is tagged with its section and its place in the tool's list."""
    asked = dict(args)
    offset = asked.pop("offset", 0)
    first = json.loads(main_mod.diff_design(**asked, offset=offset))
    assert "error" not in first, first
    if first["offset"]:
        later = first
        first = json.loads(main_mod.diff_design(**asked, offset=0))
        assert _sig(first) == _sig(later)
    pages = [first]
    while pages[-1]["has_more"]:
        pages.append(json.loads(main_mod.diff_design(**asked, offset=pages[-1]["next_offset"])))
        assert len(pages) <= _contract()["max_pages"]
    items = []
    for page in pages:
        assert _check(page) == [], _check(page)
        assert _sig(page) == _sig(first)
        for index, entry in enumerate(page["changes"]):
            items.append((entry.get("section") or page.get("section"), page["offset"] + index, entry))
    return {"first": first, "pages": pages, "items": items}


def _every_change_once(read: dict) -> None:
    first = read["first"]
    ids = [(section, index) for section, index, _ in read["items"]]
    assert len(ids) == len(set(ids)) == first["total"]
    assert [index for _, index, _ in read["items"]] == list(range(first["total"]))
    counted = {}
    for section, _, entry in read["items"]:
        counted.setdefault(section, {}).setdefault(entry["change"], 0)
        counted[section][entry["change"]] += 1
    if first.get("section"):
        assert set(counted) <= {first["section"]}
        assert counted.get(first["section"], {}) == first["summary"].get(first["section"], {})
    else:
        assert counted == first["summary"]


# ---------- a synthetic comparison with every section ----------

def _full_model(units: str | None = None) -> dict:
    """The diff tests' sheets and boards, both sides: every section has changes."""
    a = source(sheets_a(), pcb=board_a(), variants=[PROD_A], parameters={"Revision": "A"}, digest="a")
    b = source(sheets_b(), pcb=board_b(), variants=[PROD_B, LITE], parameters={"Revision": "B"},
               sheets=("Main", "Power"), digest="b")
    a.label = {"source": "folder", "folder": "rev-A"}
    b.label = {"source": "folder", "folder": "rev-B"}
    return sdd.build_design_diff(a, b, units)


@pytest.fixture
def synthetic(monkeypatch):
    """diff_design in folder mode answering with the synthetic model."""
    models = {None: _full_model(), "mm": _full_model("mm")}
    monkeypatch.setattr(main_mod, "_project", None)
    monkeypatch.setattr(main_mod, "diff_folders",
                        lambda a, b, name_a, name_b, units: dict(models[units]))
    return {"project_dir_a": "rev-A", "project_dir_b": "rev-B"}


# ---------- resource and link ----------

def test_the_view_is_a_listed_resource_linked_from_its_tool():
    assert VIEW.uri == "ui://altium-copilot-kc/design-diff.html" and VIEW.file == "design_diff.html"
    assert VIEW.name == "design-diff" and VIEW.title == "Design diff"
    assert VIEW.description.startswith(
        "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
        "diff_design's result; not for reading as text.")
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    row = listed[VIEW.uri]
    assert row["mimeType"] == uv.MIME_TYPE and row["_meta"] == uv.RESOURCE_META
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=VIEW.uri)))
    (content,) = result["contents"]
    assert content["text"] == uv.render(VIEW)
    tools = _tools()
    assert tools["diff_design"]["_meta"] == {
        "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
        "ui": {"resourceUri": VIEW.uri}, "ui/resourceUri": VIEW.uri}
    # One tool per view; the tools the links name open no view of their own here, and no
    # app-only tool was added.
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)
    for name in ("get_component", "query_net", "get_sheet_context"):
        assert "ui" not in (tools[name].get("_meta") or {}), name
    assert uv.APP_ONLY_TOOLS == ("get_board_view_geometry",)
    assert "The design diff (#76) is linked from diff_design only." in uv.__doc__
    assert "never a channel for text from the design" in uv.__doc__


def test_the_page_is_self_contained():
    page = uv.render(VIEW)
    for marker in (uv.CSP_MARKER, uv.CSS_MARKER, uv.JS_MARKER):
        assert marker not in page
    assert set(re.findall(r"https?://[^\s\"'<>)]+", page)) <= ALLOWED_URLS
    assert not re.search(r"<(link|img|iframe|object|embed|base|form|a|video|audio|source)\b", page, re.I)
    assert not re.search(r"<script[^>]*\bsrc=", page, re.I)
    styles = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    scripts = "".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    assert "@import" not in styles and "@font-face" not in styles and "url(" not in styles
    assert "url(" not in _script()
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|"
                         r"importScripts|Worker|window\.open|localStorage|sessionStorage|"
                         r"indexedDB|cookie|innerHTML|outerHTML|insertAdjacentHTML|eval|href|"
                         r"clipboard|execCommand)\b", scripts)
    assert page.count("const Shell = (() => {") == 1
    assert f'<meta http-equiv="Content-Security-Policy" content="{uv.PAGE_CSP}">' in page


# ---------- the read-only rule ----------

def test_the_page_calls_only_read_only_tools_through_one_guard_one_at_a_time():
    script = _script()
    allowed = re.search(r"const READ_ONLY_TOOLS = new Set\(\[(.*?)\]\);", script, re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', allowed)) == CALLED
    tools = _tools()
    for name in CALLED | MESSAGED:
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    assert script.count("Shell.callTool(") == 1 and "Shell.toolCache(" not in script
    guard = _function("call")
    assert "Shell.callTool(" in guard
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in guard
    assert "if (view.closed) return Promise.reject(" in guard and "if (!canCall()) return Promise.reject(" in guard
    # One call at a time: each waits for the one before it.
    assert "const run = queue.then(() => {" in guard and "queue = run.catch(() => {});" in guard
    assert "if (!READ_ONLY_TOOLS.has(tool)) return;" in _function("askDetail")
    assert set(re.findall(r'\bcall\("([a-z_]+)"', script)) == {"diff_design"}
    assert "askDetail(tool, args)" in _function("linkSection")
    assert 'const tool = item.type === "part" ? "get_component" : "query_net";' in _function("linkSection")
    # No tool that changes the server's state is named anywhere in the page.
    page = _page()
    for name in ("refresh_netlist", "set_active_variant", "set_project_dir", "package_for_xfn"):
        assert name not in page, name
    for method in ("ui/open-link", "ui/update-model-context", "openLink"):
        assert method not in page, method


# ---------- the message rule ----------

def test_messages_name_read_only_tools_in_fixed_words():
    script = _script()
    listed = re.search(r"const MESSAGE_TOOLS = new Set\(\[(.*?)\]\);", script, re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', listed)) == MESSAGED
    sentence = _function("sentence")
    # Every name is checked before any word is written: a name outside the rule, or more
    # than MAX_NAMES, makes no sentence.
    assert ('if (names.length > MAX_NAMES || !names.every((item) => validName(item.type, item.name))) '
            'return "";') in sentence
    # The only interpolations: the quoted names, the get_board_view arguments built from
    # them, and the revisions (hashes or working) from revisionArgs.
    assert set(re.findall(r"\$\{([^}]*)\}", sentence)) == {
        "item.name", "quoted(parts)", "quoted(nets)", "parts.length === 1 ? \"part\" : \"parts\"",
        "quoted(pads)", "pads.length === 1 ? \"pad\" : \"pads\"", "nets.length === 1 ? \"net\" : \"nets\"",
        '[...parts, ...pads].map((item) => item.name).join(",")', 'nets.map((item) => item.name).join(",")',
        "what", "asked", "withArgs(['section \"ict_hazards\"'])", "withArgs([])",
        "list.slice(0, -1).join(\", \")", "list[list.length - 1]", "joined", "words",
    }
    assert not re.search(r"\b(view|data|row|entry|answer|first|page)\.", sentence)
    for text in ("Show ${what} on the board view (call get_board_view with ${asked}).",
                 "on its schematic sheet: call get_component with refdes ${quoted(parts)}, then get_sheet_context for its sheet.",
                 "on the schematic: call query_net for ${quoted(nets)}, then get_sheet_context for the sheets it is on.",
                 "Explain the ICT hazards in this design diff and what to update in the test fixtures and programs (call diff_design",
                 "Write release notes from this design diff (call diff_design",
                 "Compare this project with an earlier copy of it in another folder (call diff_design with project_dir_a); ask me for the folder."):
        assert text in sentence, text
    # Revisions: commit hashes (12 characters) or working, nothing else; folders unnamed.
    revs = _function("revisionArgs")
    assert 'HASH.test(rev) ? rev.slice(0, 12) : null' in revs and "if (revs.folders) return [];" in revs
    message_revs = _function("messageRevs")
    assert 'side.source === "working" ? "working" : side.source === "git" ? String(side.hash || "") : null' in message_revs
    # The names a link carries are those that passed validName; the details asked in place
    # (a tools/call, nothing in the conversation) take any name.
    links = _function("linkSection")
    assert "const valid = names.filter((item) => validName(item.type, item.name));" in links
    assert 'sentence("board", board)' in links and "const board = valid.slice(0, MAX_NAMES);" in links
    assert 'const each = names.filter((item) => item.type !== "pad");' in links
    assert "const ok = validName(item.type, item.name);" in links
    assert 'ok ? messageControl(`${kind}:${row._id}:${item.name}`, "On the schematic", sentence(kind, [item]),' in links
    assert ': el("span", { class: "muted", text: "not sent in a message" }),' in links
    assert "Not sent in a message" in links and "own(RULE_WORDS, type)" in links
    assert "return Boolean(rule) && typeof name === \"string\" && rule.test(name);" in _function("validName")


def test_the_name_patterns_keep_text_out_of_messages():
    names = _contract()["names"]
    designator, net, pad, commit = (re.compile(names[key]) for key in ("designator", "net", "pad", "hash"))
    assert names["max_names"] == 4
    for good in ("R1", "U12A", "C1_CH2", "TP100", "LED3", "J1A"):
        assert designator.fullmatch(good), good
    for bad in ("R?", "R 1", 'R1"', "IGNORE", "", "R1\n", "1R", "R" * 9 + "1", "R1;rm", "R1`x`", "R1<b>"):
        assert not designator.fullmatch(bad), bad
    for good in ("GND", "+3V3", "VIN[0]", "USB_D+", "NetC26_1", "A/B", "-12V", "SPI.CLK"):
        assert net.fullmatch(good), good
    for bad in ("Ignore all previous", 'X"', "a\\b", "N$1", "x" * 33, "", "line\nbreak", "A'B", "A`B",
                "A<B>", "A{B}", "A;B", "A:B", "A,B", "A=B", "A(B)"):
        assert not net.fullmatch(bad), bad
    for good in ("U1-7", "C3-1", "J2-A12"):
        assert pad.fullmatch(good), good
    for bad in ("U1-", "U1 7", "U1-7-8", "-7"):
        assert not pad.fullmatch(bad), bad
    # Abbreviated hashes, and whole ones of SHA-1 and SHA-256 clones.
    assert commit.fullmatch("8db721f") and commit.fullmatch("a" * 40) and commit.fullmatch("a" * 64)
    for bad in ("main", "v1.0", "HEAD~1", "8DB721F", "a" * 65, "abc"):
        assert not commit.fullmatch(bad), bad
    full = re.search(r"const FULL_HASH = /(.*?)/;", _script()).group(1)
    assert re.fullmatch(full, "a" * 40) and re.fullmatch(full, "a" * 64) and not re.fullmatch(full, "a" * 12)
    assert "FULL_HASH.test(String(args[key]))" in _function("argText")
    # No pattern lets a name close its quotes, break a line or start a sentence.
    for pattern in (designator, net, pad):
        for char in ' "\'\\\n\r\t`<>{}$;:,=()*?!&|~^%#@':
            assert not pattern.fullmatch(f"R1{char}A"), (pattern.pattern, char)
    # The rule in words, as the page says it for a name it does not send, is the patterns'.
    words = dict(re.findall(r'^\s+(part|net|pad): "([^"]+)",$',
                            re.search(r"const RULE_WORDS = \{(.*?)\};", _script(), re.S).group(1), re.M))
    assert set(words) == {"part", "net", "pad"}
    assert "at most 24 characters" in words["part"] and designator.fullmatch("ABCDEFGH123456" + "A" * 10)
    assert not designator.fullmatch("ABCDEFGH123456" + "A" * 11) and not designator.fullmatch("R1" + "A" * 23)
    assert not designator.fullmatch("TP1.2") and not designator.fullmatch("J1-A")
    assert "_ + - . [ ] /" in words["net"] and "at most 32 characters" in words["net"]
    assert net.fullmatch("N" * 32) and not net.fullmatch("N" * 33)
    assert "at most 8 letters or digits" in words["pad"] and pad.fullmatch("U1-ABCD1234")
    assert not pad.fullmatch("U1-ABCD12345")


def test_the_rule_lets_the_public_examples_names_through(monkeypatch):
    """Not too strict: most designators and nets of a public example pass."""
    _example(monkeypatch, "Bluetooth Sentinel", "Bluetooth_Sentinel.PrjPcb")
    _, netlist, _ = main_mod._require_project()
    names = _contract()["names"]
    designators = list(netlist["components"])
    nets = list(netlist["nets"])
    passed = sum(1 for name in designators if re.fullmatch(names["designator"], name))
    assert designators and passed == len(designators)
    passed = sum(1 for name in nets if re.fullmatch(names["net"], name))
    assert nets and passed >= 0.9 * len(nets)


def test_send_message_only_from_a_click_and_only_when_the_host_takes_one():
    script = _script()
    assert script.count("Shell.sendMessage(") == 1
    ask = _function("askClaude")
    assert "Shell.sendMessage(" in ask and "if (!Shell.canSendMessage())" in ask
    calls = [match.start() for match in re.finditer(r"askClaude\(", script)]
    for at in calls:
        line = script[script.rfind("\n", 0, at) + 1:script.find("\n", at)]
        assert "function askClaude(" in line or "on: { click: () => askClaude(" in line, line
    assert len(calls) == 2
    control = _function("messageControl")
    assert "if (!Shell.canSendMessage()) return askField(text, null, id, field);" in control
    assert '"aria-disabled": pending ? "true" : null' in control and "Sending to the conversation" in control
    assert "target.focus()" in _function("redrawMessages")
    field = _function("askField")
    assert "readonly: true" in field and "input.select()" in field
    assert 'el("label", {}, el("span", { class: "ask-label", text: purpose }), input)' in field
    # Each sentence to copy is labelled with its purpose ("Ask Claude: release notes"), so
    # two fields side by side never share one name.
    assert 'const field = purpose || `Ask Claude: ${label.replace(' in control
    assert control.count(", id, field)") == 3
    assert "`Ask Claude: ${NAME_WORDS[item.type]} ${item.name} on the schematic`" in _function("linkSection")
    # Messages of another comparison do not carry over.
    assert "view.messages.clear();" in _function("finish")


# ---------- the lessons of the earlier views ----------

def test_the_page_keeps_the_lessons_of_the_earlier_views():
    script = _script()
    load = _function("loadFrom")
    rest = _function("readRest")
    # Every page read (from offset 0 when the result is a later page), the real stop reason
    # named, pages of another comparison never merged, the closed view said so.
    assert "await readRest(state, gen, Shell.pageBudget(MAX_PAGES, 1));" in load
    assert "first = await diffPage(args, 0, first);" in load and "this result starts at offset" in load
    assert "Shell.readPages({" in rest and "closed: () => view.closed || gen !== view.gen" in rest
    assert 'state.stop = !stopped ? "" : view.closed ? "the view was closed" :' in rest
    page = _function("diffPage")
    assert "if (expect && pageSig(page) !== pageSig(expect))" in page and "error.changed = true;" in page
    assert ("const pageSig = (page) => JSON.stringify([page.a || null, page.b || null, page.units || null, "
            "page.section || null, page.total]);") in script
    assert _contract()["max_pages"] == 60
    assert "Showing ${data.rows.length} of ${plural(data.total, \"change\")}: ${data.stopReason}." in _function("renderNotices")
    assert "The view was closed: it sends no more requests" in script
    assert "if (result && result.isError)" in _function("loadResult")
    assert "if (text && text === view.resultSig) return;" in _function("loadResult")
    # Every answer labelled with the arguments it was asked under; a later page given as the
    # result is said to be read again from offset 0 only when that read succeeded.
    line = _function("argLine")
    assert "Answer: " in line and "argText(asked)" in line
    assert ('later ? (data.reread ? `; read with ${argText(data.args)} from offset 0` : '
            '"; its first page could not be read") : ""') in line
    assert "first = await diffPage(args, 0, first);\n        reread = true;" in load
    assert "reread: Boolean(state.reread)" in _function("finish")
    assert "argLine(data)" in _function("renderHeader") and "argLine(data)" in _function("renderSides")
    assert "`${tool}(${callArgs(args)}) · the loaded project now`" in _function("answerBlock")
    assert "from ${view.commits.asked}" in _function("pickerBlock")
    # A stopped read is announced as the changes read of the total; a summary the tool did
    # not give (a later page) keeps the counts of the changes read apart from the total.
    finish = _function("finish")
    assert ("data.stopReason ? `Design diff: ${data.rows.length} of ${plural(data.total, \"change\")} read"
            "${hazards}; the reading stopped, the notice says why`") in finish
    assert "`${plural(total, \"change\")}; of the ${data.rows.length} read`" in _function("renderSummary")
    # The files on disk are read again per page; the pages cannot show a save in between
    # that keeps the count, so a read of several pages says so.
    notices = _function("renderNotices")
    assert 'const onDisk = [data.a, data.b].some((side) => isObject(side) && side.source === "working");' in notices
    assert "if (onDisk && data.pages > 1)" in notices and "the pages may mix the two states" in notices
    # A result the view cannot draw is an error state, never a page left on "Reading".
    assert 'showState("error", "The design diff could not be read", error.message);' in _function("loadResult")
    # Asked again from an error drawn as a state, the error and its picker stay (focus kept).
    assert "} else if (view.errorShown) {" in _function("reask") and "} else if (view.errorShown) {" in _function("progress")
    assert "view.errorShown = null;" in finish and "view.errorShown = null;" not in load
    # A failed detail is never kept as the answer (Retry asks again).
    assert "if ((entry && !entry.error) || view.closed || !canCall())" in _function("askDetail")
    # Keyboard focus kept on redraw and after a button that disappears; busy buttons are
    # aria-disabled (they keep focus), never disabled.
    assert "Shell.keepFocus(" in script and "Shell.refocus(" in script and "Shell.focusMark(" in script
    assert '"aria-disabled": view.busy || same || isCurrent ? "true" : null' in _function("pickerBlock")
    assert "disabled:" not in _function("pickerBlock")
    # One live region (the shell's); the table announces nothing itself.
    assert 'role: "status"' not in script
    assert re.findall(r'"aria-live"[^)]*', script) == ['"aria-live", "off"']
    assert script.count("quiet(Shell.table(") == script.count("Shell.table(") == 1
    assert "if (sig === view.noticeSig) return;" in _function("renderNotices")
    # Plurals; "at least" for a capped list; the host's height and narrow widths.
    assert "const plural = (count, word)" in script and "(at least ${names.length}: the tool lists ${SAMPLE})" in script
    assert _contract()["sample"] == sdd.SAMPLE and _contract()["recent_commits"] == sdd.RECENT_COMMITS
    assert 'return limit || (Shell.context().displayMode === "fullscreen" ? window.innerHeight : 0);' in _function("hostHeight")
    assert "const short = () => !Shell.roomy() || !tall(800);" in script
    assert "const compactHost = () => window.innerWidth <= 980 && !tall(1100);" in script
    assert '.split.solo > .content { display: none; }' in _page()
    assert 'ui.split.classList.toggle("solo", Boolean(row) && compactHost());' in _function("drawPane")
    fit = _function("fitOwnHeight")
    assert 'app.style.setProperty("--pane-height", `${px}px`)' in fit and "window.innerHeight" in fit
    # DOM from text only; strings from a result looked up by own keys only.
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert not re.search(r"\b(SECTIONS|KINDS|CHANGES|LABELS|ERROR_TITLES|NAME_RULES|SUBJECT_TYPES)\[", script)
    for use in ("own(SECTIONS, section)", "own(KINDS, kind)", "own(CHANGES, `${section}.${change}`)",
                "own(LABELS, key)", "own(ERROR_TITLES, payload.error)", "own(NAME_RULES, type)"):
        assert use in script, use
    assert "Object.prototype.hasOwnProperty.call(object, key)" in script
    # Counts keyed by the result's section and change names: objects without a prototype
    # ("__proto__" is then an ordinary key); the contract check reads own keys only, and
    # never uses "in" on a row that may not be an object.
    model = _function("buildModel")
    assert "const counted = Object.create(null);" in model and "Object.create(null);" in model
    assert "counted[row.section] || {}" not in model
    assert "rowsAt(data, path).filter((row) => !has(row, key))" in _function("checkContract")
    assert "fmt(own(before, key)), fmt(own(after, key))" in _function("fieldRows")
    # The page reads no host-specific field.
    assert "toolInfo" not in script and "userAgent" not in script
    # The Section column follows the grouping: the list is drawn again when it changes.
    filters = _function("renderFilters")
    assert "view.groupBy = id;\n      // Drawn again" in filters and "renderList();" in filters
    assert 'hidden: narrow || view.groupBy === "section"' in _function("columns")
    # A cramped host (under 520 px): the call's line and the sides in one disclosure, the
    # hazards' question in the notes.
    assert "return limit > 0 && limit < 520;" in script
    assert "|${short()}|${cramped()}|${compactHost()}`" in script  # a host that becomes cramped redraws
    assert "revs && !cramped() ? hazardsQuestion(revs) : null" in _function("renderHazards")
    assert 'if (cramped() && revs && (data.hazards.length || sectionCount(data.summary, "ict_hazards")))' in _function("renderSummary")


def test_colour_is_never_the_only_cue():
    contract = _contract()
    # Each kind has a glyph and a word; its badge holds both.
    assert all(len(glyph) == 1 and word for glyph, word in contract["kinds"].values())
    badge = _function("kindBadge")
    assert 'el("span", { class: "kglyph", "aria-hidden": "true", text: info.glyph }), text || info.label' in badge
    # A side with nothing says so in words.
    cell = _function("sideCell")
    assert '"not in a"' in cell and '"not in b"' in cell


def test_the_list_cells_say_what_each_value_is():
    """A before or after cell names its field (a rule's priority, a parameter, a layer's
    copper thickness), with a per-side label where one label holds two meanings; only a
    lone field the change's own label names, or a designator, net name or position, stays
    bare. Rows of the same BOM line or fitting in two variants show their variant."""
    texts = _function("sideTexts")
    assert ("const bare = (row) => diff.length === 1 &&\n"
            "      (row.label.toLowerCase() === String(spec.label).toLowerCase() || BARE_LABELS.has(row.label.toLowerCase()));") in texts
    assert '`${(key === "a" ? row.la : row.lb) || row.label}: ${row[key]}`' in texts
    assert 'const BARE_LABELS = new Set(["designator", "net name", "position"]);' in _script()
    rows = _function("fieldRows")
    for pair in ('["Pins left", "Pins joined"]', '["Designators left", "Designators joined"]',
                 '["Members left", "Members joined"]'):
        assert pair in rows, pair
    # The labels the rows get: a lone one is bare only when the change label is the same
    # word (Footprint, PCB document, Fitting, Copper layers, Layer order, Stack thickness).
    contract = _contract()["changes"]
    for name in ("components.footprint", "placement.footprint", "project.pcb_document", "variants.fitting",
                 "stackup.copper_layers", "stackup.reordered", "stackup.thickness"):
        assert contract[name][1] in ("Footprint", "PCB document", "Fitting", "Copper layers", "Layer order",
                                     "Stack thickness"), name
    for name in ("rules.changed", "stackup.layer_changed", "stackup.profile_changed", "components.parameters",
                 "project.parameters", "variants.parameters", "components.revalued", "nets.pin_moved"):
        assert contract[name][1] not in ("Footprint", "Designator", "Net name", "Position"), name
    describe = _function("describe")
    assert 'const variant = spec.subjectKey !== "variant" && typeof entry.variant === "string" ? entry.variant : "";' in describe
    columns = _function("columns")
    assert "row.variant ? `${row.subjectType} · variant ${row.variant}` : row.subjectType" in columns
    assert 'narrow ? (row.variant ? el("span", { class: "subtype", text: `variant ${row.variant}` }) : null)' in columns
    # Sheets and BOM lines in two variants: the tool gives the variant on each.
    full = _full_model()
    lines = [entry for entry in full["changes"] if entry["section"] == "bom"]
    assert len({entry["variant"] for entry in lines}) >= 2
    same = {}
    for entry in lines:
        same.setdefault((entry["change"], entry["line"]), set()).add(entry["variant"])
    assert any(len(variants) > 1 for variants in same.values())


def test_the_page_draws_every_state():
    script = _script()
    titles = re.search(r"const ERROR_TITLES = \{(.*?)\};", script, re.S).group(1)
    codes = set(re.findall(r"^\s+(\w+):", titles, re.M))
    sources = ((ROOT / "server" / "main.py").read_text(encoding="utf-8")
               + Path(sdd.__file__).read_text(encoding="utf-8"))
    for code in codes:
        assert f'"{code}"' in sources, code
    # Every error the service or the tool gives has its title.
    produced = set(re.findall(r'DiffError\(\s*"(\w+)"', Path(sdd.__file__).read_text(encoding="utf-8")))
    produced |= {"no_project", "folders_required", "diff_failed"}
    assert produced <= codes, produced - codes
    for words in ("No design change between a and b", "No change in ${sectionTitle(data.section)}",
                  "was cancelled", "The view was closed", "Waiting for the tool's arguments",
                  "This view needs a host that shows MCP Apps", "This host does not let the view call tools",
                  "Ask Claude to compare with a folder", "List recent commits", "Show every section",
                  "Read the rest", "Read again"):
        assert words in script, words
    # An unknown revision offers the picker; a folder that is not a clone, the folder option.
    error = _function("showError")
    assert "PICKER_ERRORS.has(payload.error)" in error and 'payload.error === "not_a_git_repository"' in error
    assert set(re.findall(r'"(\w+)"', re.search(r"const PICKER_ERRORS = new Set\(\[(.*?)\]\);", script).group(1))) == {
        "invalid_revision", "revision_not_found", "no_parent_commit", "project_not_in_revision"}


def test_the_picker_only_asks_diff_design_again():
    picker = _function("pickerBlock")
    assert 'reask(picked, "the revisions picked")' in picker and 'reask(parentArgs, "what the commit changed")' in picker
    assert "const picked = { rev_a: view.pickA, rev_b: view.pickB, units: args.units };" in picker
    assert "const parentArgs = { rev_b: view.pickB, units: args.units };" in picker
    assert "HASH.test(String(commit.hash || \"\"))" in picker
    # Folders get no picker; the commits come with a call without revisions.
    assert "if (!gitMode(data.args))" in _function("renderPicker")
    listing = _function("listCommits")
    assert 'cleanArgs({ units: base.units, section: "ict_hazards" })' in listing
    assert 'call("diff_design", { ...args, offset: 0 })' in listing
    assert "view.commits = { list: answer.recent_commits, asked: argText(args) };" in listing


# ---------- the data contract ----------

def _produced_changes() -> set[str]:
    """section.change of every entry design_diff.py makes (the BOM's change is a name)."""
    tree = ast.parse(Path(sdd.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_entry":
            section, change = node.args[0], node.args[1]
            if isinstance(section, ast.Constant) and isinstance(change, ast.Constant):
                found.add(f"{section.value}.{change.value}")
    source = Path(sdd.__file__).read_text(encoding="utf-8")
    for change in ("line_added", "line_removed", "quantity"):
        assert f'change, eco = "{change}"' in source, change
        found.add(f"bom.{change}")
    return found


def test_the_contract_names_keys_and_changes_the_tool_produces():
    contract = _contract()
    assert contract["tool"] == VIEW.tool
    assert list(contract["sections"]) == list(sdd.SECTIONS)
    assert set(contract["changes"]) == _produced_changes()
    entry_keys = set(contract["shapes"]["changes[]"]["optional"])
    for name, (kind, label, subject) in contract["changes"].items():
        assert kind in contract["kinds"] and label, name
        assert subject is None or subject in entry_keys, name
    sources = Path(sdd.__file__).read_text(encoding="utf-8") + (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    for path, shape in contract["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert f'"{key}"' in sources or f"{key}=" in sources, f"{path}: {key}"


def test_the_view_reads_the_keys_its_contract_names():
    script = _script()
    keys = {key for shape in _contract()["shapes"].values() for key in shape["required"] + shape["optional"]}
    labels = set(re.findall(r"(\w+): \"", re.search(r"const LABELS = \{(.*?)\};", script, re.S).group(1)))
    for key in sorted(keys):
        if key.endswith("_mil"):
            # Read in the result's units: uk("x") is x_mil or x_mm.
            assert f'uk("{key[:-4]}")' in script, key
        else:
            assert re.search(rf'\.{key}\b|"{key}"', script) or key in labels, key


def test_the_contract_holds_on_every_section_and_paging_gives_every_change_once(synthetic, monkeypatch):
    read = _view_read(**synthetic)
    first = read["first"]
    assert set(first["summary"]) == set(sdd.SECTIONS)  # every section has changes
    assert first["summary"]["ict_hazards"] and first["changes"][0]["section"] == "ict_hazards"
    _every_change_once(read)
    # Small pages: several, of varied sizes, each checked; the same changes in the same order.
    whole = [entry["text"] for _, _, entry in read["items"]]
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 3500)
    paged = _view_read(**synthetic)
    assert len(paged["pages"]) >= 4 and len({len(page["changes"]) for page in paged["pages"]}) >= 2
    assert [entry["text"] for _, _, entry in paged["items"]] == whole
    _every_change_once(paged)
    # A later page as the result: read again from offset 0, nothing lost or doubled.
    later = paged["pages"][2]["offset"]
    again = _view_read(**synthetic, offset=later)
    assert [entry["text"] for _, _, entry in again["items"]] == whole
    # One section at a time: its pages hold its changes once, without a section key.
    for section in sdd.SECTIONS:
        narrowed = _view_read(**synthetic, section=section)
        _every_change_once(narrowed)
        assert all("section" not in entry for _, _, entry in narrowed["items"])
        assert [entry["text"] for _, _, entry in narrowed["items"]] == [
            text for (where, _, entry), text in zip(read["items"], whole) if where == section]


def test_the_contract_holds_in_mm(synthetic):
    read = _view_read(**synthetic, units="mm")
    assert read["first"]["units"] == "mm"
    moved = next(entry for _, _, entry in read["items"] if entry["change"] == "moved")
    assert {"x_mm", "dx_mm", "distance_mm"} <= set(moved) and "x_mil" not in moved
    rules = [entry for _, _, entry in read["items"] if entry.get("changes")]
    assert any("from_mm" in item for entry in rules for item in entry["changes"])
    assert "length_delta_mm" in read["first"]["routing_totals"]
    assert 'own(data.routing, `length_delta_${data.units}`)' in _script()


@needs_git
def test_the_contract_holds_on_a_git_clone_with_recent_commits(clone, monkeypatch):
    root, project, (first, second) = clone
    monkeypatch.setattr(main_mod, "_project", {"name": "Board", "root_dir": str(project),
                                               "prj_pcb_path": str(project / "Board.PrjPcb")})
    default = _view_read()
    assert [commit["hash"] for commit in default["first"]["recent_commits"]] == [second, first]
    assert default["first"]["no_changes"] is True and default["first"]["no_changes_hint"]
    assert default["first"]["a"]["source"] == "git" and default["first"]["b"]["source"] == "working"
    _every_change_once(default)
    picked = _view_read(rev_a=first, rev_b=second)
    assert picked["first"]["summary"]["components"] and picked["first"]["summary"]["placement"]
    _every_change_once(picked)
    # The picker's "What b changed": b against its parent; the commit list's own call.
    parent = _view_read(rev_b=second)
    assert parent["first"]["a"]["hash"] == first and _sig(parent["first"]) == _sig(picked["first"])
    listing = json.loads(main_mod.diff_design(section="ict_hazards", offset=0))
    assert _check(listing) == [] and len(listing["recent_commits"]) == 2
    # An unknown revision and a folder that is not a clone are states with their codes.
    assert json.loads(main_mod.diff_design(rev_a="v9"))["error"] == "revision_not_found"


@needs_git
def test_the_contract_holds_on_hazards_committed_in_git(tmp_path, isolated_git, json_parsers, monkeypatch):
    """A clone whose revisions rename, merge and move pins between nets and give a
    designator to another part: ICT hazards first, every change once over several pages."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "Board.PrjPcb").write_text("[Design]\n[Document1]\nDocumentPath=Main.SchDoc\n"
                                       "[Document2]\nDocumentPath=Board.PcbDoc\n", encoding="utf-8")
    sheet_a = {f"R{i}": [f"RID{i}", "10k", "R0402"] for i in range(1, 40)}
    board_a = [[f"R{i}", f"RID{i}", 100.0 * i, 0.0, 0.0, "top", "R0402", {"1": f"N{i}", "2": "GND"}]
               for i in range(1, 40)]
    (root / "Main.SchDoc").write_text(json.dumps(sheet_a), encoding="utf-8")
    (root / "Board.PcbDoc").write_text(json.dumps(board_a), encoding="utf-8")
    first = commit_all(root, "one")
    sheet_b = {**sheet_a, "R1": ["NEWID", "4k7", "R0603"]}
    board_b = [[d, uid if d != "R1" else "NEWID", x + (5.0 if d in ("R2", "R3") else 0.0), y,
                90.0 if d == "R4" else 0.0, side, fp, {**pads, "1": "N5" if d == "R6" else pads["1"]}]
               for d, uid, x, y, _, side, fp, pads in board_a]
    (root / "Main.SchDoc").write_text(json.dumps(sheet_b), encoding="utf-8")
    (root / "Board.PcbDoc").write_text(json.dumps(board_b), encoding="utf-8")
    second = commit_all(root, "two")
    monkeypatch.setattr(main_mod, "_project", {"name": "Board", "root_dir": str(root),
                                               "prj_pcb_path": str(root / "Board.PrjPcb")})
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 3000)
    read = _view_read(rev_a=first, rev_b=second)
    assert len(read["pages"]) >= 2
    assert {"designator_reused", "net_connections_changed"} <= set(read["first"]["summary"]["ict_hazards"])
    assert read["items"][0][0] == "ict_hazards"
    _every_change_once(read)


@needs_git
def test_a_sha256_clone_fits_the_hash_rule(tmp_path, isolated_git, json_parsers, monkeypatch):
    """A clone with SHA-256 object names: its 64-character hashes pass the contract's hash
    pattern, so the picker lists its commits and the messages name them (cut to 12)."""
    root = tmp_path / "repo256"
    root.mkdir()
    made = subprocess.run(["git", "init", "-q", "--object-format=sha256", "-b", "main"], cwd=root,
                          capture_output=True, text=True)
    if made.returncode:
        pytest.skip("this git makes no SHA-256 repository")
    (root / "Board.PrjPcb").write_text(PRJ, encoding="utf-8")
    (root / "Main.SchDoc").write_text(json.dumps(SHEET_1), encoding="utf-8")
    (root / "Board.PcbDoc").write_text(json.dumps(BOARD_1), encoding="utf-8")
    first = commit_all(root, "one")
    (root / "Main.SchDoc").write_text(json.dumps(SHEET_2), encoding="utf-8")
    (root / "Board.PcbDoc").write_text(json.dumps(BOARD_2), encoding="utf-8")
    second = commit_all(root, "two")
    assert len(first) == len(second) == 64
    monkeypatch.setattr(main_mod, "_project", {"name": "Board", "root_dir": str(root),
                                               "prj_pcb_path": str(root / "Board.PrjPcb")})
    hash_rule = re.compile(_contract()["names"]["hash"])
    full = re.compile(re.search(r"const FULL_HASH = /(.*?)/;", _script()).group(1))
    default = _view_read()
    listed = [commit["hash"] for commit in default["first"]["recent_commits"]]
    assert listed == [second, first] and all(hash_rule.fullmatch(value) and full.fullmatch(value) for value in listed)
    assert all(hash_rule.fullmatch(commit["commit"]) for commit in default["first"]["recent_commits"])
    picked = _view_read(rev_a=first, rev_b=second)
    assert picked["first"]["a"]["hash"] == first and picked["first"]["b"]["hash"] == second
    assert picked["first"]["summary"]["components"]
    _every_change_once(picked)


def test_the_contract_holds_on_two_folders(tmp_path, json_parsers, monkeypatch):
    a = make_folder(tmp_path / "rel1", SHEET_1, BOARD_1)
    b = make_folder(tmp_path / "rel2", SHEET_2, BOARD_2)
    monkeypatch.setattr(main_mod, "_project", None)
    read = _view_read(project_dir_a=str(a), project_dir_b=str(b))
    assert read["first"]["a"] == {"source": "folder", "folder": str(a)}
    _every_change_once(read)
    same = _view_read(project_dir_a=str(a), project_dir_b=str(a))
    assert same["first"]["no_changes"] is True and same["first"]["total"] == 0


def _patch(path: Path, stream: str, old: bytes, new: bytes) -> None:
    olefile = pytest.importorskip("olefile")
    assert len(old) == len(new)
    with olefile.OleFileIO(str(path), write_mode=True) as ole:
        data = ole.openstream(stream).read()
        assert old in data, (stream, old)
        ole.write_stream(stream, data.replace(old, new, 1))


@needs_git
def test_the_contract_holds_on_a_public_example_committed_in_git(tmp_path, isolated_git, monkeypatch):
    """Altium's Bluetooth Sentinel in a scratch clone: a revision with a comment changed on
    a sheet and a rule and a net name changed on the board (same-size stream edits)."""
    if not (SENTINEL / "Bluetooth_Sentinel.PrjPcb").is_file():
        pytest.skip("the Bluetooth Sentinel example is not installed")
    root = tmp_path / "sentinel"
    root.mkdir()
    for item in SENTINEL.iterdir():
        if item.is_dir():
            shutil.copytree(item, root / item.name)
        elif item.suffix.lower() in (".prjpcb", ".schdoc", ".pcbdoc", ".annotation", ".harness"):
            shutil.copy2(item, root / item.name)
    git(root, "init", "-q", "-b", "main")
    first = commit_all(root, "as installed")
    _patch(root / "Microcontroller_STM32F101.SchDoc", "FileHeader", b"|TEXT=100nF|", b"|TEXT=220nF|")
    _patch(root / "Bluetooth_Sentinel.PcbDoc", "Rules6/Data", b"MAXLIMIT=19.685mil", b"MAXLIMIT=29.685mil")
    _patch(root / "Bluetooth_Sentinel.PcbDoc", "Nets6/Data", b"|NAME=VDD_XTAL|", b"|NAME=VDD_XTAB|")
    second = commit_all(root, "edits")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    monkeypatch.setattr(main_mod, "_project", {"name": "Bluetooth_Sentinel", "root_dir": str(root),
                                               "prj_pcb_path": str(root / "Bluetooth_Sentinel.PrjPcb")})
    read = _view_read(rev_a=first, rev_b=second)
    summary = read["first"]["summary"]
    assert summary["components"]["revalued"] == 1 and summary["rules"]["changed"] == 1
    assert summary["nets"]["renamed"] == 1
    _every_change_once(read)
    renamed = next(entry for _, _, entry in read["items"] if entry["change"] == "renamed")
    names = _contract()["names"]
    assert re.fullmatch(names["net"], renamed["net"]) and re.fullmatch(names["net"], renamed["previous_name"])
    default = _view_read()
    assert [commit["hash"] for commit in default["first"]["recent_commits"]] == [second, first]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                          text=True).stdout == status


def test_the_tool_errors_are_states(monkeypatch, tmp_path, isolated_git):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.diff_design())["error"] == "no_project"
    assert json.loads(main_mod.diff_design(project_dir_b="x"))["error"] == "folders_required"
    if shutil.which("git"):
        folder = tmp_path / "plain"
        folder.mkdir()
        (folder / "Board.PrjPcb").write_text("[Design]\n", encoding="utf-8")
        monkeypatch.setattr(main_mod, "_project", {"name": "Board", "root_dir": str(folder),
                                                   "prj_pcb_path": str(folder / "Board.PrjPcb")})
        assert json.loads(main_mod.diff_design())["error"] in ("not_a_git_repository", "git_failed")


# ---------- tool results unchanged ----------

def test_the_tools_text_results_are_unchanged(synthetic, monkeypatch):
    calls = [dict(synthetic), {**synthetic, "section": "nets"}, {**synthetic, "offset": 3},
             {**synthetic, "units": "mm"}, {"project_dir_b": "x"}]
    for args in calls:
        direct = main_mod.diff_design(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name="diff_design", arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], args
        assert result["structuredContent"] == {"result": direct}, args
    tools = _tools()
    assert tools["diff_design"]["outputSchema"]["properties"] == {
        "result": {"title": "Result", "type": "string"}}


# ---------- docs ----------

def test_the_readme_and_changelog_name_the_view():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `diff_design` |" in section
    row = next(line for line in readme.split("## Interactive views", 1)[0].splitlines()
               if line.endswith("| `diff_design` |"))
    assert "(see [Interactive views](#interactive-views))" in row
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #76" in unreleased and "design_diff.html" in unreleased
    # The message rule as the docs state it is the page's, per kind of name.
    view_row = next(line for line in section.splitlines() if line.startswith("| `diff_design` |"))
    flat = " ".join(unreleased.split())
    for text in (view_row, flat, " ".join(uv.__doc__.split())):
        assert "at most 24 characters" in text and "at most 32 characters" in text, text[:80]
        assert "SHA-256" in text
