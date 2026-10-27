"""The variant comparison view (roadmap #61, server/ui/variants.html): its ui:// resource
and compare_variants' link (list_variants links none and only feeds the picker), the
self-contained page, the read-only rule for every view's tool calls, its data contract
against compare_variants' and list_variants' output on synthetic variant projects (the
repo's Demo project and a copy of Altium's public SpiritLevel example with variants added:
Not Fitted parts, varied parameters, an alternate part with its .PrjPcbVariants copy,
variant parameters, OutJob outputs per variant, stale entries, more than one page of
differences), paging the way the view pages, and unchanged text results."""

import asyncio
import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import types

import server.main as main_mod
import server.services.ui_views as uv
from server.parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_pcb
from server.services.bom import BASE_VARIANT
from tests.test_output_jobs import _outjob, _output
from tests.test_variants import (
    PRJ, _FakeOle, _documented_project, _drawing, _library_streams, _netlist, _patch_main,
)

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
SL1 = EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"
VIEW = uv.view_for("compare_variants")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
# The tools the variant page calls: compare_variants (paging, the picker), list_variants
# (the picker, the active variant's name) and get_component (a part's details).
CALLED = {"compare_variants", "list_variants", "get_component"}


def _handle(request):
    handler = main_mod.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


def _page(view=VIEW) -> str:
    return (UI / view.file).read_text(encoding="utf-8")


def _script(view=VIEW) -> str:
    return _page(view).split(uv.JS_MARKER, 1)[1]


def _contract() -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>',
                      _page(), re.S)
    return json.loads(block.group(1))


def _rows_at(data, path: str) -> list:
    """The rows a contract path names, read as the view's rowsAt does: "x[]" a list's
    items, "x{}" an object's values."""
    if path == "result":
        return [data]
    rows = [data]
    for part in path.split("."):
        key = re.sub(r"(\[\]|\{\})$", "", part)
        out = []
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if part.endswith("[]"):
                out.extend(value if isinstance(value, list) else [])
            elif part.endswith("{}"):
                out.extend(value.values() if isinstance(value, dict) else [])
            elif value is not None:
                out.append(value)
        rows = out
    return rows


def _check(shapes: dict, data: dict) -> list[str]:
    problems = []
    for path, shape in shapes.items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path)
                          if not isinstance(row, dict) or key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


def _read(**args) -> dict:
    """compare_variants read the way the view reads it: the first page (from offset 0 when
    the result is a later one), then every next_offset with the result's own a and b,
    each page checked to be one of the same comparison; the differences joined by kind."""
    first = json.loads(main_mod.compare_variants(**args))
    if "error" in first:
        return first
    if first["offset"] > 0:
        again = json.loads(main_mod.compare_variants(a=first["a"], b=first["b"], offset=0))
        assert (again["a"], again["b"]) == (first["a"], first["b"])
        assert again["differences_total"] == first["differences_total"]
        first = again
    data = {**first, "differences": {kind: list(rows)
                                     for kind, rows in first["differences"].items()}}
    page = first
    while page["has_more"]:
        page = json.loads(main_mod.compare_variants(a=first["a"], b=first["b"],
                                                    offset=page["next_offset"]))
        assert (page["a"], page["b"]) == (first["a"], first["b"])
        assert page["differences_total"] == first["differences_total"]
        for key in ("documentation", "notes", "stale_variant_entries"):
            assert key not in page  # first page only
        for kind, rows in page["differences"].items():
            data["differences"].setdefault(kind, []).extend(rows)
    return data


def _flat(data: dict) -> list[tuple]:
    kinds = _contract()["kinds"]
    return [(kind, json.dumps(row, sort_keys=True))
            for kind in kinds for row in data["differences"].get(kind, [])]


# ---------- synthetic variant projects ----------

def _alt(libref: str, library: str, footprint: str) -> str:
    return (f"AltLibLink_LibIdentifierKind=Library Name|AltLibLink_LibraryIdentifier={library}"
            f"|AltLibLink_SymbolReference={libref}|AltLibLink_DesignItemID={libref}"
            f"|AltLibLink_UseLibraryName=True|AltLibLink_SourceLibraryName={library}"
            "|AltLibLink_UseDBTableName=False|AltLibLink_DatabaseTableName="
            "|AltLibLink_VaultGUID=|AltLibLink_ItemGUID=|AltLibLink_RevisionGUID="
            f"|AltLibLink_Footprint={footprint}|AltLibLink_IsSameFootprint=False")


def _variant(index: int, name: str, uids: dict, not_fitted=(), varied=None,
             alternates=None, parameters=None, stale=()) -> str:
    """One [ProjectVariantN] section with its [ParameterN_M] sections."""
    varied, alternates = varied or {}, alternates or {}
    lines = [f"Designator={d}|UniqueId=\\{uids[d]}|Kind=1|AlternatePart=" for d in not_fitted]
    lines += [f"Designator={d}|UniqueId=\\NOPART{n}|Kind=1|AlternatePart="
              for n, d in enumerate(stale)]
    lines += [f"Designator={d}|UniqueId=\\{uids[d]}|Kind=2|AlternatePart={lib}|{_alt(lib, src, fp)}"
              for d, (lib, src, fp) in alternates.items()]
    lines += [f"Designator={d}|UniqueId=\\{uids[d]}|Kind=0|AlternatePart=" for d in varied
              if d not in alternates and d not in not_fitted]
    pairs = [(d, key, value) for d, params in varied.items() for key, value in params.items()]
    text = [f"[ProjectVariant{index}]", f"Description={name}", f"UniqueId=VAR{index}",
            "AllowFabrication=0", f"ParameterCount={len(parameters or {})}",
            f"VariationCount={len(lines)}"]
    text += [f"Variation{n}={line}" for n, line in enumerate(lines, 1)]
    text.append(f"ParamVariationCount={len(pairs)}")
    for n, (d, key, value) in enumerate(pairs, 1):
        text += [f"ParamDesignator{n}={d}", f"ParamVariation{n}=ParameterName={key}|VariantValue={value}"]
    out = "\n".join(text) + "\n\n"
    for n, (key, value) in enumerate((parameters or {}).items(), 1):
        out += f"[Parameter{index + 1}_{n}]\nName={key}\nValue={value}\n\n"
    return out


def _sl1_variants(uids: dict) -> str:
    """Production (an alternate part with its library copy, varied Comment and Tolerance,
    variant parameters), Debug (another parameter varied, a stale line naming no part and
    a parameter varied on a part it does not fit), Lite, and Bare (more than 100 parts Not
    Fitted: two pages of differences against the base design)."""
    leds = [f"LED{i}" for i in range(10)]
    keep = {"U1", "U2", "U3", "U4", "U5", "U6", "J1", "F1", "Y1"}
    return "".join([
        _variant(1, "Production", uids, not_fitted=["HDR1", "JP3", "JP4", "JP5", "LED8", "LED9"],
                 varied={"R2": {"Comment": "470R", "Tolerance": "1%"}, "R3": {"Comment": "12K"},
                         "C1": {"Comment": "220uF 16V"}},
                 alternates={"C1": ("CAPX", "Alt.SchLib", "CAPPR2-5X11")},
                 parameters={"Build": "Production", "Assembly": "Rev B"}),
        _variant(2, "Debug", uids, not_fitted=["LCD1", "Q1", "R1"],
                 varied={**{led: {"Color": "Red"} for led in leds[:4]},
                         "R4": {"Comment": "10K"}, "LCD1": {"Comment": "unused"}},
                 parameters={"Build": "Debug"}, stale=["R99"]),
        _variant(3, "Lite", uids, not_fitted=leds + ["C5", "C6", "HDR2", "HDR3"],
                 varied={"R7": {"Description": "Thick film 1K"}}, parameters={"Build": "Lite"}),
        _variant(4, "Bare", uids, not_fitted=sorted(set(uids) - keep, key=main_mod.natural_key)),
    ])


@pytest.fixture
def sl1(monkeypatch, tmp_path):
    """A copy of the SpiritLevel example's project and sheets in tmp_path with four
    variants, a .PrjPcbVariants copy of the alternate part (read through a patched
    olefile), an OutJob with outputs set to variants and a Draftsman drawing (read as
    _drawing()), loaded offline with its compiled netlist."""
    if not SL1.is_file():
        pytest.skip("the SpiritLevel example is not installed")
    for source in SL1.parent.iterdir():
        if source.suffix.lower() in (".schdoc", ".prjpcb"):
            shutil.copy(source, tmp_path / source.name)
    prj = tmp_path / SL1.name
    client = main_mod.AltiumClient()
    netlist = client.compile_offline(str(prj))
    uids = {name: meta["unique_id"] for name, meta in netlist["components"].items()
            if meta.get("unique_id")}
    outjob = _outjob([
        _output("BOM_PartType", "Bill of Materials", category="Report", variant="Production"),
        _output("Pick Place", "Pick and Place", category="Assembly", variant="Production"),
        _output("PCBDrawing", "Draftsman", category="Documentation", doc="Asm.PCBDwf"),
        _output("BOM_PartType", "Debug BOM", category="Report", variant="Debug", links={}),
    ], variant="[No Variations]")
    (tmp_path / "Release.OutJob").write_text(outjob, encoding="utf-8")
    (tmp_path / "Asm.PCBDwf").write_bytes(b"drawing")
    (tmp_path / f"{prj.stem}.PrjPcbVariants").write_bytes(b"ole")
    text = prj.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    prj.write_text(text + "\n\n[Document20]\nDocumentPath=Release.OutJob\n\n"
                   "[Document21]\nDocumentPath=Asm.PCBDwf\n\n" + _sl1_variants(uids),
                   encoding="utf-8")
    library = _library_streams(description="Cap 220u", comment="220uF 16V",
                               footprint="CAPPR2-5X11", mpn="CAP-220U")
    with patch("olefile.OleFileIO", lambda path: _FakeOle(library)):
        data = parse_prj_pcb(str(prj))
    state = VariantState(data.variants)
    main_mod._resolve_dnp_uids(state, netlist)
    monkeypatch.setattr(main_mod, "_project", {
        "name": prj.stem, "root_dir": str(tmp_path), "prj_pcb_path": str(prj),
        "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths],
        **main_mod._project_documents(data)})
    monkeypatch.setattr(main_mod, "_altium", client)
    monkeypatch.setattr(main_mod, "_variant_state", state)
    notes = main_mod.DesignNotesCache()
    monkeypatch.setattr(notes, "draftsman", lambda path: _drawing())
    monkeypatch.setattr(main_mod, "_design_notes", notes)
    return state


@pytest.fixture
def demo(monkeypatch, tmp_path):
    """The repo's Demo project (tests/test_variants.py) with its documented outputs."""
    prj = _documented_project(tmp_path)
    (tmp_path / "Demo.PrjPcbVariants").write_bytes(b"ole")
    with patch("olefile.OleFileIO", lambda path: _FakeOle(_library_streams())):
        state = VariantState(parse_prj_pcb(str(prj)).variants)
    main_mod._resolve_dnp_uids(state, _netlist())
    _patch_main(monkeypatch, tmp_path, state)
    main_mod._project["prj_pcb_path"] = str(prj)
    main_mod._project["pcb_dwf_paths"] = [str(tmp_path / "Asm.PCBDwf")]
    monkeypatch.setattr(main_mod._design_notes, "draftsman", lambda path: _drawing())
    return state


def _pairs(state) -> list[tuple[str, str]]:
    names = [variant.name for variant in state.variants] + [BASE_VARIANT]
    return [(a, b) for a in names for b in names]


# ---------- resources and links ----------

def test_the_view_is_a_listed_resource_linked_from_compare_variants():
    assert VIEW.uri == "ui://altium-copilot-kc/variant-comparison.html"
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
    assert tools["compare_variants"]["_meta"] == {
        "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
        "ui": {"resourceUri": VIEW.uri}, "ui/resourceUri": VIEW.uri}
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)


def test_list_variants_links_no_view_and_only_feeds_the_picker():
    # The registry decision: the model calls list_variants at every session start, where
    # a panel would repeat the text; the view asks it through tools/call for its picker.
    tools = _tools()
    assert "_meta" not in tools["list_variants"] or "ui" not in tools["list_variants"]["_meta"]
    with pytest.raises(KeyError):
        uv.view_for("list_variants")
    script = _script()
    assert 'call("list_variants", {})' in script
    doc = (ROOT / "server" / "services" / "ui_views.py").read_text(encoding="utf-8")
    assert "linked from compare_variants only" in doc and "list_variants" in doc


def test_the_page_is_self_contained():
    page = uv.render(VIEW)
    for marker in (uv.CSP_MARKER, uv.CSS_MARKER, uv.JS_MARKER):
        assert marker not in page
    assert set(re.findall(r"https?://[^\s\"'<>)]+", page)) <= ALLOWED_URLS
    assert not re.search(r"<(link|img|iframe|object|embed|base|form|a|video|audio|source)\b",
                         page, re.I)
    assert not re.search(r"<script[^>]*\bsrc=", page, re.I)
    styles = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    scripts = "".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    assert "@import" not in styles and "@font-face" not in styles and "url(" not in styles
    assert "url(" not in _script()
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|"
                         r"importScripts|Worker|window\.open|localStorage|sessionStorage|"
                         r"indexedDB|cookie|innerHTML|outerHTML|insertAdjacentHTML|eval)\b",
                         scripts)
    assert page.count("const Shell = (() => {") == 1
    assert f'<meta http-equiv="Content-Security-Policy" content="{uv.PAGE_CSP}">' in page


# ---------- the read-only rule ----------

def _tool_names(script: str, tools: dict) -> set[str]:
    """Every registered tool a page's script names as a string literal."""
    return {name for name in re.findall(r'"([a-z_]+)"', script) if name in tools}


@pytest.mark.parametrize("view", uv.VIEWS, ids=[view.name for view in uv.VIEWS])
def test_every_view_calls_only_registered_read_only_tools(view):
    tools = _tools()
    script = _script(view)
    named = _tool_names(script, tools)
    sites = set(re.findall(r'(?:callTool|call|ask|load)\(\s*"([a-z_]+)"', script))
    sites |= set(re.findall(r'calls\.push\(\["([a-z_]+)"', script))
    assert view.tool in named and sites and sites <= named
    for name in named:
        assert tools[name]["annotations"]["readOnlyHint"] is True, (view.name, name)
        visibility = ((tools[name].get("_meta") or {}).get("ui") or {}).get("visibility")
        assert visibility is None or "app" in visibility
    # No page names a tool that changes the server's state, set_active_variant above all.
    changing = {name for name, tool in tools.items() if not tool["annotations"].get("readOnlyHint")}
    assert "set_active_variant" in changing and not named & changing
    assert '"set_active_variant"' not in _page(view)


def test_the_variant_page_calls_only_its_three_tools_through_one_guard():
    script = _script()
    tools = _tools()
    assert _tool_names(script, tools) == CALLED
    allowed = re.search(r"const READ_ONLY_TOOLS = new Set\(\[(.*?)\]\);", script).group(1)
    assert set(re.findall(r'"([a-z_]+)"', allowed)) == CALLED
    # Every tools/call goes through call() (refusing any other tool) or the details cache
    # behind ask() (the same check), and the shell's serverTools check.
    assert script.count("Shell.callTool(") == 1
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in script
    assert "if (!READ_ONLY_TOOLS.has(tool)) return;\n" in script
    assert "if (view.busy || !canCall() || view.closed) return;" in script
    assert "if (!canCall() || view.closed) return Promise.resolve();" in script
    assert "if (view.variantsLoading) return view.variantsLoading;" in script
    assert "Shell.readPages(" in script and "closed: () => view.closed" in script
    # The page says that the picker only compares.
    assert "Comparing does not switch the active variant" in script
    assert "never set_active_variant" in script


# ---------- the data contract ----------

def _sources() -> tuple[str, ...]:
    paths = [ROOT / "server" / "main.py", ROOT / "server" / "services" / "variants.py",
             ROOT / "server" / "services" / "bom.py", ROOT / "server" / "parsers" / "prj_pcb.py"]
    return tuple(path.read_text(encoding="utf-8") for path in paths)


def test_the_contract_names_keys_the_tools_produce():
    contract = _contract()
    assert (contract["tool"], contract["picker"], contract["paged"]) == (
        "compare_variants", "list_variants", "differences")
    from server.services.variants import GROUPS
    assert contract["kinds"] == list(GROUPS)
    sources = _sources()
    for shapes in (contract["shapes"], contract["list_variants"]):
        for path, shape in shapes.items():
            for key in shape["required"] + shape["optional"]:
                # documentation() counts a role's outputs past the ones listed in f"{role}_count".
                counted = key.endswith("_count") and 'f"{role}_count"' in sources[1]
                assert counted or any(f'"{key}"' in text for text in sources), f"{path}: {key}"


def test_the_view_reads_the_keys_its_contract_names():
    script = _script()
    contract = _contract()
    for shapes in (contract["shapes"], contract["list_variants"]):
        for path, shape in shapes.items():
            for key in shape["required"] + shape["optional"]:
                assert re.search(rf'\.{key}\b|"{key}"', script), f"{path}: {key}"
    # The view reads the contract's kinds and checks both tools' outputs at runtime.
    assert "const KINDS = CONTRACT.kinds;" in script
    assert "checkContract(CONTRACT.shapes, merged)" in script
    assert "checkContract(CONTRACT.list_variants, listing)" in script


def test_the_contract_holds_on_the_demo_project(demo):
    listing = json.loads(main_mod.list_variants())
    assert _check(_contract()["list_variants"], listing) == []
    assert listing["variants"][1]["alternate_parts"][0]["designator"] == "C1"
    seen = set()
    for a, b in _pairs(demo):
        data = _read(a=a, b=b)
        assert _check(_contract()["shapes"], data) == [], (a, b)
        seen |= {kind for kind, rows in data["differences"].items() if rows}
        assert data["documentation"]["per_variant"] and data["notes"]
    assert seen == set(_contract()["kinds"])
    docs = _read(a="Full", b="Lite")["documentation"]
    assert docs["per_variant"][1]["outputs_missing"] == ["bom", "pick_and_place"]
    assert docs["draftsman"]["Asm.PCBDwf"]["readable"] is True


def test_the_contract_holds_on_a_public_example_with_variants(sl1):
    listing = json.loads(main_mod.list_variants())
    assert _check(_contract()["list_variants"], listing) == []
    assert [row["name"] for row in listing["variants"]] == ["Production", "Debug", "Lite", "Bare"]
    kinds = set()
    for a, b in _pairs(sl1):
        data = _read(a=a, b=b)
        assert _check(_contract()["shapes"], data) == [], (a, b)
        assert len(_flat(data)) == data["differences_total"], (a, b)
        kinds |= {kind for kind, rows in data["differences"].items() if rows}
    assert kinds == set(_contract()["kinds"])
    data = _read(a="Production", b="Debug")
    (alternate,) = data["differences"]["alternate_parts"]
    assert alternate["a"]["part"] == "alternate" and alternate["a"]["library_copy"] is True
    assert alternate["b"] == {"part": "base", "lib_reference": alternate["b"]["lib_reference"],
                              "footprint": alternate["b"]["footprint"]}
    issues = {entry["issue"] for entry in data["stale_variant_entries"]["b"]}
    assert issues == {"no_such_part", "varied_but_not_fitted"}
    per = {row["variant"]: row for row in data["documentation"]["per_variant"]}
    # The Draftsman output prints its drawing's own variant (Lite); Debug's BOM is not
    # connected to any container.
    assert per["Production"]["outputs_missing"] == ["assembly_drawing"]
    assert per["Debug"]["outputs_missing"] == ["bom", "pick_and_place", "assembly_drawing"]
    assert per["Lite"]["outputs_missing"] == ["bom", "pick_and_place"]
    params = {(row["parameter"], row["a"], row["b"]) for row in data["differences"]["variant_parameters"]}
    assert params == {("Assembly", "Rev B", None), ("Build", "Production", "Debug")}
    # compare_variants gives no sheet: the view groups by kind or by part only.
    assert all("sheet" not in row for rows in data["differences"].values() for row in rows)
    groupings = re.search(r"const GROUPINGS = \[(.*?)\];", _script(), re.S).group(1)
    assert set(re.findall(r'id: "([a-z]+)"', groupings)) == {"kind", "part", "none"}


def test_the_contract_holds_without_a_netlist(sl1, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    data = _read(a="Debug", b="Production")
    assert data["binding"] == "stored_designators"
    assert _check(_contract()["shapes"], data) == []
    # No base value without the netlist: the view says "not known (no netlist)".
    assert all("base" not in row for row in data["differences"].get("values", []))
    script = _script()
    assert 'view.data.binding === "stored_designators"' in script
    assert 'if (!("base" in row)) return "not known (no netlist)";' in script


# ---------- paging ----------

def test_paging_as_the_view_does_gives_every_difference_once(sl1, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 10 ** 9)
    monkeypatch.setattr(main_mod, "VARIANT_DIFF_LIMITS", (10 ** 6,))
    whole = {pair: _flat(json.loads(main_mod.compare_variants(a=pair[0], b=pair[1])))
             for pair in (("Bare", BASE_VARIANT), ("Bare", "Production"), ("Debug", "Lite"))}
    monkeypatch.setattr(main_mod, "VARIANT_DIFF_LIMITS", (100, 40, 15, 5, 1))
    first = json.loads(main_mod.compare_variants(a="Bare"))
    assert first["has_more"] and first["next_offset"] == 100 and first["differences_total"] > 100
    for (a, b), rows in whole.items():
        assert _flat(_read(a=a, b=b)) == rows, (a, b)
    # Pages cut to fit the size limit: the view follows next_offset all the same.
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6000)
    trimmed = json.loads(main_mod.compare_variants(a="Bare", b="Production"))
    assert trimmed["differences_limited_to"] < 100
    for (a, b), rows in whole.items():
        data = _read(a=a, b=b)
        assert _flat(data) == rows, (a, b)
        assert len(set(_flat(data))) == len(rows)
    script = _script()
    assert "pageOffset: (page) => (page.has_more && page.next_offset !== undefined ? page.next_offset : null)" in script
    assert "getPage: (offset) => getPage(offset, first.a, first.b, first.differences_total)" in script
    assert "page.differences_total !== total || page.a !== a || page.b !== b" in script


def test_a_later_page_is_read_again_from_offset_0(sl1):
    later = json.loads(main_mod.compare_variants(a="Bare", offset=100))
    first = json.loads(main_mod.compare_variants(a="Bare"))
    assert later["offset"] == 100 and not later["has_more"]
    assert (later["a"], later["b"], later["differences_total"]) == (
        first["a"], first["b"], first["differences_total"])
    for key in ("documentation", "notes"):
        assert key in first and key not in later
    assert _flat(_read(a="Bare", offset=100)) == _flat(_read(a="Bare"))
    script = _script()
    assert "if (first.offset > 0) {" in script
    assert "firstPage = await getPage(0, first.a, first.b, first.differences_total);" in script
    assert "this result starts at offset ${first.offset}" in script
    # Without the first page the documentation's counts are not known (not 0).
    assert 'const extra = findings === null ? "?"' in script
    assert 'el("h2", { text: "Documentation: not known" })' in script


# ---------- states ----------

def test_the_error_payloads_and_empty_states(demo, monkeypatch):
    script = _script()
    unknown = json.loads(main_mod.compare_variants(a="Nope"))
    assert unknown["error"] == "variant_not_found" and "Nope" in unknown["message"]
    assert json.loads(main_mod.compare_variants(b="Nope"))["error"] == "variant_not_found"
    same = json.loads(main_mod.compare_variants(a="Full", b="full"))
    assert (same["a"], same["b"], same["differences_total"], same["differences"]) == (
        "Full", "Full", 0, {})
    assert 'Shell.state("empty", "No differences"' in script
    assert "a and b are the same variant (${data.a})" in script
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([VariantDefinition(name="Default")]))
    none = json.loads(main_mod.compare_variants())
    assert none["error"] == "no_variants"
    assert 'if (payload.error === "no_variants")' in script
    assert 'Shell.state("empty", "This project defines no variants"' in script
    monkeypatch.setattr(main_mod, "_project", None)
    for name in ("compare_variants", "list_variants"):
        assert json.loads(getattr(main_mod, name)())["error"] == "no_project"
    for code in ("no_project", "variant_not_found", "no_components_loaded"):
        assert f"{code}:" in script, code
    assert "if (first.error) {" in script and "showError(first);" in script
    # An unknown variant shows the picker so another one can be picked.
    assert 'if (payload.error === "variant_not_found" && canCall()) {' in script
    assert 'showState("info", "The compare_variants call was cancelled"' in script


def test_the_picker_offers_names_compare_variants_accepts(sl1):
    script = _script()
    assert f'const BASE = "{BASE_VARIANT}";' in script
    names = [row["name"] for row in json.loads(main_mod.list_variants())["variants"]]
    for name in [*names, BASE_VARIANT]:
        result = json.loads(main_mod.compare_variants(a=name, b=BASE_VARIANT))
        assert "error" not in result and result["a"] == name and result["b"] == BASE_VARIANT
    # The picker re-asks with offset 0; the swap compares b against a.
    assert 'call("compare_variants", { a, b, offset: 0 })' in script
    assert "compare(view.pick.b, view.pick.a)" in script


def test_get_component_is_labelled_as_the_active_variant(demo):
    # get_component answers for the active variant, whatever the comparison shows.
    demo.set_variant("Lite")
    lite = json.loads(main_mod.get_component(refdes="C1"))
    demo.set_variant("Full")
    full = json.loads(main_mod.get_component(refdes="C1"))
    assert lite["variant_changes"]["alternate_part"]["lib_reference"] == "CAPX"
    assert "variant_changes" not in full
    assert json.loads(main_mod.list_variants())["active_variant"] == "Full"
    script = _script()
    assert "text: `${label} (active variant)`" in script
    assert "get_component shows the part as the ACTIVE variant" in script
    # Asking get_component asks list_variants first (the active variant may have
    # changed), and an answer can be asked again (the shell's cache forgets it).
    assert "details.forget(tool, args);" in script
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    assert "forget(tool, args) {" in shell and "if (entry && !entry.loading) entries.delete(" in shell


def test_a_get_component_answer_keeps_the_active_variant_it_was_asked_under(demo):
    # get_component's variant_changes name the active variant the answer was made under.
    demo.set_variant("Lite")
    assert json.loads(main_mod.get_component(refdes="C1"))["variant_changes"]["variant"] == "Lite"
    demo.set_variant("Full")
    assert json.loads(main_mod.get_component(refdes="R1"))["variant_changes"]["variant"] == "Full"
    script = _script()
    ask = script.split("async function ask(tool, args, label) {", 1)[1].split("\n  }\n", 1)[0]
    # list_variants first; a kept answer asked under another active variant is dropped
    # and asked again; a new ask records the active variant it is made under.
    order = ["await loadVariants();", "const under = view.variantsError ? null : view.active;",
             "if (kept && kept.data && askedUnder.get(key) !== under) details.forget(tool, args);",
             "if (!entry || entry.error) askedUnder.set(key, under);", "details.load(tool, args);"]
    assert [ask.index(line) for line in order] == sorted(ask.index(line) for line in order)
    # The label is the variant the answer was asked under (get_component's own name for it
    # when the variant changes the part), never the one list_variants names later; a
    # changed active variant is said, with Ask again.
    assert "if (json && json.variant_changes && json.variant_changes.variant) return json.variant_changes.variant;" in script
    assert "`As the active variant when asked, ${under}, shows it (not necessarily a or b).`" in script
    assert "`The active variant is now ${view.active}: Ask again for its values.`" in script
    assert "view.active ? `, ${view.active},` : \"\"} shows it" not in script


def test_without_a_netlist_get_component_is_not_offered(sl1, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    assert json.loads(main_mod.compare_variants(a="Debug"))["binding"] == "stored_designators"
    assert json.loads(main_mod.get_component(refdes="R1"))["error"] == "netlist_required"
    script = _script()
    section = script.split("function serverSection(designator) {", 1)[1].split("\n  }\n", 1)[0]
    assert section.index("if (unbound()) {") < section.index("ui.askButton = el(")
    # A variant parameter a variant lacks is "not set", netlist or not (it comes from the
    # .PrjPcb); only a part's values are not known without the netlist.
    data = _read(a="Production", b="Debug")
    unset = [row for row in data["differences"]["variant_parameters"] if row["b"] is None]
    assert unset and data["binding"] == "stored_designators"
    assert 'return row._kind === "variant_parameters" ? "not set" : unsetText();' in script


def test_the_stale_cap_and_base_names_are_the_servers(monkeypatch, tmp_path):
    contract = _contract()
    assert contract["stale_shown"] == main_mod.VARIANT_STALE_SHOWN
    assert contract["base_names"] == list(main_mod.BOM_BASE_NAMES)
    # A variant with 25 stale lines: compare_variants lists the first 20, with no count,
    # while get_bom counts all 25; the view says "at least" at the cap.
    lines = "".join(f"Variation{n}=Designator=X{n}|UniqueId=\\NOPART{n}|Kind=1|AlternatePart=\n"
                    for n in range(1, 26))
    prj = tmp_path / "Demo.PrjPcb"
    prj.write_text(PRJ + "\n[ProjectVariant3]\nDescription=Stale\nUniqueId=VARTHREE\n"
                   "AllowFabrication=0\nParameterCount=0\nVariationCount=25\n" + lines
                   + "ParamVariationCount=0\n", encoding="utf-8")
    with patch("olefile.OleFileIO", lambda path: _FakeOle(_library_streams())):
        state = VariantState(parse_prj_pcb(str(prj)).variants)
    main_mod._resolve_dnp_uids(state, _netlist())
    _patch_main(monkeypatch, tmp_path, state)
    result = json.loads(main_mod.compare_variants(a="Stale"))
    assert len(result["stale_variant_entries"]["a"]) == main_mod.VARIANT_STALE_SHOWN
    assert json.loads(main_mod.get_bom(variant="Stale"))["stale_variant_entries"] == 25
    script = _script()
    assert "(stale[side] || []).length >= STALE_SHOWN" in script
    assert "`at least ${count}`" in script and "`Stale variant entries (${staleCount()})`" in script
    # The base design's other names pick the base design in the unknown-variant picker.
    for name in main_mod.BOM_BASE_NAMES:
        assert json.loads(main_mod.compare_variants(a=name))["a"] == BASE_VARIANT, name


def test_the_unknown_variant_picker_starts_from_the_known_side():
    script = _script()
    error = script.split("if (first.error) {", 1)[1].split("return;", 1)[0]
    assert "view.pick = { a: null, b: null };" in error
    assert "view.wanted = { a: view.args.a, b: view.args.b };" in error
    # A side is picked when the project has it (or it names the base design); the other
    # falls back to compare_variants' default: a the active variant, b the base design.
    assert "const current = known(view.pick[side]) || known(view.wanted[side]) || fallback;" in script
    assert 'const fallback = side === "b" ? BASE : known(view.active) || names[0];' in script
    # A failed compare from that state keeps it and its picker, with the failure above.
    fail = script.split("async function compare(a, b) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (view.errorPayload) showError(view.errorPayload);" in fail


def _natural_order_like_the_view(names: list[str]) -> list[str]:
    """The view's naturalOrder (variants.html), written out in Python."""
    def key(text):
        parts = re.split(r"(\d+)", text)
        return [int(part) if index % 2 else part.lower() for index, part in enumerate(parts)]
    return sorted(names, key=key)


def test_groups_and_the_part_column_follow_compare_variants_order():
    names = ["R? (UniqueId ABC)", "R1", "R10", "R9", "C1_2", "C1-2", "C1.5", "TP_1", "TP-1",
             "TP1", "r2", "R02", "U1A", "U1", "U10B", "Q1_10", "Q1_9", "LED1", "led10"]
    assert _natural_order_like_the_view(names) == sorted(names, key=main_mod.natural_key)
    script = _script()
    assert ("const naturalKey = (text) => String(text).split(/(\\d+)/).map((part, index) => "
            "(index % 2 ? Number(part) : part.toLowerCase()));") in script
    assert "const naturally = naturalOrder;" in script and "Shell.naturally" not in script
    assert ".sort(naturalOrder).map((designator, index) => [designator, index])" in script
    assert 'label: "Part", cls: "c-name", value: (row) => row._rank,' in script


def test_the_head_counts_filters_and_teardown():
    script = _script()
    # Plural counts: "4 values", "1 alternate part", "2 variant parameters".
    assert "plural(summary[kind], KIND_NOUN[kind])" in script
    # The "filtered" badge follows the kind chips while its disclosure is closed.
    assert "if (ui.filteredBadge) ui.filteredBadge.hidden = !view.kindsOn;" in script
    # After teardown no button asks anything, and the page says so.
    assert script.count("The view was closed: it asks the server nothing more.") == 2
    assert "if (view.closed || !canCall()) {\n      renderPane();\n      return;\n    }" in script
    # The details pane hands focus to the part's get_component button when the control
    # it was on is gone (Ask again, Retry).
    assert "Shell.keepFocus(ui.pane, drawPane, () => ui.askButton);" in script


def test_the_shell_counts_the_first_page_keeps_focus_and_measures_a_late_frame():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    # A budget can count the result's first page, which readPages does not: the run_erc
    # and compare_variants views read 60 pages in all, as their stop notice says (the DRC
    # view counts each first page itself).
    assert "function pageBudget(total, used = 0) {\n    return { total, left: total - used };" in shell
    for view in (uv.view_for("run_erc"), VIEW):
        assert "budget: Shell.pageBudget(MAX_PAGES, 1)," in _script(view), view.name
    drc = _script(uv.view_for("get_drc_report"))
    assert "budget.left -= 1;\n    if (progress) progress(1, 0);" in drc
    # keepFocus: a control that is gone hands focus to the fallback or its neighbour.
    assert "function keepFocus(container, rebuild, fallback = null) {" in shell
    assert "return at >= 0 && shown.length ? shown[Math.min(at, shown.length - 1)] : null;" in shell
    assert 'container.querySelectorAll("button, input, select, summary")' in shell
    # A frame with no size yet is measured again on a timer, so size-changed is sent.
    assert "if (!width || !height) {" in shell and "setTimeout(queueMeasure, Math.min(1000, 80 * unsized));" in shell


def test_the_page_keeps_focus_and_fits_the_host():
    script = _script()
    assert "function closeDetails()" in script and 'text: "Close", on: { click: closeDetails }' in script
    assert "Shell.focusMark(app)" in script and "Shell.refocus(app, mark" in script
    assert '"aria-live"' not in script and "Shell.announce(" in script
    # The picker and the filters fold away on a short or narrow host: unless the table
    # (and the details pane stacked under it at 980 px or less) keep about five rows.
    assert "view.pickerOpen = roomy;" in script and "view.filtersOpen = roomy;" in script
    assert "const roomy = roomyHere();" in script
    assert "return !limit || limit >= (window.innerWidth > 980 ? 660 : 820);" in script
    assert "@media (max-width: 980px)" in (UI / "shell.css").read_text(encoding="utf-8")
    # Long variant names wrap in the a and b headers and the details' labels.
    assert ".tbl th.c-val { white-space: normal; }" in _page()
    assert ".pane dl.facts { grid-template-columns: fit-content(45%) minmax(0, 1fr); }" in _page()
    # A short host 980 px wide or less: a selected part's details take the table's place
    # (focus goes to them, and back to the row on Close or Esc).
    assert "@media (max-width: 980px) {\n  .split.solo > .content { display: none; }\n}" in _page()
    assert 'ui.split.classList.toggle("solo", rows.length > 0 && compactHost());' in script
    assert "return window.innerWidth <= 980 && limit > 0 && limit < 820;" in script
    assert 'ui.split.classList.contains("solo") && ui.closeButton) ui.closeButton.focus();' in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


# ---------- tool results unchanged ----------

def test_text_results_are_unchanged_through_the_protocol(sl1):
    tools = _tools()
    for name in ("compare_variants", "list_variants"):
        assert tools[name]["outputSchema"]["properties"] == {
            "result": {"title": "Result", "type": "string"}}
    calls = [("list_variants", {}), ("compare_variants", {}),
             ("compare_variants", {"a": "Debug", "b": "Production"}),
             ("compare_variants", {"a": "Bare", "offset": 100}),
             ("compare_variants", {"a": "Nope"})]
    for name, args in calls:
        direct = getattr(main_mod, name)(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], (name, args)
        assert result["structuredContent"] == {"result": direct} and not result.get("isError")


# ---------- docs ----------

def test_readme_and_changelog_describe_the_variant_view():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `compare_variants` |" in section
    assert "does not switch the active variant" in section
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #61" in unreleased
