"""Offline BOM hygiene checks on a variant's BOM parts.

Thresholds come from real projects: a part without an MPN is a finding only where the
project keeps MPNs in part parameters (half its parts or more); values compare after
unit normalisation (100n = 100nF = 0.1uF, 4k7 = 4.7k); consolidation candidates are
passives of one value, size and rating under different MPNs.
"""

import re
from collections import Counter, defaultdict

try:
    from services.bom import BomPart, natural_key, needs_mpn
except ImportError:
    from server.services.bom import BomPart, natural_key, needs_mpn

CHECKS = (
    "duplicate_designator", "unresolved_special_string", "dnp_marker_on_fitted",
    "no_footprint", "mpn_inconsistent", "mpn_without_manufacturer", "missing_mpn",
    "placeholder_description", "stale_variant_entry", "consolidation", "not_compiled",
)
# Checks of the project's ActiveBOM document (services/active_bom.bom_doc_findings), run
# only when it has one.
BOMDOC_CHECKS = (
    "lifecycle_risk", "single_source", "no_solution", "out_of_stock_at_qty", "stale_cache",
    "bomdoc_out_of_sync",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS + BOMDOC_CHECKS)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
FINDING_LIMIT = 100
MPN_COVERAGE = 0.5  # below this share of parts with an MPN, MPNs are not kept here

_MULTIPLIERS = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6, "G": 1e9, "R": 1.0, "r": 1.0, "V": 1.0,
    "v": 1.0, "P": 1e-12, "N": 1e-9, "U": 1e-6,
}
_VALUE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)*|[.,]\d+)\s*"  # a comma is a decimal or thousands mark
    # The prefix; R, k, V... with digits after them are decimal marks (4R7, 4k7, 3V3).
    # Upper-case P, N, U only before a unit (100NF), since M is mega.
    r"((?i:meg)|[pnuµμmkKMGRrVv]|[PNU](?=[FfHhΩ]|(?![^\W_])))?"
    # Then no letter or digit may follow ('600R@100MHz' and '4k7_0603' read, while
    # '0402WGF1000' and '1N4148' stay text).
    r"(\d*)\s*([A-Za-zΩ]*)(?![^\W_])"
)
_THOUSANDS = re.compile(r"[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?")  # '0,100' is a decimal comma
_DNP_MARK = re.compile(
    r"\b(?:DNP|DNF|DNI|NOT[ _-]?FITTED|NO[ _-]?POP|DO[ _-]NOT[ _-](?:POPULATE|FIT|PLACE))\b",
    re.IGNORECASE,
)
_PLACEHOLDERS = {"", "?", "-", "--", "tbd", "tba", "n/a", "na", "none", "description", "todo"}
_GENERIC = {
    "resistor", "capacitor", "inductor", "diode", "connector", "led", "ic", "transistor",
    "crystal", "fuse", "ferrite bead", "header", "res", "cap", "ind",
}
_PASSIVE = re.compile(r"^(R|C|L|FB)\d", re.IGNORECASE)
_SIZE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")
# IPC-7351 names give the metric size code (RESC1005X40N is an imperial 0402).
IPC_SIZE = re.compile(r"(?:RES|CAP|IND)[A-Z]{1,2}(\d{4})X", re.IGNORECASE)
METRIC_SIZE = {
    "0402": "01005", "0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805",
    "3216": "1206", "3225": "1210", "4532": "1812", "5025": "2010", "6332": "2512",
}
_NUMBER = r"(?<![\w.])\d+(?:\.\d+)?\s?"
_RATING = re.compile(
    rf"({_NUMBER}k?V(?:DC|AC)?\b|\b[XYZ]\d[A-Z]\b|\bC[0O]G\b|\bNP[0O]\b"
    rf"|{_NUMBER}%|{_NUMBER}m?W\b|{_NUMBER}m?A\b)",
    re.IGNORECASE,
)


def normalize_value(text: str | None):
    """A component value in base units ('100n' -> 1e-07), or folded text when it does
    not read as a number. 'm' is milli, 'M' mega; 'R' and a trailing digit read as a
    decimal mark (4R7, 4k7)."""
    if not text:
        return None
    match = _VALUE.match(text)
    if not match:
        return re.sub(r"\s+", " ", text).strip().lower()
    number, prefix, tail, _unit = match.groups()
    number = number.replace(",", "") if _THOUSANDS.fullmatch(number) else number.replace(",", ".")
    try:
        value = float(f"{number}.{tail}" if tail and "." not in number else number + tail)
    except ValueError:
        return re.sub(r"\s+", " ", text).strip().lower()
    prefix = prefix or ""
    value *= _MULTIPLIERS.get("meg" if prefix.lower() == "meg" else prefix, 1.0)
    return round(value, 15) if value < 1 else round(value, 9)


def _size(part: BomPart) -> str | None:
    """The imperial size code from the footprint (IPC-7351 names are metric), else from
    the footprint or description text."""
    ipc = IPC_SIZE.search(part.footprint or "")
    if ipc and ipc[1] in METRIC_SIZE:
        return METRIC_SIZE[ipc[1]]
    # An IPC body size outside the table (RESC2013) is no imperial code: the text decides.
    footprint = "" if ipc else part.footprint
    match = _SIZE.search(" ".join(filter(None, (footprint, part.description))))
    return match[1] if match else None


def _rating(token: str) -> str:
    token = token.replace(" ", "").lower()
    if token in ("c0g", "cog", "np0", "npo"):
        return "c0g"
    return re.sub(r"v(?:dc|ac)$", "v", token)


def _fold(text: str | None) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _finding(check, severity, issue, designators=(), **fields) -> dict:
    out = {"check": check, "severity": severity}
    if designators:
        out["designators"] = sorted(designators, key=natural_key)
    out.update(fields)
    out["issue"] = issue
    return out


def _check_mpn(parts: list[BomPart], findings: list[dict],
               chosen_in_bom_doc: set[str] | None = None) -> None:
    # Mechanical parts often have none, and 'N/A' says a part has none on purpose. With
    # a BomDoc read, a part whose line has a part choice there has an MPN too.
    chosen = chosen_in_bom_doc or set()
    counted = [part for part in parts if needs_mpn(part)]
    with_mpn = [part for part in counted if part.mpn or part.designator.upper() in chosen]
    coverage = len(with_mpn) / len(counted) if counted else 1.0
    if counted and coverage < MPN_COVERAGE:
        where = (
            "parameter or a part choice in the BomDoc, so MPNs are not kept in the "
            "schematic or ActiveBOM here" if chosen_in_bom_doc is not None else
            "parameter, so MPNs are not kept in the schematic here (perhaps in ActiveBOM, "
            "which is not read)")
        findings.append(_finding(
            "missing_mpn", "info",
            f"Only {round(coverage * 100)}% of the parts carry a manufacturer part number "
            f"{where}: the BOM groups those parts by Comment and footprint.",
            coverage_percent=round(coverage * 100),
        ))
    else:
        groups: dict[tuple, list[BomPart]] = defaultdict(list)
        for part in counted:
            if not part.mpn and part.designator.upper() not in chosen:
                groups[(part.comment or part.value or "", part.footprint or "")].append(part)
        for (comment, footprint), members in groups.items():
            findings.append(_finding(
                "missing_mpn", "warning",
                f"No manufacturer part number on {comment or 'parts'} ({footprint or 'no '
                'footprint'}).",
                [part.designator for part in members], comment=comment or None,
            ))
    by_mpn: dict[str, list[BomPart]] = defaultdict(list)
    for part in with_mpn:
        if part.mpn_source == "parameter" and not part.manufacturer:
            by_mpn[part.mpn].append(part)
    for mpn, members in by_mpn.items():
        findings.append(_finding(
            "mpn_without_manufacturer", "warning",
            f"{mpn} has no manufacturer.", [part.designator for part in members], mpn=mpn,
        ))


def _check_consistency(parts: list[BomPart], findings: list[dict]) -> None:
    by_mpn: dict[str, list[BomPart]] = defaultdict(list)
    for part in parts:
        if part.mpn:
            by_mpn[_fold(part.mpn)].append(part)
    for members in by_mpn.values():
        if len(members) < 2:
            continue
        mpn = members[0].mpn
        for name, normal in (
            ("value", normalize_value), ("description", _fold), ("footprint", _fold),
        ):
            values = {normal(getattr(part, name)) for part in members if getattr(part, name)}
            if len(values) < 2:
                continue
            shown = sorted({getattr(part, name) for part in members if getattr(part, name)})
            findings.append(_finding(
                "mpn_inconsistent", "info" if name == "footprint" else "warning",
                f"{mpn} has different {name}s: {', '.join(shown[:4])}"
                + (" (another footprint model of the same part?)" if name == "footprint" else "."),
                [part.designator for part in members], mpn=mpn, field=name,
            ))


def _check_consolidation(parts: list[BomPart], findings: list[dict]) -> None:
    groups: dict[tuple, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for part in parts:
        prefix = _PASSIVE.match(part.designator)
        if not prefix or not part.mpn:
            continue
        value = normalize_value(part.value or part.comment)
        size = _size(part)
        if value is None or not size:
            continue
        rating = frozenset(
            _rating(token)
            for token in _RATING.findall(" ".join(filter(None, (part.description, part.comment))))
        )
        key = (prefix[1].upper(), value, size, rating)
        groups[key][part.mpn].append(part.designator)
    for (kind, _value, size, rating), by_mpn in groups.items():
        if len(by_mpn) < 2:
            continue
        if not rating and kind in ("L", "FB"):
            continue  # inductors and beads differ in current ratings the text does not give
        mpns = sorted(by_mpn)
        common = len(_common_prefix(mpns))
        if common >= 0.6 * min(len(mpn) for mpn in mpns):
            continue  # one series: packaging or tolerance suffixes
        basis = (
            "the same ratings in their text" if rating
            else "no rating in their text to tell them apart"
        )
        findings.append(_finding(
            "consolidation", "info",
            f"{len(mpns)} part numbers for one {kind} value in {size} with {basis}: "
            f"{', '.join(mpns[:4])}. Consider one part (or check they are deliberate "
            "second sources).",
            [d for designators in by_mpn.values() for d in designators], mpns=mpns,
        ))


def _common_prefix(texts: list[str]) -> str:
    first, last = min(texts), max(texts)
    size = 0
    while size < min(len(first), len(last)) and first[size] == last[size]:
        size += 1
    return first[:size]


def _check_parts(parts: list[BomPart], findings: list[dict]) -> None:
    unresolved = [part for part in parts if part.unresolved]
    for part in unresolved:
        findings.append(_finding(
            "unresolved_special_string", "warning",
            f"{part.designator}: {', '.join(part.unresolved)} names no parameter of the part.",
            [part.designator],
        ))
    marked = [
        part for part in parts if part.fitted
        and any(_DNP_MARK.search(text or "") for text in (part.value, part.comment))
    ]
    if marked:
        findings.append(_finding(
            "dnp_marker_on_fitted", "warning",
            "These parts say 'do not populate' in their Value or Comment but are fitted in "
            "this variant: make them Not Fitted in the variant, or fix the text.",
            [part.designator for part in marked],
        ))
    missing = [part for part in parts if part.kind != 1 and not part.footprint]
    if missing:
        findings.append(_finding(
            "no_footprint", "warning",
            "These BOM parts have no footprint.", [part.designator for part in missing],
        ))
    placeholder = defaultdict(list)
    generic = defaultdict(list)
    for part in parts:
        text = (part.description or "").strip()
        if text.lower() in _PLACEHOLDERS:
            placeholder[text].append(part.designator)
        elif text.lower() in _GENERIC and not part.mpn:
            generic[text].append(part.designator)
    for text, designators in placeholder.items():
        findings.append(_finding(
            "placeholder_description", "warning",
            f"Description is {'empty' if not text else repr(text)}.", designators,
        ))
    for text, designators in generic.items():
        findings.append(_finding(
            "placeholder_description", "info",
            f"Description is only '{text}' and there is no MPN to say which part.",
            designators,
        ))


def run_bom_checks(
    parts: list[BomPart],
    netlist: dict,
    plan=None,
    checks: list[str] | None = None,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    bom_doc_findings: list[dict] | None = None,
    chosen_in_bom_doc: set[str] | None = None,
) -> dict:
    """The checks over a variant's BOM parts. bom_doc_findings (services/active_bom's,
    given when the project's BomDoc was read) add BOMDOC_CHECKS to the checks run;
    chosen_in_bom_doc are the designators (upper case) whose BomDoc line has a part
    choice, which then count as having an MPN."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    unknown = [check for check in requested if check not in CHECKS + BOMDOC_CHECKS]
    if unknown:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {unknown}; use {list(CHECKS)}, and with an "
                           f"ActiveBOM document {list(BOMDOC_CHECKS)}."}
    available = CHECKS + BOMDOC_CHECKS if bom_doc_findings is not None else CHECKS
    fitted = [part for part in parts if part.fitted]
    findings: list[dict] = []
    _check_parts(fitted, findings)
    _check_mpn(fitted, findings, chosen_in_bom_doc if bom_doc_findings is not None else None)
    _check_consistency(fitted, findings)
    _check_consolidation(fitted, findings)
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    # Sheet designators of board-annotated parts (R1 compiled as R101), by sheet.
    annotated = {
        (meta.get("sheet"), meta["logical_refdes"]) for name, meta in components.items()
        if meta.get("logical_refdes") and name in compiled
    }
    for clash in netlist.get("designator_clashes") or []:
        # Clashes are between live parts (compile-masked copies are not recorded); one
        # outside the compiled design (its sheet is not compiled) is not in the BOM.
        refdes = clash.get("refdes") or ""
        if compiled and (clash.get("sheet"), refdes) not in annotated and not any(
            name == refdes or re.fullmatch(
                rf"{re.escape(refdes)}(?:[_.\-]?[A-Za-z]+\d*|[_.\-]\d+)+", name)
            for name in compiled
        ):
            continue
        findings.append(_finding(
            "duplicate_designator", "warning",
            f"{refdes} names {len(clash.get('lib_references') or [])} different parts on "
            f"sheet {clash.get('sheet')}: the BOM can hold only one, and the loaded data "
            "keeps the last.",
            [refdes] if refdes else [],
        ))
    for entry in getattr(plan, "stale", []) or []:
        findings.append(_finding(
            "stale_variant_entry", "info",
            f"Variant entry for {entry['designator']}: {entry['detail']}.",
            [entry["designator"]] if entry.get("designator") else [], reason=entry["issue"],
        ))
    uncompiled = (
        [name for name in components if name not in compiled] if compiled
        else [name for name, meta in components.items() if meta.get("compile_masked")]
    )
    if uncompiled:
        findings.append(_finding(
            "not_compiled", "info",
            f"{len(uncompiled)} sheet parts are not in the compiled design (compile "
            "masks, or sheets outside the project) and are not in the BOM.",
            uncompiled[:30], count=len(uncompiled),
        ))
    if bom_doc_findings:
        # A line no_solution names is not listed again part by part under missing_mpn.
        named = {designator.upper() for finding in bom_doc_findings
                 if finding["check"] == "no_solution" and finding["severity"] == "warning"
                 for designator in finding.get("designators", [])}
        for finding in findings:
            if finding["check"] == "missing_mpn" and finding.get("designators"):
                finding["designators"] = [d for d in finding["designators"]
                                          if d.upper() not in named]
        findings = [finding for finding in findings
                    if not (finding["check"] == "missing_mpn" and "designators" in finding
                            and not finding["designators"])]
        # An MPN the parameter check already found inconsistent is not reported again
        # from the BomDoc: its BomDoc lines are added to that finding.
        reported = {(_fold(finding.get("mpn")), finding.get("field")): finding
                    for finding in findings if finding["check"] == "mpn_inconsistent"}
        for finding in bom_doc_findings:
            same = reported.get((_fold(finding.get("mpn")), finding.get("field")))                 if finding["check"] == "mpn_inconsistent" else None
            if same is None:
                findings.append(finding)
                continue
            for key in ("lines", "unnumbered_lines"):
                if finding.get(key):
                    same[f"bom_doc_{key}"] = finding[key]
    # A BomDoc check asked for on a project without a BomDoc is not run (said so).
    not_run = {check: "needs an ActiveBOM document (.BomDoc) in the project"
               for check in requested if check not in available}
    run = [check for check in requested if check in available] if requested else list(available)
    wanted = set(run)
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]], finding["issue"],
    ))
    severities = Counter(finding["severity"] for finding in findings)
    offset = max(0, int(offset or 0))
    result = {
        "checks_run": run,
        "summary": dict(Counter(finding["check"] for finding in findings)),
        "severity_counts": {
            level: severities.get(level, 0) for level in ("error", "warning", "info")
        },
        "findings": findings[offset : offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if not_run:
        result["checks_not_run"] = not_run
    return result
