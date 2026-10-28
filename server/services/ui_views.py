"""Interactive views for MCP Apps hosts (roadmap #58).

A view is an HTML page a host draws as an interactive panel next to a tool's result in
the conversation. It follows the MCP Apps extension (io.modelcontextprotocol/ui), stable
specification 2026-01-26 (github.com/modelcontextprotocol/ext-apps, SEP-1865):

- the page is a ui:// resource with MIME type text/html;profile=mcp-app, its _meta.ui
  declaring its content security policy (no domains at all, so the host applies the
  spec's strictest default: no network, inline script and style only);
- its tool names it in _meta.ui.resourceUri, and in the flat _meta["ui/resourceUri"] the
  spec deprecates but hosts from before it still read;
- the host renders the page in a sandboxed iframe and talks to it in JSON-RPC over
  postMessage: ui/initialize (theme and style variables), the tool's input and result,
  tools/call back into this server, size changes and teardown (server/ui/shell.js).

Hosts without the extension (Claude Code) ignore the _meta and show the tool's text. That
text is the same whether a view is drawn or not: the tools are unchanged, and the view
reads the same JSON text (FastMCP's structuredContent {"result": text} for a str result,
as before). No structuredContent is added for the views, since text-only hosts may show
it in place of the text. A view pages through and drills into the existing tools (its
own tool, and query_net, get_component, get_sheet_context or query_pcb_region for a
selection's details); the board view adds the one app-only tool (below).

Two choices the spec leaves open, made on purpose:
- The tool's _meta link is static, not sent only to clients that advertise the extension
  in initialize (a SHOULD in the spec's capability negotiation): FastMCP builds one tool
  list for every client, hosts without the extension ignore the unknown _meta, and the
  text result is the same for both.
- The pages are listed in resources/list (the spec lets a server leave them out). A host
  may prefetch from the list; a text-only host shows them as ordinary resources, which
  their descriptions mark as pages for MCP Apps hosts.

Adding a view takes one View entry in VIEWS, one HTML page in server/ui and
link_meta(tool, meta) in the tool's decorator. The page is a whole HTML document with
three markers: <!--@csp--> for the page's own policy, /*@shell.css*/ and /*@shell.js*/
for the shared shell (theme, host bridge, grouped table, filter chips, badges, paging,
detail calls, states, auto-resize), which render() inlines so that each resource is one
self-contained page.

One view per tool: run_erc and get_drc_report (roadmap #60) have a page each, sharing the
shell's grouped table, level badges, chips, details pane and paging, rather than one page
for both. Their results share the table and its details but little else (ERC's summary,
report levels, suppressed counts and advisories; DRC's assessment, board units and
origin, per-rule paging, health, waived and warning sections), so each page carries one
tool's data contract and the registry keeps one tool per view.

The variant comparison (roadmap #61) is linked from compare_variants only. list_variants
feeds its picker through tools/call and links no view: the model calls it at the start of
every session, where a panel would only show the variant list the text already gives, and
the registry stays one tool per view. The picker only compares: no view calls
set_active_variant or any other tool that changes the server's state (every tool a page
calls is a read-only one; tests/test_ui_variants.py checks each page).

The BOM view (roadmap #62) is linked from get_bom only. check_bom has no page of its own:
its findings name parts, which read best on the BOM lines that hold them, so the view asks
check_bom for the variant it shows through tools/call and marks each line with its
findings (and lists them by check in a tab); a second page would repeat the BOM table.
get_active_bom links no view either: the BOM view asks it for one line's ranked solutions
in its details pane, and a whole-BomDoc page (sourcing, cached stock and prices) is not on
the roadmap. The registry stays one tool per view.

The board view (#63) is linked from get_board_view only. Its geometry comes from
get_board_view_geometry, the first app-only tool (_meta.ui.visibility ["app"], spec
2026-01-26). It links no view, returns compact pages without structuredContent, and exists
because whole-board geometry (about 0.7 MB on MiniPC) is useless and costly as model
context. A page or section argument on get_board_view would sit in every model's schema.
Text-only hosts still list the tool (the static tool list), so its description redirects
to the model-facing tools and it needs get_board_view's rev. The registry stays one tool
per view. APP_ONLY_TOOLS lists the tools pages may call that link no view.

The project dashboard (#74) is linked from get_project_dashboard, a new model-visible,
read-only tool. check_release_readiness and project_tour keep their text and link no
view: every release check or tour would otherwise pop a 33-check panel, and project_tour
returns a prompt, not data. The server runs the checks in-process with their default
arguments and normalises each first page into a card (services/project_dashboard.py):
one adapter set, in Python, tested on real output, and a compact text that is useful
without the view (the cards' titles, groups and views are the page's: a test keeps its
copy equal). The page calls only get_project_dashboard (checks=[one] fills a card), one
call at a time.
It opens the other views by asking the model through ui/message after a click (the spec
defers view-to-view links, so only a tool call by the model makes the host draw a view);
the message names tools only, never project data. The registry stays one tool per view.

The firmware pin map (#75) is linked from get_firmware_pin_map only. package_for_xfn,
which writes the same rows into its snapshot, changes files and links none. The page reads
every page of the table (from offset 0 when the result is a later page, and as the table
when the call asked for csv, c_header or devicetree), asks the tool again with another
mcu= from its candidates, and asks the tool itself for csv, c_header and devicetree with
the map's arguments, showing each page's text unchanged in a read-only text area: the
formats come from the tool, never from the page. Its package diagram is a layout by pin
number, labelled so: a grid when ball names (A1, B12) name the pins, numbers 1..N around
four sides (QFP and QFN order) or two, else a list; every pin is placed once or listed as
not on the layout, a read that stopped short of a part's pins draws no layout by number,
and nothing is drawn for a footprint the files do not give. On request
it draws the pad centres get_component_placement (read-only, model-facing) gives instead.
It calls get_firmware_pin_map, query_net, get_component and get_component_placement, one
call at a time. No app-only tool is needed: the pages are the tool's own, and placement
and details are tools the model uses too.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

SPEC_VERSION = "2026-01-26"
EXTENSION_ID = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
URI_PREFIX = "ui://altium-copilot-kc/"
LEGACY_URI_KEY = "ui/resourceUri"  # deprecated by the spec, kept for older hosts
UI_DIR = "ui"

# The resource's policy for the host: no connect, resource, frame or base-URI domain, so
# the host applies the spec's default (default-src 'none', inline script and style, data:
# images, connect-src 'none'). No permissions are asked for.
RESOURCE_META = {
    "ui": {
        "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [],
                "baseUriDomains": []},
        "prefersBorder": True,
    },
}

# The page's own policy, on top of the host's: nothing loads or connects anywhere (no
# fetch, no external script, style, font, image or frame; data: images only), no base
# URI and no form target. Inline script and style are allowed, since the page is inlined.
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'")

CSP_MARKER = "<!--@csp-->"
CSS_MARKER = "/*@shell.css*/"
JS_MARKER = "/*@shell.js*/"
# Text that would end an inlined <script> or <style> early, or switch the HTML parser's
# script state (a comment opener).
_UNSAFE_INLINE = re.compile(r"</(script|style)|<!--", re.IGNORECASE)
_MARKERS = re.compile("|".join(re.escape(marker) for marker in (CSP_MARKER, CSS_MARKER, JS_MARKER)))


@dataclass(frozen=True)
class View:
    name: str         # the resource's name and the end of its URI
    tool: str         # the tool whose result the page draws
    file: str         # the page in server/ui
    title: str
    description: str

    @property
    def uri(self) -> str:
        return f"{URI_PREFIX}{self.name}.html"


VIEWS = (
    View(
        name="power-tree",
        tool="get_power_tree",
        file="power_tree.html",
        title="Power tree",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "get_power_tree's result; not for reading as text. Sources, protection and "
            "series parts, converters, rails and loads as a diagram laid out by depth, "
            "with voltages, feedback dividers and findings, collapse and expand, the path "
            "of a rail or part highlighted, details from query_net and get_component, and "
            "sortable tables as the fallback."
        ),
    ),
    View(
        name="erc-results",
        tool="run_erc",
        file="erc_results.html",
        title="ERC results",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to run_erc's "
            "result; not for reading as text. The violations as a sortable, filterable "
            "table grouped by violation type, level, group or sheet with counts, level and "
            "group filters, the suppressed counts (or rows, with min_level all), the "
            "advisories and report levels, and a details pane per violation with its "
            "sheet, parts and net from get_sheet_context, get_component and query_net; or "
            "the single-pin-net report."
        ),
    ),
    View(
        name="drc-results",
        tool="get_drc_report",
        file="drc_results.html",
        title="DRC report",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "get_drc_report's result; not for reading as text. The assessment first "
            "(whether to trust the report, and why), then every violation as a sortable, "
            "filterable table grouped by rule, rule kind, component or net with counts, "
            "PCB health issues, waived violations and report warnings, and a details pane "
            "per violation with the board around its location, its parts and nets from "
            "query_pcb_region, get_component and query_net."
        ),
    ),
    View(
        name="variant-comparison",
        tool="compare_variants",
        file="variants.html",
        title="Variant comparison",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "compare_variants' result; not for reading as text. Two variants side by "
            "side: one row per designator and difference with variant a, variant b and "
            "the base design (parts fitted in one only, values, other parameters, "
            "alternate parts, variant parameters), grouped by kind or by part with "
            "counts, sortable and filterable, values equal to the base design marked; a "
            "picker for a and b filled from list_variants that only compares (it never "
            "switches the active variant); the per-variant documentation check and stale "
            "variant entries; and a details pane per part with get_component, which shows "
            "the active variant."
        ),
    ),
    View(
        name="bom",
        tool="get_bom",
        file="bom.html",
        title="BOM",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to get_bom's "
            "result; not for reading as text. The BOM lines of a variant as a sortable, "
            "filterable table (quantity, designators, Comment, value, footprint, "
            "manufacturer, MPN, what varies within a line, alternate parts, BomDoc line "
            "and chosen part) grouped by manufacturer, footprint or finding, with "
            "check_bom's findings per line and by check, filters for Not Fitted parts, "
            "missing MPNs and findings, a variant switch filled from list_variants that "
            "only shows (it never switches the active variant), a local quantity for a "
            "number of boards, and a details pane per line with its ranked BomDoc "
            "solutions from get_active_bom (cached, with their cache dates) and "
            "get_component, which shows the active variant."
        ),
    ),
    View(
        name="board",
        tool="get_board_view",
        file="board.html",
        title="Board view",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "get_board_view's result; not for reading as text. The board in 2D (outline "
            "and cutouts, copper per layer: pads, vias, tracks, arcs, areas and pours, "
            "poured copper, silkscreen and part boxes with designators) with the nets, "
            "parts, pads, spots and finding locations get_board_view names highlighted, "
            "pad-to-pad distances, zoom, pan, fit, a top or bottom view and layer toggles; "
            "its geometry read from get_board_view_geometry in pages; an Objects and a "
            "Parts table; and a details pane per object with get_component_placement, "
            "get_component, get_net_pcb and query_pcb_region."
        ),
    ),
    View(
        name="project-dashboard",
        tool="get_project_dashboard",
        file="project_dashboard.html",
        title="Project dashboard",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "get_project_dashboard's result; not for reading as text. The release verdict "
            "and its gates, the board and project facts, and one row per check (33 of "
            "them) with its state, counts, what it examined, its first finding and when it "
            "ran, grouped by domain or state with filters; the checks the first call left "
            "out filled one get_project_dashboard call at a time (stopped on request or "
            "when the files change); a details pane per check with its findings; and "
            "buttons that ask Claude, through a message in the conversation, to open a "
            "check's own view or explain its findings."
        ),
    ),
    View(
        name="firmware-pin-map",
        tool="get_firmware_pin_map",
        file="firmware_pins.html",
        title="Firmware pin map",
        description=(
            "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to "
            "get_firmware_pin_map's result; not for reading as text. The processor's pins "
            "as a sortable, filterable table (number, name and alternates, port and bit, "
            "type, net, function with its basis, pull, the parts reached) grouped by "
            "function or port with function chips and a column chooser, the power and "
            "ground pins per net, a package diagram coloured by function (a layout by pin "
            "number or ball name, or the board's pad centres from get_component_placement), "
            "the checks' findings linked to their pins, a candidate picker, the tool's own "
            "CSV, C header and devicetree text, and a details pane per pin with query_net "
            "and get_component."
        ),
    ),
)

# Tools a page may call that link no view: they serve the views and are not meant for
# the model (_meta.ui.visibility ["app"]).
APP_ONLY_TOOLS = ("get_board_view_geometry",)


def view_for(tool: str) -> View:
    """The view that draws a tool's result (KeyError when it has none)."""
    for view in VIEWS:
        if view.tool == tool:
            return view
    raise KeyError(tool)


def link_meta(tool: str, meta: dict | None = None) -> dict:
    """A tool's _meta with its view linked: ui.resourceUri (spec 2026-01-26) and the
    deprecated flat ui/resourceUri; the other keys are kept as they are."""
    uri = view_for(tool).uri
    linked = dict(meta or {})
    linked["ui"] = {**linked.get("ui", {}), "resourceUri": uri}
    linked[LEGACY_URI_KEY] = uri
    return linked


def app_only_meta(meta: dict | None = None) -> dict:
    """An app-only tool's _meta: ui.visibility ["app"] (spec 2026-01-26), so hosts that
    know the extension keep it from the model; the other keys are kept as they are."""
    return {**(meta or {}), "ui": {"visibility": ["app"]}}


def ui_dir() -> Path:
    """The pages: server/ui, or 'ui' in a PyInstaller bundle (release.yml and pack.ps1
    add it with --add-data)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / UI_DIR
    return Path(__file__).resolve().parent.parent / UI_DIR


def render(view: View, folder: Path | None = None) -> str:
    """The view's page with the policy and the shared shell inlined. A ValueError when a
    marker is missing or doubled, or an inlined part would close its own tag early."""
    folder = folder or ui_dir()
    page = (folder / view.file).read_text(encoding="utf-8")
    css = (folder / "shell.css").read_text(encoding="utf-8")
    js = (folder / "shell.js").read_text(encoding="utf-8")
    for name, text in (("shell.css", css), ("shell.js", js)):
        if _UNSAFE_INLINE.search(text):
            raise ValueError(f"{name} closes a script or style tag (or opens an HTML comment): "
                             "it cannot be inlined")
    parts = {
        CSP_MARKER: f'<meta http-equiv="Content-Security-Policy" content="{PAGE_CSP}">',
        CSS_MARKER: css,
        JS_MARKER: js,
    }
    for marker in parts:
        if page.count(marker) != 1:
            raise ValueError(f"{view.file} must hold {marker} exactly once")
    # One pass, so that a marker's text inside an inlined part is left as it is.
    return _MARKERS.sub(lambda match: parts[match.group(0)], page)


def register(mcp) -> None:
    """Register every view as a ui:// resource of the FastMCP server."""
    for view in VIEWS:
        mcp.resource(view.uri, name=view.name, title=view.title,
                     description=view.description, mime_type=MIME_TYPE,
                     meta=RESOURCE_META)(_reader(view))


def _reader(view: View):
    def read() -> str:
        return render(view)

    read.__name__ = f"view_{view.name.replace('-', '_')}"
    return read
