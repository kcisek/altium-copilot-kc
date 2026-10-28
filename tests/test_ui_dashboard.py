"""The project dashboard view (roadmap #74, server/ui/project_dashboard.html): its ui://
resource and link, the self-contained page, the one tool it calls (read-only, through one
guarded helper), the messages it may send to the conversation (fixed sentences naming
read-only tools, never project data), the lessons of the earlier views it keeps, and its
data contract against get_project_dashboard's output on Altium's public examples and
synthetic results covering every state."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import types

import server.main as main
import server.services.ui_views as uv
from tests.test_project_dashboard import (
    PUBLIC, Fakes, _handle, _inputs, _load, _need, _project, _tools, isolated_main,
)

pd = main.project_dashboard
ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
VIEW = uv.view_for("get_project_dashboard")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
CALLED = {"get_project_dashboard"}
LINKED = {"get_bom", "get_board_view", "compare_variants", "project_tour", "release_review",
          "get_project_dashboard"}


def _page() -> str:
    return (UI / VIEW.file).read_text(encoding="utf-8")


def _script() -> str:
    return _page().split(uv.JS_MARKER, 1)[1]


def _contract() -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>', _page(), re.S)
    return json.loads(block.group(1))


def _rows_at(data, path: str) -> list:
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
    problems = []
    for path, shape in _contract()["shapes"].items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


def _function(name: str) -> str:
    """A function of the page's script, up to its closing brace at two spaces."""
    script = _script()
    start = re.search(rf"\n  (?:async )?function {name}\(", script).start()
    return script[start:script.index("\n  }\n", start) + 4]


# ---------- resource and link ----------

def test_the_view_is_a_listed_resource_linked_from_its_tool():
    assert VIEW.uri == "ui://altium-copilot-kc/project-dashboard.html" and VIEW.file == "project_dashboard.html"
    assert VIEW.name == "project-dashboard" and VIEW.title == "Project dashboard"
    assert VIEW.description.startswith(
        "HTML page (MCP Apps view) that hosts showing MCP Apps draw next to get_project_dashboard's "
        "result; not for reading as text.")
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    row = listed[VIEW.uri]
    assert row["mimeType"] == uv.MIME_TYPE and row["_meta"] == uv.RESOURCE_META
    result = _handle(types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri=VIEW.uri)))
    (content,) = result["contents"]
    assert content["text"] == uv.render(VIEW)
    # One tool per view; the release and the tour link none.
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)
    tools = _tools()
    for name in ("check_release_readiness", "project_tour"):
        assert "ui" not in (tools[name].get("_meta") or {}), name
        with pytest.raises(KeyError):
            uv.view_for(name)


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


# ---------- the read-only rule and the messages ----------

def test_the_page_calls_only_get_project_dashboard_through_one_guard():
    script = _script()
    allowed = re.search(r"const READ_ONLY_TOOLS = new Set\(\[(.*?)\]\);", script, re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', allowed)) == CALLED
    assert script.count("Shell.callTool(") == 1
    helper = _function("call")
    assert "Shell.callTool(" in helper
    assert "if (!READ_ONLY_TOOLS.has(tool)) return Promise.reject(" in helper
    assert "if (view.closed) return Promise.reject(" in helper
    assert "if (!canCall()) return Promise.reject(" in helper
    assert set(re.findall(r'\bcall\(\s*"([a-z_]+)"', script)) == CALLED
    # No tool that changes the server's state is named anywhere in the page.
    page = _page()
    for name in ("refresh_netlist", "set_active_variant", "set_project_dir", "package_for_xfn",
                 "generate_rule_file", "get_mechanical_handoff"):
        assert f'"{name}"' not in page and f"'{name}'" not in page
    assert "refresh_netlist" not in page and "set_active_variant" not in page


def test_messages_name_read_only_tools_only_and_carry_no_project_data():
    script = _script()
    listed = re.search(r"const MESSAGE_TOOLS = new Set\(\[(.*?)\]\);", script, re.S).group(1)
    names = set(re.findall(r'"([a-z_]+)"', listed))
    assert names == set(pd.CARD_TOOLS) | LINKED
    tools = _tools()
    for name in names:
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    # Every sentence is fixed text: tool names from MESSAGE_TOOLS only, no field of the
    # answer (project, variant, file, designator, net) is put in one.
    sentence = _function("sentence")
    assert "if (tool && !MESSAGE_TOOLS.has(tool)) return \"\";" in sentence
    assert sentence.startswith("\n  function sentence(kind, tool) {")
    assert set(re.findall(r"\$\{([^}]*)\}", sentence)) == {"tool"}
    assert not re.search(r"\b(view|card|data|row|answer)\.", sentence)
    for text in ("Show the ERC results in the ERC view (run_erc).",
                 "Show the batch DRC report in the DRC view (get_drc_report).",
                 "Show the BOM with its check_bom findings in the BOM view (get_bom).",
                 "Show the power tree view (get_power_tree).",
                 "and show its findings on the board view (get_board_view with those findings and finding_tool ",
                 "findings for this project and what to fix first (call ",
                 "Give me a guided tour of this project (project_tour).",
                 "Show the board view (get_board_view).",
                 "Compare the variants in the variant view (compare_variants).",
                 "Run the release review (release_review).",
                 "Run the checks the project dashboard did not run yet (get_project_dashboard with checks= its not_run list)."):
        assert text in sentence, text


def test_send_message_only_from_a_click_and_only_when_the_host_takes_one():
    script = _script()
    assert script.count("Shell.sendMessage(") == 1
    ask = _function("askClaude")
    assert "Shell.sendMessage(" in ask and "if (!Shell.canSendMessage())" in ask
    # askClaude is called from click handlers only.
    calls = [match.start() for match in re.finditer(r"askClaude\(", script)]
    for at in calls:
        line = script[script.rfind("\n", 0, at) + 1:script.find("\n", at)]
        assert "function askClaude(" in line or "on: { click: () => askClaude(" in line, line
    assert len(calls) == 2
    control = _function("messageControl")
    assert "if (!Shell.canSendMessage()) return askField(text, null, id);" in control
    # Pending: marked disabled, focus kept on it (aria-disabled, the same label).
    assert '"aria-disabled": pending ? "true" : null' in control and "Sending to the conversation" in control
    assert "target.focus()" in _function("redrawMessages")
    assert 'entry.state === "denied"' in control and '"Sent to the conversation"' in control
    # The sentence to copy: a read-only field labelled "Ask Claude", selected on focus.
    field = _function("askField")
    assert "readonly: true" in field and 'text: "Ask Claude"' in field and "input.select()" in field
    # Not used: links, the model context, other display modes.
    page = _page()
    for method in ("ui/open-link", "ui/update-model-context", "openLink", '"pip"'):
        assert method not in page


def test_the_page_keeps_the_lessons_of_the_earlier_views():
    script = _script()
    # Draw what it has and name the real stop reason; the closed view says so.
    for words in ("you pressed Stop", "the view was closed", "this host does not let the view run checks",
                  "no answer in ${CONTRACT.fill_timeout_s} s", "the call failed: ${error.message}",
                  "the files changed while checking", "The view was closed: it sends no more requests",
                  "the check in progress finishes on the server"):
        assert words in script, words
    # Plurals, "at least" for a capped count, keyboard focus kept, one live region.
    assert "const plural = (count, word)" in script and "at least " in script
    assert "Shell.keepFocus(" in script and "Shell.refocus(" in script and "Shell.announce(" in script
    assert "All ${view.cards.size} checks current" in script
    # One polite live region (the shell's): the blocks rebuilt on each answer are not, the
    # tables' footers are turned off, and the notices are redrawn only when they change.
    assert 'role: "status"' not in script
    assert re.findall(r'"aria-live"[^)]*', script) == ['"aria-live", "off"']
    assert script.count("Shell.table(") == script.count("quiet(Shell.table(") == 2
    notices = _function("renderNotices")
    assert "if (sig === view.noticeSig) return;" in notices
    # Every answer is labelled with the arguments it was asked under.
    assert "default call" in script and "narrowed to ${plural(args.checks.length, \"check\")} by checks=" in script
    # The fill: one call at a time with a 300 s timeout, never a failed card again, stops
    # between calls, and a newer answer ends it.
    fill = _function("runFill")
    assert 'ask({ checks: [tool] })' in fill and "if (view.answer !== answer0) return;" in fill
    assert "fill.asked.add(tool)" in fill and "if (fill.stopping)" in fill and "if (view.changed)" in fill
    queue = _function("fillQueue")
    assert '!["failed", "unreadable"].includes(card.state)' in queue
    assert "if (view.fill.asked.has(tool)) return false;" in queue
    # The overview's facts the budget left out are filled too (their answer's overview).
    assert "if (FACT_TOOLS.has(tool)) return true;" in queue
    assert "overview: answer.overview" in _function("mergeCard")
    assert _contract()["fill_timeout_s"] == 300
    start = _function("startFill")
    assert "data.args.checks !== null || view.closed" in start and "if (!canCall())" in start
    # The files changed while checking: the other cards are stale and the fill stops.
    merge = _function("mergeCard")
    assert 'stale_because: "files changed while checking"' in merge and "view.changed = true" in merge
    # The page reads no host-specific field (a standalone host serves it unchanged).
    assert "toolInfo" not in script and "userAgent" not in script
    assert "prefers-reduced-motion" in _page() or "prefers-reduced-motion" in (UI / "shell.css").read_text(encoding="utf-8")


def test_one_call_at_a_time_and_focus_kept_on_its_buttons():
    script = _script()
    # Every way to ask get_project_dashboard waits while a call is in flight.
    assert ("const busy = () => view.calls > 0 || view.pending.size > 0 || view.fill.running || "
            "Boolean(view.busy);") in script
    ask = _function("ask")
    assert "view.calls += 1;" in ask and "view.calls -= 1;" in ask and "setTimeout(startFill, 0)" in ask
    for name in ("reloadAll", "cardCall", "resumeFill"):
        assert "if (busy() || view.closed || !canCall()) return;" in _function(name), name
    start = _function("startFill")
    assert "if (view.calls || view.busy)" in start and "view.fillWanted = true;" in start
    # The buttons that call are marked disabled while busy (aria-disabled: focus stays).
    button = _function("callButton")
    assert '"aria-disabled": busy() ? "true" : null' in button and "if (!busy()) onClick();" in button
    header = _function("renderHeader")
    assert header.count('"aria-disabled": busy() ? "true" : null') == 2 and "disabled:" not in header
    pane = _function("drawPane")
    assert "disabled:" not in pane and pane.count("callButton(") == 4
    # The same tool result twice is drawn once; a result marked isError is an error.
    load = _function("loadResult")
    assert "if (sig && sig === view.resultSig && view.data) return;" in load
    assert "if (result && result.isError)" in load
    # After teardown a failed Check again is no error: the view says it was closed.
    reload = _function("reloadAll")
    assert "if (!view.closed) view.headError" in reload
    assert "The view was closed" in _function("renderNotices")


def test_stale_rows_say_so_in_words_and_gates_reveal_their_card():
    script = _script()
    # A word in the row (not a dimmed row alone, nor an off-screen span), only for a card
    # that ran on older files: a card that never ran is "Not run", not stale.
    assert "const isStale = (card) => !card.current && ran(card);" in script
    table = _function("checksTable")
    assert 'text: "stale"' in table and "sr-only" not in table
    assert 'render: (card) => (ran(card) ? clock(card.as_of) : "")' in table
    # A gate's button shows its card even when the chips or the filter hide its row.
    assert "revealCard(source)" in _function("gateRow")
    reveal = _function("revealCard")
    assert "if (!rowFilter(card))" in reveal and 'ui.table.setQuery("")' in reveal and "ui.table.focus(" in reveal
    # Full screen measures its own height (no unlimited room).
    assert "fullscreen" not in _function("tall")
    # Stop sits first in the fill line, before the text that changes.
    fill = _function("renderFillBox")
    running = fill.split("if (fill.running) {", 1)[1].split("return;", 1)[0]
    assert running.index('text: "Stop"') < running.index("Filling the checks")
    # The "Ask Claude" label wraps its field.
    field = _function("askField")
    assert 'el("label", {}, el("span", { class: "ask-label", text: "Ask Claude" }), input)' in field
    assert "aria-label" not in field


def test_the_page_draws_states_with_words_glyphs_and_borders():
    script = _script()
    contract = _contract()
    assert contract["states"] == list(pd.STATES)
    states = set(re.findall(r"^    (\w+): \{ word:", script.split("const STATE = {", 1)[1].split("};", 1)[0], re.M))
    assert states == set(pd.STATES)
    page = _page()
    for state in pd.STATES:
        assert f".badge.st-{state}" in page, state
    assert contract["groups"] == dict(pd.GROUPS)
    assert contract["cards"] == len(pd.CATALOGUE)
    # "Show more findings" asks for one card, which lists the most a card keeps.
    assert contract["more_findings"] == pd.FINDINGS_STORED == pd.NARROW_LADDER[0].findings
    assert '"No findings"' in script and '"Pass"' not in script


# ---------- the data contract ----------

def _synthetic_outputs(tmp_path) -> list[dict]:
    """Answers covering every state and every key, built through the service with fake
    tools."""
    def boom():
        raise RuntimeError("synthetic")
    release = {"verdict": "fail", "verdict_summary": "FAIL: x", "counts": {"fail": 1},
               "blocking_failed": ["netlist_fresh"], "blocking_not_verified": ["erc"], "gates_total": 3,
               "gates": [{"id": "netlist_fresh", "title": "Netlist", "status": "pass", "blocking": True,
                          "evidence": "older", "source_tool": "check_sch_pcb_sync", "ms": 0},
                         {"id": "erc", "title": "ERC", "status": "not_verified", "blocking": True,
                          "evidence": "never", "source_tool": None, "ms": 0}]}
    payloads = {
        pd.RELEASE_TOOL: release,
        "run_erc": {"summary": {"by_level": {}}, "checks_run": ["a"]},
        "get_drc_report": {"totals": {"rule_violations": 0}, "rules_checked": 3, "assessment": [
            {"issue": "violation_limit_reached", "severity": "error", "detail": "stopped"}]},
        "check_dfm": boom,
        "check_vias": {"findings": []},
        "check_bom": {"error": "netlist_required", "message": "x"},
        "check_multiboard": {"error": "no_multiboard_system", "message": "x"},
        "check_lengths": {"rules": [], "rule_count": 0},
        "check_pcb_health": {"severity_counts": {}, "checks_run": ["a"], "not_checked": ["b"]},
        "check_connectivity": {"severity_counts": {}, "summary": {"nets_checked": 4, "routed_pct": 100}},
        "check_decoupling": {"severity_counts": {}, "rails_total": 2},
        "check_thermal": {"severity_counts": {"warning": 1}, "parts_total": 2},
        "check_libraries": {"severity_counts": {"error": 1}, "parts": {"schematic": 4},
                            "findings": [{"severity": "info", "check": "x", "message": "a note",
                                          "designators": ["R1"]}]},
    }
    # A check that examined parts but could judge none of them (judged 0: partial).
    payloads["check_derating"] = {"severity_counts": {}, "summary": {
        "parts_checked": {"capacitors": 2}, "capacitors": {"applied_voltage_known": 0},
        "resistors": {"voltage_across_known": 0}}}
    fakes = Fakes(cost=0.01, payloads=payloads)
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    inputs = _inputs(project, fakes)
    later = _inputs(project, fakes, now=lambda: datetime(2026, 10, 28, 9, 7, tzinfo=timezone.utc))
    outputs = [json.loads(pd.render(pd.build(inputs, cache)))]
    # A card whose counts changed (was), in a narrowed answer, run later than the others.
    fakes.payloads["check_thermal"] = {"severity_counts": {"warning": 3}, "parts_total": 2}
    outputs.append(json.loads(pd.render(pd.build(later, cache, checks=["check_thermal"], refresh=True))))
    # A design file saved: stale cards (one of them was clean, one ran later: as_of).
    (tmp_path / "Board.PcbDoc").write_bytes(b"saved again")
    outputs.append(json.loads(pd.render(pd.build(_inputs(project, fakes, budget_s=0.0), cache))))
    # Another variant: the stale cards name the one they ran under.
    outputs.append(json.loads(pd.render(pd.build(_inputs(project, fakes, budget_s=0.0, variant="B"), cache))))
    # The netlist older than the sheets and other release counts (was).
    release["counts"] = {"fail": 2}
    older = _inputs(project, fakes, netlist_freshness=lambda: (True, "1 sheet (Power) saved after the netlist was compiled"))
    outputs.append(json.loads(pd.render(pd.build(older, cache, refresh=True))))
    # The release failing, and a call the budget stops at once.
    fakes.payloads[pd.RELEASE_TOOL] = boom
    outputs.append(json.loads(pd.render(pd.build(_inputs(project, fakes, budget_s=0.0), pd.DashboardCache()))))
    return outputs


def test_the_contract_holds_on_synthetic_answers_with_every_state(tmp_path):
    outputs = _synthetic_outputs(tmp_path)
    states = set()
    for data in outputs:
        assert _check(data) == []
        states |= {card["state"] for card in data["cards"]}
    assert states == set(pd.STATES)
    # Every key the contract names is in some answer.
    seen: dict[str, set] = {path: set() for path in _contract()["shapes"]}
    with_public = outputs + _public_outputs()
    for data in with_public:
        for path, shape in _contract()["shapes"].items():
            for row in _rows_at(data, path):
                seen[path] |= {key for key in shape["required"] + shape["optional"] if key in row}
    for path, shape in _contract()["shapes"].items():
        assert seen[path] == set(shape["required"] + shape["optional"]), (path, set(shape["required"] + shape["optional"]) - seen[path])


_PUBLIC_CACHE: list = []


def _public_outputs() -> list[dict]:
    """Answers on Altium's public examples (those installed), read once per run."""
    if _PUBLIC_CACHE:
        return _PUBLIC_CACHE[0]
    outputs = []
    for path in PUBLIC.values():
        if not path.is_file():
            continue
        with isolated_main():
            _load(path)
            outputs.append(json.loads(main.get_project_dashboard()))
            outputs.append(json.loads(main.get_project_dashboard(checks=["check_dfm", "run_erc"])))
    _PUBLIC_CACHE.append(outputs)
    return outputs


def test_the_contract_holds_on_the_public_examples():
    outputs = _public_outputs()
    if not outputs:
        pytest.skip("no public example is installed")
    for data in outputs:
        assert _check(data) == []
        for card in data["cards"]:
            assert card["state"] in pd.STATES
            assert card["tool"] in _contract()["catalogue"]


def test_the_page_holds_the_catalogue_the_answer_leaves_out():
    """Each card's title, group, view and flags are the page's: equal to the service's."""
    catalogue = _contract()["catalogue"]
    assert list(catalogue) == list(pd.CARD_TOOLS)
    for spec in pd.CATALOGUE:
        row = catalogue[spec.tool]
        assert row["title"] == spec.title and row["group"] == spec.group, spec.tool
        assert row.get("view") == spec.view, spec.tool
        assert row.get("board", False) == spec.board, spec.tool
        assert row.get("slow", False) == spec.slow, spec.tool
        assert row.get("grouped", False) == (spec.tool in pd.GROUPED_ROWS), spec.tool
    assert _contract()["fact_tools"] == list(pd.FACT_TOOLS)
    # The page puts them back on every card it reads, with the time and variant it ran under.
    full = _function("fullCard")
    assert "CATALOGUE[card.tool]" in full and "cards_as_of_utc" in full and "answer.variant" in full
    assert "fullCard(card, data)" in _function("loadAnswer") and "fullCard(row, answer)" in _function("mergeCard")


def test_the_view_reads_every_key_its_contract_names():
    script = _script()
    keys = {key for shape in _contract()["shapes"].values() for key in shape["required"] + shape["optional"]}
    # Keys the page shows through the shell or not at all (the model reads them).
    text_only = {"excluded", "notes", "budget", "limit_s", "spent_ms", "stopped_at", "next", "others",
                 "key", "latest_save_utc", "netlist_updated_utc", "in_checks_run", "by_state", "ids",
                 "slow", "gates_total", "ms", "as_of", "was", "current", "stale", "not_run", "reason"}
    for key in sorted(keys - text_only):
        assert re.search(rf'\.{key}\b|"{key}"', script), key


def test_a_narrowed_page_and_the_header_label_name_the_arguments(tmp_path):
    fakes = Fakes(cost=0.01)
    model = pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache(), checks=["check_vias"])
    assert model["args"] == {"checks": ["check_vias"], "refresh": False}
    assert "overview" not in model and set(model["release"]) >= {"state", "current"}
    script = _script()
    assert '"Show all checks"' in script and "reloadAll(false)" in script and "reloadAll(true)" in script


def test_the_public_example_answers_draw_without_contract_warnings(tmp_path):
    path = _need(PUBLIC["Sentinel"])
    with isolated_main():
        _load(path)
        data = json.loads(main.get_project_dashboard())
    assert _check(data) == []
    assert {row[0] for row in data["release"]["gates"]} >= {"drc", "erc", "netlist_fresh"}


# ---------- docs ----------

def test_the_readme_changelog_and_instructions_name_the_dashboard():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    assert "| `get_project_dashboard` |" in section
    assert "`get_project_dashboard` |" in readme.split("## Interactive views", 1)[0]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #74" in unreleased and "get_project_dashboard" in unreleased
    position = main.SERVER_INSTRUCTIONS.index("## Project Dashboard")
    assert position > 4096 and "get_project_dashboard" in main.SERVER_INSTRUCTIONS[position:position + 800]
