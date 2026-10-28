"""The board view (roadmap #63, server/ui/board.html): its ui:// resource and link, the
self-contained page, the tools it calls (through one guarded helper, all read-only), the
paging, drawing and wheel rules it keeps, and its data contract against get_board_view's
and get_board_view_geometry's output on synthetic boards and Altium's public examples."""

import json
import re
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.board_view as bv
import server.services.ui_views as uv
from server.parsers.prj_pcb import VariantDefinition
from server.services.pcb_index import PcbIndex
from tests.test_board_view import BOARDS, _handle, _load, _need, _public, _rich, _tools
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
VIEW = uv.view_for("get_board_view")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
# The only tools the page calls, all read-only.
CALLED = {"get_board_view", "get_board_view_geometry", "get_component_placement", "get_component",
          "get_net_pcb", "query_pcb_region"}


def _page() -> str:
    return (UI / VIEW.file).read_text(encoding="utf-8")


def _script() -> str:
    return _page().split(uv.JS_MARKER, 1)[1]


def _contract() -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>', _page(), re.S)
    return json.loads(block.group(1))


def _rows_at(data, path: str) -> list:
    if path in ("result", "first", "later"):
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


def _has(row: dict, key: str) -> bool:
    """A contract key: 'at_*' is at_mil or at_mm (the result's units), as the page reads it."""
    if key.endswith("_*"):
        return f"{key[:-2]}_mil" in row or f"{key[:-2]}_mm" in row
    return key in row


def _check(shapes: dict, data: dict) -> list[str]:
    problems = []
    for path, shape in shapes.items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if not _has(row, key))
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


def _seen(shapes: dict, outputs: list[dict]) -> dict[str, set]:
    """Every contract key some output holds, per path."""
    seen: dict[str, set] = {path: set() for path in shapes}
    for data in outputs:
        for path, shape in shapes.items():
            for row in _rows_at(data, path):
                seen[path] |= {key for key in shape["required"] + shape["optional"] if _has(row, key)}
    return seen


# ---------- resources and links ----------

def test_the_view_is_a_listed_resource_linked_from_get_board_view():
    assert VIEW.uri == "ui://altium-copilot-kc/board.html" and VIEW.file == "board.html"
    assert VIEW.name == "board" and VIEW.title == "Board view"
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    row = listed[VIEW.uri]
    assert row["mimeType"] == uv.MIME_TYPE and row["_meta"] == uv.RESOURCE_META
    assert row["description"].startswith("HTML page (MCP Apps view)")
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=VIEW.uri)))
    (content,) = result["contents"]
    assert content["mimeType"] == uv.MIME_TYPE and content["_meta"] == uv.RESOURCE_META
    assert content["text"] == uv.render(VIEW)
    tools = _tools()
    assert tools["get_board_view"]["_meta"] == {
        "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
        "ui": {"resourceUri": VIEW.uri}, "ui/resourceUri": VIEW.uri}
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)


def test_the_geometry_tool_is_the_one_app_only_tool_and_links_no_view():
    assert uv.APP_ONLY_TOOLS == ("get_board_view_geometry",)
    with pytest.raises(KeyError):
        uv.view_for("get_board_view_geometry")
    assert uv.app_only_meta({"a": 1, "ui": {"x": 1}}) == {"a": 1, "ui": {"visibility": ["app"]}}
    description = " ".join(_tools()["get_board_view_geometry"]["description"].split())
    assert description.startswith("Pages of numbers for the board view.")
    assert "never call it to answer a question" in description
    doc = (ROOT / "server" / "services" / "ui_views.py").read_text(encoding="utf-8")
    assert "The board view (#63) is linked from get_board_view only." in doc
    assert "the first app-only tool" in doc and "APP_ONLY_TOOLS lists the tools pages may call" in doc
    assert "no app-only tool is registered" not in doc


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
                         r"toDataURL|toBlob|drawImage)\b", scripts)
    assert page.count("const Shell = (() => {") == 1
    assert f'<meta http-equiv="Content-Security-Policy" content="{uv.PAGE_CSP}">' in page


# ---------- the read-only rule ----------

def test_the_page_calls_only_its_six_tools_through_one_guard():
    script = _script()
    tools = _tools()
    named = {name for name in re.findall(r'"([a-z_]+)"', script) if name in tools}
    assert named == CALLED
    allowed = re.search(r"const READ_ONLY_TOOLS = new Set\(\[(.*?)\]\);", script, re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', allowed)) == CALLED
    for name in CALLED:
        assert tools[name]["annotations"]["readOnlyHint"] is True
    # One tools/call, in call(): the read-only set, a closed view and serverTools checked.
    assert script.count("Shell.callTool(") == 1
    helper = script.split("function call(tool, args) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in helper
    assert "if (view.closed) return Promise.reject(" in helper
    assert "if (!canCall()) return Promise.reject(" in helper
    # The details cache asks the same tools only, never after teardown or without the tools.
    assert "if (!READ_ONLY_TOOLS.has(tool)) return;\n" in script
    assert "if (!canCall() || view.closed) return;" in script
    assert set(re.findall(r'(?:call|ask)\(\s*"([a-z_]+)"', script)) <= CALLED
    assert set(re.findall(r'calls\.push\(\["([a-z_]+)"', script)) == {
        "get_component_placement", "get_component", "get_net_pcb", "query_pcb_region"}
    assert '"set_active_variant"' not in _page()


def test_the_page_reads_the_plan_within_its_budgets_and_stops_when_closed():
    script = _script()
    contract = _contract()
    assert contract["tool"] == "get_board_view" and contract["geometry_tool"] == "get_board_view_geometry"
    assert (contract["format"], contract["page_chars"], contract["auto_pages"], contract["session_pages"]) == \
        (bv.FORMAT, bv.PAGE_CHARS, bv.AUTO_PAGES, bv.SESSION_PAGES)
    assert contract["kinds"] == {kind: stride or ("strings" if kind == "nets" else "arrays")
                                 for kind, stride in bv.STRIDES.items()}
    assert {int(code) for code in contract["pad_shapes"]} == {1, 2, 3, 9}
    assert script.count("Shell.readPages(") == 1
    assert "closed: () => view.closed || run !== generation" in script
    assert "Shell.pageBudget(AUTO_PAGES, 1)" in script and "Shell.pageBudget(SESSION_PAGES, 1)" in script
    # Pages whose chunks all lie on hidden copper layers are read last.
    assert "return [...pages.filter((entry) => !hidden(entry)), ...pages.filter(hidden)]" in script
    # A stop names its reason; Retry continues from the page that failed; a changed board
    # stops with Reload; teardown sends nothing more.
    assert "Reading the board stopped: ${view.stopReason" in script and '"Retry"' in script
    assert "The board changed since this answer (saved again, or another board loaded)" in script
    assert "The view was closed" in script and "closeCalls()" in script
    assert "Read the remaining ${plural(missing, \"page\")}" in script
    assert "This host does not let the view read the board's geometry (${GEOMETRY_TOOL})" in script


def test_the_wheel_zooms_only_with_ctrl_focus_or_full_screen():
    script = _script()
    wheel = script.split("function onWheel(event) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const zoomKey = event.ctrlKey || event.metaKey;" in wheel
    assert "const focused = document.activeElement === ui.board;" in wheel
    assert 'const fullscreen = Shell.context().displayMode === "fullscreen";' in wheel
    assert "if (!zoomKey && !focused && !fullscreen) {" in wheel and "Ctrl + wheel to zoom" in wheel
    assert wheel.index("event.preventDefault()") > wheel.index("if (!zoomKey && !focused && !fullscreen)")
    assert 'addEventListener("wheel", onWheel, { passive: false })' in script


def test_the_page_draws_on_canvases_and_keeps_the_shell_lessons():
    script = _script()
    # Two canvases (base, overlay), Y up, flip mirrors X; designators drawn unmirrored.
    assert script.count('el("canvas"') == 2
    assert "ctx.setTransform(s * flip, 0, 0, -s," in script
    assert "ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0)" in script
    # A draw waits for an animation frame or a timer (a hidden pane holds frames back), and
    # a frame without a size is measured again on a timer.
    assert "requestAnimationFrame(run);\n    setTimeout(run, 60);" in script
    assert "function retrySize()" in script
    # Keyboard focus survives a redraw and buttons that disappear; text only in the DOM.
    assert "Shell.keepFocus(" in script and "Shell.refocus(" in script
    assert 'role: "application", "aria-roledescription": "board view"' in script
    assert "aria-describedby" in script and "Shell.announce(" in script
    # The server's totals are exact (what a cap left out counts in them): a list cut short
    # reads "N of M", never "at least".
    assert "`${rows.length} of ${total}`" in script and "at least ${" not in script
    assert "prefers-reduced-motion" in script


def test_the_page_fills_overlaps_reads_pages_into_growing_batches_and_hits_real_shapes():
    script = _script()
    # Copper fills are nonzero with holes wound against their outline, so overlapping
    # areas, pours and poured islands stay filled (one even-odd path per layer left holes
    # where two shapes overlapped); the cutouts' union is cut out of the board.
    assert "evenodd" not in script
    assert "area.rings[ring] = wound(pts, ring === 0)" in script and "wound(pts, true)" in script
    # Decoded objects join the batches once (geo.pending); showing a layer rebuilds only
    # the vias' batch, and the net highlight is kept per layer and net.
    assert "geo.batches = new Map()" not in script.replace("if (!geo.batches) geo.batches = new Map()", "")
    assert "geo.viasDirty = true;  // only the vias' batch depends on what is shown" in script
    assert "`${layer}|${net}|${geo.layerVersion.get(layer) || 0}|${toggles}`" in script
    # A click hits a pad's own shape (rotated rectangle, rounded rectangle, octagon, round
    # or obround), the nearest centre first; the click cycle starts again when the view,
    # the side, the layers or the objects under the cursor change, and a double-click's
    # second click does not step it on.
    assert "padContains(geo.stacks[item.pad.stack], x - item.pad.x, y - item.pad.y, tolerance)" in script
    assert "PRIORITY[a.kind] - PRIORITY[b.kind] || centre(a) - centre(b)" in script
    assert "view.cycleAt.where === where" in script and "function onDoubleClick(event)" in script
    # A text part names its own channel instance (schematic designator, else position).
    assert "for (const index of partsOf(target)) parts.add(index);" in script
    # get_component gets a pad's part's schematic designator.
    assert "const sch = owner && geo.sch[String(owner.index)];" in script


def test_the_page_folds_on_short_hosts_and_after_teardown_offers_no_server_buttons():
    script = _script()
    # The Layers panel opens by the room the host gives, again on full screen or a new
    # size, unless the user opened or folded it.
    assert "function layersRoomy()" in script and "view.layersUser === null && ui.layersBox.open !== layersRoomy()" in script
    # A short host: one line for the file and the arguments, the side switch and the legend
    # behind More, the notes and warnings folded; the details take the board's place
    # without its tools; the board may shrink to the shell's smallest pane.
    assert "main.solo-details > .board-tools, main.solo-details > .layers-box, main.solo-details > .tabs" in _page()
    assert "min-height: 64px" in _page() and "min-height: 140px" not in _page()
    # After teardown the details say the view was closed and offer no server button.
    assert "canCall() && !view.closed\n        ? [el(\"div\", { class: \"btn-row\" }" in script
    # The view's own messages replace each other in the notices (no prepend above the page).
    assert "app.prepend(" not in script and 'message("find", "info",' in script


# ---------- the data contract ----------

def _text_outputs(monkeypatch, tmp_path, pcb_data) -> list[dict]:
    from tests import test_decoupling as td
    outputs = []
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    _load(monkeypatch, PcbIndex(_rich(pcb_data), NETLIST), path)
    for args in ({"nets": "CLK_.*, NOPE", "parts": "U3, R1, U3-2, U3-9", "x": 300, "y": 300},
                 {"x": -50, "y": -150, "units": "mm", "layers": "top, nope"},
                 {"findings": [{"check": "spacing", "at_mil": [950, 950], "layer": "Top Layer",
                                "objects": "Pad U3-1 (CLK_A) / Track (GND)"}], "finding_tool": "check_dfm"},
                 {"findings": {"at_mil": [1, 2], "designators": ["R1"]}, "finding_tool": "check_vias"}):
        outputs.append(json.loads(main_mod.get_board_view(**args)))
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    outputs.append(json.loads(main_mod.get_board_view(parts="R1")))  # notes
    netlist = td._netlist(td.BOARD_PARTS)
    _load(monkeypatch, PcbIndex(td._pcb(td._placements()), netlist), path, netlist=netlist)
    far = [f for f in json.loads(main_mod.check_decoupling())["findings"] if f["check"] == "far_capacitor"]
    outputs.append(json.loads(main_mod.get_board_view(findings=far, finding_tool="check_decoupling")))
    return outputs


def test_the_contract_names_keys_get_board_view_produces(monkeypatch, tmp_path, pcb_data):
    contract = _contract()
    outputs = _text_outputs(monkeypatch, tmp_path, pcb_data)
    for data in outputs:
        assert "error" not in data
        assert _check(contract["shapes"], data) == []
    seen = _seen(contract["shapes"], outputs)
    for path, shape in contract["shapes"].items():
        assert seen[path] == set(shape["required"] + shape["optional"]), path


def _pages_of(export) -> list[dict]:
    return [json.loads(export.page(k)) for k in range(export.page_count)]


def test_the_contract_names_keys_the_geometry_pages_hold(pcb_data):
    contract = _contract()
    index = PcbIndex(_rich(pcb_data), NETLIST)
    pages = _pages_of(bv.build_export(index, "rev"))
    shapes = contract["pages"]
    first = {path: shape for path, shape in shapes.items() if path != "later"}
    assert _check(first, pages[0]) == []
    seen = _seen(first, pages)
    for path, shape in first.items():
        assert seen[path] == set(shape["required"] + shape["optional"]), path
    kinds = {chunk["k"] for page in pages for chunk in page["chunks"]}
    assert kinds == set(contract["kinds"]) - {"poured_arcs"} | {"poured_tracks"}


@pytest.mark.parametrize("name", ["SL1", "Sentinel", "WiFi_panel", "MiniPC"])
def test_the_contract_holds_on_the_public_examples(monkeypatch, name):
    _need(name)
    contract = _contract()
    index = _load(monkeypatch, _public(name), BOARDS[name], netlist=None,
                  variants=[VariantDefinition(name="Default")])
    data = json.loads(main_mod.get_board_view(nets="GND", x=100.0, y=100.0))
    assert _check(contract["shapes"], data) == []
    rev = data["view"]["geometry_rev"]
    first = json.loads(main_mod.get_board_view_geometry(rev, 0))
    assert _check({key: contract["pages"][key] for key in ("first", "layers[]", "plan[]", "chunks[]")}, first) == []
    for offset in range(1, min(first["pages"], 4)):
        page = json.loads(main_mod.get_board_view_geometry(rev, offset))
        assert _check({"later": contract["pages"]["later"], "chunks[]": contract["pages"]["chunks[]"]}, page) == []
    assert {chunk["k"] for chunk in first["chunks"]} <= set(contract["kinds"])
    assert index.pcb.board  # loaded through the tools, offline


def test_the_view_reads_every_key_its_contract_names():
    script = _script()
    contract = _contract()
    skipped = set(contract["text_only"])
    keys = {key for shape in contract["shapes"].values() for key in shape["required"] + shape["optional"]}
    keys |= {key for shape in contract["pages"].values() for key in shape["required"] + shape["optional"]}
    for key in sorted(keys - skipped):
        if key.endswith("_*"):
            assert f'"{key[:-2]}"' in script, key  # read through unitKey(row, "<stem>")
        else:
            assert re.search(rf'\.{key}\b|"{key}"', script), key


# ---------- docs and manifest ----------

def test_the_manifest_readme_and_changelog_name_the_board_view():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in manifest["tools"]}
    assert {"get_board_view", "get_board_view_geometry"} <= names
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `get_board_view` |" in section and "get_board_view_geometry" in section
    assert "`get_board_view`" in readme.split("## Interactive views", 1)[0]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #63" in unreleased and "get_board_view_geometry" in unreleased
