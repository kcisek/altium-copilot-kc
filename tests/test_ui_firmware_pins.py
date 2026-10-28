"""The firmware pin map view (roadmap #75, server/ui/firmware_pins.html): its ui://
resource and link, the self-contained page, the read-only tools it calls (one at a time,
through one guard), the lessons of the earlier views it keeps, its data contract against
get_firmware_pin_map on Altium's public examples, the repo's fixtures and synthetic
processors (QFP-like numbering, ball names, odd numbering, many alternate functions,
several candidates, several pages), paging as the view pages, the package diagram's layout
rule (every pin placed once or listed as not on the layout), the outputs as the tool's own
text, and the tool's text results unchanged."""

import csv
import io
import json
import re
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.firmware_pins as fp
import server.services.ui_views as uv
from server.parsers.prj_pcb import VariantDefinition, VariantState
from tests.test_firmware_pins import MCU, PARTS, _netlist
from tests.test_ui_views import _example, _handle, _tools

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
VIEW = uv.view_for("get_firmware_pin_map")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
CALLED = {"get_firmware_pin_map", "query_net", "get_component", "get_component_placement"}


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
    """The page's checkContract: required keys, and the keys that are objects by name."""
    problems = []
    for path, shape in _contract()["shapes"].items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
        for key in shape.get("objects", []):
            wrong = sum(1 for row in _rows_at(data, path) if key in row and not isinstance(row[key], dict))
            if wrong:
                problems.append(f"{path}: {key} not an object in {wrong}")
    return problems


# ---------- the view's reading, in Python ----------

def _view_pages(**args) -> dict:
    """get_firmware_pin_map read as the view reads it: offset 0 with format table, then each
    next_offset with the same mcu and checks; rows and findings joined, every page checked
    against the contract."""
    first = json.loads(main_mod.get_firmware_pin_map(**args, format="table", offset=0))
    assert "error" not in first, first
    pages = [first]
    while pages[-1]["has_more"]:
        pages.append(json.loads(main_mod.get_firmware_pin_map(**args, format="table",
                                                              offset=pages[-1]["next_offset"])))
        assert len(pages) <= _contract()["max_pages"]
    for page in pages:
        assert _check(page) == [], _check(page)
        assert page["variant"] == first["variant"] and page["rows_total"] == first["rows_total"]
    rows = [row for page in pages for row in page.get("rows", [])]
    findings = [finding for page in pages for finding in page["findings"]]
    return {"first": first, "pages": pages, "rows": rows, "findings": findings}


def _part_pins(read: dict) -> dict[str, list[str]]:
    """Each mapped part's pins as the view lists them: its rows, then the power and ground
    pins the tool summarises per net. Each (part, pin) once."""
    first = read["first"]
    single = first["mapped"][0] if len(first["mapped"]) == 1 else None
    out: dict[str, list[str]] = {mcu: [] for mcu in first["mapped"]}
    seen = set()
    for row in read["rows"]:
        mcu = row.get("mcu", single)
        assert (mcu, row["pin"]) not in seen, (mcu, row["pin"])
        seen.add((mcu, row["pin"]))
        out[mcu].append(row["pin"])
    for mcu, groups in (first.get("power_pins") or {}).items():
        for nets in groups.values():
            for pins in nets.values():
                for pin in pins:
                    assert (mcu, pin) not in seen, (mcu, pin)
                    seen.add((mcu, pin))
                    out[mcu].append(pin)
    return out


def _plan(pins: list[str]) -> dict:
    """The page's planLayout, step for step, on the contract's layout block."""
    layout = _contract()["layout"]
    number, ball = re.compile(layout["number"]), re.compile(layout["ball"])
    total = len(pins)

    def enough(count):
        return count >= layout["min_pins"] and count >= layout["min_share"] * total

    balls = [(pin, match.group(1), int(match.group(2))) for pin in pins
             if (match := ball.match(pin.upper()))]
    numbered = [(pin, int(pin)) for pin in pins if number.match(pin)]
    prefix = ""
    if enough(len(balls)):
        rows = sorted({row for _, row, _ in balls}, key=lambda row: (len(row), row))
        if len(rows) >= 2:
            cells, placed = set(), []
            for pin, row, col in balls:
                if (row, col) in cells:
                    continue
                cells.add((row, col))
                placed.append((pin, (row, col)))
            taken = {pin for pin, _ in placed}
            return {"kind": "grid", "rows": rows, "cols": max(col for *_, col in balls),
                    "placed": placed, "unplaced": [pin for pin in pins if pin not in taken]}
        prefix = rows[0]
        numbered = [(pin, col) for pin, _, col in balls]
    if not enough(len(numbered)):
        return {"kind": "list", "placed": [], "unplaced": list(pins)}
    last = max(value for _, value in numbered)
    if len(numbered) < layout["min_fill"] * last:
        return {"kind": "list", "placed": [], "unplaced": list(pins), "gaps": True}
    slots = last - 1 if last % 2 else last
    used, placed = set(), []
    for pin, value in numbered:
        if value > slots or value in used:
            continue
        used.add(value)
        placed.append((pin, value))
    taken = {pin for pin, _ in placed}
    quad = slots % 4 == 0 and slots >= 8
    return {"kind": "quad" if quad and slots >= layout["quad_from"] else "dual", "quad": quad,
            "slots": slots, "prefix": prefix, "placed": placed,
            "unplaced": [pin for pin in pins if pin not in taken]}


def _assert_layout(pins: list[str]) -> dict:
    """Every pin placed once, each place taken once, or listed as not on the layout."""
    plan = _plan(pins)
    placed = [pin for pin, _ in plan["placed"]]
    places = [where for _, where in plan["placed"]]
    assert len(placed) == len(set(placed)) and len(places) == len(set(places))
    assert sorted(placed + plan["unplaced"]) == sorted(pins)
    assert not set(placed) & set(plan["unplaced"])
    if plan["kind"] in ("quad", "dual"):
        assert all(1 <= where <= plan["slots"] for where in places) and plan["slots"] % 2 == 0
        if plan["kind"] == "quad":
            assert plan["slots"] % 4 == 0
    if plan["kind"] == "grid":
        assert all(row in plan["rows"] and 1 <= col <= plan["cols"] for row, col in places)
    return plan


# ---------- synthetic processors ----------

def _load(monkeypatch, tmp_path: Path, parts, variants=("Default",)):
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(parts))
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name=name) for name in variants]))


PORTS = "ABCDEFGH"


def _qfp(ref="U1", count=64, description="ARM Cortex-M4 microcontroller", alternates=False):
    """A part numbered 1..count: supply pins every 16th pin, the rest GPIOs on nets."""
    pins = {}
    for number in range(1, count + 1):
        if number % 16 == 0:
            pins[str(number)] = ("VSS" if number % 32 == 0 else "VDD", "GND" if number % 32 == 0 else "3V3", "Power")
            continue
        port, bit = PORTS[(number // 16) % len(PORTS)], number % 16
        name = f"P{port}{bit}"
        if alternates:
            name += "/USART1_TX/TIM1_CH2/I2C3_SMBA/OTG_FS_VBUS/DCMI_D0/SPI2_MOSI"
        net = None if number % 7 == 0 else f"{ref}_SIG{number}"
        pins[str(number)] = (name, net, "IO")
    return (ref, pins, {"description": description})


def _bga(ref="U1", rows="ABCDEFGHJK", cols=10, skip=("E5", "E6", "F5", "F6")):
    """A ball grid named A1..K10 (no I), a few centre balls depopulated."""
    pins = {}
    for row in rows:
        for col in range(1, cols + 1):
            ball = f"{row}{col}"
            if ball in skip:
                continue
            if (col + rows.index(row)) % 9 == 0:
                pins[ball] = ("VCC", "3V3", "Power")
            elif (col * 3 + rows.index(row)) % 11 == 0:
                pins[ball] = ("GND", "GND", "Power")
            else:
                pins[ball] = (f"GPIO{rows.index(row) * cols + col}", f"BALL_{ball}" if col % 4 else None, "IO")
    return (ref, pins, {"description": "SoC application processor"})


ODD = ("U1", {"VDD": ("VDD", "3V3", "Power"), "GND": ("GND", "GND", "Power"),
              "X": ("PA0", "LED1", "IO"), "Y": ("PA1/USART1_TX", "UART_TX", "IO"),
              "Z": ("PA2/USART1_RX", "UART_RX", "IO"), "W": ("PA3", "BTN", "IO"),
              "T1": ("PA4/SPI1_NSS", "CS", "IO"), "PAD": ("EP", "GND", "Power")},
       {"description": "ARM Cortex-M0 microcontroller"})

SYNTHETIC = {
    "qfp64": ([_qfp()], {}),
    "qfp48_two_rows_by_default": ([_qfp(count=28)], {}),
    "bga": ([_bga()], {}),
    "odd_numbering": ([ODD], {}),
    "many_alternates": ([_qfp(count=48, alternates=True)], {}),
    "fixture": (PARTS, {}),
    "two_candidates": ([_qfp(), _qfp("U10", 32, "STM32 microcontroller")], {}),
    "two_mapped": ([_qfp(), _qfp("U10", 32, "STM32 microcontroller")], {"mcu": "U1,U10"}),
}


@pytest.mark.parametrize("name", list(SYNTHETIC))
def test_the_contract_paging_and_layout_hold_on_synthetic_processors(monkeypatch, tmp_path, name):
    parts, args = SYNTHETIC[name]
    _load(monkeypatch, tmp_path, parts, ("Full", "Lite"))
    read = _view_pages(**args)
    first = read["first"]
    assert first["mapped"]
    pins = _part_pins(read)
    for mcu, listed in pins.items():
        overview = next(item for item in first["overview"] if item["designator"] == mcu)
        assert len(listed) == overview["pins"], mcu  # every pin of the part, once
        plan = _assert_layout(listed)
        expected = {"qfp64": "quad", "qfp48_two_rows_by_default": "dual", "bga": "grid",
                    "odd_numbering": "list", "many_alternates": "quad"}.get(name)
        if expected:
            assert plan["kind"] == expected, (name, plan["kind"])
    assert len(read["findings"]) == first["total"]
    if name == "two_candidates":
        assert first["candidates_total"] == 2 and len(first["mapped"]) == 1
        # The picker asks again with mcu= the other candidate.
        other = next(item["designator"] for item in first["candidates"] if item["designator"] not in first["mapped"])
        again = _view_pages(mcu=other)
        assert again["first"]["mapped"] == [other]
    if name == "two_mapped":
        assert all("mcu" in row for row in read["rows"]) and set(pins) == {"U1", "U10"}
    if name == "odd_numbering":
        assert _plan(pins["U1"])["unplaced"] == pins["U1"]
    if name == "bga":
        plan = _plan(pins["U1"])
        assert plan["rows"] == list("ABCDEFGHJK") and plan["cols"] == 10 and not plan["unplaced"]


def test_paging_as_the_view_does_gives_every_pin_once(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, [_qfp(count=208, alternates=True)])
    whole = json.loads(main_mod.get_firmware_pin_map())
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 12000)
    read = _view_pages()
    assert len(read["pages"]) > 3 and read["first"]["page_limited_to"] < 100
    keys = [row["pin"] for row in read["rows"]]
    assert len(keys) == len(set(keys)) == read["first"]["rows_total"]
    assert len(_part_pins(read)["U1"]) == 208
    plan = _assert_layout(_part_pins(read)["U1"])
    assert plan["kind"] == "quad" and plan["slots"] == 208 and not plan["unplaced"]
    assert read["findings"] == whole["findings"] or len(read["findings"]) == whole["total"]
    # A later page given as the result: read again from offset 0, the same rows.
    later = json.loads(main_mod.get_firmware_pin_map(offset=read["first"]["next_offset"]))
    assert later["offset"] > 0 and later["rows"][0]["pin"] != read["rows"][0]["pin"]
    assert _view_pages()["rows"] == read["rows"]
    script = _script()
    assert "first = await mapPage(args, 0, first);" in script
    assert "the view read the map again from offset 0" in script


def test_the_outputs_read_as_the_view_reads_them_are_the_tools_own_text(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, [_qfp(count=208)])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    for fmt in _contract()["outputs"]:
        pages = [json.loads(main_mod.get_firmware_pin_map(format=fmt, offset=0))]
        # The view's pageOffset: next pages while has_more and the next offset is a pin.
        while pages[-1]["has_more"] and pages[-1]["next_offset"] < pages[-1]["rows_total"]:
            pages.append(json.loads(main_mod.get_firmware_pin_map(format=fmt, offset=pages[-1]["next_offset"])))
        assert all(isinstance(page[fmt], str) for page in pages)
        if fmt == "csv":
            assert len(pages) > 1
            pins = [row["pin"] for page in pages for row in csv.DictReader(io.StringIO(page["csv"]))]
            assert len(pins) == len(set(pins)) == pages[0]["rows_total"] == 208
    script = _script()
    # The text goes into a read-only text area as it is: no other assignment, no reformatting.
    assert script.count("area.value = page[format];") == 2
    assert script.count(".value =") == 2
    assert 'readonly: true, rows: "16", wrap: "off"' in _function("outputBlock")
    assert ("const textNext = (page) => (page.has_more && page.next_offset !== undefined && page.next_offset < page.rows_total"
            " ? page.next_offset : null);") in script
    assert "pageOffset: textNext," in _function("askOutput")
    assert set(_contract()["outputs"]) == set(fp.FORMATS) - {"table"}
    # Asked with the map's mcu and checks; a call at a time; no download, no clipboard.
    assert "const args = { ...view.data.args };" in _function("askOutput")
    assert 'call("get_firmware_pin_map", { ...args, format, offset })' in _function("textPage")
    for word in ("clipboard", "execCommand", "download", "Blob", "createObjectURL", "href"):
        assert word not in script, word


# ---------- resource and link ----------

def test_the_view_is_a_listed_resource_linked_from_its_tool():
    assert VIEW.uri == "ui://altium-copilot-kc/firmware-pin-map.html" and VIEW.file == "firmware_pins.html"
    assert VIEW.name == "firmware-pin-map" and VIEW.title == "Firmware pin map"
    assert VIEW.description.startswith(
        "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
        "get_firmware_pin_map's result; not for reading as text.")
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    row = listed[VIEW.uri]
    assert row["mimeType"] == uv.MIME_TYPE and row["_meta"] == uv.RESOURCE_META
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=VIEW.uri)))
    (content,) = result["contents"]
    assert content["text"] == uv.render(VIEW)
    tools = _tools()
    meta = tools["get_firmware_pin_map"]["_meta"]
    assert meta == {"anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
                    "ui": {"resourceUri": VIEW.uri}, "ui/resourceUri": VIEW.uri}
    # One tool per view; package_for_xfn (it writes files) and the placement link none,
    # and no app-only tool was added.
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)
    for name in ("package_for_xfn", "get_component_placement"):
        assert "ui" not in (tools[name].get("_meta") or {}), name
    assert uv.APP_ONLY_TOOLS == ("get_board_view_geometry",)
    assert "The firmware pin map (#75) is linked from get_firmware_pin_map only." in uv.__doc__


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
    for name in CALLED:
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    assert script.count("Shell.callTool(") == 1 and "Shell.toolCache(" not in script
    guard = _function("call")
    assert "Shell.callTool(" in guard
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in guard
    assert "if (view.closed) return Promise.reject(" in guard and "if (!canCall()) return Promise.reject(" in guard
    # One call at a time: each waits for the one before it.
    assert "const run = queue.then(() => {" in guard and "queue = run.catch(() => {});" in guard
    assert "if (!READ_ONLY_TOOLS.has(tool)) return;" in _function("askDetail")
    assert set(re.findall(r'\bcall\("([a-z_]+)"', script)) == {"get_firmware_pin_map"}
    assert set(re.findall(r'askDetail\("([a-z_]+)"', script)) == {"get_component_placement"}
    # No tool that changes the server's state is named, and no message is sent.
    page = _page()
    for name in ("refresh_netlist", "set_active_variant", "set_project_dir", "package_for_xfn"):
        assert name not in page, name
    for method in ("Shell.sendMessage(", "ui/message", "ui/open-link", "ui/update-model-context"):
        assert method not in page, method


# ---------- the lessons of the earlier views ----------

def test_the_page_keeps_the_lessons_of_the_earlier_views():
    script = _script()
    # Every page read, the real stop reason named, the closed view said so.
    load = _function("loadFrom")
    rest = _function("readRest")
    assert "await readRest(state, gen, Shell.pageBudget(MAX_PAGES, 1));" in load
    assert "Shell.readPages({" in rest and "closed: () => view.closed || gen !== view.gen" in rest
    assert 'state.stop = !stopped ? "" : view.closed ? "the view was closed" :' in rest
    assert "this result starts at offset" in load
    assert _contract()["max_pages"] == 60
    assert "Showing ${read} of ${plural(data.rowsTotal, \"signal pin\")}" in _function("renderNotices")
    assert "The view was closed: it sends no more requests" in script
    # A text format is asked again as the table where the host lets the view call tools,
    # else its text is shown as the tool wrote it, with the reason.
    assert "first = await mapPage(args, 0, null);" in load
    assert "drawTextOnly(first, textKey, args, view.closed ?" in load
    assert "area.value = page[format];" in _function("drawTextOnly")
    # A page of another map is not merged; a result marked isError is an error.
    assert "it is not a page of this map" in _function("mapPage")
    assert "if (result && result.isError)" in _function("loadResult")
    # Every answer labelled with the arguments it was asked under.
    assert "Answer: " in _function("renderHeader") and "argText(" in _function("renderHeader")
    assert "label = argText({ ...entry.args, format })" in _function("outputBlock")
    assert "`${tool}(${Object.entries(args)" in _function("answerBlock")
    # A failed detail is never kept as the answer (Retry asks again).
    detail = _function("askDetail")
    assert "if ((entry && !entry.error) || view.closed || !canCall())" in detail
    # Keyboard focus kept on redraw and after a button that disappears; busy buttons are
    # aria-disabled (they keep focus), never disabled.
    assert "Shell.keepFocus(" in script and "Shell.refocus(" in script and "Shell.focusMark(" in script
    assert '"aria-disabled": view.busy ? "true" : null' in _function("renderCandidates")
    # One live region (the shell's); the tables announce nothing themselves.
    assert 'role: "status"' not in script
    assert re.findall(r'"aria-live"[^)]*', script) == ['"aria-live", "off"']
    assert script.count("quiet(Shell.table(") == script.count("Shell.table(") == 2
    assert "if (sig === view.noticeSig) return;" in _function("renderNotices")
    # Plurals; the host's height (full screen measures its own); short and narrow hosts.
    assert "const plural = (count, word)" in script
    tall = _function("tall")
    assert "const limit = hostHeight();" in tall
    height = _function("hostHeight")
    assert "dims.height || dims.maxHeight" in height
    assert 'return limit || (Shell.context().displayMode === "fullscreen" ? window.innerHeight : 0);' in height
    assert "const short = () => !Shell.roomy() || !tall(800);" in script
    assert "const compactHost = () => window.innerWidth <= 980 && !tall(1100);" in script
    assert '.split.solo > .content { display: none; }' in _page()
    assert 'ui.split.classList.toggle("solo", shown && compactHost());' in _function("drawPane")
    # DOM from text only.
    assert "textContent = " in script and "innerHTML" not in script
    # The page reads no host-specific field.
    assert "toolInfo" not in script and "userAgent" not in script


def test_the_page_draws_every_state():
    script = _script()
    titles = re.search(r"const ERROR_TITLES = \{(.*?)\};", script, re.S).group(1)
    codes = set(re.findall(r"^\s+(\w+):", titles, re.M))
    assert codes == {"no_project", "netlist_required", "no_components_loaded", "mcu_not_found",
                     "invalid_format", "invalid_checks"}
    sources = (ROOT / "server" / "main.py").read_text(encoding="utf-8") + Path(fp.__file__).read_text(encoding="utf-8")
    for code in codes:
        assert f'"{code}"' in sources, code
    for words in ("No processor found", "Show the candidates", "was cancelled", "The view was closed",
                  "Waiting for the tool's arguments", "This view needs a host that shows MCP Apps",
                  "This host does not let the view call tools", "No layout: the pins are not numbered",
                  "No layout: the pin numbers leave too many gaps"):
        assert words in script, words


def test_the_tool_errors_are_states(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_firmware_pin_map())["error"] == "no_project"
    _load(monkeypatch, tmp_path, PARTS)
    assert json.loads(main_mod.get_firmware_pin_map(mcu="U99"))["error"] == "mcu_not_found"
    none = json.loads(main_mod.get_firmware_pin_map())
    assert none["mapped"] == ["U1"]
    _load(monkeypatch, tmp_path, [part for part in PARTS if part[0] != "U1"])
    empty = json.loads(main_mod.get_firmware_pin_map())
    assert empty["mapped"] == [] and _check(empty) == [] and "rows" not in empty
    assert empty["notes"][0].startswith("No processor found")


# ---------- the data contract ----------

def test_the_contract_names_keys_the_tool_produces():
    contract = _contract()
    assert contract["tool"] == VIEW.tool and contract["checks"] == list(fp.CHECKS)
    sources = (Path(fp.__file__).read_text(encoding="utf-8")
               + (ROOT / "server" / "services" / "pull_resistors.py").read_text(encoding="utf-8")
               + (ROOT / "server" / "main.py").read_text(encoding="utf-8"))
    for path, shape in contract["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert f'"{key}"' in sources or f"{key}=" in sources, f"{path}: {key}"
    assert set(contract["check_titles"]) == set(fp.CHECKS)


def test_the_view_reads_the_keys_its_contract_names():
    script = _script()
    keys = {key for shape in _contract()["shapes"].values() for key in shape["required"] + shape["optional"]}
    # Keys the page leaves to the model (the text) or reads through the shell.
    text_only = {"netlist_updated_utc", "page_limited_to", "io_typed_pins", "port_parsed", "functions",
                 "on", "to_signal"}
    # The text formats are read by their key from the contract's outputs.
    outputs = set(_contract()["outputs"])
    assert "const OUTPUT_KEYS = Object.keys(OUTPUTS);" in script
    assert 'OUTPUT_KEYS.find((key) => typeof first[key] === "string")' in script
    for key in sorted(keys - text_only - outputs):
        assert re.search(rf'\.{key}\b|"{key}"', script), key


def test_the_function_table_covers_every_function_the_tool_gives():
    contract = _contract()
    source = Path(fp.__file__).read_text(encoding="utf-8")
    produced = {family.name for family in fp._FAMILIES}
    classify = source.split("\ndef classify(", 1)[1].split("\ndef ", 1)[0]
    produced |= set(re.findall(r'return "([a-z_]+)", ', classify))
    produced |= set(re.findall(r'row\.function(?:, row\.\w+)? = "([a-z_]+)"', source))
    produced |= {"power", "ground", "gpio", "unknown", "unconnected", "tied", "nc"}
    assert produced <= set(contract["functions"]), produced - set(contract["functions"])
    families = contract["families"]
    page = _page()
    codes = [spec[1] for spec in contract["functions"].values()]
    assert len(codes) == len(set(codes))  # each function its own code (no colour alone)
    for function, (family, code, label) in contract["functions"].items():
        assert family in families and code and label, function
    for family in families:
        assert f".fam-{family}" in page, family
    # Eight categorical hues in a fixed order, light and dark (the reference palette).
    for slot in range(1, 9):
        assert page.count(f"--f{slot}: #") == 3, slot


def test_the_contract_holds_on_the_repo_fixture(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, PARTS, ("Full", "Lite"))
    read = _view_pages()
    assert read["first"]["mapped"] == ["U1"] and read["findings"]
    pins = _part_pins(read)["U1"]
    assert sorted(pins, key=int) == sorted(MCU[1], key=int)
    plan = _assert_layout(pins)
    assert plan["kind"] == "dual" and plan["slots"] == 22
    seen = {row["function"] for row in read["rows"]}
    assert seen <= set(_contract()["functions"])
    for fmt in _contract()["outputs"]:
        page = json.loads(main_mod.get_firmware_pin_map(format=fmt))
        assert _check(page) == [] and isinstance(page[fmt], str)
    # A narrowed call: the view infers checks= only when some check did not run.
    narrowed = json.loads(main_mod.get_firmware_pin_map(checks=["uart_crossing"]))
    assert narrowed["checks_run"] == ["uart_crossing"] and _check(narrowed) == []
    infer = _function("inferredArgs")
    assert "args.mcu = first.mapped.join(\",\")" in infer
    assert "...Object.keys(first.not_checked || {})" in infer


def test_a_finding_links_its_pins(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, PARTS, ("Full", "Lite"))
    read = _view_pages()
    rows = {row["pin"] for row in read["rows"]}
    linked = 0
    for finding in read["findings"]:
        pins = ([finding["pin"]] if "pin" in finding else []) + finding.get("pins", []) + finding.get("debug_pins", [])
        assert set(pins) <= rows | {pin for nets in read["first"]["power_pins"]["U1"].values() for group in nets.values() for pin in group}
        linked += bool(pins)
    assert linked
    pins = _function("findingPins")
    assert "finding.pin" in pins and "finding.pins" in pins and "finding.debug_pins" in pins


PUBLIC = {
    "WiFi": (("Mini PC", "Mini PC - WiFi", "WiFi_miniPCIe.PrjPcb"), {"U2": ("quad", 64, []), "U1": ("dual", 30, [])}),
    "Sentinel": (("Bluetooth Sentinel", "Bluetooth_Sentinel.PrjPcb"), {"U1": ("quad", 32, ["33"])}),
    "SpiritLevel": (("SpiritLevel-SL1", "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"), {"U1": ("quad", 208, [])}),
    "MiniPC": (("Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb"), {"U1": ("grid", None, [])}),
}


@pytest.mark.parametrize("name", list(PUBLIC))
def test_the_contract_paging_and_layout_hold_on_the_public_examples(monkeypatch, name):
    parts, expected = PUBLIC[name]
    _example(monkeypatch, *parts)
    for mcu, (kind, slots, unplaced) in expected.items():
        read = _view_pages(mcu=mcu)
        first = read["first"]
        assert first["mapped"] == [mcu]
        assert {row["function"] for row in read["rows"]} <= set(_contract()["functions"])
        pins = _part_pins(read)[mcu]
        overview = next(item for item in first["overview"] if item["designator"] == mcu)
        assert len(pins) == overview["pins"]
        plan = _assert_layout(pins)
        assert plan["kind"] == kind and plan["unplaced"] == unplaced, (mcu, plan["kind"], plan["unplaced"][:5])
        if slots:
            assert plan["slots"] == slots
        if kind == "grid":
            assert len(plan["placed"]) == overview["pins"] == 1517 and len(read["pages"]) > 1
    if name == "WiFi":
        default = _view_pages()
        assert default["first"]["candidates_total"] == 2 and default["first"]["mapped"] == ["U2"]


# ---------- the package diagram ----------

def test_the_page_runs_the_tested_layout_rule():
    plan = _function("planLayout")
    for line in ('const enough = (count) => count >= LAYOUT.min_pins && count >= LAYOUT.min_share * total;',
                 "if (rows.length >= 2) {",
                 "if (cells.has(cell)) continue;",
                 "numbered = balls.map((ball) => ({ pin: ball.pin, number: ball.col }));",
                 'if (numbered.length < LAYOUT.min_fill * last) return { kind: "list", placed: [], unplaced: [...pins], gaps: true };',
                 "const slots = last % 2 ? last - 1 : last;",
                 "if (item.number > slots || used.has(item.number)) continue;",
                 "const quad = slots % 4 === 0 && slots >= 8;",
                 'return { kind: quad && slots >= LAYOUT.quad_from ? "quad" : "dual", quad, slots, prefix, placed,',
                 "unplaced: pins.filter((pin) => !taken.has(pin)) };"):
        assert line in plan, line
    assert "BALL.exec(String(pin).toUpperCase())" in plan and "NUMBER.test(pin)" in plan
    # Every placed pin is drawn once (a grid ball, a slot 1..N), a number with no pin is an
    # empty slot, and the pins left out are listed.
    geometry = _function("geometry")
    assert "for (const ball of plan.placed) {" in geometry and "for (let slot = 1; slot <= plan.slots; slot += 1) {" in geometry
    assert "unplacedList(unplaced, unplacedWhy, items)" in _function("packageTab")


def test_the_diagram_says_what_it_shows():
    note = _function("layoutNote")
    assert note.count("not the footprint") == 3
    package = _function("packageTab")
    assert "Pad centres from ${label}: as placed on the board, seen from the top" in package
    assert "Pad shapes and sizes are not drawn" in package
    assert 'const placementArgs = { refdes: view.part, units: "mil" };' in package
    assert 'if (id === "pads") askDetail("get_component_placement", placementArgs);' in package
    pads = _function("padGeometry")
    assert "unplaced: [...items.keys()].filter((pin) => !onBoard.has(pin))" in pads
    # Colour is never alone: a code in each pin (and the legend), a dot for findings, and
    # the label, title and aria-label of every pin say its function.
    draw = _function("drawDiagram")
    assert 'item ? fnInfo(item.function).code : ""' in draw and '"aria-label": markLabel(mark)' in draw
    assert 'svg("title", { text: markLabel(mark) })' in draw
    assert "a dot on a pin: it has a finding" in _function("legend")
    # Keyboard: one pin in the tab order, arrows move to the nearest pin that way.
    keys = _function("onMarkKey")
    assert "ArrowLeft: [-1, 0]" in keys and '(event.key === "Enter" || event.key === " ")' in keys
    assert 'mark.el.setAttribute("tabindex", mark === target ? "0" : "-1")' in _function("rove")


def test_the_layout_rule_on_edge_cases():
    assert _plan([str(n) for n in range(1, 34)])["unplaced"] == ["33"]  # QFN32 and its pad
    assert _plan([f"P{n}" for n in range(1, 41)])["kind"] == "quad"  # one letter: numbers
    assert _plan([str(n) for n in range(1, 21)])["kind"] == "dual"
    gaps = _plan(["1", "2", "3", "40", "80"])
    assert gaps["kind"] == "list" and gaps.get("gaps")
    twice = _plan(["A1", "a1", "B1", "B2", "A2"])
    assert twice["kind"] == "grid" and twice["unplaced"] == ["a1"]
    assert _plan(["VDD", "GND", "1", "2"])["kind"] == "list"  # numbers name half the pins
    mixed = _plan([str(n) for n in range(1, 65)] + ["EP"])
    assert mixed["kind"] == "quad" and mixed["unplaced"] == ["EP"]


# ---------- findings, candidates, reading on, honest layouts ----------

VARIANTS = [VariantDefinition(name="Full"),
            VariantDefinition(name="Lite", dnp_refdes=["R3", "U3", "R4"]),
            VariantDefinition(name="Cost", param_variations={"R2": {"value": ("Value", "1k")}})]


def test_a_variant_finding_names_its_parts_and_values_as_objects(monkeypatch, tmp_path):
    """variant_changes findings carry parts ({part: {fitted_in, not_fitted_in}}) and values
    ({part: {variant: value}}) as objects: the contract says so (checked on every page), and
    the details pane draws them per part rather than joining them as lists."""
    _load(monkeypatch, tmp_path, PARTS)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(VARIANTS))
    read = _view_pages()
    found = [finding for finding in read["findings"] if finding["check"] == "variant_changes"]
    assert any("parts" in finding for finding in found) and any("values" in finding for finding in found)
    for finding in found:
        for key in ("parts", "values"):
            if key in finding:
                assert isinstance(finding[key], dict) and all(isinstance(item, dict) for item in finding[key].values())
    parts = next(finding["parts"] for finding in found if "parts" in finding)
    assert all(set(info) == {"fitted_in", "not_fitted_in"} for info in parts.values())
    values = next(finding["values"] for finding in found if "values" in finding)
    assert None in {value for by_variant in values.values() for value in by_variant.values()}  # as drawn
    assert _contract()["shapes"]["findings[]"]["objects"] == ["parts", "values"]
    assert all(_check(page) == [] for page in read["pages"])
    listed = {**read["first"], "findings": [{**found[0], "parts": ["R3"]}]}
    assert _check(listed) == ["findings[]: parts not an object in 1"]
    assert "for (const key of shape.objects || [])" in _function("checkContract")
    pane = _function("findingPane")
    assert '["Parts", perPart(finding.parts, fittedText)]' in pane
    assert '["Values", perPart(finding.values, valuesText)]' in pane
    script = _script()
    assert ".parts || []).join" not in script and ".values || []).join" not in script
    assert "fitted in ${listOf(info.fitted_in)" in script and '"as drawn"' in script


def test_the_picker_offers_the_ranked_part_when_mcu_named_another(monkeypatch):
    _example(monkeypatch, "Bluetooth Sentinel", "Bluetooth_Sentinel.PrjPcb")
    first = _view_pages(mcu="U2")["first"]
    ranked = [str(candidate["designator"]) for candidate in first["candidates"]]
    assert first["mapped"] == ["U2"] and ranked == ["U1"]
    # The page's rule: several ranked parts, or one the map does not show.
    assert len(ranked) > 1 or any(name not in first["mapped"] for name in ranked)
    cands = _function("renderCandidates")
    assert "const unmapped = list.some((candidate) => !data.mapped.includes(String(candidate.designator)));" in cands
    assert "if (list.length <= 1 && !unmapped) return;" in cands
    assert "mapped because mcu= named" in cands


def test_a_page_of_another_netlist_is_not_merged(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, [_qfp(count=208)])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    monkeypatch.setattr(main_mod, "_netlist_last_updated", "2026-10-28T08:00:00Z")
    first = json.loads(main_mod.get_firmware_pin_map(format="table", offset=0))
    assert first["has_more"] and first["netlist_updated_utc"] == "2026-10-28T08:00:00Z"
    monkeypatch.setattr(main_mod, "_netlist_last_updated", "2026-10-28T08:05:00Z")  # read again
    later = json.loads(main_mod.get_firmware_pin_map(format="table", offset=first["next_offset"]))
    # The same counts from another netlist: only the stamp tells the pages apart.
    same = ("rows_total", "variant", "total", "mapped")
    assert [later[key] for key in same] == [first[key] for key in same]
    assert later["netlist_updated_utc"] != first["netlist_updated_utc"]
    for name in ("mapPage", "textPage"):
        assert "if (expect && stamp(page) !== stamp(expect)) throw changedError(page, expect);" in _function(name)
    assert "error.changed = true;" in _function("changedError")
    assert "if (failed && failed.changed) state.again = true;" in _function("readRest")
    # Another netlist is another map: its outputs and selection do not carry over.
    assert "previous.netlist === view.data.netlist" in _function("finish")


def test_a_stopped_read_reads_on_from_where_it_stopped(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, [_qfp(count=208, alternates=True)])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 12000)
    whole = _view_pages()
    # Stopped after two pages (a failed page, the page budget), then read on from the last
    # page's next_offset: the rows of an uninterrupted read, each once.
    pages = whole["pages"][:2]
    while pages[-1]["has_more"]:
        pages.append(json.loads(main_mod.get_firmware_pin_map(format="table", offset=pages[-1]["next_offset"])))
    assert [row for page in pages for row in page.get("rows", [])] == whole["rows"]
    resume = _function("resumeRead")
    assert "const next = mapNext(data.read.last);" in resume
    assert 'reask({}, "the map again from offset 0");' in resume  # another netlist, a later page
    assert "await readRest(state, gen, Shell.pageBudget(MAX_PAGES));" in resume
    notices = _function("renderNotices")
    assert '(fresh ? "Read again" : "Read the rest")' in notices and "resumeRead();" in notices
    # Pages are numbered from offset 0 in the stop reason, also after reading on.
    assert "stopped.replace(/^page \\d+ of/, `page ${read + 1} of`)" in _function("readRest")
    # A failed placement offers Retry.
    assert 'text: "Retry", title: label' in _function("packageTab")


def test_a_text_result_keeps_its_first_page_for_the_outputs(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, [_qfp(count=208)])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    given = json.loads(main_mod.get_firmware_pin_map(format="csv"))
    assert given["has_more"] and given["next_offset"] < given["rows_total"]
    # Read on from the call's own page: the pages a fresh read gives after its first.
    fresh = [json.loads(main_mod.get_firmware_pin_map(format="csv", offset=0))]
    while fresh[-1]["has_more"] and fresh[-1]["next_offset"] < fresh[-1]["rows_total"]:
        fresh.append(json.loads(main_mod.get_firmware_pin_map(format="csv", offset=fresh[-1]["next_offset"])))
    assert fresh[0]["csv"] == given["csv"] and len(fresh) > 2
    keep = _function("keepTextResult")
    assert "const kept = !page.offset && stamp(page) === stamp(state.first) && page.variant === state.first.variant;" in keep
    assert 'view.outputs.set(format, { pages: [page], args: state.args, stopped: "", next, fromResult: true });' in keep
    assert "view.outputFormat = format;" in keep  # the Outputs tab opens on the call's own text
    assert "reads the other pages when asked" in keep and "asks the tool for the ${label} text again" in keep
    ask = _function("askOutput")
    assert "const pages = onFrom ? [...entry.pages] : [];" in ask
    assert "first: pages[pages.length - 1]" in ask
    assert '"Read the other pages"' in _function("outputBlock")
    # A button that can do nothing says so (no tool calls, the view closed).
    assert '"aria-disabled": (view.busy || !canAsk) && !view.outputs.has(format) ? "true" : null' in _function("outputsTab")


def test_a_short_read_draws_no_layout_by_number(monkeypatch, tmp_path):
    """Sides worked out from the pins read would put them on the wrong side when the read
    stopped (a host without tool calls reads one page): no layout by number then."""
    io_only = ("U1", {str(n): (f"P{PORTS[n // 16 % 8]}{n % 16}", f"N{n}", "IO") for n in range(1, 401)},
               {"description": "ARM Cortex-M7 microcontroller"})
    _load(monkeypatch, tmp_path, [io_only])
    first = json.loads(main_mod.get_firmware_pin_map())
    assert first["has_more"]
    pins = _part_pins({"first": first, "rows": first["rows"]})["U1"]
    overview = first["overview"][0]["pins"]
    assert len(pins) < overview == 400
    plan = _plan(pins)
    assert plan["kind"] == "quad" and plan["slots"] == len(pins) < 400  # 1..100 drawn as a whole part
    package = _function("packageTab")
    assert ("const partial = Boolean(data.stopReason) && (expected ? pins.length < expected : "
            "data.rows.length < data.rowsTotal);") in package
    assert 'partial ? { kind: "list", placed: [], unplaced: pins.map((row) => row.pin), partial: true }' in package
    assert "a layout by number needs every pin" in package


def test_the_grid_names_its_first_and_last_column():
    geometry = _function("geometry")
    assert "if (col === 1 || col === plan.cols || col % step === 0)" in geometry
    for cols in (10, 23, 39):
        step = 5 if cols > 30 else 2 if cols > 15 else 1
        labels = [col for col in range(1, cols + 1) if col in (1, cols) or col % step == 0]
        assert labels[0] == 1 and labels[-1] == cols


def test_the_page_reads_a_result_once_and_the_latest_read_wins():
    load = _function("loadResult")
    assert "if (text && text === view.resultSig) return;" in load  # also while it is read
    reading = _function("loadFrom")
    assert "async function loadFrom(first, asked, origin, gen = ++view.gen) {" in reading
    assert reading.count("if (!current()) return;") >= 3
    reask = _function("reask")
    assert "const gen = ++view.gen;" in reask and 'await loadFrom(first, args, "view", gen);' in reask


def test_the_page_looks_up_result_strings_by_own_keys_only():
    script = _script()
    assert not re.search(r"\b(FUNCTIONS|CHECK_TITLES|SEVERITY_RANK|ERROR_TITLES)\[", script)
    assert "Object.prototype.hasOwnProperty.call(table, key)" in script
    for use in ("own(FUNCTIONS, fn)", "own(CHECK_TITLES, check)", "own(SEVERITY_RANK, severity)",
                "own(ERROR_TITLES, payload.error)", "listOf(own(view.data.sections.boot_straps, row.mcu))"):
        assert use in script, use
    # A text-only result appends no null: Shell.append leaves it out.
    text = _function("drawTextOnly")
    assert "Shell.append(clear(app), [" in text and "clear(app).append(" not in text


def test_focus_columns_and_sort_survive_a_redraw():
    script = _script()
    # A pin's row, a finding's row and a pin on the diagram are found again after a redraw
    # (another tab, a layout change, an answer arriving).
    assert "{ key: \"pin\", label: \"Pin\", cls: \"c-pin\", render: (row) => el(\"span\", { data: { key: row._key } }" in script
    assert "el(\"span\", { data: { fid: String(finding._id) } }, sevBadge(finding.severity))" in script
    tab = _function("renderTab")
    assert "const spot = ui.panel.contains(document.activeElement) ? focusSpot() : null;" in tab
    assert "if (!restoreSpot(spot) && mark) {" in tab
    layout = _function("onLayoutChange")
    assert "if (!restoreSpot(spot) && mark) Shell.refocus(app, mark, () => ui.headline || null);" in layout
    # The column chooser and the sort are kept for both tables.
    assert 'keepTable("pins");' in tab and 'keepTable("findings");' in tab
    assert 'const spec = tableSpec("pins", pinColumns(), { key: "pin", dir: 1 });' in _function("pinsTab")
    assert 'tableSpec("findings", [' in _function("findingsTab")
    keep = _function("keepTable")
    assert "if (shown && column.hidden) kept.added.add(column.key);" in keep
    assert "if (!shown && !column.hidden) kept.removed.add(column.key);" in keep
    # Full screen with no height from the host: the window's own height.
    fit = _function("fitOwnHeight")
    assert 'app.style.setProperty("--pane-height", `${px}px`)' in fit and "window.innerHeight" in fit


def test_the_diagram_says_selection_fading_and_severity_in_words():
    marks = _function("applyMarks")
    assert 'mark.el.setAttribute("aria-pressed", selected ? "true" : "false");' in marks
    assert 'mark.el.setAttribute("aria-label", label);' in marks
    label = _function("markLabel")
    assert "findingWords(findings)" in label and "faded: the Pins tab's filters hide it" in label
    words = _function("findingWords")
    assert 'plural(warnings, "warning")' in words and "info" in words
    package = _function("packageTab")
    # Faded pins are said on the Package tab, with a way back.
    assert '"Show every pin"' in package and "on: { click: clearFilters }" in package
    # What only a drawn diagram uses (zoom, its keys) is left out without one.
    assert 'geo ? el("button", { type: "button", class: "btn", text: "−"' in package
    assert 'geo ? el("p", { class: "foot"' in package
    # The board's pads: pin numbers on request, and pin 1 marked.
    assert '{ id: "number", label: "Pin number" }' in package
    assert 'svg("circle", { class: "pin1", cx: geo.pin1.x' in _function("drawDiagram")
    assert 'marks.find((mark) => String(mark.pad.pin) === "1")' in _function("padGeometry")


# ---------- tool results unchanged ----------

def test_the_tools_text_results_are_unchanged(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, PARTS, ("Full", "Lite"))
    calls = [{}, {"mcu": "U1"}, {"format": "csv"}, {"format": "c_header"}, {"format": "devicetree"},
             {"checks": ["uart_crossing"]}, {"offset": 5}, {"mcu": "U99"}]
    for args in calls:
        direct = main_mod.get_firmware_pin_map(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name="get_firmware_pin_map", arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], args
        assert result["structuredContent"] == {"result": direct}, args
    tools = _tools()
    assert tools["get_firmware_pin_map"]["outputSchema"]["properties"] == {
        "result": {"title": "Result", "type": "string"}}


# ---------- docs ----------

def test_the_readme_and_changelog_name_the_view():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `get_firmware_pin_map` |" in section
    row = next(line for line in readme.split("## Interactive views", 1)[0].splitlines()
               if line.endswith("| `get_firmware_pin_map` |"))
    assert "(see [Interactive views](#interactive-views))" in row
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #75" in unreleased and "firmware_pins.html" in unreleased
