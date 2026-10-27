"""PCB design-rule values, scope evaluation and rule resolution.

Pure logic over parsed PcbData records: no SQLite, no MCP. PcbIndex builds a
RuleContext and asks which rules apply to a net or differential pair.

Scope expressions are Altium query language (e.g. "InNetClass('HS') And Not
IsVia"). They are evaluated with three-valued logic for one target (a net or
a pair): True (applies to the target's objects), False (cannot apply), or
Unknown (applies only to some of its objects, e.g. its vias or the part of it
inside a room). Unknown results carry the sub-expressions that decide them.
"""

import re
from dataclasses import dataclass, field, replace

try:
    from parsers.pcb_doc import ALTIUM_UNITS_PER_MIL, PcbRule
except ImportError:
    from server.parsers.pcb_doc import ALTIUM_UNITS_PER_MIL, PcbRule

# ---------- query language ----------

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:[^']|'')*')"
    r"|(?P<number>-?\d+(?:\.\d+)?(?![A-Za-z_]))"
    r"|(?P<op>&&|\|\||<>|<=|>=|[()=<>,])"
    r"|(?P<ident>[A-Za-z0-9_][A-Za-z0-9_.]*)"
    r")"
)


class ScopeSyntaxError(ValueError):
    pass


@dataclass
class _Token:
    kind: str  # string | number | op | ident | end
    value: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN_RE.match(text, position)
        if not match or match.end() == position:
            raise ScopeSyntaxError(
                f"unexpected character {text[position:].strip()[:1]!r}"
            )
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "string":
            value = value[1:-1].replace("''", "'")
        tokens.append(_Token(kind, value, match.start(kind), match.end()))
        position = match.end()
    tokens.append(_Token("end", "", len(text), len(text)))
    return tokens


# AST nodes are tuples whose last element is the source text of the node:
#   ("const", bool, text) | ("call", name_lower, args, text)
#   ("cmp", prop_lower, op, value, text) | ("not", node, text)
#   ("and" | "or" | "xor", left, right, text)

_COMPARISON_OPS = {"=", "<>", "<", ">", "<=", ">=", "like"}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.position = 0

    def peek(self) -> _Token:
        return self.tokens[self.position]

    def take(self) -> _Token:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def word(self) -> str:
        token = self.peek()
        return token.value.lower() if token.kind == "ident" else ""

    def source(self, start: int) -> str:
        end = self.tokens[self.position - 1].end
        return self.text[start:end].strip()

    def parse(self):
        node = self.parse_loose_or()
        if self.peek().kind != "end":
            raise ScopeSyntaxError(f"unexpected {self.peek().value!r}")
        return node

    # Precedence, loosest first: && and || (one level, left to right), then
    # Or/Xor, then And, then Not — as in Altium's query-language reference.
    def parse_loose_or(self):
        start = self.peek().start
        node = self.parse_or()
        while self.peek().kind == "op" and self.peek().value in ("&&", "||"):
            operator = "and" if self.take().value == "&&" else "or"
            node = (operator, node, self.parse_or(), self.source(start))
        return node

    def parse_or(self):
        start = self.peek().start
        node = self.parse_and()
        while self.word() in ("or", "xor"):
            operator = self.take().value.lower()
            node = (operator, node, self.parse_and(), self.source(start))
        return node

    def parse_and(self):
        start = self.peek().start
        node = self.parse_not()
        while self.word() == "and":
            self.take()
            node = ("and", node, self.parse_not(), self.source(start))
        return node

    def parse_not(self):
        start = self.peek().start
        if self.word() == "not":
            self.take()
            operand = self.parse_not()
            return ("not", operand, self.source(start))
        return self.parse_comparison()

    def parse_comparison(self):
        start = self.peek().start
        node = self.parse_primary()
        token = self.peek()
        operator = (
            token.value
            if token.kind == "op" and token.value in _COMPARISON_OPS
            else ("like" if self.word() == "like" else None)
        )
        if operator is None:
            return node
        self.take()
        value_token = self.take()
        if value_token.kind not in ("string", "number", "ident"):
            raise ScopeSyntaxError("comparison needs a value")
        prop = node[1] if node[0] == "call" and not node[2] else "?"
        return ("cmp", prop, operator, value_token.value, self.source(start))

    def parse_primary(self):
        start = self.peek().start
        token = self.take()
        if token.kind == "op" and token.value == "(":
            node = self.parse_loose_or()
            closing = self.take()
            if closing.kind != "op" or closing.value != ")":
                raise ScopeSyntaxError("missing ')'")
            return node
        if token.kind != "ident":
            raise ScopeSyntaxError(f"unexpected {token.value!r}")
        name = token.value.lower()
        if name in ("true", "false"):
            return ("const", name == "true", self.source(start))
        args: list[str] = []
        if self.peek().kind == "op" and self.peek().value == "(":
            self.take()
            if not (self.peek().kind == "op" and self.peek().value == ")"):
                while True:
                    argument = self.take()
                    if argument.kind not in ("string", "number", "ident"):
                        raise ScopeSyntaxError("bad argument")
                    args.append(argument.value)
                    separator = self.take()
                    if separator.kind == "op" and separator.value == ")":
                        break
                    if separator.kind != "op" or separator.value != ",":
                        raise ScopeSyntaxError("expected ',' or ')'")
            else:
                self.take()
        return ("call", name, args, self.source(start))


_PARSE_CACHE: dict[str, tuple] = {}


def parse_scope(text: str):
    """Parse a scope expression into an AST; raises ScopeSyntaxError."""
    if text not in _PARSE_CACHE:
        _PARSE_CACHE[text] = _Parser(text).parse()
    return _PARSE_CACHE[text]


def _wildcard_matches(pattern: str, value: str) -> bool:
    if "*" not in pattern and "?" not in pattern:
        return pattern.lower() == value.lower()
    regex = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(regex, value, re.IGNORECASE) is not None


def _any_matches(patterns: list[str], values) -> bool:
    return any(
        _wildcard_matches(pattern, value)
        for pattern in patterns
        for value in values
    )


# ---------- evaluation ----------


@dataclass
class RuleContext:
    """Lookup tables the evaluator needs, built once per PCB index."""

    net_classes: dict[str, set[str]] = field(default_factory=dict)
    pair_classes: dict[str, set[str]] = field(default_factory=dict)
    component_classes: dict[str, set[str]] = field(default_factory=dict)
    pad_classes: dict[str, set[str]] = field(default_factory=dict)
    polygon_classes: dict[str, set[str]] = field(default_factory=dict)  # pour names
    superclasses: dict[str, set[str]] = field(default_factory=dict)
    pairs: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    net_names: dict[str, str] = field(default_factory=dict)
    rooms: set[str] = field(default_factory=set)
    # Room outlines (lower name -> vertices in mil) for object-level checks.
    room_shapes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # Layer names (display, standard and positional, lower) -> v6 id.
    layer_ids: dict[str, int] = field(default_factory=dict)
    # xSignal name (lower) -> its nets (lower); xSignal classes -> members.
    xsignals: dict[str, set[str]] = field(default_factory=dict)
    xsignal_classes: dict[str, set[str]] = field(default_factory=dict)


MULTI_LAYER = 74


@dataclass(frozen=True)
class ObjectFacts:
    """One copper object, so object scopes (IsVia, OnLayer, WithinRoom...)
    can be decided exactly. Names are lower-case.

    layer is the object's Layer property (v6 id; 74 = Multi-Layer for vias and
    through-hole pads); span is every copper layer it occupies. A polygon is
    a pour itself (the polygon side of a Polygon Connect Style rule).
    """

    kind: str  # "track" | "arc" | "via" | "pad" | "polygon"
    layer: int
    span: frozenset = frozenset()
    points: tuple = ()  # track/arc endpoints; via/pad centre; polygon outline
    in_pour: bool = False  # a hatched pour's track or arc (InPolygon)
    # The pour's name for a polygon, or its pour's for in_pour copper; None
    # when the pour has no NAME.
    polygon: str | None = None
    component: str | None = None
    footprint: str | None = None
    pad: str | None = None  # "u1-5"
    hole: float = 0.0


@dataclass
class RuleTarget:
    """A net, differential pair or xSignal, with the facts its scopes are
    tested on.

    Names are lower-case. components/footprints/pads describe the pads the
    target's nets connect to, so component-scoped rules can be ruled out. A
    net-kind target with no nets stands for objects that carry no net.
    """

    kind: str  # "net" | "diff_pair" | "xsignal"
    name: str
    nets: set[str]
    pairs: set[str]
    components: set[str] = field(default_factory=set)
    footprints: set[str] = field(default_factory=set)
    pads: set[str] = field(default_factory=set)
    has_pours: bool = False
    # Names of the nets' unshelved pours (None for a pour without NAME), so
    # InNamedPolygon can be decided on their copper (pour_view).
    pour_names: set[str | None] = field(default_factory=set)
    # Evaluation views (set by evaluate_scope / resolve_kind / callers that
    # check one object): member — evaluate a pair as one of its nets;
    # pour_view — the net's polygon pour copper instead of its routing;
    # obj — one copper object of the target.
    member: str | None = None
    pour_view: bool = False
    obj: ObjectFacts | None = None


# Result: (value, conditions) with value True / False / None (unknown).
_TRUE = (True, ())
_FALSE = (False, ())


def _unknown(node) -> tuple:
    return (None, (node[-1],))


def _class_member(
    ctx: RuleContext,
    classes: dict[str, set[str]],
    kind: str,
    class_patterns: list[str],
    names: set[str],
) -> bool:
    """Is any of names in a class matching one of class_patterns?"""
    if not names:
        return False
    for pattern in class_patterns:
        for class_name in ctx.superclasses.get(kind, set()):
            if _wildcard_matches(pattern, class_name):
                return True
        for class_name, members in classes.items():
            if _wildcard_matches(pattern, class_name) and members & names:
                return True
    return False


_COMPONENT_ONLY = {
    "isbga", "islcc", "issoic", "isthrucomponent", "issmtcomponent",
    "iscomponent", "iscomment", "isdesignator", "istext",
    "iskeepout", "iscoordinate", "isdimension", "isroom",
}
_POLYGON_ONLY = {
    "inpoly", "inpolygon", "ispoly", "ispolygon", "innamedpolygon",
    "isnamedpolygon", "inpolygonclass", "issplitplane",
}
_OBJECT_TYPES = {
    "isvia", "ispad", "istrack", "isarc", "isfill", "isregion",
    "isthrupin", "issmtpin", "isthrupad", "issmtpad",
}
_LAYER_PREDICATES = {
    "onlayer", "ontoplayer", "onbottomlayer", "onmultilayer", "onmid",
    "onplane", "onsignal", "oncopper", "onoutside", "oninside",
    "existsonlayer",
}
_XSIGNAL_PREDICATES = {"inxsignal", "inxsignalclass", "inanyxsignal", "isxsignal"}
_REGION_PREDICATES = {
    "withinroom", "touchesroom", "inroom", "inregion", "inregionabsolute",
    "inregionrelative", "touchesregion",
}


def point_in_polygon(x: float, y: float, vertices) -> bool:
    inside = False
    count = len(vertices)
    for index in range(count):
        x1, y1 = vertices[index]
        x2, y2 = vertices[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _object_anchor(obj: ObjectFacts) -> tuple[float, float] | None:
    """Point that decides room/region membership: a via or pad centre, the
    midpoint of a track or arc chord (Altium tests pads and vias by their
    centre; whole-object containment for straddling tracks is approximated)."""
    if not obj.points:
        return None
    xs = [point[0] for point in obj.points]
    ys = [point[1] for point in obj.points]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _polygon_region_call(node, obj: ObjectFacts, ctx: RuleContext) -> tuple:
    """A room or region term on a polygon (its outline in obj.points): false
    when the outline's bounds miss every room or region it names, else unknown
    (how much of a pour lies in a room is not measured)."""
    name, args = node[1], node[2]
    if name in ("withinroom", "touchesroom", "inroom"):
        shapes = [
            vertices
            for room, vertices in ctx.room_shapes.items()
            if _any_matches(args, {room})
        ]
        if not shapes:
            return _FALSE if args and not _any_matches(args, ctx.rooms) else _unknown(node)
        boxes = [
            (min(x for x, _ in vertices), min(y for _, y in vertices),
             max(x for x, _ in vertices), max(y for _, y in vertices))
            for vertices in shapes
        ]
    elif name in ("inregion", "inregionabsolute") and len(args) == 4:
        try:
            x1, y1, x2, y2 = (float(str(arg).lower().replace("mil", "")) for arg in args)
        except ValueError:
            return _unknown(node)
        boxes = [(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))]
    else:
        return _unknown(node)
    if not obj.points:
        return _unknown(node)
    minx, maxx = min(x for x, _ in obj.points), max(x for x, _ in obj.points)
    miny, maxy = min(y for _, y in obj.points), max(y for _, y in obj.points)
    if all(
        box[2] < minx or maxx < box[0] or box[3] < miny or maxy < box[1] for box in boxes
    ):
        return _FALSE
    return _unknown(node)


def scope_name(name: str | None) -> str | None:
    """A name as scopes can match it: lower-case, or None when it is not read
    or holds a character above U+00FF, which scope and class text (read as
    Latin-1) cannot hold."""
    if name is None or any(ord(char) > 0xFF for char in name):
        return None
    return name.lower()


def _named_pour(node, names) -> tuple:
    """Does IsNamedPolygon / InNamedPolygon name the pour, or every one of
    the pours, whose names (lower; None when not read) are given? Unknown
    when only some match or a name is not read."""
    hits = {None if name is None else _any_matches(node[2], {name}) for name in names}
    if hits == {True}:
        return _TRUE
    if hits == {False}:
        return _FALSE
    return _unknown(node)


def _pour_copper_call(node, names) -> tuple:
    """A polygon term on a pour's copper (its child primitives, which rules
    are applied to), names being those of the pours it may belong to.
    InPolygon returns them, InNamedPolygon those of the pours it names;
    IsPolygon, IsNamedPolygon and InPolygonClass return the polygon object
    only. IsSplitPlane needs the pour's type, which is not used here."""
    name = node[1]
    if name in ("inpoly", "inpolygon"):
        return _TRUE
    if name == "innamedpolygon":
        return _named_pour(node, names)
    if name == "issplitplane":
        return _unknown(node)
    return _FALSE


_UNDECIDED_COMPONENT = {"isbga", "islcc", "issoic"}


def _component_call(node, obj: ObjectFacts, ctx: RuleContext) -> tuple | None:
    """A predicate on a component object (kind "component": layer 1 or 32 for its side,
    points the corners of its extent, hole its largest pad hole), for the placement
    rules (Height, ComponentClearance) that select components. None: decided as for
    other objects (InComponent, HasFootprint, OnTopLayer ...)."""
    name, args = node[1], node[2]
    if name == "iscomponent":
        return _TRUE
    if name in ("isthrucomponent", "issmtcomponent"):
        # A part with any hole in its pads is a through-hole component here.
        through = obj.hole > 0
        return _TRUE if through == (name == "isthrucomponent") else _FALSE
    if name in _UNDECIDED_COMPONENT:
        return _unknown(node)
    if name in _COMPONENT_ONLY or name in _OBJECT_TYPES or name in _POLYGON_ONLY \
            or name == "isfree":
        return _FALSE
    if name in ("withinroom", "inroom", "touchesroom"):
        shapes = [vertices for room, vertices in ctx.room_shapes.items()
                  if _any_matches(args, {room})]
        if not shapes:
            return _FALSE if args and not _any_matches(args, ctx.rooms) else _unknown(node)
        if not obj.points:
            return _unknown(node)
        if name == "touchesroom":
            hit = any(point_in_polygon(x, y, vertices)
                      for vertices in shapes for x, y in obj.points)
        else:  # the whole extent inside the room
            hit = any(all(point_in_polygon(x, y, vertices) for x, y in obj.points)
                      for vertices in shapes)
        return _TRUE if hit else _FALSE
    return None


def _eval_object_call(node, obj: ObjectFacts, ctx: RuleContext) -> tuple | None:
    """Decide an object predicate for one object; None if not an object term."""
    name, args = node[1], node[2]
    if obj.kind == "component":
        decided = _component_call(node, obj, ctx)
        if decided is not None:
            return decided
    if name in _OBJECT_TYPES:
        if name in ("isvia", "ispad", "istrack", "isarc"):
            return _TRUE if obj.kind == name[2:] else _FALSE
        if name in ("isfill", "isregion"):
            return _FALSE
        # Altium decides IsThruPin/IsSMTPin (like IsThruPad/IsSMTPad) by the
        # pad's hole size alone: a free pad (mounting hole, test point) is a
        # pin too.
        is_pad = obj.kind == "pad"
        if name in ("isthrupad", "isthrupin"):
            return _TRUE if is_pad and obj.hole > 0 else _FALSE
        return _TRUE if is_pad and obj.hole <= 0 else _FALSE
    if name == "isfree":
        # A free primitive is one that is not part of a component.
        return _TRUE if obj.component is None else _FALSE
    if name in _POLYGON_ONLY and obj.kind == "polygon":
        # The pour itself, never a split plane. IsNamedPolygon and
        # InNamedPolygon (which returns the parent pour too) test its name,
        # InPolygonClass its classes, whose members are pour names.
        if name in ("ispoly", "ispolygon"):
            return _TRUE
        if name == "issplitplane":
            return _FALSE
        if name in ("isnamedpolygon", "innamedpolygon"):
            return _named_pour(node, {obj.polygon})
        if name == "inpolygonclass":
            # All Polygons holds a pour whose name is not read, too.
            member = _class_member(
                ctx, ctx.polygon_classes, "polygon", args, {obj.polygon or ""}
            )
            if member:
                return _TRUE
            return _unknown(node) if obj.polygon is None else _FALSE
        return _unknown(node)
    if name in _POLYGON_ONLY and obj.in_pour:
        return _pour_copper_call(node, {obj.polygon})
    if name in _LAYER_PREDICATES:
        layer = obj.layer
        if name in ("onlayer", "existsonlayer"):
            ids = [ctx.layer_ids.get(str(arg).lower()) for arg in args]
            known = [layer_id for layer_id in ids if layer_id is not None]
            if not known:
                return _unknown(node)
            if name == "onlayer":
                return _TRUE if layer in known else _FALSE
            span = obj.span or frozenset({layer})
            return _TRUE if any(layer_id in span for layer_id in known) else _FALSE
        checks = {
            "ontoplayer": layer == 1,
            "onbottomlayer": layer == 32,
            "onmid": 2 <= layer <= 31,
            "onmultilayer": layer == MULTI_LAYER,
            "onsignal": 1 <= layer <= 32 or layer == MULTI_LAYER,
            "oncopper": 1 <= layer <= 32 or layer == MULTI_LAYER or 39 <= layer <= 54,
            "onplane": 39 <= layer <= 54,
            "onoutside": layer in (1, 32),
        }
        if name in checks:
            return _TRUE if checks[name] else _FALSE
        return _unknown(node)
    if name in _REGION_PREDICATES and obj.kind == "polygon":
        return _polygon_region_call(node, obj, ctx)
    if name in ("withinroom", "touchesroom", "inroom"):
        anchor = _object_anchor(obj)
        shapes = [
            vertices
            for room, vertices in ctx.room_shapes.items()
            if _any_matches(args, {room})
        ]
        if not shapes:
            return _FALSE if args and not _any_matches(args, ctx.rooms) else _unknown(node)
        if anchor is None:
            return _unknown(node)
        points = [anchor] + list(obj.points) if name == "touchesroom" else [anchor]
        return _TRUE if any(
            point_in_polygon(x, y, vertices)
            for vertices in shapes
            for x, y in points
        ) else _FALSE
    if name in ("inregion", "inregionabsolute") and len(args) == 4:
        try:
            x1, y1, x2, y2 = (float(str(arg).lower().replace("mil", "")) for arg in args)
        except ValueError:
            return _unknown(node)
        anchor = _object_anchor(obj)
        if anchor is None:
            return _unknown(node)
        inside = (
            min(x1, x2) <= anchor[0] <= max(x1, x2)
            and min(y1, y2) <= anchor[1] <= max(y1, y2)
        )
        return _TRUE if inside else _FALSE
    if name == "incomponent":
        return _TRUE if obj.component and _any_matches(args, {obj.component}) else _FALSE
    if name == "hasfootprint":
        return _TRUE if obj.footprint and _any_matches(args, {obj.footprint}) else _FALSE
    if name == "inanycomponent":
        return _TRUE if obj.component else _FALSE
    if name == "incomponentclass":
        if not obj.component:
            return _FALSE
        member = _class_member(
            ctx, ctx.component_classes, "component", args, {obj.component}
        )
        return _TRUE if member else _FALSE
    if name == "haspad":
        return _TRUE if obj.pad and _any_matches(args, {obj.pad}) else _FALSE
    if name == "inpadclass":
        if not obj.pad:
            return _FALSE
        member = _class_member(ctx, ctx.pad_classes, "pad", args, {obj.pad})
        return _TRUE if member else _FALSE
    return None


def _eval_xsignal_call(node, target: RuleTarget, ctx: RuleContext) -> tuple:
    name, args = node[1], node[2]
    if target.kind == "xsignal":
        own = {target.name.lower()}
        if name == "isxsignal" or name == "inanyxsignal":
            return _TRUE
        if name == "inxsignal":
            return _TRUE if _any_matches(args, own) else _FALSE
        member = _class_member(ctx, ctx.xsignal_classes, "xsignal", args, own)
        return _TRUE if member else _FALSE
    if name == "isxsignal":
        return _FALSE
    # A net or pair: the rule covers only the copper of xSignals running
    # through it, if any.
    if name == "inanyxsignal":
        names = {
            xsignal for xsignal, nets in ctx.xsignals.items() if nets & target.nets
        }
    elif name == "inxsignal":
        names = {
            xsignal
            for xsignal, nets in ctx.xsignals.items()
            if nets & target.nets and _any_matches(args, {xsignal})
        }
    else:
        names = {
            xsignal
            for xsignal, nets in ctx.xsignals.items()
            if nets & target.nets
            and _class_member(ctx, ctx.xsignal_classes, "xsignal", args, {xsignal})
        }
    return _unknown(node) if names else _FALSE


def _net_membership(node, target: RuleTarget, test) -> tuple:
    """Decide a net-membership test; a pair matches when all its nets do."""
    nets = {target.member} if target.member else target.nets
    hits = [test({net}) for net in sorted(nets)]
    if hits and all(hits):
        return _TRUE
    if not any(hits):
        return _FALSE
    return _unknown(node)


def _eval_call(node, target: RuleTarget, ctx: RuleContext) -> tuple:
    name, args = node[1], node[2]
    is_net = target.kind == "net"
    if name == "all":
        return _TRUE
    if name in _XSIGNAL_PREDICATES:
        return _eval_xsignal_call(node, target, ctx)
    if target.obj is not None:
        decided = _eval_object_call(node, target.obj, ctx)
        if decided is not None:
            return decided
    if name == "innet":
        return _net_membership(
            node, target, lambda nets: _any_matches(args, nets)
        )
    if name == "innetclass":
        return _net_membership(
            node,
            target,
            lambda nets: _class_member(
                ctx, ctx.net_classes, "net", args, nets
            ),
        )
    if name == "indifferentialpair":
        return _TRUE if _any_matches(args, target.pairs) else _FALSE
    if name == "indifferentialpairclass":
        member = _class_member(
            ctx, ctx.pair_classes, "diff_pair", args, target.pairs
        )
        return _TRUE if member else _FALSE
    # A target without nets stands for no-net objects (e.g. free vias in the
    # compliance check): InAnyNet and IsNet select nothing there, so
    # "Not InAnyNet" selects them, as in Altium.
    if name == "inanynet":
        return _TRUE if target.nets else _FALSE
    if name == "inanydifferentialpair":
        return _TRUE if target.pairs else _FALSE
    if name == "isnet":
        return _TRUE if is_net and target.obj is None and target.nets else _FALSE
    if name == "isfree":
        # A net's tracks, arcs and vias are free primitives; its component
        # pads are not. Decided per object (see _eval_object_call).
        return _unknown(node) if is_net else _FALSE
    if name == "isdifferentialpair":
        # Tracks of a pair's nets are checked through the pair (member view).
        return _TRUE if target.kind == "diff_pair" else _FALSE
    if name in _POLYGON_ONLY and target.pour_view:
        return _pour_copper_call(node, target.pour_names)
    if name in _COMPONENT_ONLY or name in _POLYGON_ONLY:
        # These select components, text or pour children, never a net's
        # routing or the pair object itself.
        return _FALSE
    if name in ("incomponent", "hasfootprint", "inanycomponent"):
        values = (
            target.components
            if name == "incomponent"
            else target.footprints
        )
        if name == "inanycomponent":
            return _unknown(node) if target.components else _FALSE
        return _unknown(node) if _any_matches(args, values) else _FALSE
    if name == "incomponentclass":
        member = _class_member(
            ctx, ctx.component_classes, "component", args, target.components
        )
        return _unknown(node) if member else _FALSE
    if name == "haspad":
        return _unknown(node) if _any_matches(args, target.pads) else _FALSE
    if name == "inpadclass":
        member = _class_member(
            ctx, ctx.pad_classes, "pad", args, target.pads
        )
        return _unknown(node) if member else _FALSE
    if name in _OBJECT_TYPES:
        return _unknown(node) if is_net else _FALSE
    if name in _LAYER_PREDICATES:
        # Layer names in scopes may be renamed or standard names; never
        # rule a layer out, just report the condition.
        return _unknown(node)
    if name in _REGION_PREDICATES:
        if (
            name in ("withinroom", "touchesroom", "inroom")
            and args
            and not _any_matches(args, ctx.rooms)
        ):
            return _FALSE
        return _unknown(node)
    # xSignals, from-tos and anything not modelled: decided elsewhere.
    return _unknown(node)


def _eval_cmp(node, target: RuleTarget) -> tuple:
    prop, value = node[1], str(node[3]).lower()
    obj = target.obj
    if obj is not None and prop == "objectkind":
        if node[2] == "=":
            return _TRUE if value == obj.kind else _FALSE
        if node[2] == "<>":
            return _FALSE if value == obj.kind else _TRUE
    if obj is not None and obj.kind == "polygon" and prop == "name":
        # A pour's own name. Like takes wildcards; = and <> compare the whole
        # name, so where only a wildcard reading of * or ? matches, unknown.
        operator, pattern = node[2], str(node[3])
        if obj.polygon is None or operator not in ("=", "like", "<>"):
            return _unknown(node)
        if operator == "like":
            matched = _wildcard_matches(pattern, obj.polygon)
        elif pattern.lower() == obj.polygon:
            matched = True
        elif _wildcard_matches(pattern, obj.polygon):
            return _unknown(node)
        else:
            matched = False
        if operator == "<>":
            return _FALSE if matched else _TRUE
        return _TRUE if matched else _FALSE
    if obj is not None and obj.kind == "component" and prop in ("name", "designator") \
            and node[2] in ("=", "like", "<>"):
        # A component's Name is its designator.
        matched = bool(obj.component) and _wildcard_matches(str(node[3]), obj.component)
        if node[2] == "<>":
            return _FALSE if matched else _TRUE
        return _TRUE if matched else _FALSE
    if obj is not None and obj.kind == "component" and prop == "comppincount":
        return _unknown(node)
    if obj is not None and prop == "name" and node[2] in ("=", "like", "<>"):
        # A pad's Name is "<designator>-<pad>"; other routed objects have none.
        matched = bool(obj.pad) and _wildcard_matches(str(node[3]), obj.pad)
        if node[2] == "<>":
            return _FALSE if matched else _TRUE
        return _TRUE if matched else _FALSE
    if prop == "objectkind":
        if value == "net":
            return _TRUE if target.kind == "net" and target.nets else _FALSE
        if value in ("differential pair", "differentialpair"):
            return _TRUE if target.kind == "diff_pair" else _FALSE
        if value in ("text", "component", "dimension", "coordinate", "room",
                     "polygon", "embedded board", "board"):
            return _FALSE
        return _unknown(node) if target.kind == "net" else _FALSE
    if prop in ("stringtext", "hide", "textheight", "textwidth"):
        return _FALSE
    if prop == "comppincount":
        return _unknown(node) if target.components else _FALSE
    if prop == "name" and target.kind == "diff_pair":
        # The pair wizard scopes rules as IsDifferentialPair And (Name = 'X').
        # For nets Name may mean a pad or a primitive's name, so stay unknown.
        operator = node[2]
        if operator in ("=", "like"):
            return _TRUE if _wildcard_matches(str(node[3]), target.name) else _FALSE
        if operator == "<>":
            return _FALSE if _wildcard_matches(str(node[3]), target.name) else _TRUE
    return _unknown(node)


def evaluate(node, target: RuleTarget, ctx: RuleContext) -> tuple:
    """Kleene-logic evaluation: (True | False | None, conditions)."""
    tag = node[0]
    if tag == "const":
        return _TRUE if node[1] else _FALSE
    if tag == "call":
        return _eval_call(node, target, ctx)
    if tag == "cmp":
        return _eval_cmp(node, target)
    if tag == "not":
        value, conditions = evaluate(node[1], target, ctx)
        if value is None:
            return (None, (node[-1],))
        return (not value, ())
    left = evaluate(node[1], target, ctx)
    right = evaluate(node[2], target, ctx)
    if tag == "and":
        if left[0] is False or right[0] is False:
            return _FALSE
        if left[0] and right[0]:
            return _TRUE
        return (None, left[1] + right[1])
    if tag == "or":
        if left[0] or right[0]:
            return _TRUE
        if left[0] is False and right[0] is False:
            return _FALSE
        return (None, left[1] + right[1])
    # xor
    if left[0] is None or right[0] is None:
        return (None, left[1] + right[1])
    return (left[0] != right[0], ())


def evaluate_scope(text: str, target: RuleTarget, ctx: RuleContext) -> dict:
    """Evaluate one scope string; never raises."""
    try:
        node = parse_scope(text)
    except ScopeSyntaxError as error:
        return {
            "matches": "unknown",
            "conditions": [text],
            "parse_error": str(error),
        }
    value, conditions = evaluate(node, target, ctx)
    if value is None and target.kind == "diff_pair" and not target.member:
        # "InNet('A_P') or InNet('A_N')" is unknown term by term but true for
        # each member net, so it covers the whole pair.
        per_member = [
            evaluate(node, replace(target, member=net), ctx)[0]
            for net in sorted(target.nets)
        ]
        if per_member and all(result is True for result in per_member):
            value = True
        elif per_member and all(result is False for result in per_member):
            value = False
    if value is None:
        return {
            "matches": "conditional",
            "conditions": list(dict.fromkeys(conditions)),
        }
    return {"matches": "yes" if value else "no"}


# Membership functions whose arguments name objects that must exist.
_REFERENCE_FUNCTIONS = {
    "innet": "nets",
    "innetclass": "net_classes",
    "indifferentialpair": "pairs",
    "indifferentialpairclass": "pair_classes",
    "incomponentclass": "component_classes",
    "inpadclass": "pad_classes",
    "withinroom": "rooms",
    "touchesroom": "rooms",
    "inxsignal": "xsignals",
    "inxsignalclass": "xsignal_classes",
}


def stale_references(text: str, ctx: RuleContext) -> list[str]:
    """Names in a scope that match no net, class, pair or room on the board.

    Such a term matches nothing, which usually means the rule is stale (the
    class or pair was renamed or deleted) and silently not applied.
    """
    try:
        node = parse_scope(text)
    except ScopeSyntaxError:
        return []
    existing = {
        "nets": set(ctx.net_names),
        "net_classes": set(ctx.net_classes) | ctx.superclasses.get("net", set()),
        "pairs": set(ctx.pairs),
        "pair_classes": (
            set(ctx.pair_classes) | ctx.superclasses.get("diff_pair", set())
        ),
        "component_classes": (
            set(ctx.component_classes)
            | ctx.superclasses.get("component", set())
        ),
        "pad_classes": set(ctx.pad_classes) | ctx.superclasses.get("pad", set()),
        "rooms": ctx.rooms,
        "xsignals": set(ctx.xsignals),
        "xsignal_classes": (
            set(ctx.xsignal_classes) | ctx.superclasses.get("xsignal", set())
        ),
    }
    missing: list[str] = []

    def walk(current):
        if current[0] == "call":
            table = _REFERENCE_FUNCTIONS.get(current[1])
            if table:
                for argument in current[2]:
                    if not any(
                        _wildcard_matches(argument, name)
                        for name in existing[table]
                    ):
                        missing.append(f"{current[1]}('{argument}')")
        elif current[0] in ("not",):
            walk(current[1])
        elif current[0] in ("and", "or", "xor"):
            walk(current[1])
            walk(current[2])

    walk(node)
    return list(dict.fromkeys(missing))


# ---------- rule kinds ----------

# Binary rules test a pair of objects; scope1 and scope2 each pick one side.
BINARY_KINDS = frozenset({
    "Clearance", "ComponentClearance", "HoleToHoleClearance",
    "ShortCircuit", "Creepage", "PolygonConnect", "SilkToSilkClearance",
    "SilkToSolderMaskClearance", "MinimumSolderMaskSliver",
    "BoardOutlineClearance", "SilkToBoardRegionClearance",
})

# Kinds that concern a net's routing; get_rules_for_net reports these.
NET_RULE_KINDS = (
    "Clearance", "Width", "RoutingVias", "DiffPairsRouting", "Length",
    "MatchedLengths", "RoutingLayers", "RoutingTopology", "RoutingPriority",
    "RoutingCorners", "PlaneConnect", "PlaneClearance", "PolygonConnect",
    "HoleSize", "ShortCircuit", "UnRoutedNet", "NetAntennae", "SupplyNets",
    "Creepage", "BackDrilling",
)

# Kinds where every matching rule applies, not just the highest priority.
ALL_MATCHING_KINDS = frozenset({"MatchedLengths"})

ROOM_KIND = "RoomDefinition"

# Testpoint rules: the style rules (Fabrication / Assembly Testpoint) set the testpoint's
# pad or via size and hole, the sides allowed, its grid and whether it may sit under a
# component; the usage rules (Fabrication / Assembly Testpoint Usage) say per net whether
# a testpoint is required, prohibited or don't care. Testpoint and TestPointUsage are the
# kinds' names before Altium split fabrication from assembly. SIDE (3 on every style rule
# read, where both ALLOWSIDE keys are TRUE) stays under other.
TESTPOINT_STYLE_KINDS = ("FabricationTestpoint", "AssemblyTestpoint", "Testpoint")
TESTPOINT_USAGE_KINDS = (
    "FabricationTestPointUsage", "AssemblyTestPointUsage", "TestPointUsage",
)
_TESTPOINT_STYLE = {
    "MINSIZE": ("min_size_mil", "mil"),
    "PREFEREDSIZE": ("preferred_size_mil", "mil"),
    "MAXSIZE": ("max_size_mil", "mil"),
    "MINHOLESIZE": ("min_hole_mil", "mil"),
    "PREFEREDHOLESIZE": ("preferred_hole_mil", "mil"),
    "MAXHOLESIZE": ("max_hole_mil", "mil"),
    "ALLOWSIDETOP": ("allow_top", "bool"),
    "ALLOWSIDEBOTTOM": ("allow_bottom", "bool"),
    "USEGRID": ("use_grid", "bool"),
    "TESTPOINTGRID": ("grid_mil", "mil"),
    "GRIDTOLERANCE": ("grid_tolerance_mil", "mil"),
    "TESTPOINTUNDERCOMPONENT": ("allow_under_component", "bool"),
}
_TESTPOINT_USAGE = {
    "VALID": ("usage", "usage"),
    "ALLOWMULTIPLE": ("allow_multiple", "bool"),
}
# Usage rule VALID: 0 on Altium's default rule (scope All, which Altium creates as
# Required: 100 of 104 fabrication usage rules of the private set and all 6 of the
# examples), 2 on the 4 others read; 1 and 2 follow the dialog's order (Required,
# Prohibited, Don't Care). Assembly usage rules at the default carry no VALID: required.
TESTPOINT_USAGE = {"0": "required", "1": "prohibited", "2": "dont_care"}

# Explicitly named values per kind: raw key -> (output name, type).
# Types: mil (length string), ohm, float, int, bool, str. Unlisted keys
# still appear, under other / other_mil.
_KIND_VALUES: dict[str, dict[str, tuple[str, str]]] = {
    "Clearance": {
        "GENERICCLEARANCE": ("clearance_mil", "mil"),
        "IGNOREPADTOPADCLEARANCEINFOOTPRINT": (
            "ignore_pad_to_pad_in_footprint", "bool",
        ),
        "GAP": ("", "skip"),
    },
    "BoardOutlineClearance": {
        "GENERICCLEARANCE": ("clearance_mil", "mil"),
        "GAP": ("", "skip"),
    },
    "Width": {
        "MINLIMIT": ("min_width_mil", "mil"),
        "PREFEREDWIDTH": ("preferred_width_mil", "mil"),
        "MAXLIMIT": ("max_width_mil", "mil"),
        # Legacy keys saved with a 50 ohm default whatever the rule targets;
        # the real target is the impedance profile below.
        "MINIMP": ("", "skip"),
        "FAVIMP": ("", "skip"),
        "MAXIMP": ("", "skip"),
        "IMPEDANCEPROFILEDRIVEN": ("impedance_profile_driven", "bool"),
        "IMPEDANCEPROFILEVALUE": ("impedance_profile_ohm", "ohm"),
        "IMPEDANCEPROFILEID": ("", "skip"),
    },
    "DiffPairsRouting": {
        "MINLIMIT": ("min_gap_mil", "mil"),
        "MOSTFREQGAP": ("preferred_gap_mil", "mil"),
        "MAXLIMIT": ("max_gap_mil", "mil"),
        "MAXUNCOUPLEDLENGTH": ("max_uncoupled_length_mil", "mil"),
        "IMPEDANCEPROFILEDRIVEN": ("impedance_profile_driven", "bool"),
        "IMPEDANCEPROFILEVALUE": ("impedance_profile_ohm", "ohm"),
        "IMPEDANCEPROFILEID": ("", "skip"),
    },
    "RoutingVias": {
        "MINWIDTH": ("min_diameter_mil", "mil"),
        "WIDTH": ("preferred_diameter_mil", "mil"),
        "MAXWIDTH": ("max_diameter_mil", "mil"),
        "MINHOLEWIDTH": ("min_hole_mil", "mil"),
        "HOLEWIDTH": ("preferred_hole_mil", "mil"),
        "MAXHOLEWIDTH": ("max_hole_mil", "mil"),
        "VIASTYLE": ("via_style", "str"),
    },
    "Length": {
        "MINLIMIT": ("min_length_mil", "mil"),
        "MAXLIMIT": ("max_length_mil", "mil"),
        "USEDELAYUNITS": ("use_delay_units", "bool"),
    },
    "MatchedLengths": {
        "TOLERANCE": ("tolerance_mil", "mil"),
        # "Within Differential Pair Length" vs "Group Matched Lengths"; the
        # group flags split the group by object type.
        "CHECKNETSINDIFFPAIR": ("within_pair", "bool"),
        "CHECKDIFFPAIRVSDIFFPAIR": ("group_diff_pairs", "bool"),
        "CHECKXSIGNALS": ("group_xsignals", "bool"),
        "CHECKOTHERS": ("group_other_nets", "bool"),
        "USEDELAYUNITS": ("use_delay_units", "bool"),
        "TARGETSOURCENAME": ("target_source", "str"),
        "STYLE": ("", "skip"),
        "AMPLITUDE": ("", "skip"),
    },
    "HoleSize": {
        "MINLIMIT": ("min_hole_mil", "mil"),
        "MAXLIMIT": ("max_hole_mil", "mil"),
        "MINPERCENT": ("min_percent", "float"),
        "MAXPERCENT": ("max_percent", "float"),
        "ABSOLUTEVALUES": ("absolute_values", "bool"),
    },
    "PlaneClearance": {"CLEARANCE": ("clearance_mil", "mil")},
    "Height": {
        "MINHEIGHT": ("min_height_mil", "mil"),
        "PREFHEIGHT": ("preferred_height_mil", "mil"),
        "MAXHEIGHT": ("max_height_mil", "mil"),
    },
    "MinimumAnnularRing": {"MINIMUMRING": ("min_ring_mil", "mil")},
    "HoleToHoleClearance": {
        "GAP": ("clearance_mil", "mil"),
        "ALLOWSTACKEDMICROVIAS": ("allow_stacked_microvias", "bool"),
    },
    # Max Via Stub Length (Back Drilling): the back drill is hole + oversize (the
    # help says radial, hole + 2 x oversize, but Altium's own drill table and hole
    # list on the Mini PC example add it once); the max stub length is a DRC limit,
    # not the drill depth.
    "BackDrilling": {
        "MAXSTUBLENGTH": ("max_stub_length_mil", "mil"),
        "HOLEOVERSIZE": ("oversize_mil", "mil"),
        "HOLETOLPLUS": ("tolerance_plus_mil", "mil"),
        "HOLETOLMINUS": ("tolerance_minus_mil", "mil"),
        "USETOPBACKDRILL": ("use_top_back_drill", "bool"),
        "USEBOTTOMBACKDRILL": ("use_bottom_back_drill", "bool"),
    },
    "ViasUnderSMD": {"ALLOWED": ("allowed", "bool")},
    "LayerPairs": {"ENFORCE": ("enforce", "bool")},
    "ComponentClearance": {"GAP": ("clearance_mil", "mil")},
    **{kind: _TESTPOINT_STYLE for kind in TESTPOINT_STYLE_KINDS},
    **{kind: _TESTPOINT_USAGE for kind in TESTPOINT_USAGE_KINDS},
}

_LAYER_KEY_RE = re.compile(
    r"(TOPLAYER|BOTTOMLAYER|MIDLAYER(\d+))_(?:(\{[^}]+\})_)?"
    r"(MINWIDTH|MAXWIDTH|PREFWIDTH|MINGAP|MAXGAP|PREFGAP)"
)
_ROUTING_LAYER_KEY_RE = re.compile(
    r"(TOP LAYER|BOTTOM LAYER|MID LAYER (\d+))_V5"
)
_LAYER_FIELDS = {
    "MINWIDTH": "min_width",
    "PREFWIDTH": "preferred_width",
    "MAXWIDTH": "max_width",
    "MINGAP": "min_gap",
    "PREFGAP": "preferred_gap",
    "MAXGAP": "max_gap",
}
_GAP_FALLBACK = {
    "min_gap": "MINLIMIT",
    "preferred_gap": "MOSTFREQGAP",
    "max_gap": "MAXLIMIT",
}
_WIDTH_FALLBACK = {
    "min_width": "MINLIMIT",
    "preferred_width": "PREFEREDWIDTH",
    "max_width": "MAXLIMIT",
}
_MIL_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?mil$")
_ROOM_VERTEX_RE = re.compile(r"(VX|VY|CX|CY|SA|EA|R|KIND)\d+")


def _mil(raw: str | None) -> float | None:
    if raw is None or not _MIL_VALUE_RE.match(raw.strip()):
        return None
    return float(raw.strip()[:-3])


def _typed(raw: str, kind: str):
    if kind == "mil":
        return _mil(raw)
    if kind == "bool":
        return raw.strip().upper() == "TRUE"
    if kind in ("ohm", "float"):
        try:
            return float(raw)
        except ValueError:
            return None
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return None
    if kind == "usage":
        return TESTPOINT_USAGE.get(raw.strip(), f"unknown ({raw.strip()})")
    return raw


def _v5_layer_id(match: re.Match) -> int:
    """Map a V5 slot token to the board's v6 layer id (1 top, 32 bottom)."""
    if match.group(1).startswith("TOP"):
        return 1
    if match.group(1).startswith("BOTTOM"):
        return 32
    return int(match.group(2)) + 1


def _plain_value(raw: str):
    stripped = raw.strip()
    if stripped.upper() in ("TRUE", "FALSE"):
        return stripped.upper() == "TRUE"
    try:
        number = float(stripped)
    except ValueError:
        return raw
    return int(number) if re.fullmatch(r"-?\d+", stripped) else number


def rule_values(
    rule: PcbRule,
    signal_layers: dict[int, str],
    substacks: list[dict] | None = None,
    profiles: dict[str, dict] | None = None,
) -> dict:
    """Decode a rule's kind-specific values.

    signal_layers maps v6 id -> display name for the board's routable copper
    layers; per-layer values for slots the board does not have are dropped.
    substacks ({"id", "name", "is_flex"}, primary first) names rigid/flex
    per-layer tables. Lengths are in mils under *_mil keys; unrecognised keys
    are kept.
    """
    props = rule.properties
    known = _KIND_VALUES.get(rule.kind, {})
    values: dict = {}
    other_mil: dict[str, float] = {}
    other: dict = {}
    per_layer: dict[int, dict[str, float]] = {}
    substack_layers: dict[str, dict[int, dict[str, float]]] = {}
    routing_layers: dict[str, bool] = {}
    for key, raw in props.items():
        if key in known:
            name, kind = known[key]
            if kind != "skip":
                typed = _typed(raw, kind)
                if typed is not None:
                    values[name] = typed
            continue
        layer_match = _LAYER_KEY_RE.fullmatch(key)
        if layer_match:
            v6_id = _v5_layer_id(layer_match)
            value = _mil(raw)
            if v6_id in signal_layers and value is not None:
                target = (
                    substack_layers.setdefault(layer_match.group(3), {})
                    if layer_match.group(3)
                    else per_layer
                )
                target.setdefault(v6_id, {})[
                    _LAYER_FIELDS[layer_match.group(4)]
                ] = value
            continue
        routing_match = _ROUTING_LAYER_KEY_RE.fullmatch(key)
        if routing_match:
            v6_id = _v5_layer_id(routing_match)
            if v6_id in signal_layers:
                routing_layers[signal_layers[v6_id]] = (
                    raw.strip().upper() == "TRUE"
                )
            continue
        if key == "OBJECTCLEARANCES":
            matrix = _object_clearances(raw)
            if matrix:
                values["object_clearances_mil"] = matrix
            continue
        if rule.kind == ROOM_KIND and _ROOM_VERTEX_RE.fullmatch(key):
            continue
        mil = _mil(raw)
        if mil is not None:
            other_mil[key.lower()] = mil
        else:
            other[key.lower()] = _plain_value(raw)

    if rule.kind in ("Width", "DiffPairsRouting") and (
        per_layer or rule.kind == "DiffPairsRouting"
    ):
        fallback = (
            _GAP_FALLBACK if rule.kind == "DiffPairsRouting" else _WIDTH_FALLBACK
        )
        layers_out: dict[str, dict[str, float]] = {}
        for v6_id, layer_name in signal_layers.items():
            entry = dict(per_layer.get(v6_id, {}))
            for field_name, base_key in fallback.items():
                if field_name not in entry:
                    base = _mil(props.get(base_key))
                    if base is not None:
                        entry[field_name] = base
            if entry:
                layers_out[layer_name] = entry
        if layers_out:
            values["layers_mil"] = layers_out
            # Every routable layer is listed with its resolved value, so the
            # rule-level numbers would only be a misleading average.
            for field_name in fallback:
                values.pop(f"{field_name}_mil", None)
    substack_names = {
        substack["id"].upper(): substack for substack in substacks or []
    }
    if substack_layers:
        named: dict[str, dict] = {}
        for guid, layers in substack_layers.items():
            substack = substack_names.get(guid.upper())
            label = substack["name"] if substack else guid
            named[label] = {
                signal_layers[v6_id]: fields
                for v6_id, fields in layers.items()
            }
        values["substack_layers_mil"] = named
    if (substack_layers or len(substack_names) > 1) and substacks and (
        "layers_mil" in values
    ):
        values["layers_substack"] = substacks[0]["name"]
    if not values.get("impedance_profile_driven"):
        values.pop("impedance_profile_ohm", None)
    profile_id = props.get("IMPEDANCEPROFILEID", "").strip()
    if profile_id:
        profile = (profiles or {}).get(profile_id.upper())
        if profile:
            values["impedance_profile"] = profile
        elif profiles is not None:
            values["impedance_profile"] = {"id": profile_id, "missing": True}
    if routing_layers:
        values["routing_layers"] = routing_layers
    if rule.kind == ROOM_KIND:
        values.update(room_geometry(rule))
    if rule.kind in TESTPOINT_USAGE_KINDS:
        values.setdefault("usage", "required")
    if other_mil:
        values["other_mil"] = other_mil
    if other:
        values["other"] = other
    return values


def _object_clearances(raw: str) -> dict[str, float]:
    """Decode the sparse clearance matrix (raw 1/10000 mil, no unit)."""
    matrix: dict[str, float] = {}
    for cell in raw.split(";"):
        pair, _, amount = cell.partition(":")
        if not amount.strip():
            continue
        try:
            value = int(amount.strip()) / ALTIUM_UNITS_PER_MIL
        except ValueError:
            continue
        names = [
            part.replace("ClearanceObj_", "")
            for part in pair.split("-ClearanceObj_")
        ]
        if len(names) == 2:
            matrix[f"{names[0].replace('ClearanceObj_', '')}-{names[1]}"] = (
                value
            )
    return matrix


def room_vertices(rule: PcbRule) -> list[tuple[float, float]]:
    """Outline of a RoomDefinition rule in board coordinates (mil)."""
    props = rule.properties
    vertices: list[tuple[float, float]] = []
    index = 0
    while f"VX{index}" in props and f"VY{index}" in props:
        x, y = _mil(props[f"VX{index}"]), _mil(props[f"VY{index}"])
        if x is not None and y is not None:
            vertices.append((x, y))
        index += 1
    return vertices


def room_geometry(rule: PcbRule) -> dict:
    """Side, vertex count and bounding box of a RoomDefinition rule."""
    props = rule.properties
    xs: list[float] = []
    ys: list[float] = []
    index = 0
    while f"VX{index}" in props and f"VY{index}" in props:
        x, y = _mil(props[f"VX{index}"]), _mil(props[f"VY{index}"])
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
        index += 1
    geometry: dict = {
        "side": "bottom" if props.get("LAYER", "").upper() == "BOTTOM" else "top",
        "vertex_count": len(xs),
    }
    if xs:
        geometry["bbox_mil"] = {
            "minx_mil": min(xs),
            "miny_mil": min(ys),
            "maxx_mil": max(xs),
            "maxy_mil": max(ys),
        }
    return geometry


# ---------- resolution ----------


# NETSCOPE of binary rules ("Connective Checking" in Altium).
_CONNECTIVE = {
    "DifferentNets": "different nets only",
    "SameNet": "same net only",
    "AnyNet": "any net (including the same net)",
}


def _connective_condition(net_scope: str | None) -> str | None:
    """Extra condition a binary rule's connective checking adds, if any."""
    scope = (net_scope or "").lower()
    if "pair" in scope and "same" in scope:
        return "only between the two nets of the same differential pair"
    if "pair" in scope and "different" in scope:
        return "only between nets of different differential pairs"
    if scope == "samenet":
        return "only between objects of the same net"
    return None


def _scope_is_all(text: str) -> bool:
    return text.strip().lower() == "all"


def _side(rule_scope: str, target: RuleTarget, ctx: RuleContext) -> dict:
    """Evaluate one side of a binary rule, including the net's pour copper."""
    side = evaluate_scope(rule_scope, target, ctx)
    if (
        side["matches"] == "no"
        and target.kind == "net"
        and target.has_pours
    ):
        pour_side = evaluate_scope(
            rule_scope, replace(target, pour_view=True), ctx
        )
        if pour_side["matches"] != "no":
            return {
                "matches": "conditional",
                "conditions": ["only the net's polygon pour copper"]
                + pour_side.get("conditions", []),
            }
    return side


def rule_header(rule: PcbRule) -> dict:
    """Compact identity of a rule for listings."""
    header: dict = {
        "name": rule.name,
        "kind": rule.kind,
        "priority": rule.priority,
        "enabled": rule.enabled,
        "scope": rule.scope1,
    }
    if rule.kind in BINARY_KINDS:
        header["scope2"] = rule.scope2
        if rule.net_scope:
            header["connective_checking"] = _CONNECTIVE.get(
                rule.net_scope, rule.net_scope
            )
    if rule.comment:
        header["comment"] = rule.comment
    if rule.from_schematic:
        header["from_schematic"] = True
    return header


def resolve_kind(
    rules: list[PcbRule],
    target: RuleTarget,
    ctx: RuleContext,
    describe,
) -> dict:
    """Resolve the rules of one kind for a target.

    rules must all be of one kind. describe(rule) renders the effective rule.
    Unary kinds: the first enabled rule by priority whose scope matches
    applies; higher-priority rules that match only conditionally are listed,
    because for some of the target's objects they win instead.
    Binary kinds: the winner depends on the other object, so every rule with
    a side matching the target is listed in priority order.
    """
    kind = rules[0].kind
    ordered = sorted(
        (rule for rule in rules if rule.enabled),
        key=lambda rule: rule.priority,
    )
    result: dict = {"kind": kind}
    if kind in BINARY_KINDS:
        candidates = []
        for rule in ordered:
            sides = (
                ("scope1", _side(rule.scope1, target, ctx), rule.scope2),
                ("scope2", _side(rule.scope2, target, ctx), rule.scope1),
            )
            live = [side for side in sides if side[1]["matches"] != "no"]
            if not live:
                continue
            # Altium tests both orientations of an object pair. One that
            # matches the target fully against All covers the other, as does
            # a symmetric rule; otherwise report each orientation.
            full = [side for side in live if side[1]["matches"] == "yes"]
            if any(_scope_is_all(other) for _, _, other in full):
                live = [next(side for side in full if _scope_is_all(side[2]))]
            elif (
                len(live) == 2
                and rule.scope1.strip().lower() == rule.scope2.strip().lower()
            ):
                live = live[:1]
            connective = _connective_condition(rule.net_scope)
            orientations = []
            for side_name, matched, other in live:
                orientation = {"target_side": side_name, "other_side": other}
                conditions = list(matched.get("conditions", []))
                if connective:
                    conditions.append(connective)
                if conditions:
                    orientation["conditions"] = conditions
                if "parse_error" in matched:
                    orientation["parse_error"] = matched["parse_error"]
                orientations.append(orientation)
            candidate = describe(rule)
            candidate["orientations"] = orientations
            candidates.append(candidate)
        result["candidates"] = candidates
        result["note"] = (
            "Binary rule: for a pair of objects, the first candidate by "
            "priority with an orientation whose target side matches this "
            "object and whose other_side matches the other object applies."
        )
        return result

    effective = []
    conditional = []
    for rule in ordered:
        side = evaluate_scope(rule.scope1, target, ctx)
        if side["matches"] == "no":
            continue
        if side["matches"] == "yes":
            effective.append(describe(rule))
            if kind not in ALL_MATCHING_KINDS:
                break
            continue
        entry = rule_header(rule)
        entry["conditions"] = side["conditions"]
        if "parse_error" in side:
            entry["parse_error"] = side["parse_error"]
        conditional.append(entry)
    if kind in ALL_MATCHING_KINDS:
        result["applicable"] = effective
        if conditional:
            result["applicable_to_some_objects"] = conditional
    else:
        result["effective"] = effective[0] if effective else None
        if conditional:
            # With an effective rule these win for some objects; without one
            # they are the only rules that can apply at all.
            key = (
                "overridden_for_some_objects_by"
                if effective
                else "applicable_to_some_objects"
            )
            result[key] = conditional
    if not effective:
        disabled = [rule.name for rule in rules if not rule.enabled]
        if disabled:
            result["disabled_rules"] = disabled
    return result


class ObjectResolver:
    """Which rule of one kind applies to each object of one net or pair.

    Net-level verdicts are computed once; only rules whose scope is
    conditional at net level are re-evaluated per object (a decided net-level
    verdict cannot change when object facts are added). A target without nets
    (no-net objects) has no net-level facts: its component, pad and footprint
    terms describe nothing, so every rule is decided per object.
    """

    def __init__(self, rules: list[PcbRule], target: RuleTarget, ctx: RuleContext):
        self.target = target
        self.ctx = ctx
        self.rules = sorted(
            (rule for rule in rules if rule.enabled), key=lambda rule: rule.priority
        )
        self.net_level = [
            evaluate_scope(rule.scope1, target, ctx)["matches"]
            if target.nets
            else "conditional"
            for rule in self.rules
        ]

    def resolve(self, obj: ObjectFacts) -> tuple[PcbRule | None, list[dict]]:
        """(rule, undecided): the first rule whose scope matches the object.

        undecided lists higher-priority rules whose scope stayed unknown for
        this object (e.g. an xSignal term); the result is then not certain.
        """
        undecided: list[dict] = []
        object_target = None
        for rule, verdict in zip(self.rules, self.net_level):
            if verdict == "no":
                continue
            if verdict == "yes":
                return rule, undecided
            if object_target is None:
                object_target = replace(self.target, obj=obj)
            side = evaluate_scope(rule.scope1, object_target, self.ctx)
            if side["matches"] == "yes":
                return rule, undecided
            if side["matches"] != "no":
                undecided.append({"rule": rule.name, "conditions": side["conditions"]})
        return None, undecided
