"""The BOM view (roadmap #62, server/ui/bom.html): its ui:// resource and get_bom's link
(check_bom and get_active_bom link none and only feed the view through tools/call), the
self-contained page and the tools it calls, its data contract against get_bom's,
check_bom's, get_active_bom's and list_variants' output on Altium's public examples with an
ActiveBOM document (Mini PC Motherboard and WiFi, a Harness Demo board), the repo's
synthetic Demo and ActiveBOM fixtures, a copy of the public SpiritLevel example with
variants added and a synthetic board (lines whose parts disagree, placeholders, a
mechanical part, sheet designators), paging the way the view pages, check_bom's findings
mapped to the lines by designator, the missing-MPN rule, a line's BomDoc solutions asked
designator by designator, and unchanged text results."""

import asyncio
import json
import re
from functools import cache
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.active_bom as ab
import server.services.ui_views as uv
from server.parsers.prj_pcb import VariantDefinition, VariantState, Variation, parse_prj_pcb
from server.services.bom import _UNDECIDED_MPN, BASE_VARIANT
from server.services.bom_checks import BOMDOC_CHECKS, CHECKS
from tests.test_active_bom import abom_project  # noqa: F401
from tests.test_active_bom import bom_doc, choice, component, item
from tests.test_ui_variants import demo, sl1  # noqa: F401

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
VIEW = uv.view_for("get_bom")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
# The tools the BOM page calls: get_bom (paging, the variant switch, grouping, the BomDoc
# columns, the Not Fitted lines, the variant comparison), check_bom (the findings),
# get_active_bom (a line's ranked solutions), list_variants (the switch, the active
# variant's name) and get_component (a part, as the active variant shows it).
CALLED = {"get_bom", "check_bom", "get_active_bom", "list_variants", "get_component"}
ARG_KEYS = ("variant", "group_by", "include_not_fitted", "compare_variants", "active_bom")
BOM_DOC_EXAMPLES = [
    ("Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb"),
    ("Mini PC", "Mini PC - WiFi", "WiFi_miniPCIe.PrjPcb"),
    ("Harness Demo MB", "Harness_Demo_PCB_A", "Harness_Demo_PCB_A.PrjPcb"),
]


def _handle(request):
    handler = main_mod.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


def _page() -> str:
    return (UI / VIEW.file).read_text(encoding="utf-8")


def _script() -> str:
    return _page().split(uv.JS_MARKER, 1)[1]


def _contract() -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>',
                      _page(), re.S)
    return json.loads(block.group(1))


def _rows_at(data, path: str) -> list:
    """The rows a contract path names, read as the view's rowsAt does."""
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


# ---------- reading the tools the way the page does ----------

def _clean(args: dict) -> dict:
    """The page's cleanArgs: get_bom's arguments it keeps, false and empty left out."""
    return {key: args[key] for key in ARG_KEYS if args.get(key) not in (None, False, "")}


def _read_bom(**args) -> dict:
    """get_bom read the way the view reads it: the result, again as JSON from offset 0 when
    it is CSV or a later page, then every next_offset with the same arguments, each page
    checked to be one of the same BOM; the lines joined (_pages: the pages read)."""
    first = json.loads(main_mod.get_bom(**args))
    if "error" in first:
        return first
    asked = _clean(args)
    if "csv" in first or first["offset"] > 0:
        again = json.loads(main_mod.get_bom(**asked, offset=0))
        assert (again["variant"], again["lines_total"]) == (first["variant"], first["lines_total"])
        first = again
    lines, page, pages = list(first["lines"]), first, 1
    while page["has_more"]:
        page = json.loads(main_mod.get_bom(**asked, offset=page["next_offset"]))
        assert (page["variant"], page["lines_total"]) == (first["variant"], first["lines_total"])
        lines += page["lines"]
        pages += 1
    return {**first, "lines": lines, "_pages": pages}


def _read_checks(variant: str) -> dict:
    """check_bom for the variant shown, every page (each one of the same findings)."""
    first = json.loads(main_mod.check_bom(variant=variant, offset=0))
    findings, page = list(first["findings"]), first
    while page["has_more"]:
        page = json.loads(main_mod.check_bom(variant=variant, offset=page["next_offset"]))
        assert (page["variant"], page["total"]) == (first["variant"], first["total"])
        findings += page["findings"]
    return {**first, "findings": findings}


def _map(rows: list[dict], findings: list[dict]) -> tuple[list[list[int]], list[list[str]]]:
    """The page's mapFindings: each finding's lines (by designator, exact then upper case;
    a duplicate_designator's sheet designator through the lines' logical_designators) and
    its designators on no line."""
    lines, unmapped, _ = _map_all(rows, findings)
    return lines, unmapped


def _map_all(rows: list[dict], findings: list[dict]):
    """_map with each finding's lines it may be on (a sheet designator that names both a
    part and other parts' sheet designator: not marked)."""
    exact, upper, by_sheet = {}, {}, {}
    for index, row in enumerate(rows):
        for designator in row["designators"]:
            exact[designator] = index
            upper.setdefault(designator.upper(), index)
        for logical in (row.get("logical_designators") or {}).values():
            by_sheet.setdefault(logical.upper(), [])
            if index not in by_sheet[logical.upper()]:
                by_sheet[logical.upper()].append(index)

    def physical(designator):
        index = exact.get(designator, upper.get(designator.upper()))
        if index is None:
            return None
        own = {name.upper(): logical for name, logical in (rows[index].get("logical_designators") or {}).items()}
        logical = own.get(designator.upper())
        return None if logical is not None and logical.upper() != designator.upper() else index

    lines, unmapped, maybe = [], [], []
    for finding in findings:
        mine, gone, may = [], [], []
        for designator in finding.get("designators") or []:
            if finding["check"] == "duplicate_designator":
                sheet = by_sheet.get(designator.upper(), [])
                named = physical(designator)
                if sheet and named is not None and named not in sheet:
                    may += [index for index in [named, *sheet] if index not in may]
                    continue
                found = sheet or ([] if named is None else [named])
            else:
                index = exact.get(designator, upper.get(designator.upper()))
                found = [] if index is None else [index]
            if not found:
                gone.append(designator)
            mine += [index for index in found if index not in mine]
        lines.append(mine)
        unmapped.append(gone)
        maybe.append(may)
    return lines, unmapped, maybe


def _missing_from_checks(checks: dict | None) -> tuple[set[str] | None, set[str]]:
    """The page's missingFromChecks: (exact: every part that needs an MPN and has none, or
    None; listed: the parts check_bom's missing_mpn names, each such a part)."""
    if checks is None:
        return None, set()
    findings = [finding for finding in checks["findings"] if finding["check"] == "missing_mpn"]
    listed = {designator.upper() for finding in findings for designator in finding.get("designators") or []}
    exact = ("missing_mpn" in checks["checks_run"] and "bom_doc" not in checks
             and "bom_doc_error" not in checks and all(finding.get("designators") for finding in findings))
    return (listed if exact else None), listed


def _missing(row: dict, coverage, checks: tuple = (None, frozenset())) -> str:
    """The page's missingMpn for a fitted line: "yes", "may" (its data cannot tell) or ""."""
    if coverage is None:
        return ""
    exact, listed = checks
    names = {designator.upper() for designator in row["designators"]}
    if exact is not None:
        return "yes" if names & exact else ""
    if names & listed:
        return "yes"
    varies = (row.get("varies") or {}).get("mpn")
    if row.get("mpn"):
        counted = (row.get("varies_count") or {}).get("mpn")
        if varies and (None in varies or (counted and counted > len(varies))):
            return "may"
        return ""
    if not row.get("mpn_placeholder"):
        return "yes"
    marks = [text.strip().lower() for text in row["mpn_placeholder"].split(" / ")]
    if any(text in _contract()["mpn_undecided"] for text in marks):
        return "yes"
    return "may" if row["quantity"] > len(marks) else ""


def _truth(variant: str | None) -> tuple[set[str], set[str]]:
    """(the fitted parts that need an MPN and have none, the mechanical ones without an
    MPN), from the parts get_bom builds."""
    project, netlist, state = main_mod._require_project()
    plan, _, _ = main_mod._bom_plan(netlist, state, variant)
    parts, _ = main_mod.build_bom_parts(netlist, plan, project.get("parameters") or {})
    fitted = [part for part in parts if part.fitted]
    needing = {part.designator for part in fitted if main_mod.needs_mpn(part) and not part.mpn}
    mechanical = {part.designator for part in fitted if part.kind == 1 and not part.mpn}
    return needing, mechanical


def _without_line(checks: dict) -> set[str]:
    """The page's partsWithoutLine: the parts check_bom's bomdoc_out_of_sync names as
    having no BomDoc line (upper case)."""
    return {designator.upper() for finding in checks["findings"]
            if finding["check"] == "bomdoc_out_of_sync" and finding.get("reason") == "parts_without_line"
            for designator in finding.get("designators") or []}


def _walk_all(line: dict, variant: str, skip=frozenset(), budget: int | None = None):
    """The page's askSolutions: get_active_bom for the line's first designator, then the
    first one no answer covers (nor skip names), until every designator is covered, a call
    is neither a line nor line_not_found, or budget calls were made. (answers, the parts
    get_active_bom puts on no line, the parts not asked)."""
    designators = line["designators"]
    covered, answers, no_line, asked = set(), [], [], 0

    def left():
        return [d for d in designators if d.upper() not in covered and d.upper() not in skip]

    while budget is None or asked < budget:
        nxt = next(iter(left()), None)
        if nxt is None:
            break
        result = json.loads(main_mod.get_active_bom(line=nxt, variant=variant))
        asked += 1
        covered.add(nxt.upper())
        if "line" in result:
            covered |= {d.upper() for d in result["line"]["designators"] + result["line"].get("not_fitted", [])}
            answers.append(result)
        elif result.get("error") == "line_not_found":
            no_line.append(nxt)
        else:
            break
    return answers, no_line, left()


def _walk(line: dict, variant: str, skip=frozenset()) -> list[dict]:
    return _walk_all(line, variant, skip)[0]


# ---------- projects ----------

@cache
def _compiled(path: Path):
    client = main_mod.AltiumClient()
    client.compile_offline(str(path))
    return parse_prj_pcb(str(path)), client


def _example(monkeypatch, *parts: str) -> Path:
    path = EXAMPLES.joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"{path.name} is not installed")
    prj, client = _compiled(path)
    monkeypatch.setattr(main_mod, "_project", {
        "name": path.stem, "root_dir": str(path.parent), "prj_pcb_path": str(path),
        "sheets": [], "parameters": prj.parameters, "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod, "_altium", client)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(prj.variants))
    return path


def _board_netlist(extra: int = 0) -> dict:
    """Made-up parts: a line whose parts have seven descriptions (five listed), one whose
    parts have two MPNs (grouped by Comment), 'N/A' and 'TBD' part numbers, a mechanical
    part without one, a Part Number parameter, channel copies with sheet designators, an
    MPN without a manufacturer, a no-BOM part; extra adds that many one-part lines."""
    components, parts = {}, {}

    def add(name, comment, footprint, kind=0, description=None, logical=None, **params):
        components[name] = {"unique_id": f"U{name}", "unique_ids": [f"U{name}"], "kind": kind,
                            "sheet": "Main", "description": description,
                            "params": {"comment": comment, **params}, "lib_reference": "LIB",
                            "footprint": footprint, "pins": {}}
        if logical:
            components[name]["logical_refdes"] = logical
        parts[name] = {"footprint": footprint, "comment": comment}

    for index in range(1, 9):
        add(f"R{index}", "10k", "R0603", value="10k",
            description="Resistor 10k" if index < 6 else "Thick film 10k",
            **{"manufacturer 1": "Yageo",
               "manufacturer part number 1": "RES-10K-A" if index < 7 else "RES-10K-B"})
    for index in range(1, 8):
        add(f"C{index}", "10u", "C0805", value="10u", description=f"Capacitor 10u note {index}",
            **{"manufacturer 1": "Maker", "manufacturer part number 1": "CAP-10U"})
    for index in range(1, 4):
        add(f"TP{index}", "TP", "TP1MM", description="Test point", **{"manufacturer part number 1": "N/A"})
    add("U1", "LDO 3V3", "SOTX-5", description="LDO regulator", **{"manufacturer part number 1": "TBD"})
    add("HS1", "Heatsink", "HS-TO220", kind=1, description="Heatsink")
    add("J1", "USB-C", "USBC-16", description="USB-C receptacle", **{"part number": "CONN-USBC", "manufacturer": "Maker"})
    for name in ("C101", "C201"):
        add(name, "100n", "C0402", value="100n", description="Capacitor 100n", logical="C11",
            **{"manufacturer 1": "Maker", "manufacturer part number 1": "CAP-100N"})
    add("D1", "LED red", "LED0603", description="LED", **{"manufacturer part number 1": "LED-RED"})
    add("MH1", "MH", "MH3", kind=5, description="Mounting hole")
    for index in range(extra):
        # A Value naming no parameter: one check_bom finding per part.
        add(f"R{100 + index}", f"{100 + index}R", "R0402", value="=Nope",
            description="Resistor " + "thick film " * 8,
            **{"manufacturer 1": "Maker", "manufacturer part number 1": f"RES-{100 + index}"})
    return {"nets": {}, "pin_to_net": {}, "parts": parts, "components": components}


def _load_board(monkeypatch, tmp_path, netlist: dict) -> VariantState:
    full = VariantDefinition(name="Full")
    lite = VariantDefinition(name="Lite", variations=[
        Variation(designator="R8", unique_id="", kind=1), Variation(designator="TP3", unique_id="", kind=1),
        Variation(designator="R1", unique_id="", kind=0)],
        param_variations={"R1": {"comment": ("Comment", "4k7")}})
    state = VariantState([full, lite])
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text("[Design]\nVersion=1.0\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Board", "root_dir": str(tmp_path), "prj_pcb_path": str(prj),
        "sheets": [], "pcb_doc_paths": [], "parameters": {}})
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", state)
    return state


@pytest.fixture
def board(monkeypatch, tmp_path):
    return _load_board(monkeypatch, tmp_path, _board_netlist())


def _names(state) -> list[str]:
    return [variant.name for variant in state.variants] + [BASE_VARIANT]


def _contract_holds(variant: str | None, **args) -> dict:
    """get_bom (all pages) and check_bom (all pages) for a variant: both contracts hold."""
    contract = _contract()
    extra = {} if variant is None else {"variant": variant}
    data = _read_bom(**extra, **args)
    assert "error" not in data, (variant, data)
    assert _check(contract["shapes"], data) == [], (variant, args)
    assert len(data["lines"]) == data["lines_total"]
    checks = _read_checks(data["variant"])
    assert _check(contract["check_bom"], checks) == [], variant
    assert len(checks["findings"]) == checks["total"]
    return data


# ---------- resources and links ----------

def test_the_view_is_a_listed_resource_linked_from_get_bom():
    assert VIEW.uri == "ui://altium-copilot-kc/bom.html" and VIEW.file == "bom.html"
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
    assert tools["get_bom"]["_meta"] == {
        "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
        "ui": {"resourceUri": VIEW.uri}, "ui/resourceUri": VIEW.uri}
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)


def test_check_bom_and_get_active_bom_link_no_view_and_feed_the_bom_view():
    # The registry decision: check_bom's findings read best on the BOM lines (the view asks
    # it through tools/call), get_active_bom gives a line's solutions in the details pane.
    tools = _tools()
    for name in ("check_bom", "get_active_bom"):
        assert tools[name]["_meta"] == {
            "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS}, name
        with pytest.raises(KeyError):
            uv.view_for(name)
    script = _script()
    assert 'call("check_bom", { variant, offset: 0 })' in script
    assert 'details.load("get_active_bom", args)' in script
    doc = (ROOT / "server" / "services" / "ui_views.py").read_text(encoding="utf-8")
    assert "The BOM view (roadmap #62) is linked from get_bom only." in doc
    assert "check_bom has no page of its own" in doc and "get_active_bom links no view" in doc


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
                         r"indexedDB|cookie|innerHTML|outerHTML|insertAdjacentHTML|eval|href)\b",
                         scripts)
    assert page.count("const Shell = (() => {") == 1
    assert f'<meta http-equiv="Content-Security-Policy" content="{uv.PAGE_CSP}">' in page


def test_the_page_calls_only_its_five_tools_through_one_guard():
    script = _script()
    tools = _tools()
    named = {name for name in re.findall(r'"([a-z_]+)"', script) if name in tools}
    # compare_variants is named as get_bom's argument, never called.
    assert named - {"compare_variants"} == CALLED
    assert "compare_variants" not in set(re.findall(r'(?:callTool|call|ask|load)\(\s*"([a-z_]+)"', script))
    allowed = re.search(r"const READ_ONLY_TOOLS = new Set\(\[(.*?)\]\);", script).group(1)
    assert set(re.findall(r'"([a-z_]+)"', allowed)) == CALLED
    for name in CALLED:
        assert tools[name]["annotations"]["readOnlyHint"] is True
    assert script.count("Shell.callTool(") == 1
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in script
    assert "if (!READ_ONLY_TOOLS.has(tool)) return;\n" in script
    assert "if (view.busy || !canCall() || view.closed) return;" in script
    assert "if (!canCall() || view.closed) return Promise.resolve();" in script
    assert script.count("Shell.readPages(") == 2 and script.count("closed: () => view.closed") == 2
    # The switch only shows another variant, and the page says so.
    assert "Showing another variant does not switch the active variant" in script
    assert "never set_active_variant" in script and '"set_active_variant"' not in _page()


# ---------- the data contract ----------

def _sources() -> str:
    paths = [ROOT / "server" / "main.py", ROOT / "server" / "services" / "bom.py",
             ROOT / "server" / "services" / "bom_checks.py",
             ROOT / "server" / "services" / "active_bom.py"]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_the_contract_names_keys_the_tools_produce():
    contract = _contract()
    assert (contract["tool"], contract["checks_tool"], contract["solutions_tool"], contract["picker"],
            contract["paged"]) == ("get_bom", "check_bom", "get_active_bom", "list_variants", "lines")
    sources = _sources()
    for shapes in (contract["shapes"], contract["check_bom"], contract["get_active_bom"],
                   contract["list_variants"]):
        for path, shape in shapes.items():
            for key in shape["required"] + shape["optional"]:
                # A finding's fields are _finding() keyword arguments; check_bom adds a
                # BomDoc finding's lines to an MPN finding as f"bom_doc_{key}".
                spelled = f'"{key}"' in sources or re.search(rf"\b{key}=", sources)
                prefixed = key.startswith("bom_doc_") and 'f"bom_doc_{key}"' in sources
                assert spelled or prefixed, f"{path}: {key}"


def test_the_view_reads_the_keys_its_contract_names():
    script = _script()
    contract = _contract()
    for shapes in (contract["shapes"], contract["check_bom"], contract["get_active_bom"],
                   contract["list_variants"]):
        for path, shape in shapes.items():
            for key in shape["required"] + shape["optional"]:
                assert re.search(rf'\.{key}\b|"{key}"', script), f"{path}: {key}"
    # The view checks every tool's answer against its contract at runtime.
    for call in ("checkContract(CONTRACT.shapes, { ...first, lines })",
                 "checkContract(CONTRACT.check_bom, { ...first, findings })",
                 "checkContract(CONTRACT.get_active_bom, json)",
                 "checkContract(CONTRACT.list_variants, listing)"):
        assert call in script, call


def test_the_contracts_constants_are_the_servers():
    contract = _contract()
    assert contract["base"] == BASE_VARIANT
    assert contract["base_names"] == list(main_mod.BOM_BASE_NAMES)
    assert set(contract["mpn_undecided"]) == _UNDECIDED_MPN
    assert list(contract["checks"]) == list(CHECKS + BOMDOC_CHECKS)
    assert contract["bom_doc_checks"] == list(BOMDOC_CHECKS)
    # check_bom lists a not_compiled finding's first 30 parts, a BomDoc finding's first 50.
    assert contract["designators_listed"] == {"not_compiled": 30, "bom_doc": ab.JOIN_LISTED}
    checks = (ROOT / "server" / "services" / "bom_checks.py").read_text(encoding="utf-8")
    assert "uncompiled[:30], count=len(uncompiled)" in checks


@pytest.mark.parametrize("parts", BOM_DOC_EXAMPLES, ids=[parts[-1] for parts in BOM_DOC_EXAMPLES])
def test_the_contracts_hold_on_the_public_bom_doc_examples(monkeypatch, parts):
    _example(monkeypatch, *parts)
    contract = _contract()
    listing = json.loads(main_mod.list_variants())
    assert _check(contract["list_variants"], listing) == []
    for args in ({}, {"active_bom": True}, {"group_by": "comment", "active_bom": True},
                 {"compare_variants": True}):
        data = _contract_holds(None, **args)
    assert data["variant"] == listing["active_variant"]
    rich = _read_bom(active_bom=True)
    assert rich["bom_doc"]["file"].endswith(".BomDoc")
    checks = _read_checks(rich["variant"])
    assert "bom_doc" in checks and set(BOMDOC_CHECKS) <= set(checks["checks_run"])
    # A line's BomDoc lines, asked designator by designator, are the ones get_bom names
    # (every line of the smaller boards, a sample of Mini PC's), and every part of the line
    # is on one of them or said to be on none.
    skip = _without_line(checks)
    lines = [line for line in rich["lines"] if line["bom_doc_lines"]]
    sample = lines if len(lines) <= 20 else lines[::7]
    for line in sample:
        answers, no_line, left = _walk_all(line, rich["variant"], skip)
        assert {answer["line"]["line"] for answer in answers} == set(line["bom_doc_lines"]), line["designators"]
        held = {d.upper() for answer in answers for d in answer["line"]["designators"]}
        assert not left and {d.upper() for d in line["designators"]} <= held | set(skip) | {d.upper() for d in no_line}
        for answer in answers:
            assert _check(contract["get_active_bom"], answer) == []
            # Cached supply figures come with their cache dates.
            for solution in answer["line"].get("ranked_solutions", []):
                if "stock_total" in solution or "best_unit_price" in solution:
                    assert "cache_date" in solution or not solution.get("part_choices") or all(
                        "cache_date" not in choice for choice in solution["part_choices"])
            assert answer["cache"]["note"] == ab.CACHE_NOTE


def test_the_mini_pc_bom_has_several_pages_and_unnumbered_harness_lines(monkeypatch):
    _example(monkeypatch, "Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb")
    first = json.loads(main_mod.get_bom())
    assert first["has_more"] and first["lines_total"] > 100
    data = _read_bom()
    assert data["_pages"] >= 2 and len(data["lines"]) == data["lines_total"]
    _example(monkeypatch, "Harness Demo MB", "Harness_Demo_PCB_A", "Harness_Demo_PCB_A.PrjPcb")
    harness = _read_bom(active_bom=True)
    assert any(None in line["bom_doc_lines"] for line in harness["lines"])
    script = _script()
    assert '(number === null ? "unnumbered" : String(number))' in script


def test_the_contracts_hold_on_the_demo_and_the_active_bom_fixture(demo, abom_project, monkeypatch, tmp_path):
    # abom_project loads last: the repo's ActiveBOM fixture, a Lite variant with R2 Not Fitted.
    contract = _contract()
    for variant in ["Lite", BASE_VARIANT]:
        for args in ({}, {"include_not_fitted": True, "active_bom": True}, {"compare_variants": True}):
            data = _contract_holds(variant, **args)
        assert _check(contract["list_variants"], json.loads(main_mod.list_variants())) == []
        rich = _read_bom(variant=variant, active_bom=True)
        for line in rich["lines"]:
            for answer in _walk(line, rich["variant"]):
                assert _check(contract["get_active_bom"], answer) == []
                assert answer["variant"] == rich["variant"]
    lite = _read_bom(variant="Lite", include_not_fitted=True)
    assert [line["designators"] for line in lite["not_fitted"]] == [["R2"]]
    assert data["variants"] == [{"variant": "Lite", "not_fitted": ["R2"], "changed": []}]


def test_the_contracts_hold_on_a_public_example_with_variants(sl1):
    contract = _contract()
    kinds = set()
    for variant in _names(sl1):
        data = _contract_holds(variant, include_not_fitted=True, compare_variants=True)
        if data["parts"]["not_fitted"]:
            assert sum(line["quantity"] for line in data["not_fitted"]) == data["parts"]["not_fitted"]
        kinds |= {key for line in data["lines"] for key in line}
        no_doc = json.loads(main_mod.get_bom(variant=variant, active_bom=True))
        assert no_doc["bom_doc_error"]["error"] == "no_bom_doc"
        assert _check(contract["shapes"], no_doc) == []
        assert "bom_doc" not in _read_checks(data["variant"])
    # Not Fitted parts, varied values, an alternate part with its library copy.
    assert {"alternate_parts", "varied_parameters"} <= kinds
    production = _read_bom(variant="Production")
    assert production["alternate_parts_note"] and production["mpn_coverage_percent"] <= 1
    debug = _read_bom(variant="Debug")
    assert debug["stale_variant_entries"] == 2 and debug["mpn_coverage_percent"] == 0
    script = _script()
    assert "stale(data.stale_variant_entries)" in script and "data.alternate_parts_note" in script
    # The switch offers names get_bom takes.
    for name in [row["name"] for row in json.loads(main_mod.list_variants())["variants"]] + [BASE_VARIANT]:
        assert "error" not in json.loads(main_mod.get_bom(variant=name))


def test_the_contracts_hold_on_a_synthetic_board(board):
    contract = _contract()
    for variant in _names(board):
        for args in ({}, {"group_by": "comment", "include_not_fitted": True}):
            _contract_holds(variant, **args)
    by_comment = _read_bom(variant="Full", group_by="comment")
    lines = {line["designators"][0]: line for line in by_comment["lines"]}
    assert lines["C1"]["varies"]["description"][:2] == ["Capacitor 10u note 1", "Capacitor 10u note 2"]
    assert lines["C1"]["varies_count"] == {"description": 7} and len(lines["C1"]["varies"]["description"]) == 5
    assert lines["R1"]["varies"]["mpn"] == ["RES-10K-A", "RES-10K-B"]
    assert lines["C101"]["logical_designators"] == {"C101": "C11", "C201": "C11"}
    assert lines["TP1"]["mpn_placeholder"] == "N/A" and lines["U1"]["mpn_placeholder"] == "TBD"
    assert lines["J1"]["mpn_source"] == "part number"
    script = _script()
    assert "(the first ${values.length} of ${count})" in script
    assert 'row.mpn_source === "part number" ? "a Part Number parameter" : null' in script
    assert "`${designator} (sheet ${logical[designator]})`" in script
    assert _check(contract["list_variants"], json.loads(main_mod.list_variants())) == []


def test_no_part_needing_an_mpn_and_no_mpn_parameters_at_all(monkeypatch, tmp_path):
    netlist = _board_netlist()
    for name, meta in netlist["components"].items():
        meta["params"] = {"comment": meta["params"]["comment"]}
        if name.startswith("TP"):
            meta["params"]["manufacturer part number 1"] = "N/A"
    _load_board(monkeypatch, tmp_path, netlist)
    none = _read_bom(variant="Full")
    assert none["mpn_coverage_percent"] == 0 and not any("mpn" in line for line in none["lines"])
    needing, _ = _truth("Full")
    checks = _missing_from_checks(_read_checks("Full"))
    assert checks[0] is None and not checks[1]  # under half the parts with an MPN: a summary
    assert {d for line in none["lines"] if _missing(line, 0, checks) for d in line["designators"]} >= needing
    for meta in netlist["components"].values():
        meta["kind"] = 1 if meta["kind"] == 0 else meta["kind"]
    nothing = _read_bom(variant="Full")
    assert nothing["mpn_coverage_percent"] is None
    assert not any(_missing(line, None) for line in nothing["lines"])
    script = _script()
    assert "if (coverage === null || coverage === undefined) return no;" in script
    assert 'text: data.mpn_coverage_percent === null ? "no MPN needed"' in script


# ---------- paging ----------

def test_paging_as_the_view_does_gives_every_line_and_finding_once(monkeypatch, tmp_path):
    _load_board(monkeypatch, tmp_path, _board_netlist(extra=230))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 10 ** 9)
    # The whole BOM and findings in 100-line (100-finding) pages, nothing trimmed.
    every = ((), (("group_by", "comment"),), (("variant", "Lite"), ("include_not_fitted", True)))
    whole = {args: _read_bom(**dict(args)) for args in every}
    checks_whole = _read_checks("Full")
    assert all(data["_pages"] == 3 for data in whole.values())
    assert checks_whole["total"] > 200 and len(checks_whole["findings"]) == checks_whole["total"]
    assert json.loads(main_mod.check_bom(variant="Full"))["has_more"]
    for limit in (30000, 9000):
        # Pages cut to fit the size limit: the view follows next_offset all the same.
        monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", limit)
        for args, full in whole.items():
            data = _read_bom(**dict(args))
            assert data["lines"] == full["lines"], (limit, args)
            keys = [json.dumps(line, sort_keys=True) for line in data["lines"]]
            assert len(set(keys)) == len(keys) == data["lines_total"]
            assert data["_pages"] > 3
        checks = _read_checks("Full")
        assert checks["findings"] == checks_whole["findings"]
        ids = [json.dumps(finding, sort_keys=True) for finding in checks["findings"]]
        assert len(set(ids)) == len(ids)
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    assert json.loads(main_mod.get_bom())["lines_limited_to"] in (40, 15)
    script = _script()
    assert "pageOffset: (page) => (page.has_more && page.next_offset !== undefined ? page.next_offset : null)" in script
    assert "getPage: (offset) => bomPage(asked, offset, first)," in script
    assert "getPage: (offset) => checkPage(variant, offset, first)," in script
    assert "page.variant !== expect.variant || page.lines_total !== expect.lines_total" in script
    assert "page.variant !== first.variant || page.total !== first.total" in script
    assert script.count("budget: Shell.pageBudget(MAX_PAGES, 1),") == 2


def test_a_later_page_or_a_csv_result_is_read_again_from_offset_0(monkeypatch, tmp_path):
    _load_board(monkeypatch, tmp_path, _board_netlist(extra=230))
    later = json.loads(main_mod.get_bom(offset=100))
    assert later["offset"] == 100 and "lines" in later
    assert _read_bom(offset=100)["lines"] == _read_bom()["lines"]
    csv = json.loads(main_mod.get_bom(format="csv"))
    assert "csv" in csv and "lines" not in csv
    assert _read_bom(format="csv")["lines"] == _read_bom()["lines"]
    script = _script()
    assert "} else if (first.offset > 0 && canCall() && !view.closed) {" in script
    assert "first = await bomPage(asked, 0, first);" in script
    assert "this result starts at offset ${first.offset}" in script
    assert 'if (!Array.isArray(first.lines) && typeof first.csv === "string") {' in script
    assert "drawCsv(first, asked);" in script
    # Without the call's arguments (they say how lines were grouped), the pages are read
    # again from offset 0 with group_by mpn, a CSV result asked again for JSON so too, and
    # labelled so (one read from offset 0, not two); if that read fails, only the page
    # given is shown, with the reason.
    assert script.count('const json = { ...asked, group_by: "mpn" };') == 1
    assert 'const json = groupKnown ? asked : { ...asked, group_by: "mpn" };' in script
    assert "the view asked get_bom again for JSON with group_by mpn (the tool's default" in script
    assert "pageOn = false;" in script and "pageOffset: (page) => (pageOn && page.has_more" in script
    # The Not Fitted lines are asked with the grouping the lines were drawn with.
    assert 'group_by: data.groupKnown ? data.asked.group_by : "mpn" });' in script
    # Why the grouping matters: a CSV grouped by Comment, asked again without its
    # arguments, gives other lines (get_bom's default, MPN), which the view labels so.
    csv = json.loads(main_mod.get_bom(format="csv", group_by="comment"))
    again = _read_bom(variant=csv["variant"], group_by="mpn")
    assert again["lines"] == _read_bom()["lines"]
    assert again["lines_total"] != csv["lines_total"] == _read_bom(group_by="comment")["lines_total"]


# ---------- findings, missing MPNs, BomDoc solutions ----------

def test_findings_map_to_the_lines_by_designator(board, abom_project):
    for variant in ("Lite", BASE_VARIANT):
        data = _read_bom(variant=variant, include_not_fitted=True)
        rows = data["lines"] + data.get("not_fitted", [])
        checks = _read_checks(data["variant"])
        lines, unmapped = _map(rows, checks["findings"])
        for finding, mine, gone in zip(checks["findings"], lines, unmapped):
            named = finding.get("designators") or []
            # Every part a finding names is on exactly one line, or on none (gone).
            assert len(named) == sum(1 for d in named if d in gone) + sum(
                1 for d in named if any(d in rows[index]["designators"] for index in mine))
            for designator in gone:
                assert not any(designator.upper() == d.upper() for row in rows for d in row["designators"])
        assert any(lines), variant
    # abom_project loaded last: J1 has no BomDoc line, C1/C2 an NRND alternate.
    data = _read_bom(active_bom=True)
    checks = _read_checks(data["variant"])
    lines, _ = _map(data["lines"], checks["findings"])
    by_check = {}
    for finding, mine in zip(checks["findings"], lines):
        by_check.setdefault(finding["check"], set()).update(
            data["lines"][index]["designators"][0] for index in mine)
    assert by_check["lifecycle_risk"] == {"C1", "U1"} and "J1" in by_check["missing_mpn"]
    script = _script()
    assert "const row = byName.get(String(designator)) || upper.get(String(designator).toUpperCase());" in script
    assert "if (!row._findings.includes(finding)) row._findings.push(finding);" in script


def _sheet_netlist(with_top: bool) -> dict:
    """Made-up channel copies R1_CH1/R1_CH2 (sheet designator R1 on sheet Chan) whose sheet
    designator clashes there, optionally a part named R1 on sheet Top, and R5."""
    components = {}

    def add(name, comment, sheet, logical=None):
        components[name] = {"unique_id": f"U{name}", "unique_ids": [f"U{name}"], "kind": 0, "sheet": sheet,
                            "description": "Resistor", "lib_reference": "LIB", "footprint": "R0603", "pins": {},
                            "params": {"comment": comment, "manufacturer 1": "Maker",
                                       "manufacturer part number 1": f"RES-{comment}"}}
        if logical:
            components[name]["logical_refdes"] = logical

    add("R1_CH1", "1k", "Chan", "R1")
    add("R1_CH2", "1k", "Chan", "R1")
    if with_top:
        add("R1", "2k", "Top")
    add("R5", "5k", "Top")
    parts = {name: {"footprint": "R0603", "comment": meta["params"]["comment"]} for name, meta in components.items()}
    return {"nets": {}, "pin_to_net": {}, "parts": parts, "components": components,
            "designator_clashes": [{"refdes": "R1", "sheet": "Chan", "lib_references": ["A", "B"]}]}


@pytest.mark.parametrize("with_top", [False, True])
def test_a_duplicate_sheet_designator_maps_through_the_lines_sheet_designators(monkeypatch, tmp_path, with_top):
    _load_board(monkeypatch, tmp_path, _sheet_netlist(with_top))
    data = _read_bom(variant="Full")
    checks = _read_checks("Full")
    (duplicate,) = [finding for finding in checks["findings"] if finding["check"] == "duplicate_designator"]
    assert duplicate["designators"] == ["R1"] and "sheet Chan" in duplicate["issue"]
    lines, unmapped, maybe = _map_all(data["lines"], [duplicate])
    channels = next(index for index, line in enumerate(data["lines"]) if "R1_CH1" in line["designators"])
    if with_top:
        # A part named R1 (on another sheet) and the channel copies called R1 on theirs:
        # get_bom's lines give no sheet, so no line is marked and both are listed.
        top = next(index for index, line in enumerate(data["lines"]) if line["designators"] == ["R1"])
        assert lines == [[]] and unmapped == [[]] and set(maybe[0]) == {top, channels}
    else:
        assert lines == [[channels]] and unmapped == [[]] and maybe == [[]]
    # Other findings name physical designators: R1 maps to the part named R1.
    if with_top:
        assert _map(data["lines"], [{"check": "no_footprint", "designators": ["R1"]}])[0] == [[top]]
    script = _script()
    assert 'if (finding.check === "duplicate_designator") {' in script
    assert "finding._maybe = maybe;" in script and "One of these lines, not marked:" in script


def test_the_missing_mpn_rule_is_mpn_coverage_percents(board):
    for variant in _names(board):
        data = _read_bom(variant=variant, group_by="comment")
        coverage = data["mpn_coverage_percent"]
        needing, mechanical = _truth(variant)
        checks = _missing_from_checks(_read_checks(data["variant"]))
        assert checks[0] == needing, variant  # no BomDoc, coverage over 50%: check_bom names them
        flagged = {line["designators"][0] for line in data["lines"] if _missing(line, coverage, checks)}
        truth = {line["designators"][0] for line in data["lines"]
                 if set(line["designators"]) & needing}
        assert flagged == truth
        assert all(_missing(line, coverage, checks) in ("yes", "") for line in data["lines"])
        # From the lines alone: every line that misses one is flagged or marked "may"; a
        # line flagged outright holds a part without one that is not counted only when
        # that part is mechanical, which the lines cannot tell.
        alone = {line["designators"][0]: _missing(line, coverage) for line in data["lines"]}
        yes = {first for first, state in alone.items() if state == "yes"}
        maybe = {first for first, state in alone.items() if state == "may"}
        assert truth <= yes | maybe
        assert yes - truth <= {line["designators"][0] for line in data["lines"] if set(line["designators"]) & mechanical}
    assert "U1" in truth and "TP1" not in truth and "HS1" in yes - truth and "TP1" in maybe
    script = _script()
    assert "const UNDECIDED = new Set(CONTRACT.mpn_undecided);" in script
    assert "if (checks.exact) return names.some((name) => checks.exact.has(name))" in script
    assert "!header.bom_doc &&\n      !header.bom_doc_error && findings.every(" in script


def _fallback_netlist() -> dict:
    """Under half the parts with an MPN (check_bom then names no part): R1-R8 with none,
    TP1 'N/A' and TP2 none (one line by Comment and footprint), C1 an MPN and C2 'N/A'
    (one line by Comment), L1-L6 six MPNs and L7 none (one line by Comment)."""
    components, parts = {}, {}

    def add(name, comment, footprint, **params):
        components[name] = {"unique_id": f"U{name}", "unique_ids": [f"U{name}"], "kind": 0, "sheet": "Main",
                            "description": comment, "params": {"comment": comment, **params},
                            "lib_reference": "LIB", "footprint": footprint, "pins": {}}
        parts[name] = {"footprint": footprint, "comment": comment}

    for index in range(1, 9):
        add(f"R{index}", "10k", "R0603", value="10k")
    add("TP1", "TP", "TP1MM", **{"manufacturer part number 1": "N/A"})
    add("TP2", "TP", "TP1MM")
    add("C1", "100n", "C0402", **{"manufacturer part number 1": "CAP-A", "manufacturer 1": "Maker"})
    add("C2", "100n", "C0402", **{"manufacturer part number 1": "N/A"})
    for index in range(1, 8):
        add(f"L{index}", "BEAD", "L0603", **({"manufacturer part number 1": f"BEAD-{index}", "manufacturer 1": "Maker"}
                                             if index < 7 else {}))
    return {"nets": {}, "pin_to_net": {}, "parts": parts, "components": components}


def test_lines_whose_data_cannot_tell_an_mpn_gap_are_marked_may(monkeypatch, tmp_path):
    _load_board(monkeypatch, tmp_path, _fallback_netlist())
    needing, _ = _truth("Full")
    assert needing == {f"R{index}" for index in range(1, 9)} | {"TP2", "L7"}
    expected = {"mpn": {"R1": "yes", "TP1": "may", "L7": "yes", "C1": "", "C2": "", "L1": ""},
                "comment": {"R1": "yes", "TP1": "may", "C1": "may", "L1": "may"}}
    for group_by, states in expected.items():
        data = _read_bom(variant="Full", group_by=group_by)
        assert data["mpn_coverage_percent"] == 41
        checks = _missing_from_checks(_read_checks("Full"))
        assert checks == (None, set())  # check_bom names no part under 50% coverage
        got = {line["designators"][0]: _missing(line, 41, checks) for line in data["lines"]}
        assert {first: got[first] for first in states} == states, group_by
        assert all(not state for first, state in got.items() if first not in states), group_by
        # Every line holding a part that needs an MPN is flagged or marked; no line holding
        # only parts with an MPN or 'N/A' is flagged outright.
        for line in data["lines"]:
            state = got[line["designators"][0]]
            assert bool(state) or not set(line["designators"]) & needing, line["designators"]
            assert state != "yes" or set(line["designators"]) & needing, line["designators"]
    script = _script()
    assert 'return { state: "may", why: `get_bom lists ${varies.length} of the ${counted} MPNs' in script
    assert 'if ((Number(row.quantity) || 0) > marks.length) {' in script
    assert "Missing MPN (${missing} + ${maybe} may)" in script and '"MPN may be missing"' in script


def test_a_lines_bom_doc_solutions_show_cached_figures_with_their_dates():
    script = _script()
    # Lifecycle, stock and price only next to their cache date, and never as current.
    for line in ('["Lifecycle", cachedValue(solution.lifecycle, solution.cache_date)]',
                 '["Stock (all suppliers)", cachedValue(solution.stock_total, solution.cache_date)]',
                 '["Best unit price at the need", cachedValue(best, solution.cache_date)]',
                 '{ key: "cache_date", label: "Cached on", cls: "c-name", value: (choice) => cachedText(choice.cache_date, choice.cache_date_text) }',
                 '["Stock", "stock" in finding ? cachedValue(finding.stock, finding.cache_date) : null]',
                 '["Lifecycle", "lifecycle" in finding ? cachedValue(finding.lifecycle, finding.cache_date) : null]'):
        assert line in script, line
    assert 'return "no cache date: not current";' in script
    assert "`cached ${String(date).replace(\"T\", \" \")}, not current`" in script
    assert "not current data: no supplier is asked" in script
    # What other suppliers report, a risky lifecycle's badge and the part choices' figures
    # say they are cached too; the part choices table has no column chooser, so the cache
    # date cannot be hidden from them.
    assert ('["Other lifecycles", cachedValue(text(finding.other_lifecycles), null, '
            '"the line\'s part choices give each one\'s date")]') in script
    assert 'Shell.levelBadge("warning", `${solution.lifecycle} (cached)`)' in script
    for label in ('"Stock (cached)"', '"Unit price (cached)"', '"Lifecycle (cached)"'):
        assert label in script, label
    assert ".solution .tbl-block > .toolbar { display: none; }" in _page()
    assert 'label: "Pinned"' not in script
    # The boards input is a local calculation, said so, and never sent to a tool.
    assert "a local calculation, not stock and not price" in script
    assert "production_quantity:" not in script


def test_the_solutions_walk_asks_by_designator_under_the_variant_shown(abom_project):
    data = _read_bom(variant=BASE_VARIANT, active_bom=True)
    resistors = next(line for line in data["lines"] if line["designators"][0] == "R1")
    answers = _walk(resistors, data["variant"])
    assert [answer["line"]["line"] for answer in answers] == [10]
    assert answers[0]["line"]["designators"] == ["R1", "R2"]
    # A designator on no BomDoc line: line_not_found, said so.
    header = next(line for line in data["lines"] if line["designators"][0] == "J1")
    assert header["bom_doc_lines"] == [] and _walk(header, data["variant"]) == []
    assert json.loads(main_mod.get_active_bom(line="J1"))["error"] == "line_not_found"
    # A line too large for one answer even alone: get_active_bom leaves it out (no error),
    # and the walk stops there rather than asking for the line's next designator.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1500)
        omitted = json.loads(main_mod.get_active_bom(line="C1", variant=BASE_VARIANT))
        assert "line" not in omitted and "error" not in omitted and omitted["lines_omitted"]
        assert _check(_contract()["get_active_bom"], omitted) == []
        capacitors = next(line for line in data["lines"] if line["designators"][0] == "C1")
        assert _walk(capacitors, data["variant"]) == []
    script = _script()
    walk = script.split('async function askSolutions(row, mode = "start") {', 1)[1].split("\n  }\n", 1)[0]
    assert "const args = { line: next, variant };" in walk
    assert "const variant = view.data.variant;" in walk
    assert '} else if (!json || json.error !== "line_not_found") {' in walk
    assert "while (state.asks.length < state.budget) {" in walk and "state.left = open();" in walk
    assert "`On no BomDoc line (get_active_bom): ${listed(noLine)}.`" in script
    assert "} else if (json.lines_omitted) {" in script
    assert "`Not asked, as the calls stopped above: ${listed(state.left)}.`" in script


def _spread_project(monkeypatch, tmp_path, numbers):
    """One get_bom line (R1-R12 share an MPN) whose parts sit on three BomDoc lines (R1-R4,
    R5-R8, R9-R12; numbered as given, None for unnumbered), and R13-R30 (the same MPN)
    on no BomDoc line: added after the BomDoc was saved."""
    doc = bom_doc(*[item(f"RES-{tag}", number, description=f"Resistor {tag}", comment="10k",
                         choices=[choice(mpn="SAME-1", sku=f"PH-{tag}")])
                    for tag, number in zip("ABC", numbers)])
    folder = tmp_path / "Spread"
    folder.mkdir()
    (folder / "Board.BomDoc").write_bytes(doc)
    (folder / "Board.PrjPcb").write_text("[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Board.BomDoc\n",
                                         encoding="utf-8")
    mpn = {"manufacturer part number 1": "SAME-1", "manufacturer 1": "Acme"}
    components = {f"R{index}": component("RES-A" if index <= 4 else "RES-B" if index <= 8 else
                                         "RES-C" if index <= 12 else "RES-NEW", "10k", value="10k", **mpn)
                  for index in range(1, 31)}
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Board", "root_dir": str(folder), "prj_pcb_path": str(folder / "Board.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [], "parameters": {}})
    monkeypatch.setattr(main_mod._altium, "_netlist", {"components": components, "pin_to_net": {}, "nets": {}})
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([VariantDefinition(name="Only")]))


@pytest.mark.parametrize("numbers", [(10, 20, 30), (None, None, None), (10, 20, None)])
def test_a_line_spread_over_several_bom_doc_lines_shows_them_all(monkeypatch, tmp_path, numbers):
    _spread_project(monkeypatch, tmp_path, numbers)
    for args in ({}, {"active_bom": True}):
        data = _read_bom(**args)
        (line,) = data["lines"]
        assert len(line["designators"]) == 30
        checks = _read_checks(data["variant"])
        skip = _without_line(checks)
        assert skip == {f"R{index}" for index in range(13, 31)}
        answers, no_line, left = _walk_all(line, data["variant"], skip)
        # Every BomDoc line holding a part of the line, numbered or not, one call each.
        assert [answer["line"]["design_item_id"] for answer in answers] == ["RES-A", "RES-B", "RES-C"]
        assert [answer["line"]["line"] for answer in answers] == list(numbers)
        assert no_line == [] and left == []
        if args:
            # get_bom's BomDoc columns collapse unnumbered lines into one null: the walk
            # does not stop at as many lines as they name.
            assert line["bom_doc_lines"] == list(dict.fromkeys(numbers))
        # Without check_bom's list the parts on no line are asked one by one, ten calls
        # at a time (the page's WALK_ASKS), and the ones not asked yet are named.
        answers, no_line, left = _walk_all(line, data["variant"], budget=10)
        assert len(answers) == 3 and no_line == [f"R{index}" for index in range(13, 20)]
        assert left == [f"R{index}" for index in range(20, 31)]
    script = _script()
    assert "const WALK_ASKS = 10;" in script and "askSolutions(row, \"more\")" in script
    assert 'finding.check === "bomdoc_out_of_sync" && finding.reason === "parts_without_line"' in script


# ---------- states, labels, focus ----------

def test_the_error_payloads_and_states(sl1, monkeypatch):
    script = _script()
    assert json.loads(main_mod.get_bom(variant="Nope"))["error"] == "variant_not_found"
    assert json.loads(main_mod.check_bom(variant="Nope"))["error"] == "variant_not_found"
    for code in ("no_project", "netlist_required", "no_components_loaded", "variant_not_found"):
        assert f"{code}:" in script, code
    assert 'if (payload.error === "variant_not_found" && canCall()) {' in script
    assert 'showState("info", "The get_bom call was cancelled"' in script
    assert 'Shell.state("empty", `${data.variant} has no BOM line`' in script
    assert 'error.error === "no_bom_doc" ? Shell.notice("info", `No BomDoc columns: ${text}`)' in script
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    assert json.loads(main_mod.get_bom())["error"] == "netlist_required"
    monkeypatch.setattr(main_mod, "_project", None)
    for name in ("get_bom", "check_bom", "get_active_bom", "list_variants"):
        assert json.loads(getattr(main_mod, name)())["error"] == "no_project"


def test_an_empty_bom_is_a_state(abom_project, monkeypatch):
    netlist = main_mod._altium._netlist
    none = VariantDefinition(name="Bare board", variations=[
        Variation(designator=name, unique_id="", kind=1) for name in netlist["components"]])
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([none]))
    data = _read_bom()
    assert data["lines_total"] == 0 and data["lines"] == [] and data["mpn_coverage_percent"] is None
    assert data["parts"]["not_fitted"] == 7 and _check(_contract()["shapes"], data) == []


def test_answers_are_labelled_with_what_they_were_asked_under():
    script = _script()
    # The head, the caption and the notes name the result's own variant and grouping; the
    # findings and solutions the variant they were asked for.
    assert 'el("span", { class: "name", text: data.variant })' in script
    assert "Totals of ${data.variant}'s whole BOM as get_bom counts them${checks}" in script
    assert "check_bom for ${checks.variant}:" in script
    assert "`Asked for ${state.variant}.`" in script
    # Kept answers for another variant are dropped; a failure is never kept.
    assert "if (view.checks && view.checks.variant !== first.variant) view.checks = null;" in script
    assert "if (view.checks !== state) return;  // another variant was asked meanwhile" in script
    assert "if (!force && view.checks && view.checks.variant === variant && !view.checks.error) return;" in script
    # get_component: the ACTIVE variant's, named, and not the variant this BOM shows.
    assert "`${label} (active variant: ${view.active}, not ${shown})`" in script
    assert "get_component shows the part as the ACTIVE variant" in script
    order = ["await loadVariants();", "const under = view.variantsError ? null : view.active;",
             "if (kept && kept.data && askedUnder.get(key) !== under) details.forget(tool, args);",
             "if (!entry || entry.error) askedUnder.set(key, under);", "details.load(tool, args);"]
    ask = script.split("async function ask(tool, args, label) {", 1)[1].split("\n  }\n", 1)[0]
    assert [ask.index(line) for line in order] == sorted(ask.index(line) for line in order)


def test_the_head_filters_focus_and_fit():
    script = _script()
    page = _page()
    # The variant's totals, labelled as such over a filtered table; plural counts.
    assert "not of the rows the filters show" in script
    assert 'plural(data.lines_total || 0, "line")' in script and "severityText(" in script
    assert "if (ui.filteredBadge) ui.filteredBadge.hidden = !filtered();" in script
    # Long caps are "at least" counts.
    assert '`${capped(finding) ? "at least " : ""}${count}${maybe ? ` (+ one of ${maybe}?)` : ""}`' in script
    # The Not Fitted chip counts lines as the In the BOM chip does, once they are known;
    # before, it says it counts parts.
    assert '{ count: plural(parts, "part"), title:' in script and "{ count: String(rows), title:" in script
    assert "updateChips();" in script
    # After teardown no button asks anything, no answer is waited for, and the page says
    # so: a BOM being paged is drawn with the lines read, a call still loading says the
    # view was closed (no "…" left behind).
    assert script.count("The view was closed: it asks the server nothing more.") >= 4
    assert "return Promise.race([Shell.callTool(tool, args), whenClosed]);" in script
    teardown = script.split('Shell.on("teardown", () => {', 1)[1].split("\n  });\n", 1)[0]
    assert "closeCalls();" in teardown and 'showState("info", "The view was closed"' in teardown
    assert 'if (stopped) reasons.push(view.closed ? "the view was closed" : stopped);' in script
    assert 'closed: "Not known: the view was closed before check_bom answered",' in script
    # Keyboard focus stays in the details section it was in (a solutions button that is
    # gone hands it to that section's heading, never to get_component), and the pane
    # keeps its scroll position while it shows the same line.
    assert 'if (was === "solutions") return ui.solutionsButton || ui.solutionsHead;' in script
    assert 'if (was === "component") return ui.askButton || ui.componentHead;' in script
    assert "if (before && before === ui.paneShows) {" in script
    assert 'el("section", { data: { section: "solutions" } }, solutionsSection(row))' in script
    # The designators' toggle keeps focus; asking get_bom again for the same lines (the
    # BomDoc columns) keeps the line selected and its details open.
    assert "if (toggle) toggle.focus();" in script
    assert "const sameLines = Boolean(before) && previous.variant === first.variant" in script
    assert "if (mark) Shell.refocus(app, mark, focusAfterAsk);" in script
    # The solutions button waits until check_bom says whether there is a BomDoc.
    assert 'if (bomDoc === null && checksState() === "loading") {' in script
    # A number of boards that is not a whole number from 1 is marked, said so and put back.
    assert 'if (!/^\\d+$/.test(text) || (input.validity && input.validity.badInput)) return null;' in script
    assert 'input.setAttribute("aria-invalid", valid ? "false" : "true");' in script
    assert "else input.value = String(view.boards);" in script
    assert "Shell.focusMark(app)" in script and "Shell.refocus(app, mark" in script
    assert '"aria-live": "off"' in script and "Shell.announce(" in script
    # The switch and filters fold, and the details take the table's place, unless the
    # table (and details stacked under it) keep about five BOM rows.
    assert "view.controlsOpen = roomy;" in script and "view.filtersOpen = roomy;" in script
    assert "return limit >= CHROME_HEIGHT + OPEN_HEIGHT + panes * ROWS_HEIGHT + (panes - 1) * 10;" in script
    assert "return window.innerWidth <= 980 && limit > 0 && limit < CHROME_HEIGHT + 2 * ROWS_HEIGHT + 10;" in script
    assert "@media (max-width: 980px) {\n  .split.solo > .content { display: none; }\n}" in page
    assert 'ui.split.classList.toggle("solo", shown && compactHost());' in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    # Groups follow get_bom's order: rows unsorted at first, the designators column by rank.
    assert 'label: "Designators", cls: "c-name", value: (row) => row._rank,' in script
    assert "naturalOrder(x, y)" in script


# ---------- tool results unchanged ----------

def test_text_results_are_unchanged_through_the_protocol(sl1, abom_project):
    tools = _tools()
    for name in ("get_bom", "check_bom", "get_active_bom", "list_variants"):
        assert tools[name]["outputSchema"]["properties"] == {
            "result": {"title": "Result", "type": "string"}}
    calls = [("list_variants", {}), ("get_bom", {}), ("get_bom", {"variant": "Lite"}),
             ("get_bom", {"active_bom": True, "include_not_fitted": True}),
             ("get_bom", {"format": "csv", "active_bom": True}), ("get_bom", {"compare_variants": True}),
             ("get_bom", {"offset": 2, "group_by": "comment"}), ("get_bom", {"variant": "Nope"}),
             ("check_bom", {}), ("check_bom", {"variant": BASE_VARIANT, "offset": 3}),
             ("get_active_bom", {"line": "C1", "variant": BASE_VARIANT}), ("get_active_bom", {}),
             ("get_active_bom", {"line": "J1"})]
    for name, args in calls:
        direct = getattr(main_mod, name)(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], (name, args)
        assert result["structuredContent"] == {"result": direct} and not result.get("isError")


# ---------- docs ----------

def test_readme_and_changelog_describe_the_bom_view():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `get_bom` |" in section
    assert "does not switch the active variant" in section.split("| `get_bom` |", 1)[1]
    assert "cache date" in section.split("| `get_bom` |", 1)[1]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #62" in unreleased
