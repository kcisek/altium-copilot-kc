# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]

### Added
- Interactive views for MCP Apps hosts (roadmap #58) and the interactive power tree
  (roadmap #59). Claude Desktop (and other MCP Apps hosts that run local servers) opens
  `get_power_tree`'s result as an interactive panel; Claude Code and other text-only
  hosts show the text as before. claude.ai on the web does not run this local server.
  - Plumbing: `server/services/ui_views.py` registers each view as a `ui://` resource
    (MIME type `text/html;profile=mcp-app`, MCP Apps spec 2026-01-26) whose `_meta.ui`
    declares no network, resource, frame or base-URI domain (the host's strictest
    content security policy) and links it from its tool's `_meta.ui.resourceUri` (and
    the deprecated flat `ui/resourceUri` for older hosts). Adding a view is one entry in
    `VIEWS`, one page in `server/ui` and the link in its tool's meta. The link is static
    (the same tool list for every client; hosts without the extension ignore it), and
    the pages are listed in `resources/list`, so text-only hosts see them there as
    resources marked as MCP Apps pages.
  - Shared shell (`server/ui/shell.css`, `shell.js`), inlined into every page so each
    resource is one self-contained file: the host bridge (`ui/initialize`, tool input,
    result and cancellation, `tools/call` with request ids and timeouts when the host
    offers `serverTools`, host context changes, teardown, size changes with the panes
    fitted to the host's container height), the host's theme and style variables with
    light and dark fallbacks, a sortable table with a column chooser, sticky header and
    keyboard focus, a filter box, tabs, and empty, loading and error states. The page's
    own policy forbids any load or connection.
  - Tool results are unchanged, byte for byte: the views read the same JSON text
    (FastMCP's `structuredContent` `{"result": text}` as before). No `structuredContent`
    is added for the views and no app-only tool is registered: the power tree view pages
    through `get_power_tree` itself and asks `query_net` and `get_component` for details.
  - Power tree view: sources, connector outlets, series parts, converters and rails in
    columns by depth with curved links, voltages, feedback dividers (Vout, implied Vref)
    and finding badges; collapse and expand, a rail's loads, the upstream and downstream
    path of a selected rail or part highlighted, details in a side panel, a filter, zoom
    and fit, full screen where the host allows it, tables of rails, converters, sources
    and power inputs and findings, and an empty state for a project with no power tree.
    A connector that is an outlet for one rail and an input for another is two nodes, a
    converter a filter left out is drawn as a converter outside the view, links past
    `get_power_tree`'s per-rail lists are counted on their rail, and a page that cannot
    be read (or a host without tool calls) leaves the pages already read drawn with a
    notice.
  - The release build and `pack.ps1` bundle `server/ui` (`--add-data`), read from
    `sys._MEIPASS/ui` when frozen. `requirements.txt` now asks for `mcp>=1.26.0`, the
    first release that carries a resource's `_meta` on `resources/list` and
    `resources/read`. The extension manifest has no field for views: its tool list is
    unchanged.
- ERC and DRC results views (roadmap #60): Claude Desktop opens `run_erc`'s and
  `get_drc_report`'s results as interactive tables; the tools' text results are
  unchanged, byte for byte (only their `_meta` links a view), and Claude Code shows the
  text as before.
  - Two pages, `server/ui/erc_results.html` and `drc_results.html` (one view per tool, so
    the registry is unchanged), sharing new shell pieces: a table with collapsible group
    headers and counts (a grid, a treegrid when grouped, in a tab panel), a text filter
    beside an outside filter, keyboard focus that stays on the same header, group or row
    when the table is redrawn and goes back to the row when the details close (and to the
    same control after a reload or a dismissed notice), one live region instead of a live
    details pane, level badges, filter chips, a one-of switch, fact lists, a paging helper
    that follows a tool's next offsets within a page budget and says why it stopped
    (teardown, a host without `serverTools`, the budget, a failed page), a details cache
    that asks once and never keeps a failure, and a full-screen button. Message cells keep
    a readable width and the details pane stacks under the table below 980 px; long names
    wrap rather than scroll the page; the panes shrink to 64 px before the page outgrows a
    short host; tertiary text meets WCAG AA in both themes; the host's style variables
    may hold colours and arithmetic only (no other CSS function, escape or at-rule); no
    request is sent after teardown. The power tree page is unchanged and gets these shell
    fixes too.
  - ERC view: every page of `run_erc` read (a later page as the result: from offset 0,
    else the summary's counts shown as not known), grouped by violation type, level, group or
    sheet with counts and worst level, sortable, filterable by text and by level, group
    and state chips, with a column chooser; suppressed rows marked when the result lists
    them (`min_level="all"`), otherwise the suppressed counts with a button that asks
    `run_erc` for them (the Level chips keeping the call's floor); a call narrowed by
    checks, sheet or min_level counts its own rows in the header, `run_erc`'s summary
    marked as the whole project's; advisories, report levels and notes in their own tabs;
    a details pane per violation (message, object, sheet, net, location in mil, suggested
    fix, the level and why) with buttons that ask `get_sheet_context`, `get_component` and
    `query_net` (none for a single-pin net, which the netlist leaves out); the
    single-pin-net report as a table by sheet; an empty state with the summary.
  - DRC view: the assessment first, as a banner by its worst severity (whether to trust
    the report, with each reason), then every rule violation read through `rule=` (once
    per description, the rows given back by exact title), grouped by rule, rule kind,
    component or net with counts; the view groups the rows itself once it has them all,
    and otherwise shows the rows read before a stop or each group's examples with the
    report's counts, asking `get_drc_report` with `group_by` for a component or net
    grouping it does not hold; a later page as the result is read again from offset 0;
    follow-up calls use the result's own units and grouping; a rule the report counts but
    lists no row of is named. Health issues, waived violations and report warnings in
    their own tabs, read in full (in a `rule=` listing too; tables sharing a title once);
    a details pane per violation (rule, message, components, nets, location in the
    result's units, absolute and from the board origin) with buttons that ask
    `query_pcb_region` around the location, `get_component` and `query_net`; the tools'
    error payloads (no project, no board, no report) as an error state with their text.
  - Tests (`tests/test_ui_violations.py`): the resources and links, the pages' calls and
    capability checks, both data contracts against `run_erc` on three of Altium's public
    examples and `get_drc_report` on a copy of one with the repo's report fixture and
    synthetic reports (stopped at the limit, stale, paged and trimmed, rules sharing a
    description, waived tables sharing a title, a rule counted but not listed), paging
    the way the views page gives every row once, a later page, narrowed calls, single-pin
    nets, and the shell's contrast, style-value, teardown, grid and focus rules.
- Variant comparison view (roadmap #61): Claude Desktop opens `compare_variants`' result
  as an interactive side-by-side comparison; the tool's text result is unchanged, byte for
  byte (only its `_meta` links the view), and Claude Code shows the text as before.
  - `server/ui/variants.html`, linked from `compare_variants` only. `list_variants` links
    no view (the model calls it at every session start, where a panel would repeat the
    text) and only fills the view's picker through `tools/call`; the registry stays one
    tool per view.
  - Every page of `compare_variants` read (a later page as the result: from offset 0, else
    the documentation, stale entries and notes shown as not known), each page checked to be
    one of the same comparison. One row per designator and difference with variant a,
    variant b and the base design, grouped by kind (fitted in one only, values, other
    parameters, alternate parts, variant parameters) or by part, with counts, sort, a text
    filter, kind chips and a column chooser; Fitted and Not Fitted as badges (with the
    fitted side's varied parameters or alternate part), a value that differs from the base
    design highlighted and one equal to it marked; no sheet grouping, since
    `compare_variants` gives no sheet.
  - A picker for a and b filled from `list_variants` (with the base design) that asks
    `compare_variants` again (Compare, Swap a and b): it only compares, and the page says
    so; no view calls `set_active_variant` or any tool that changes the copilot's state
    (the page refuses any tool but `compare_variants`, `list_variants` and
    `get_component`, and a test checks that every tool any view page names is registered
    and read-only).
  - A Documentation tab: per variant, the OutJob outputs set to it (BOM, pick and place,
    assembly drawing), the missing ones as findings (a's and b's listed, the others
    counted), the Draftsman drawings set to it or whose notes name it, and the stale
    variant entries.
  - A details pane per part: its differences across a, b and the base design, its
    alternate part's library data, and a button that asks `get_component`, labelled as the
    ACTIVE variant's view: `list_variants` is asked first, each answer names the active
    variant it was asked under (the one `get_component`'s variant_changes names when it
    has them), a kept answer is asked again when `list_variants` names another active
    variant, and an answer can be asked again. Without a netlist (which `get_component`
    needs) the button is not offered.
  - States: no variants (empty, with the tool's text), two identical variants (empty, with a
    note), no project, an unknown variant (with the picker, starting from the side of the
    call the project has; a failed compare from there keeps the state and the picker), a
    cancelled call, paging stopped with its reason, the view closed (no more asks); the
    picker and the filters fold behind a disclosure unless the host leaves the table (and,
    on a host 980 px wide or less, the details pane under it) about five rows, and on a
    short host 980 px wide or less a part's details take the table's place (Close or Esc
    brings the table back, with focus on its row). Groups and the Part column follow
    `compare_variants`' natural order; the a and b headers wrap; a value that differs from
    the base design keeps a ring on a selected row; `compare_variants`' cap of 20 stale
    entries per variant shows as "at least".
  - Shell: the details cache can forget an answer that may be out of date
    (`toolCache().forget`); keyboard focus stays in a redrawn container when the control
    it was on is gone (a Retry or Ask again replaced by "Asking…"): on a fallback the
    page gives, else the control now at its place; a page budget can count the result's
    first page (`pageBudget(total, used)`), so the run_erc and compare_variants views
    stop after 60 pages read in all, as their notice says; and a frame measured before
    it has a size is measured again on a timer, so its first size-changed is sent where
    animation frames and ResizeObserver callbacks are held back.
  - Tests (`tests/test_ui_variants.py`): the resource and link, the self-contained page,
    the read-only rule for every view's calls, the data contract against `compare_variants`
    and `list_variants` on the repo's Demo project and a copy of Altium's public
    SpiritLevel example with four variants added (Not Fitted parts, varied parameters, an
    alternate part with its `.PrjPcbVariants` copy, variant parameters, OutJob outputs per
    variant, stale entries, more than one page of differences, no netlist), paging the way
    the view pages gives every difference once (also with trimmed pages), a later page,
    the error payloads and empty states, and unchanged text results.
- BOM view (roadmap #62): Claude Desktop opens `get_bom`'s result as an interactive BOM;
  the tool's text result is unchanged, byte for byte (only its `_meta` links the view),
  and Claude Code shows the text as before.
  - `server/ui/bom.html`, linked from `get_bom` only. `check_bom` has no page of its own:
    its findings name parts, which read best on the BOM lines that hold them, so the view
    asks `check_bom` for the variant it shows through `tools/call`; `get_active_bom` links
    none either and gives a line's ranked solutions in the details pane. The registry
    stays one tool per view.
  - Every page of `get_bom` read with the call's arguments (a later page as the result:
    from offset 0; a CSV result asked again as JSON, or without tool calls its rows shown
    as text with the reason; without the call's arguments, which say how the lines were
    grouped, read again from offset 0 with `group_by` mpn), each page checked to be one of
    the same BOM. Lines with quantity, designators (ranges such as R1–R4, the first ones
    with "+N more"), Comment, value, description, footprint, manufacturer, MPN (placeholders
    marked as needing none or still needing one), what varies within a line (the first
    five values of more, as `get_bom` lists them), alternate parts and varied parameters,
    BomDoc line and chosen part; `get_bom`'s grouping (MPN or Comment, asked again) and the
    view's own by manufacturer, footprint or finding; sort, text filter, column chooser;
    the variant's totals (lines, parts in the BOM, Not Fitted, excluded by kind, MPN
    coverage, `check_bom`'s severity counts) labelled as the whole BOM's over a filtered
    table.
  - Filters: the Not Fitted parts as lines (one `get_bom` call with `include_not_fitted`
    when the result lists only their designators, grouped as the lines are; the chip
    counts their lines once known, else says it counts parts), lines missing an MPN by
    `mpn_coverage_percent`'s rule (`check_bom`'s missing_mpn names the parts once it has
    answered for a project without a BomDoc, mechanical parts left out; otherwise a part
    it names counts and the lines' own data decide the rest, a line they cannot decide,
    such as a placeholder or one of more than five MPNs shared by only some of its parts,
    marked "MPN may be missing"), and lines with `check_bom` findings: every page of
    `check_bom` for the variant shown, its findings mapped to the lines by designator (a
    duplicate sheet designator through the lines' sheet designators, on no line when
    both a part of that name and parts with that sheet designator are in the BOM),
    severity badges per line, a Findings tab grouped by check (a finding whose part list
    reaches `check_bom`'s cap marks "at least" that many lines).
  - A variant switch filled from `list_variants` (with the base design) that asks
    `get_bom` and `check_bom` again with `variant`: it only shows, and the page says so (no
    view calls `set_active_variant`); what each variant changes in the BOM (`get_bom` with
    `compare_variants`) in a Variants tab; a button for the BomDoc columns (`active_bom`)
    when the project has an ActiveBOM document.
  - A number of boards that multiplies the quantities in the view, labelled as a local
    calculation (not stock, not price).
  - A details pane per line: all fields, designators with their sheet designators, what
    varies, alternate parts, the line's findings, its BomDoc line and chosen part, a
    button that asks `get_active_bom` for the line's ranked solutions and part choices
    (designator by designator until every part of the line is on a BomDoc line found or
    said to be on none, so lines numbered or not, and a line spread over several BomDoc
    lines, are found; parts `check_bom` lists as without a line are not asked; up to 10
    calls at a time, then a button for the next ones) with lifecycle, stock and price
    only next to their cache date or marked cached (a risky lifecycle's badge, what
    other suppliers report, the part choices' columns, whose cache date column cannot be
    hidden) and `get_active_bom`'s note that they are cached, not current; and a
    `get_component` button for a designator, labelled as the ACTIVE variant's (named, and
    "not" the variant shown when they differ). A details pane per finding with buttons to
    its lines.
  - States: no project, no netlist, no components, an unknown variant (with the switch),
    an empty BOM, BomDoc errors as notices, a cancelled call, paging stopped with its
    reason, the view closed (no more asks, no answer waited for: a BOM being paged is
    drawn with the lines read, and what was waiting says so); answers kept for another
    variant are dropped and a failure is never kept; asked again for the same lines (the
    BomDoc columns) the selected line stays open; focus stays on redraws and in the
    details section it was in when a button disappears, with the pane's scroll kept; a
    number of boards that is not a whole number is marked and put back; the switch and
    filters fold unless the table keeps about five BOM rows, and a line's details take
    the table's place on a narrow host too short for both.
  - Shell: the page's height is measured with its scrollbar shown, so a height that just
    fits no longer leaves a narrow page stuck taller than the host's limit with a
    scrollbar that makes its text wrap more.
  - Tests (`tests/test_ui_bom.py`): the resource and link, the self-contained page and
    its five read-only tools, the contracts against `get_bom`, `check_bom`,
    `get_active_bom` and `list_variants` on Altium's public examples with an ActiveBOM
    document (Mini PC Motherboard and WiFi, a Harness Demo board), the repo's Demo and
    ActiveBOM fixtures, a copy of the public SpiritLevel example with variants added and a
    synthetic board (lines whose parts disagree, placeholders, a mechanical part, sheet
    designators, no MPN parameters, no part needing one), paging the way the view pages
    gives every line and finding once (also trimmed pages), a later page and a CSV result,
    findings mapped to lines (a duplicate sheet designator too), the missing-MPN rule
    against the parts `get_bom` builds (lines whose data cannot tell marked "may"), a
    line's BomDoc lines found designator by designator (a line spread over several
    numbered, unnumbered or mixed BomDoc lines, parts on none, ten calls at a time), and
    unchanged text results.
- `trace_net_across_boards`, `check_multiboard` and `get_system_connections` (roadmap
  #54): multi-board systems (`.PrjMbd`) read offline; the loaded project is never
  changed and none of the three needs its netlist.
  - Parsers: `server/parsers/prj_mbd.py` reads the `.PrjMbd` (the `.PrjPcb` INI layout:
    `[Design]`, the Error Reporting levels, `[Document<n>]` child projects, multi-board
    schematic, assembly, harness projects, parameters, variant names);
    `server/parsers/mbs_doc.py` reads the AD25 `.MbsDoc` (a zip of JSON files: core.json
    and logical.json) into modules (designator, title, child project, connector parts
    with their UniqueId path, sheet and pins with the net stored at the last import),
    entries and the pins each carries, connections by kind (direct, wire, cable,
    harness; a cable's or harness's own plugs, entries and internal wires), pin pairs,
    power ports and the conflicts Altium stored. Unknown keys and connection types are
    kept (an unknown type is read by its pin pairs, with a warning); an `.MbsDoc` that is
    an OLE compound file, a text record file, bare JSON or a zip without logical.json is
    reported as unsupported, not guessed. The `.MbaDoc` (3D placement) is not read.
  - The system: system= (a `.PrjMbd` or its folder), else the `.PrjMbd` in the loaded
    project's folder or up to two folders above it (and their subfolders, so a sibling
    folder holding the system is found) that lists the project. Each module is a board;
    its child project is compiled offline (cached by its files; the loaded project's own
    netlist serves its board), so two modules placing one project are two boards. A
    module's connector is matched to its child part by the stored UniqueId path, then by
    designator; pins by number; nets are the child project's now. A project that does not
    compile (board_not_loaded), and a connector or pin the child project no longer has
    (connector_not_in_project), keep the nets the `.MbsDoc` stored, so one deletion is one
    finding. The model is rebuilt when any file it was read from changes, a module's
    project the `.PrjMbd` omits included.
  - Pin map: every pair of board pins a connection joins end to end, a cable or harness
    followed through its plugs and wires (a splice gives one row per pair of ends, up to
    64 ends per pin, with a system warning beyond), with status same_net,
    different_names, no_net, unconnected, unwired (the plug pin has no wire on) or
    dead_end (the wire ends in a plug pin no board mates); system nets join board nets
    through mated pins.
  - trace_net_across_boards: from a board's net, or the net on a connector pin (J5-3),
    through every mated pin, hop by hop and recursively, with the system net, each board
    net's depth, class, voltage and other pins, the hops (connection, kind, connectors,
    pins, plug pins passed, name check), unwired and dead-end pins and splice links between
    nets already reached.
  - check_multiboard: Altium's five multi-board violations (Different Net Names, Entry Is
    Empty, No Mated Part, No Net, Unresolved Conflict: Type114 to Type118) with the level
    the `.PrjMbd` sets (a warning of a check set to No Report is shown as info); No Net
    per pin as Altium counts it (every entry pin with no net, summary.no_net_pins), No
    Mated Part also for an entry whose cable or harness reaches no other module, Entry Is
    Empty also for a connector whose child part lost System=Connector (the next import
    would empty it). Checks Altium has no counterpart for carry no Altium name: unwired
    cable or harness pins, two nets of one board joined through other boards (a warning
    for signals, info for grounds or rails), rails of different voltages or a ground and a
    rail joined through more than one hop, ground not shared with the connected boards,
    pins with a net that no connection mates, System=Connector parts the multi-board
    schematic does not place, connectors or pins deleted from a child project and nets
    changed in it since the last import. A summary per connection and the rails across
    boards. Different names are read by a name policy: info when they look like one
    signal (board prefix or shared tail, a one-letter board suffix, leading zeros, one
    name in two active-low notations, an abbreviated stem, a common synonym such as CLK
    and SCK, a name Altium generated (Net<part>_<pin>), nets numbered per board, two names
    of one rail, TX against RX or SDO against SDI), warning or error when they suggest
    different signals (ground against a rail or two voltages: error; ground or a rail
    against a signal, another index on the stem or the bus (I2C1 against I2C2), P against
    N (DP against DM, D+ against D-), an active-low mark on one side only, one bus or group
    name ending in another signal (SPI_MOSI against SPI_MISO), two signals swapped between
    the boards, unrelated names: warning). Info findings are grouped per connection.
    Stored conflicts are quoted, not recomputed, and matched against the pins, entries
    and connections the findings name (grouped ones included).
  - get_system_connections: the inter-board table, filtered by connection, board or net.
  - Fallback: mating=[{board_a, connector_a, board_b, connector_b, pin_map?, kind?,
    name?}] replaces the `.MbsDoc`'s connections (missing or unreadable `.MbsDoc`: the
    tools say so and list the boards). A connector mated with itself is refused; one
    connector in two direct matings, or one pair of connectors listed twice, is warned
    about.
  - Registered in the manifest, the server instructions, the README, the review
    checklist's evidence tools (check_multiboard) and the headless check command
    (no_multiboard_system is not applicable).
  - Calibration on Altium's two multi-board examples. Mini PC: 4 modules (two place the
    same SODIMM project), 6 connectors all matched by UniqueId, 3 direct connections, 572
    pin pairs (204 same net, 300 different names, 13 no net on one side, 55 unconnected),
    142 system nets, 123 No Net pins; 7 warnings, all the second SODIMM socket wired to
    the other rank (CKE1, CS1, ODT1, CK2/CK3 against the module's CKE0, S0, ODT0,
    CK0/CK1), and 17 grouped info findings (board prefixes, VDD_DDR against VDD, unused
    positions, the WiFi card's unused PCIe pins, each SODIMM's VDDSPD and VPP both fed
    from the motherboard's IO_2V5, mounting pins); 46 pairs checked by hand against the
    JEDEC DDR4 SODIMM and PCI Express Mini Card pin assignments, and all 572 re-derived
    independently from logical.json and a fresh compile, identical. Harness Demo: 5
    modules, one harness with 5 plugs and 12 internal wires (two splices), 18 pin pairs
    (17 user net labels numbered per board, 1 unwired plug pin), 2 No Net mounting pins;
    4 info findings. Edited copies of both (a connector deleted or renumbered in its child
    project, a lost System=Connector, a USB pair swapped, one pin mated twice, a stored
    conflict, a harness plug wired to nowhere, every check at No Report) each add only the
    findings that name the edit, with no cascade of open pins. Neither file stores a
    conflict. Mini PC builds in 1.4 s (child
    compiles), cached calls take milliseconds; results 5 to 40 kB. On 63 private projects
    (no multi-board files): every call answers no_multiboard_system in under 0.05 s, and
    in memory each board mated to a copy of itself (1164 pin pairs) gave only No Net info
    for unused positions and 8 ground_not_shared warnings (boards joined only through
    signal connectors); the existing tools' outputs are unchanged.
- `get_db_part` and `check_db_parameters` (roadmap #53): parts placed from a database
  library (`.DbLib`, `.SVNDbLib`) or linked through a database link file (`.DbLink`)
  against their database record; netlist required, read-only throughout.
  - Parser (`server/parsers/db_lib.py`): the INI layout of Altium's database library
    editor (UTF-8, older files Windows-1252, a `.udl` UTF-16): `[DatabaseLinks]`
    (connection, default update options, quote characters, schema, the simple-mode
    database type and path, relative to the library when DatabasePathRelative is set,
    the DbLib's library search path; an SVNDbLib's repository keys, masked), `[Table<n>]`
    (enabled, key lookup or UserWhere with its WHERE clause) and `[FieldMap<n>]` Options
    lists (field, table, key or normal, the parameter or system field it maps to:
    `[Library Ref]`, `[Library Path]`, `[Footprint Ref]` and its numbered copies,
    `[Footprint Path]`, `[Description]`, `[None]`; VisibleOnAdd; the field's own Add,
    Remove and Update options: Default, Never, Always, if not blank / if blank in the
    database). The key names and option values come from Altium's own binaries, as no
    database library with tables could be read. Lines end at CR or LF only and a section
    met twice is read once, as Altium's INI reader does. Connections are reduced to a
    safe description: kind (access, excel, text, sql_server, mysql, postgresql, oracle,
    odbc_dsn, other), provider, data source, whether credentials are present, and the
    string with every password, user name and token masked (keys matched anywhere in
    their name: Passwd, SVNUserName, Login; inside a nested Extended Properties string;
    a URL's user:password@ and Oracle's user/password@ in any value; a segment without
    '=' is never joined to the next key and is masked unless it is an ISAM name such as
    `Excel 8.0`); `File Name=<x.udl>` reads the Data Link file (one on a network share
    or in a OneDrive folder only with allow_network=true); an ODBC data source name is
    looked up in the registry and counts as a file source when it names a local Access,
    Excel or text file.
  - Finding the libraries: the project's documents, the installed libraries and search
    paths `services/libraries.py` reads, and the library a part names (SourceLibraryName,
    DatabaseLibraryName, LibraryPath); each says which found it, and parts naming a
    database library none has are listed. The sheet parser now keeps DatabaseLibraryName,
    UseDBTableName and the names of parameters left empty on the sheet (blank_params;
    params and the BOM are unchanged), and the netlist components carry them.
  - Query layer (`server/services/db_query.py`, `server/scripts/query_db.ps1`): only
    `SELECT * FROM <table> WHERE <comparisons>`, built from checked and quoted names and a
    comparison tree whose values are parameters, with a row limit and a timeout. `.xlsx`
    and `.xlsm` workbooks are read in-process with the existing workbook reader; anything
    else goes to Windows PowerShell's .NET OleDb / Odbc in one process per library, the
    request (connection included) on its standard input, never on a command line. Files
    are opened read-only (Mode=Read, ReadOnly=1): the library's provider, then ACE 16 and
    12, then the Access, Excel or text ODBC driver (an ODBC string tries its driver
    first; Jet has no 64-bit build); ODBC strings are written the ODBC way (no quotes,
    braces only around a ';'). Database servers, ODBC data sources that are not a local
    file, unknown providers, URLs, network shares and OneDrive folders (SharePoint and
    Teams libraries OneDrive syncs, from the registry, and cloud-only files included) are
    skipped unless allow_network=true, for every path the connection names (a workgroup
    file too), and inside a local Access file only its own tables are read: a linked
    table or saved query, which can read another database or a server, is refused per
    query (not_local_table). Workbooks give dates, numbers and blank-looking cells as the
    Excel driver does, and text compares without trailing spaces, as Jet does. Errors
    are fixed codes, never the provider's message.
  - Linking: a DbLib part by its table's key field (the part's parameter or system field
    the key maps to, else its Design Item ID, both tried when they differ) or the
    table's WHERE clause with `{Parameter}` placeholders (comparisons, AND, OR, NOT,
    LIKE, IS NULL, nested at most 32 deep as the script allows; `"..."` is a string for
    Access, Excel and text sources; anything else is where_unsupported); a DbLink in
    every enabled table (only the part's DatabaseTableName's table when UseDBTableName
    is set; parts restricted to a table it lacks or has disabled are counted) by the key
    parameter, parts without it counted as not linked. Tables are tried in the library's
    order and the first with a record is used. A table or field name that cannot be
    quoted safely leaves out only its own lookups (bad_name).
  - check_db_parameters: a preview of Update Parameters From Database with each field's
    update options (would_change, would_add with VisibleOnAdd, would_remove,
    differs_not_updated, blank_in_database, case-only differences as info; a parameter
    left empty on the sheet counts as present; the key field's own parameter is compared
    when the Design Item ID found the record), record_not_found, duplicate_records (with
    more_records when more than 5), key_conflict (the key parameter and the Design Item
    ID name different records), found_in_several_tables, symbol_differs,
    footprint_differs ([Footprint Ref] first, then [Footprint Ref 2] ...; info when the
    current footprint is another of the record's, or the record's is another of the
    part's models), library_path_differs (when the part records a library file),
    description_differs, key_missing, table problems (once per table for a DbLink) and
    bad_name, reported even when the source is skipped, query_failed, library_not_found,
    library_unreadable, library_skipped with the reason (no_driver,
    network_not_allowed, file_missing, onedrive_or_share, unreadable), no_tables and the
    variant's alternate parts; per-library rows (found by, connection, tables, parts
    linked, records found, queries, seconds). Pages read one cached model (2 minutes).
  - get_db_part: a placed part, a Design Item ID or a key value: the record with every
    mapped field, its parameter, both values and what the update would do, unmapped
    fields, the symbol and footprint the record names against the placed ones, how it
    was matched with a confidence (exact; design_item_id whenever the Design Item ID
    stood in for the key parameter; ambiguous for several records or a key conflict)
    and alternates (same Value and `[Footprint Ref]`, or alternates_by=; records with the
    part's own key value left out).
  - Calibration: synthetic DbLib + Access (a copy of Altium's Empty.mdb with generic
    tables), DbLib + `.xlsx`, a DbLink, a WHERE-clause table, a `.udl` connection and a SQL
    Server connection (skipped as network_not_allowed) all linked and compared as
    expected, about 0.9 s per Access library (PowerShell start included) and 0.02 s per
    workbook. On 63 private projects (4380 parts): 6 list a `.DbLink` (the only database
    files there, all found by the project), each an Access connection with no tables and a
    data source not on this PC (no_tables, data source file_missing), no part placed from
    a database library, no credentials; checks take 0.016 s at most.
- `get_active_bom` (roadmap #43): the project's ActiveBOM document (`.BomDoc`) read
  offline (`server/parsers/bom_doc.py`, `server/services/active_bom.py`), so the copilot
  sees the engineer's sourcing decisions; netlist required. No supplier is queried: stock,
  price and lifecycle are what ActiveBOM cached, and every one comes with its cache date.
  - Parser: one record per CRLF line of '|'-separated KEY=VALUE fields, each line decoded
    on its own (UTF-8 when valid, else Windows-1252; a %UTF8% key wins). A line that is no
    record continues the previous record with its line break, and the joined text is then
    split into fields, so the fields after a multi-line description (comment, note, line
    number, parameters) are read; a fragment that is no KEY= continues the previous value
    ('|' inside a description). BOM header, the option records, CatalogItem (one per line)
    with its PartChoice (P<n>/Q<n> price breaks and the breaks' other <letters><n> fields
    kept as written, stock, lifecycle, LASTUPDATEDAT as an OLE date; a negative stock is
    unknown, and a part choice with no supplier or SKU caches no stock), Solution and
    PartChoiceGroups records; unknown records and keys are kept raw, and a number that is
    not finite (NAN, INF) is missing, so the output is always strict JSON. A 2.5 MB file
    parses in about 0.05 s.
  - The BomDoc to read: the project's only one, else the one the OutJobs' BOM outputs
    name, else the first listed; name= picks another, also one in the folder but not in
    the project (said so). No BomDoc, one missing on disk or unreadable: an error that
    says which.
  - The join: a line covers the compiled BOM parts whose Design Item ID is its
    DESIGNITEMID; lines sharing one Design Item ID (ActiveBOM splits an item whose parts
    differ in Comment, Value, Footprint or other parameters) are told apart by the values
    stored in COMPONENTPARAMETERS; a Workspace item saved under another revision (a 1-2
    digit suffix after an item number of 3 or more digits) matches as other_revision. The
    variant's Not Fitted parts and parameter variations apply as in get_bom, and an
    alternate part joins by its own Design Item ID. A line without parts says why: no
    part has its ID, only parts outside the BOM (compile-masked, not compiled, no-BOM)
    have it, a split line whose parts matched its sibling, or the variant's alternate
    replaces them. On 56 private
    projects' BomDocs 3388 of 3466 BOM parts joined a line (the rest are mostly in BomDocs saved before
    the sheets); of the 89 lines without parts, 48 name an ID no part has, 37 an ID only
    on parts outside the BOM and 4 are split lines whose parts matched a sibling. Checked against the 52
    BOMs their OutJobs exported from ActiveBOM: 807 of 830 base-view rows hold exactly one
    line's fitted parts, the MPN is the chosen solution's on 386 of 389 rows of lines with
    part choices and the parts' MPN parameter on 279 of 306 rows of lines without, the
    manufacturer or one of its aliases on 360 of 374 rows, the Line # on 250 of 275 rows
    of files not older than the BomDoc (24 of the 25 misses in BomDocs that give one number
    to several lines), and an unnumbered line exports an empty Line #.
  - Output: header (production quantity, currency, target and actual unit price,
    favourite suppliers, solutions per item, risky-solution filtering, custom columns,
    column aliases, export options; a BomDoc stores no variant), lines in line-number
    order with their design parts, quantity per board and at the production quantity
    (production_quantity= for another number of boards), the chosen solution and cache
    dates; sections=solutions adds every ranked solution
    (higher RANK first; a Solution record's rank wins; pinned suppliers first) with each
    part choice's SKU, stock and enough_stock for the need, unit price at the need (the
    lowest price of the breaks the need reaches, as a part choice can hold several price
    tables back to back; under the first break below_minimum_order, and extended_price
    buys that break's quantity), price breaks and lifecycle. The chosen solution's best
    price comes from offers the need can buy (best_price_below_minimum_order when none
    can), and its stock_total counts each supplier once (its largest figure). non_pcb
    lines (BOM items with no design parts) and the join report (parts without a line,
    lines whose parts are gone and why, other revisions, ties, line numbers the BomDoc
    gives to several lines, chosen MPNs that differ from the parts' MPN parameter). A
    line without part choices takes its parts' MPN as a component_parameters solution, as
    ActiveBOM's export does. line= gives one line by number, Design Item ID (an
    unnumbered line), ref ('#12', the line's position in the file, given to every line
    whose number or ID names another line too) or designator (line_ambiguous lists the
    refs); pages and shrinks to the result limit. No total BOM cost and no currency
    conversion.
- `get_constraints_brief` and `generate_rule_file` (roadmap #48): a schematic-to-layout
  constraints brief and the files to apply an agreed constraint plan, offline
  (`server/services/constraints_brief.py`). Neither edits a design file.
  - `get_constraints_brief` (netlist required; the PcbDoc, when it loads, says what
    already exists and measures placements). Each item carries its evidence and whether
    the PCB (classes with the same members, pairs, rules not scoped All whose scope takes
    the members) or the schematic directives already cover it. A board rule covers an item
    only when its values and mode ask at least as much: a Width rule the IPC width on
    every layer, a Clearance the IPC-2221B gap, a MatchedLengths rule in the same mode
    (within each pair, or across the group, through xSignals too) and no looser than the
    typical tolerance (5 % slack); a rule that takes the members but asks less is listed
    as weaker and named in the plan rule's board_rules. `existing` adds the project type
    (classic or Constraint Manager, `detect_project_type`), the directives and
    check_directive_sync's summary. net_classes: differential pairs from PCB pairs (legs
    under their netlist spelling), Differential Pair directives and pair naming
    (`diff_pair_checks.netlist_pair_candidates`, new) grouped by interface from their
    names, the parts on their legs (magnetics make Ethernet MDI, a memory part a DDR clock)
    and processor pin roles (`firmware_pins.classify`), a USB port's D+/D- kept apart from
    its SuperSpeed lanes, with typical impedance and in-pair skew from INTERFACE_TARGETS
    (`TYPICAL_PAIRS`, every number the row's own, marked typical); power classes by rail
    current (`power_paths.rail_currents`, new, current= as check_power_paths takes it) in
    bands with the IPC-2221 width outside and inside for temp_rise and copper_oz (one
    value, outer/inner, or per layer name; else the stack's thinnest copper, else 1 oz),
    rails without a current in one class without a width; high-voltage classes above 30 V
    peak (names, power tree, switch nodes, hv=) with the IPC-2221B B2 / B1 clearance; an RF
    class only when an RF part's RF pin, an RF net name or an antenna corroborates a path
    (a coax connector alone does not). length_matching: in-pair tolerances, DDR byte lanes
    (DQ, DM and their DQS, one lane per side of series resistors; DDR4 2 ps, about 10 mil,
    typical), address/command groups with their own channel's clocks (and S<n> chip
    selects), RGMII groups with their clocks and schematic buses of memory or high-speed
    lines, a group without a sourced tolerance getting none. placement: decoupling
    capacitors per IC and rail (at a pin only when the schematic draws them within 600 mil
    of it and clearly nearer it than the IC's other rail pins, else one per supply pin;
    shared ones per rail), measured to that pin or the IC's nearest pad on the rail;
    TVS/ESD parts at their connectors, crystal groups (load capacitors, series and feedback
    resistors, oscillator pins and whether they are named so; active oscillators left
    out), switching-converter groups (input capacitors, switch-node inductor and diode,
    bootstrap capacitor, output capacitors, feedback divider), each measured pad to pad on
    the PcbDoc. zones per sheet (digital, power, analog, io, filtering, rf) with the parts
    or the sheet name behind each (load switches count as power; a coax connector is rf
    only on an RF path); designer_notes: sheet notes that state placement intent, quoted.
    plan: classes and rules with values in units (Width, Clearance, DiffPairsRouting only
    from a board impedance profile at the typical target, MatchedLengths), a class the PCB
    already holds under its PCB name, rules the PCB already covers left out and listed;
    needs_input says what to ask (currents, voltages, impedance profiles). sections=,
    sheet=, units, offset paging and a shrink loop; the plan is never cut: whole on the
    first page, else a pointer to sections=['plan'], which pages a long plan by class with
    each rule beside its class.
  - `generate_rule_file` validates a plan (the brief's, or one passed as JSON: units,
    class kinds, rule kinds, scopes (ASCII; scope2 for Clearance only), value keys,
    ordering on the inner layers as merged, ranges (above 0 at Altium's 0.0001 mil, at
    most 10000 mil, lengths 200000 mil), names, priorities unique per kind) and gives a
    .RUL file, CSV class lists (class, kind, member, pair nets) and a Markdown step list:
    classic projects get Parameter Set (ClassName) and Differential Pair directives,
    Design > Classes and Import Rules; Constraint Manager projects get the Constraint
    Manager's entries. Without plan= it rebuilds the brief's plan with the current=, hv=,
    copper_oz= and temp_rise= given (plan_assumptions says which). Rule names avoid the
    board's and the plan's own (numbered); priorities count from 1 per kind around the
    plan's explicit ones, with where they sit against the board's rules of the kind
    (board_priorities, the board rules overlapping each new rule, a board rule with the
    same number) in the result and the steps; rules scoped to a class neither in the plan
    nor on the PCB, and a board that was not read, are said. Scopes are InNetClass /
    InDifferentialPairClass / InNet / InDifferentialPair or a parsed query; per-layer
    width keys follow the board's own rule of that kind (substack GUIDs included). Files
    only into the absolute output_dir named ('~' expanded; created when missing, never
    over a file: -2, -3 ...; absolute paths returned); otherwise the texts come inline, cut
    at a line.
  - The .RUL format: no rule file exported by Altium was available (the install's only
    *.rul is the router's route.rul; the private set's 111 *.rul files are not Rules6
    records: other keys and a header line). Advpcb.dll's import names 'Invalid RUL file
    structure!', RULEKIND and SCOPE1EXPRESSION, the PcbDoc Rules6 keys, so each rule is
    written as its Rules6 text record, one per line (CRLF, ASCII), with the keys, key
    order and value forms of Altium's own records: lengths in mil to 4 decimals,
    OBJECTCLEARANCES on Clearance records (empty: the generic gap), MAXUNCOUPLEDLENGTH on
    every DiffPairsRouting record (the plan's, else the board's rule scoped All, else
    Altium's 500 mil, said), IMPEDANCEPROFILEVALUE beside a profile's ID. Every line is
    parsed back with the PcbDoc's decoder (`pcb_doc.parse_text_properties`, made public),
    every key and value compared; a record that does not read back as written withholds
    the file. Advpcb.dll also holds 'Choose Design Rule Type' and 'Clear existing rules
    prior to import?': the steps say to select only the file's kinds and answer No. Its
    import into Altium, and whether it keeps the priorities, are untested: the tool, steps
    and README say to try it on a copy of the board first.
  - Made public in place: `decoupling.decoupling_design`, `directive_sync.pair_suffixes`,
    `power_tree.inductors_on`, `firmware_pins.is_crystal`. `constraints_review` points to
    the brief (Phase 1) and to generate_rule_file (Phase 2); the schematic checklist's
    SCH-SIG-09 hint names the brief; the review checklist may call it as evidence.
  - Calibrated on the AD25 examples (11 projects, 5 PcbDocs): 115 pairs (PCIe 29, DDR4
    strobes 28 and clocks 7, Ethernet MDI 12, SerDes 12, clocks 12, DisplayPort 8, USB 2 3,
    USB 3 3, 1 not recognised); 28 byte lanes (the SODIMM's 16 on either side of its series
    resistors, matching its BL classes), 24 of them already matched through xSignal rules;
    address/command groups with their own clocks; RGMII groups with RX_CLK / GTX_CLK; 28
    proposed rules left out as covered, 8 MatchedLengths rules proposed, read back
    identical. MiniPC by hand: its USB 3 port's SuperSpeed lanes and D+/D- pair apart, PCIe
    lanes with reference clocks and AC-coupled halves, DisplayPort, MDI and SGMII pairs, 12
    DDR4 byte lanes (the PCB's BYTELINE classes reused by name, all already matched), no RF
    class from its clock SMA connectors; crystals Y1-Y4 on clock generators (no load
    capacitors drawn), its active oscillators left out; 10 switching converters (8 PowerSoC
    modules and 2 regulators found by their SW pin, no external inductor). Decoupling: 8
    capacitors drawn at a pin, 166 at their IC, 230 as the only IC on the rail, 140 shared;
    68 IC rails within 5 mm, 2 beyond, 12 with bulk capacitors only. On a private set of
    63 boards, aggregates only: 160 pairs (Ethernet 56, USB 2 47, USB 3 6, CAN 1, 50 not
    recognised, mostly board-to-board connector pairs) in 53 pair classes; 411 power rails
    in 58 projects, none with a known current, so each asks for current= (with 3 A on every
    rail, 6 width classes are taken by narrower board rules, none counted as covered); no
    net above 30 V (5 projects with high-voltage names ask for hv=); decoupling: 25
    capacitors drawn at a pin, 201 at their IC, 80 IC rails within 5 mm, 22 beyond; 21 TVS
    placements (5 beyond 2 mm of the pin), 3 crystal groups, 90 switching converters; 76
    placement notes quoted in 29 projects; plans with 49 rules in 30 projects (19
    DiffPairsRouting from a board impedance profile, every IMPEDANCEPROFILEVALUE as the
    board's own rules write it; 30 MatchedLengths), all 49 records and 178 of generic
    hand plans read back identical, no priority repeated within a file; at most 0.28 s per
    board after the compile, first page at most 29 KB.
- `check_directive_sync` (roadmap #4, narrowed): whether the schematic's directives
  reached the PcbDoc, offline and read-only (`server/services/directive_sync.py`); it
  needs the netlist and a PcbDoc, and reads the sheets as saved now through the offline
  compile.
  - Schematic side, per item with its sheets, places and nets: net classes from Parameter
    Set directives with ClassName, differential pairs from Differential Pair directives
    (legs named by Altium's _P / _N and the project's [DiffPairSuffixN] pairs, a channel
    copy's room suffix set aside; with a ClassName, a differential pair class), design
    rules from Rule parameters, net classes for buses (Generate Net Classes for Buses),
    per-sheet component classes (named after the sheet symbol, the channel's room, or the
    sheet in a flat design; a multi-part component split over sheets is in none; a part
    Update PCB may not place, mechanical, graphical or without a footprint, is listed as
    parts_not_on_pcb and not compared, and a sheet with only such parts gets no class) and
    their rooms (named after the schematic's class), and user-defined component classes
    from a ClassName part parameter. Each kind says whether the project's Class
    Generation options enable it (`class_generation`, Altium's defaults where a key is not
    written, listed); with a kind left out Update PCB neither adds nor changes its
    classes, so its findings are info with option_off (a pair class is taken to follow
    the user-defined net classes switch: unconfirmed).
  - Coverage: the offline compile now records each Parameter Set's nets
    (`ErcParameterSet.nets`, `.via`): the wire's net, a bus's members, the nets a signal
    harness carries (its connectors' entries), or the nets under a blanket (RECORD=225,
    wires and objects partly or wholly under it) when the directive sits on its edge; the
    compiled sheet copies (`ErcFacts.instances`) and the buses with their member nets
    (`ErcFacts.buses`). A directive on a blanket edge no longer reads as floating in
    `run_erc`.
  - PCB side: net, differential pair and component classes with their members, pairs,
    rooms (RoomDefinition rules, by the component class they scope) and the rules marked
    DEFINEDBYLOGICALDOCUMENT. Nets follow `check_sch_pcb_sync`'s matching (members, then
    name; `sch_pcb_sync.sync_maps`), so a net renamed on the PCB is still found; parts
    follow their unique-ID link. A class whose name is not on the PCB is matched by its
    members as Altium's Comparator does (the project's Min Match % and Min Matched
    Members per object kind): `class_name_differs`.
  - Rule parameters are decoded leniently: KEY=value properties as Rules6 stores them
    (RULEKIND, NAME, SCOPE1EXPRESSION, MINLIMIT ..., per-layer widths and gaps) or a
    description ("Width Constraint (Min Width = 10mil)"), each value read whole with its
    unit (mil, mm, cm, um or inch; "1e1mil" is 10 mil, an unknown unit is not read);
    matched by unique ID, then name, and every schematic-defined rule of the kind whose
    scope names a covered net, the class or the pair (a directive on a bus or blanket may
    give one rule per net, none of them stale); values compared per field and layer. No
    project read so far carries a Rule parameter, so the format is unconfirmed
    (rule_text_not_decoded says when it cannot be read).
  - Project type: classic, or Constraint Manager when the .PrjPcb's [Design]
    ConstraintManagerFlow switch is not 0 (the name Altium's own code gives it; written
    as 0 in a classic project, and then it outweighs any other trace, listed as
    contrary_evidence), a Settings/Constraints file or a Constraints.xml beside the
    .PrjPcb (only its root and top-level element counts are reported), a .CstrDot, or the
    PcbDoc's ConstraintManager
    stream (base64 of zlib data; empty in a classic board) says so. Other .PrjPcb keys
    naming the Constraint Manager are listed, not taken as evidence. In a Constraint
    Manager project every finding is info with reason constraint_manager_project:
    directives are ignored unless imported.
  - Findings: directive_class_missing (info when Class Generation leaves it out),
    bus_class_missing (info), class_name_differs, class_members_differ (members missing
    from the PCB class: warning; PCB members no directive gives: info; designators the
    .Annotation file gives several parts are named; ClassName directives of the class on
    no net are counted, as one moved off its wire leaves its nets in the PCB class),
    diff_pair_missing (also a directive on one leg, or a net without a polarity suffix),
    diff_pair_nets_differ (swapped legs, other nets), diff_pair_name_differs (the same nets
    under another name, ignoring case: info), directive_rule_missing, rule_values_differ,
    rule_text_not_decoded, component_class_missing, room_missing (one info finding when
    the board has none of the generated rooms) and stale_pcb_item (a schematic-defined rule
    no directive or power port gives now, and PCB pairs and net classes no directive gives
    where the project defines that kind by directives: info). A SupplyNets rule on a net
    with a power port is the power port's, not stale. summary counts per kind items,
    transferred, missing, differing, disabled by options and from directives. kind= and
    sheet= (with or without .SchDoc) narrow; findings page with offset, the directive rows
    with directives_offset; the page shrinks to fit.
  - `constraints_review` points to it for schematic Differential Pair directives;
    `check_diff_pairs`' not_checked names it; the schematic checklist's SCH-SIG-09 is now
    assisted with it as evidence (leaning yes only when pairs, net classes or rules from
    directives reached the board, not on bus classes alone).
  - `sch_pcb_sync.sync_maps` gives check_sch_pcb_sync's net and part matching, and the
    parts Update PCB may not place, to other services.
  - Calibrated on the AD25 examples: all 115 Differential Pair directives (MiniPC 96,
    SODIMM 18, WiFi 1) found on the PCB, checked by hand against the raw
    DifferentialPairs6 records (4 MiniPC pairs only through nets whose case differs on the
    PCB); SpiritLevel's 3 bus classes and its sheet classes checked by hand against
    Classes6 (its one mechanical part without a footprint, on no PCB part, is left out of
    its sheet's class). On a private set of 63 boards, aggregates only: all classic (58
    with ConstraintManagerFlow=0, 60 with the empty ConstraintManager stream); directives
    on 1064 wires, 55 floating, none on a bus, harness or blanket; 99 ClassName net
    classes in 19 projects (84 transferred, 15 with extra PCB members, 6 of these beside a
    ClassName directive of the class on no net), 98 directive pairs in 25 projects (all
    transferred), no Rule parameter; 278 schematic-defined PCB rules, all SupplyNets, 274
    on a power-port net; 150 per-sheet component classes (125 transferred, 12 differing, 3
    of them renamed on the PCB and matched by members, 4 missing in one project, 9 left
    out by the option; 32 parts Update PCB may not place, 26 of them listed in a PCB class
    all the same, left out of the comparison); 62 of 63 projects without a warning; at
    most 0.02 s per board after the compile, first page at most 19.9 KB.
- `check_mechanical_fit` and `get_mechanical_handoff` (roadmap #34): mechanical fit and the
  mechanical handoff of the PcbDoc, offline (`server/services/mechanical_fit.py`); a PcbDoc
  is required. Plan-view outlines and heights: not a STEP or IDF / IDX replacement.
  - Heights per placed part: the tallest 3D body projected on its side (ComponentBodies6
    overall height, standoff and model; a flat body of height 0 gives no height), else the
    footprint's Height, else the tallest body of its library footprint (read with
    check_libraries' resolver for the parts that have neither, at most 300), else none.
    Parts without a body are `missing_3d_body` (info; a warning when nothing gives a height
    and a limit the user or a drawing gives applies); graphical parts, net ties,
    fiducials, pad-less parts, SMD test points and card-edge fingers (four or more
    elongated SMD pads only, each within 0.5 mm of the board edge: board copper) are not
    expected to have one. Not Fitted parts of the active variant are listed apart and take
    part in nothing else (their mounting holes are still listed, fitted false: the hole is
    drilled, its screw not checked). A part's
    plan-view shape: its bodies, else its courtyard (the box, in the part's frame, of its
    lines on a mechanical layer named courtyard or placement outline), else its pad box.
  - Limits, the tightest that applies: height_limit_top / height_limit_bottom; regions=
    (polygons or rectangles in the board origin's frame, per side, with max_height or as a
    no-parts keepout); free 3D bodies (an enclosure part placed on the board: its standoff
    is the limit, and it is a keepout when it reaches the board); areas on a mechanical
    layer named for a height restriction with a length in its name; Altium's Height rules
    (MAXHEIGHT, MINHEIGHT), the rule that applies to each component found by scope and
    priority; and a maximum component height Draftsman notes state (only when every such
    note agrees for a side; quoted with its place; a line with one top and one bottom
    length gives both; a note for one place or with exceptions, such as "under the shield"
    or "except connectors", is quoted in limits.draftsman_local_notes, not applied).
    summary.parts_with_height_limit counts the parts a real limit applies to (Altium's
    default 1000 mil Height rule is none). Findings `height_over_limit` and
    `height_under_min` (warning, with the limit's source and the margin) and
    `part_in_keepout` (bodies or courtyard over a no-parts region: a user keepout, a free
    body reaching the board, an area on a mechanical layer named as a component keepout).
    Keep-Out layer areas, outer-layer copper keepouts and rooms are regions listed with the
    tallest part over them (Altium's DRC judges them).
  - Mounting holes: free pads and bare holes of 1.5 mm or more and the holes of parts that
    are mounting holes (holes, SMD pads only as copper rings centred on them, and an
    H/MH/MTG designator, a mounting-hole or screw footprint name, a Mechanical part or at
    most one net), with drill, slot, plated, net, pad (its own pad or ring), the metric
    screw whose thread passes (ISO 273 clearance; the hole taken to 0.001 mm, so a 2.6 mm
    hole stored as 102.3622 mil takes an M2.5) and the free radius per face: the nearest
    copper of another net on the outer layer, another fitted part's body or pad box, or the
    board edge, with what limits it; copper of the hole's own net is given apart, a free
    hole's free ring pads are its own and pad-less parts covering the hole are its
    hardware. A hole in a board cutout is off the board (off_board, in_cutout).
    `mounting_hole_clearance`: a part or exposed copper (pads, untented vias) within half
    the larger of the head and washer diameters plus clearance_margin (defaults: the
    screw's ISO 7045 pan head and ISO 7089 washer, 0.5 mm: info; screw_head_diameter /
    washer_diameter given: warning); copper under the solder mask only is one grouped info
    finding. `hole_near_edge`: the wall closer to the edge than the board thickness, at
    most 1 mm (info; half of it: warning).
  - ICT: the probe side check_testpoints decides from the probe sites, or probe_side; parts
    at least tall_part_height (5 mm) on it are `tall_part_on_probe_side` (info; a warning
    when probe_side is given) with the probe points within 5 mm of them.
  - Connectors (a connector designator prefix and no crystal, holder, jumper or test-point
    text, external or internal by their text, the netlist's descriptions included): the
    nearest board edge (left, right, top, bottom, another outline edge or a cutout), the
    distance of the bodies and of each pad to it, the signed edge offset (negative: the
    overhang past the outline) and the outward direction faced at an edge (on an arc
    edge, the radial direction where the part is: at its point nearest the arc, or its
    vertex deepest past it).
  - Summaries: per side the tallest parts, height classes and the area used; per region
    (user, keepout, room, free body) the parts over it and the tallest. Findings, part rows
    (tallest first), connectors, regions and holes page and shrink to the result size.
  - `get_mechanical_handoff`: board (thickness from the layer stack, size, extent, area),
    outline polygon with cutouts (arc edges as chords), mounting holes, connectors, regions
    with polygons, rigid-flex board regions (layer stack, flex, the thickness of the layers
    its stack holds, bending lines; the board's thickness is then the rigid stack's),
    per-side summaries and part rows; sections= picks them. An AutoCAD R12 ASCII DXF and an SVG (mm, origin-relative,
    one layer or group per kind) are written only into the existing output_dir the user
    names, never over a file (a new one is numbered).
  - `package_for_xfn`: with a PcbDoc the snapshot gains the `mech_board`, `mech_outline`,
    `mech_mounting_holes`, `mech_connectors`, `mech_regions` and `mech_parts` tables (mm,
    origin-relative; `export.MECHANICAL_TABLES`); the existing tables are unchanged. A
    handoff that cannot be built leaves them empty with a warning.
  - Calibrated on the AD25 examples (all part heights against the ComponentBodies6 text,
    every mounting hole's free radius by brute force and every connector's edge offset
    agree) and on a private set of 63 boards, aggregates only (an independent review
    re-derived heights, holes, free radii and connector offsets from the raw streams;
    every connector facing points off the board).
- `check_diff_pair_geometry` (roadmap #28): differential-pair routing geometry on the
  PcbDoc, read-only and offline (`server/services/diff_pair_geometry.py`); a PcbDoc is
  required.
  - Pairs: the PCB's differential pairs, and nets named as complementary legs that are not
    one (the naming check of `check_diff_pairs`, now `diff_pair_checks.inferred_pairs`),
    marked inferred: no DiffPairsRouting rule applies to them, so their findings are info;
    an inferred pair routed nowhere is left to the naming check. pair= (pairs or their
    nets) and net_class= (a differential-pair or net class) select them.
  - Coupling: each leg's tracks and arcs on a signal layer, chained where they meet, are
    sampled every 2 mil along the centreline (an arc on its true arc, not its chords). A
    sample is coupled where a track or arc of the other leg runs parallel beside it (within
    10 degrees, its perpendicular foot on that track or arc) within the coupling distance:
    3x the layer's max gap (else its preferred gap), at least 2x the width, 3x the width
    without a gap limit, or coupling_distance=. Concentric arcs couple along their common
    angle, an arc and a track where their tangents agree, and at a bend of up to 50 degrees
    the inner leg's track is extended past its corner so 45 degree bends stay coupled. The
    gap is the centre distance less both half widths.
  - Limits per track and arc: the DiffPairsRouting rule that applies to it (scope and
    priority decided per object as check_rule_compliance does) with its per-layer min,
    preferred and max width and gap and Max Uncoupled Length; a profile-driven rule's
    impedance profile fills what the rule leaves out on a layer; without a rule, the
    board's only differential impedance profile solving the layer, as an assumption
    (limit_source layer_profile: its gap and width findings are info); else none
    (measured, not judged: limit_source none).
  - Findings: gap_below_min / gap_above_max (runs of the coupled gap outside the rule's
    range, 0.1 mil allowed, with location, length, gap, widths and the excess beyond the
    limit: a warning when at least min_stretch long, 20 mil by default; info when
    shorter, a breakout of at most 50 mil starting within 50 mil of the pad's or via's
    copper edge before the first stretch within the range, marginal: at most 0.25 mil
    beyond the limit, as 0.125 mm routed against 0.12 mm, still counted as uncoupled),
    width_out_of_range (tracks and arcs, an arc counted once as check_rule_compliance
    counts it: warning; also_reported_by check_rule_compliance when judged by the rule's
    own width, which that tool reports too), gap_varies (info: the 5th to 95th percentile
    spread of the in-range gap over at least 200 mil of long stretches),
    uncoupled_length_high (the longer leg's routed length less its length coupled within
    the gap range over Max
    Uncoupled Length: warning; without one, over a quarter of its routed length and 200
    mil: info), unmatched_layer_change (a via joining one leg's tracks on two layers with
    no via of the other leg joining two of the same layers within via_distance, 2 mm by
    default, so a stub on a third layer does not break a match: warning), pair_not_routed
    and partially_routed (info).
  - Per pair: per layer its routed and coupled length, coupled stretches, gap (min, mean,
    max), widths, limits and coupling distance; each leg's coupled_in_gap_range and
    uncoupled length (they add up to its routed length), the uncoupled length's pad and
    via breakouts (an uncoupled run at a pad or via, inside its copper and up to 50 mil
    beyond) apart from the rest; layer changes per leg with how many via pairs match and
    their spacing (the summary counts matched vias). Findings and the pairs rows are paged
    apart (offset, pairs_offset) and the page shrinks to fit (pairs with findings first).
  - check_rule_compliance and check_diff_pairs now point to it for the uncoupled length;
    constraints_review's copper spot checks call it per interface; it is an evidence tool
    for checklists and custom checks. Reused, made public in place: `return_path.chains`
    (the chaining of a net's tracks), `return_path.length_text` (lengths in messages, now
    without an exponent from 1000 up), `reference_planes.Grid` and
    `pcb_compliance.object_id`.
  - Checked on Altium's examples (3 boards with pairs, 115 pairs, all under a rule;
    MiniPC, 96 pairs, about 1 s): 57 coupled stretches on MiniPC (7 of them on arcs)
    recomputed from the track coordinates (line or radius distance less half widths)
    agree to 0.0001 mil; an independent recomputation from the raw track, arc and via
    records gives the same routed length on all 230 nets (0.01 mil) and the same
    uncoupled length within 2 mil on 219 of them (within 5 mil on 229), the same rule and
    limits for every pair and the same layer changes (all 274 vias pair up). 24 gap
    warnings with 29 stretches (a 434 mil one routed 0.5 mil farther apart than its rule
    takes one pair over its Max Uncoupled Length) and 3 uncoupled-length warnings; three
    0.2 mil roundings are marginal. On a private set of 63 boards (31 with pairs, 160
    pairs: no errors, at most 0.22 s each) no pair is over its Max Uncoupled Length, and
    every breakout of 20 mil or more lies within 25 mil of a pad or via.
- `check_return_path` (roadmap #29): a return-path and reference-plane review of the
  PcbDoc, read-only and offline (`server/services/return_path.py` on the new reference
  copper model `server/services/reference_planes.py`); a PcbDoc is required. A geometric
  screen of the copper as saved, not a field solver; Altium's Return Path rule is not
  decoded.
  - Reference layers per signal layer: the references an impedance profile of the Layer
    Stack Manager names for it (REF_TOP / REF_BOT of its enabled entries), else its nearest
    copper layers above and below, each with the summed dielectric height between, the
    copper layers skipped and its kind: internal plane, large pour (one net's area copper
    covers at least 15% of the board outline) or signal layer; the stack neighbours are
    listed too. A routed signal layer with no plane or large pour reference is flagged
    (no_adjacent_reference: a warning when it carries selected nets, else info).
  - Internal planes decoded as negative layers: the net from PLANEnNETNAME, the regions
    from the saved Split Plane polygons with their nets (the innermost wins; one region of
    the plane net without them), the pullback from the stack (new
    `BoardInfo.plane_pullbacks`: V9_STACK_LAYERn_PULLBACKDISTANCE, PLANEnPULLBACK, the
    layer cache; 20 mil default) along the outline and cutouts, tracks, arcs, regions and
    fills on the plane as voids (split lines told from the boundary lines along the edge;
    keepouts are not voids), and an anti-pad around every via, through-hole pad and bare
    hole crossing it unconnected (another net, no net, or a No Connect Power Plane Connect
    Style, VIA./PAD. keys read): its copper grown by the Power Plane Clearance rule (20 mil
    without one). Signal layers: tracks, arcs, pads, vias and area copper as check_dfm's
    copper model holds them; a hatched pour as its saved hatch tracks and arcs with the
    openings of its grid bridged inside its outline (new `PolygonPour.grid_mil` and
    `track_width_mil` from GRIDSIZE and TRACKWIDTH; its outline when they are missing), so
    the channels a hatch leaves out are voids. Coverage along a segment cuts it exactly at
    every boundary and names each piece by its net.
  - Selected nets (with their reasons): net= (nets or differential pairs) and net_class=,
    else the members of differential pairs, of net classes with a high-speed name, of
    xSignals, of the scopes of impedance-profile-driven Width and DiffPairsRouting rules
    and of Length and MatchedLengths rules (other than All, or than a scope taking in 90%
    of the routed nets), and nets named like clocks or high-speed buses (PCIe, USB data,
    SATA, HDMI, LVDS, MIPI, DDR, Ethernet, SerDes, RF ...) but not their sideband nets
    (enables, resets, wake and clock requests, I2C/SMBus, debug ports, LEDs); power and
    ground names (prefixed supplies such as AVDD, IOVDD, PGND, P3V3 and AUX rails
    included) are left out.
  - Segments: every track and arc of a selected net, chained where they meet, over the
    copper of each plane or pour reference of its layer. Reference copper is a plane's
    nets, or on a pour layer the nets with at least 5% of the board and supply or ground
    nets; another net's copper (a signal net's fill or routing) is a gap, and so is a
    reference net's routing copper with a gap on both sides (a GND via's pad in a PWR
    pour). The net's own vias and pads (and its differential partner's) are left out with
    their clearance: the anti-pad on a plane, on a pour the clearance measured in the
    saved copper on eight rays from the hole; the cut-out measured under its own SMD pads
    likewise. segment_crosses_split: the reference net changes under it across a gap of
    at most 100 mil (warning); segment_over_void: no reference copper under part of it,
    with length, both ends and cause (split line, drawn void, plane edge or pullback,
    outside the split planes, merged via clearances, no copper, copper without a net,
    another net's copper, a reference net's island; warning), info for one other via's or
    pad's clearance (as measured), the cut-out under the net's own pad, or where a
    stripline's other reference stays one net's copper across it;
    segment_near_plane_edge: reference copper of another net, or none, within the edge
    distance on either side (3x the dielectric height to that reference, edge_distance=
    overrides; via clearances are not edges; info).
  - plane_split (info): internal planes split among several nets with each region's area,
    and pour layers used as a reference whose copper is split among nets with their
    shares.
  - Stitching: each layer change of a selected net (a via or through-hole pad its tracks
    end on in two signal layers, via-in-pad included) whose layers share no reference
    layer needs a via of the reference net through a reference layer of each side within
    stitch_radius (1 mm default), or, where the reference net changes (GND to a power
    plane), a capacitor between each pair of reference nets that changes with a pad
    within it (designator C and a number, or a C-prefixed part whose comment or footprint
    says capacitor or farads; never CN, CON or CR). missing_stitching_via: info, a warning
    for a reference net change without a capacitor (pairs_without_capacitor); a
    differential pair's two vias of one layer change are judged together. layer_changes
    counts every outcome (stitched, shared_reference, missing, reference_net_change,
    stitched_by_capacitor).
  - Places resting on approximated copper (anti-pad sizes, default pullback, bridged hatch
    openings, assumed pour outlines, a plane whose split lines are not partitioned) are
    marked likely; approximations lists every assumption. Findings are grouped per net,
    layer and reference with their places (longest first), paged, and the page shrinks to
    fit.
  - run_review_checklist: PCB-HS-06 (continuous reference plane, no plane splits, layer
    changes next to a return via) is now assisted by check_return_path.
  - Checked on Altium's examples (6 boards, 649 selected nets, 36,849 segments; MiniPC,
    the largest, about 15 s) and a private set of 63 boards (no errors, at most 0.25 s
    each): an independent point-in-polygon reading of the saved copper found no reference
    copper inside any of the 864 voids reported, both nets' copper beside all 66 split
    crossings, and the same outcome for 1,366 of 1,368 layer changes (the other 2: the
    sampled rings differ).
- `check_power_paths` (roadmap #31, phase 1): a power-path estimate per rail on the
  PcbDoc, read-only and offline (`server/services/power_paths.py`,
  `server/parsers/power_analyzer.py`); a PcbDoc is required (`no_pcb_document` without
  one). Every result is labelled an estimate: Power Analyzer by Keysight, the datasheets
  or a measurement decide.
  - Currents per rail, each with its source: current= wins ('VIN=3,3V3=0.5' amps per rail;
    'U5=0.3' per load designator, on its supply rail, the one holding most of its supply
    pins; 'U5@3V3' picks the rail; a converter's designator is its output current, so the
    rail it makes carries it and its input rail its input current, as check_thermal's load=
    does, while 'U3@5V' on its input rail is its input current only; a connector's,
    battery's or series part's designator is the current it supplies or passes).
    Otherwise a rail carries the sum of what draws from it: its power-tree loads, its
    outlet connectors (connector power pins on a rail made on the board: an SFP cage, a
    PCIe slot's auxiliary rail) and the parts current= or the setup name with a pin on it.
    A load's current comes from current=, from a Power Analyzer or PDN Analyzer setup
    (used unasked only when it is the one setup with load currents and names this PcbDoc,
    or names no board and sits in the project folder; setup= names one; its resistive
    loads draw V / R, its converters what it lists on their input, '<id>|x|y|U1' part names
    read as U1), or from a 'Current Consumption' (or Supply Current, Operating Current,
    Icc, Idd ... with a unit in the value or the name, the largest current the value
    states) part parameter; a converter on the rail draws its output current (linear
    regulators, load switches, eFuses) or Vout x Iout / (eta x Vin) (switchers, charge
    pumps; eta= parsed as check_thermal's, voltages from rails=, names and the power tree
    as check_derating's); a series part (ferrite, fuse, 0 ohm link, diode, FET) carries
    the current of the rail it feeds. With some loads unknown the known sum is a lower
    bound (partial, with how many are known, carried through converters and series parts);
    what is missing is listed (needs), and current= or setup entries that apply to no rail
    are listed in notes.
  - Copper per rail net: tracks and arcs per layer with widths and routed length, vias with
    their drills, pads, the area of pour, region and fill copper (hatched pours' tracks
    count as pour) and the internal planes assigned to it. A rail whose pours, regions and
    fills hold more than half its copper area, or that has a plane, is pour-fed: its
    resistance and drop are not estimated (pour_fed_not_estimated) and its narrowest track
    is still listed with the rise if the whole rail current used it; a rail with less pour
    says its paths' resistance is an upper bound (pour_note).
  - Narrowest routed width per layer and overall, leaving out stubs into pads (pieces
    shorter than stub_length, default 1 mm, with an end on a pad; the narrowest stub is
    listed), with its layer's copper thickness from the stackup (Altium stores one thickness
    per layer; 1 oz labelled assumed where the stack gives none; copper_oz= '2',
    'outer=2,inner=1' or per layer name) and its current capacity at 10 and 20 C rise.
  - IPC-2221 (section 6.2, the curve fit of its charts): dT = (I / (k x A^0.725))^(1/0.44),
    A in mil^2, k 0.048 external and 0.024 internal; it reproduces the trace-width
    calculators' 11.8 mil (external) and 30.7 mil (internal) for 1 A at a 10 C rise in 1 oz
    copper. Its charts end at a 100 C rise: a rise beyond is reported as beyond_chart, not
    as a figure. The fit assumes a long conductor, so stubs into pads are not judged
    (hottest_neck lists the worst). IPC-2152 is chart-based and often less conservative
    (mainly inside): cited, not applied.
  - Resistance: tracks and arcs (true length) rho x L / (w x t) over their length outside
    the pads and vias they join (that copper is at its pad's potential, 0 ohm: a track
    ending in a pad, or touching one and running over it, counts from the pad's far edge),
    rho 1.72e-8 ohm m at 20 C with 0.00393 /C (temperature=); via barrels rho x h / (pi x
    ((d/2 + p)^2 - (d/2)^2)), h the barrel between the layers the current enters and leaves
    from the stackup (a 1.6 mm board, labelled assumed, when the stack has no heights),
    plating p 25 um (plating=), plated through-hole pads alike. The copper graph is
    check_lengths' NetGraph: nodes joined by 0 ohm edges merge, and barrels or tracks
    between the same two nodes combine in parallel.
  - IR drop, phase 1 (no resistor-network solver): from the rail's source pads (a
    converter's output pins, or the inductor, diode or FET its output passes through; a
    connector's or battery's power pins; the series part feeding the rail) to each load's
    farthest supply pad (each pad reached at its nearest layer), the least-resistance path
    (Dijkstra) with its resistance (tracks and vias apart), track length, physical vias,
    layers, narrowest width and layer changes (a via crossed in several barrel pieces is
    one change). The paths form a tree from the sources: each piece carries the currents
    of the loads downstream of it and the drop to a load is the sum of R x I along its path
    (drop_mv); where some of those loads' currents are unknown it is a range, from the
    known loads' currents to the rail's current through every piece whose loads are unknown
    (drop_low_mv, drop_high_mv; a piece every load's path uses carries the whole rail
    current). Loads with no track path (reached through pour or plane copper, or not
    routed) are listed apart.
  - Findings, each judged at the current its copper carries, where only an upper bound
    crosses a threshold at most info: temp_rise_high (above rise_info, default 10 C: info;
    above rise_warning, default 20 C: warning; one per rail, its hottest track),
    ir_drop_high (above drop_info, default 2%, or drop_warning, default 5%, of the rail
    voltage), path_through_few_vias (a set of vias, counting those within one via's
    resistance of the path's copper on both layers as parallel, carrying more than
    IPC-2221's external formula on their barrels allows at those rises; with the vias
    needed and the loads whose current crosses it; one per set of vias),
    power_analyzer_fail (a Power Analyzer report's failing net: warning; its warning
    status: info), needs_current, needs_voltage (rails with a drop in mV and no known
    voltage to judge it against) and pour_fed_not_estimated (info). Rails that cannot be
    estimated say why: no_source (the power tree finds none), no_source_pads, no_loads,
    own_supply (a part's own regulator feeding its own pins), no_load_pads, no_routing.
  - Power Analyzer by Keysight: a <Project>.pdnaK document is reported (read only when it
    is plain text); the earlier PDN Analyzer's setups (*.pdna XML, *.pidc_config JSON, as
    Altium's SpiritLevel-SL1 example ships them) are listed with their sources, loads,
    series parts and VRMs; HTML reports under PowerAnalyzerByKeysight_Output\HTMLReport
    (Report.html and the per-net pages, as Altium documents them) are parsed generically
    (tables by their header cells, label/value pairs). A report whose folder
    ('<Project> [PDNA]_<date>') or pages name this project or PcbDoc replaces the
    estimate for the rails it covers with its Vdrop, Idensity, Ivia_max and status (the
    estimate and the counts of its findings kept beside them; a failing net is a finding);
    another project's report is listed, not applied.
  - The tool takes rail=, net=, checks=, current=, eta=, rails=, copper_oz=, plating=,
    temperature=, stub_length=, rise_info=, rise_warning=, drop_info=, drop_warning=,
    setup= and units= (mm default, or mil); one threshold given past the other's default
    moves that default. Findings and rails page together and the page shrinks to fit; the
    prepared result is kept on the PCB index (the last four argument sets). Listed among
    the netlist tools, the review checklists' evidence tools (so check packs can gate on
    it), the server instructions and the manifest. thermal.parse_values reads current=.
  - NetGraph (check_lengths, get_signal_length) now joins copper that touches exactly:
    its containment tests allow 0.01 mil, so float rounding of stored coordinates no longer
    leaves tangent end caps, pad edges and touching vias apart (on MiniPC this joins 51
    more stitching vias on GND; no other example net's length changes). Its pad test is
    public as pcb_length.pad_contains.
  - Calibrated on the 11 public Altium example projects (5 with a PcbDoc) and 53 private
    boards (aggregates only). Of 102 rails on the examples and 450 on the private boards
    (452 in their power trees, 2 not on the board): estimated 44 / 179, pour- or
    plane-fed 37 / 110 (36% / 24%), no load 6 / 60, a part's own regulator 11 / 30, no
    source 3 / 62, no routing 0 / 9, loads without pads 1 / 0; outlet connectors are 5 of
    the examples' 58 paths and 74 of the private boards' 301. No private board has a
    current-consumption parameter (none of its 107 current-like parameters is a
    consumption name: most are fuse, connector and other ratings) or a Power Analyzer
    file; MiniPC's three ISupply(mA) parameters sit on LDOs, whose input current comes from
    their outputs; SpiritLevel-SL1's six PDN Analyzer setups are listed and, with
    setup='ALL_PWR_Nominal', give VCCINT 0.9 A, VCCO 0.88 A (6 of 7 loads, resistive ones
    as V / R) and 5V 1.80 A (a lower bound through the LDOs). Narrowest routed widths
    (stubs into pads left out): on the examples mostly 0.2-0.3 mm (65 of 100), on the
    private boards 139 at 0.2-0.3 mm, 95 at 0.3-0.5, 81 at 0.5-1 and 54 of 1 mm or more (of
    381), on outer layers 94% and 91% of the time. Path resistances on the private boards:
    57 below 1 mOhm, 116 at 1-5, 67 at 5-20, 52 at 20-50 and 7 above 50 mOhm. Against an
    independent model of the copper (raw stack, own joins and clipping, own Dijkstra) the
    examples' 58 paths agree within 1% for 49 and within 6% (0.31 mOhm) for all. With 1 A
    on every rail: 9 temperature-rise and 3 drop warnings on 10 private boards (22 and 8
    infos), 23 boards asking for rail voltages; with 3 A: 64 rise warnings (28 beyond the
    charts), 9 drop and 62 few-via warnings on 25 boards. Runtime on the private boards: a
    median of 0.02 s and at most 0.15 s per board once the copper graphs are built; on
    MiniPC (84 rails) about 0.7 s for the first call and 0.6 s after. The default page
    (indent 2) has a median of 11,300 characters on the private boards; 5 of 53 shrink to
    15 rails, as MiniPC does.
  - Not covered (phase 1): a resistor-network solution (parallel copper off the
    least-resistance path, pours, regions and planes only lower the true resistance, and a
    load's other supply pads share its current), current spreading and density in pours,
    ground return paths and their drop, IPC-2152's charts, AC and transient current,
    copper roughness and plating thickness variation, the copper's own heating feeding back
    into its resistance, and the Power Analyzer by Keysight .pdnaK format when it is not
    plain text (its HTML reports are read instead).
- `check_pin_table` (roadmap #22): a pin table from a datasheet or a vendor pinout file
  compared with a part's pins, pin by pin, offline.
  - The table (`server/parsers/pin_table.py`): pasted text (tabs, commas, semicolons,
    pipes, or the runs of spaces or single spaces a PDF copy gives, whichever reads the
    most rows; CSV quoting; with single spaces a header's two-word names such as "Pin
    Number" are joined back and a row's extra words go to the name, the type or a last
    description column, whichever leaves a pin number and a type word in their columns,
    other rows listed as unread; header row optional, the columns inferred from the
    values without one) or a
    file (`.csv`, `.tsv`, `.txt`, `.xlsx` through `verify_outputs`' workbook reader;
    `.xls` and PDF are refused with what to do). Columns are found by their header names
    (pin number or ball, name, alternate functions, type or direction, bank or unit); a
    cell listing pins ("4, 9, 15", "12-15") gives one row per pin. Vendor formats are
    recognised by their header row: AMD/Xilinx package pin files (types from the names,
    since I/O Type is the bank type; the No-Connect column; the Total Number of Pins
    footer), Intel pin-out files (one section per package, each with its own header, or
    several package columns; Optional, Configuration, HPS and Tx/Rx channel functions kept
    as alternates), STM32CubeMX pinout CSVs (Signal and Label as alternates) and datasheet
    tables with a column per package. Rows that could not be read are listed with their
    line and reason; rows a package does not have are counted. Footnote marks are dropped
    from names, except an attached (n) the table uses as a bus index (D(0), D(1)).
  - Names are normalised (case, separators, `I/O`, `N/C`, leading zeros, `D(3)` as `D3`,
    Unicode dashes and minus signs, and `nRESET`, `RESETn`, `~RESET`, `!RESET`, `/RESET`,
    `RESET#`, `RESET_N`, `RESET_B`, Altium's `R\E\S\E\T\` and a Unicode overline as one
    active-low RESET); a name holding several functions ("PA9/SERX9_TXO", "I/O, VREF 7,
    L49P_Y", "IO (DIN, D0)", "OE/R\E\S\E\T\") is split into them.
  - The comparison (`server/services/pin_table.py`): the placed part (every pin's name and
    electrical type from the netlist, units merged, the nets of connected pins) and its
    library symbol (resolved as `get_library_part` does; every unit's pins, hidden ones
    marked), or any symbol by `lib_reference=`; `compare='both'|'placed'|'library'`.
    Pins pair by number (BGA balls in natural order; a prefix every number of one side
    carries, such as P1..P208 against 1..208, is dropped or added when that pairs them).
    Names are classed exact, equivalent (counted, with examples), likely_same (info,
    grouped by reason: an alternate function, functions added, active-low marking,
    another ground or NC name, tokens reordered, an index, voltage or qualifier more such
    as VSS_1 / VSS or VREF 7 / VREF Bank 7, an abbreviation of three letters or more such
    as RST / RESET; functions paired one to one) or different. Names close enough to be
    other pins are different: a polarity or channel suffix (CLK_P / CLK_N, OUT_A / OUT_B),
    a word added (CLK / CLK_EN), a short or one-letter abbreviation (CE / CEO, IN / INT,
    DQ0 / DQS0, VDD / VDDA), and a resembling name that is another pin's own name on the
    other side (`why` says which pin). Findings: shifted runs (three or more names offset
    by a constant), swapped pins (names exchanged exactly first), different names, pins
    missing in the design (NC pins as info) or extra (shield, mounting and NC pins exempt,
    not signals such as SH_CP; an exposed pad numbered differently paired; a table of
    fewer than half the design's pins lists the others once, as `not_in_partial_table`
    info), duplicated numbers and names (over the pins both sides have), type mismatches
    (power vs signal, input vs output; milder differences as info), hidden signal and
    power pins, power pins on no net and NC pins on a net. With several packages the one
    matching the part best is used unless `package=` names one, with each package's
    score. A finding both sides have is reported once (`in`, the placed pin's net and the
    symbol's hidden mark kept), and `placed_vs_library` says how the placed part differs
    from its symbol. The verdict is match, differences, or poor_match when fewer than half
    the table's pins match a name of the part (another part or package, or another
    numbering).
  - Calibrated on the public examples: tables built from their own symbols match with no
    name or number finding in five text layouts, and dropped, added, renamed, swapped,
    shifted and retyped pins are all found, as are swaps of differential pairs, channels,
    complements and near names; the Spartan-IIE PQ208 pinout of the datasheet
    and the Arria 10 NF40 pin-out match the examples' FPGA symbols (naming-style
    differences as likely_same info, the NF40 package picked by its score).
- Headless CI mode with JUnit output (roadmap #50): a `check` subcommand of the same
  entry point (`python server/main.py check <project>` or `altium-copilot-kc.exe check
  <project>`, dispatched by `main.run_entry` to the new `server/cli.py`; without a
  subcommand the MCP server starts as before) runs the offline checks in a CI job on
  every commit, with no Altium licence and no Windows.
  - Runs the review checklists (`--checklist`, default `schematic,release`, team
    checklists and overrides included) through `run_review_checklist`'s runner with one
    tool runner shared by all of them, the check packs (`--packs auto|none|<ids>`,
    `--pack-file`; plugins only with `--allow-plugins` and
    `ALTIUM_COPILOT_ALLOW_PLUGINS=1`, as #49 requires), single tools (`--checks`, any tool
    a checklist item or a pack's tool rule may call), the committed Batch DRC report
    (`--drc-report`, `get_drc_report`) and a design diff against a base revision
    (`--diff-base <ref>`: `diff_design`'s read-only git export into a temporary folder,
    the work tree untouched). `--variant` sets the variant; `--rails`, `--hv` and `--vref`
    are given to every tool that takes them, checklist items and pack tool rules
    included, and are validated first.
  - The project is loaded headless (`main.load_project_headless`): the netlist compiled
    offline from the saved sheets (a committed `.NET` is not used), Altium never started
    or asked, no registry or state-file write, no update check, nothing written into the
    project folder. The loaders take `headless` to skip the registry and the Altium 365
    status. `run_custom_checks`' run before paging is `_custom_run`, which the command
    reads every rule and finding from.
  - Statuses: the project load is a suite (a sheet that cannot be read or a netlist that
    cannot be compiled fails as an error, an unreadable `.Annotation` file is a warning);
    checklist Yes passes, No fails as an error (a custom item with its pack rule's
    severity; custom items see `--pack-file`'s rules too), N/A and needs judgment are
    skipped; a pack rule fails with its severity (a pass that left objects not evaluated
    is skipped); a single tool gives one case per finding group (check, ERC violation
    type, release gate with blocking fails as errors, rule-compliance kind, length rule,
    DRC rule, report assessment, PCB health table) with the group's highest severity,
    info never failing; the design diff's ICT hazards are warnings, and a board the base
    revision does not have (added, moved or renamed) is a skipped diff. Exit code 1 when
    a result fails at or above `--fail-on` (`error` default, `warning`, `never`), 2 for
    usage, load or configuration problems (unknown option, checklist, tool, pack, variant
    or revision, a project that does not load, an override or pack file that does not
    validate), a run that checked nothing, or an internal error, with the reports written
    either way (`--help` aside).
  - Outputs: JUnit XML (`--junit`: a testsuite per checklist, pack, tool, the project
    load, the DRC report and the diff; a testcase per item, rule or finding group with
    failure, error or skipped and the evidence in the failure text or system-out; valid
    against the Jenkins xunit plugin's `junit-10.xsd` element and attribute rules; text
    escaped and characters XML 1.0 cannot carry dropped), a Markdown summary
    (`--markdown`, also appended to `$GITHUB_STEP_SUMMARY`, shortened to fit 1 MiB:
    verdict, design state, coverage per suite and per checklist section, failing items
    with evidence and anchors, the design-diff summary, what was not evaluated; design
    text shown as written, its control characters dropped and Markdown markup escaped),
    JSON (`--json`, with each case's properties) and a short console summary (`--quiet`
    for none).
  - CI examples in `examples/ci`: GitHub Actions on Ubuntu (the design checked out, the
    server fetched at a pinned ref outside its work tree, `pip install -r
    requirements.txt`, the check, a JUnit test report and the results as an artifact),
    the same as a GitLab job (merge request and branch pipelines), and a Windows runner
    with the released executable (optional SHA256 check). README: "Continuous
    integration".
  - `services/review_checklist.runner_needs`: the paging needs of a set of checklist
    items, shared by `run_review_checklist` and the check command's one runner.
  - Calibration (a fresh interpreter per project, an empty home folder): 11 public AD25
    example projects and 63 private ones all ran without an exception, no project or
    home folder written, every JUnit file passing the `junit-10.xsd` element, attribute
    and count checks, the step summary equal to the Markdown, the project load passing on
    all 74. Defaults (schematic and release checklists): 50 of the 63 private projects
    exit 1, 13 exit 0, 4.4 s per project (median, 7.7 s max);
    the bundled items answered No most often are SCH-CMP-02 (31 boards; unchanged
    without the PC's installed libraries), REL-OUT-01 (22), REL-VAL-01 (18), REL-VAL-04
    (13) and SCH-SIG-01 (9). With `--checks
    run_erc,check_sch_pcb_sync,check_release_readiness --drc-report` all 63 exit 1 (45
    have no DRC report), 4.9 s (median, 7.7 s max). `--diff-base` was run on a public
    example in a temporary Git repository (a path with spaces, given relative to the
    repository root): the project-parameter change was reported, `git status` stayed
    clean and no temporary folder was left. The existing tools (`set_project_dir`,
    `run_custom_checks`, `run_review_checklist`) answer as on the base branch on all 11
    public examples.
- `run_custom_checks`: team-defined deterministic check packs, run offline (roadmap #49,
  `services/custom_checks.py` on a new read-only design API, `services/design_api.py`).
  Works on a netlist-free load (tool rules run; the select rules and plugins say
  netlist_required) and without a PcbDoc (distance and side assertions and side filters
  are not evaluated, never passed).
  - Packs: `check_packs.json` or `check_packs/*.json` next to the .PrjPcb or in the Git
    repository root above it (the repository's first; a pack whose id was loaded replaces
    it), plus `path=`. A pack has an id (not `plugins`), title, version, optional rails
    (net voltages its rules assume) and rules with an id unique across packs, a title, a
    severity (error, warning, info), a message template and `on_unknown`. Every file is
    validated like a checklist override (keys, kinds, classes, regular expressions,
    assertions per object, message fields, tools and the arguments they take, conditions,
    duplicate ids, JSON line and column); a problem is `invalid_check_pack` with the file
    and the place.
  - Select-and-assert rules select parts, nets or pins (designator, prefix, kind, fitted in
    the active variant, BOM, parameters, text fields, PCB side, case size, net, net voltage;
    net name, class, voltage, parts on it, pin count, one net per line; pin name, number,
    electrical type, role, part and net; `not` everywhere) and assert has_parameter, has,
    parameter_matches, matches, count (parts of a kind on the net, its line or within N
    series parts), connected, nearest (PCB, pad edge to pad edge), rating_ratio (rated
    over applied voltage, as check_derating reads both), side, case_size or none;
    `other_end` tests the far side of a part found on a net (a capacitor to ground, a
    pull-up to a supply). Selection is three-valued: an object that only a rail of
    unknown voltage or a side without a PcbDoc could select, at any depth of the filter
    (a pin's or part's net, `has_part`, `not`, an assertion's parts filter), is counted
    as not evaluated, with the rails named, never as selected or left out.
  - Tool rules run any checklist evidence tool in-process through review_checklist's runner
    (cached per tool and arguments, the pages their conditions read) and map its rows with
    the checklist conditions: `fail` rows become findings with the tool's designators and
    nets, `pass` must hold, `na` and `na_errors` make the rule not applicable. A count
    missing from a tool's summary of counts (a clean board lists none) is 0 and named in
    the row; any other field the tool does not give leaves the rule could_not_evaluate,
    unless other conditions read findings (they count).
  - Python plugins (`check_packs/plugins/*.py` in the project folder only; a `check_packs`
    or `plugins` folder linked to another place gives none) define `check(api)`; they run
    only with `allow_plugins=true` and `ALTIUM_COPILOT_ALLOW_PLUGINS=1` in the server's
    environment, compiled from source in a fresh module (no bytecode is written), with
    standard output captured; an exception, `sys.exit`, a missing `check` or a bad result
    is reported in the plugin's rule row. They have no time limit, and output written past
    `sys.stdout` is not captured (README).
  - Results: ERC-style findings as check_bom's (check is the rule id, pack, severity,
    designators, nets, pins, location with sheet and PCB side and position in mm, issue),
    errors first and paginated with the size shrink loop; every rule keeps a row on every
    page (status pass, fail, not_applicable, could_not_evaluate with the reason, objects
    checked and not evaluated), shortened to compact and then minimal rows when a page
    must shrink, so a checklist always reads its rule's status; a verdict (fail, warn,
    incomplete when a rule could not be evaluated or left objects not evaluated, pass) and
    counts by pack and severity. The run is reused for the next pages for 5 minutes while
    the project folder, the pack files and the call's choices are unchanged.
  - The design API (`DesignAPI`, version 1): frozen parts (kind from check_interfaces'
    reading, fitted, in BOM, parameters with the variant's variations, value, comment,
    description, footprint, manufacturer and MPN as get_bom reads them, case size, pins
    with name, electrical type, role and net, side and position), nets (classes ground,
    rail, supply, signal, i2c, open_drain, diff_pair (signals only), connector,
    connector_power, off_board, external, single_pin from check_decoupling,
    check_pull_resistors and check_interfaces; voltage with source and confidence from
    check_derating; the line, joined by series resistors under 1 kohm as
    check_pull_resistors joins a signal; the connectors it leaves the board through),
    rated voltage and voltage across, walks through series parts, pads, distances and the
    nearest of many parts (pad bounding boxes skip the parts that cannot be nearer: a
    board-wide nearest search on the MiniPC takes 7 s instead of 66 s). Helpers made
    public where they were: `review_checklist.read_json`, `clip`, `row_text` (and
    `evaluate` takes `absent_counts`); `interfaces.categorize`, `part_text`,
    `exposed_lines`; `pull_resistors.classify_net`, `merged_signals`, `open_drain_nets`,
    `pull_supply`.
  - Review checklists take `custom` items naming a pack rule (`"rule": "TB-HV-01"`), from
    a team's overrides too: the rule's pass is Yes, fail No (with its findings' anchors),
    not applicable N/A; a rule that could not be evaluated, left objects not evaluated, is
    not defined or is a plugin needs judgment (plugins are not run from a checklist). A run
    is reused only while the pack files are unchanged. `schematic_review` calls
    `run_custom_checks` when the project has packs.
  - `examples/check_packs.json`: the roadmap's six examples (a TVS on every off-board
    signal, decoupling within 5 mm of every IC supply pin, capacitors above 24 V rated 2x,
    one pull-up per I2C line, no 0201 on the bottom side, manufacturer and MPN on every
    part) and a tool rule (check_interfaces' no_esd on user-facing connectors as an error).
    Calibration: on Altium's 11 public AD25 examples every rule ran; TB-HV-01 could not be
    evaluated on the 4 boards whose rails carry no voltage in their names (VBAT, VDD_DDR,
    VCCINT ..., named in the reason) and selected nothing on the other 7; the MiniPC gave
    41 TB-ESD-01 lines on its user-facing connectors (the 41 lines check_interfaces'
    no_esd flags on them), 48 TB-DEC-01 supply pins farther than 5 mm from a capacitor to
    ground on their net or with none, 7 TB-I2C-01 lines without exactly one pull-up (the
    lines check_pull_resistors reports, 9 of 9 on the examples) and 223 bottom-side 0201s;
    at most 6 s for a first call and 49 k characters on a first page. On 63 private boards
    (after the load): verdicts 15 fail, 33 warn, 4 incomplete, 11 pass; TB-HV-01 could not
    be evaluated on 42 boards (410 capacitors on rails of unknown voltage) and selected
    nothing on 21; findings TB-ESD-01 78 on 15 boards (all 78 are check_interfaces'
    external no_esd lines, and it has no other), TB-DEC-01 54 on 32, TB-I2C-01 52 on 25
    (the same lines as check_pull_resistors), TB-ASM-01 2 on 1, TB-BOM-01 400 on 22,
    TB-TOOL-01 18 errors on 15; no exception, median 0.1 s and at most 0.8 s per board,
    first pages at most 31 k characters. Tuned on them: off_board and external take only
    the connector lines that reach an IC, a transistor or another active part (as no_esd;
    not lines into a transformer or another connector, power, LED or centre-tap pins: a new
    connector_power class), and a finding names the connectors the line leaves through;
    I2C lines follow check_pull_resistors (one finding per line, `one_per_line` and
    `on_line`; a supply a GPIO switches counts behind a series resistor too); a voltage
    range on a rail of unknown voltage is not evaluated instead of silently out of range;
    TB-BOM-01 asks heat sinks and screws placed as standard parts for their MPN, as
    check_bom does.
- `run_review_checklist`: design-review checklists run against the saved project, offline
  (roadmap #46 phase 1, `services/review_checklist.py`). Works on a netlist-free load (the
  schematic tools' items then say they could not run) and without a PcbDoc (board items
  are N/A).
  - Four bundled checklists in `server/checklists/*.json`, written in this project's own
    words on the topics of Altium's schematic review checklist and its PCB design review
    checklists (cited in each file): `schematic` (power; signals and interfaces;
    components and ratings; connectors and protection; testability and production; style
    and documentation, 41 items), `pcb` (placement; power and thermal; high speed and
    constraints; manufacturing; testability, 33), `release` (validation, generated outputs,
    documentation, configuration, 21) and `bom` (part data, sourcing, variants, 12). Each
    item has an id, its text, a kind (auto: a deterministic tool decides; assisted: a tool
    gives evidence and the datasheet or the reviewer decides; judgment: a person), a scope,
    an optional hint and source, and for tool items its evidence: the tool and arguments
    and condition lists (`na`, `no`, `judge`, `yes`) over the tool's findings and summary
    fields. No checklist named lists them with sections and item counts.
  - The tools run in-process through the MCP tool functions themselves, with the item's
    arguments and the active variant, once per tool and arguments per call; pages are read
    while they bring new rows of a list an item looks at (at most 25). Each item row gives
    its status (yes, no, n/a, needs_judgment; an assisted item's lean yes or no), an
    evidence line (counts, finding ids and the first finding's own words), the tool and
    arguments, finding ids with counts, anchors (designators, nets, sheets, rule and
    variant names) and could_not_run (the tool's error code, such as netlist_required).
    A field an item reads that is not in its tool's result makes it needs judgment
    (`unresolved`), never Yes; a Yes quotes what was checked. A coverage table counts
    items by status per section.
  - Design state: SHA1 and save time of the .PrjPcb, sheets, PcbDocs and Draftsman
    drawings with a fingerprint over them, and when the project is in a Git work tree the
    commit and whether any of those files differs from it, wherever it is (a sheet outside
    the project folder too; an untracked or git-ignored file, or one outside the work
    tree, counts as changed). New `clone_status.head_state`, with the same local, hardened
    git calls as `get_clone_status`; `_uncommitted` takes literal, case-insensitive paths.
  - `decisions={"ID": "no: reason"}` records the review's own verdicts (the tool's status
    is kept as tool_status). `format='markdown'` returns the review record: header and
    design state, the design files with their SHA1s, the 'Checklist coverage' table,
    every item with status, evidence and anchors, the open items, a sign-off table and a
    machine-readable block (an HTML comment). `previous=` reads such a record back and
    reports the design-state change, newly failing, fixed and still-open items, other
    status changes (No to N/A is a change, not a fix) and the earlier review decisions on
    items that are open again. A record too large for one result even shortened is a
    `record_too_large` error asking for one record per section, never a record cut
    through its block. Nothing is written; a run is reused for 5 minutes while the design
    files and every file in the project folder (OutJobs, reports, outputs) are unchanged.
  - Team overrides: `review_checklists.json` or `review_checklists/*.json` next to the
    .PrjPcb or in the root of the Git repository above it replace a checklist, add
    checklists, sections and items, and disable items by id. Every file is validated
    (keys, ids, kinds, scopes, tools and the arguments they take, operators, values,
    duplicate ids across checklists ignoring case, JSON with line and column, size, links
    out of the folder); a problem is an `invalid_checklist` error naming the file and the
    place. A `where` field can be compared with another field of the row (`field`).
    `overrides=false` ignores them. Template: `examples/review_checklists.json`.
  - `schematic_review`, `constraints_review`, `pcb_manufacturing_review` and
    `release_review` call `run_review_checklist` with their checklist first and finish
    with its coverage table and record. The bundled checklists are packaged with
    `--add-data "server/checklists;checklists"` (release workflow and `pack.ps1`).
- `check_libraries` and `get_library_part`: the libraries behind the project's parts, read
  offline (roadmap #52, `services/libraries.py`, on new readers `parsers/sch_lib.py`,
  `parsers/pcb_lib.py` and `parsers/int_lib.py`). Both work on a netlist-free load (the PCB's
  parts are then checked) and without a PcbDoc (the schematic's).
  - Readers: SchLib (FileHeader component list with aliases; per component its pins from the
    binary pin records and the older text ones, with designator, name, electrical type, part,
    display mode, hidden, location and length; parameters, description, default designator,
    Component Type and model links with ModelDataFile0 and UseComponentLibrary), PcbLib
    (Library/Data names and mechanical layer kinds, per footprint its primitives split by type
    and read by the PcbDoc decoders: pads, bare holes, tracks, arcs, vias, fills, regions, 3D
    bodies with model name, id, embedding and V7 layer, texts; Parameters HEIGHT and
    DESCRIPTION; courtyard, assembly, 3D-body and component-centre outlines by layer kind,
    mechanical layers above 16 named by the V7 layer id tracks, arcs and fills carry at 41,
    52 and 42 and by a region's V7_LAYER, where the v6 byte says Mechanical 16) and IntLib (each embedded library is one byte, 2, then a zlib stream holding the library's own
    OLE file; read in memory). A storage is found by Altium's storage name ('/' as '_', cut
    to 31 characters), else by reading every storage. `sch_doc.read_records` takes a hook for
    binary records; `read_records`, `coord_mil`, `pin_end` and `prj_pcb.split_sections` are
    public now. Schematic parts keep their library, LibraryPath, Workspace and DbLib table
    and their current footprint's library; PCB components their footprint and component
    library, SOURCELIBREFERENCE, Workspace flag and HEIGHT; 3D bodies their model name;
    tracks, arcs, fills and regions their mechanical layer number (`mechanical`). An older
    3D body record without MODEL.EXTRUDED.MAXZ whose STANDOFFHEIGHT exceeds its
    OVERALLHEIGHT is read with the two swapped, as Altium places it (the SpiritLevel
    example's library bodies against their placed copies; no board body read has it).
  - Resolution, as Altium does it, in the order of the Available File-based Libraries tabs:
    the project's [DocumentN] libraries, the installed libraries (Altium's AD*\Library and
    extension library folders plus the existing files of IntLib_Installed.ini), then its
    [SearchPathN] folders (pattern and subfolders). A
    symbol is looked up only in the library the part names, by Design Item ID then library
    reference (a same-name symbol elsewhere is a hint); a footprint in its named library
    (ModelDataFile0, SOURCEFOOTPRINTLIBRARY, the part's IntLib) first, then in any, as a
    footprint link set to Any. Exact names first, then differing only in case. A Workspace
    footprint is read from its cached PcbLib when the PcbDoc names one that exists. Library
    name indexes are cached by path, size and time.
  - `check_libraries` checks (default all but where_used): `resolution` (parts placed from a
    library the project no longer has, with the libraries holding a same-name symbol; names
    not found; footprints found outside their named library; names differing in case;
    Workspace and DbLib parts, not readable offline: one finding per side, with the
    libraries they name counted inside); `footprint_drift` (each placed
    footprint against its library footprint, as Altium's Footprint Comparison Report: pad
    and bare-hole counts, pads missing or extra by designator, every pad at one offset from
    the reference point, the footprint turned or mirrored, pads moved with their delta, pad
    size, angle, shape, corner radius, hole, slot, plating and layer; Height, 3D bodies and
    the other primitives' counts per layer as info; parts with the same difference, its
    numbers within the tolerance, grouped; a footprint compared with a same-name one from
    another library is labelled; mask and paste expansions, hole offsets, slot angles,
    texts and the shapes of other primitives are not compared). Placed pads
    are taken back to the footprint's own frame: Altium mirrors a bottom-side footprint
    about its X axis, then turns it by the component's rotation (pad rotation on the bottom
    is the component's minus the pad's); confirmed on every bottom-side part of the examples
    and the private set. `pads_cover_pins` (every pin on a net has a copper pad in the placed
    and in the library footprint: errors, as Update PCB's Unknown Pin; a bare hole, a pad
    without copper, does not count and is named in `on_bare_holes`; board pads carrying a
    net without a pin; `footprint=` with `part=` checks a proposed swap: `swap` says per
    part whether every used pin keeps a copper pad, the pins lost and the new pads without
    pins; parts whose netlist pins are not their symbol's are skipped). `symbol_rules`
    (Altium's SchLib Component Rule Check: duplicate pins (same name: info), pins without a
    designator (info on a Graphical symbol) or a name, gaps in the pin numbering, no
    description, default designator or footprint (pinless and simulation-only symbols:
    info), IC pins all Passive; each symbol against its footprint: pins
    without pads, pads without pins, a pad four times the median without a pin flagged as an
    exposed or thermal pad with no net). `footprint_rules` (Altium's PcbLib Component Rule
    Check: pads without a designator or sharing one, duplicate primitives, mirrored,
    reference point outside the pads, shorted copper (pads of different designators touching
    or joined by tracks, arcs, fills or regions: a warning on the library item, skipped for
    net ties, naming the parts net_ties reports) and unconnected copper, no description, no
    courtyard on a library declaring courtyard layers). `net_ties` (net-tie parts: copper
    joins the pads, the schematic, PCB and library types agree, the tie joins different nets
    on the board; a part that is not a net tie whose library footprint joins pads the board
    has on different nets, one finding per footprint: an error when the placed copy joins
    them too, as DRC's short, a warning when only the library's does, as Update From PCB
    Libraries would).
    `height_3d` (per part: a 3D model, an extruded body, only the fallback Height, neither).
    `where_used` (`where_used=` a symbol or footprint name: the registered projects and the
    projects under `folders=` that place it, from their SchDocs and the PcbDocs' Components6,
    cached by file time; with `library=`, only parts placed from that library). `part=`
    narrows every check; `library=` runs the rule checks, net ties and 3D on every item of
    one library. Findings (errors first; items many share grouped per library, warnings
    such as a library of symbols without footprints too) are paged with a size shrink loop;
    lengths in mm or mil, finding messages without numbers.
  - `get_library_part`: a placed part (`designator=`) or a library item (`lib_reference=`,
    `footprint=`, `library=`: only that file is searched, a same-name item elsewhere is a
    hint, and a library that cannot hold the item is an error): where the symbol and
    footprint were found or why not, the symbol's pins with electrical types, parts,
    footprint links, other models and parameters, the footprint's pads, bare holes,
    outlines, courtyard, Height and 3D bodies (with a summary), and the part's rule checks,
    pins against pads, used pins without a library copper pad, footprint drift and 3D
    category. Pins, pads and bodies page together; a part too large even at the smallest
    page comes back without them.
  - Not available offline (stated in the results): Workspace (Altium 365) parts (a
    footprint is read only from a cached PcbLib the PcbDoc names), DbLib and SVNDbLib parts'
    database links (roadmap #53), LibPkg packages, LibCrossRef.Txt.
  - Calibrated on Altium's examples and a private set of 63 projects (aggregates only): every
    placed footprint of the examples compared (120) matches its library footprint, and 97%
    of the private set's (3,475 of 3,566); the rest are real edits (single-pad parts whose
    pad is offset from the reference point, footprints compared with a same-name footprint
    from another library, pads renamed or removed, Heights changed). The checks take about
    3.2 s over the whole private set (its netlist compiles and PcbDoc reads aside) and
    about 40 ms per library in library mode.
- `verify_outputs`: the files Altium generated cross-checked against the saved design,
  offline and read-only (roadmap #39, `services/verify_outputs.py`, on stage A's readers in
  `parsers/fab_outputs.py`). Works on a netlist-free load (the BOM is then compared with the
  PCB's parts: designators, Comment and footprint only).
  - Files: `path=` a folder, a release `.zip` (read in memory, one nested zip deep, nothing
    extracted) or one file; without it, the folders the project's OutJobs generate into
    (`output_jobs.output_locations`: each output's folder-structure container, its data
    sources and the project's `.BomDoc`s) and the Output Path. Below a container folder named
    by a date/time expression (and below a container's timestamp folders) each first-level
    folder is a generation: only the newest generation of each kind is checked, ordered by the
    date and time in the folder names (a copy or checkout resets file times), else by file
    time; older ones are counted and the generation checked is named. Files are classified
    by content (`detect_output_kind`; a workbook counts as a BOM once a BOM header row is
    found in it, other workbooks are listed as not a BOM), identical files and pick-and-place
    twins (same rows) are checked once, and each file is matched to the OutJob output whose
    folder holds it (its variant, data source and settings; Test Points and Test Points For
    Assembly outputs make the IPC-D-356A netlist). Zips in the OutJob folders are listed, not
    opened (pass `path=`).
  - `pick_place`: every fitted part of the file's variant (its preamble's Variant line;
    `variant=` overrides; with a variant the project lacks, a part some variant does not fit
    may be absent) present once, Not Fitted parts absent, side, rotation (mod 360, the
    component's own on both sides; a file writing whole degrees matches within 0.5),
    Center-X/Y against check_dfm's centroid (the pad-centre box midpoint relative to the board
    origin; a part without pads at the absolute origin) within `position_tolerance`
    (default 0.01 mm), Ref-X/Y and Pad-X/Y when present (a bottom-side Y written negated is
    Altium's Y-flip option), footprint (Altium writes a space in a footprint name as '_');
    Comment and Description against the variant's BOM part are info. Standard (No BOM),
    Graphical and Net Tie (No BOM) parts may be listed or not. A file whose rows mostly
    differ is called out once (`layout_differs`: another revision of the board).
  - `bom`: the fitted lines' designators against the variant's BOM parts as get_bom builds
    them, lines a 'Fitted'-style column or a zero quantity marks Not Fitted against the
    variant's Not Fitted parts, quantity against the designator count, duplicates, and the
    Comment, Value, Footprint and Manufacturer Part Number columns (warnings) and
    Description and Manufacturer (info: supplier data spells them its own way) against the
    part: a column named after a parameter shows that parameter ('Manufacturer Part Number 1',
    also in the templates' 'Manufacture' spelling), its special strings ('=Value') resolved as
    get_bom resolves them, else get_bom's value. A BOM generated from an ActiveBOM document
    (its OutJob output's data source, or every BOM output of the project's for a file given by
    path) takes part numbers from the BomDoc's part choices, which are not read: its part
    number differences are info (`part_choices_not_read`). The variant: the file's title
    block, its OutJob output (unless the fitted lines are exactly another variant's parts:
    then that variant, with `variant_differs_from_outjob` as info), a variant named in its
    path, else the variant whose parts fit it best (`variant_from`). `.xlsx` workbooks are
    read by a stdlib reader (`fab_outputs.xlsx_sheets`, `parse_bom_xlsx`); `.xls` is reported
    as not read.
  - `netlist`: IPC-D-356A (317/327 records; vias and 367 tooling holes left out, N/C as
    unconnected) and IPC-2581 LogicalNets against the PcbDoc's pad nets pin by pin, with the
    format's 6/4/14-character cut of designators, pins and net names: pins missing, extra or
    connected on one side only, nets split or merged (warnings, listing the pins on the
    smaller side), renamed (info, with the net's first pins), each pin with designator, pin
    and XY; a file mostly for another board is called out once. IPC-2581 components against
    the placement (side, rotation) and its BOM populate flags against the variant (the
    argument's, the OutJob output's, else the variant whose Not Fitted parts fit the
    unpopulated ones best).
  - `stackup`: IPC-2581's stackup against the PcbDoc's layer stack (copper count, names,
    order and thickness, and the dielectrics between the outer copper layers, so solder mask
    is left out however the file labels it: thickness, Dk and material, within
    `thickness_tolerance`, default 10 %; layers pair up in order when the counts agree, else
    by name only), and the layers the `.LDP` (Gerber extensions: gtl, gN, gpN, gbl, listed in
    layer-ID order, so their order is not judged) and `.DRR` layer pairs name.
  - `drill`: per drill set: a `.DRR` report's layer pair with the drill files it names (in
    its own folder, else elsewhere in its generation), an `.LDP` pair's files no report
    names, else a folder's other drill files taken as the through holes; identical sets are
    compared once. A set's Excellon tools against the board's pad, via and bare holes of the
    pair (its span the outermost layers named; stacked coincident holes drilled once): size
    within half the tool size's written precision plus `drill_tolerance` (default 0.01 mil),
    plating from the ;TYPE sections, hole and slot counts per tool, sizes no tool drills,
    tools no hole needs, and hole positions (at the origin the OutJob sets, else whichever
    fits; a file whose hits mostly miss is called out once); the `.DRR` report against the
    drill files it names by tool number, or against the board when they are not there. A
    report or layer-pairs file naming another PcbDoc is called out (`other_board`: a warning
    for a board the project does not list, info for its other PcbDocs) and its files are
    not compared.
  - `staleness`: an output file older than the design documents it comes from (its OutJob
    output's data sources as get_output_jobs reads them, else the PcbDoc, and for a BOM the
    sheets and `.BomDoc`s; for pick and place and BOMs also the `.PrjPcb` when the project
    has variants), by file times, info. An OutJob output with no file found, and no output
    found at all, are info.
  - Paging with offset, a shrink loop to the result size, `units`, the model kept per
    arguments, files and OutJob outputs for paging; verdict `consistent`, `mismatches` or
    `no_outputs`. Calibrated on 63 real projects and 11 public examples: 147
    pick-and-place, 73 BOM and 250 NC drill files found in the OutJob folders (1,083
    older-generation files counted, not checked). There, every drill set whose holes sit on
    the board's (46) matches it with no finding, and the mismatches left trace to outputs
    generated from other board revisions and to BOM part-number columns filled from part
    choices, not to the readers. Runtime median 0.3 s per project (1.0 s reading a whole
    project tree with its release zips).
- `check_hv_clearance`: voltage-aware clearance and creepage screen (roadmap #32,
  `services/hv_clearance.py`), read-only and offline from the compiled netlist, the active
  variant and the PcbDoc (a PcbDoc is required: `no_pcb_document` without one). A
  screening tool, labelled so in every result: the product's spacing and safety
  standards decide, and IPC-2221B and IPC-9592B are functional minimums, not safety
  isolation.
  - Net voltages: check_derating's resolution (rails= wins, grounds 0 V, names, the power
    tree with vref=, divider midpoints), each with its source and confidence, plus hv=
    for working voltages that are not DC rails ('L=230AC,N=0,HV_BUS=400'; hv= wins over
    rails=; names before a value share it). AC is given as RMS and judged at its peak
    (x sqrt 2); '325pk' gives a peak, '230AC@120' a phase, so two phases 120 degrees
    apart see their line-to-line voltage (AC nets are otherwise in phase). A converter's
    switch node swings between 0 V and the voltage it peaks at, the power tree's or the
    value given in rails= or hv= (half of it plus a square wave of half, whose peak adds
    to any other AC); one of unknown voltage is listed to ask about. The voltage between
    two nets is their DC difference plus the peak of their AC difference when both are
    known. groups= ('PRI:L,N,HV_*,PGND;SEC:GND,3V3', wildcards; undeclared PCB nets form
    the board's group) declares isolated groups with their own reference: pairs across
    groups take working_voltage= ('250AC' or '400'); without it they are not judged (a
    note says so, and they are listed among unknown_voltage_pairs near a known
    high-voltage net).
  - Pairs: every pair of nets whose voltage difference (peak) is above threshold= (default
    15 V). Its copper is searched from the side more than half the threshold from its
    reference (across groups with a working voltage, from every group but the largest),
    each pair within twice its largest requirement or user figure (at most 5 mm more; a
    figure given for the other net counts), on every signal layer, through check_dfm's
    copper model (net= searches only for the pairs of those nets): tracks, arcs (measured,
    and located, on the arc, not the chord), pads with their copper on the layer, vias'
    pads on the layer (the barrel
    where the pad is removed), hatched pours' tracks, poured regions, free regions and
    fills; shelved and never-poured pours hold no copper (a note counts them), a pour's
    outline rows are not copper, internal plane layers are not measured, no-net copper
    and a net tie's own copper are counted, not judged, and copper wholly inside another
    net's region or fill touches. Per pair and layer: the smallest distance, its location
    (origin-relative) and objects.
  - Requirement per pair and layer from the peak voltage: IPC-2221B Table 6-1 (B1 on
    internal layers, B2 on external ones, B3 above 3050 m with altitude=, B4 with
    coating=true; with standard='ipc2221_assembly' A6/A7 where a component pad is one
    side and A5 for coated conductors, the larger of A6 and B3 for an uncoated lead above
    3050 m; above 500 V the per-volt value times the volts above 500 is added to the
    301-500 V value; a voltage between two rows takes the higher row), or
    standard='ipc9592' (0.13 mm below 15 V, 0.25 mm to 30 V, 0.1 + 0.01 x V to 100 V,
    0.6 + 0.005 x V above, any layer). min_distance= replaces the standard ('2.5' every
    pair, 'HV_BUS=4' a net's pairs, 'L&N=3' one pair, in units; for the pairs judged and
    for the requirement listed beside an unknown pair): IEC 62368-1 and
    IEC 60664-1 clearances and creepages depend on overvoltage category, pollution
    degree, material group and insulation grade and are not encoded. The board's own
    Clearance rule for each pair is resolved per net by priority (when higher rules apply
    only to some objects, every candidate with its clearance; its object matrix's copper
    cells counted, not copper to hole).
  - Table sources, cross-checked cell by cell (the table is only an image in Altium's
    article): Altair PollEx help 'IPC Material' and EMA Design Automation's 'PCB
    Clearance and Creepage Distance Table' give the whole table, identical in every cell;
    Sierra Circuits' reproduction differs in two cells (B3 151-170 V 6.4, B1 above 500 V
    0.025 mm/V) and philipmcgaw.com's in one (B2 251-300 V 2.5) where three sources
    agree, and the latter multiplies the per-volt values by the whole voltage, which the
    worked examples contradict; the rule above 500 V from Altair's and Siemens EDA's
    example (B1 at 600 V: 0.50 mm), viox.com's calculator (every column's 500 V value and
    increment) and smpspowersupply.com (B1, B2, B4 to 5 kV). IPC-9592B from Altium's
    'IPC-2221 calculator for high voltage design' and smps.us / smpspowersupply.com,
    which agree. Cited in the module and in every result (sources).
  - Findings: spacing_below_standard (warning: nets, voltage with its basis and
    confidence, layer and layer type, objects and parts, distance, requirement, standard,
    column and band, location, the board's rule, and slots, holes and cutouts within
    1 mm of the straight path, in_path when they cross it), spacing_below_rule (info, one
    per board Clearance rule, or set of candidate rules, that allows less than some pairs
    need where their copper is close, listing those pairs), unknown_voltage_pairs (info:
    nets that look like high voltage with no known voltage, each with why: HV, mains,
    DC-link and line words in the name (L, N, PE, AC and LINE only as the whole name or
    before a mains line's word such as L_F, AC_IN or LINE_L, so TX_N, L_EN and AC_OK are
    not), 60 V or more or kilovolts as a word of the name (400V_BUS, not U100VCC), a
    connector's power input, a mains connector's pin, a varistor's or an X/Y or 230 V AC
    capacitor's pin, a converter's switch node; and unknown-voltage copper near a
    high-voltage net with the requirement it would need at 0 V) and creepage_not_computed
    (info: straight-line clearance only; creepage around slots and cutouts is longer and
    not credited). pairs lists every pair measured with its distance per layer, its
    closest point and the board's rule; the summary gives net voltages by source, the
    high-voltage nets, pairs above threshold, searched and measured, per layer type,
    findings by check and severity, the nets to ask about, and with groups= the pairs
    across them and whether they are judged.
  - The tool takes net=, layer= (a signal layer), checks=, hv=, rails=, groups=,
    working_voltage=, threshold=, standard=, coating=, altitude=, min_distance=, vref=
    and units= (mm default, or mil), pages findings and pairs together and shrinks the
    page to fit; the prepared result is kept on the PCB index (the last four argument
    sets) so later pages do not measure again. Listed among the netlist tools and in the
    server instructions.
  - Shared code made public in place: check_dfm's copper model as
    `dfm_geometry.copper_model` (check_dfm, its assembly checks and check_testpoints
    use it), with a net-focused pass `net_gaps` (the chosen nets' copper against every
    other net's, each pair within its own reach, pruned by the smallest gap found so
    far), `openings_near` (outline, cutouts and holes without copper along a path) and
    `GapRecord.ends` (each copper's nearest point, on the true arc); the net-tie test is
    shared by both passes. check_dfm's own results are unchanged.
  - Calibrated on the 11 public Altium example projects (5 with a PcbDoc) and 53 private
    boards (aggregates only). With names alone: the examples have no net above 15 V by
    name (79 nets with a known voltage, 8 high-voltage-looking nets to ask about); the
    private boards have 320 nets with a known voltage (129 grounds, 111 by name, 80 from
    the power tree), 42 nets above 7.5 V on 10 boards, 117 pairs above 15 V of which 11
    come within reach (all 16-30 V, 0.1-0.2 mm apart, none below B2), and 94 nets to ask
    about (118 connector power inputs, 15 mains-rated parts: 12 capacitors rated 230 V AC
    or more and 3 varistors, 9 switch nodes of unknown swing, 4 names). Answering those
    as a user might (48 V for power inputs, 230 V AC for mains parts and words): 154
    pairs measured on 408 pair-layers, 167 warnings on 110 pairs, all on outer layers and
    mostly 48 V copper 0.1-0.5 mm from another net (B2 asks 0.6 mm in the 31-50 V band;
    coating=true gives B4's 0.13 mm), and 130 pairs whose board rule allows less than
    they need, in 48 spacing_below_rule infos (one per rule). A stress run (the three
    non-ground nets with the most copper at 400 V) measured 122 pairs on 833 pair-layers
    on the examples and 477 pairs on 1,286 pair-layers on the private boards, without
    reaching the work limit. Every table lookup used (2,725 across the runs) was
    recomputed from an independent copy of the cross-checked table: none differ. On the
    public examples an independent brute-force geometry (true arcs, every pad shape,
    vias' pads and barrels, regions, fills and poured copper) recomputed 17,227 pair-layer
    minimums within reach over five scenarios (connector and rail voltages, every other
    net at 0 V, ground at 400 V, padless-via and arc nets at 400 V): none missing, none
    extra, every gap within 0.002 mil, and every gap's two end points on their copper,
    their distance the gap. Runtime: at most 0.4 s on the private boards; on MiniPC,
    the largest public board, 0.4 s with names alone, 3.0 s (1.8 s once the copper model
    is built) with two of its largest nets at 400 V and 2.2 s with the stress run's
    three; later pages and repeats 0.03 s. Default page median 3,500 characters with
    names alone; large results shrink to fit.
  - Not covered: true surface creepage (2D path-finding around slots and cutouts: they
    are listed, not credited), IEC 62368-1 / IEC 60664-1 tables (min_distance takes
    their figures), spacing between layers through the laminate (distance through
    insulation), internal plane layers and their anti-pads, component bodies and leads
    above the board, solder-mask coverage per object (coating= is one answer for every
    outer layer), three-phase systems without phases given, transients and
    overvoltage categories, and voltages of nets the user does not give (names are a
    guess: unknown pairs are listed, never judged).
- `check_thermal`: thermal estimate and thermal-via audit (roadmap #21,
  `services/thermal.py`), read-only and offline from the compiled netlist, the active
  variant and the PcbDoc. Everything is labelled an estimate: thetaJA, Tj max, loads and
  efficiencies come from the datasheets or the user, and the datasheet and a thermal
  simulation decide. After Altium's IC thermal management article (thermal vias close to
  the heat source, heating from neighbouring parts, thetaJA x P as the rise) and 'Beyond
  the PCB review' (a thermal estimate as a final verification step).
  - Hot parts, each with its basis: get_power_tree's converters (linear regulators and
    LDOs, switchers and modules, chargers, load switches, eFuses, hot-swap and ideal-diode
    controllers), the FETs a converter's gate pins drive (switch FETs, or a pass
    controller's pass FET) and the diodes on its switch node, transistors and diodes in
    series with a rail, current-sense shunts, resistors whose computed power
    (check_derating's V^2/R, or I^2 x R at an LED current get_circuit_calculations
    computes) reaches 0.25 W or half their rating, LEDs at 0.2 W or more, processors and
    FPGAs (get_firmware_pin_map's candidates), Ethernet PHYs (check_logic_levels'
    classes), LED drivers, motor, bridge and power-stage drivers and power amplifiers by
    their words, parts named in power= (and listed, as no heat source, when named in
    part=), and with a PcbDoc any part with an exposed pad or thermal tab, placed parts
    the compiled netlist lacks included.
  - Dissipation where the inputs allow: linear P = (Vin - Vout) x I_out + Vin x Iq,
    switcher P = Vout x I_out x (1/eta - 1) (the converter's total loss), per output for a
    multi-output part (the outputs with a load counted, the rest listed), shunt I^2 x R
    (never V^2/R: nominal net voltages cannot resolve the millivolts across it), diode
    Vf x I with its stated Vf (a forward-voltage parameter, 'Vf 0.55V' in its text, or
    the one voltage under 1.6 V in a rectifier's text), else a typical one (0.45 V
    Schottky, 0.8 V silicon, labelled), resistor V^2/R, LED Vf x I; a FET, a switch, a
    processor or a driver needs its figure in power=. Voltages are check_derating's net
    voltages (rails=, names, the power tree, vref=); an iq= whose Vin is unknown is asked
    for, not dropped. Loads are never guessed: load= takes amps per designator or output
    rail, read without float noise ('700mA' is 0.7). With thetaJA (theta=) the junction
    temperature Tj = Ta + thetaJA x P at ta (default 25 C) and at a worst-case ta_max,
    against tj_max (per designator or one for all, default 125 C). inputs_needed lists the
    designators waiting for each argument.
  - Thermal pads on the PcbDoc: a component's largest SMD pad (with same-named pads of at
    least a quarter of its area: a split pad; then a second pad of another name: dual
    pads), or a fill or region its footprint draws on its pads' layer and on one of its
    nets (a PowerPAK's drain paddle, with the pads it carries), at 2.5 times the median
    area of its other pads or more and 0.5 mm^2 or more, in the box of the other pads'
    centres (an exposed pad) or at one edge (a DPAK, D2PAK or SOT-223 tab), of ICs,
    transistors (T and TR ones too), diodes, converters and unknown parts; connectors,
    switches, crystals, inductors, transformers, capacitors, resistors and other
    mechanical parts are left out, and so is a no-net tab of a part not known to dissipate
    (a holder's anchor). More than four such pads make a pad array (an LGA module, a power
    stage): its large pads on ground (else on the largest one's net) are audited as one
    pad, and pad_array gives how many large pads there are and how many were audited.
    Vias count when their centre lies inside the pad's copper (rotation and side as
    placed) and their span reaches the pad's layer (a blind via under a bottom pad does
    not), with the footprint's own plated holes in it; each pad's count, drills, unplated
    holes, vias on another net, same-net vias within 1 mm of its edge, whether it meets a
    pour of its net, and the layers where its vias meet its net's copper as
    check_connectivity joins it (planes, poured regions with their relief spokes, fills,
    and hatched pours' outlines). A shelved or never-poured pour holds no copper: vias
    landing in one are listed under reaches_pours_without_copper, not as reached and not
    as unconnected, and a board note counts those pours.
  - Findings: tj_over_max (warning), tj_near_max (above 80% of tj_max: info),
    thermal_pad_no_vias (warning for a part known to dissipate, info otherwise or when
    four or more vias of its net sit beside it or a tab sits in its pour),
    thermal_vias_wrong_net (warning), thermal_pad_few_vias (fewer than one via per
    1.5 mm^2, or 4 on pads over 4 mm^2, labelled a common guideline: info),
    thermal_pad_unconnected (a pad with no net, or vias reaching no plane or pour of its
    net on another layer: info), hot_neighbours (two hot parts, one at least known to
    dissipate and neither only named in part=, within max_distance, default 5 mm, edge to
    edge between their pad boxes in plan view, same or opposite side; the parts of one
    power stage (a converter, power switch or driver with the FETs, diodes and shunts on
    its output, switch node or other nets, a part feeding its input, FETs in series or
    parallel) are not paired, but a part drawing from the same supply as a converter is:
    info) and sensitive_near_hot (an aluminium electrolytic or polymer capacitor,
    supercapacitor, crystal or oscillator, voltage reference, temperature sensor or
    thermistor, or battery near a part known to dissipate; a crystal, reference or sensor
    wired to that part by a net other than a supply is beside it by design; a
    temperature sensor's finding says it may be there to monitor the part: info). Without
    a PcbDoc the PCB checks are listed in not_checked and the rest runs.
  - The tool takes part=, checks=, load=, power=, theta=, ta=, ta_max=, tj_max=, eta=,
    iq=, rails=, vref=, max_distance= and units= (mm default, or mil), pages hot parts
    (hottest first) and findings (warnings first) together and shrinks the page to fit.
    Listed among the netlist tools and in the server instructions.
  - Shared readers made public in place for it: get_circuit_calculations' `Board`,
    `LED_DRIVER_TEXT` and `VF_TEXT` and its LED rows as `led_calculations`,
    check_derating's `across_voltage` and `is_shunt`, get_power_tree's `gate_fets` and
    check_logic_levels' `POWER_DRIVER_TEXT`. The other tools' outputs are unchanged (103
    calls of get_power_tree, check_derating, check_logic_levels, get_circuit_calculations,
    get_firmware_pin_map, check_decoupling, check_pull_resistors, check_vias, check_dfm and
    check_testpoints on the public examples, compared with the base branch: identical).
  - Calibrated on the 11 public Altium example projects (5 with a PcbDoc): 50 hot parts
    (43 known to dissipate), 37 thermal pads (35 exposed pads, 2 TO-263 tabs; 11 of them
    PowerPAK drain paddles drawn as footprint fills, with 4 to 15 vias each; the other 26
    checked by hand against pad and via coordinates, rotations 0 to 270 degrees, 5 on the
    bottom side: every via count matched); 4 no-via warnings (two VLDOs' DFN pads,
    two regulators' TO-263 tabs that also have no net), 3 few-via, 3 unconnected, 14
    neighbour pairs and 8 sensitive parts near heat. On 53 private boards (aggregates
    only): 217 hot parts (189 known to dissipate: 85 switchers, 18 linear regulators, 11
    power switches, 15 power-path transistors, 10 power-path and converter diodes, 36
    shunts, 14 LED drivers), 129 thermal pads (116 exposed pads, 13 tabs; 25 pad arrays
    of 8 large pads, their 4 ground pads audited as one; 1 footprint region; 13 with no
    via, 45 with 1-3, 71 with 4 or more; 106 reaching a plane or pour, 10 landing only in
    shelved or unpoured pours, no via on another net), 9 no-via warnings, 67 few-via and
    1 unconnected infos, 29 neighbour pairs and 46 sensitive parts near heat. Default page
    median 3,800 characters (the largest pages shrink to fit), runtime at most 1.3 s.
  - Not covered: thetaJC and heatsinks, copper-area spreading and airflow, the split of
    a converter's loss between its controller, FETs, diode and inductor, inductors and
    transformers as heat sources, switching losses, duty-cycle-weighted diode and FET
    currents, through-hole tabs on heatsinks, vias that touch a pad without their centre
    inside it, tracks as heat paths, component bodies (3D) for neighbour distance, a pad
    array's large pads on other nets (counted, not audited), and sibling load switches on
    one shared rail (still grouped as one power path).
- `get_circuit_calculations`: circuit calculators (roadmap #19,
  `services/circuit_calcs.py`), read-only and offline from the compiled netlist and the
  active variant. Each result carries its inputs (every value with where it came from),
  its formula, its result and a confidence; Vf, Vref, EN thresholds, GPIO current limits
  and ADC sample capacitors are datasheet values, so the results are calculations to
  check, not verdicts.
  - dividers: a midpoint with a resistor leg (one resistor, parallel resistors to one
    net, or a series chain through nodes holding only the chain and capacitors, a test
    point, jumper or header pin, noted, or high-impedance IC inputs) to ground and one to
    a rail, a net of known voltage or a driven signal (a push-pull output, a processor
    GPIO that is not an input: its high level is its I/O rail), or legs to two rails of
    known voltage. Each tap of a resistor stack (a window comparator's two inputs, an EN
    pin with a hysteresis tap) is a midpoint of its own, the rest of the stack its leg; an
    open-drain output on the bottom leg gives the ratio while it sinks too. A midpoint a
    push-pull output drives, one with a supply pin or a processor bus pin, a rail's source
    net, a series resistor into a pull-up with no ground leg, or a series resistor of up
    to 1 kohm from a driving pin into a pull-down (a termination; kept and labelled at a
    transistor's gate or base) is not a divider. Ratio Rbot/(Rtop+Rbot), the unloaded
    midpoint voltage (net voltages with source and confidence, as check_derating reads
    them), Rtop||Rbot, the divider current, a min/max ratio and midpoint voltage when
    every resistor's tolerance is read (a Tolerance parameter, '1%' in its text, or the
    tolerance letter of Yageo/Samsung RC, Stackpole RMC, Vishay CRCW, Panasonic ERJ or
    Bourns CR part numbers), legs a jumper, header or switch joins to an end (each with
    its ratio, not a connector consumer), and what it feeds, on the midpoint or one series
    resistor beyond: a converter's FB/ADJ (get_power_tree's feedback row when it reads the
    same divider, else the same formula; the Vref the top's voltage implies, from rails=
    or a joined rail too, with a three-terminal regulator's Vref across the top resistor,
    or the output a given vref sets), a converter's EN/UVLO (V_top at trip = V_th / ratio
    for vth, else at 1.2 V and 0.8 V as examples only), a supervisor or comparator input
    (with vth), a shunt reference such as a TL431 (the top a vref per designator sets, or
    the implied Vref), an ADC input, an op-amp input (the non-inverting gain when the top
    is its own output), an IC's reference pin, a transistor, a processor or IC pin, or a
    connector; other resistors on the midpoint are listed.
  - leds: each LED (check_derating's reading; two pins by their A/K names, else by the
    circuit: the side on ground is the cathode's, a rail beats a sinking pin, an
    open-drain pin only sinks; a multi-colour part by its common pin or channel numbers; a
    part with supply or data pins has its own driver and is only counted; LED+/LED- pin
    pairs on another part, such as a display's backlight, with no indicator rating)
    walked to its supply (a rail directly or through series or parallel resistors, a GPIO
    with its I/O rail, a high-side transistor to its emitter's or source's rail) and its
    sink (ground, a sinking pin, a low-side transistor; an LED driver or a converter's pin
    sets its own current): I = (V_supply - V_sink - Vf)/R - Vf/R_across (a resistor across
    the LED takes its share, and can hold it below Vf) with Vf from vf (per designator or
    colour), a stated forward-voltage parameter, a Vf in the text (VF=, or a voltage
    beside a current such as '30 mA, 2 V'), else typical for the colour read from
    parameters, text or wavelength (red 2.0 V, orange/amber/yellow 2.1 V, green 2.2 V,
    true green and blue/white 3.0 V, IR 1.3 V; a colour or Vf shared with the LEDs of the
    same part number), else the current at 2.0 V and 3.0 V; each resistor's power at that
    current against check_derating's rating (stated, else typical for its size). A side
    that is not followed says why (another diode, resistors to different nets, unnamed
    transistor pins).
  - optos: optocouplers by their words or part-number families (PC817, TLP, 4N35, H11,
    LTV, EL, CNY17, SFH6, HCPL, ACPL, FOD, VO, PS2, MOC, TCMT, 6N13x ...), their input
    diode by A/K pin names, else the standard 4-, 6- and 8-pin pinouts when the names it
    has agree (low confidence): I_F with Vf 1.2 V typical, less the current of a resistor
    across the input, a resistor or capacitor across the input, the output's pull-up (or
    emitter resistor) with its rail, the current at saturation (V/R) and the CTR it needs
    (I_out/I_F, from unrounded currents).
  - adc_inputs: processor pins get_firmware_pin_map reads as analog (not DAC or
    comparator pins), pins that list an ADC function and carry a divider or a capacitor,
    and the AIN/CH/IN pins of ADC parts: the series R (or the divider's source impedance,
    plus a series resistor between), the shunt C and any capacitor to another input, fc =
    1/(2 pi R C), and the divider feeding it.
  - rc_filters: other nodes with one series resistor from a driving pin (or a connector
    or switch) and a capacitor to ground into an IC or transistor input, with fc (a pull
    on the node joins R as its Thevenin resistance).
  - Findings: led_overcurrent (above the stated or a typical 20 mA rating: a warning when
    Vf is known, both voltages are read and I exceeds 30 mA or a rating a parameter
    states, else info; with an unknown colour, info only when even 3.0 V gives too much),
    diode_shunted_off (a resistor across an LED or optocoupler input holds it below Vf:
    warning, info when a typical Vf decides it by less than 15%),
    divider_output_above_input_rating (the midpoint, at its tolerance high bound, above
    the reached pin's rail + 0.3 V: warning, info when the rail is a guess; an EN pin
    against its converter's input), divider_output_below_ground (the low bound below
    -0.3 V on a pin powered from a positive rail), resistor_overpower (at the computed
    current: warning above the rating, info above the derated limit, derating_policy.json
    applies; with an unknown Vf, a warning when even 3.0 V overloads it, info when only
    2.0 V does), adc_no_filter and opto_no_input_shunt (info). Each finding stays with its
    kind of calculation when kinds narrows the result.
  - The tool takes part=, net=, kinds= (dividers, leds, optos, adc_inputs, rc_filters),
    rails=, vref=, vf= ('LED1=2.9,red=1.9') and vth= ('U3=1.2', or one for all), pages
    calculations and findings together and shrinks the page to fit (finer steps than other
    tools': its rows are long). voltages_needed lists the nets whose voltage the
    calculations lack, most calculations first. Listed among the netlist tools, in the
    server instructions and as step 10 of schematic_review.
  - Shared readers made public in place for it: check_derating's `resistor_limits`,
    `merged_params` and `is_led` (its parts keep the parameters, texts and part numbers
    they were read from), get_power_tree's `diode_side`, `parallel`, `fb_gain`,
    `fb_formula`, `floating_reference` and `vout_or_vref`, check_logic_levels'
    `OPTO_TEXT` and `AMPLIFIER_TEXT`, and get_firmware_pin_map's `PinMap` (which can reuse
    a built context) and `pin_rows` (for chosen pins). The other tools' outputs on the
    public examples are unchanged.
  - Calibrated on the 11 public Altium example projects: 36 dividers (13 feedback
    dividers, every one get_power_tree reads, 2 of them with jumper-selected legs; 11
    memory reference pins, 4 transistor inputs, 4 connector pins, 2 window-comparator taps
    of one stack, 1 EN/UVLO, 1 IC input; the midpoint voltage known for 15 and a
    tolerance range for 32), 31 LEDs (15 currents, 15 waiting for their supply's voltage
    in rails, 1 display backlight whose switch has unnamed pins), 6 RC filters, no ADC
    input, no finding; about 60 results checked by hand (feedback ratios and implied
    Vrefs, the EN trip examples, gate and reference dividers, LED currents and resistor
    power, RC cutoffs), and the stack taps and jumper legs recomputed. On 53 private
    boards (aggregates only): 51 dividers (34 feedback, every feedback pin with a resistor
    to ground and another resistor, including 3 whose chain passes a test point or header
    pin; 9 op-amp, 4 transistor, 2 EN, 2 supervisor), the midpoint voltage known for 29, a
    tolerance range for 47, all 24 implied Vrefs near a common one; 86 LED parts (36 with
    their own driver counted only; of 50 calculated, 17 currents, 15 set by a driver or
    converter, 10 waiting for a supply voltage, 3 unread), 10 optocouplers (all read, 6
    by the standard pinout; 10 opto_no_input_shunt infos), 1 ADC input, 1 RC filter, no
    warning. Default page median 2,000 characters (max 25,700; the largest example pages
    at 25 rows), runtime at most 0.55 s.
  - Not covered: a divider whose bottom is switched by a transistor (a P-FET gate
    divider), LED strings and series diodes on an LED's supply side, constant-current
    driver set resistors, triac-output optocouplers' output side, comparator thresholds
    from the other input, an ADC's own sample capacitor and source-impedance limit, loads
    on a midpoint, and a header that shorts resistors inside a chain (the ratio is given
    with nothing fitted).
- Full variant support (roadmap #9, `services/variants.py`): varied parameters, alternate
  parts and the new `compare_variants` tool, read-only and offline.
  - Parser (`parsers/prj_pcb.py`): each `[ProjectVariantN]` keeps its UniqueId,
    AllowFabrication and its variant parameters (the `[ParameterN_M]` sections after it);
    `VariantDefinition` gains `not_fitted()`, `varied_parameters()` (designator, the
    UniqueId of its variation line, parameter, value) and `alternate_parts()` (library
    reference, design item, library, footprint unless IsSameFootprint, database table,
    the other AltLibLink_* fields). Alternate parts are looked up in the project's
    `<project>.PrjPcbVariants` file, an OLE schematic library with one storage per
    alternate part (Altium: "The Alternate Parts are stored in the
    <ProjectName>.PrjPcbVariants file"): its description, parameters and footprints.
    `VariationN`, `ParamDesignatorN` and `ParamVariationN` lines are read with spaces
    around '=' too (`VariationN = ...`), like every other key: some saved projects write
    every key line that way (3 corpus projects), and one corpus variant has 3 Not Fitted
    lines written so, which were skipped before. The alternate's library reference is
    AlternatePart, then AltLibLink_DesignItemID, then SymbolReference, in the identity,
    the BOM and the library lookup alike. The old `[VariationN]`/`[CompVarN]` format
    stays DNP-only (no sample, no documentation).
  - `get_component`, `get_sheet_context` and `search_components` show the active
    variant's MPN, description and value; a part the variant varies gets
    `variant_changes` (each changed field with the base design's, the varied parameters
    that differ from the base design's with their base values, the alternate part with
    the base part's library reference and footprint), or a `variant <name>:` line on the
    sheet. Description and Footprint variations compare against the part's fields. A
    part the variant does not fit keeps its base values (variations stored on it are
    `stale_variant_entries`, as in get_bom). Variations bind to parts by UniqueId like
    the BOM (renamed parts keep their variations). Outputs are unchanged for parts a
    variant does not vary.
  - `get_bom` and `check_bom`: an alternate part found in the `.PrjPcbVariants` file
    takes its copy's parameters (Comment, MPN) and description, with the variant's
    parameter variations on top, and its library reference; without the file, as before,
    and when the file cannot be read the alternate-parts note says why (so do
    `list_variants`' `library_error` and compare_variants' notes).
  - `compare_variants(a, b)` (default: the active variant against `[No Variations]`):
    parts fitted in one only (with that side's variations), Value and Comment
    differences (an alternate's library copy brings its own, compared like the variant's
    variations), other parameters (Description and Footprint against the part's fields),
    alternate parts (base part or alternate, with `same_as_base_part` when an alternate
    re-selects the base part) and variant parameters, each with a's, b's and the base
    design's value, designators in natural order, paginated with the size shrink loop.
    The first page adds a per-variant documentation check (info only): the connected
    OutJob BOM, pick-and-place and assembly-drawing outputs set to each variant (a
    Draftsman drawing's own variant counts) and the roles with none, and the Draftsman
    drawings set to it or whose notes name it (the name next to the word "variant", as
    in "variant Lite" or "Lite variant", outside the title block, or a drawing parameter
    about the variant), whether a drawing prints the VariantName field; and stale
    variation lines. The unvalidated-format note shows only for a varied Value or an
    alternate other than the base part. Without a netlist it compares the
    designators stored in the .PrjPcb (`binding: stored_designators`).
  - `list_variants` adds, when a variant has them, `varied_components` and
    `varied_parameter_count` (of the parts it fits), `alternate_parts`,
    `allow_fabrication` and `parameters`.
    `project_tour`'s variants step calls `compare_variants` per variant (it called
    `get_bom(compare_variants=True)`).
  - `package_for_xfn`: a new `variant_variations` table (variant, refdes, kind
    parameter or alternate_part, parameter, value, base value, data) for the parts each
    variant fits; existing tables unchanged, no schema bump. `output_jobs.variant_outputs` lists the BOM,
    pick-and-place and assembly-drawing outputs with their variant.
  - Calibration: the 11 public AD25 examples have no variants and no `.PrjPcbVariants`
    file; every existing tool's output is identical to before on them. On 63 private
    projects (53 with variants, 63 variants): 315 Not Fitted, 8 fitted and 6
    alternate-part lines, 175 parameter variations (10 Value or Comment), 6 alternates
    all found in the `.PrjPcbVariants` file (52 such files; every corpus alternate
    re-selects the base part); 0 parse failures. compare_variants against the base
    design matches an independent count of the raw INI in all 63 variants (fitted 314,
    values 3, parameters 40, alternate parts 6, variant parameters 6): median 2,014
    characters (max 11,286), median 0.01 s (max 0.33 s). Per variant, 38 of 63 have a
    connected BOM, pick-and-place and assembly drawing set to them, 9 have none; 44 have
    a Draftsman drawing set to them. DNP sets and BOM fields are identical to before
    except in the one variant whose 3 spaced Not Fitted lines are now read.
  - Unvalidated (no sample): an alternate part with another library part or footprint,
    a varied Value parameter, variants on multi-channel designs.
- `check_logic_levels`: driver/receiver logic-level compatibility candidates (roadmap
  #18, `services/logic_levels.py`), read-only and offline from the compiled netlist. For
  every signal that links ICs powered from different rails, each IC pin's I/O supply
  with its basis and confidence: an FPGA's (or any part's) bank supply when the part has
  bank-numbered I/O supplies on two or more nets and the pin name gives the bank
  (Xilinx IO_L1P_T0_14 and DONE_0 by their last token, Intel LVDS3G_6p, DIFFIO2L_16p,
  CLK_3G_1p, PLL_3G_FB0 and RZQ_3G, BANK2/B14 tokens, or a token equal to a bank such as
  HPS; a pin whose bank is not in its name takes the part's I/O supply outside its banks,
  such as VCCPGM, at medium confidence, else the highest bank at low, never the part's
  core supply); a level translator's or isolator's side (A/B pins on VCCA/VCCB, 1/2 on
  VCC1/VCC2, VDD1/VDD2 or VREF1/VREF2, VL/VCC; DIR and OE on the A side; else the half of
  the numbered pins the side's supply sits in, medium); a clock generator's or buffer's
  outputs on its VDDO pins (per output when VDDO0, VDDO1 ... sit on different nets) and
  its inputs on the rest; else the part's only supply (high), its one I/O supply (VDDIO,
  VCCIO, VIO, VDD_IO, OVDD, VCCO, VDDQ, VL, VDRIVE, DRVDD ...: high), its one digital
  supply among analog, core, PLL or battery ones (medium; low when a lone numbered I/O
  supply such as an STM32's VDDIO2 sits on another net, since that powers one port only)
  or the highest of several (low). Supply pins are read by name (pin_role) and classed
  I/O, digital, analog, core, battery, power input (VIN, PVIN, VIN0/VIN1, VDDH, VM: used
  only when a part names no other supply, so a regulator module's PMBus pins take its
  VDD33 and a radio SoC's GPIOs its VDD) or skipped (VPP, VBUS, references, a regulator's
  own outputs and bias pins such as VDDB, and any supply pin whose net reaches only the
  part and capacitors). Voltages come from `derating.net_voltages`: `rails` given by the
  user (they win), net names, the power tree (`vref`) and divider midpoints, each with
  its source and confidence. Signals are nets joined through series resistors under
  1 kohm and 0 ohm links (`link_group`); a fitted series resistor of 1 kohm or more is
  crossed once, a pull-down behind it divides the level, and such lines are counted
  apart (`series_resistor_links`, `series_resistor_links_judged`) and shown under
  `series_crossings` in the `net` view. Pin directions come from the electrical type
  when it is not Passive (Output and HiZ drive, OpenCollector and OpenEmitter are
  open-drain, Input receives, IO both), else from the name for the part's class
  (processors, FPGAs, bridges, bus transceivers, Ethernet PHYs, SPI peripherals, logic
  gates, comparators, optocouplers, clock parts): a processor's TX, MOSI, SCK, CS drive
  and RX, MISO receive; a SPI peripheral's SDO drives and its SDI, SCK and CS receive; a
  transceiver's TXD and DI receive and RXD and RO drive; a PHY's TXD receives; JTAG TDO
  drives; I2C lines are open-drain; peripherals' INT, ALERT and PG drive (open-drain or
  push-pull) and their RESET, EN and mode pins receive; OUTPUT and INPUT name
  themselves; a processor GPIO named by its port is a GPIO first, its alternate ADC, LVDS
  or oscillator functions do not exclude the net, and the net name (UART TX, SPI MISO)
  gives it a likely direction. On a line with a certain push-pull output, bidirectional
  and unknown pins only receive. Candidates: driver_low_for_receiver (a push-pull
  driver's rail below 70% of a receiver's, the usual CMOS VIH, or below 2.0 V into a
  TTL-input part: info; warning at or below 50%, such as 1.8 V into 5 V),
  receiver_overvoltage (a driver's rail, or a pull-up on a push-pull line, more than
  0.3 V above a receiver's supply: warning; info when the receiver pin is 5 V tolerant
  by its name (FT, 5VT), its description or its 74LVC, 74LCX, 74LVT, 74AHC, 74VHC or NC7S
  family, up to 5.5 V, or when a series resistor of 1 kohm or more limits the current,
  with that current; an Output pin under such a pull-up is judged as well, since it is
  back-driven while tri-stated or high), open_drain_pullup_mismatch (an open-drain
  line's pull-up rail above a receiver's by more than 0.3 V: warning, softened the same
  way, and info on an open-drain output pin, many of which are rated above their supply;
  OpenCollector-typed I2C and MDIO pins receive; optocoupler, comparator, regulator and
  driver outputs are rated apart and not judged; below a receiver's VIH: info),
  unknown_rail (one row listing the I/O and pull-up rails of unknown voltage on signals
  between ICs, most signals first and every rail of a pair at once, each with the rails
  behind a 0 ohm link, ferrite, inductor or fuse that take its voltage (`joined_rails`);
  supply pins on nets named like signals, such as PA1, are flagged under
  `supply_pins_on_signal_nets` for a library check instead of a voltage, and parts with
  no supply pin read are listed; to pass in `rails`) and translator_ok (level
  translators and isolators listed, as handled when both sides are read;
  `translators_sides_unknown` counts the others). A pair is a warning only when one
  end's direction is certain (typed or named) and both rails and voltages are read with
  medium or high confidence; two bidirectional or unknown pins are judged both ways at
  info. Regulators', references' and supervisors' inputs are judged for overvoltage
  only, at info, and their pins drive only when typed or named outputs; gate, motor and
  LED drivers and parts powered above 5.5 V get info only (fixed thresholds). Left out:
  analog nets (ADC, DAC, sense, temperature names; op-amp and comparator inputs;
  optocoupler LEDs; a sensor's VOUT or AOUT), power nets, crystal lines (a crystal or
  resonator on them, or crystal pins no active clock drives; an XTAL_IN or CLKIN pin an
  oscillator or clock buffer drives is judged at info against the part's highest supply,
  and an oscillator with a supply pin drives whatever its prefix, Y1 included),
  differential pairs (a net whose complement exists, read with
  `diff_pair_checks.complement`: P/N, +/-, DP/DM, CAN H/L and its other styles; a pin
  named as one half of a pair, Intel's lower-case p/n suffix included; USB D+/D-, LVDS,
  MIPI, TMDS, CAN_H/L or MDI by name; an active-low RESET_N alone is no pair, and
  single-ended PERST, GTX_CLK or SFP status pins are judged), a bus transceiver's line-side pins (CANH/CANL, A/B, Y/Z, LIN, RS232 TxOUT and
  RxIN) and nets reaching one IC. Findings with the same check, severity, driver,
  receiver (and its role) and rails group into one row with the nets, the pin pairs and
  `count`, the number of signals; each names the line's off-board connectors. The
  summary comes first (ICs by part class, rail basis and confidence, signals checked,
  judged and between different levels with pull-up idle levels and terminations left
  out, open-drain lines, exclusions, candidates by check and severity, the unknown rails
  to ask about, translators handled); `part` lists each named IC's supplies, banks and
  sides and, as `part_pins`, every pin's rail, direction and basis, `net` each named
  signal's pins, pull-ups, connectors and series crossings; findings and listed rows
  page together through the shrink loop, the design read once. Registered in
  `NETLIST_TOOLS`, the manifest, the README and the server instructions;
  `schematic_review` gains a Logic levels checklist item. `derating.net_voltages`,
  `derating.read_parts` and `derating.SAME_VOLTAGE_EDGES`, `firmware_pins.part_kind`,
  `port_like`, `ethernet_part`, `BRIDGE_TEXT`, `CRYSTAL_TEXT`, `OUT_NAME`, `IN_NAME`,
  `XCVR_IN`, `XCVR_OUT`, `SLAVE_IN` and `SLAVE_OUT`, and `pull_resistors.COMPARATOR_TEXT`
  and `DRIVER_TEXT` are public now and their callers use them; the other tools' output
  is unchanged. Calibrated on Altium's 11 public AD25 example projects (56 ICs, 49 with
  an I/O rail read, 30 with its voltage known without `rails`; 212 signals between ICs
  checked, 93 judged; 210 differential, 17 crystal and 10 analog nets left out; the
  Arria 10 board's 14 banks mapped from LVDS, DIFFIO, CLK and HPS pin names, its RGMII
  lines to the PHY's 1.8 V DVDDH, its USB 3.0 FIFO bus on bank 2A, its regulator
  modules' PMBus pins on VDD33 and its oscillators into the PHYs' XI and XTAL1 pins
  against 3.3 V AVDDH and 2.5 V matched; 1 warning: a configuration clock reaching its
  flash through a 10k series resistor with a 10k pull-down, 0.9 V at a 1.8 V input; with
  `rails` for the Spartan-IIE board's VCCO, a 5 V character LCD's pull-ups into 3.3 V
  FPGA pins as 2 warnings, 8 data lines and 3 FPGA Output pins, and the two directions
  between the LCD and the FPGA's IO pins as info; the rails asked about were the DDR
  rail, the FPGA VCCO with the 0 ohm-fed accelerometer rail joined to it, the battery
  rail, and a DIMM's VTT and VDD in one pass, while a supply pin the offline netlist puts
  on a port-named net is flagged instead of asked; output at most 4.7k characters,
  runtime under 0.7 s)
  and on 53 private boards read in aggregate (mostly power and analog designs: 188 ICs,
  100 of them regulators or supervisors, 148 with an I/O rail read; 58 signals between
  ICs checked, 1509 reaching one IC and 279 differential; 0 warning or info candidates,
  8 unknown-rail rows
  over 8 rails, 6 parts without a supply pin read, 4 isolators whose sides are not
  named; output at most 3.7k characters, runtime under 0.1 s). Not covered: mapping a
  port supply such as VDDIO2 to its port's pins (those parts get a low-confidence
  guess), VBAT-domain pins, the actual VOH, VIH, VIL, VOL and absolute maximum of each
  part (estimated from its supply), LVDS, SSTL and other referenced or differential
  standards, signals through transistors, MOSFET level shifters judged as such (each
  side is judged on its own), drive strength and injection-current limits, and timing.
- `check_derating`: component derating check (roadmap #17, `services/derating.py`),
  read-only and offline from the compiled netlist. Net voltages carry their source and
  confidence: `rails` given by the user ('VIN=24,VBUS=5', or 'VIN,VIN_F=24' for names
  sharing one value; they win over everything), grounds (0 V), a voltage in the net name
  when a name token is a voltage as a whole (3V3, +5V, VDD_1V8, P3V3, 1P8V, 3P3, 72V,
  HV_400V; -12V, N5V and VNEG_5V negative; VDD33-style names only for the usual rails
  and at medium confidence, VDD12 and VCC15 (1.2 V or 12 V?) left unknown; never an EN,
  PG or SENSE net, an AC net such as 24VAC, a frequency such as 32.768K, a pin bank such
  as A10VCC..., nor a net on a converter's bootstrap, soft-start, compensation,
  feedback, current-limit, gate or mode pin such as BST_5V or SS_3V3), then the power
  tree: an output a given `vref` sets through its feedback divider, rails joined to a
  known one through 0 ohm links, shunts, ferrites, fuses, inductors or common-mode
  chokes, and a buck's switch node at its input (a boost's at its output, low
  confidence); last the unloaded midpoint of a two-resistor divider between two known
  voltages with nothing else on it but capacitors, connectors and IC inputs (medium
  confidence). Switching converters' inputs, outputs and switch nodes are ripple nets,
  each naming every converter role it has (output first). The voltage across a capacitor
  is the difference of its nets' voltages (arrays and feed-throughs per element); a
  bootstrap capacitor (a switch node to a net of unknown voltage) stays unknown. Each
  fitted part's rating, dielectric, technology, capacitance, case size and a resistor's
  power rating come from its parameters (Voltage, Rated Voltage, Voltage Rating,
  Dielectric, Temperature Coefficient, Type, Power, Power Rating, Case/Package, Size,
  Case Code (Metric), a package description's body size), then its value, comment and
  description (a text voltage only when it is a standard rating, so a rail named in a
  description is not read; of several, those beside the capacitance, dielectric, case or
  power; AC ratings kept apart), then its part number decoded by the published schemes
  of Murata GRM/GCM/GRT, TDK C and CGA/CGB/CGJ, Samsung CL, KEMET C...C, Yageo CC/AC,
  AVX/Kyocera, Taiyo Yuden and Walsin ceramics, KEMET T49x/T5xx, AVX TAJ/TPS/TCJ and
  Panasonic POSCAP (TP and TQ series) tantalums and Yageo RC, Vishay CRCW (and CRCW-HP),
  Panasonic ERJ and Stackpole RMCF/RMCP resistors (their size, and whether the series is
  a high-power one), then the footprint for the size (imperial codes, metric codes
  marked metric, metric-only or in IPC-7351 names, mapped to imperial). Each field names
  its source; when sources disagree the value most kinds of source agree on wins (on a
  tie a parameter, then the decoded part number, then free text, then the footprint),
  every candidate is listed, a generic technology is narrowed by a precise one (a
  polymer series after 'Tantalum'), and a lower rating that ties is judged too, its
  warnings kept as warnings that say the sources disagree. A capacitor of no stated
  technology with a ceramic dielectric or a chip footprint is taken for a ceramic. A
  resistor array of numbered pins pairs pins 1 and N, 2 and N-1 (also with one element
  on ground, unless it says bussed or its one ground pin is an end pin), and an array
  whose power sources disagree takes the lowest, per element. Findings: over_rated
  (warning), ceramic_voltage (above 75% of rated, or 50% on a ripple net, C0G too, with
  the next standard rating that meets it; the reason quotes the policy's limit:
  warning), tantalum_voltage (above 80% for MnO2 and polymer, mentioning 50% as common
  practice for MnO2: warning), electrolytic_voltage (above 80%, also polarized parts of
  no stated kind: warning), capacitor_voltage (a capacitor whose technology was not
  read: warning above the most lenient limit of any technology, info above the
  strictest), dc_bias (a Class II ceramic in 0603 or smaller with a high value for its
  size at 30% of its rating or more: info with an approximate loss band, Class III
  wider, the datasheet's DC-bias curve as the authority), resistor_voltage (above the
  stated or typical working voltage of its size: warning; info when only one end's
  voltage is known), resistor_power (dV^2/R against the stated or typical rating of its
  size: warning above it, info above the derated half; a 0 ohm link between two voltages
  is a warning), needs_current (LED series resistors whose bound with Vf = 0 exceeds the
  derated rating, naming the rail behind the LED when it drives the resistor through it,
  shunts, series supply resistors and resistors from a rail to a net of unknown voltage:
  info saying what to get from the datasheet) and unknown_rating (one info row per board
  listing the capacitors with a known voltage and no rating). Identical findings group
  into one row with a count. Thresholds come from a policy table: the defaults (after
  Altium's ceramic capacitor derating article and its schematic review checklist),
  `derating_policy.json` in the project folder (read and validated like a fab profile;
  `examples/derating_policy.json` holds the defaults) and the `policy` argument;
  `policy` in the result says which applied and which keys each layer set, a note says
  when a ripple limit looser than the standard one has no effect, and invalid keys or
  values answer `invalid_policy` with a hint. The summary comes first (parts checked by
  type, net voltages by source, ratings, dielectrics, cases and technologies by source,
  conflicts, findings by check and severity), then rails_without_voltage (rails whose
  voltage is not known, most capacitors first, to ask the user about; not switch nodes
  or converter control nets). `part` and `net` narrow the findings and list those parts
  with every parsed field, its source and the voltage across, and the named nets'
  voltages; findings and listed parts page together, and the parts are read once and
  only rendered per step of the shrink loop. `schematic_review` gains a Ratings
  checklist item. `power_tree.tree_data` (the built tree as plain data),
  `bom_checks.IPC_SIZE` and `METRIC_SIZE` and `fab_profiles.read_project_json` are
  public now and their callers use them; the other tools' output on Altium's examples is
  unchanged. Calibrated on Altium's 11 public AD25 example projects (743 capacitors and
  530 resistors; ratings read for 93% of capacitors, 82% from parameters; 388 capacitors
  with a known voltage; 7 ceramic_voltage warnings, all 100 uF 6.3 V parts on a 3.3 V
  net that is a buck's output and two bucks' input, 8 dc_bias parts, 48 parts listed for
  currents, 4 divider legs above their derated power, 256 capacitors whose two voltage
  parameters disagree, settled by their text and part number; a hand review of about 100
  distinct parts found no parse errors left after a generic 'Tantalum' learnt to defer
  to a polymer series and POSCAP values were read in uF) and on 53 private boards read
  in aggregate (1113 capacitors and 935 resistors; ratings read for 93% of capacitors:
  27% from parameters, 53% from text, 13% from part numbers; a decoded part number
  agreed with 459 of 462 ratings read from parameters or text (on the 3 others the
  comment and description give four times the part number's rating, and the part number
  now settles the tie), all 597 sizes and all 592 dielectrics read elsewhere; 209
  capacitors with a known voltage, 128 ripple nets; 38 capacitors with a ceramic_voltage
  warning, 37 of them on converter outputs (23 between 50% and 60% of their rating) and
  1 at 75-100% of the rating its part number gives; 3 dc_bias parts; 50 parts in
  needs_current; output at most 11k characters, runtime under 0.1 s). Calibration and
  review removed name voltages read from a crystal frequency, a pin-bank name, a
  bootstrap or soft-start net named after its rail, a P-form name with a trailing V
  (P1P8V read as 8 V) and a VCC with two digits that are no usual rail (VCC36 read as
  3.6 V); a 0 ohm link on an LED's rail taken for its resistor; voltage-parameter
  conflicts judged against the outvoted value; an isolated resistor array with one
  element on ground read as every pin to ground; and a divider's top leg taken for a
  series supply resistor. Not covered: transients, tolerance and ripple amplitude
  (voltages are nominal), ripple current and ESR heating, temperature derating curves,
  resistor pulse and surge ratings, the actual DC-bias value, inductor, diode, MOSFET
  and connector ratings, and boards whose rail names carry no voltage until the user
  gives them in `rails`.
- `get_power_tree`: power tree extraction and power-path checks (roadmap #15,
  `services/power_tree.py`), read-only and offline from the compiled netlist, as JSON or
  a Mermaid flowchart (sources -> protection -> converters -> rails -> load counts).
  Converters fitted in the active variant are recognised by their words (category words
  first: supervisor, reference, charger, ideal diode, hot-swap, eFuse, load switch,
  PMIC, though a supervisor, reference or load-switch word after a regulator word is a
  feature of that regulator; then the earliest topology word: buck, boost, buck-boost,
  SEPIC, flyback, inverting, charge pump, LDO, switching, power module or PSU), by
  part-number families when the words say nothing, and by pin names alone (VIN/IN with
  VOUT/OUT or SW, plus FB, EN, PG, BST, SS or COMP) when no word names another function;
  MOSFET and gate drivers are left out, and a regulator module on a connector's
  designator (J, P, X) is a converter when its words name a regulator and its pins an
  input and an output. Each carries its basis. A part's input is the net on its VIN/IN
  pins (a supply pin when it has none); its output the net on its VOUT/OUT pins, the far
  side of the inductor on its SW/LX/PH pin (past the diode for a boost, through a
  transformer and diode for a flyback, through the MOSFETs its gate pins drive for a
  controller), for a load switch, eFuse, hot-swap or ideal-diode controller with no OUT
  pin the far side of the MOSFET it drives, pins named for their voltage (+15V, -15V on
  a DC/DC module) or the top of its feedback divider; PMICs get an edge per output,
  typed buck or LDO. References and supervisors are listed (with the rails a supervisor
  monitors and who reads its outputs), never taken for converters. Rails are
  `check_decoupling`'s rails and supply pins of any part, every converter's input and
  output, connector and battery power pins and unnamed nets that only carry power between
  two of them (dead-end branches pruned). Edges: converters, and fitted 0 ohm links and
  shunts, resistors up to 100 ohm (a larger one only into reference inputs, such as a
  DDR or FPGA VREF divider), ferrites, inductors, common-mode chokes, fuses and PTCs,
  NTCs, diodes (anode to cathode), MOSFET and transistor channels (P or N by their
  words), switches, relays and jumpers between rails, oriented outward from the power
  inputs. Sources: connector power pins (`check_interfaces`' classes and exposure; a
  connector on a rail made on the board, reached from a likelier input such as a power
  jack, or less likely than another input connector on its net, is an outlet; as likely
  ones on one net share one input row), batteries (their positive pins, one source per
  net), converter outputs, voltage references' outputs, a converter's own bias pin
  (VDDB, INTVCC, VDD33, an AVINO feeding its own AVIN), IC pins that regulate their net
  (VCAP, VDD_CORE, an internal SMPS's LX through its inductor, not a regulator's own
  input such as VDD_REG_IN) and IC output pins on a rail. Loads per rail are parts with a
  supply, reference or capacitor pin on it, never passives. Per converter: EN state
  (tied to its own input or another rail, pulled up or down, an undervoltage divider with
  its ratio, held through a series resistor by a shared enable net, driven by a GPIO,
  another converter's PG, a supervisor, a connector or a transistor, capacitor only,
  floating or unconnected; held_off when tied or pulled to the level that keeps the part
  off, read from the pin's name and negation: EN, RUN, ON high, EN\ low, SHDN\ high, INH
  low, a plain SHDN or a module's ON pin not guessed), PG (pulled up to which rail, read
  by which EN, reset or GPIO, or unused; PGATE, PG_DRV and PGM are not power-good, nor a
  bare PG beside POK or PGOOD or on a capacitor to the part's own pin), input and output
  capacitors (any capacitor prefix), the feedback divider (Rtop and Rbot, two resistors
  in series followed, Vout/Vref, the Vref the rail name implies and the nearest common
  reference within 3% (0.4, 0.5, 0.6, 0.75, 0.765, 0.8, 0.9, 1.0, 1.2, 1.215, 1.23 or
  1.25 V), or with `vref` the output it sets, and the same with a leg a two-pin jumper,
  header or switch joins to ground or the output; an unnamed pin with a divider from the
  output is read as ADJ, and a regulator with no ground pin uses the LM317 formula Vout
  = Vref (1 + Rbot/Rtop)), soft-start, current-limit, mode and sense pins, and for a
  switcher whether an inductor or ferrite separates its input from its source (pi, LC,
  shared; not one on a branch its input feeds). Per power input: the series chain inward
  to the first branch or converter, with fuses or eFuses, reverse-polarity parts
  (forward diode, a P-FET with its drain or an N-FET with its source toward the input,
  ideal diode, a diode or TVS to ground behind the fuse as a crowbar; a P-FET with its
  source toward the input is a switch, its body diode conducting a reversed supply), TVS
  and clamp diodes, inrush parts, filters and bulk capacitance. Findings:
  rail_without_source (warning; info for a rail that reaches one part only, a connector
  or a test point, or one part only through a resistor above 100 ohm; none for a VREF
  input or a rail fed through a series part from a net not read as a rail),
  enable_floating (warning; info for a capacitor only), enable_held_off (warning; info
  for one channel of a multi-output part), fb_divider_mismatch (warning when a given Vref
  sets an output more than 5% from the rail name's voltage, also with the jumper leg,
  else info with the implied Vref), no_input_fuse, no_reverse_protection (not for USB)
  and no_input_tvs (one per input net, external inputs and power-only connectors of
  unknown exposure), smps_input_unfiltered, no_output_capacitor, no_input_capacitor
  (when `check_decoupling` does not cover the input), converter_unknown_rail (output or
  input not traced, or an output reaching nothing), pgood_unused and sequencing (EN/PG
  and rail-to-EN chains), all info; enable_tied_note runs only when named. `rail` and
  `part` narrow the tree and findings (a converter's own findings only with that
  converter); converters, findings, rails (those with loads and no source first),
  sources and power inputs page together on one offset, and the tree is built once and
  only rendered per step of the shrink loop. A `vref` for a part that is not a converter,
  or a converter with no divider read, is noted. `build_power_tree` returns the tree as
  plain data for the derating (#17) and thermal (#21) items, `power_findings` its
  findings, `prepare_power_tree` and `render_power_tree` the built view and its pages.
  `interfaces.audit_connector` (one connector's pins, walks, power feeds, exposure and
  shield) is public now and `run_interface_check` uses it; the other tools' output on
  the public examples is unchanged apart from `schematic_review`'s new Power checklist
  item. Calibrated on Altium's 11 public AD25 example
  projects (16 converters on 2 boards: MiniPC's ten bucks and modules, three VLDOs and a
  current-limit switch, SL1's two LM1084-ADJ read with the three-terminal formula; 99 of
  103 rails supplied; 7 warnings, all on MiniPC: six enable pins left unconnected or on
  nets with nothing driving them and one held off by a pull-down with nothing driving
  it; MiniPC's power jack gets no_reverse_protection, its P-FET being a switch) and on
  53 private boards read in aggregate (114 converters on 23 boards, 113 by their words
  and 1 by pin names alone, 7 of them modules on a connector's designator; 106 of 108
  parts an independent word list names as regulators found; 95 outputs traced; 387 of
  452 rails supplied; 20 of 20 implied Vrefs within 3% of a common reference; 2
  warnings, both rails with two ICs' supply pins and nothing feeding them; 256 info,
  most of them input-protection and SMPS-filter advice and converter outputs not
  traced). Calibration removed false positives from converters' own bias regulators,
  capacitors with other prefixes, internal regulator pins, enables held through a shared
  enable net, connectors that pass power on or share a net with a likelier input,
  gate-control branches taken for power paths, P-gate pins read as power-good,
  regulators with a reset or watchdog read as supervisors, and modules' ON pins whose
  logic varies. Not covered: ratings (current, voltage, power, thermal; items #17 and
  #21), Vref and thresholds (datasheet facts, given with `vref`), converters whose pins
  are all unnamed and whose rails carry no voltage in their name, isolated grounds, PMBus
  or register-set outputs, and a load's actual current.
- `run_erc`: an offline approximation of Altium's ERC (roadmap #5, `services/erc.py`),
  read-only, from the saved sheets compiled offline (the loaded netlist is left as it is)
  and the project's Error Reporting levels and Connection Matrix. Altium never saves its
  own ERC result in the project files; the output says so. Violations carry Altium's
  violation-type names (project_options' list), groups (Components, Documents, Nets,
  Others) and message wording from its Design Validation page, with
  level, sheet, object, net, location in mil and a suggested fix. The level is the
  project's for the type, else Altium's default (Nets with only one pin: No Report, as
  Altium's documentation says; Duplicate sheet numbers and Port with no matching ports,
  with no agreed default, are assumed No Report, as Altium's own project files write
  them); Connection Matrix violations take the cell's level and are not reported when
  their type is No Report. Errors come first; Off-grid object and Object not completely
  within sheet boundaries come last in their level, and a page lists them one row per
  sheet with a count and examples unless `checks` names them. 32 of Altium's types are
  checked, each registered in a small registry:
  - Nets: Nets with only one pin (a pin with a wire, label, port or power port on its net
    and no other pin), Nets with multiple names (net labels, power ports, off-sheet
    connectors, and sheet entries and ports when the project lets them name nets; bus
    names left out), Duplicate Nets (two nets with one name, unless Append Sheet Numbers
    to Local Nets is on), Global Power-Object scope changes (a power port wired to a port
    or an off-sheet connector under Hierarchical scope; the compile now also makes a power
    port on an off-sheet connector's wiring local, as Altium's documentation says, which
    no example or surveyed board has), Floating net labels and Floating power objects
    (touching no wire, bus, pin end or other object at their hot spot), Unconnected
    wires (wiring that
    reaches no object but junctions), Port with no matching ports (Flat and Global scope:
    no other port of its name and no entry above it), Sheets containing duplicate ports,
    Nets containing floating input pins (an Input pin that is its net's only pin: alone,
    or on wiring that reaches no other pin), Nets with no driving source (an Input pin and
    no IO, Output, Open Collector, HiZ, Open Emitter or Power pin, Altium's definition;
    Passive pins drive nothing; the message lists the net's pins, as Altium's does), Net
    Parameters with no value or no name, Missing Positive / Negative Net in Differential
    Pair (Differential Pair directives on <pair>_P / <pair>_N nets, matched across the
    design; a net with a channel's room suffix pairs within its channel).
  - Judged by the Connection Matrix (group Nets, as on Altium's page): Nets with
    possible connection problems and Nets containing multiple similar objects (pins
    against pins on the whole net; ports and sheet entries
    against the objects on their own sheet copy, each sheet entry against the port it
    matches, ports against ports on every sheet under Flat and Global scope: judging
    boundary objects across the flattened net reported correct hierarchical I/O as errors
    on Altium's examples), Unconnected objects in net (the matrix's Unconnected column:
    a pin alone on its net, a port or sheet entry nothing touches on its own sheet, even
    when its net goes on through the hierarchy).
  - Components: Duplicate Part Designators (with a note when a designator from the
    project's .Annotation file makes the clash, and how many of its entries name no placed
    part), Un-Designated parts requiring annotation, Unused sub-part in component and
    Components containing duplicate sub-parts (from the
    placed parts; compile-masked parts count as placed), Components with duplicate pins
    (one placed part, its current display mode), Sheet Symbol with duplicate entries.
  - Documents: Port not linked to parent sheet symbol (under a hierarchical scope, per
    symbol placing the sheet), Sheet Entry not linked to child sheet, Missing child sheet
    for sheet symbol, Duplicate sheet numbers (the SheetNumber sheet parameter; blank and
    * left out), Sheet Names Clash (project sheets sharing a file name in different
    folders).
  - Others: Off-grid object (pin ends, wire vertices, net labels, power ports, ports,
    junctions, off-sheet connectors and sheet symbols off the sheet's snap grid, as the
    sheet header gives it; a sheet without one, or with snap off (SnapGridOn left out of
    the header), is not checked and a note says so), Object not completely within sheet
    boundaries (the same points beyond the sheet's size), Floating Directive Object
    (Parameter Set and Differential Pair directives on no wire, bus, harness or sheet
    symbol; No ERC markers on nothing).
  Suppression: types at No Report, and violations under an active No ERC, are counted in
  summary.suppressed and listed only with min_level="all". A No ERC suppresses a
  violation about an object (an unconnected pin, port or sheet entry, a floating label, a
  wire, an unmatched port, an off-grid point) when it sits on that object, and a violation
  about a whole net (its names, drivers, pin types, its one pin, a floating input) when it
  sits anywhere on that net: designers' markers in Altium's examples and the corpus sit on
  a net's wiring or stub ends, not on the conflicting pins (unconfirmed in Altium); a
  specific No ERC covers the types it names (all when they cannot be read). Compile masks
  hide every part, wire, label, port, directive, sheet symbol (its entries included) and
  point they cover. Advisories, level info and never mixed with Altium's names:
  near-duplicate power aliases (separate nets read as one voltage, 3V3 / +3.3V /
  VCC_3V3 / P3V3, by
  `pull_resistors.rail_voltage` and `decoupling.net_kind`; a qualifier glued to the
  voltage, 2V5OUT or 3V3A, names another rail), names differing only in case,
  near-miss wire ends (a wire end touching nothing 3 to 10 mil from a pin's end: drawn to
  the pin, not connected), and the ground nets with their pin counts.
  report="single_pin_nets" lists every single-pin net with its pin and sheet whatever its
  level. Filters: checks (violation names, groups, advisories), sheet, min_level
  (warning, error, fatal, all); paginated and cut to fit. `schematic_review` now runs it
  first (its errors before the scope question, its warnings judged per sheet in Phase 2);
  `get_project_options` cites its counts (offline_erc) and says how many violations each
  check set to No Report hides here (offline_erc_hidden); `check_release_readiness`
  quotes its counts in the ERC gate's evidence, which stays blocking and not_verified.
  Calibrated on Altium's AD25 examples (11 projects; MiniPC, the PC4 SODIMM, Harness A
  report nothing; MiniPC's own levels switch off the 239 off-grid objects and 122
  unmatched power entries this finds) and on a private corpus of 63 boards (aggregates
  only: 12,988 violations, 12,481 of them off-grid objects on 57 boards drawn off their
  sheet's snap grid; 55 errors; a first page is at most 52k characters, the off-grid
  rows folded per sheet).
- The offline compiler's ERC facts (`sch_netlister.ErcFacts`, `CompiledNetlist.erc`):
  every net (single-pin and pinless ones included) with Altium's name for it, its pins,
  objects (pins with their electrical type, labels, power ports, ports and sheet entries
  with their I/O type, off-sheet connectors) with their locations and whether they touch
  anything, its active No ERC markers and what each sits on; the placed parts per sheet
  copy (and whether the .Annotation file named each), the .Annotation entries naming no
  placed part; parameter sets with the net their wire is on; the power ports a port or
  off-sheet connector makes local; and per sheet document (`ErcSheet`) its size and snap
  grid, sheet number, symbols with their entries and child sheet, the sheets placing it,
  unconnected wires,
  floating No ERC markers, the points the grid and sheet checks read, duplicate pins and
  near-miss wire ends. The netlist and its warnings are unchanged, but for a power port
  wired to an off-sheet connector under Hierarchical scope (now local, as above).
  `parse_sch_connectivity` reads No ERC markers (RECORD=22), parameter sets (43 with
  their 41s), the sheet size and snap grid (31) and the SheetNumber parameter;
  `AltiumClient.compiled_netlist` gives the cached compile without making it the loaded
  netlist; `project_options.type_number` and `level_index` are public, with `erc_levels`
  and `matrix_levels` reading the project's levels and matrix, and `sch_netlister.alpha`
  (1 -> A) letters sub-parts.
- `get_firmware_pin_map`: firmware pin map and firmware-facing pinout review (roadmap #20,
  `services/firmware_pins.py`), read-only and offline from the compiled netlist; the
  PcbDoc is read only when a debug or memory pin reaches no connector or test part, for
  pads and vias marked as Fabrication Testpoints. Candidate processors are U/IC and module
  parts fitted in the active variant, ranked by GPIO-named pins (PA0, PB12, PTA1, RB3,
  P0.12, P1_05, PIO0_7, P407, GPIO1_IO05; GPIO12, IO12, PIO[2], DIO_5 as GPIO numbers;
  FPGA IO names; a scheme counts only with enough pins of it, so a flash's IO0-IO3 or a
  PHY's RD0 never make a processor), IO-typed pins and processor, MCU-family, FPGA, radio
  or module words; memory parts are never candidates; an FPGA is labelled one. The top
  candidate is mapped (mcu= names others). Each pin: number, full name and its
  alternatives, port and bit, electrical type, net, the parts it reaches on its net and
  behind up to two series parts (check_interfaces' series edges; a resistor of 1 kohm or
  more or a series capacitor only as the last step), pulls (check_pull_resistors'
  reading) and a likely function with its basis: a dedicated pin name first (NRST, BOOT0,
  OSC_IN, MSEL0), then the net name (a bus token such as USART2 or SPI1 wins over a bare
  role token such as TX or CS; a role token of two buses such as RTS1_DP names neither,
  LED_RX is a LED and DDR4_SPD_SDA an I2C line; a crystal only when a crystal is
  reached; Altium's own NetR9_2 names are skipped), then the parts reached (a LED or
  switch, a crystal, a transistor, a bridge's RXD as the MCU's TX, a SPI device's SI as
  MOSI, an Ethernet PHY's TXD0 as Ethernet, a CAN transceiver's TXD as CAN; a peer pin
  that lists several functions gives none), then the pin's alternate functions when they
  agree (I2C among several when the line is pulled up); a pin's own single alternate
  function wins over a peer pin's name it does not list. Functions: UART, debug UART,
  SPI, QSPI, I2C, SWD, JTAG, vendor programming, USB, CAN, Ethernet, SDIO, I2S, memory
  bus, high-speed lanes, display, camera, crystal, reset, boot/strap, enable, interrupt,
  analog, PWM/timer, LED, button, wake-up, RF, tied, NC. Power and ground pins are
  summarised per net. Checks: `uart_crossing` (TX meeting TX or RX meeting RX on UART
  lines and on a CAN controller's lines to its transceiver: warning when both ends are
  certain by pin type, the MCU pin's own name or the peer's part: a USB-UART bridge,
  another processor or module whose pin lists no other function, a CAN, RS-485, RS-232
  or IrDA transceiver whose TXD/DI takes the MCU's TX; info for a connector pin named TX,
  for net-name-only cases and for a pin named TX on a net named RX), `debug_access` (SWD,
  JTAG, UPDI, PDI, Spy-Bi-Wire, C2, ICSP, a vendor SPI debug port and UART bootloaders,
  with STM32's PA13/PA14/PB3 and USART1 and each ESP32 or ESP8266 family's boot pin, EN
  and UART0 as vendor defaults; per role a pin's own name wins over a vendor default and
  a net name, so a dedicated TCK beats a soft-core JTAG on user I/O; a GPIO on a net named
  for another part's debug pin drives that part's port, and that part is reported as
  programmed in-system through the GPIOs; a TDO into another part's TDI is named as a JTAG
  chain; a switch or jumper on the boot pin counts as access; warning when no set reaches
  a connector, test point or testpoint pad, also when the debug pins are unconnected and
  only USB reaches a connector, info for partial sets, USB only, in-system access, nothing
  recognised, and for a part named in mcu= that is no processor), `boot_straps` (boot,
  strap, mode, program and reset pins, FPGA mode pins (MSEL, M0-M2), ESP32 strapping pins
  and the STM32F1/F2/F4 BOOT1 on PB2: tied, pulled, a divider with its ratio and voltage,
  a 0 ohm tie, set by a switch or jumper, driven by an output or by another processor's
  GPIO, shared with the part's own pin (a PHY's CONFIG pin on its LED pin's net), Not
  Fitted pulls, or the internal default; warning for a divider between 30% and 70% of the
  rail and for an STM32's BOOT0 held high or an ESP32's boot pin held low with no switch,
  jumper or driver to change it; info for a strap driven only by another processor's GPIO
  and for a floating BOOT1 when BOOT0 can be high), `external_memory` (flash, EEPROM,
  FRAM and FPGA configuration memories (EPCQ, EPCS, Platform Flash) by words or family
  part numbers on the mapped MCU: bus I2C, SPI, QSPI, parallel or serial configuration,
  the MCU pins and memory pins, chip select, WP/HOLD/RESET and address pins' state, a
  warning for a floating HOLD or RESET on a SPI memory, and info on whether its bus
  reaches a connector or test point for programming), `unused_pins` (the debug port's
  unconnected pins are left out of the advice, strap pins are named),
  `conflicting_assignment` (two MCU pins on one net, a warning when both are typed Output;
  pins tied together through a 0 ohm link and strap pins on a shared line are left out; a
  net named for a bus the pin's listed alternate functions lack) and `variant_changes`
  (parts on a pin's path Not Fitted in some of the project's variants, or valued
  differently). Notes name a part that reads as a processor but has no connected pin, and
  duplicate designators from the compile among the parts involved. Formats: JSON rows
  (default), CSV of every pin, a C header (`#define NET_PORT GPIOA` / `NET_PIN 9`: GPIOA
  only on STM32-family parts, PTA for Kinetis, PORTB for PIC, else the port letter or
  number; names unique across pages) and a Zephyr-style devicetree fragment (controller
  labels only for STM32, Kinetis, nRF, LPC, i.MX RT, ESP32 and RP2040; `GPIO_ACTIVE_LOW`
  from negated names, _B and _L only after a control name such as RESET_B; GPIO uses and
  GPIOs that drive another part's debug pins as `zephyr,user` gpios, peripheral pins as
  pinctrl comments; property names of at most 31 characters); anything else is a comment,
  never invented. Rows and findings page together with a shrink loop. Calibrated on
  Altium's public examples (an STM32 with a Wi-Fi module, a Bluetooth SoC, two FPGAs: 2
  warnings, for the STM32's unconnected SWD and its BOOT0 held high, and 16 infos); the
  private boards hold no processor, so there it showed no false candidate on 49 unique
  boards and, mapped by force on their largest ICs, no warning.
- `package_for_xfn` snapshots gain a `firmware_pins` table (MCU, pin, name, port, bit,
  net, function, role, pull and the parts reached as JSON) for every fitted MCU, radio or
  module candidate (at most four, else the top candidate). A new table older readers
  ignore: `SCHEMA_VERSION` stays 1. A map that cannot be built leaves the table empty
  with a warning; the snapshot is written either way.
- `check_interfaces`: connector and interface protection audit (roadmap #16,
  `services/interfaces.py`), read-only and offline from the compiled netlist and, when it
  loads, the PcbDoc. Connectors are J, P, CN, X, CON, CONN, USB, RJ, HDR (and a few more)
  parts fitted in the active variant, not crystals, battery or fuse holders, jumpers or
  test points. Each pin is classed signal, ground, power (a rail by name or a supply pin,
  a power-jack's pins, or a net that feeds a rail through a fuse, series diode, ferrite
  or MOSFET channel, listed with the part), shield (shell or mounting pins by name or
  lettered designator, shield/chassis/earth nets, legs past the position count in the
  description) or unconnected; a ground pin whose net is not a ground and reaches one only
  through a capacitor, resistor or ferrite says so in its class basis. Signal pins that
  drive a LED (by name, or reaching a PHY's LED pin) or are a magnetics centre tap are
  tagged and not counted as lines. A signal pin's net is walked through up to 3 fitted
  series elements (resistors and 0 ohm links, resistor-array sections, ferrites and
  inductors, common-mode chokes paired by polarity, capacitors up to 10 uF) to the first
  IC, transistor, transformer or other connector, recording the ESD/TVS devices on the
  way (connector side or behind a series element), Not Fitted ESD and series footprints,
  plain or zener clamp diodes (not counted as ESD), capacitors to ground, pulls (the
  `check_pull_resistors` reading) and the first pin's name and electrical type. ESD parts
  are TVS/ESD/SPD prefixes, a protection family's part number (PESD, TPD, USBLC, PRTR,
  SMAJ/SMBJ, SMF, ESDA, IP4220, RCLAMP, SP05, ...; SP3xxx/SP7xxx only beside protection
  words, as transceivers share them) or text that names the part as one (TVS, transient
  voltage suppressor, ESD protection array, varistor, GDT); a part whose text first names
  something else (MCU, transceiver, PHY, regulator, eFuse, MOSFET) stays that, and a
  part's own rating ('ESD sensitive', '+/-15 kV ESD protection') is not a device; a bare
  'ESD' counts for a diode or an IC with no Input or Output pins. Exposure is external
  (USB, RJ45/Ethernet, D-Sub, circular, terminal block, power jack or power supply
  connector, audio, coax, video, SFP/QSFP module port by text or nets, or the pins reach
  a CAN, RS-485, RS-232 transceiver, or a PHY or USB part on high-speed lines, or carry
  field-bus names; a processor listing its buses is not a transceiver), internal
  (board-to-board, FPC/FFC, debug or programming header, test header, card edge or module
  socket, debug nets, a configuration jumper whose lines only reach feedback, select or
  strap pins, more than 60 pins) or unknown (pin headers, card slots), each with its
  basis; `external=` and `internal=` override it, and 'without magnetics' is not
  integrated magnetics. Findings: no_esd (warning on external connectors; info when
  unknown, behind integrated magnetics, on RF coax or an SFP/QSFP port, or when the parts
  the lines reach claim ESD-protected pins in their text, quoted for the datasheet),
  shield_floating (named shield pins connected to nothing: warning on external),
  ground_indirect (a ground pin reaching board ground only through a part: warning on
  external with no DC path, else info), esd_behind_series, shield_rc (quoting the TVS
  layout guidance, not calling it wrong), sparse_ground (fewer than 1 ground per 8
  single-ended signal pins on a connector with 8 or more; differential pairs carry their
  own return) and no_series_element (low-speed external lines, not high-speed,
  differential or field-bus, with no series resistor, 0 ohm or ferrite footprint), all
  info; with the PcbDoc, tvs_far_from_connector, straight line in plan view, pad edge to
  pad edge, when a TVS more than 2 mm from the connector pin is nearer the IC pin (warning
  beyond 5 mm on an external connector) or is farther than `max_distance` (default 10 mm,
  info), with a TVS on the other side of the board from the connector pad marked
  opposite_side and the text in the requested units. Internal connectors get findings
  (PCB ones included) only when named in `connector=`. Output: an overview by exposure, a
  summary per connector (exposure and basis, pin counts by class, protected and
  unprotected lines, shield state), per-pin tables (for named connectors always, cut to
  their signal pins with `pin_tables_truncated` when they do not fit; otherwise all, then
  external connectors only, as they fit), paged findings, warnings first, shrunk to the
  result size like `check_decoupling`. `services/pull_resistors.py` and
  `services/decoupling.py` helpers it reuses are public now (`build_design`,
  `is_resistor`, `resistor_sections`, `resolve_names`, `ohms_text`, `CONNECTOR_PREFIXES`,
  `TRANSISTOR_PREFIXES`, `NOT_CONNECTOR_TEXT`, `DIFF_HALF`, `is_capacitor`,
  `FERRITE_TEXT`, `farads_text`, `pcb_components`, `component_pads`, `THT_LAYER`).
  Calibrated on Altium's 11 public AD25 example projects (33 connectors: 12 external, 12
  internal, 9 unknown; 8 of 49 external lines reaching a part have ESD, the DisplayPort
  lanes with TVS arrays at the connector, all within 2 mm of its pads in plan view and on
  the other side of the board; 2 warnings, both on the MiniPC's USB 3.0 receptacle: no
  ESD, and its GND pins reaching ground only through a capacitor; 14 info) and on 53
  private boards read in aggregate (410 connectors: 50 external, 5 internal, 355 unknown,
  279 of them pin headers; 2 of 78 external lines reaching a part have ESD; 17 warnings:
  11 no_esd, 4 tvs_far_from_connector, 2 shield_floating; 105 info). Calibration removed
  false positives from mounting pins left unconnected (119 connectors), card slots, RJ45
  pairs in sparse_ground and field-bus lines in no_series_element. Not checked: the TVS
  part's ratings (standoff, clamping voltage, capacitance, direction: its datasheet), a
  transceiver's own ESD rating, whether a reverse-polarity or input protection part on a
  power pin is adequate (listed in the pin table only), copper between the TVS and
  ground, and connectors on other boards.
- `check_pull_resistors`: floating-net and pull-up / pull-down audit with I2C pull-up
  sizing (roadmap #14, `services/pull_resistors.py`), read-only and offline from the
  compiled netlist. Signals that need a defined state come from net names, the pin names
  of the parts on them and the pins' electrical types: reset, enable, shutdown, boot,
  strap, mode and program nets (NRST, RESET_N, PWR_EN, ~SHDN, BOOT0, PROG_B; an Ethernet
  TX_EN is data, a regulator's BOOT/BST pin a bootstrap), open-drain nets (OpenCollector
  or OpenEmitter pins, I2C, INT, IRQ, ALERT, PGOOD, FAULT, and STAT/CHG/FLAG pins), MOSFET
  gates and transistor bases driven from an IC pin (straight, or through one series
  resistor), comparator outputs, and on request (`connector_no_pull`) connector pins wired
  straight to IC Input or IO pins. Nets joined by a series resistor under 1 kohm are one
  signal. A pull is a resistor fitted in the active variant from the signal to a rail or a
  ground as `check_decoupling` reads them, or to a part's supply pin (a power module's
  power-good pulled to its own output); R-prefixed parts count as resistors when their
  text says so (RP, RS, RB), arrays are read as isolated (1 to N) or bussed, and an
  open-drain line may be pulled up to a net that pulls up two or more such lines (a
  GPIO-switched supply). Findings: no pull on an OpenCollector/OpenEmitter pin, an I2C
  line or a MOSFET gate driven from an IC (warning), on named open-drain, reset, enable or
  boot nets, bases and comparator outputs (info), none when a push-pull output (an Output
  pin, an IC's OUT, GATE, DRV, HO/LO) drives it (an I2C line or an OpenCollector pin is
  still reported), and info whenever the signal reaches a connector (the pull may be on
  the other board); pulls that are all Not Fitted (warning, info when the signal reaches a
  connector); both up and down, and pull-ups in parallel with their effective resistance
  (info). I2C SDA and SCL pair into buses by name stem (I2C1_SDA/I2C1_SCL, SDA0/SCL0,
  SMBCLK/SMBDAT), else by the parts they share, and each line is sized per NXP UM10204:
  the parallel pull-up against Rmin = (VDD - 0.4 V) / IOL (3 mA, 20 mA in fast-plus) with
  VDD from the rail's name (3V3, +3.3V, 1V8, P3V3, VDD33) and Rmax = tr / (0.8473 x Cb)
  (1000, 300 or 120 ns), Cb from `bus_capacitance_pf` or an estimate labelled with its
  basis (10 pF per pin, capacitors to ground, and 0.12 pF/mm of routed length when the
  PcbDoc loads), with a verdict ok, too_low, too_high (also when the rail's voltage is
  unknown: Rmax needs none) or unknown (and why). `i2c_mode` defaults to fast; a pull-up
  too weak only for the assumed fast mode is info. Paged and shrunk like
  `check_decoupling`, with `net` and `checks` filters (a gate's driving net finds the
  gate). Calibrated on Altium's 11 public AD25 example projects (72 signals classified, 48
  with a fitted pull, 26 findings of which 2 warnings: an on-board I2C bus with no
  pull-up) and on 53 private boards, read in aggregate (180 signals, 82 with a fitted
  pull, 112 findings of which 6 warnings; all 44 I2C lines without a pull, and both lines
  whose pull-ups are all Not Fitted, leave the board through a connector). Not checked:
  pins left unconnected (not in the netlist), unused op-amp or comparator sections (no
  multi-section op-amp or comparator in either set to validate on), pull-ups inside ICs,
  and bus speed, which the design does not record
- Pin electrical types: the sheets' pin records are read into `ComponentMeta.pin_types`
  (pin designator -> Input, IO, Output, OpenCollector, Passive, HiZ, OpenEmitter or
  Power; a pin without the property is an Input), each pin of a multi-part component
  keeping its own record's type and the normal display mode winning, and every netlist
  component carries `pin_types`, unconnected pins included, from both Altium's netlist
  and the offline compile. Checked on the AD25 examples' 47,099 pin records: supply pins
  are Power, resistor and capacitor pins Passive, transceiver TX pins Output, alert,
  interrupt and power-good pins of supervisors and regulators OpenCollector. The
  decoupling check's net helpers are public for reuse (`classify_nets`, `net_kind`,
  `net_tokens`, `first_function`, `designator_prefix`, `IC_PREFIXES`)
- The fork has its own identity: the repository is `kcisek/altium-copilot-kc`, the MCP server
  and the manifest are `altium-copilot-kc` ("Altium Copilot (KC)"), and `install.ps1` installs
  `altium-copilot-kc.exe` into `%LOCALAPPDATA%\altium-copilot-kc`, registers it under that name in
  Claude Desktop and Claude Code, and keeps its own state file. The update check and the release
  build follow the fork, so it installs beside upstream `altium-copilot` instead of replacing it
- Offline schematic netlister (`services/sch_netlister.py`): without Altium Designer and
  without an up-to-date `.NET`, the project's netlist is compiled from the saved sheets,
  so `query_net`, `get_component`, `search_components`, `get_sheet_context`, `get_bom`,
  `check_bom`, `check_decoupling`, `check_sch_pcb_sync`, `schematic_review` and
  `package_for_xfn` work without Altium, `get_design_hierarchy` names each port's net and
  the PCB tools link `sch_refdes` again. Each sheet is wired once (`parse_sch_connectivity`
  reads parts and pins, hidden ones flagged, wires, buses, harnesses with their connectors
  and entries, junctions, labels, power ports, off-sheet connectors, ports, symbols with
  their entries and compile masks in one pass, with the component metadata enrichment
  reuses) and every sheet copy (several symbols or a `Repeat(...)` placing it) is joined
  by the project's Net Identifier Scope (Automatic resolved as in `get_design_hierarchy`):
  ports by name (Flat, Global), net labels by name (Global), sheet entries to their child
  port in that copy (Hierarchical, Strict), power ports by name (local under Strict, and
  under Hierarchical when wired to a port), a net label and a power port of one name on a
  sheet, off-sheet connectors among the sheets one symbol places, ranged bus names on
  labels, ports and entries by member along the bus's own wiring (`D[0..7]`), `Repeat(X)`
  entries giving channel N the member XN, harness entries of one name on joined harnesses,
  junctions and pin ends within 3 mil of a wire (a wire, or a junction, never joins a bus),
  the parts of a multi-part component across sheets, compile masks removing parts, wires
  and labels, and hidden pins only through a legacy HiddenNetName. Physical designators
  come from the `.Annotation` file, else the sheet, else the project's multi-channel
  Designator Format and room naming; local net labels of a sheet compiled several times
  take its room name as a suffix. Nets are named by the netlist options (Power Port Names
  Take Priority, Allow Ports / Sheet Entries to Name Nets, Higher Level Names Take
  Priority), ties going to the case-insensitive smallest name, else
  `Net<designator>_<pin>` from the smallest designator and pin in natural order;
  single-pin nets are left out unless `NetlistSinglePinNets` keeps them. The result has
  the `.NET` load's shape (`nets`, `pin_to_net`, `parts` with footprint and the Comment as
  shown, then the same enrichment) plus `source: offline`, the compile time as
  `net_file_mtime` (so sheets saved later read as stale) and `compile_warnings`
  (duplicate designators and net names, nets with several names, sheet entries without a
  port, missing child sheets, compile-masked and unannotated parts, unconnected hidden
  pins, and what no sample confirmed: channel designators, `Repeat(X)` entries, harness
  member names, off-sheet connectors, Append Sheet Numbers to Local Nets, which is reported
  and not applied).
  `set_project_dir` loads with it (`load_mode: offline_netlist`, `netlist_source:
  offline_compiler`, `netlist_note`, `compile_warnings`, `net_identifier_scope`) instead
  of the netlist-free load, also when the cached `.NET` is stale (`cached_netlist_stale`);
  Altium's netlist is still used whenever it is up to date or Altium has the project
  open. `refresh_netlist` compiles again, or takes Altium's netlist once Altium has the
  project open; an unchanged compile is reused. `query_net`, `get_sheet_context` and
  `check_sch_pcb_sync` name the source when it is the offline compile, and the release
  gate's netlist check, `schematic_review` and `package_for_xfn` (whose snapshot leaves
  the session) say so. Validated against Altium's own output: 4 private
  projects' `.NET` files match exactly (208 of 208 nets with members and case-exact
  names, 235 of 235 parts with footprint and comment); Altium's 5 public AD25 example
  boards match their PcbDoc on every pin and all 1,274 net names; 62 of 63 private
  projects match their PcbDoc on every pin (13,623 of 13,639 pins; the other 16 sit on a
  board whose PCB lacks 30 of the sheets' parts) and 2,769 of 2,778 nets carry the PCB's
  name (the other 9 PCB names are on no schematic object); the 63 compile in 1.8 s
  in all (at most 0.11 s), Altium's 46-sheet MiniPC example in about 1.1 s
- `check_testpoints`: test access (DFT) review of the saved PcbDoc (roadmap #33), offline
  and netlist-free (`services/testpoints.py`). Probe sites are pads and vias marked as
  Fabrication Testpoints (top and bottom, now decoded from the Pads6 main subrecord and the
  Vias6 header as `Primitive.test_fab_top` / `test_fab_bottom`; KiCad's importer reads the
  same flags1 0x80 / flags2 0x01, and in the private set of 90 boards the 8 marked pads, on
  2 boards, are all SMD pads of TP parts) and the pads of TP parts (designator TP..., or a
  testpoint footprint), each on the faces its copper is on. Assembly Testpoint properties
  are not decoded (no file read sets one apart from the fabrication flags; main-subrecord
  byte 118 is 1 on exactly the 8 marked pads and 0 on the other 37,418 read, a candidate
  left undecoded). Reports the sites per side and source, the share of nets (2+ pads) with
  access per side and the probe side (auto: the side reaching more nets), the Testpoint
  Usage rules resolved per net (`get_design_rules` now decodes the Testpoint and Testpoint
  Usage rules: sizes, holes, sides, grid, under component; usage required / prohibited /
  don't care, VALID 0 being Altium's default Required), and findings: no probe sites,
  nets a usage rule or the `nets` argument requires without access (Altium's default rule
  over All is info unless the batch DRC checks it), nets reachable only from the other
  side of a single-sided fixture, rails (power-named nets, check_decoupling's name rules,
  so 3V3_EN or GND_SENSE is a signal, and nets of 25+ connections) with fewer than 2 or
  more than 10 sites, ground with about 1 site per 20 connections, and on the probe
  side's sites (both sides for flying probe): site size (1.0 mm preferred, 0.9 mm
  acceptable), centre spacing (2.5 mm, 1.8 mm minimum), board-edge clearance (3.2 mm),
  sites under another part's 3D body (info when the Testpoint rule allows it) and sites
  near parts over 5 mm tall; on every marked face: tented testpoint vias and sites outside
  the Testpoint rule; and fewer than 2 non-plated tooling holes. Profiles `ict` (default) and
  `flying_probe` hold guidance values (said so in the result), overridable in `limits`.
  check_release_readiness's testpoints gate now runs it (advisory: warn on a warning
  finding). Validated on Altium's public AD25 examples (SpiritLevel's 2 GND testpoint vias
  and 3 TP parts reach 4 of 126 nets; Bluetooth Sentinel's 11 TP parts 10 of 55; the others
  have no site) and the private set (90 boards, no exceptions: 12 with probe sites, 2 with
  marked pads, coverage 0.7% to 32%, median 11.8%; review median 9 ms, at most 48 ms after
  the board is loaded).
- `check_release_readiness`: one "is this ready for review or fab?" report (roadmap #38),
  modelled on Altium's Project Releaser and the board-finalising steps. A fixed table of
  23 gates, each with status (pass, fail, warn, not_verified, not_applicable), blocking,
  one line of evidence quoted from the check behind it and source_tool, the tool to call
  for the details. The gates call the existing services directly over the cached PCB
  index and design-notes cache (`services/release_gate.py`, no logic duplicated): Batch
  DRC report clean and not older than the board (get_drc_report's assessment: a report
  for another board, an incomplete one or one that never checked Short-Circuit is not
  verified, a stale one fails), ERC
  (blocking and always not verified: no ERC result is saved in the files; the evidence
  says whether a connected OutJob ERC validation output makes the Releaser run it),
  netlist newer than every sheet and schematic-PCB sync (both need the netlist),
  unrouted nets (check_connectivity, nets routed only through weak joins included),
  shelved or modified polygons, board outline, global fiducials per assembled side
  (check_dfm), mounting holes (parts of holes only, free pads with a 1 mm hole, bare
  holes), Not Fitted parts left out of the BOM and pick-and-place outputs (Altium keeps
  them on the PCB by design, so the outputs' variant is checked, not the board), the
  OutJob complete for every variant (gaps, BOM and pick-and-place on different
  variants, ERC levels that make the release fail) and its generated outputs newer than
  the design, Draftsman fab notes against the stackup (not verified without a Draftsman
  drawing), title-block revision and date across the sheets and the drawings' own
  parameters, project integrity, the Altium 365 copy committed and level, and
  silkscreen from the DRC report when the board batch-checks it (not verified when the
  run stopped at its violation limit); footprints against the libraries, lifecycle and
  compliance, teardrops, via stitching and the team's template are not
  verified, each with the reason. Only blocking gates fail (a non-blocking problem is a warn); a check
  that raises is not verified. Verdict: fail when a blocking gate failed,
  not_ready_unverified when none failed but a blocking gate is not verified, pass only
  when every blocking gate passed, warned or is not applicable (the summary names the
  blocking warns). Gate rows are paginated, each timed (ms). Validated on
  Altium's public AD25 examples (Bluetooth Sentinel not_ready_unverified; Mini PC fail on
  its OutJob's ERC levels; 13.9 s cold for Mini PC's 46 sheets, 1046 parts and 2
  drawings, 9.3 s after the session start's get_design_notes, 0.6 s warm) and a corpus
  of 63 projects (netlist-free loads: 41 fail, 22 not_ready_unverified, no exceptions;
  cold median 0.9 s, at most 2.5 s; DRC report missing in 45 and with violations in 18,
  8 boards with unrouted nets, 4 with shelved or modified polygons, OutJob gaps failing
  22, outputs older than the design in 25, the Altium 365 copy failing in 3).
- `release_review`: a short guided release go / no-go: check_release_readiness, then each
  failing gate's source tool for its cause and fix, ending in tables of blocking items,
  what was not verified (never reported as passed) and warnings.
- `check_fab_drawing`: the project's Draftsman drawings checked offline (roadmap #36). Each
  .PCBDwf (or one named) is classified from its views and notes as a fabrication,
  assembly or other drawing (with a second role when both are strong), and its notes,
  free text, tables, document parameters and layer notes are scanned with keyword and
  regex patterns for the fabrication checklist (IPC class with IPC-6012/6013/A-600, laminate
  material, Tg, UL 94 flammability, surface finish families ENIG, ENEPIG, HASL/LF-HASL,
  ImAg, ImSn, OSP and hard gold, solder mask and silkscreen colours, board thickness and
  its tolerance, copper weights, impedance where the stack has profiles, drill table,
  registration and tolerances, electrical test, rigid-flex items on a flex stack) and the
  assembly one (IPC-A-610 / J-STD-001 class, BOM table, a view of each side with parts).
  Each item is found (the line quoted briefly with its page, note and line), missing
  (with a suggested note line built from the design's numbers, e.g. the stack's total
  thickness, its copper weights and impedance targets; `<...>` marks a value to agree) or
  not applicable; a missing finish, thickness, material or IPC class is a warning.
  Cross-checks against the drawing's own PcbDoc (its source board when the project lists
  it): stated thickness against the stack's total and copper-to-copper height (a
  maximum or minimum as a bound, a tolerance on its own line applied to it, a number
  without a unit read as mm, inches or mil, whichever is nearest) and a stated layer count
  (not "inner 2 layers", a flex section or a double-sided assembly; errors when they
  contradict it), named laminates against the stack's dielectrics (FR-4 read as a grade),
  copper weights, impedance targets in the notes (info when the stack has no profiles to
  confirm them) and the impedance table's cached lines against the Layer Stack Manager
  profiles (layer, target, width, gap), the drill table against the board's holes
  (check_dfm's drill table, summed over spans; panels not checked), the layer stack
  legend against the stack, the BOM table's variant against the OutJob printing the
  drawing and the active variant and its cached parts against get_bom (not-fitted parts
  too when the table shows them; only with a netlist, otherwise not_checked says so), a
  drawing made from another board, a board changed after the
  drawing's last Import Changes (the last commit time in a Git clone, where file times
  are the checkout's; a board committed with the drawing is not counted) and notes that
  contradict each other (two finishes, thicknesses, IPC classes or colours). The
  Draftsman parser now also reads the cached layer stack (materials, thicknesses,
  weights, Tg, flex), the transmission lines behind an impedance table, the BOM table rows
  of each BOM data set (scanned from the bytes of the board copy it cuts out) and the BOM
  views' data source; `output_jobs.draftsman_outputs` names the drawing and variant each
  OutJob prints. Validated on Altium's public Mini PC drawings and a corpus of 54 drawings
  in 53 projects (no exceptions, at most 1.5 s per project): 50 fabrication and 4
  assembly drawings (49 also assembly); hit rates material 50/50, copper weight 50/50,
  Tg 46/50, surface finish 30/50, thickness 18/50 (none with a tolerance), mask and
  silkscreen colour 5/50, drill table 4/50, impedance 4 of the 32 where it applies, IPC
  class, flammability, electrical test and registration 0/50; all 19 stated thicknesses
  (18 drawings), 45 material sets, 49 copper sets, 4 drill tables and 4 impedance checks
  match the design; 1 of 46 legends and 25 drawings (24 by commit history) are older than
  the board, 5 drawings' BOM tables show another variant than their OutJob, and 6
  drawings were made from another board.
- `get_output_jobs`: the project's Output Jobs offline (roadmap #37), from a new .OutJob
  parser (`parsers/out_job.py`) whose keys are mapped only where Altium's 11 public AD25
  example and template OutJobs and a corpus agree. OutJobs come from the .PrjPcb document
  list; .OutJob files in the project folder (or one folder down, outside nested projects)
  it does not list are shown and flagged but not counted. Per OutJob: the variant (the
  job's VariantName, an output's own OutputVariantName where written; VariantScope 1 read
  as one variant for the whole job, other values passed raw), the container selected in
  the editor, each container (OutputMediumN: print job, PDF, folder structure, video) with
  its outputs in order and its path: release managed (the Output Path of Project Options)
  or manually managed (OutputBasePath), the [Media Name], custom or expression container
  folder (an expression of quoted text, ProjectName and project parameters such as
  "=ProjectName" is evaluated, one with CurrentDate or CurrentTime is not; 121 corpus
  containers use one, 40 of them dated), the [Output Type] / [Output Name] / custom
  output sub-folder, file naming (a PDF is named by its OutputFileNameSpecial expression
  when one is written, as all 13 corpus PDFs found next to one are), timestamp and
  add-to-project flags and the path Altium last resolved; and the outputs grouped by
  category with state (OutputEnabledN_OutputMediumM: connected with its order, 0 not
  connected; OutputEnabledN only mirrors the selected container, which every sample
  confirms), data source, variant and mapped settings (Gerber and Gerber X2 units,
  decimals, origin, board profile and the mechanical layers plotted, ODB++ units, NC
  Drill units, format and origin, IPC-2581 version and units, pick-and-place units and
  formats, ERC report options); other keys are counted or passed raw. coverage gives
  per role (fabrication data, NC drill, pick-and-place, assembly drawing, BOM, ERC, DRC)
  connected, present but not connected, or missing. Findings with severity, examples, an
  Altium fix and the knowledge-base article where one documents it: missing, unreadable
  or unlisted OutJobs; each missing role (no NC drill for Gerbers without ODB++, IPC-2581
  or Gerber X2 drill files is an error; an assembly-drawing gap is info when PCB Prints
  are connected); Gerber only on a rigid-flex stack or with blind/buried drill pairs; BOM
  and pick-and-place on different variants and a variant with no assembly drawing; data
  sources missing on disk, outside the project, a Draftsman drawing last updated from
  another board, the default source with several PcbDocs; an ERC output whose own
  ERCTypeN levels or Connection Matrix differ from Project Options (KB "Release Fails
  When ERC Validation Settings Differ from Project Options"); Output Path, ECO Log Path or
  manual container paths on another drive or a share (KB "Empty Project Release Caused by
  Output Path Configuration"); file paths estimated near or past 260 characters (KB "File
  Buffer is not Initialized Error"); Gerbers on the absolute origin next to ODB++ (KB
  "Resolve component position offset in ODB++ files") and Gerber vs NC Drill origins in
  one OutJob, both skipped when the board's relative origin is the absolute one; Top or
  Bottom Overlay missing from the V9 layer stack (KB "ODB++ Export Missing Overlay
  Layers"); board cutouts with Gerbers plotting no mechanical layer (KB "Board Cutout not
  in Gerber output"); Draftsman views whose own variant overrides the drawing's and
  drawings whose variant differs from the OutJob's (KB "How to control the Draftsman
  output variant for Project Release"; the Draftsman reader now reads the drawing's
  AssemblyVariantV2 and each view's Variant); generated files at a container's folder (or
  an output's sub-folder, or the PDF) older than the documents they come from by more
  than 2 minutes (file times only; nothing is written); outputs connected to no
  container. not_checked names what the files cannot tell (release folders, plotted layer
  sets, DRC output settings, which no sample stores, long-path support). The board facts
  come from a light read of the PcbDoc (Board6 stack and origin, component sides, region
  heads), so the tool needs no netlist. `project_tour` now uses it in its outputs phase.
  On Altium's examples: Bluetooth Sentinel's Assembly.OutJob plots Gerbers on the
  absolute origin while its Fabrication.OutJob has ODB++, and no OutJob validates ERC or
  DRC; Mini PC's two OutJobs carry ERC levels that differ from the project in 8 types, its
  Job1.OutJob is not in the project and its Draftsman drawing was updated from a board of
  the same name in another folder. On a corpus of 63 projects (113 OutJobs, 100 listed by
  60 projects, 13 unlisted; 854 outputs of 17 types; 253 folder, 170 PDF, 89 print-job
  and 20 video containers): no exceptions, median 0.07 s and at most 0.84 s per project,
  every answer under 37k characters; no ERC and no DRC validation output in 60 projects
  (one has them, not connected), outputs connected to no container in 26 (49 outputs),
  outputs older than the design in 25 (47 containers), a Gerber on the absolute origin
  next to a relative NC Drill in one OutJob in 3 and next to ODB++ in 4, board cutouts
  with no mechanical layer or profile plotted in 4 (13 more plot one: info), no assembly
  drawing in 9, no pick-and-place in 4, Draftsman views overriding the drawing's variant
  in 3 and drawings on another variant than their OutJob in 2, a variant without an
  assembly drawing in 2, both overlays missing from the stack in 1 (the bottom-side
  layers removed on boards without bottom parts in 4: info), 6 outputs whose data source
  is missing in 1, and Draftsman drawings last updated from a board of the same name in
  another folder in 16 (info) and from another board in 2.
- `check_project_integrity`: problems in the project folder, the .PrjPcb and the strings
  on the sheets that silently break compile, sync or saving, offline (roadmap #12). File
  checks: schematics with legacy extensions (.Sch, .S01, .S02 ...; Altium KB: Update PCB
  then removes every part they place) and a legacy .Pcb (a warning: the KB renames it
  along with them), documents listed in the project but missing on disk (sheets and
  harnesses are errors; device sheets, which Altium also looks for in the Device Sheets
  folders set in Preferences, and other types warnings), a .Annotation file that renames parts on a design with no sheet compiled
  more than once (the Compiled tab, PCB and BOM then differ from the editor; KB "Multiple/
  different designators in schematic"), one that renames nothing, entries for parts that
  are gone and a `<project>.Annotation` the .PrjPcb does not list, parts placed outside
  the sheet (all their points beyond it: compiled but not printed) or partly outside,
  compile-masked parts and masked copies, and variant entries that no longer fit their
  parts (bound by `get_bom`'s plan: designator changed, UniqueId gone, no such part,
  ambiguous, on a masked part; older [CompVar] Not Fitted lists too), with the project's ERC level for "Object not completely
  within sheet boundaries", "Incorrect link in project variant" and "Bus range syntax
  errors". Name checks on net labels, power ports, ports, sheet entries, designators and
  component parameters as written: hidden characters (Unicode Cc, Cf, Co, Cn and spaces
  other than U+0020; errors in net names and designators, the KB's CstrSchDoc save
  failure), non-ASCII characters (not in parameter values), leading or trailing spaces,
  brackets that are no `Name[first..last]` bus range, local parameters named
  'Manufacturer N' / 'Manufacturer Part Number N' (reserved by ActiveBOM, overwritten from
  Part Choices in exported BOMs per the KB) with the parts whose copilot MPN comes from
  one (sch_doc's `_MPN_RE` reads exactly those names, and the parameter is found as it
  does: the unnumbered name first, then the lowest N, quoted texts skipped; `get_bom`
  reads them among other spellings), and on multi-channel
  designs a Designator Format that puts channel text after the name on channel sheets
  carrying buses (KB "Duplicate Net Names error on Multi-Channel buses") or writes
  brackets, commas or spaces. Each finding groups one kind with its count, examples
  (sheet, object, location in mil), an Altium fix and its KB source; `checks=` filters,
  findings page under the result limit, and with a netlist the load diagnostics come
  along. The sheet size is read from the sheet record (SheetStyle 0-17 with KiCad's
  importer's table, UseCustomSheet with CustomX/CustomY, WorkspaceOrientation portrait;
  a custom size on a portrait sheet is accepted either way round, as Altium does not
  document whether it turns it);
  names keep their spaces and use Altium's %UTF8% copy, else Windows-1252, so 0x80-0x9F
  bytes read as the dashes and quotes they are. On Altium's 11 AD25 example projects the
  one finding is a value holding two no-break spaces in Mini PC; their sheets (A4
  portrait, A, B, C, D) keep every part inside. On a corpus of 63 projects (188 sheets):
  no exceptions, median 0.07 s and at most 0.67 s per project uncached; none with errors;
  982 compile-masked parts in 19 projects (info: usually deliberate; 945 of their 996
  UniqueIds are not on the saved board while 1,888 of 1,919 live ones are; the other 51
  are still on one project's board, which Update PCB would remove), 3,857 reserved
  parameters in 39 projects (info, Altium's intended overwrite; 38 with a .BomDoc, and
  844 of 1,941 local MPNs equal one of its Part Choices) and 1,918 parts whose copilot MPN comes from one, 9 net labels with
  U+2212 MINUS SIGN in 8 projects, 6 stale variant entries in one project (all six agree
  with the saved board), one missing library and one value with a trailing space; no
  legacy extensions, hidden characters, parts outside the sheet (the 10 parts with a
  point outside are compile-masked) or multi-channel designs.
- `diff_design`: what changed between two states of a project, as an ECO-style changelog
  for reviews and release notes (roadmap #45). Sources: two revisions of the Git clone
  holding the loaded project (commit hashes, branches, tags, HEAD~n, or `working` for the
  files on disk; by default the last commit that changed a design file against the files
  on disk, or a commit against its parent; with no revisions the recent commits that
  changed design files are listed to pick from) or two project folders. A revision's
  .PrjPcb and the .SchDoc, .PcbDoc and .Annotation files it lists are read with
  `git ls-tree` and one `git cat-file --batch` (no filters run, Git LFS pointers reported
  as not read) into a temporary folder outside the clone, parsed with the existing
  parsers and deleted; git runs with the `get_clone_status` hardening and only
  rev-parse, ls-tree, log and cat-file, so nothing is written to the clone. Files whose
  blob ids match are not parsed. Sections: project (sheets, project parameters),
  components (added, removed, re-designated, revalued Comment/Value with special strings
  resolved, footprint, parameters, description and library reference), variants (Not
  Fitted and alternate parts per variant, varied parameters), BOM line changes per
  variant (a variant whose changes equal the base BOM's is only named), nets from the
  PcbDoc's pads (added, removed, renamed, merged, split, pins moved between nets), parts
  added, removed, moved, rotated or flipped (0.1 mil / 0.01 deg tolerance) and footprint
  or designator changes on the board, rules (added, removed, changed scope, enable and
  values through `rule_values`; a priority change only when the order of the rules both
  states have changes), classes, differential pairs, stackup (layers, thickness, copper
  count, impedance profiles) and per-net routed length and via count. Components match
  by UniqueId (channel path, sheet, UniqueId alone) then designator, nets by shared pads
  (the project's Comparator Min Match % and Min Matched Members) then name, as Altium's
  comparator does. ICT-fixture hazards are listed first: nets that kept their name while
  their pins changed (flagged when only added or removed parts explain it) and
  designators now on a different part. Purely graphical edits are not reported, and
  schematic wiring not yet pushed to the board is not seen (no offline schematic
  netlister: see `check_sch_pcb_sync`); Local History folders and package_for_xfn
  snapshots are not read yet. Paged under the result limit, one section at a time if
  asked, lengths in the board's units or `units`. On a corpus of 63 projects, 62 in Git
  clones with design history (1 to 15 design commits listed), the last three design
  commits of each were diffed against their parents: 158 pairs (16 first commits have no
  parent), 73 with changes, median 0.7 s and at most 2.0 s per diff, no exceptions, and
  every clone left unchanged (file list, mtimes and sizes of 15,233 files compared
  before and after). Over those pairs: 137 component, 57 BOM, 27 variant, 38 net (14
  renamed, 11 added, 10 removed, one merge, one split, one pin moved), 168 placement, 38
  rule, 41 class, 12 diff-pair, 11 stackup and 176 routing entries, and 52 ICT hazards
  (35 designators on a new part, 30 of them with another comment or footprint; 17 nets
  whose pins changed under the same name).
- `get_project_options`: the project settings that change how a design is read, offline
  from the .PrjPcb (no netlist needed): the Net Identifier Scope and net naming (read as
  `get_design_hierarchy` reads them), Allow Single Pin Nets, diff-pair suffixes (Altium's
  _P/_N default; custom `[DiffPairSuffixN]` sections passed on raw), class generation
  (`[PrjClassGen]` and each sheet's component class, room, net-class scope and structure
  class), multi-channel Room Naming Style (`TChannelRoomNamingStyle`), designator format
  and level separator, the report level of every Error Reporting violation type, the
  Connection Matrix, the Comparator's object matching criteria and the output and ECO log
  paths with the output options. `[Electrical Rules Check]` TypeN is mapped to Altium's
  violation type of TErrorKind ordinal N-1 (Altium's WSM API order and the order of the
  165 violation names in AD25's Workspace Manager, anchored by the two slots Altium
  writes by name, MultiChannelAlternate and AlternateItemFail; Altium Develop's Workspace
  Manager appends the 166th and 167th) and the 17 matrix rows and columns to the
  dialog's pin, port and sheet-entry order. Each check is reported at its default
  (counted), lowered, raised, or, where its default is unknown, listed by level. The
  audit lists ERC checks switched off or downgraded from Altium's default (key checks
  first, with what goes unreported), Connection Matrix cells relaxed for driver
  conflicts and unconnected input pins, and every No ERC directive. A default is the level Altium's AD25 examples, the projects
  shipped in its extensions and a corpus of real projects agree on; where they disagree
  (e.g. Nets with only one pin) the check is not audited. Keys no Altium source verifies
  (Difference Levels types, Comparison Options Confirm and InclAllRules, the Annotate
  naming format, ERC types beyond the 167 names, TypeN values outside 0-3) are passed on
  raw under `unmapped`.
- No ERC directives (SchDoc RECORD=22) are read with the sheet hierarchy: location,
  IsActive and SuppressAll (generic or specific, absent read as true as KiCad's importer
  does, so records saved before the flags read as active generic ones), Symbol, and a
  specific one's ErrorKindSetToSuppress and ConnectionPairsToSuppress as written. Each
  takes the wiring of the object at its point (a pin's end, a port, a power port), else
  of the wire through it, without joining anything itself; with a netlist its pins name
  the compiled net and the net's pin count. A No ERC suppresses the violations reported
  at its point, not the whole net. `get_project_options` lists them flagged first: an
  active one on a power net (a power port on its wiring, or a rail name with no signal
  token: VCC_EN is a signal), a high-fanout net, a net with several pins, or (offline) a
  net that leaves the sheet through a sheet entry, a port (under the hierarchical scopes
  only one a symbol placing the sheet has an entry for; under Flat and Global one another
  sheet shares) or a net label another sheet shares under Global scope. On a corpus of
  63 projects (offline): 29 have
  ERC checks lowered from the default (26 switch off Nets with possible connection
  problems and Nets containing multiple similar objects, 3 Nets with no driving source),
  3 relax a driver-conflict matrix cell, and 62 hold 1,699 No ERC directives, all active
  and generic, of which 30 in 8 projects are flagged (10 on power nets, 9 on nets with
  several pins, 11 on nets leaving the sheet); no exceptions, at most 0.9 s per project.
- `project_tour`: a guided tour of the loaded project for a newcomer, in the order Altium
  teaches a design: project and sheet hierarchy (`get_design_hierarchy`, `list_variants`),
  each schematic block (`get_sheet_context` per sheet in tree order, asking first above
  12 sheets) and the power rails (from the sheets' regulators and power ports, confirmed
  with `query_net`, presented as a derived reading since no tool builds a power tree),
  the PCB (`get_board_info` stackup and impedance profiles, `get_component_placement`,
  key nets through `list_diff_pairs`, `get_classes`, `get_net_pcb` and `check_lengths`),
  outputs and manufacturing state (`get_drc_report` with its assessment,
  `check_pcb_health`, `get_bom`; OutJobs and generated outputs are named as not
  covered), then variants and design notes (`get_design_notes`). It adapts to the load:
  a netlist-free load tours each sheet's ports, symbols and notes and says the parts,
  pins, rails and BOM need the netlist; a PCB-only project (no schematic sheets) skips
  the schematic phase; a project without a PcbDoc skips the board phase and the DRC and
  health items. It ends in a Markdown project primer with a fixed
  11-section outline (a section the tour could not cover keeps its heading with the
  reason), which Claude shows in the chat and writes only after the user agrees to a
  path (suggested next to the .PrjPcb). Ground rules: state only what the tools return,
  cite sheets and designators, mark inferences as assumptions.
- `get_design_hierarchy`: the project's sheet hierarchy offline, like the OutJob's
  Report Project Hierarchy, from the .PrjPcb and the .SchDoc files alone (no netlist
  needed, so it also works after a netlist-free load). The sheet tree runs from the top
  sheet(s), those no sheet symbol places, down through the sheet symbols (RECORD=15 with
  their designator 32 and file name 33), with device sheets (the project's
  `[DeviceSheetN]` documents), `Repeat(name, first, last)` channels with their count, and
  the compiled copies a sheet gets down the tree. Per sheet: its ports (RECORD=18: name,
  I/O type, harness type) and its symbols' sheet entries (16: name, I/O type, harness;
  the child port's I/O type when it differs or is missing; `Repeat(X)` entries match
  port X). Each port and entry lists the net labels and power ports on its wiring
  (wires, buses, signal harnesses from the Additional stream, junctions, and pins at
  their electrical end; labels, power ports and ports of one name join on a sheet), and,
  with a compiled netlist, the net it joins (one per channel copy; through the other
  sheets' wiring when its own has no pins, where an entry takes only the copy of the
  child its own symbol places, told apart by the `.Annotation` UniqueId paths, and
  never a sibling symbol's net; a note when the copies cannot be told apart or its pins
  are in no compiled net).
  The Net Identifier Scope is read from `[Design] HierarchyMode` (0 Automatic, 1 Flat,
  2 Hierarchical, 3 Global, Altium's TFlattenMode order; 4 read as Strict Hierarchical,
  which that list predates), with what Automatic resolves to by Altium's rule (sheet
  entries on the top sheet: Hierarchical, else ports: Flat, else Global), a paragraph
  on how nets cross sheets under it, and the Netlist Options that decide net names
  (ports, sheet entries, higher-level names, power-port priority, sheet numbers) as a
  name precedence. Issues: symbols pointing at a file that is not a project sheet
  (`on_disk` when it is next to the parent), symbols without a file, cycles, bad channel
  ranges, entries without a port in the child (an entry matching only a child power
  port is info where power ports are global, an error under Strict Hierarchical) and
  ports without an entry (not under Flat or Global for a symbol with no entries at
  all, whose ports are global), I/O and harness mismatches, duplicate symbol designators and
  entry names, ports on a top sheet and several top sheets under a hierarchical scope,
  ports no other sheet shares under Flat or Global, sheets placed several times, and
  unwired ports and entries; findings of one kind on one symbol or sheet are grouped
  (`entries`, `ports`), `issue_help` explains each kind. `sheet=` narrows to a subtree;
  sheet details are paginated, the tree and issues come with the first page. Checked
  against the `.PrjPcbStructure` Altium wrote for its AD25 examples (every sheet
  symbol's sheet, designator and file matched). On the private set of 56 projects (180
  sheets) it ran without an exception in under 0.8 s per project: 3 projects have a
  hierarchy (22 sheet symbols, 171 entries; no device sheet or Repeat channel), 707
  ports; scopes seen were Automatic 40 (resolving to Hierarchical 3, Flat 5, Global 32)
  and Global 16; all 55 projects with a `.PrjPcbStructure` matched it (22 of 22 sheet
  symbols). Issues found: 124 ports no other sheet shares (10 flat or global
  projects), 3 unwired entries and 2 ports without an entry. In the 4 projects with a
  cached netlist, 202 of 234 ports and entries named their net (64 through other
  sheets), and each of the 63 with a net label on its wiring got that label's name.
- `check_dfm`: a design-for-manufacture check of the saved PcbDoc against a
  fabricator's capability limits. The limits come from a profile: Altimade's published
  limits (Altium's ordering service, page updated 2026-10-13) built in and used when
  nothing else is chosen, said so in the result's `scope`; a `fab_profile.json` in the
  project folder or another `<name>.json` there named by `profile` (template:
  `examples/fab_profile.json`, a blank to fill in with your fab's numbers); or
  `profile="none"` with only the `limits` argument. The file, the argument and the
  echoed profile share one vocabulary: lengths end in `_mm` or `_mil`; via, PTH and
  NPTH drills, rings and hole-to-hole distances fall back to a generic limit, a hole
  wall to copper (`min_hole_to_copper`) to `min_spacing` and a hole to the board edge
  (`min_hole_to_edge`) to `min_copper_to_edge`; null removes one. Every value is checked
  by the server, from a file or the argument: numbers only (true/false and text are
  refused, not read as numbers), and a length outside what any fab offers (a spacing
  above 1 mm or below 0.5 mil, say) is refused, with the other unit suggested only when
  that reading is plausible. A bad file or limit answers `invalid_profile` or
  `invalid_limits` with what to fix and never switches to another profile; an unknown
  profile lists the built-ins and the files found.
  Checks: trace width; copper spacing between nets on each signal layer (pads per layer,
  vias per layer with removed pads read as the barrel, poured copper, regions and fills;
  net ties, no-net copper and shorts an enabled ShortCircuit rule allows are left out,
  pad gaps inside one footprint are their own class), counted in places where two nets'
  copper comes too close and grouped into routed and area copper; hole walls (non-plated
  holes, plated holes without a pad) to copper; hole sizes per drill row, bare holes and
  routed slots included; annular rings of vias and plated pads per layer; aspect ratios;
  hole to hole, slots as capsules, with overlapping and coincident holes reported
  whatever the profile; copper and holes to the board outline and cutouts, with
  edge-connector fingers inside the clearance as a warning (they need the fab's
  edge-connector option) and edge-plated holes and copper off the board as info; hole
  walls and holes near the edge judged against the `min_spacing` or
  `min_copper_to_edge` stand-in are warnings unless the drill cuts the copper or the
  edge; layer
  count; copper weight (`copper_weight`); board size; drill count; and board rules looser
  than the profile, which Altium's DRC would let through (`rules_vs_profile`). Each check
  gives its status, severity, limit and worst value: fail for a miss of severity error
  or warning, marginal, advisory when its only misses are info (Altimade's service-only
  limits for layer counts, board size and copper weight while Altimade is only the
  default, and the drill count), pass, or not_checked with the reason. Findings are
  classes (a width, a family of gaps, a drill row or via type) with counts, the worst
  value and its range, and one example per place; a miss within 2% of a limit (at most
  1 mil for a length) is a warning, and mm/mil rounding is named only where it explains
  the miss. `board_minimums` says what the board needs from any fabricator, whatever the
  profile, and `drills` is the drill table. Solder mask, silkscreen, acute angles, copper
  text, plane pullback and panels are listed as not checked. The review is
  computed once per board, profile and units, then paged to fit the result size.
  On Altium's examples MiniPC's drill table matches its Draftsman drill table (back
  drills aside, all 16 bare holes included), and the review takes about 2.2 s after the
  2.5 s index (a repeat call or another page 1 ms); its fabrication checks give 18
  findings (6 of them spacing). On a private set of 55 real boards it ran without an
  exception, at most 0.22 s per board after the index (median 0.05 s), with every page
  under the 60,000-character limit
- `check_dfm` checks assembly (DFA) too, for the active variant (its Not Fitted parts
  are left out): `part_counts` (placements and unique parts against `max_placements`
  and `max_unique_parts`, Altimade's published 600 and 200, which are advice while
  Altimade is only the default; unique parts are the BOM's lines with a netlist, else
  distinct footprint and Comment, said to be approximate), `assembly_sides` (SMD parts
  on both sides and through-hole parts on the bottom: extra process steps, info),
  `part_spacing` (same-side neighbours by the nearest of their 3D body outlines and pad
  copper, the pads' extent for a part without a body, each part's nearest fitted
  neighbour, against `min_part_spacing`; overlapping parts are a warning, or advice
  without the limit; a part the assembler does not place, such as a fan model or a
  mounting hole, over others is its own info class), `part_to_edge` (pad copper or body
  to the outline and cutouts against `min_part_to_edge`; pads crossing the edge, bodies
  past it and card-edge connectors are their own info classes, parts off the board a
  note), `fiducials` (global fiducials per assembled side and two near each fine-pitch
  part, against `min_global_fiducials` and `local_fiducial_pitch`, else the typical 3
  and 0.5 mm as info), `tombstoning` (small passives with one pad connected directly to
  a pour, region or fill and the other not are a warning; with no area copper at either
  pad, one pad on a much wider track is an imbalance to review) and `fine_pitch`
  (against `min_pitch`, the pitch measured between pins: a pin drawn as several pads is
  one; Altimade publishes no number, so parts at 0.5 mm or finer are advice). The new
  profile keys are in the template; an assembly limit that is not set is measured, not
  judged. `board_minimums` adds placements per side, unique parts, fiducials per side,
  the finest pitch and the smallest part gap and part-to-edge distance; courtyards,
  component heights, pick-and-place, paste and through-hole pins against the other
  side's parts are listed as not checked. On MiniPC the whole review takes
  about 2.5 s after the index. On a private set of 55 real boards it ran without an
  exception, at most 0.3 s per board after the index, every page under the limit; 7
  boards have a small passive with one pad directly connected, 16 have overlapping
  bodies and 3 have fiducials
- `check_dfm` checks the pick-and-place data (`pick_and_place`, no limit needed). It
  computes each part's Center-X/Y the way Altium's pick-and-place file does: the
  midpoint of the box of its pad centres over every pad (SMD and through-hole, any
  layer; pad size plays no part), counted from the board's relative origin. On a private
  set, 40 of Altium's own files (for 25 boards) agree on 2571 of 2571 rows of unchanged
  parts within 0.0001 mm (the copper extent's centre would miss 304 of them by up to
  1.4 mm), and their 12 rows for parts without pads sit at the absolute origin (2 of
  them on boards whose relative origin is set). Findings: fitted parts without pads,
  whose Center-X/Y Altium writes at the absolute origin (a warning for a part in the BOM
  or a fiducial, info for Mechanical, Graphical and No-BOM parts); asymmetric parts
  whose Center-X/Y is 0.5 mm (SMD or mixed) or 2 mm (through-hole) from their 3D body's
  centre (the pad copper's without a body), such as edge connectors, with the fix
  (Ref-X/Y with the reference point at the body centre, or the assembler's correction);
  a relative origin that is not set, outside the outline or more than 0.1 mm from its
  lower-left corner (info, with Gerber Setup's 'Reference to relative origin' for
  Gerbers to line up with the pick-and-place file and ODB++); and, not a miss, parts
  whose Ref-X/Y is more than
  0.5 mm from Center-X/Y (mostly through-hole parts with a pin-1 origin). Cases Altium's
  files did not settle (bare-hole pads, pads on both sides, rotations off the 90-degree
  grid) are counted per board where they move a centre. Rotations against the
  assembler's zero orientation stay listed as not checked. On the private set of 83
  boards it ran without an exception in about 0.05 s per board at most after the index:
  82 boards are advisory and none fails; 53 have an asymmetric SMD part, 39 an origin
  away from the lower-left corner (24 never set), 13 have No-BOM parts without pads
- `get_assembly_guide`: an assembly sequence for prototype builds of a variant (the
  active one by default), from the saved PcbDoc. The SMD of both sides comes first, the
  side whose tallest SMD part is shorter first (else the one with fewer SMD parts, else
  the bottom), so the side with the taller, heavier parts is soldered last; then the
  through-hole parts (top, then bottom), whose leads would keep the board from lying
  flat; on each side short to tall by 3D-body height (a chip passive without a body
  counts as at most its EIA width; parts with no height go last, smallest pad extent
  first). Mixed parts (SMD pads and plated pins) go with the SMD of their side. The
  result says why (`first_side_reason`, `sequence_rules`). Steps are BOM lines as
  `get_bom` groups them (MPN first) with a netlist, else footprint and Comment, marked
  approximate; each has its quantity, Comment, footprint, MPN and height, and per part
  the designator, Center-X/Y as Altium's pick-and-place file writes it and Ref-X/Y from
  the board origin, rotation and, for polarized parts (diodes and LEDs, polarized
  capacitors, batteries, connectors, ICs and any part with three or more pad names), a
  pin-1 hint from the pad named 1, A1, K or + ('pin 1 top-left', 'cathode (K) left',
  seen from the side being assembled, the bottom flipped left to right; a depopulated
  A1 ball is placed at the grid's corner), else 'no pin 1 marked'. Diodes and polarized
  capacitors whose pads are only numbered get 'pin 1 ...', which does not say where the
  cathode or + is: they are counted and a note says to check the marking against the
  silkscreen. Left out and listed: the variant's Not Fitted parts (a Not Fitted part is
  never listed among the parts to fit by hand), fiducials, Graphical, Net Tie and No-BOM
  parts, mounting holes, parts without pads and, with a netlist, PCB parts not in the
  BOM and BOM parts not on the board. Without the schematic parts, a variant's Not
  Fitted entries stored with no designator (only a UniqueId) cannot be applied: a note
  gives their count. `format` markdown is a printable checklist with Done and
  Skipped columns (the first page opens with the sequence, the last lists what was left
  out); csv has one part per row. `side`, `units` and row paging under the result size.
  No board map is drawn. On the private set of 83 boards it ran without an exception in
  at most 0.03 s per board after the index (2 pages at most, the largest 32 kB): 97.6%
  of 2778 steps have a 3D-body height, and 2557 of 2568 polarized parts (99.6%) get a
  pin-1 hint; the first side was decided by part height on 66 boards, 12 have SMD on
  one side only. With the sheets' parts (56 projects), 7 projects have BOM parts not on
  the board and 1 has board parts not in the BOM. On those 56 projects without a
  netlist, 298 of 1070 polarized parts (on 32 projects) are diodes or polarized
  capacitors with only numbered pads, and 1 variant has Not Fitted entries known only
  by UniqueId
- `pcb_manufacturing_review`: a guided manufacturing-readiness review of the saved PCB.
  Claude asks which fabricator and assembler (their numbers or a `fab_profile.json`),
  confirms the profile, checks fabrication (`check_dfm`, vias, health, connectivity,
  Altium's DRC report with its assessment) then assembly (`check_dfm`'s DFA checks, the
  BOM), and ends in a go/no-go that lists what was not checked; it never presents
  Altimade's limits as another fab's
- The PcbDoc parser reads the parts' 3D bodies (outline, overall and standoff height,
  side and model type; never the model's name)
- The PcbDoc parser reads whether a pad's hole is plated, a square or slot hole's
  angle, whether the hole is offset from the pad, pads that are bare holes (no copper,
  such as mounting holes; MiniPC has 16 that Altium's drill table lists) and the number
  of embedded board arrays (panels)
- `check_pcb_health`: Altium's PCB Health Check Monitor run offline on the saved PcbDoc,
  with Altium's category names, ids and levels (High error, Medium warning, Low info) so
  a saved Batch DRC report's health section compares one to one (`altium_report`).
  Checks: Shelved/Modified Polygons (shelved, or COPPERINVALIDATE), Duplicate Component
  Designators, Zero Area Polygons and Regions, Self-Intersecting Regions, Micro-Segments
  (Board, Copper; Graphics/Mechanical on request, as Altium runs it only in debug mode):
  region edges under 0.03 mil, Slots Hole Size > Length, Broken and Unused xSignals,
  360deg Component, Reference Point Outside the Component Area, Components on
  Non-signal Layers, Invalid PCB Design Rules (empty or repeated names); plus parts
  outside the board shape, duplicate copper tracks and vias, and board-outline
  micro-segments. Checks turned off in the board's saved Health Check settings are
  skipped; mirrored footprints, panels, fonts and From-Tos are listed as not checked.
  Counts follow Altium's report: one row per object, per short edge for micro-segments
  and per crossing point for self-intersecting regions.
  On real boards it matched every saved report's health section (reference points
  outside the component on 11 of 11 boards). The PcbDoc parser now also reads region
  shapes (ShapeBasedRegions6, Regions6), fills, board regions, pour and outline arcs,
  COPPERINVALIDATE, the component's layer, pad hole shapes and slot lengths, and the
  board's Health Check settings
- PCB design rules, classes and differential pairs, read from the `.PcbDoc` (`Rules6`,
  `Classes6`, `SignalClasses`, `DifferentialPairs6`, and xSignals from Board6). New tools:
  - `get_design_rules` — every rule's kind, priority, enabled flag and scope; per kind
    or name the values: widths, gaps, clearances with the object-to-object matrix,
    via sizes, lengths, per-layer values for the board's real signal layers, rooms.
    Flags `stale_references`: scope names that match no net, class, pair or room
  - `get_rules_for_net` — which rule of each kind applies to a net, pair or xSignal,
    resolved by priority like Altium. Scopes are evaluated with an Altium query-language
    subset that decides every scope seen on 61 real boards; rules that apply only to some
    of the net's objects (vias, a layer, a room, a component's pads, its pour copper) are
    listed with that condition instead of being claimed or dropped. Clearance-type
    rules list candidates with both orientations and their connective checking.
    Component and pad scopes use the PCB designator (decoded from `Texts6`), which
    differs from the schematic designator on multi-channel and board-annotated designs.
    For a diff-pair net, the note on Width points to the DiffPairsRouting rule that
    checks its track width
  - `get_classes` — net, component, diff-pair, pad, polygon, xSignal, layer, from-to and
    design-channel classes and their members, with unresolved members and DNP flags
  - `list_diff_pairs` — pairs with nets, classes, length per leg (pad-to-pad path plus
    via barrels; routed copper stays as `routed_length_mil`), skew and the routing rule
    for each pair
  - `get_net_pcb` adds the net's classes and pairs; `get_net_neighbors` marks the
    diff-pair partner; `get_board_info` adds rule, room, class and pair counts
- Rule-compliance, length and diff-pair checks on the saved copper. Rule scopes are
  decided per object (IsVia/IsTrack/IsPad, OnLayer and layer classes,
  WithinRoom/TouchesRoom, InComponent/HasPad/InPadClass, pad Name) and for xSignals
  (InxSignal, InxSignalClass, InAnyxSignal, IsxSignal). Lengths are measured like
  Altium's Signal Length: the copper path from pad centre to pad centre with true arc
  lengths, plus the via barrel over the layers it traverses, plus the pin/package length
  stored on the end pads; nets without exactly two pads use routed copper plus barrels,
  and xSignals are followed through series parts. New tools:
  - `check_rule_compliance` — track widths per layer (diff-pair nets against their
    DiffPairsRouting widths), via diameter and hole, via hole size and annular ring,
    routing layers and Length windows, with the rule resolved for each object: room,
    layer, via and pad scopes are decided per object. Optional approximate clearance
    spot-check. Says whether the board's own batch DRC checks each kind
  - `check_lengths` — Length and MatchedLengths rules evaluated like Altium: windows,
    matched-length groups (xSignals, pairs or nets) against the longest member or the
    target source, within-pair skew; stale scopes and conditional members reported
  - `get_signal_length` — one net, pair, xSignal or pin-to-pin path
  - `check_diff_pairs` — undeclared P/N pairs, polarity swaps at pins (with a hint when
    the interface tolerates inversion), series parts and AC coupling symmetry (value,
    footprint, DNP), in-pair skew against within-pair rules
  - What a check does not cover (pad holes and rings, uncoupled length, poured copper)
    is listed in its result
- `constraints_review`: a 4-phase audit of the board's high-speed interfaces, found from
  their parts and pins. Targets come from each chip's layout guide (typical values only
  as a labelled fallback); pairs, classes, impedance-driven rules, matched-length groups,
  clearances, reference layers and stale rules are checked against them, then
  the saved copper is spot-checked. Altium's last DRC report is quoted only with its
  assessment
- Impedance profiles from the Layer Stack Manager: `get_board_info` lists each profile's
  target and tolerance, its solved trace width/gap, calculated impedance and reference
  layers per signal layer, a derived propagation delay, the rules that use it, and issues
  (calculated impedance outside tolerance, profile-driven rule widths that drifted from the
  profile, unused profiles). Profile-driven rules name their profile in their values
- `check_sch_pcb_sync`: an offline Project > Show Differences. It compares the compiled
  schematic netlist with the saved PcbDoc and reports parts missing from or extra on the
  PCB, designator, footprint and comment differences, pads whose net differs from their
  pin's, renamed nets, nets on one side only, and schematic pins whose placed footprint
  has no pad (Update PCB stops with 'Unknown Pin'). Components link by unique ID first
  (channel copies by their full sheet-symbol path from the .Annotation file, else by
  their non-auto-named nets) and by designator second; nets match by shared members
  first and by name second, with the project's Comparator settings. Mechanical and
  graphical parts, variant alternates, copied footprints, removed channels, net ties,
  jumpers and single-pin nets are handled as Altium does, and every finding names its
  Altium difference type. Sheets saved after the PcbDoc are listed as the likely cause;
  sheets saved after the netlist was compiled are reported as `netlist_stale`, so a stale
  netlist is not mistaken for an out-of-date board. To feed it, the Protel netlist's part
  list (footprint, comment) is now read, the sheets give each part's unique IDs,
  footprint models, kind and pin designators, and the PcbDoc gives the full source unique
  ID, the component kind (Jumper included, stored as ComponentKindVersion3) and the
  comment text (from WideStrings6, so µ and Ω survive)
- `get_drc_report`: reads Altium's last Batch DRC report ('Design Rule Check - <PcbDoc>'
  .html or .drc in the project's output folder) and explains it. Violations are grouped by
  rule, component or net and linked to the board (components and nets through 'Pad
  <designator>-<pad>', the copper each violation touches on the layers it names, pours
  for shorts, differential pairs, and the board location), with PCB health issues, waived
  violations and the report's own warnings. An assessment comes first: the report is
  incomplete (an empty HTML gives way to the .drc of the same run), the DRC stopped at its
  violation limit (later rules, shorts included, read 0), Un-Routed Net or Short-Circuit
  was not checked, the report is older than the board or an Update PCB ran since, it was
  made for another board or found outside the output folder, or the rules changed since.
  The PcbDoc's Design Rule Checker settings (violation limit, batch and online rule kinds,
  report and health options) and the PrjPcb's output and log folders are now read. ERC,
  OutJob outputs and the DRC XML report are not read
- Bill of materials per variant:
  - `get_bom` lists lines (quantity, designators, Comment, description, value,
    footprint, manufacturer, manufacturer part number) built from the compiled parts, so
    compile-masked parts stay out, without Graphical, Net Tie and Standard (No BOM)
    parts and without the variant's Not Fitted parts, with its parameter variations
    applied (they follow their variation's part) and an alternate part's footprint used.
    Variants bind to parts by UniqueId as Altium does, so a stale designator in the
    project file does not pick the wrong part. The MPN comes from part parameters (the
    spellings libraries use for 'Manufacturer Part Number 1', paired with its
    manufacturer), never from Comment; 'N/A' and the like are shown as placeholders, not
    part numbers. Special strings such as '=Value' are resolved against the part's
    parameters, then the project's. Lines group by MPN, else Comment and footprint, and
    list the values a line's parts disagree on; variants can be compared and the lines
    returned as CSV (formula-like cells kept as text)
  - `check_bom` runs offline hygiene checks: duplicate designators, unresolved special
    strings, fitted parts marked DNP in their text, parts without a footprint, one MPN
    with different values/descriptions/footprints, MPNs without a manufacturer, parts
    without an MPN (where the project keeps MPNs in parameters), placeholder
    descriptions, stale variant entries, consolidation candidates among passives, and
    sheet parts outside the compiled design. ActiveBOM and supply data (lifecycle,
    stock, price) are not read
  - The sheets now keep every parameter and the library reference, and the project
    file's variants are read in full (alternate parts, parameter variations,
    CurrentVariant)
- `check_decoupling`: offline decoupling coverage. From the compiled netlist and pin names
  it finds the rails (nets named like power, or reaching an IC supply, reference or
  capacitor pin; never a net carrying EN, PG or SENSE), the power pins of ICs fitted in
  the active variant, and the fitted capacitors to ground (two-pin, arrays and
  feed-throughs); a 0 ohm link joins two rails. Per rail it reports no capacitor to
  ground, only Not Fitted ones, a rail joined through a ferrite or resistor to
  capacitors with none of its own (warnings), and only capacitors to other nets
  (bootstrap, another rail, a 0 ohm link), no capacitor under 1 µF, unreadable values
  and, when asked, no bulk capacitor (info). With the PcbDoc it lists each IC rail's
  nearest capacitor (and nearest under 1 µF) with its distance, straight line, pad edge
  to pad edge, warns above 5 mm, and reports power pads far from every capacitor and an
  opposite-side capacitor with no via or pour found at its pad; naming an IC lists its
  5 nearest capacitors. Rules and thresholds were calibrated on real boards (85% of the
  IC rails measured have a capacitor within 5 mm); regions and planes are not read, so
  it is advice, not DRC. Filters by IC and rail; any part can be checked as an IC
- `check_vias`: via inventory and fabrication review. Via types by span (first and last
  layer in stack order: through, blind, buried, microvia per IPC-2226A: one dielectric,
  at most 0.25 mm deep, and 1:1 or a hole up to 0.15 mm), hole as drawn and pad
  diameter, with counts per type and per net class, copper-to-copper depth and aspect
  ratio from the stack heights, the smallest annular ring over the layers that keep a
  pad, tenting, vias in SMD pads,
  stacked microvias and the stack's drill pairs. Back drilling is recomputed the way
  Altium writes the drill files: the Layer Stack Manager's back drills (drilled from the
  outer layer, stopping before the other one) apply to vias in a Max Via Stub Length
  (Back Drilling) rule's scope that have no connection on the layers they remove; each
  back drill lists its drilled layers, depth to the stop layer and drill sizes (hole
  plus the oversize). Findings: aspect ratio above 10:1 (info above 8:1, and for a
  mechanically drilled blind via above 1:1; microvias above 1:1, info above 0.8:1),
  annular ring below the Minimum Annular Ring rule or the requested minimum (info below
  0.1 mm, 0.05 mm on microvias), sizes outside the Routing Via Style rule (info for
  holes of 1 mm and up drawn as vias), blind or buried spans without a drill pair, vias
  in SMD pads (fill and cap advice, one note per thermal via array; warning for another
  net's pad or a ViasUnderSMD rule forbidding it), untented or one-side tented vias and
  tented holes above 0.5 mm, stubs above a back-drill rule's maximum (the residual after
  back drilling is taken as the worst case, the drill tip stopping just past the first
  drilled layer), back-drill rules without back drills, and stubs over 40 mil with their
  quarter-wave resonance on differential pairs no back-drill rule covers. On Altium's
  MiniPC example the recomputed back drill selects the same 16 vias whose pads Altium
  removed on the drilled layers
- `check_connectivity`: copper connectivity of the saved board, like Altium's Un-Routed
  Net and Net Antennae rules, offline. Per net, whether its copper joins all its pads:
  tracks, true arcs, vias, pads and any same-net copper that touches, a hatched pour's
  tracks and arcs, the regions a solid pour was last poured into (thermal spokes
  included), copper regions, fills, and internal planes assigned to the net (split
  planes by their outline, Power Plane Connect No Connect honoured; plane connections
  are labelled assumed). A Jumper component's pads on one net count as joined, as in
  Altium. The summary counts nets routed, partly routed and unrouted with percentages
  and connections complete (Altium's routing completion formula, N pads needing N - 1);
  single-pad nets and nets without pads are counted apart. Findings:
  - `unrouted`: each missing connection between the nearest pads of two copper
    islands with its distance, the islands' closest copper approach when within
    25 mil (`copper_gap_mil`, also listed as `near_misses` beyond the spanning tree),
    `same_component` for two pads of one part, and the net's shelved pours and pours
    with no saved copper
  - `isolated_copper`: copper of a net that reaches none of its pads; dead pour islands
    as info
  - `antenna`: an open-ended track or arc (once per primitive, also in isolated
    copper), or one ending on a via that connects nothing else, with the stub length
    back to the first branch and the open track's own length against the Net Antennae
    rule that applies, when one is enabled
  - `weak_connection`: copper that only touches where a net needs it, which Altium's
    'Check for incomplete connections' rejects (centrelines that do not meet, a track
    centreline off the pad, via or pour copper, a via centre off the pad), listed when
    `checks` names `weak` or the board's Un-Routed Net rule has that option on (see
    Changed; the summary always counts them); `nets_joined_only_by_weak_connections`
    would read un-routed with that option on
  Nets connected only through assumed copper are listed in `assumed_copper_nets`.
  Filters by net or pair, net class, layer and check; paginated, with long connection
  lists shortened to fit. `altium_drc` sets Altium's last Batch DRC report beside it:
  its Un-Routed Net rows (missing connections apart from dead copper and plane rows)
  and Net Antennae counts, the nets each side lists as not routed, the board's Net
  Antennae rules, and the issues that limit trust in the report. On Altium's 6 examples
  every net is routed (without regions and fills 2,572 connections would read
  missing). On the 18 of 83 real boards whose own DRC report sits beside them, none
  reports an un-routed net and neither does this check (so un-routed violations
  themselves are not yet compared on real data); the 2 Net Antennae violations on one
  board are the 2 antennae found here, inside the report's violation boxes, and vias
  that connect on one layer only are not reported, as those reports do not
- The PcbDoc parser reads `Regions6` (copper regions on signal layers: a solid pour's
  poured copper, free and footprint regions, with holes; keepouts, polygon cutouts and
  board cutouts left out) and `Fills6` (copper fills on signal layers) into
  `PcbData.areas`; regions and fills on internal plane layers are voids in the plane and
  are not read as copper. A stream with records that cannot be read (a record past the
  end, vertices that are not board coordinates) warns and does not count as read. Also
  read: the internal plane net assignments (Board6 `PLANEnNETNAME`) and each pour's
  `POLYGONTYPE` (split planes). A shelved pour's regions are dropped like its tracks.
  A pour's layer is now resolved from Altium's own tokens (`TOP`, `MIDn`, `BOTTOM`,
  `PLANEn`, so split planes land on their plane layer) before the board's layer names,
  which can no longer shadow them. Other tools do not use the regions yet
- The PcbDoc parser reads each via's tenting (with a manual solder mask expansion that
  covers the ring counted as tented), test-point flags, pad stack mode, per-layer pad
  diameters and removed pads, and the stack's drill pairs and back drills (Board6
  LAYERPAIRn)
- Design notes: Claude now reads what designers wrote into the project besides the
  circuit. New tool `get_design_notes` (paginated, or one document by name) returns:
  - project parameters from the `.PrjPcb`
  - per schematic sheet: notes (author, collapsed state), text frames, free text labels,
    pictures (embedded or linked, size, position, nearby caption text), directives /
    parameter sets with their parameters, title-block parameters and the sheet template.
    Text drawn inside component symbols or the template is left out. Each note lists the
    components drawn next to it (`near R4, U1`) as a proximity hint, measured at
    Altium's real font size with rotated labels turned about their anchor
  - per Draftsman document (`.PCBDwf`, now read from the `.PrjPcb` document list):
    notes, free text, tables, view titles and sides (fabrication views name the layer
    they show), pictures, and the drill table and layer stack notes cached from the
    board (with several stacks, each note names its stack). A drawing last updated from a `.PcbDoc` that is not
    this project's is flagged as possibly out of date, or as a copied project or earlier
    revision when a board of the same name sits in another folder. Large drawings are
    parsed without the board copy they embed (~0.7 s for a 6.8 MB drawing)
  - text as the sheet shows it: Altium's escapes are decoded (`~1` a line break, `~2` a
    `|`, `~~` a `~`), and special strings are resolved: label and text-frame formulas
    (`=PrjMPN + '-v' + Version`) and title-block values that point at project
    parameters (`Revision = =SCH_Revision`) against the sheet's parameters, then the
    project's, then `ProjectName`/`DocumentName` (a `*` placeholder counts as not set;
    values Altium computes when printing, such as dates, stay as typed). In Draftsman
    notes, text, tables and view titles, `=Name` takes the value the drawing cached
    (e.g. `Final thickness of PCB: =Total_Thickness`) and `=ViewScale` the view's scale
    (e.g. `2:1`)
  - not included: review comments made in Altium 365, which live in the cloud workspace,
    not in the project files
- `get_sheet_context` shows the sheet's notes above its components (first page)
- New tool `get_design_image` returns a schematic or Draftsman picture as an image
  Claude can look at (schematic bitmaps are re-encoded as PNG, no new dependency).
  Pictures over 8000 px per side are refused (sizes are read from PNG, GIF, JPEG and
  WebP headers), and a linked TIFF or metafile is reported as an unsupported format
- `set_project_dir` reports `project_parameters` and `draftsman_documents`
- `package_for_xfn` snapshots include the design notes in a new `notes` table: project
  parameters; per sheet the notes, text frames, labels, pictures, directives, title-block
  parameters and template; per Draftsman document its source board, parameters, notes,
  text, tables, pictures, view titles, drill table and layer stack notes. One row per item
  with `document_kind`, `document`, `sheet_id`, `page`, `kind`, `title`, `text`, `author`,
  `near_refdes` (JSON) and kind-specific `data` (JSON). Pictures are stored as metadata
  only (name, size, position, index), never their image data. Paths are reduced to file
  names (template, linked pictures, the drawing's source board), so a shared snapshot
  carries no local folders or user names. A document whose notes cannot be read gets one
  `unreadable` row with the reason and a warning, instead of failing the export. The
  snapshot keeps `SCHEMA_VERSION` 1: the `notes` table is new and no existing table
  changed, so every released pcb-copilot (which accepts only schema 1 and reads only the
  tables it knows) still opens it
- `package_for_xfn` warns when the snapshot is missing components from failed sheets
- `set_project_dir` / `refresh_netlist` responses include `component_count`, and
  `refdes_without_metadata` (with a sample of up to 10) when netlist refdes are missing
  from every parsed sheet
- A designator that names two or more different components on one sheet is reported
  instead of only logged. Only the last component's metadata is loaded, so the others
  silently had none. `set_project_dir` and `refresh_netlist` add `designator_clashes`
  (sheet, refdes and each component's `lib_references`, the first 20),
  `designator_clash_count` and a hint, and a `refresh_netlist` cache hit names them; the
  server instructions tell Claude to pass them on. A compile-masked copy is not a clash
  (see Fixed). `parse_sch_doc` and `components_from_stream` take an optional `clashes`
  list to fill
- `get_board_info` adds `refdes_mapping_unmatched_sample` (up to 10 board designators
  with no netlist part) and `refdes_differs_from_logical` (how many parts the board
  renames)
- Altium Designer 17 and older, which run as `DXP.exe` (e.g. 14.3), are now detected
  alongside `X2.exe` (AD 18+). Their title bar (`Altium Designer (14.3) - <sheet path> -
  <project>.PrjPcb. Licensed to …`) gives the project, active sheet and project path. The
  project path is found in the sheet's folder or a parent folder
  ([ee-in-a-box/altium-copilot#5](https://github.com/ee-in-a-box/altium-copilot/issues/5)).
  The title layout has not been checked on a live DXP install yet
- Netlist-free load: with Altium Designer closed and no up-to-date cached netlist (e.g. a
  fresh copy of an Altium 365 project, whose `.gitignore` leaves out `Project Outputs*/`),
  `set_project_dir` now loads the project from its files instead of answering
  `altium_not_running`. The response says `partial: true`, `load_mode: netlist_free`,
  why, and lists `netlist_required_tools`. Board, rule, length, differential-pair, via,
  DRC report, design-notes, picture and variant tools work, as does `constraints_review`
  (its prompt says how to find the interfaces without schematic parts). `query_net`,
  `get_component`, `search_components`, `get_sheet_context`, `get_bom`, `check_bom`,
  `check_decoupling`, `check_sch_pcb_sync`, `schematic_review` and `package_for_xfn`
  answer `netlist_required`: open the project in Altium once, then load it again. PCB
  parts are not linked to schematic parts (`sch_refdes` is empty, `get_board_info` says
  so instead of counting every part as unmatched), and DNP comes from the designators
  stored in the variants. A cached netlist older than the sheets is not used
  (`netlist_stale`). With Altium closed, a `.NET` that is newer than every source but
  not the one the registry recorded (Altium regenerated it on its own, the folder moved,
  a new registry) now loads fully instead of partially, and is recorded; with several
  `.NET` files under the folder only the one in the project's output folder is trusted.
  After a partial
  load, `refresh_netlist` loads the project again, fully once Altium has it open; it
  keeps the active variant (or says it is gone in `variant_note`), and when the full
  load fails (e.g. Altium is busy) the partial load is restored (`partial_load_kept`)
- Altium 365 clone status, offline. When the project folder is in a Git clone whose
  origin is `https://<workspace>.365.altium.com/git/<project GUID>.git`, every
  `set_project_dir` response carries an `altium365` block, and the new read-only tool
  `get_clone_status` reads it again: workspace and project GUID, the last fetch
  (`FETCH_HEAD` time, else the remote-tracking branch's reflog), commits ahead of and
  behind the remote-tracking branch as of that fetch, uncommitted changes by kind with a
  sample (design files first; files in new folders are listed one by one, so a new
  folder of sheets counts) and the last commit's date and message. Warnings: last
  fetch older than 30 days or unknown, behind, ahead, no remote branch to compare with,
  and uncommitted changes to the design files the tools read (`.PrjPcb`, `.SchDoc`,
  `.PcbDoc`, `.PCBDwf`, `.Annotation`); other local files such as `.PrjPcbStructure` are
  counted without a warning. Nothing is fetched and no credential is used: every `git`
  call runs with `--no-optional-locks` (the index is not rewritten),
  `protocol.allow=never` (no transport, so no lazy fetch either) and no prompts, and
  starts nothing the configuration names: `core.fsmonitor` off, filter drivers blanked
  for `git status` (a clean filter such as git-lfs could otherwise run and write to
  `.git`; passed as `GIT_CONFIG_KEY_n`, so a driver name with `=` is blanked too),
  submodules ignored, no signature checks in `git log`. A filtered file that only looks
  changed (its stat data moved, so its raw content is compared with the stored pointer)
  is counted as `unverified`, not as a change. A git that hangs past
  10 s is stopped with everything it started. A missing `git` or a git error is
  reported, never fails a load; whether the folder is an Altium 365 clone then comes
  from `.git/config`, so other repositories get no `altium365` block. Credentials in the
  remote URL are never echoed. On 55 local Altium 365 clones it
  recognises all 55 and warns about 50 fetched more than 30 days ago and 2 with an
  uncommitted `.PcbDoc`, in about 0.3 s per clone

### Changed
- `check_bom`: with a BomDoc in the project, BOMDOC_CHECKS run too (`checks_run` lists
  them; without one the output is as before): lifecycle_risk (NRND, EOL, last-time-buy or
  Obsolete on the chosen solution: warning; on an alternate only: info), single_source
  (lines with one manufacturer: one info finding), no_solution (no part choice and no MPN
  parameter: warning; one info when under half the lines have part choices),
  out_of_stock_at_qty (no supplier of the chosen solution had the production need in
  stock: warning with the cache date), stale_cache (part choices cached over 365 days
  ago: info), bomdoc_out_of_sync (parts without a line, lines without parts with the
  reason, and lines saved for another revision of the parts' Workspace item: info; a
  chosen MPN that differs from the parts' MPN parameter: warning), and
  mpn_without_manufacturer and mpn_inconsistent over every ranked MPN (an MPN the
  parameter check already reports is not repeated: its BomDoc lines are added to that
  finding). A part whose line has a part choice counts as having an MPN for missing_mpn,
  and missing_mpn no longer lists parts a no_solution warning names. out_of_stock_at_qty
  counts only suppliers' offers and is info when no stock figure has a cache date; a
  lifecycle_risk carries the cache date of the part choices that report it. A finding
  about one line gives its line number and design_item_id (an unnumbered line is named
  by its Design Item ID). BomDoc checks asked for on a project without a BomDoc are listed
  in checks_not_run instead of reported as run. The header names the BomDoc and its cache
  dates; a BomDoc that cannot be read, or that breaks the checks, leaves check_bom as
  without it (bom_doc_error).
- `get_bom`: active_bom=true adds each line's BomDoc line numbers and chosen solutions
  (and the CSV columns BomDoc Line # and BomDoc MPN); without it the output is as before.
- `verify_outputs`: a BOM generated from an ActiveBOM document is checked against that
  BomDoc instead of leaving its part numbers unverified: Manufacturer Part Number against
  the line's chosen solution (warning; on a line without part choices against the parts'
  MPN parameter, field_mpn_parameter, info, as ActiveBOM does not always fall back to
  it), Manufacturer against it and its aliases (info), Supplier and Supplier Part Number
  against the chosen solution's part choices and Line # against the BomDoc's line number
  (warnings; Line # info when the BomDoc gives a number to several lines). A BomDoc that
  cannot be read keeps the old info, and the file row's bom_doc_not_read says why.
- `package_for_xfn`: new tables bom_solutions (per line, solution and part choice:
  manufacturer, MPN, supplier, SKU, stock, unit price at the production need with its
  price break, below_minimum_order and extended_price, lifecycle and cache date, for the
  active variant) and bom_non_pcb_lines; the other tables are unchanged.
- Knock-on effects on projects with a BomDoc: `check_release_readiness`'s outputs_current
  gate reads verify_outputs, so an ActiveBOM BOM file that differs from its BomDoc can
  turn it from pass to warn; in `run_review_checklist`, REL-OUT-02 can move from yes to
  needs_judgment for the same reason, BOM-DAT-01 counts a no_solution warning as a line
  without a part number (and a no_solution info as part numbers kept elsewhere), and
  BOM-DAT-02 sees the BomDoc's mpn_inconsistent findings. The review_bom hint for
  BOM-DAT-01 now says ActiveBOM part choices count.
- Placement scopes are decided on components: `pcb_rules` evaluates a component object
  (kind "component": IsComponent, IsSMTComponent and IsThruComponent by its holes,
  WithinRoom / TouchesRoom on its extent, Name = its designator; IsBGA and the like stay
  undecided), for the Height rule. The review checklist's PCB-PLC-07 (connectors, holes and
  tall parts against the enclosure) is now assisted by check_mechanical_fit; it leans Yes
  only when a real height limit applies to the parts (summary.parts_with_height_limit).
- Helpers made public for the mechanical check: `dfm_assembly.outlines_overlap`,
  `polygon_area`, `points_box`, `rotate_points` and `boundary_distance`;
  `fab_drawing.note_lines` / `NoteLine`, `LENGTH`, `MAX_WORDS` and `length_mm`;
  `interfaces.text_exposure` (the text part of a connector's exposure);
  `dfm.stack_total_thickness` (with substack= for one layer stack of a rigid-flex
  board); `dfm_geometry.nearest_true_cores`; the new `testpoints.probe_sides` and
  `libraries.footprint_body_heights`. `decode_region_records` can read only the records
  of given layers, BoardRegions records carry their layer stack id and bending line
  count (`PcbRegion.layer_stack_id`, `bending_lines`), and stack layers the layer stacks
  that hold them (`PcbLayer.substacks`, from V9_STACK_LAYERn_{id}CONTEXT = 0).
- `check_release_readiness`: the dnp_parts and outputs_current gates cite verify_outputs'
  result of the generated files (`ReleaseInputs.verify_outputs`). dnp_parts warns when the
  files themselves list Not Fitted parts as placed; outputs_current warns when their
  contents differ from the design, and names the kinds of files that match it (nothing is
  claimed about files it cannot compare, such as Gerbers). Both stay non-blocking and keep
  their titles: the verdict is unchanged. The release_review prompt sends those gates to
  verify_outputs.
- Shared helpers made public for verify_outputs: `via_checks.layer_token_v6` (the Board6
  layer tokens, used by its own drill-pair resolution), `bom.BomPart.parameters` (the part's
  parameters as the BOM sees them), `bom.norm_name` and the part-number name patterns,
  `output_jobs.output_locations` and `output_jobs.utc_iso`. Stage A readers:
  `split_designators` keeps a designator with a space ('U1 A') whole between commas,
  Excellon tool definitions carry their written precision (`DrillTool.diameter_step_mil`),
  and `PickPlace.source` is documented as what Altium writes there: the output file's own
  path, not the PcbDoc's.
- Compiled components carry `pin_names` (every named pin, connected or not, united across
  the sheets a part is placed on), so unconnected pins have names. Helpers reused by
  `get_firmware_pin_map` are public: `pull_resistors.link_group` and
  `push_pull_drivers`, `interfaces.build_context`, `choke_far`, `bus_transceiver`,
  `PROCESSOR_TEXT` and `DEBUG_HEADER_WORDS`, `testpoints.TP_DESIGNATOR`
  and `TP_FOOTPRINT`, and the new `testpoints.marked_testpoint_nets`
- The netlist-free (partial) load is now the fallback for a project whose sheets cannot be
  compiled offline (`compile_error`) or that lists none; `netlist_required` and the
  partial-load message say so. Load and refresh responses name `netlist_source` (`altium`
  or `offline_compiler`)
- `check_vias` is described as the via inventory and drill review, no longer a "fab
  review", in its description, the server instructions and the README; questions about
  a fabricator's capability ("can my fab make this?") go to `check_dfm`
- The session-start instructions read the design notes (`get_design_notes`) before
  summarising the board, and `schematic_review` treats notes as intent to verify. The
  server instructions fit within the 4096 characters that Claude Code shows; the
  design-notes caveats live in the tool descriptions
- CONTRIBUTING asks for a `SCHEMA_VERSION` bump only when an existing snapshot table or
  column changes or changes meaning; a new table needs none
- `get_design_rules` and `get_rules_for_net` name the values of Back Drilling rules
  (max_stub_length_mil, oversize_mil, tolerances, use_top_back_drill,
  use_bottom_back_drill), ViasUnderSMD (allowed), LayerPairs (enforce) and the stacked
  microvia flag of Hole To Hole Clearance (allow_stacked_microvias); they were under
  other / other_mil. Only Back Drilling is among get_rules_for_net's default kinds
- `check_rule_compliance` checks a via's Minimum Annular Ring on each signal layer of its
  span that keeps a pad, like `check_vias`: a top-middle-bottom or full-stack via whose
  pad is smaller on some layer than its nominal diameter is measured there, and the
  violation names that `layer` and its `pad_diameter_mil`. Before, the ring came from
  the nominal diameter, so the two tools could disagree. Simple-stack vias keep their
  ring; no ring changes on 83 real boards (all simple stack) or on Altium's examples
  (MiniPC's 34 full-stack vias have nominal-size pads where they keep one)
- `check_connectivity`'s `weak_connection` also tests a pad or via that touches a
  pour's copper (solid or hatched, or the outline standing in for unread copper), as
  Altium's 'Check for incomplete connections' does: when it is connected directly
  (Polygon Connect Style), its centre must be inside the pour's outline, arc edges
  included, within 0.5 mil. Relief connections are not tested, as in Altium's help,
  and neither is a pad or via whose style is not known. The style is that of the first
  enabled rule by priority whose first scope matches the pad or via and second the
  pour. Layer, net, IsPolygon, name and polygon class terms are decided on the pour, a
  room or region term only when the pour lies clear of it. Altium's
  help does not say whether the scopes may also match the other way
  round, so every rule that may come first counts, and the pad or via is tested only
  when they all say Direct. On Altium's 6 examples, 12 of 28,988 pad and via contacts
  with solid pour copper have their centre off the outline: 7 direct vias and 5
  relief-connected edge-connector fingers (not tested). One Mini PC via is a connection
  its net needs, so it is reported. On 83 real boards, 81 of 35,429 contacts are off
  the outline (78 direct). 24 SMD pads on 4 boards are needed connections, each with
  its centre 5 to 100 mil past the outline and at most half of the pad inside it. The
  test was left out when `check_connectivity` was added, because pour outlines then
  ignored their arcs
- `check_connectivity` reads the Un-Routed Net rule's 'Check for incomplete
  connections' option (saved as `CHECKBADCONNECTIONS=TRUE`). `rules.UnRoutedNet` gives
  each rule's `check_incomplete_connections`: true, false, or null when the rule does
  not save it, which means off. Altium's help gives no default, and boards save this
  key only when it is on, while other rule options are saved as FALSE too. Per net,
  the option is that of the Un-Routed Net rule that applies: the first enabled one by
  priority whose scope matches the net. When a scope is not decided for a net, every
  rule that may come first counts, and the option is null when they differ. Where it
  is or may be on, `weak_connection` findings are listed by default. Where it is on,
  they are warnings (`incomplete_connections_checked` on each finding), and the nets
  routed here only through weak connections read un-routed in Altium. Those nets are
  listed in `nets_unrouted_in_altium` and flagged `unrouted_in_altium` in `nets`. The
  summary gives `incomplete_connections_checked`,
  `nets_routed_here_unrouted_in_altium` and `incomplete_connections_note`.
  `altium_drc` counts those nets as not routed here, and the connections they join
  only through weak connections as missing
  (`nets_not_routed_here_only_by_weak_connections`,
  `missing_connections_here_only_by_weak_connections`). Net status and routing
  counts still follow touching copper. `pcb_manufacturing_review` treats a net in
  `nets_unrouted_in_altium` as a no-go, like an unrouted net. Altium's 6 examples do
  not save the option, so nothing changes there. On 83 real boards, 4 of 93 Un-Routed
  Net rules have it on, all enabled with scope All, one per board. 3 of those boards
  have no weak connection. The fourth has 17 (9 track to track, 7 track to pad, 1
  track to via; 7 miss by 0.5 to 1 mil, where Altium's undocumented tolerance
  decides). Altium would read 15 connections on 10 nets routed here as missing because
  of them. None of the 4 has a DRC report to compare
- Rule scopes that name polygon pours are decided: each pour's name is read from its
  Polygons6 `NAME`, which Altium saves as UTF-16 character codes ("71,78,68" is GND),
  and matched without regard to case, as other names are. On a pour itself (the pour
  side of a Polygon Connect Style rule) `IsNamedPolygon`, `InNamedPolygon`, `Name =`,
  `<>` and `Like` (`=` and `<>` compare the whole name, so a match that needs `*` or
  `?` as a wildcard stays unknown) and `InPolygonClass` (class members are pour names;
  All Polygons holds every pour) were unknown. On a hatched pour's tracks and arcs
  `InNamedPolygon` was, per object (`check_rule_compliance`) and for a net's pour
  copper in binary rules (`get_rules_for_net`). They stay unknown for a pour without
  `NAME` and for a name with a character beyond Latin-1, which scope text (read as
  Latin-1) cannot hold. Pours may share a name. `get_net_pcb` lists each pour's
  `name`. On 83 real boards and Altium's 6 examples, all 1,266 pours but one have a
  name, all ASCII; 2 of 193 Polygon Connect Style rules use `IsNamedPolygon`, naming no
  pour on their board, so 223 pad and via contacts there move from no known style to
  Direct (no `weak_connection` finding changes, their centres being inside the
  outline). The 141 contacts still without a style come from rules that match only
  the other way round. `check_rule_compliance` and the rule resolution behind
  `get_rules_for_net` (every net, every kind) give the same results as before on all
  89 boards

### Fixed
- `check_connectivity`: a Power Plane Connect Style rule that sets its style only per
  object kind (VIA.PLANECONNECTSTYLE, PAD.PLANECONNECTSTYLE) was read as no style, so a
  via or pad its No Connect kept off a plane still joined the plane's net. The kind's own
  style is now read first, then the plain one.
- The rail voltage read from a net name (`pull_resistors.rail_voltage`, shared by
  `check_pull_resistors`, `get_power_tree`, `run_erc`'s rail advisories,
  `get_firmware_pin_map` and `check_derating`) read the digit before the V of a P-form
  name with a trailing V: 1P8V and P1P8V gave 8 V, VDD_1P35V 35 V and 3P3V 3 V (and
  1,8V 8 V). They now give 1.8 V, 1.35 V and 3.3 V. A VDD or VCC with two digits reads
  as volts and tenths only for the usual rails (08, 09, 10, 11, 12, 15, 18, 25, 28, 33):
  VCC36 or VCC20 no longer reads as 3.6 V or 2.0 V. Altium's examples hold no such
  names; their outputs are unchanged
- `pull_resistors.resistance` read a simulation expression as a resistance ('(12-2.7)/1m'
  as 1 Mohm, '2/10m' as 2 ohm); an arithmetic value is no resistance now. On Altium's
  'Quick start' example (simulation values) this changes no tool's output
- `get_design_hierarchy` read an off-sheet connector (a RECORD=17 with
  IsCrossSheetConnector=T) as a power port; it is no longer listed among the power ports
  or joined by name with them
- Layer-specific keepout tracks and arcs on signal layers (Tracks6/Arcs6 flags2 = 2)
  were read as no-net copper: `get_board_info` counted them as tracks and arcs, and
  `check_rule_compliance` listed them among the no-net copper it skips. They are now
  skipped like keepout regions and fills; objects on the Keep-Out layer are unchanged
- `check_vias` sizes a back drill as the via hole plus the Max Via Stub Length (Back
  Drilling) rule's oversize, matching the drill sizes Altium wrote for the Mini PC
  example. It added the oversize twice, following Altium's help, which calls the
  oversize radial and gives hole + 2 x oversize. On Mini PC (0.2 mm holes, 0.3 mm
  oversize, saved by Altium Designer 22) the drill table cached in MiniPC_Drill.PCBDwf
  lists the 16 back drills at 0.5 mm, and the board's own hole list (the Board6
  `HASHKEY` entry marked `[Backdrill]`) holds the same 0.5 mm; `check_vias` gave
  0.8 mm. `drill_diameters_mil` now matches them
- Pour outlines follow their arc edges (Polygons6 `KINDn=1` with `CXn`, `CYn`, `Rn`,
  `SAn`, `EAn`). They were read as straight lines between the vertices, so a point
  near a rounded pour edge was outside the pour. Each arc now becomes chords at most
  0.25 mil inside it, as arc copper does, run counter-clockwise from start to end angle
  and drawn from the end at its vertex, as KiCad's Altium importer does (which also
  draws an arc of under 0.2 degrees straight). This applies to the pour outline and
  its zero-width outline rows, so containment and outline distance follow the arcs
  in `get_net_neighbors` (pours), `query_pcb_region`, `check_vias` (a pour around a
  via), `get_drc_report` (a violation's polygon net), `check_decoupling` (an
  opposite-side capacitor pad in a pour) and `check_connectivity` (pour outlines
  standing in for unread copper, split planes). A pour's `bbox_mil` in `get_net_pcb`
  and where `check_pcb_health` places a pour finding include the arcs too. Bluetooth
  Sentinel's round GND pours have 3 to 5 vertices and arcs of up to 315 degrees.
  Read straight, 13,468 of their 20,340 poured track and arc ends lay more than
  0.3 mil outside them; now none do. On 83 real boards, 116 pours have arc edges
- The board outline's points follow its arc edges (Board6 `KINDn=1`) too: they were
  its vertices only, so an arc bulging past them was cut off. `get_board_info` gave
  Altium's Bluetooth Sentinel example `board_extents_mil` of 3149.6 x 196.9 mil, the
  strip its vertices span; with its two 315-degree arcs the board is 3149.6 x 2165.4 mil.
  The arcs become chords at most 0.25 mil inside them, as pour outlines do
  (`check_dfm` and `check_pcb_health` already read the arcs themselves). `get_drc_report`
  measures a violation's zoom box against the outline with its arcs: adding the copper's
  extents, as it did, missed an arc reaching past the copper. On Altium's other examples
  the extents do not change; on 83 real boards, 51 have outline arcs and one board's
  extents change. An outline or pour arc whose angles are finite but too large to subtract
  (a corrupt record) is a straight edge, as other bad arc values are; in a pour it raised
  an error that dropped every pour ("Polygons6 parse failed")
- `check_pcb_health` read a Regions6 or BoardRegions record whose vertices are not
  finite coordinates, or whose hole list runs past the record, as a region. It now
  counts it as unread, as `check_connectivity` already did: both decode Regions6 and
  Fills6 with one record reader. No region changes on Altium's examples or 83 real boards
- `get_drc_report`: a report where several tables list a different number of rows
  than their counts ("3 tables list ...") was called incomplete instead of having counts
  that differ
- The length engine (`get_signal_length`, `check_lengths`, `list_diff_pairs`) grew a
  rectangular pad by a track's half width or a via's radius with square corners, so
  copper just off a pad's corner (up to 0.41 times that reach) counted as touching it.
  The corners are now round. On Altium's examples 2 of 1,276 net lengths change and on
  83 real boards 1 of 5,729, each by under 1 mil
- `requirements.txt` pins `mcp<2`: mcp 2.x removed `mcp.server.fastmcp`, so a fresh
  install failed to start the server. `httpx`, which the update check imports, is now a
  declared dependency instead of arriving through mcp
- Projects that keep their `.SchDoc` files in a subfolder (e.g. `source\`) or outside
  the project folder (`..\common\`) now load components. Sheets come from the `.PrjPcb`
  document list instead of a top-level `*.SchDoc` glob, for both enrichment and netlist
  freshness ([ee-in-a-box/altium-copilot#6](https://github.com/ee-in-a-box/altium-copilot/issues/6))
- Device sheets are read as project sheets. The `.PrjPcb` lists them under
  `[DeviceSheetN]` (a managed one as `Managed\Sheets\<GUID>\Name.SchDoc`), not
  `[DocumentN]`, so their parts got no schematic metadata, saving one did not make the
  netlist stale, and `get_sheet_context` and design notes did not know the sheet. On
  Altium's Bluetooth_Sentinel example (10 device sheets) 72 of 72 schematic parts now
  load (was 15). A listed device sheet missing on disk is reported in `sheet_errors`,
  like a missing project sheet; a sheet listed both ways is read once. Projects without
  device sheets are unchanged
- Schematic enrichment reads sheets that write record keys in upper case (`OWNERINDEX`,
  `TEXT`, `NAME`, `UNIQUEID`, ...), as most of Altium's AD25 examples do. `parse_sch_doc`
  matched only the mixed-case spelling, so Bluetooth Sentinel, Mini PC (Motherboard,
  WiFi) and SpiritLevel-SL1 loaded no components and `set_project_dir` reported
  `no_components_loaded`; PC4-SODIMM loaded 16 of 199. Keys and component parameter
  names (`VALUE`, `COMMENT`) are now matched case-insensitively, a record's trailing NUL
  no longer ends up in its last value, and a non-numeric `OwnerIndex` is skipped instead
  of failing the sheet. On the 84 example sheets 1537 of 1581 parts load (was 98); the
  other 44 are parts of multi-part components, merged by the next fix
- Parts of a multi-part component that each have their own UniqueId, as on most of
  Altium's AD25 examples, are merged by designator. `parse_sch_doc` merged parts only
  when they shared a UniqueId and kept one entry per designator, so the last part placed
  replaced the others: 44 parts of 37 components on the examples, 399 pin names among
  them. Parts with the same designator, different part ids and the same part count now
  merge (pins united, first non-empty description and value, best MPN), so all 1581
  example parts load. The component keeps the UniqueId of its highest part, the one the
  PcbDoc's `SOURCEUNIQUEID` names (29 of 29 on the examples; the last part placed
  matched 28). Parts placed from different library references are merged and logged.
  Two different components with one designator on a sheet are reported as a designator
  clash and, as before, the later one is kept
- Compile-masked parts are recognised (inside a sheet's compile mask, RECORD=211). A
  masked copy that shares a live part's designator, on the same sheet or another, no
  longer replaces the live part's parameters, description and kind, and is not reported
  as a designator clash; a live part replaces a masked one, and a variant entry on the
  masked copy no longer applies to it. A collapsed (disabled) mask masks nothing. On
  real projects the masked copy's data had replaced 25 live parts' (wrong MPN, Comment
  and description in their BOM lines), and every designator clash found involved a
  masked copy
- Schematic metadata (MPN, description, value, pin names, sheet) of board-annotated and
  multi-channel designs follows the project's `.Annotation` file. The netlist names
  physical designators and the sheets logical ones, so the netlist's U1 took the metadata
  of the sheet's U1, another part, and renamed parts got none. Each sheet part is now
  matched by sheet and UniqueId to its physical designators, one per channel; the same
  sheet designator on two sheets no longer drops one of the parts. On Altium's
  Bluetooth_Sentinel example with every sheet read, 72 of 72 parts carry their own
  metadata (before: 14, with 42 carrying another part's). Parts the file does not list
  keep their sheet designator unless an annotated part has that name. Projects without
  an `.Annotation` file are unchanged
  - `get_component`, `search_components`, `get_sheet_context`, `package_for_xfn` and
    stale-designator DNP resolution name board designators. `get_component` adds
    `logical_refdes` when the sheet's designator differs and accepts a sheet designator
    that no board part has; `get_sheet_context` shows it as `U3 (U1 on sheet)`
  - `set_project_dir` and `refresh_netlist` report `renamed_by_annotation`, and
    `annotation_errors` for a listed `.Annotation` file that cannot be read
  - Saving the `.Annotation` file makes the cached netlist stale
- A sheet part with no nets no longer takes, by channel-suffix matching, the netlist name
  of another part (TP1 claiming TP1V8): exact designators are matched first. Unconnected
  channel copies are now found through the compiled part list, so they get their
  sheet's metadata
- A sheet that fails to parse no longer discards every other sheet's components; the
  failure is returned as `sheet_errors` (sheet, path, error)
- Nets loaded with zero components now report `no_components_loaded` from
  `set_project_dir`, `refresh_netlist`, `search_components`, `get_component`,
  `get_sheet_context` and `package_for_xfn` instead of "Pattern matched all 0 components"
- Saving a sheet in a subfolder now makes `refresh_netlist` regenerate, and
  `set_project_dir` no longer serves a cached netlist older than the sheets or `.PrjPcb`
  (offline load returns `altium_not_running` with `netlist_stale: true`)
- A failed regeneration (e.g. Altium closed) restores the previous `.NET` instead of
  leaving the project without a netlist
- Protel netlist members whose pin designator holds a '-' (a battery's '-' pin, written
  `BT1--`) are split at the rightmost '-' that leaves a known part, so the pin no longer
  becomes a phantom part `BT1-` with an empty pin. The .NET is read as UTF-8, falling
  back to the Windows code page instead of replacing characters such as µ
- Netlist generation only activates a window owned by Altium. Before, the first visible
  window with "Altium Designer" in its title got the keystrokes, which could be a
  browser tab
- A variant's Not Fitted entry (Kind=1) that still carries an AlternatePart (Altium leaves
  some behind, e.g. 'NP') now makes the part DNP; it was read as fitted
- DNP flags (`get_component`, `list_variants`, sheet context, PCB tools, sync check,
  export) bind a variant's Not Fitted entries by UniqueId, then the stored designator,
  as Altium and `get_bom` do: a stale designator in the project file named the wrong part
- `refresh_netlist` re-reads the project's variants, so Not Fitted parts, parameter
  variations and variants saved since the project was loaded apply; the active variant
  stays by name, and the response says so when it was renamed or deleted
- Stale-designator DNP UniqueIds that could not be resolved at load are kept and
  resolved by the next `refresh_netlist`
- `get_net_neighbors` and `query_pcb_region` no longer measure polygon-pour outlines as
  copper. A pour's outline (the boundary drawn in `Polygons6`, kept as zero-width edges)
  was matched like a track: on Altium's PC4-SODIMM example DQ09 read GND at 1.13 mil
  with an 81.6 mil parallel run, where the nearest GND copper is 4.92 mil away, and 22 of
  the 25 neighbours listed for GND were at 0.0 mil (now 3.94 mil at the closest, against
  a 3.937 mil clearance rule). Copper distances come from tracks, arcs, vias and pads
  only; a hatched pour's tracks are real copper and still count. `get_net_neighbors`
  adds `pours`: other nets' pours on the net's layers, by outline, with `inside_pour`
  when the net's copper lies within the outline and `min_outline_distance_mil` when the
  outline comes within the search distance. The net's own outlines are not measured.
  `query_pcb_region` lists outlines only under `pours`. Solid pour copper is still not
  parsed. The clearance spot-check, DRC violation nets and `check_decoupling` already
  left outlines out
- PCB tools name components by the designator printed on the board (decoded from
  `Texts6`), not the PcbDoc's `SOURCEDESIGNATOR`, which is the schematic sheet's logical
  name and differs on multi-channel and board-annotated designs (57 of 73 parts on
  Altium's Bluetooth_Sentinel example, 8 of 199 on PC4-SODIMM). `get_component_placement`,
  `query_pcb_region` and the `get_net_pcb` pad list report it as `refdes`.
  `get_component_placement` adds `logical_refdes` and still accepts the logical name when
  no designator or netlist refdes matches, returning every channel instance. PCB-only
  parts (mounting holes, logos) now have a `refdes` instead of `null`
- `sch_refdes` (the netlist name) matches the PCB designator first, then the schematic
  UniqueId, then `SOURCEDESIGNATOR`, and a fallback never takes a netlist part that
  another board designator names. Before, a board-annotated board's U3 (sheet U1) was
  mapped to the netlist's U1, a different part: 64 wrong mappings on the AD25 examples.
  Netlist refdes without schematic metadata now match too
- Rounded-rectangle pads keep their corners in every PCB distance (clearance
  spot-check, `query_pcb_region`, `get_net_neighbors`, `check_decoupling`, DRC
  violation nets, pad-to-pad length). Altium stores them as round pads with the real
  shape and corner radius in the pad's size-and-shape subrecord; they were modelled as
  capsules, which cut their corners away. On Altium's MiniPC example U9's 3V3 pad is
  33.2 mil from C69, pad edge to pad edge (was 37.0), and on its SODIMM board a 1%
  corner pad that read 7.6 mil from a track is 3.97 mil from it (the clearance rule is
  3.937); on real boards pad gaps shrank by up to 14.6 mil. Corner radii were checked
  against the ROUNDEDRECT apertures of Altium's Gerber output. Pads with a
  top-middle-bottom or full stack on the bottom or an inner layer take that layer's
  size and shape, and an empty slot falls back to a filled one: three bottom-layer
  pads with an empty top slot on the Bluetooth Sentinel example were dropped
- Through-hole pads with a top-middle-bottom or full pad stack keep their size and
  shape per layer in every PCB distance (clearance spot-check, `query_pcb_region`,
  `get_net_neighbors`, DRC violation nets, pad-to-pad length): two objects are measured
  on the layers they share, each pad with its copper there. They had the top-layer size
  on every layer. On 83 real boards (aggregate) 3,255 through-hole pads have other
  copper on the inner layers: 21 gaps to inner-layer tracks grew (by up to 12.4 mil),
  450 pad pairs with larger inner copper came within 20 mil (the closest 9.8 mil), and
  spot checks of 395 nets lost 6 THPad-Track clearance violations that only the
  top-layer size caused, with none new; the length of the 1,447 nets with such pads is
  unchanged. `query_pcb_region` lists only the layers a pad's copper reaches, and a
  clearance violation names the layer its gap is on. Altium's examples have no such
  pads and measure the same
- Stack layers get their layer ids from the stack's LAYERID, not from the layer name.
  Boards keep stale names in old layer slots (e.g. internal planes once named like a
  signal layer), and the name lookup put those signal layers on the plane ids: on
  Altium's MiniPC example 8 of the 16 copper layers, so their tracks fell outside the
  stack for lengths, barrels and rule scopes
- Shelved polygon pours are read (`SHELVED` in `Polygons6`). Altium hides a shelved pour
  from DRC and connectivity, so it is no longer treated as copper: `get_net_neighbors`,
  `query_pcb_region`, `check_decoupling` ("a pour around it"), `check_vias` (a same-net
  pour around a via connects it on that layer), DRC violation nets, rule scopes on a
  net's pour copper and the length tools' "pours may complete the connection" note leave
  it out, and its outline gets no proximity rows. Copper that
  Altium keeps in the file for a shelved pour is dropped
  (`skipped_records.shelved_pour_copper`). `get_net_pcb` still lists the pour with
  `shelved: true`, and `get_board_info` counts `shelved_pours`. On 2 of 83 real boards
  every pour is shelved (12 in all); for sampled nets there, `get_net_neighbors` listed
  292 pour entries, all from pours with no copper
- A hatched or outline-only pour's tracks and arcs are read as the pour's copper: the
  `Tracks6` / `Arcs6` polygon index (KiCad's `polygon` field; 0xFFFE marks board-outline
  primitives, not a pour). Altium saves them without a net, so they were netless routing:
  invisible to the distance tools and counted as unchecked no-net copper. They now take
  the pour's net. `get_net_neighbors` and `query_pcb_region` measure them (counted in
  `pour_copper_segments` with no parallel run, kind `pour`). The clearance spot-check
  matches them as the matrix's `Poly` object, and Width checks them, with `pour_copper`
  on violations: a Width rule on Altium's SL1 example is scoped `Not InPolygon`, though
  Altium's docs do not say. They are not routing, so routed length, the `get_net_pcb`
  layers, the length graph, DiffPairsRouting and RoutingLayers leave them out;
  `get_board_info` counts them as `pour_copper`. Rule scopes are decided per object: a
  pour's copper is InPolygon, but IsPolygon, IsNamedPolygon and InPolygonClass match only
  the polygon object, as in Altium. On Altium's Bluetooth Sentinel example (ten
  Horizontal GND pours: 6,473 tracks, 675 arcs) GND copper is within 10 mil of all 40 of
  the 40 longest signal nets (was 30), 3.94 mil at the closest: the pour's arcs around
  vias, at the board's 3.937 mil clearance. The 83 real boards have no hatched pours
- Copper distances to arcs are exact. Arcs are still split into chords (at most 0.25 mil
  sagitta) for the spatial index and for length, but each chord row now carries its
  arc's centre and radius (`Primitive.arc_x` / `arc_y` / `arc_radius`, same `prims`
  columns), and distances measure the arc between the chord's ends, grown by half the
  width, to points, tracks, pads, vias and other arcs, with bounding boxes that include
  the arc's bulge. Chords lie inside their arc, so copper on an arc's inner side read up
  to 0.25 mil too close and on its outer side up to 0.25 mil too far. This applies to
  `get_net_neighbors`, `query_pcb_region`, the clearance spot-check, DRC violation nets
  and `check_vias` connections. The clearance spot-check drops its
  `arc_chord_marginal_pairs` allowance: on Altium's Bluetooth Sentinel example the GND
  pour's arcs around vias read 3.937 mil, exactly the clearance (were 3.69), and the 10
  longest signal nets report no violations. On the MiniPC example the bends of the SFP
  differential pairs read 5.91 mil between legs on all four pairs (were 5.72 to 5.89);
  the other public examples are unchanged. On the 83 real boards no gap within 10 mil
  of a net's arc changes, and neither does the clearance spot-check of their arc nets

## [0.3.0] - 2026-07-19

### Added
- PCB understanding: parse the project's `.PcbDoc` directly (no export step) into a
  spatial index. New tools:
  - `get_board_info` — stackup (thickness, dielectric, material), origin, extents, counts
  - `get_net_pcb` — where a net routes: layers, lengths, vias, pad endpoints; exact net
    names always resolve to full detail, even when they also match other nets by pattern
  - `get_net_neighbors` — crosstalk proximity: nearby nets, edge-to-edge distance,
    parallel run length, broadside overlaps on adjacent copper layers
  - `query_pcb_region` — what's near a board coordinate, including polygon pour
    containment
  - `get_component_placement` — placement, pins, nearest components; resolves
    multi-channel refdes (`U3` vs `U3A`/`U3B`) via UniqueId + pad-net signature
  - All four net/region/placement tools annotate results with the active variant's
    DNP status; `get_net_neighbors`/`query_pcb_region` reject non-copper layer names
    (e.g. silkscreen) with the valid copper layer list
- PCB data auto-refreshes on `.PcbDoc` save (mtime-based, no manual refresh)
- `get_board_info` and polygon-returning tools note that pour outlines reflect the
  last-saved nominal geometry, not live copper — repour and save in Altium before
  relying on polygon results for voids, cutouts, or clearances

### Fixed
- `install.ps1` now verifies Claude Code MCP registration persisted and prints recovery
  steps when a running session clobbers it
- Inner copper layers (`Mid1`–`Mid30`) referenced by their Altium positional alias in
  polygon pour data now resolve correctly — previously these pours were dropped from
  crosstalk/proximity results entirely
- `page_netlist.py` high-fanout threshold now matches its documented "more than 25
  pins" contract (was flagging exactly-25-pin nets)

---

## [0.2.4] - 2026-06-09

### Fixed
- Variant DNP detection now correctly resolves components whose designator entry in the
  `.PrjPcb` variant table is stale (e.g. `R?` — set before annotation ran). The parser
  captures the stable `UniqueId` for any stale entry and defers resolution until after the
  schematics are parsed; the `UniqueId → refdes` map built from `.SchDoc` files is then
  applied to produce the correct DNP list. Previously, such components were silently omitted
  from the DNP set in all queries and exports.

---

## [0.2.3] - 2026-05-08

### Added
- `set_project_dir` now loads from disk cache when the netlist is unchanged — no Altium required.
  The `.NET` file mtime is stored in the project registry after every successful load or refresh;
  on subsequent calls the mtime is compared and, if it matches, the project loads instantly
  without touching Altium. Falls back to the full Altium flow when the cache is absent or stale.
- `schematic_review` prompt now suggests parallel agent dispatch for large multi-sheet projects,
  including the exact MCP init sequence each agent needs to self-initialize independently.

### Fixed
- `get_sheet_context` `has_more` flag now emits lowercase `true`/`false` instead of Python's
  `True`/`False` so it is consistent with JSON convention and easier to parse in agent prompts.
- Registry entries with a stale camelCase `lastUsed` key (written by older versions) are now
  silently cleaned up on the next load — no migration script required.
- `refresh_netlist` now updates the registry `netlist_mtime` after regeneration so the cache
  remains valid across sessions after a schematic save.

---

## [0.2.2] - 2026-05-07

### Changed
- `query_net` now accepts regex patterns for discovery (e.g. `UART` finds all UART nets) and
  falls back to exact case-insensitive match when the pattern is invalid regex or finds nothing —
  so PCB net names like `+5V`, `+3V3`, and `VIN[0]` resolve correctly without escaping
- `search_nets` removed — `query_net` covers all its use cases with the same or smaller token cost
- `get_sheet_context` output switched from JSON to compact text format (reduces token usage ~60%)
  with automatic pagination: if `has_more: True`, call again with `offset=<next_offset>`
- Added `maxResultSizeChars` MCP annotations to `query_net` (60K) and `get_sheet_context` (200K)

### Fixed
- `get_sheet_context` with a negative or out-of-bounds `offset` is now handled gracefully
  instead of silently returning wrong data or looping

---

## [0.2.1] - 2026-05-03

### Fixed
- Netlist generation now retries on focus acquisition timeout and mid-sequence focus loss (another window stealing focus) instead of surfacing an error immediately — up to 3 attempts before giving up
- PowerShell focus poll reduced from 10 s to 5 s so transient failures retry faster; worst-case total wait drops from 30 s to 15 s

---

## [0.2.0] - 2026-05-03

### Added
- `package_for_xfn` tool — exports the loaded project to a portable SQLite `.db` snapshot that cross-functional team members (firmware, mechanical, test, reliability) can query with [pcb-copilot](https://github.com/ee-in-a-box/pcb-copilot) without needing Altium
- SQLite export schema: `project`, `sheets`, `variants`, `components`, `nets`, `pins` tables with indexed lookups; `nets.pin_count` for fanout queries
- Registry now stamps `last_exported_xfn` timestamp on every successful `package_for_xfn` call
- `CONTRIBUTING.md`: DB schema versioning guidelines — bump `SCHEMA_VERSION` in `export.py` and `SUPPORTED_SCHEMA_VERSION` in pcb-copilot on any schema change

### Removed
- `--update` self-update command — the one-line `install.ps1` installer handles updates; in-chat update notifications are retained

### Fixed
- Registry writes are now atomic (write to `.tmp` then `os.replace`) — no corruption on crash or power loss
- Connection leak in SQLite export when an exception occurred mid-write; now always closes the connection in `finally`
- Swallowed `OSError` in export rollback path — exceptions now propagate correctly
- Duplicate `HIGH_FANOUT_THRESHOLD` definition removed (was defined in both `export.py` and `main.py`)

### Internal
- 330 lines of new test coverage: `test_export.py`, `test_package_for_xfn.py`, registry tests for `mark_xfn_exported`
- `.gitignore`: added `*-pcb-copilot.db` (binary export snapshots) and `.worktrees/`

---

## [0.1.10] - 2026-04-23

### Internal
- Release workflow now runs lint and tests before building — a failing test blocks the release

---

## [0.1.9] - 2026-04-23

### Fixed
- Netlist foreground acquisition now uses `AttachThreadInput` so `SetForegroundWindow` reliably succeeds even when Altium is behind another window or minimized; focus poll timeout extended to 10s (was 1.5s) — no keystrokes are sent if focus is never confirmed
- Settle time after focus increased to 2s; ESC sent before menu sequence to clear any residual state; inter-keystroke delay increased to 50ms

---

## [0.1.7] - 2026-04-23

### Fixed
- Subsheet fan-out suffix matching extended: numeric-only suffixes with a separator now matched, chained segments now matched and bare numeric suffixes without a separator correctly rejected (`C111`)

---

## [0.1.6] - 2026-04-22

### Fixed
- MCP server initialization timeout on cold starts: update check now runs on a background thread so `mcp.run()` starts immediately instead of blocking on a GitHub HTTP call
- Subsheet fan-out pin enrichment: refdes suffix matching now handles all separator styles (`_`, `.`, `-`, none), any case, multi-letter suffixes, and trailing digits

---

## [0.1.5] - 2026-04-22

### Fixed
- Netlist keystroke reliability on slow machines: increased inter-keystroke delay from 1ms to 10ms, added 1s settle wait after Altium gains focus before sending the first key (prevents dropped Alt+D on slower PCs)
- Added ESC after netlist generation (with 1s delay) to dismiss any lingering Altium dialog
- Clear error when a non-schematic tab is active (homepage, Extensions and Updates, etc.) — instead of timing out with a confusing "check Altium is responsive" message, Claude now tells you to click on a schematic sheet tab

### Removed
- `save_schematic_review` tool — unnecessary overhead; ask Claude to write the review directly if you want it saved

### Internal
- Scoped pytest to `tests/` directory to prevent vendor directory crash on collection

---

## [0.1.4] - 2026-04-20

### Changed
- `brainstorm_circuits` trigger broadened — now fires on any question about topology, architecture, or how to design/improve a sub-circuit, not just when the user says "brainstorm"
- `brainstorm_circuits` docstring updated to match

---

## [0.1.3] - 2026-04-20

### Changed
- `get_sheet_context` is now the primary tool for all circuit questions, not just design reviews — server instructions guide Claude to start every question with a single bulk sheet load before falling back to `query_net` or `get_component`
- Cross-sheet tracing now uses `get_sheet_context(sheet_name=X)` instead of calling `get_component` one-by-one for each cross-sheet neighbor — significantly faster on dense, multi-sheet designs
- `get_sheet_context` docstring updated to make the cross-sheet and general Q&A use cases explicit

---

## [0.1.2] - 2026-04-20

### Changed
- Added `title` and `readOnlyHint`/`destructiveHint` annotations to all 13 tools — Claude Desktop now surfaces correct read/write intent for each tool call
- Schematic review prompt refactored to a template string — same review behavior, cleaner internals
- `manifest.json`: richer description, author GitHub URL, repository metadata, otter icon

### Added
- `assets/icon.png` — otter icon displayed in Claude Desktop Extensions UI when installed via `.mcpb`

---

## [0.1.1] - 2026-04-20

### Changed
- Removed `.mcpb` packaging — installer downloads the `.exe` directly, no bundle needed
- Simplified release workflow: dropped Node.js dependency and checksums generation

## [0.1.0] - 2026-04-19

### Added
- Initial release
- `detect_altium_project` — detects your running Altium instance and surfaces known projects
- `set_project_dir` — loads a project and builds the netlist context Claude works from
- `list_variants` / `set_active_variant` — switch between project variants; DNP components are excluded automatically
- `get_sheet_context` — reads the active schematic sheet and summarizes components on it
- `get_component` — pulls a component's description, parameters, and all connected nets by refdes
- `query_net` — traces a net across all sheets and returns every pin connected to it
- `search_components` — finds components by refdes, MPN, or description using regex
- `search_nets` — finds nets by keyword without needing exact names
- `refresh_netlist` — reloads the netlist after saving changes in Altium
- `schematic_review` — structured 3-phase design review: reads datasheets, audits against them, saves findings to a markdown report
- `brainstorm_circuits` — 5-phase design session: reads your loaded schematic, asks constraints, proposes topologies with tradeoffs
- Auto-update: server checks for new releases on startup and notifies you in chat
- One-line installer via PowerShell (`irm | iex`) — no Python required
- PyInstaller build — ships as a standalone `.exe`, no Python required
