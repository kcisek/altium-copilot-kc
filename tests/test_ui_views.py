"""MCP Apps views (services/ui_views.py, server/ui): the ui:// resources, their tools'
links, the self-contained pages, unchanged tool results and the power tree view's data
contract against get_power_tree's output (synthetic boards and Altium's public examples)."""

import asyncio
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.power_tree as pt
import server.services.ui_views as uv
from server.parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_pcb
from tests.test_power_tree import BOARD
from tests.test_pull_resistors import _netlist

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
POWER_TREE = uv.view_for("get_power_tree")
# The only absolute URL a page may hold: the SVG namespace createElementNS takes.
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}


def _handle(request):
    """A request through the low-level MCP server, as a host sends it."""
    handler = main_mod.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


def _served(view=POWER_TREE) -> str:
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=view.uri)))
    (content,) = result["contents"]
    return content["text"]


def _contract() -> dict:
    page = (UI / POWER_TREE.file).read_text(encoding="utf-8")
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>',
                      page, re.S)
    return json.loads(block.group(1))


def _view_script() -> str:
    page = (UI / POWER_TREE.file).read_text(encoding="utf-8")
    return page.split(uv.JS_MARKER, 1)[1]


def _rows_at(data, path: str) -> list:
    """The rows a contract path names, read as the view's rowsAt does."""
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


def _check_contract(data: dict) -> list[str]:
    problems = []
    for path, shape in _contract()["shapes"].items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


def _all_pages(**kwargs) -> dict:
    """get_power_tree read the way the view reads it: the first page, then every
    next_offset, the paged lists joined."""
    first = json.loads(main_mod.get_power_tree(**kwargs))
    data = {**first, **{key: list(first[key]) for key in _contract()["paged"]}}
    offset = first.get("next_offset") if first["has_more"] else None
    while offset is not None:
        page = json.loads(main_mod.get_power_tree(**kwargs, format="json", offset=offset))
        for key in _contract()["paged"]:
            data[key].extend(page[key])
        offset = page.get("next_offset") if page["has_more"] else None
    return data


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(BOARD)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    return netlist


def _example(monkeypatch, *parts: str):
    """Load one of Altium's public examples offline (compiled from its sheets)."""
    path = EXAMPLES.joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"{path.name} is not installed")
    client = main_mod.AltiumClient()
    client.compile_offline(str(path))
    prj = parse_prj_pcb(str(path))
    monkeypatch.setattr(main_mod, "_project", {
        "name": path.stem, "root_dir": str(path.parent), "prj_pcb_path": str(path),
        "sheets": [], "parameters": prj.parameters, "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod, "_altium", client)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(prj.variants))


PUBLIC = [
    ("Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb"),
    ("SpiritLevel-SL1", "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"),
    ("Bluetooth Sentinel", "Bluetooth_Sentinel.PrjPcb"),
    ("Harness Demo MB", "Harness_Demo_PCB_A", "Harness_Demo_PCB_A.PrjPcb"),
]


# ---------- resources and links ----------

def test_every_view_is_a_ui_resource_with_the_apps_mime_type_and_csp():
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    for view in uv.VIEWS:
        row = listed[view.uri]
        assert view.uri.startswith("ui://") and row["mimeType"] == "text/html;profile=mcp-app"
        assert row["name"] == view.name and row["title"] == view.title
        ui = row["_meta"]["ui"]
        assert ui["csp"] == {"connectDomains": [], "resourceDomains": [], "frameDomains": [],
                             "baseUriDomains": []}
        assert "permissions" not in ui and "domain" not in ui and ui["prefersBorder"] is True
        # Text-only hosts list it too: the description says what it is.
        assert row["description"].startswith("HTML page (MCP Apps view)")


def test_reading_a_view_returns_its_page_with_mime_type_and_meta():
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=POWER_TREE.uri)))
    (content,) = result["contents"]
    assert content["uri"] == POWER_TREE.uri and content["mimeType"] == uv.MIME_TYPE
    assert content["_meta"] == uv.RESOURCE_META
    assert content["text"].startswith("<!doctype html>")
    assert content["text"] == uv.render(POWER_TREE)


def test_the_tool_links_its_view_in_current_and_legacy_meta():
    tools = _tools()
    for view in uv.VIEWS:
        meta = tools[view.tool]["_meta"]
        assert meta["ui"] == {"resourceUri": view.uri}  # no visibility: model and app
        assert meta["ui/resourceUri"] == view.uri
    assert tools["get_power_tree"]["_meta"]["anthropic/maxResultSizeChars"] == \
        main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    linked = {view.tool for view in uv.VIEWS}
    assert tools["get_board_view"]["_meta"]["ui"] == {"resourceUri": uv.view_for("get_board_view").uri}
    for name, tool in tools.items():
        if name in uv.APP_ONLY_TOOLS:
            # Pages call it; hosts that know the extension keep it from the model.
            assert tool["_meta"] == {"ui": {"visibility": ["app"]},
                                     "anthropic/maxResultSizeChars": 60000}, name
        elif name not in linked:
            assert set(tool.get("_meta") or {}) <= {"anthropic/maxResultSizeChars"}, name
    assert set(uv.APP_ONLY_TOOLS) <= set(tools) and not set(uv.APP_ONLY_TOOLS) & linked


def test_link_meta_keeps_the_other_keys_and_needs_a_view():
    meta = uv.link_meta("get_power_tree", {"anthropic/maxResultSizeChars": 5, "ui": {"x": 1}})
    assert meta == {"anthropic/maxResultSizeChars": 5,
                    "ui": {"x": 1, "resourceUri": POWER_TREE.uri},
                    "ui/resourceUri": POWER_TREE.uri}
    with pytest.raises(KeyError):
        uv.link_meta("query_net")


def test_the_manifest_lists_tools_only():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in manifest["tools"]}
    assert names == set(_tools())
    assert "ui://" not in json.dumps(manifest)


# ---------- self-contained pages ----------

def test_served_pages_are_self_contained():
    for view in uv.VIEWS:
        page = _served(view)
        for marker in (uv.CSP_MARKER, uv.CSS_MARKER, uv.JS_MARKER):
            assert marker not in page
        assert set(re.findall(r"https?://[^\s\"'<>)]+", page)) <= ALLOWED_URLS
        assert not re.search(r"<(link|img|iframe|object|embed|base|form|a|video|audio|source)\b",
                             page, re.I)
        assert not re.search(r"<script[^>]*\bsrc=", page, re.I)
        styles = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        scripts = "".join(re.findall(r"<script>(.*?)</script>", page, re.S))
        assert "@import" not in styles and "@font-face" not in styles and "url(" not in styles
        assert set(re.findall(r"url\((.)", scripts)) <= {"#"}  # SVG markers only
        assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|"
                             r"importScripts|Worker|window\.open|localStorage|sessionStorage|"
                             r"indexedDB|cookie|innerHTML|outerHTML|insertAdjacentHTML|eval)\b",
                             scripts)
        # The shared shell is inlined once, with its bridge and theme.
        assert page.count("const Shell = (() => {") == 1
        assert page.count("--c-bg: var(--color-background-primary") == 1
        assert f'<meta http-equiv="Content-Security-Policy" content="{uv.PAGE_CSP}">' in page


def test_the_page_policy_forbids_loading_and_connecting():
    directives = dict(item.strip().split(" ", 1) for item in uv.PAGE_CSP.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["base-uri"] == "'none'" and directives["form-action"] == "'none'"
    assert directives["img-src"] == "data:"
    assert set(directives) == {"default-src", "script-src", "style-src", "img-src",
                               "base-uri", "form-action"}  # connect, font, frame: none
    assert "http" not in uv.PAGE_CSP and "*" not in uv.PAGE_CSP


def test_the_shell_speaks_the_spec_messages():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    assert f'PROTOCOL_VERSION = "{uv.SPEC_VERSION}"' in shell
    for method in ("ui/initialize", "ui/notifications/initialized",
                   "ui/notifications/tool-input", "ui/notifications/tool-input-partial",
                   "ui/notifications/tool-result", "ui/notifications/tool-cancelled",
                   "ui/notifications/host-context-changed", "ui/resource-teardown",
                   "ui/notifications/size-changed", "ui/request-display-mode", "tools/call",
                   "ui/message"):
        assert f'"{method}"' in shell, method
    # Messages go to the host only and are taken from the host only.
    assert shell.count("postMessage(") == 1 and "event.source !== host" in shell
    # tools/call only when the host offers serverTools (an optional host capability).
    assert "capabilities.serverTools" in shell and "if (!canCallTools()) throw" in shell
    # ui/message only when the host takes text messages (hostCapabilities.message.text:
    # message: {} declares no content type); a denial is an error or a result with isError.
    assert "return Boolean(capabilities.message && capabilities.message.text);" in shell
    send = shell.split("async function sendMessage(", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!canSendMessage()) throw" in send
    assert '{ role: "user", content: [{ type: "text", text: String(text) }] }' in send
    assert "if (result && result.isError) throw" in send
    assert "ui/open-link" not in shell and "ui/update-model-context" not in shell
    # The page is measured with its scrollbar shown, so a height that fits never leaves it
    # stuck taller than the host's limit with a scrollbar that narrows it (text wraps more).
    measure = shell.split("function contentHeight() {", 1)[1].split("\n  }\n", 1)[0]
    assert 'html.style.overflowY = "scroll";' in measure and "html.style.overflowY = overflow;" in measure


def test_the_view_asks_the_host_only_for_what_it_offers():
    script = _view_script()
    # Every display-mode request, back to inline too, is one the host lists; the button
    # follows the host's own changes of mode.
    assert script.count("Shell.requestDisplayMode(") == 1
    assert "return Shell.canDisplay(mode) ? mode : null;" in script
    assert 'Shell.on("context", syncDisplayButton)' in script
    # Paging and details check the capability before any tools/call.
    assert script.count("Shell.callTool(") == 3
    assert "else if (!canCall()) stopped" in script and "if (!canCall()) return;" in script


def test_views_call_only_registered_tools_open_to_apps():
    tools = _tools()
    called = set(re.findall(r'callTool\("([a-z_]+)"', _view_script()))
    assert called == {"get_power_tree", "query_net", "get_component"}
    for name in called:
        visibility = ((tools[name].get("_meta") or {}).get("ui") or {}).get("visibility")
        assert visibility is None or "app" in visibility


@pytest.mark.parametrize("change, problem", [
    (lambda page, css, js: (page.replace(uv.CSS_MARKER, ""), css, js), "exactly once"),
    (lambda page, css, js: (page + uv.JS_MARKER, css, js), "exactly once"),
    (lambda page, css, js: (page, css, js + "</script>"), "closes a script"),
    (lambda page, css, js: (page, css + "</STYLE>", js), "closes a script"),
    (lambda page, css, js: (page, css, js + "// <!-- x"), "opens an HTML comment"),
])
def test_render_refuses_a_broken_page(tmp_path, change, problem):
    page, css, js = change((UI / POWER_TREE.file).read_text(encoding="utf-8"),
                           (UI / "shell.css").read_text(encoding="utf-8"),
                           (UI / "shell.js").read_text(encoding="utf-8"))
    (tmp_path / POWER_TREE.file).write_text(page, encoding="utf-8")
    (tmp_path / "shell.css").write_text(css, encoding="utf-8")
    (tmp_path / "shell.js").write_text(js, encoding="utf-8")
    with pytest.raises(ValueError, match=problem):
        uv.render(POWER_TREE, tmp_path)


def test_render_inlines_in_one_pass(tmp_path):
    # A marker's text inside an inlined part stays as it is (not a second insertion).
    shutil.copytree(UI, tmp_path, dirs_exist_ok=True)
    css = tmp_path / "shell.css"
    css.write_text(css.read_text(encoding="utf-8") + f"\n/* {uv.JS_MARKER} */\n", encoding="utf-8")
    page = uv.render(POWER_TREE, tmp_path)
    assert page.count(uv.JS_MARKER) == 1 and page.count("const Shell = (() => {") == 1


# ---------- packaging ----------

def test_pages_are_read_from_the_source_tree_or_the_bundle(tmp_path, monkeypatch):
    assert uv.ui_dir() == UI
    shutil.copytree(UI, tmp_path / "ui")
    monkeypatch.setattr(uv.sys, "frozen", True, raising=False)
    monkeypatch.setattr(uv.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert uv.ui_dir() == tmp_path / "ui"
    assert uv.render(POWER_TREE) == uv.render(POWER_TREE, UI)


def test_the_builds_bundle_the_pages():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pack = (ROOT / ".github" / "pack.ps1").read_text(encoding="utf-8")
    assert '--add-data "server/ui;ui"' in release
    assert r'--add-data "$root\server\ui;ui"' in pack
    for view in uv.VIEWS:
        assert (UI / view.file).is_file()


def test_readme_and_changelog_describe_the_views():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "Claude Code" in section and "Claude Desktop" in section
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #58" in unreleased and "roadmap #59" in unreleased
    # The copilot is a local stdio server: claude.ai on the web cannot run it, so the
    # docs do not promise the view there.
    main_source = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    assert "mcp.run()" in main_source and "transport=" not in main_source
    for text in (readme, unreleased):
        assert "Desktop and claude.ai" not in text and "on claude.ai the result" not in text
    assert "does not run the copilot" in section and "does not run this local server" in unreleased


# ---------- tool results unchanged ----------

def test_tool_results_are_the_same_text_through_the_protocol(board):
    calls = [("get_power_tree", {}), ("get_power_tree", {"format": "mermaid"}),
             ("get_power_tree", {"rail": "1V8", "vref": "U1=0.6"}),
             ("query_net", {"pattern": "3V3"}), ("get_component", {"refdes": "U1"}),
             ("search_components", {"pattern": "regulator"}), ("list_variants", {})]
    for name, args in calls:
        direct = getattr(main_mod, name)(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], name
        assert result["structuredContent"] == {"result": direct}, name
        assert not result.get("isError")


def test_the_linked_tool_keeps_its_output_schema():
    tools = _tools()
    assert tools["get_power_tree"]["outputSchema"]["properties"] == \
        tools["query_net"]["outputSchema"]["properties"] == {
            "result": {"title": "Result", "type": "string"}}


# ---------- the power tree view's data contract ----------

def test_the_contract_names_keys_the_power_tree_produces():
    contract = _contract()
    assert contract["tool"] == POWER_TREE.tool
    source = Path(pt.__file__).read_text(encoding="utf-8")
    for path, shape in contract["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert f'"{key}"' in source, f"{path}: {key}"
    # The lists get_power_tree pages together (each with its *_total, findings with total).
    assert set(contract["paged"]) == {"converters", "findings", "rails", "sources", "inputs"}


def test_the_view_reads_the_keys_its_contract_names():
    script = _view_script()
    for path, shape in _contract()["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert re.search(rf'\.{key}\b|"{key}"', script), f"{path}: {key}"


def test_the_contract_holds_on_a_synthetic_board(board):
    data = json.loads(main_mod.get_power_tree())
    assert _check_contract(data) == []
    assert data["converters"] and data["rails"] and data["sources"] and data["findings"]
    narrowed = json.loads(main_mod.get_power_tree(part="U2"))
    assert _check_contract(narrowed) == []


def test_paging_as_the_view_does_gives_the_whole_tree(board, monkeypatch):
    many = list(BOARD)
    for index in range(20):
        many += [(f"U{index + 10}", {"1": ("VIN", "3V3", "Power"),
                                     "2": ("VOUT", f"{index + 1}V0", "Power"),
                                     "3": ("GND", "GND", "Power")},
                  {"description": "LDO regulator"}),
                 (f"U{index + 100}", {"1": ("VDD", f"{index + 1}V0", "Power"),
                                      "2": ("GND", "GND", "Power")}, {})]
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(many))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 10 ** 9)
    text = main_mod.get_power_tree()
    full = json.loads(text)
    assert not full["has_more"]
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(text) // 4)
    first = json.loads(main_mod.get_power_tree())
    assert first["has_more"] and first["page_limited_to"] < 100
    data = _all_pages()
    for key in _contract()["paged"]:
        assert data[key] == full[key], key
    assert _check_contract(data) == []


@pytest.mark.parametrize("parts", PUBLIC, ids=[parts[-1] for parts in PUBLIC])
def test_the_contract_holds_on_the_public_examples(monkeypatch, parts):
    _example(monkeypatch, *parts)
    data = _all_pages()
    assert _check_contract(data) == []
    assert len(data["rails"]) == data["rails_total"]
    assert len(data["converters"]) == data["converters_total"]
    mermaid = json.loads(main_mod.get_power_tree(format="mermaid"))
    assert "rails" not in mermaid  # the view asks for the JSON tree then


def _page_value(name: str) -> str:
    match = re.search(rf"const {name} = (.*?);\n", _view_script().replace("\r\n", "\n"), re.S)
    return match.group(1)


def _converter_types() -> set[str]:
    """The view's converter types: the keys of its TYPE_LABEL table."""
    return set(re.findall(r"(\w+):", _page_value("TYPE_LABEL")))


def _prepared_edges() -> list[dict]:
    """Every edge of the loaded project's tree (what get_power_tree's lists come from)."""
    project, netlist, variant_state = main_mod._require_project()
    plan, _, _ = main_mod._bom_plan(netlist, variant_state, None)
    prepared = pt.prepare_power_tree(netlist, plan, project.get("parameters") or {})
    return prepared["view"]["edges"]


def _named_links(data: dict) -> dict[str, set]:
    """The links get_power_tree's lists name, per rail they leave: a rail's feeds, another
    rail's fed_by or undirected feeds, and the converters' outputs. The same rule as the
    view's buildModel, which counts a rail's feeds_total past these as not drawn."""
    named = defaultdict(set)
    for row in data["rails"]:
        for item in row.get("feeds", []):
            named[row["rail"]].add((item["part"], item.get("to") or item.get("with")))
            if "with" in item:
                named[item["with"]].add((item["part"], row["rail"]))
        for item in row.get("fed_by", []):
            named[item["from"]].add((item["part"], row["rail"]))
    for conv in data["converters"]:
        inputs = [item["rail"] for item in conv.get("inputs", [])]
        for output in conv.get("outputs", []):
            for rail in inputs if output.get("channel") else inputs[:1]:
                named[rail].add((conv["designator"], output["rail"]))
    return named


def _unlisted(data: dict) -> dict[str, int]:
    named = _named_links(data)
    return {row["rail"]: row["feeds_total"] - len(named[row["rail"]]) for row in data["rails"]
            if row.get("feeds_total", 0) > len(named[row["rail"]])}


def test_the_views_converter_types_are_the_power_trees():
    kinds = {kind for kind, _ in pt._CATEGORY_TEXT + pt._TOPOLOGY_TEXT + pt._MPN_FAMILIES}
    # Supervisors and references are listed, not converters; 'regulator' becomes
    # switching or linear.
    kinds = (kinds - {"supervisor", "reference", "regulator"}) | {"switching", "linear"}
    assert _converter_types() == kinds
    assert set(re.findall(r'"(\w+)"', _page_value("SWITCHING_TYPES"))) == set(pt.SWITCHING_KINDS)


def test_converter_links_and_series_links_differ_by_type(board):
    types = _converter_types()
    edges = _prepared_edges()
    assert any(edge.get("converter") for edge in edges) and any(not edge.get("converter") for edge in edges)
    for edge in edges:
        assert bool(edge.get("converter")) == (edge["type"] in types), edge["type"]


def test_links_past_a_rails_lists_are_counted_not_lost(board, monkeypatch):
    # Twelve ferrites in parallel from one rail to another: the first rail lists 10 of its
    # 12 links, the second its first 6 feeders, so 2 are named nowhere.
    parts = [("J1", {"1": ("VIN", "+5V", "Power"), "2": ("GND", "GND", "Power")},
              {"description": "DC power jack"}),
             ("U1", {"1": ("VDD", "+5V_A", "Power"), "2": ("GND", "GND", "Power")}, {})]
    parts += [(f"L{index}", {"1": ("1", "+5V", "Passive"), "2": ("2", "+5V_A", "Passive")},
               {"description": "Ferrite bead"}) for index in range(1, 13)]
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(parts))
    assert (_page_value("FEEDS_LISTED"), _page_value("FED_BY_LISTED")) == ("10", "6")
    source = Path(pt.__file__).read_text(encoding="utf-8")
    assert 'row["feeds"] = feeds[:10]' in source and 'row["fed_by"] = fed_by[:6]' in source
    data = _all_pages()
    rows = {row["rail"]: row for row in data["rails"]}
    assert len(rows["+5V"]["feeds"]) == 10 and rows["+5V"]["feeds_total"] == 12
    assert len(rows["+5V_A"]["fed_by"]) == 6
    assert _unlisted(data) == {"+5V": 2}
    named = {part for links in _named_links(data).values() for part, _ in links}
    assert len({edge["part"] for edge in _prepared_edges()} - named) == 2
    script = _view_script()
    assert "(row.feeds_total || 0) - (named.get(row.rail) || new Set()).size" in script
    assert "output.channel ? inputs : inputs.slice(0, 1)" in script


@pytest.mark.parametrize("parts", PUBLIC[:3], ids=[parts[-1] for parts in PUBLIC[:3]])
def test_the_lists_name_every_link_of_the_public_examples(monkeypatch, parts):
    _example(monkeypatch, *parts)
    data = _all_pages()
    named = {(part, frozenset((rail, other))) for rail, links in _named_links(data).items()
             for part, other in links}
    for edge in _prepared_edges():
        assert (edge["part"], frozenset((edge["from"], edge["to"]))) in named, edge["part"]
    assert _unlisted(data) == {}


def test_a_connector_can_be_an_outlet_for_one_rail_and_an_input_for_another(monkeypatch):
    # MiniPC's J14 passes two board rails on and brings a third in: the view draws an
    # outlet node and an input node, so no supply path runs through the connector.
    _example(monkeypatch, "Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb")
    roles = defaultdict(set)
    for source in _all_pages()["sources"]:
        roles[source["part"]].add(source.get("role"))
    assert {"outlet", "input"} <= roles["J14"]
    assert "node(outlet ? outletId(source.part) : partId(source.part)" in _view_script()


def test_a_filtered_tree_names_the_converters_it_leaves_out_by_type(monkeypatch):
    _example(monkeypatch, "Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb")
    data = _all_pages(rail="HPS_0V95out")
    listed = {conv["designator"] for conv in data["converters"]}
    linked = defaultdict(set)
    for row in data["rails"]:
        for item in row.get("fed_by", []) + row.get("feeds", []):
            if item["part"] not in listed:
                linked[item["type"] in _converter_types()].add(item["part"])
    # The view draws these as converters outside the view, the rest as series parts.
    assert linked[True] >= {"U3", "U4", "U5", "U6"} and not linked[True] & linked[False]
    assert "CONVERTER_TYPES.has(item.type) ? outsideConverter(item) : series(item)" in _view_script()


def test_the_mini_pc_tree_draws_with_every_link_end_known(monkeypatch):
    _example(monkeypatch, "Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb")
    data = _all_pages()
    assert data["overview"]["converters"] == len(data["converters"]) > 5
    rails = {row["rail"] for row in data["rails"]}
    ends = {item["rail"] for conv in data["converters"]
            for item in conv.get("inputs", []) + conv.get("outputs", [])}
    ends |= {item["from"] for row in data["rails"] for item in row.get("fed_by", [])}
    ends |= {item.get("to") or item.get("with") for row in data["rails"]
             for item in row.get("feeds", [])}
    ends |= {source["net"] for source in data["sources"]}
    assert ends <= rails  # an unfiltered tree has no rail outside the view


# ---------- details the view asks for ----------

# The view's query_net pattern: the name with every Python regex metacharacter escaped,
# anchored (exactPattern in power_tree.html).
_JS_ESCAPE = r'replace(/[\\^$.|?*+()[\]{}]/g, "\\$&")'


def _exact(name: str) -> str:
    return "^" + re.sub(r"[\\^$.|?*+()\[\]{}]", lambda m: "\\" + m.group(0), name) + "$"


def test_the_exact_net_pattern_finds_one_net_only():
    assert _JS_ESCAPE in _view_script()
    netlist = {"nets": {}, "pin_to_net": {}, "components": {"U1": {}}}
    names = ["+3V3", "3V3", "VIN[0]", "VIN0", "A.B", "AxB", "P(1)", "x|y", "\\N", "{V}", "$V^"]
    for index, name in enumerate(names):
        netlist["nets"][name] = [("U1", str(index))]
    for name in names:
        result = json.loads(main_mod._query_net_impl(netlist, _exact(name)))
        assert result["net"] == name, name
