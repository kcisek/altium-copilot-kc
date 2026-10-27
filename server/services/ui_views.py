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
it in place of the text, and no app-only tool is registered: a view pages through and
drills into the existing tools (get_power_tree, query_net, get_component).

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
for the shared shell (theme, host bridge, table, filter, states, auto-resize), which
render() inlines so that each resource is one self-contained page.
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
)


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
