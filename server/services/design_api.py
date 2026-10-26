"""A stable, read-only view of the loaded design for team check packs (run_custom_checks)
and their Python plugins.

What the deterministic checks read about the design, in one place and in plain values:

- parts: every compiled part (graphical and no-BOM parts too) with its designator,
  designator prefix, a class guess (kind: resistor, capacitor, inductor, ferrite, choke,
  diode, zener, led, tvs, ic, connector, transistor, transformer, crystal, fuse, switch,
  relay, test_point, fiducial, mechanical, battery, jumper, thermistor or other, read as
  check_interfaces reads parts: designator prefix, then description, comment, value,
  footprint and part number words), whether the active variant fits it, whether it is
  in the BOM and should carry a manufacturer part number (check_bom's reading), its parameters with the variant's parameter variations applied, value,
  comment, description, footprint, manufacturer and part number (as get_bom reads
  them), the imperial case size of passives and diodes (from a Case/Package/Size
  parameter, the part's text or its footprint name), its side and position on the PCB,
  and its pins;
- pins: the part, pin number, pin name, electrical type, role (decoupling's reading of
  the name: supply, ground, ref, cap, out, in, other or none) and net (None when
  unconnected);
- nets: name, pins, parts, classes (ground and rail as check_decoupling and
  check_pull_resistors read them; supply: a rail, or a net that feeds the pull-ups of two
  or more I2C or open-drain lines, a supply a GPIO switches; signal; i2c and open_drain as
  check_pull_resistors reads net and pin names and pin types; diff_pair (signals only);
  connector, connector_power, and off_board and external for the lines into the board
  that check_interfaces' no_esd asks ESD protection for: a connector signal pin whose
  line reaches an IC, a transistor or another active part; single_pin), the voltage with
  its source and confidence as check_derating resolves it (user rails, ground, the net
  name, the power tree, a divider midpoint), its line (the nets joined to it by series
  resistors under 1 kohm, as check_pull_resistors joins a signal) and the connectors it
  leaves the board through.

Measures: the voltage across a capacitor and its rated voltage (check_derating's
reading), the nets reached from a net through fitted series parts (0 ohm links and
resistors, ferrites, inductors, common-mode chokes, series capacitors, as
check_interfaces walks a connector line; rails and grounds are never crossed), and the
straight distance from pads to the nearest pad of other parts, pad edge to pad edge
(check_decoupling's measure), in mm, with the pads' bounding boxes skipping parts that
cannot be nearer.

Everything is computed lazily (the PCB is read only when a side, position or distance is
asked for; voltages only when one is) and cached for the API object's life. Objects are
frozen: plugins read them, they never change the design. API_VERSION changes only when a
field is removed or changes meaning; new fields may be added.
"""

import math
import re
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

try:
    from services.bom import build_parts, natural_key, needs_mpn
    from services.decoupling import (
        FERRITE_TEXT, component_pads, designator_prefix, pcb_components, pin_role,
    )
    from services.derating import (
        _CASE_PARAMS, _param_name, across_voltage, case_in_text, net_voltages, read_parts,
    )
    from services.interfaces import (
        audit_connector, build_context, categorize, choke_far, exposed_lines, part_text,
    )
    from services.power_tree import build_tree
    from services.pull_resistors import (
        DIFF_HALF, classify_net, link_group, merged_signals, open_drain_nets, pull_supply,
    )
except ImportError:
    from server.services.bom import build_parts, natural_key, needs_mpn
    from server.services.decoupling import (
        FERRITE_TEXT, component_pads, designator_prefix, pcb_components, pin_role,
    )
    from server.services.derating import (
        _CASE_PARAMS, _param_name, across_voltage, case_in_text, net_voltages, read_parts,
    )
    from server.services.interfaces import (
        audit_connector, build_context, categorize, choke_far, exposed_lines, part_text,
    )
    from server.services.power_tree import build_tree
    from server.services.pull_resistors import (
        DIFF_HALF, classify_net, link_group, merged_signals, open_drain_nets, pull_supply,
    )

API_VERSION = 1
MM_PER_MIL = 0.0254

KINDS = (
    "resistor", "capacitor", "inductor", "ferrite", "choke", "diode", "zener", "led", "tvs",
    "ic", "connector", "transistor", "transformer", "crystal", "fuse", "switch", "relay",
    "test_point", "fiducial", "mechanical", "battery", "jumper", "thermistor", "other",
)
# Other words for a kind that filters accept.
KIND_ALIASES = {"esd": "tvs", "bead": "ferrite", "inductive": "inductor", "cap": "capacitor",
                "res": "resistor", "xtal": "crystal", "oscillator": "crystal"}
NET_CLASSES = ("ground", "rail", "supply", "signal", "diff_pair", "i2c", "open_drain",
               "connector", "connector_power", "off_board", "external", "single_pin")
NET_CLASS_ALIASES = {"power": "rail", "gnd": "ground"}
PIN_ROLES = ("supply", "ground", "ref", "cap", "out", "in", "other", "none")
PIN_TYPES = ("Input", "IO", "Output", "OpenCollector", "Passive", "HiZ", "OpenEmitter", "Power")
# Series parts a walk crosses (user words -> check_interfaces' edge kinds).
THROUGH = {"resistor": ("resistor", "0_ohm"), "ferrite": ("ferrite",),
           "inductor": ("inductor",), "choke": ("common_mode_choke",),
           "capacitor": ("capacitor",)}
# Kinds whose case size is read from text and the footprint name as well as parameters.
CASE_KINDS = frozenset({"resistor", "capacitor", "inductor", "ferrite", "diode", "zener",
                        "led", "tvs", "fuse", "thermistor", "crystal"})
CASE_ORDER = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1806", "1808",
              "1812", "1825", "2010", "2220", "2225", "2512", "2920")

_LED_TEXT = re.compile(r"\bLEDs?\b|light[- ]?emitting", re.I)
_CRYSTAL_TEXT = re.compile(r"crystal|\bxtal|oscillat|resonator", re.I)
_TEST_POINT_TEXT = re.compile(r"test ?point", re.I)
_FIDUCIAL_TEXT = re.compile(r"fiducial", re.I)
_MOUNT_TEXT = re.compile(r"mounting hole|\bstand-?off|\bspacer|heat ?sink|\bscrew\b", re.I)
_BATTERY_TEXT = re.compile(r"battery|coin cell|cell holder", re.I)
_THERMISTOR_TEXT = re.compile(r"thermistor|\bNTC\b|\bPTC\b", re.I)
_JUMPER_TEXT = re.compile(r"jumper|solder ?bridge|\bshunt\b", re.I)


def _norm(name: str) -> str:
    """A parameter name for comparison: lower case, separators folded ('Manufacturer
    Part Number 1' == 'manufacturer_part_number_1')."""
    return re.sub(r"[\s_\-]+", " ", str(name).strip().lower())


@dataclass(frozen=True)
class Voltage:
    """A net's voltage as check_derating resolves it. source: user (rails given), ground,
    name, power_tree or divider; confidence: high, medium or low."""
    volts: float
    source: str
    confidence: str
    basis: str


@dataclass(frozen=True)
class Placement:
    """Where a part sits on the PCB (its first copy when the board holds several)."""
    side: str  # top or bottom
    x_mm: float
    y_mm: float
    rotation: float
    pcb_designator: str


@dataclass(frozen=True)
class Pin:
    part: str  # the part's designator
    number: str  # the pin designator
    name: str
    type: str | None  # Altium's electrical type: Input, IO, Output, Passive, Power ...
    role: str  # supply, ground, ref, cap, out, in, other or none (from the name)
    net: str | None  # None when the pin is not connected

    @property
    def ref(self) -> str:
        """'U1.5'."""
        return f"{self.part}.{self.number}"


@dataclass(frozen=True, eq=False)
class Part:
    designator: str
    prefix: str
    kind: str
    fitted: bool
    in_bom: bool
    value: str | None
    comment: str | None
    description: str | None
    footprint: str | None
    manufacturer: str | None
    mpn: str | None
    lib_reference: str | None
    sheet: str | None
    parameters: Mapping[str, str]
    pins: tuple[Pin, ...]
    # Whether the part should carry a manufacturer part number, as check_bom reads it: in
    # the BOM, not a mechanical part, and not marked as having none on purpose ('N/A').
    needs_mpn: bool
    _api: "DesignAPI" = field(repr=False, compare=False)

    @property
    def nets(self) -> tuple[str, ...]:
        """The nets of the part's connected pins, in pin order, each once."""
        return tuple(dict.fromkeys(pin.net for pin in self.pins if pin.net))

    def parameter(self, name: str) -> str | None:
        """A parameter's value by name (case and separators ignored), None when missing or
        empty."""
        value = self._api._parameter(self, name)
        return value if value and value.strip() and value.strip() != "*" else None

    @property
    def case_size(self) -> str | None:
        return self._api._case_size(self)

    @property
    def placement(self) -> Placement | None:
        return self._api.placement(self.designator)

    @property
    def side(self) -> str | None:
        placement = self.placement
        return placement.side if placement else None

    @property
    def rated_voltage(self) -> float | None:
        return self._api.rated_voltage(self.designator)

    @property
    def voltage_across(self) -> float | None:
        return self._api.voltage_across(self.designator)


@dataclass(frozen=True, eq=False)
class Net:
    name: str
    pins: tuple[Pin, ...]
    _api: "DesignAPI" = field(repr=False, compare=False)

    @property
    def designators(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pin.part for pin in self.pins))

    @property
    def parts(self) -> tuple[Part, ...]:
        return tuple(self._api.part(ref) for ref in self.designators if self._api.part(ref))

    @property
    def classes(self) -> frozenset[str]:
        return self._api.net_classes(self.name)

    @property
    def voltage(self) -> Voltage | None:
        return self._api.voltage(self.name)

    @property
    def line(self) -> tuple[str, ...]:
        """This net and the nets joined to it by series resistors under 1 kohm."""
        return self._api.line(self.name)

    @property
    def connectors(self) -> tuple[str, ...]:
        """The fitted connectors the net leaves the board through (connectors_of)."""
        return self._api.connectors_of(self.name)


class DesignAPI:
    """The read-only design data of one project and variant (see the module notes).

    netlist is the loaded netlist, plan the variant plan (bom.plan_variant; None for the
    base design), project_params the .PrjPcb's parameters, pcb a callable returning
    (PcbIndex, None) or (None, error dict) that is called only when the board is needed,
    rails the user's net voltages ({'VIN': 24}) that win over every other source."""

    version = API_VERSION

    def __init__(self, netlist: dict, plan=None, project_params: dict | None = None, *,
                 pcb: Callable[[], tuple] | None = None, rails: dict[str, float] | None = None,
                 base: "DesignAPI | None" = None):
        self._netlist = netlist
        self._plan = plan
        self._params = project_params or {}
        self._pcb_loader = pcb
        self._rails = dict(rails or {})
        self._base = base  # shares parts, nets and the PCB with another rails map

    def with_rails(self, rails: dict[str, float] | None) -> "DesignAPI":
        """The same design with other user net voltages (parts, nets and the PCB are
        shared; voltages are resolved again)."""
        if not rails and not self._rails:
            return self
        return DesignAPI(self._netlist, self._plan, self._params, pcb=self._pcb_loader,
                         rails=rails, base=self._root)

    @property
    def _root(self) -> "DesignAPI":
        return self._base._root if self._base is not None else self

    # ----- parts, pins and nets -----

    @property
    def variant(self) -> str:
        return getattr(self._plan, "name", None) or "[No Variations]"

    @property
    def parts(self) -> tuple[Part, ...]:
        return tuple(self._root._part_map.values())

    @property
    def nets(self) -> tuple[Net, ...]:
        return tuple(self._root._net_map.values())

    @property
    def pins(self) -> tuple[Pin, ...]:
        return tuple(pin for part in self.parts for pin in part.pins)

    def part(self, designator: str) -> Part | None:
        root = self._root
        return root._part_map.get(designator) or root._part_folded.get(str(designator).casefold())

    def net(self, name: str) -> Net | None:
        root = self._root
        return root._net_map.get(name) or root._net_folded.get(str(name).casefold())

    def parts_on(self, net: str) -> tuple[Part, ...]:
        found = self.net(net)
        return found.parts if found else ()

    @cached_property
    def _bom(self) -> dict:
        parts, _ = build_parts(self._netlist, self._plan, self._params)
        return {part.designator: part for part in parts}

    @cached_property
    def _context(self):
        """check_interfaces' reading of the design (fitted parts' kinds, series edges)."""
        return build_context(self._netlist, self._plan, self._params)

    @cached_property
    def _part_map(self) -> dict[str, Part]:
        netlist = self._netlist
        components = netlist.get("components") or {}
        compiled = netlist.get("parts") or {}
        pin_to_net = netlist.get("pin_to_net") or {}
        names = list(compiled) if compiled else [
            name for name, meta in components.items() if not meta.get("compile_masked")]
        not_fitted = getattr(self._plan, "not_fitted", set()) or set()
        varied = getattr(self._plan, "params", {}) or {}
        out: dict[str, Part] = {}
        for ref in sorted(names, key=natural_key):
            meta = components.get(ref) or {}
            bom = self._bom.get(ref)
            connected = pin_to_net.get(ref) or {}
            numbers = list(dict.fromkeys([*(meta.get("pin_designators") or []), *connected]))
            pin_names = meta.get("pin_names") or {}
            pin_types = meta.get("pin_types") or {}
            pin_meta = meta.get("pins") or {}
            pins = []
            for number in numbers:
                if not number:
                    continue
                name = pin_names.get(number) or (pin_meta.get(number) or {}).get("name") or ""
                pins.append(Pin(ref, str(number), str(name), pin_types.get(number),
                                pin_role(name), connected.get(number)))
            if bom is not None:
                params = dict(bom.parameters)
                value, comment, description = bom.value, bom.comment, bom.description
                footprint, mpn, maker = bom.footprint, bom.mpn, bom.manufacturer
                fitted, lib_reference = bom.fitted, bom.lib_reference or None
            else:
                params = dict(meta.get("params") or {})
                for key, (_, text) in (varied.get(ref) or {}).items():
                    params[key] = text
                value = params.get("value") or meta.get("value")
                comment = (compiled.get(ref) or {}).get("comment") or params.get("comment")
                description = meta.get("description") or params.get("description")
                footprint = (compiled.get(ref) or {}).get("footprint") or meta.get("footprint")
                mpn = maker = None
                fitted = ref not in not_fitted
                lib_reference = meta.get("lib_reference") or None
            text = part_text(meta, bom)
            prefix = designator_prefix(ref)
            kind = self._kind(prefix, text, meta, len(numbers),
                              [pin.type for pin in pins if pin.net])
            out[ref] = Part(ref, prefix, kind, bool(fitted), bom is not None, value or None,
                            comment or None, description or None, footprint or None,
                            maker or None, mpn or None, lib_reference, meta.get("sheet"),
                            MappingProxyType({str(k): str(v) for k, v in params.items()
                                              if v is not None}),
                            tuple(pins), bom is not None and needs_mpn(bom), self)
        return out

    @staticmethod
    def _kind(prefix: str, text: str, meta: dict, pins: int, pin_types: list) -> str:
        """check_interfaces' category of a part, refined into the KINDS words."""
        kind = categorize(prefix, text, pins, pin_types)
        if kind == "esd":
            return "tvs"
        if kind == "inductive":
            return "ferrite" if FERRITE_TEXT.search(text) or prefix in ("FB", "BEAD", "FE") \
                else "inductor"
        if kind == "diode" and (prefix in ("LED", "DS") or _LED_TEXT.search(text)):
            return "led"
        if kind in ("passive", "other"):
            if prefix == "TP" or _TEST_POINT_TEXT.search(text):
                return "test_point"
            if prefix in ("FID", "FD") or _FIDUCIAL_TEXT.search(text):
                return "fiducial"
            if prefix in ("Y", "XTAL", "OSC") or _CRYSTAL_TEXT.search(text):
                return "crystal"
            if prefix in ("BAT", "BT") or _BATTERY_TEXT.search(text):
                return "battery"
            if prefix in ("RT", "TH", "NTC") or _THERMISTOR_TEXT.search(text):
                return "thermistor"
            if prefix in ("JP", "SJ", "W") or _JUMPER_TEXT.search(text):
                return "jumper"
            if prefix in ("MH", "H", "HOLE", "MP", "MK", "LOGO") or _MOUNT_TEXT.search(text) \
                    or int(meta.get("kind") or 0) in (1, 2):
                return "mechanical"
            return "other"
        return kind

    @cached_property
    def _part_folded(self) -> dict[str, Part]:
        return {ref.casefold(): part for ref, part in self._part_map.items()}

    @cached_property
    def _net_map(self) -> dict[str, Net]:
        members: dict[str, list[Pin]] = {}
        for part in self._part_map.values():
            for pin in part.pins:
                if pin.net:
                    members.setdefault(pin.net, []).append(pin)
        return {name: Net(name, tuple(pins), self)
                for name, pins in sorted(members.items(), key=lambda item: natural_key(item[0]))}

    @cached_property
    def _net_folded(self) -> dict[str, Net]:
        return {name.casefold(): net for name, net in self._net_map.items()}

    def _parameter(self, part: Part, name: str) -> str | None:
        wanted = _norm(name)
        for key, value in part.parameters.items():
            if _norm(key) == wanted:
                return value
        return None

    # ----- case sizes -----

    def _case_size(self, part: Part) -> str | None:
        root = self._root
        if part.designator not in root._cases:
            root._cases[part.designator] = self._read_case(part)
        return root._cases[part.designator]

    @cached_property
    def _cases(self) -> dict[str, str | None]:
        return {}

    @staticmethod
    def _read_case(part: Part) -> str | None:
        """The imperial case code of a passive or diode: a Case/Package/Size parameter (a
        metric one read as metric), then the value, comment and description text, then
        the footprint name. None for other kinds (an IC's part number or package
        text is no case code)."""
        if part.kind not in CASE_KINDS:
            return None
        by_name = {_param_name(key): value for key, value in part.parameters.items()}
        for name in _CASE_PARAMS:
            if by_name.get(name):
                found = case_in_text(by_name[name], metric="metric" in name)
                if found:
                    return found
        for text in (part.value, part.comment, part.description, part.footprint):
            found = case_in_text(text)
            if found:
                return found
        return None

    # ----- net classes -----

    @cached_property
    def _design(self):
        return self._context.design

    @cached_property
    def _connector_audit(self) -> tuple[dict[str, set[str]], dict[str, dict[str, list[str]]]]:
        """(net -> connector, connector_power, off_board, external; net -> the connectors
        that make it off_board and external) from the fitted connectors' pins as
        check_interfaces audits them: off_board is a line into the board whose walk
        reaches an IC, a transistor or another active part (the pins check_interfaces'
        no_esd asks ESD protection for; not a line into a transformer or another
        connector, not a LED or centre-tap pin), external the same on a connector it
        judges user-facing, connector_power a power pin."""
        ctx = self._context
        out: dict[str, set[str]] = {}
        lines: dict[str, dict[str, list[str]]] = {}
        for ref, kind in ctx.kind.items():
            if kind != "connector" or not ctx.fitted.get(ref, True):
                continue
            try:
                connector = audit_connector(ctx, ref)
            except Exception:  # one unreadable connector must not stop the others
                continue
            for pin in connector.pins:
                if not pin.net:
                    continue
                classes = out.setdefault(pin.net, set())
                classes.add("connector")
                if pin.cls == "power":
                    classes.add("connector_power")
            for pin in exposed_lines(ctx, connector):
                if not pin.net:
                    continue
                found = ["off_board", *(["external"] if connector.exposure == "external"
                                        else [])]
                out[pin.net].update(found)
                for name in found:
                    refs = lines.setdefault(pin.net, {}).setdefault(name, [])
                    if ref not in refs:
                        refs.append(ref)
        return out, lines

    def connectors_of(self, net: str) -> tuple[str, ...]:
        """The fitted connectors a net leaves the board through: those that make it
        external (user-facing), else off_board, else every fitted connector on it."""
        root = self._root
        found = root.net(net)
        if found is None:
            return ()
        on_net = tuple(part.designator for part in found.parts
                       if part.kind == "connector" and part.fitted)
        if not on_net:  # no connector: the audit of every connector is not needed
            return ()
        lines = root._connector_audit[1].get(found.name) or {}
        for name in ("external", "off_board"):
            if lines.get(name):
                return tuple(sorted(lines[name], key=natural_key))
        return on_net

    @cached_property
    def _diff_nets(self) -> set[str]:
        names = set(self._net_map)
        folded = {name.casefold() for name in names}
        out = set()
        for name in names:
            match = DIFF_HALF.search(name)
            if not match:
                continue
            stem = name[:match.start()]
            tail = match.group(0)
            partners = {"P": "N", "N": "P", "+": "-", "-": "+"}
            if tail in ("DP", "DM", "D+", "D-"):
                partner = {"DP": "DM", "DM": "DP", "D+": "D-", "D-": "D+"}[tail]
                candidates = [stem + partner]
            else:
                last = tail[-1].upper()
                candidates = [stem + tail[:-1] + partners.get(last, last)]
            if any(candidate.casefold() in folded and candidate.casefold() != name.casefold()
                   for candidate in candidates):
                out.add(name)
        index = self._pcb[0]  # the board's differential pairs too, when there is one
        if index is not None:
            out |= {name for name in names if index.diff_pairs_of(name)}
        return out

    def net_classes(self, name: str) -> frozenset[str]:
        root = self._root
        if name not in root._class_cache:
            root._class_cache[name] = frozenset(root._read_classes(name))
        return root._class_cache[name]

    @cached_property
    def _class_cache(self) -> dict[str, frozenset[str]]:
        return {}

    def _read_classes(self, name: str) -> set[str]:
        design = self._design
        net = self._net_map.get(name)
        classes: set[str] = set()
        if name in design.grounds:
            classes.add("ground")
        elif name in design.rails:
            classes.add("rail")
        else:
            classes.add("signal")
        if "rail" in classes or name in self._pull_supplies:
            classes.add("supply")
        if "signal" in classes and name in self._diff_nets:  # never a split rail (V+, V-)
            classes.add("diff_pair")
        classes |= self._signal_classes.get(name, set()) & {"i2c", "open_drain"}
        classes |= self._connector_audit[0].get(name, set())
        if net is not None and len(net.pins) == 1:
            classes.add("single_pin")
        return classes

    @cached_property
    def _signal_classes(self) -> dict[str, set[str]]:
        """check_pull_resistors' classes of each net that is not a rail or ground (i2c,
        open_drain, control ...), from net and pin names and pin types."""
        design = self._design
        out: dict[str, set[str]] = {}
        for net in design.nets:
            if design.fixed(net):
                continue
            entry = classify_net(design, net)
            if entry is not None:
                out[net] = set(entry.classes)
        return out

    @cached_property
    def _pull_supplies(self) -> set[str]:
        """Nets that pull up two or more I2C or open-drain lines through 1 kohm or more: a
        supply a GPIO switches, as check_pull_resistors recognises it (a line is the
        signal with the nets joined to it, so a pull-up behind a series resistor counts)."""
        design = self._design
        lines = open_drain_nets(merged_signals(design))
        return {net for net in design.series
                if net not in lines and not design.fixed(net)
                and pull_supply(design, net, lines)}

    def line(self, net: str) -> tuple[str, ...]:
        """The net and the nets joined to it by fitted series resistors under 1 kohm (0 ohm
        links, damping resistors): one signal line, as check_pull_resistors joins them.
        A rail or ground is a line of its own."""
        root = self._root
        found = root.net(net)
        if found is None:
            return ()
        if found.name not in root._lines:
            design = root._design
            root._lines[found.name] = (found.name,) if design.fixed(found.name) or \
                found.name not in design.nets else tuple(link_group(design, found.name))
        return root._lines[found.name]

    @cached_property
    def _lines(self) -> dict[str, tuple[str, ...]]:
        return {}

    # ----- voltages and ratings -----

    @cached_property
    def _tree(self):
        return build_tree(self._netlist, self._plan, self._params)

    @cached_property
    def _derating_parts(self) -> dict:
        root = self._root
        tree = root._tree
        checked, _, _ = read_parts(tree, self._netlist, self._plan)
        return {part.ref: part for part in checked}

    @cached_property
    def _voltages(self) -> dict:
        root = self._root
        tree = root._tree
        voltages, _ = net_voltages(tree, {"converters": []}, self._rails or None,
                                   list(root._derating_parts.values()))
        return voltages

    @cached_property
    def _switch_nodes(self) -> set[str]:
        return {net for conv in self._root._tree.converters.values()
                for net in conv.switch_nodes}

    def voltage(self, net: str) -> Voltage | None:
        """The net's voltage with its source and confidence (check_derating's), or None
        when no source gives it."""
        found = self.net(net)
        if found is None:
            return None
        volt = self._voltages.get(found.name)
        if volt is None:
            return None
        return Voltage(float(volt.volts), volt.source, volt.confidence, volt.basis)

    def rated_voltage(self, designator: str) -> float | None:
        """A fitted capacitor's or resistor's voltage rating as check_derating reads it
        (parameters, text, decoded part number), or None."""
        part = self._root._derating_parts.get(designator)
        rated = part.fields.get("rated_v") if part else None
        return float(rated.value) if rated else None

    def rating_source(self, designator: str) -> str | None:
        part = self._root._derating_parts.get(designator)
        rated = part.fields.get("rated_v") if part else None
        return rated.describe() if rated else None

    def voltage_across(self, designator: str) -> float | None:
        """The largest voltage across a fitted capacitor or resistor, from its nets'
        voltages (both ends known), or None."""
        part = self._root._derating_parts.get(designator)
        if part is None:
            return None
        known = [item.volts for a, b in part.sections
                 if (item := across_voltage(a, b, self._voltages, self._switch_nodes)).volts
                 is not None]
        return max(known) if known else None

    # ----- connectivity through series parts -----

    def reachable(self, nets: Iterable[str], hops: int,
                  through: Iterable[str] | None = None) -> dict[str, int]:
        """net -> the fewest series parts crossed to reach it from nets, up to hops,
        through fitted series parts of the THROUGH kinds (default all). Rails and grounds
        are never crossed (check_interfaces' edges)."""
        allowed = {edge for word in (through or THROUGH) for edge in THROUGH.get(word, ())}
        ctx = self._root._context
        seen = {net: 0 for net in nets if net}
        level = list(seen)
        for depth in range(1, max(0, hops) + 1):
            following = []
            for net in level:
                edges = [edge for edge in ctx.edges.get(net, ()) if edge.fitted]
                plain = [edge for edge in edges
                         if edge.kind != "common_mode_choke" and edge.kind in allowed]
                chokes = choke_far(ctx, [edge for edge in edges
                                         if edge.kind == "common_mode_choke"], "", net) \
                    if "common_mode_choke" in allowed else []
                for edge in [*plain, *chokes]:
                    if edge.far not in seen:
                        seen[edge.far] = depth
                        following.append(edge.far)
            level = following
            if not level:
                break
        return seen

    # ----- the PCB -----

    @cached_property
    def _pcb(self) -> tuple:
        if self._pcb_loader is None:
            return None, "no PCB was given"
        try:
            index, error = self._pcb_loader()
        except Exception as error:  # a broken board must not stop the checks
            return None, f"{type(error).__name__}"
        if index is None:
            message = (error or {}).get("error") if isinstance(error, dict) else error
            return None, str(message or "the PCB could not be read")
        return index, None

    @property
    def has_pcb(self) -> bool:
        return self._root._pcb[0] is not None

    @property
    def pcb_error(self) -> str | None:
        return self._root._pcb[1]

    @cached_property
    def _pcb_rows(self) -> dict[str, list[dict]]:
        index = self._pcb[0]
        if index is None:
            return {}
        by_name = pcb_components(index)
        folded: dict[str, list[dict]] = {}
        for name, rows in by_name.items():
            folded.setdefault(name.casefold(), []).extend(rows)
        return folded

    @cached_property
    def _pads(self) -> dict[int, list]:
        index = self._pcb[0]
        if index is None:
            return {}
        rows = self._pcb_rows
        return component_pads(index, {name: found for name, found in rows.items()},
                              list(rows))

    def placement(self, designator: str) -> Placement | None:
        """The part's side and position on the PCB, or None (no PCB, or not on it)."""
        root = self._root
        rows = root._pcb_rows.get(str(designator).casefold()) if root.has_pcb else None
        if not rows:
            return None
        row = rows[0]
        return Placement(row["side"], round(row["x"] * MM_PER_MIL, 3),
                         round(row["y"] * MM_PER_MIL, 3), float(row["rotation"] or 0.0),
                         row["refdes"])

    def pads(self, designator: str, pin: str | None = None) -> list:
        """The part's pad rows on the PCB (one pin's with pin=)."""
        root = self._root
        if not root.has_pcb:
            return []
        out = []
        for row in root._pcb_rows.get(str(designator).casefold()) or []:
            for pad in root._pads.get(row["id"], ()):
                if pin is None or str(pad["pad_name"]) == str(pin):
                    out.append(pad)
        return out

    def distance_mm(self, pads_a: list, pads_b: list) -> float | None:
        """The smallest straight distance between two sets of pad rows, pad edge to pad
        edge, in mm (0 when they touch), or None when either is empty."""
        index = self._root._pcb[0]
        if index is None or not pads_a or not pads_b:
            return None
        return round(max(0.0, _closest(index, pads_a, pads_b)) * MM_PER_MIL, 3)

    def nearest(self, pads: list, candidates: Mapping[str, list]) -> tuple[str, float] | None:
        """The candidate (designator -> its pad rows) nearest to pads, pad edge to pad
        edge, and its distance in mm, as distance_mm measures it (a tie goes to the first
        designator in natural order); None without a PCB or pads. The pads' bounding boxes
        skip the candidates that cannot be nearer, so a search over every part of a board
        measures only the few close ones."""
        index = self._root._pcb[0]
        if index is None or not pads:
            return None
        around = _box(pads)
        ranked = sorted((_gap(around, _box(found)), natural_key(ref), ref)
                        for ref, found in candidates.items() if found)
        best: tuple[float, list, str] | None = None
        for gap, key, ref in ranked:
            # Rounded to 0.001 mm, a candidate this far cannot be nearer or tie.
            if best is not None and gap * MM_PER_MIL > best[0] + 0.001:
                break
            limit = (best[0] + 0.001) / MM_PER_MIL if best is not None else math.inf
            distance = round(max(0.0, _closest(index, pads, candidates[ref], limit))
                             * MM_PER_MIL, 3)
            if best is None or (distance, key) < (best[0], best[1]):
                best = (distance, key, ref)
        return (best[2], best[0]) if best is not None else None


def _box(pads: list) -> tuple[float, float, float, float] | None:
    """The bounding box (mil) of pad rows' copper on every layer, None when one has none."""
    try:
        return (min(pad["minx"] for pad in pads), min(pad["miny"] for pad in pads),
                max(pad["maxx"] for pad in pads), max(pad["maxy"] for pad in pads))
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _gap(first, second) -> float:
    """The gap between two bounding boxes in mil (0 when they overlap or one is None): a
    lower bound of the copper distance."""
    if first is None or second is None:
        return 0.0
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def _closest(index, pads_a: list, pads_b: list, limit: float = math.inf) -> float:
    """The smallest pad-to-pad copper distance in mil, measuring pairs in order of their
    bounding-box gap and stopping where the gap reaches the best found (or limit: a
    distance at or beyond it is not needed exactly; it is returned as is)."""
    pairs = sorted(((_gap(_box([a]), _box([b])), i, j) for i, a in enumerate(pads_a)
                    for j, b in enumerate(pads_b)), key=lambda item: item[0])
    best = math.inf
    for gap, i, j in pairs:
        if gap >= min(best, limit):
            break
        best = min(best, index._primitive_distance(pads_a[i], pads_b[j]))
    return best if best < math.inf else max(limit, pairs[0][0] if pairs else 0.0)
