"""Parser for Altium .PcbDoc OLE compound files.

Pure parsing only: OLE streams -> dataclasses. No MCP or index logic here.
All lengths and coordinates are converted to float mils at parse time.
"""

import json
import math
import re
import struct
from dataclasses import dataclass, field

import olefile

ALTIUM_UNITS_PER_MIL = 10000
MM_PER_MIL = 0.0254
NO_NET = 0xFFFF
NO_COMPONENT = 0xFFFF

_DIM_RE = re.compile(r"^\s*(-?[\d.]+(?:[eE][+-]?\d+)?)\s*(mil|mm)?\s*$")


def parse_dim_mil(raw: str | None) -> float | None:
    """Parse an Altium dimension string into mils."""
    if not raw:
        return None
    match = _DIM_RE.match(raw)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if match.group(2) == "mm":
        return value / MM_PER_MIL
    return value


def parse_text_properties(body: bytes) -> dict[str, str]:
    text = body.rstrip(b"\x00").decode("latin-1", "replace")
    properties: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            key, _, value = part.partition("=")
            properties[key.strip().upper()] = value
    return properties


def read_text_records(data: bytes) -> list[dict[str, str]]:
    """Decode u32-length-prefixed, pipe-delimited text-property records."""
    records: list[dict[str, str]] = []
    offset = 0
    while offset + 4 <= len(data):
        (record_length,) = struct.unpack_from("<I", data, offset)
        if record_length == 0 or offset + 4 + record_length > len(data):
            break
        records.append(
            parse_text_properties(
                data[offset + 4 : offset + 4 + record_length]
            )
        )
        offset += 4 + record_length
    return records


def read_kind_prefixed_text_records(
    data: bytes,
) -> tuple[list[tuple[int, dict[str, str]]], int]:
    """Decode Rules6-style records: u16 kind id, u32 length, text properties.

    Returns (records, trailing_bytes). read_text_records cannot read these:
    it would take the kind id as part of the length and silently stop.
    """
    records: list[tuple[int, dict[str, str]]] = []
    offset = 0
    while offset + 6 <= len(data):
        kind_id, record_length = struct.unpack_from("<HI", data, offset)
        if record_length == 0 or offset + 6 + record_length > len(data):
            break
        records.append(
            (
                kind_id,
                parse_text_properties(
                    data[offset + 6 : offset + 6 + record_length]
                ),
            )
        )
        offset += 6 + record_length
    return records, len(data) - offset


@dataclass(frozen=True)
class PadCopper:
    """A pad's copper on one layer, in mils (corner radius as on Primitive)."""

    width: float
    height: float
    shape: int
    corner_radius: float | None = None


# Primitive.layer_copper key for the inner layers without an entry of their
# own: every mid layer of a top-middle-bottom stack, Mid-Layer 1 and the
# internal planes of a full stack.
PAD_INNER_LAYERS = 0


def pad_copper_on(
    layer_copper: dict[int, PadCopper] | None, layer: int
) -> PadCopper | None:
    """A multi-layer pad's copper on v6 layer `layer` from its layer_copper;
    None when the layer has the pad's main (top-layer) copper."""
    if not layer_copper:
        return None
    if layer in layer_copper:
        return layer_copper[layer]
    if layer in (1, _BOTTOM_LAYER):
        return None
    return layer_copper.get(PAD_INNER_LAYERS)


@dataclass
class ViaStack:
    """A Vias6 record's mask and pad-stack fields (see decode_vias)."""

    # Tented: the via's tenting flag, or a manual solder mask expansion that
    # leaves no opening wider than the hole on that side.
    tent_top: bool
    tent_bottom: bool
    test_fab_top: bool
    test_fab_bottom: bool
    # 0 Simple, 1 Top-Middle-Bottom, 2 Full Stack.
    mode: int = 0
    # Pad diameter (mil) per v6 layer 1..32, kept for Top-Middle-Bottom and
    # Full Stack vias only (simple vias use the nominal diameter everywhere).
    layer_diameters: tuple[float, ...] | None = None
    # v6 layers whose pad is removed: Remove Unused Pad Shapes, and the
    # non-functional pads Altium removes on back-drilled layers.
    removed_pads: frozenset[int] = frozenset()

    def pad_diameter(self, layer: int, diameter: float) -> float | None:
        """The via's pad diameter on a v6 layer, None where it has no pad."""
        if layer in self.removed_pads:
            return None
        sizes = self.layer_diameters
        if self.mode not in (1, 2) or sizes is None or not 1 <= layer <= 32:
            return diameter
        if self.mode == 1:
            size = sizes[0] if layer == 1 else sizes[31] if layer == 32 else sizes[1]
        else:
            size = sizes[layer - 1]
        return size if size > 0 else None


@dataclass
class Primitive:
    """One copper or geometry primitive normalized to mils."""

    kind: str
    layer: int
    net: int | None
    component: int | None
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float | None = None
    rotation: float = 0.0
    shape: int | None = None
    source_id: int | None = None
    layer_to: int | None = None
    hole: float | None = None
    pad_name: str | None = None
    # Set on a pour's outline rows (decode_polygons): the drawn polygon
    # boundary as zero-width edges, not copper. Polygons6 record index.
    pour_id: int | None = None
    # Polygons6 record index of the pour this track or arc is poured copper
    # of: hatched and outline-only pours are stored as tracks and arcs (solid
    # pours as regions, not read). Real copper, but not routing. None for
    # routing and every other primitive.
    poured_by: int | None = None
    # True copper length of an arc chord (r * sweep / steps); the chord itself
    # is shorter. None for other primitives: their length is the segment's.
    length: float | None = None
    # Arc chords: centre and radius (mil) of their arc. The copper of a chord
    # row is the arc between its end points grown by half the width; the
    # chord itself lies up to ARC_MAX_SAGITTA_MIL inside it. None for other
    # primitives.
    arc_x: float | None = None
    arc_y: float | None = None
    arc_radius: float | None = None
    # Pad Pin/Package Length (pad to die, mil): 0.0 when unset, None when the
    # pad record is too short to carry it (and for non-pad primitives).
    pin_package: float | None = None
    # Corner radius (mil) of a rounded-rectangle pad (shape PAD_SHAPE_ROUNDRECT);
    # None for every other shape and primitive.
    corner_radius: float | None = None
    # Copper of a multi-layer pad whose stack (top-middle-bottom or full
    # stack) differs between layers: v6 layer -> PadCopper where it is not
    # the main copper above (the top layer's), with PAD_INNER_LAYERS for the
    # inner layers without an entry; read it with pad_copper_on. None for
    # every other primitive and for pads with one copper on every layer.
    layer_copper: dict[int, PadCopper] | None = None
    # Vias only: tenting and pad stack.
    via: ViaStack | None = None
    # Pads only: hole shape (0 round, 1 square, 2 slot) and the slot or square
    # hole's length (mil), from the size-and-shape subrecord; None when absent.
    hole_shape: int | None = None
    slot_length: float | None = None
    # Pads with a hole: plated (main subrecord byte 60), None when the record is
    # too short to say. The square or slot hole's angle (degrees, relative to the
    # pad's rotation), None for round holes. hole_offset: the hole is offset from
    # the pad centre (size-and-shape offsets not zero; their axes are not decoded).
    plated: bool | None = None
    slot_rotation: float | None = None
    hole_offset: bool = False
    # Pads and vias: Altium's Fabrication Testpoint top / bottom properties (the
    # record header's flags, see _test_flags). Assembly Testpoint properties are not
    # decoded (no file read so far sets one apart from these). False elsewhere.
    test_fab_top: bool = False
    test_fab_bottom: bool = False
    # Tracks and arcs on a mechanical layer: its number from the record's V7 layer id
    # (v7_mechanical), which names the layers above 16 that `layer` folds into
    # Mechanical 16 (72). None elsewhere and in records without the id.
    mechanical: int | None = None


# Tracks6, Arcs6 and Fills6 records of newer files carry the primitive's V7 layer id
# (u32; 0x0102nnnn is Mechanical nnnn) at 41, 52 and 42. Their v6 layer byte at 0 is 72
# (Mechanical 16) for every mechanical layer above 16. Checked on Altium's examples and a
# private set: the id always agrees with the v6 byte on Mechanical 1-16.
V7_MECHANICAL = 0x01020000
_TRACK_V7_AT, _ARC_V7_AT, _FILL_V7_AT = 41, 52, 42


def v7_mechanical(payload: bytes, at: int) -> int | None:
    """The mechanical layer number of the V7 layer id at `at` of a record; None when the
    record is too short to hold one or it names another layer."""
    if len(payload) < at + 4:
        return None
    (layer_id,) = struct.unpack_from("<I", payload, at)
    return layer_id & 0xFFFF if layer_id & 0xFFFF0000 == V7_MECHANICAL else None


# Pads6 main subrecord and Vias6 record header (KiCad's APAD6 and AVIA6 read the same
# two bytes): flags1 at 1 (0x20 tented top, 0x40 tented bottom, 0x80 fabrication test
# point top), flags2 at 2 (0x01 fabrication test point bottom). Seen set: flags1 0x80 on
# 8 pads of 2 private boards, all SMD pads of TP-designator parts on a net, and flags2
# 0x01 on 2 GND vias of the SpiritLevel example; no other pad or via of the examples or
# the private set has either. Main-subrecord byte 118 is 1 on exactly those 8 pads and 0
# on the other 37,418 read (an Assembly Testpoint flag is a candidate; not decoded).
_TEST_FAB_TOP = 0x80
_TEST_FAB_BOTTOM = 0x01


def _test_flags(header: bytes) -> dict[str, bool]:
    """Primitive.test_fab_top / test_fab_bottom from a pad's main subrecord or a via
    record."""
    return {
        "test_fab_top": bool(header[1] & _TEST_FAB_TOP),
        "test_fab_bottom": bool(header[2] & _TEST_FAB_BOTTOM),
    }


def iter_binary_records(data: bytes):
    """Yield (record_type, payload) from u8-type/u32-length records."""
    offset = 0
    while offset + 5 <= len(data):
        record_type = data[offset]
        (record_length,) = struct.unpack_from("<I", data, offset + 1)
        if offset + 5 + record_length > len(data):
            break
        yield (
            record_type,
            data[offset + 5 : offset + 5 + record_length],
        )
        offset += 5 + record_length


def _units_to_mil(units: int) -> float:
    return units / ALTIUM_UNITS_PER_MIL


def _optional_index(value: int, sentinel: int) -> int | None:
    return None if value == sentinel else value


def _valid_layer(layer: int) -> bool:
    return 0 < layer <= 82


def _signal_keepout(payload: bytes) -> bool:
    """A Tracks6 / Arcs6 layer-specific keepout on a signal layer (flags2 at 2
    is 2, as for regions and fills): a routing barrier, not copper."""
    return payload[2] == _KEEPOUT and _signal_layer(payload[0])


# Tracks6 / Arcs6 u16 at payload offset 5: the Polygons6 index of the pour
# the primitive belongs to (KiCad's ATRACK6/AARC6 `polygon`). 0xFFFF is none;
# 0xFFFE, which KiCad also skips, seems to mark board-outline primitives.
_POLYGON_INDEX_OFFSET = 5
_NO_POLYGON = (0xFFFF, 0xFFFE)


def _poured_by(payload: bytes) -> int | None:
    (polygon,) = struct.unpack_from("<H", payload, _POLYGON_INDEX_OFFSET)
    return None if polygon in _NO_POLYGON else polygon


def decode_tracks(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Tracks6 records."""
    primitives: list[Primitive] = []
    skipped = 0
    for record_type, payload in iter_binary_records(data):
        if (
            record_type != 4
            or len(payload) < 33
            or not _valid_layer(payload[0])
        ):
            skipped += 1
            continue
        net = struct.unpack_from("<H", payload, 3)[0]
        component = struct.unpack_from("<H", payload, 7)[0]
        x1, y1, x2, y2, width = struct.unpack_from(
            "<iiiii",
            payload,
            13,
        )
        if width <= 0:
            skipped += 1
            continue
        if _signal_keepout(payload):
            continue
        primitives.append(
            Primitive(
                kind="track",
                layer=payload[0],
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=_units_to_mil(x1),
                y1=_units_to_mil(y1),
                x2=_units_to_mil(x2),
                y2=_units_to_mil(y2),
                width=_units_to_mil(width),
                poured_by=_poured_by(payload),
                mechanical=v7_mechanical(payload, _TRACK_V7_AT),
            )
        )
    return primitives, skipped


# Vias6 record offsets beyond the common primitive header (KiCad's AVIA6 reads
# the same ones): flags1 at 1 (0x20 tented top, 0x40 tented bottom, 0x80
# fabrication test point top), flags2 at 2 (0x01 test point bottom: _test_flags), the
# solder mask expansion (int32 at 54; 66 bit 0x02 manual, 67 bit 0x01 measured
# from the hole edge; 241 bit 0x01 bottom linked to top, else int32 at 242),
# the stack mode at 74 and 32 int32 pad diameters (v6 layers 1..32) from 75.
# Bytes 209..240 (208 + v6 id) mark layers whose pad is removed: on the MiniPC
# example they are exactly the layers below its back-drill pair's stop layer on
# the vias its BackDrilling rule selects, where Altium removes the
# non-functional pads; elsewhere single layers marked by Remove Unused Pad Shapes.
_VIA_MASK_AT = 54
_VIA_MASK_FLAGS_AT = 66
_VIA_MASK_LINKED_AT = 241
_VIA_MASK_BOTTOM_AT = 242
_VIA_MODE_AT = 74
_VIA_DIAMETERS_AT = 75
_VIA_REMOVED_PADS_AT = 208


def _mask_covers(payload: bytes, diameter: int, hole: int) -> tuple[bool, bool]:
    """(top, bottom): a manual mask expansion leaves no opening wider than the
    hole, so no copper of the via is exposed on that side."""
    if len(payload) < _VIA_MASK_FLAGS_AT + 2 or not payload[_VIA_MASK_FLAGS_AT] & 0x02:
        return False, False
    (top,) = struct.unpack_from("<i", payload, _VIA_MASK_AT)
    bottom = top
    if (
        len(payload) >= _VIA_MASK_BOTTOM_AT + 4
        and not payload[_VIA_MASK_LINKED_AT] & 0x01
    ):
        (bottom,) = struct.unpack_from("<i", payload, _VIA_MASK_BOTTOM_AT)
    base = hole if payload[_VIA_MASK_FLAGS_AT + 1] & 0x01 else diameter
    return base + 2 * top <= hole, base + 2 * bottom <= hole


def _via_stack(payload: bytes, diameter: int, hole: int) -> ViaStack:
    flags1, flags2 = payload[1], payload[2]
    mask_top, mask_bottom = _mask_covers(payload, diameter, hole)
    stack = ViaStack(
        tent_top=bool(flags1 & 0x20) or mask_top,
        tent_bottom=bool(flags1 & 0x40) or mask_bottom,
        test_fab_top=bool(flags1 & _TEST_FAB_TOP),
        test_fab_bottom=bool(flags2 & _TEST_FAB_BOTTOM),
    )
    if len(payload) >= _VIA_DIAMETERS_AT + 32 * 4:
        stack.mode = payload[_VIA_MODE_AT]
        if stack.mode in (1, 2):
            stack.layer_diameters = tuple(
                _units_to_mil(value)
                for value in struct.unpack_from("<32i", payload, _VIA_DIAMETERS_AT)
            )
    if len(payload) > _VIA_REMOVED_PADS_AT + 32:
        stack.removed_pads = frozenset(
            layer
            for layer in range(1, 33)
            if payload[_VIA_REMOVED_PADS_AT + layer]
        )
    return stack


def decode_vias(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Vias6 records."""
    primitives: list[Primitive] = []
    skipped = 0
    for record_type, payload in iter_binary_records(data):
        if record_type != 3 or len(payload) < 31:
            skipped += 1
            continue
        layer_from, layer_to = payload[29], payload[30]
        if not (_valid_layer(layer_from) and _valid_layer(layer_to)):
            skipped += 1
            continue
        net = struct.unpack_from("<H", payload, 3)[0]
        component = struct.unpack_from("<H", payload, 7)[0]
        x, y, diameter, hole = struct.unpack_from("<iiii", payload, 13)
        if diameter <= 0 or hole < 0 or hole > diameter:
            skipped += 1
            continue
        x_mil = _units_to_mil(x)
        y_mil = _units_to_mil(y)
        primitives.append(
            Primitive(
                kind="via",
                layer=layer_from,
                layer_to=layer_to,
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=x_mil,
                y1=y_mil,
                x2=x_mil,
                y2=y_mil,
                width=_units_to_mil(diameter),
                hole=_units_to_mil(hole),
                via=_via_stack(payload, diameter, hole),
                **_test_flags(payload),
            )
        )
    return primitives, skipped


ARC_CHORD_STEP_DEG = 30.0
ARC_MAX_SAGITTA_MIL = 0.25


def _arc_chord_points(
    center_x: float, center_y: float, radius: float, start: float, end: float
) -> list[tuple[float, float]]:
    """Chord points (mil) of an arc running counter-clockwise from the start
    angle to the end angle (degrees; a whole circle when they are equal): at
    least two chords, each of at most ARC_CHORD_STEP_DEG and lying at most
    ARC_MAX_SAGITTA_MIL inside the arc."""
    sweep = (end - start) % 360.0 or 360.0
    adaptive_step = math.degrees(
        2.0
        * math.acos(
            max(
                -1.0,
                1.0 - min(ARC_MAX_SAGITTA_MIL / radius, 2.0),
            )
        )
    )
    chord_step = min(
        ARC_CHORD_STEP_DEG,
        max(adaptive_step, 0.5),
    )
    steps = max(2, math.ceil(sweep / chord_step))
    return [
        (
            center_x
            + radius * math.cos(math.radians(start + sweep * index / steps)),
            center_y
            + radius * math.sin(math.radians(start + sweep * index / steps)),
        )
        for index in range(steps + 1)
    ]


def decode_arcs(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Arcs6 records into chord rows (indexing and length), each
    carrying its arc's centre and radius so distances use the arc itself."""
    primitives: list[Primitive] = []
    skipped = 0
    for arc_id, (record_type, payload) in enumerate(
        iter_binary_records(data)
    ):
        if (
            record_type != 1
            or len(payload) < 45
            or not _valid_layer(payload[0])
        ):
            skipped += 1
            continue
        try:
            net = struct.unpack_from("<H", payload, 3)[0]
            component = struct.unpack_from("<H", payload, 7)[0]
            center_x, center_y = struct.unpack_from("<ii", payload, 13)
            (radius,) = struct.unpack_from("<i", payload, 21)
            start_angle, end_angle = struct.unpack_from("<dd", payload, 25)
            (width,) = struct.unpack_from("<i", payload, 41)
        except struct.error:
            skipped += 1
            continue
        if (
            radius <= 0
            or width < 0
            or not math.isfinite(start_angle)
            or not math.isfinite(end_angle)
        ):
            skipped += 1
            continue
        if width == 0 and net == NO_NET:
            # Component/mechanical outline arcs can legitimately be zero-width
            # and netless; they are not copper primitives for these tools.
            continue
        if width == 0:
            skipped += 1
            continue
        if _signal_keepout(payload):
            continue
        center_x_mil = _units_to_mil(center_x)
        center_y_mil = _units_to_mil(center_y)
        radius_mil = _units_to_mil(radius)
        sweep = (end_angle - start_angle) % 360.0 or 360.0
        points = _arc_chord_points(
            center_x_mil, center_y_mil, radius_mil, start_angle, end_angle
        )
        chord_length = radius_mil * math.radians(sweep) / (len(points) - 1)
        poured_by = _poured_by(payload)
        mechanical = v7_mechanical(payload, _ARC_V7_AT)
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            primitives.append(
                Primitive(
                    kind="arc",
                    layer=payload[0],
                    net=_optional_index(net, NO_NET),
                    component=_optional_index(component, NO_COMPONENT),
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=_units_to_mil(width),
                    source_id=arc_id,
                    length=chord_length,
                    poured_by=poured_by,
                    arc_x=center_x_mil,
                    arc_y=center_y_mil,
                    arc_radius=radius_mil,
                    mechanical=mechanical,
                )
            )
    return primitives, skipped


_PAD_SUBRECORD_COUNT = 5
_PAD_MAIN_INDEX = 3
_PAD_MAIN_MIN_LENGTH = 60
# Pin/Package Length: int32 in internal units (1e-4 mil) at this offset of
# the main subrecord. Seen set only in 194- and 202-byte records (whole-mm
# values on user boards; on the MiniPC example it is what closes the tuned
# groups at the FPGA); 171-, 185- and 186-byte records hold 0 there throughout
# the corpus.
_PAD_PIN_PACKAGE_OFFSET = 158
# Pad stack mode (main subrecord byte 62): simple pads use the top size and
# shape on every layer; top-middle-bottom and full-stack pads keep them per
# layer (top, mid and bottom in the main subrecord at 21/29/37 and 49/50/51,
# mid layers 2-30 of a full stack in the size-and-shape subrecord), so a pad
# on the bottom or an inner layer has its copper in that layer's slot, and
# its top slot can be empty. A multi-layer pad has copper in every slot
# (Primitive.layer_copper).
_PAD_MODE_OFFSET = 62
_PAD_MODE_TOP_MID_BOTTOM = 1
_PAD_MODE_FULL_STACK = 2
_BOTTOM_LAYER = 32
MULTI_LAYER = 74
# Size-and-shape subrecord (the one after the main subrecord; KiCad's
# importer reads the same layout): int32 sizes of mid layers 2-30 (x at 0,
# y at 116) and their shapes at 232, then 32 per-layer slots (0 top, 1-30
# mid, 31 bottom) of alt shape at 532 and corner radius percent at 564.
_PAD_SIZE_SHAPE_INDEX = 4
_PAD_SIZE_SHAPE_MIN_LENGTH = 596
_PAD_INNER_COUNT = 29
_PAD_INNER_SIZE_Y_OFFSET = 116
_PAD_INNER_SHAPE_OFFSET = 232
_PAD_ALT_SHAPE_OFFSET = 532
_PAD_CORNER_RADIUS_OFFSET = 564
_PAD_BOTTOM_SLOT = 31

PAD_SHAPE_ROUND = 1  # round, or oblong when not square
PAD_SHAPE_RECT = 2
PAD_SHAPE_OCTAGONAL = 3
# Altium writes a rounded rectangle as round in the main subrecord; the
# size-and-shape subrecord's alt shape 9 and corner radius (percent of half
# the shorter side, 100 = oblong) make it one.
PAD_SHAPE_ROUNDRECT = 9


def _pad_mode(main: bytes) -> int:
    return main[_PAD_MODE_OFFSET] if len(main) > _PAD_MODE_OFFSET else 0


def _main_slots(main: bytes) -> tuple[tuple, tuple, tuple]:
    """Raw (size x, size y, shape, slot) of top, mid and bottom."""
    return (
        (*struct.unpack_from("<ii", main, 21), main[49], 0),
        (*struct.unpack_from("<ii", main, 29), main[50], 1),
        (*struct.unpack_from("<ii", main, 37), main[51], _PAD_BOTTOM_SLOT),
    )


def _inner_slot(size_shape: bytes, layer: int) -> tuple | None:
    """Raw (size x, size y, shape, slot) of a full stack's v6 layer 3-31,
    or None when the size-and-shape subrecord is too short to hold it."""
    inner = layer - 3
    if not (
        0 <= inner < _PAD_INNER_COUNT
        and len(size_shape) >= _PAD_SIZE_SHAPE_MIN_LENGTH
    ):
        return None
    return (
        struct.unpack_from("<i", size_shape, 4 * inner)[0],
        struct.unpack_from(
            "<i", size_shape, _PAD_INNER_SIZE_Y_OFFSET + 4 * inner
        )[0],
        size_shape[_PAD_INNER_SHAPE_OFFSET + inner],
        layer - 1,
    )


def _pad_copper(main: bytes, size_shape: bytes) -> tuple[int, int, int, int]:
    """(size x, size y, shape, slot) of a pad's copper on its own layer.

    A per-layer pad whose own slot is empty takes the first filled one of
    top, mid and bottom instead of being dropped.
    """
    top, mid, bottom = _main_slots(main)
    mode = _pad_mode(main)
    if mode not in (_PAD_MODE_TOP_MID_BOTTOM, _PAD_MODE_FULL_STACK):
        return top
    layer = main[0]
    own = top
    if layer == _BOTTOM_LAYER:
        own = bottom
    elif 1 < layer < _BOTTOM_LAYER:
        own = mid
        if mode == _PAD_MODE_FULL_STACK:
            own = _inner_slot(size_shape, layer) or mid
    return next(
        (
            copper
            for copper in (own, top, mid, bottom)
            if copper[0] > 0 and copper[1] > 0
        ),
        own,
    )


def _slot_copper(raw: tuple, size_shape: bytes) -> PadCopper:
    """Copper of a raw (size x, size y, shape, slot) in mils: a round shape
    whose slot has alt shape 9 in the size-and-shape subrecord is a rounded
    rectangle."""
    size_x, size_y, shape, slot = raw
    corner_radius = None
    if (
        shape == PAD_SHAPE_ROUND
        and len(size_shape) >= _PAD_SIZE_SHAPE_MIN_LENGTH
        and size_shape[_PAD_ALT_SHAPE_OFFSET + slot] == PAD_SHAPE_ROUNDRECT
    ):
        shape = PAD_SHAPE_ROUNDRECT
        percent = min(size_shape[_PAD_CORNER_RADIUS_OFFSET + slot], 100)
        corner_radius = (
            _units_to_mil(min(size_x, size_y)) / 2.0 * (percent / 100.0)
        )
    return PadCopper(
        _units_to_mil(size_x), _units_to_mil(size_y), shape, corner_radius
    )


def _pad_layer_copper(
    main: bytes, size_shape: bytes, own: PadCopper
) -> dict[int, PadCopper] | None:
    """Primitive.layer_copper of a multi-layer pad whose main copper is own.

    A top-middle-bottom stack has one mid copper on every inner layer; a
    full stack keeps Mid-Layer 1 in the main subrecord and Mid-Layers 2-30
    in the size-and-shape subrecord. An empty slot (not seen in files) keeps
    the main copper, or for Mid-Layers 2-30 the mid copper.
    """
    mode = _pad_mode(main)
    if mode not in (_PAD_MODE_TOP_MID_BOTTOM, _PAD_MODE_FULL_STACK):
        return None

    def copper(raw: tuple, empty: PadCopper) -> PadCopper:
        if raw[0] <= 0 or raw[1] <= 0:
            return empty
        return _slot_copper(raw, size_shape)

    _, mid_slot, bottom_slot = _main_slots(main)
    layer_copper: dict[int, PadCopper] = {}
    bottom = copper(bottom_slot, own)
    if bottom != own:
        layer_copper[_BOTTOM_LAYER] = bottom
    mid = copper(mid_slot, own)
    if mid != own:
        layer_copper[PAD_INNER_LAYERS] = mid
    if mode == _PAD_MODE_FULL_STACK:
        for layer in range(3, _BOTTOM_LAYER):
            raw = _inner_slot(size_shape, layer)
            if raw is not None and (inner := copper(raw, mid)) != mid:
                layer_copper[layer] = inner
    return layer_copper or None


def _pad_records(data: bytes):
    """Yield (record type, pad name, five subrecords) per Pads6 record; stop at a
    record that runs past the stream."""
    offset = 0
    while offset + 5 <= len(data):
        record_type = data[offset]
        (name_length,) = struct.unpack_from("<I", data, offset + 1)
        if offset + 5 + name_length > len(data):
            return
        name_blob = data[offset + 5 : offset + 5 + name_length]
        pad_name = (
            name_blob[1 : 1 + name_blob[0]].decode("latin-1", "replace")
            if name_blob
            else ""
        )
        position = offset + 5 + name_length
        subrecords: list[bytes] = []
        for _ in range(_PAD_SUBRECORD_COUNT):
            if position + 4 > len(data):
                return
            (subrecord_length,) = struct.unpack_from("<I", data, position)
            if position + 4 + subrecord_length > len(data):
                return
            subrecords.append(
                data[position + 4 : position + 4 + subrecord_length]
            )
            position += 4 + subrecord_length
        offset = position
        yield record_type, pad_name, subrecords


def _pad_main(record_type: int, subrecords: list[bytes]) -> bytes | None:
    main = subrecords[_PAD_MAIN_INDEX]
    if (
        record_type != 2
        or len(main) < _PAD_MAIN_MIN_LENGTH
        or not _valid_layer(main[0])
    ):
        return None
    return main


def _hole_fields(main: bytes, size_shape: bytes, hole_shape: int | None) -> dict:
    """plated, slot_rotation and hole_offset of a pad with a hole."""
    rotation = None
    if hole_shape in (1, 2) and len(size_shape) >= _PAD_SLOT_ROTATION_OFFSET + 8:
        (rotation,) = struct.unpack_from("<d", size_shape, _PAD_SLOT_ROTATION_OFFSET)
        if not math.isfinite(rotation):
            rotation = None
    offset = False
    if len(size_shape) >= _PAD_HOLE_OFFSET_Y + 4:
        offset = any(
            struct.unpack_from("<i", size_shape, at)[0]
            for at in (_PAD_HOLE_OFFSET_X, _PAD_HOLE_OFFSET_Y)
        )
    plated = bool(main[_PAD_PLATED_OFFSET]) if len(main) > _PAD_PLATED_OFFSET else None
    return {"plated": plated, "slot_rotation": rotation, "hole_offset": offset}


def decode_pads(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Pads6 multi-subrecord records. Pads without copper (hole-only
    pads) are counted as skipped; decode_bare_holes reads their holes."""
    primitives: list[Primitive] = []
    skipped = 0
    for record_type, pad_name, subrecords in _pad_records(data):
        main = _pad_main(record_type, subrecords)
        if main is None:
            skipped += 1
            continue
        net = struct.unpack_from("<H", main, 3)[0]
        component = struct.unpack_from("<H", main, 7)[0]
        x, y = struct.unpack_from("<ii", main, 13)
        (hole,) = struct.unpack_from("<i", main, 45)
        size_shape = subrecords[_PAD_SIZE_SHAPE_INDEX]
        raw_copper = _pad_copper(main, size_shape)
        (rotation,) = struct.unpack_from("<d", main, 52)
        if raw_copper[0] <= 0 or raw_copper[1] <= 0 or hole < 0:
            skipped += 1
            continue
        copper = _slot_copper(raw_copper, size_shape)
        hole_shape, slot_length = _hole_slot(size_shape)
        pin_package = None
        if len(main) >= _PAD_PIN_PACKAGE_OFFSET + 4:
            (raw_pin_package,) = struct.unpack_from(
                "<i", main, _PAD_PIN_PACKAGE_OFFSET
            )
            if raw_pin_package >= 0:
                pin_package = _units_to_mil(raw_pin_package)
        x_mil = _units_to_mil(x)
        y_mil = _units_to_mil(y)
        primitives.append(
            Primitive(
                kind="pad",
                layer=main[0],
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=x_mil,
                y1=y_mil,
                x2=x_mil,
                y2=y_mil,
                width=copper.width,
                height=copper.height,
                rotation=rotation if math.isfinite(rotation) else 0.0,
                shape=copper.shape,
                hole=_units_to_mil(hole),
                pad_name=pad_name,
                pin_package=pin_package,
                corner_radius=copper.corner_radius,
                layer_copper=(
                    _pad_layer_copper(main, size_shape, copper)
                    if main[0] == MULTI_LAYER
                    else None
                ),
                hole_shape=hole_shape if hole > 0 else None,
                slot_length=slot_length if hole > 0 else None,
                **(_hole_fields(main, size_shape, hole_shape) if hole > 0 else {}),
                **_test_flags(main),
            )
        )
    return primitives, skipped


@dataclass
class BareHole:
    """A Pads6 pad with a hole and no copper (copper size 0 or less), such as a
    mounting hole drawn as a bare hole: a drilled hole, not a copper primitive.
    Lengths in mil, absolute coordinates; fields as on Primitive."""

    x: float
    y: float
    hole: float
    layer: int
    net: int | None
    component: int | None
    pad_name: str
    rotation: float = 0.0
    hole_shape: int | None = None
    slot_length: float | None = None
    plated: bool | None = None
    slot_rotation: float | None = None
    hole_offset: bool = False


def decode_bare_holes(data: bytes) -> list[BareHole]:
    """The holes of Pads6 pads without copper, which decode_pads skips."""
    holes: list[BareHole] = []
    for record_type, pad_name, subrecords in _pad_records(data):
        main = _pad_main(record_type, subrecords)
        if main is None:
            continue
        (hole,) = struct.unpack_from("<i", main, 45)
        size_shape = subrecords[_PAD_SIZE_SHAPE_INDEX]
        raw_copper = _pad_copper(main, size_shape)
        if hole <= 0 or (raw_copper[0] > 0 and raw_copper[1] > 0):
            continue
        x, y = struct.unpack_from("<ii", main, 13)
        net = struct.unpack_from("<H", main, 3)[0]
        component = struct.unpack_from("<H", main, 7)[0]
        (rotation,) = struct.unpack_from("<d", main, 52)
        hole_shape, slot_length = _hole_slot(size_shape)
        holes.append(BareHole(
            x=_units_to_mil(x),
            y=_units_to_mil(y),
            hole=_units_to_mil(hole),
            layer=main[0],
            net=_optional_index(net, NO_NET),
            component=_optional_index(component, NO_COMPONENT),
            pad_name=pad_name,
            rotation=rotation if math.isfinite(rotation) else 0.0,
            hole_shape=hole_shape,
            slot_length=slot_length,
            **_hole_fields(main, size_shape, hole_shape),
        ))
    return holes


# Size-and-shape subrecord: 29 inner sizes x and y (232 bytes), 29 inner shapes, one
# byte, then the hole shape (u8: 0 round, 1 square, 2 slot) and the slot length (int32).
_PAD_HOLE_SHAPE_OFFSET = 262
_PAD_SLOT_LENGTH_OFFSET = 263
# Then the square or slot hole's angle relative to the pad (double), and per layer
# hole offsets: the top layer's x and y (int32) at 275 and 403.
_PAD_SLOT_ROTATION_OFFSET = 267
_PAD_HOLE_OFFSET_X = 275
_PAD_HOLE_OFFSET_Y = 403
# Main subrecord: 1 for a plated hole (matches Altium's drill tables).
_PAD_PLATED_OFFSET = 60


def _hole_slot(size_shape: bytes) -> tuple[int | None, float | None]:
    """(hole shape, slot length in mil) from a pad's size-and-shape subrecord."""
    if len(size_shape) < _PAD_SLOT_LENGTH_OFFSET + 4:
        return None, None
    shape = size_shape[_PAD_HOLE_SHAPE_OFFSET]
    (length,) = struct.unpack_from("<i", size_shape, _PAD_SLOT_LENGTH_OFFSET)
    if length > 0:
        return shape, _units_to_mil(length)
    # A square or slot hole of length 0 is a real (degenerate) slot; round holes
    # store 0 there too, and carry no slot length.
    return shape, 0.0 if shape in (1, 2) else None


@dataclass
class PcbLayer:
    """One physical stackup or legacy board layer."""

    v6_id: int | None
    name: str
    kind: str
    stack_order: int | None
    copper_thick_mil: float | None = None
    diel_height_mil: float | None = None
    diel_const: float | None = None
    material: str | None = None
    v7_id: int | None = None
    # The layer stacks (V9_SUBSTACKn_ID, upper case) that hold this stack layer: those
    # whose V9_STACK_LAYERn_{id}CONTEXT is 0 (a rigid-flex board's flex stack leaves the
    # rigid-only layers out with 1); None when the stack records no CONTEXT for it.
    substacks: frozenset[str] | None = None


@dataclass
class ImpedanceLayer:
    """One signal layer of an impedance profile (Layer Stack Manager).

    Lengths in mil; None for unset or unbounded (Altium writes int32 max).
    """

    layer_v7: int | None
    ref_top_v7: int | None
    ref_bottom_v7: int | None
    substack_id: str | None
    width_mil: float | None
    min_width_mil: float | None
    max_width_mil: float | None
    gap_mil: float | None
    min_gap_mil: float | None
    max_gap_mil: float | None
    calculated_ohm: float | None
    error_pct: float | None
    propagation_speed: float | None  # a delay in ns/inch (= ps/mil), per the stack Dk
    enabled: bool


@dataclass
class ImpedanceProfile:
    """A Layer Stack Manager impedance profile and its per-layer solutions."""

    id: str
    name: str
    display_name: str | None
    target_ohm: float | None
    tolerance_pct: float | None
    differential: bool
    layers: list[ImpedanceLayer] = field(default_factory=list)


@dataclass
class DrillPair:
    """A Board6 LAYERPAIRn drill pair (Layer Stack Manager drill pairs and
    back drills). low/high are the stored layer tokens (TOP, MID4, BOTTOM,
    or a layer name). A back drill starts at its outer layer and stops before
    the other one."""

    low: str
    high: str
    back_drill: bool = False
    pair_type: int | None = None  # DRILLPAIRTYPE (2 on back drills)


def decode_drill_pairs(properties: dict[str, str]) -> list[DrillPair]:
    pairs: list[DrillPair] = []
    index = 0
    while f"LAYERPAIR{index}LOW" in properties:
        prefix = f"LAYERPAIR{index}"
        pairs.append(
            DrillPair(
                low=properties[f"{prefix}LOW"],
                high=properties.get(f"{prefix}HIGH", ""),
                back_drill=properties.get(f"{prefix}BACKDRILL", "").upper() == "TRUE",
                pair_type=_optional_int(properties.get(f"{prefix}DRILLPAIRTYPE")),
            )
        )
        index += 1
    return pairs


@dataclass
class BoardInfo:
    """Board coordinate system and layer metadata."""

    origin_x: float | None
    origin_y: float | None
    sheet_x: float | None
    sheet_y: float | None
    sheet_width: float | None
    sheet_height: float | None
    display_unit: str
    layers: list[PcbLayer]
    stackup_source: str
    # The outline as points, arc edges approximated by chords (shape_points).
    outline_vertices: list[tuple[float, float]] = field(
        default_factory=list
    )
    impedance_profiles: list[ImpedanceProfile] = field(default_factory=list)
    # Layer-stack regions (rigid/flex); each {"id", "name", "is_flex"}.
    substacks: list[dict] = field(default_factory=list)
    drill_pairs: list[DrillPair] = field(default_factory=list)
    # The outline with its arcs (outline_vertices flattens them).
    outline_shape: list["ShapeVertex"] = field(default_factory=list)
    # Health Check Monitor settings saved with the board (HEALTHCHECKSTORAGE):
    # check id -> enabled; None when the board stores none (all on but id 20).
    health_checks: dict[int, bool] | None = None
    # Internal plane net assignments (PLANEnNETNAME): v6 layer (38 + n) ->
    # net name; planes without a net ('(No Net)') are left out.
    plane_nets: dict[int, str] = field(default_factory=dict)
    # Internal plane pullback from the board edge (Layer Stack Manager 'Pullback
    # distance'): v6 layer (38 + n) -> (mil, where it was read): "stack" (the stack
    # layer's V9_STACK_LAYERn_PULLBACKDISTANCE), else "plane" (PLANEnPULLBACK), else
    # "cache" (the V9_CACHE_LAYERn_PULLBACKDISTANCE of the layer cache entry whose
    # LAYERID is the plane's). Planes with none of them are left out.
    plane_pullbacks: dict[int, tuple[float, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ShapeVertex:
    """A vertex of an outline, pour or region shape (mil). With arc set, the edge
    from this vertex is the arc (centre x, centre y, radius, start and end angle in
    degrees, counter-clockwise) instead of a straight line."""

    x: float
    y: float
    arc: tuple[float, float, float, float, float] | None = None


def shape_from_properties(properties: dict[str, str]) -> list[ShapeVertex]:
    """VXn/VYn vertices with their KINDn=1 arcs (CXn, CYn, Rn, SAn, EAn), as Board6
    outlines and Polygons6 pours store them."""
    vertices: list[ShapeVertex] = []
    index = 0
    while f"VX{index}" in properties and f"VY{index}" in properties:
        x = parse_dim_mil(properties[f"VX{index}"])
        y = parse_dim_mil(properties[f"VY{index}"])
        arc = None
        if properties.get(f"KIND{index}", "0").strip() == "1":
            cx = parse_dim_mil(properties.get(f"CX{index}"))
            cy = parse_dim_mil(properties.get(f"CY{index}"))
            radius = parse_dim_mil(properties.get(f"R{index}"))
            start = _optional_float(properties.get(f"SA{index}"))
            end = _optional_float(properties.get(f"EA{index}"))
            values = (cx, cy, radius, start, end)
            if (
                None not in values
                and all(map(math.isfinite, values))
                and math.isfinite(end - start)  # huge angles of opposite sign
                and radius > 0
            ):
                arc = values
        if x is not None and y is not None:
            vertices.append(ShapeVertex(x, y, arc))
        index += 1
    if len(vertices) > 1 and vertices[-1] == ShapeVertex(vertices[0].x, vertices[0].y):
        vertices.pop()  # the closing copy of the first vertex
    return vertices


# Altium rounds an arc's end points and the vertices they join each on its
# own: up to 0.0033 mil apart over the Polygons6 arcs of 95 boards.
_SHAPE_SNAP_MIL = 0.01
# KiCad's importer draws an arc edge of a smaller sweep (start = end
# included) as a straight one.
_SHAPE_MIN_SWEEP_DEG = 0.2


def shape_points(shape: list[ShapeVertex]) -> list[tuple[float, float]]:
    """The shape as points (mil), each arc edge approximated by chords that lie
    at most ARC_MAX_SAGITTA_MIL inside it. As in KiCad's Altium importer, the
    arc runs counter-clockwise from its start to its end angle and is drawn
    from the end nearer its vertex, the far end meeting the next vertex; an
    end within _SHAPE_SNAP_MIL of a vertex is that vertex, and an arc of
    under _SHAPE_MIN_SWEEP_DEG is a straight edge."""
    points: list[tuple[float, float]] = []
    for index, vertex in enumerate(shape):
        here = (vertex.x, vertex.y)
        if (
            vertex.arc is None
            or (vertex.arc[4] - vertex.arc[3]) % 360.0 < _SHAPE_MIN_SWEEP_DEG
        ):
            points.append(here)
            continue
        arc = _arc_chord_points(*vertex.arc)
        if math.dist(arc[-1], here) < math.dist(arc[0], here):
            arc.reverse()
        if math.dist(arc[0], here) <= _SHAPE_SNAP_MIL:
            arc[0] = here
        following = shape[(index + 1) % len(shape)]
        if math.dist(arc[-1], (following.x, following.y)) <= _SHAPE_SNAP_MIL:
            arc.pop()  # the next vertex is the arc's far end
        points.extend(arc)
    return points


def _health_check_storage(raw: str | None) -> dict[int, bool] | None:
    """Board6 HEALTHCHECKSTORAGE: comma-separated byte codes of a JSON object whose
    healthCheckRuleEnabledDict holds {"<check id>": true|false} as JSON text."""
    if not raw:
        return None
    try:
        outer = json.loads(bytes(int(code) for code in raw.split(",") if code.strip()))
        inner = outer.get("healthCheckRuleEnabledDict", {})
        if isinstance(inner, str):
            inner = json.loads(inner)
        return {int(key): bool(value) for key, value in inner.items()}
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class PcbComponent:
    """A Components6 placement record."""

    index: int
    refdes: str | None
    source_uid_tail: str | None
    channel_path: str | None
    x: float | None
    y: float | None
    rotation: float | None
    side: str
    footprint: str | None = None
    # The designator shown on the board (Texts6). Differs from refdes
    # (SOURCEDESIGNATOR) on multi-channel and board-annotated designs; class
    # members and rule scopes use it.
    designator: str | None = None
    # The comment text (Texts6), and ComponentKind (see _component_kind).
    comment: str | None = None
    kind: int = 0
    # The full SOURCEUNIQUEID path (\SheetSymbol...\Component): channel copies
    # share the tail but not the path.
    source_unique_id: str | None = None
    # The LAYER token as written (TOP, BOTTOM, MID1, ...); side folds it to two.
    layer_name: str = ""
    # Where the footprint and part were placed from: SOURCEFOOTPRINTLIBRARY (a PcbLib
    # or IntLib name, or the path of a Workspace part's cached PcbLib),
    # SOURCECOMPONENTLIBRARY and SOURCELIBREFERENCE (the schematic library and symbol),
    # a Workspace item (VAULTGUID or ITEMGUID set) and the footprint's Height (HEIGHT;
    # None when not written).
    footprint_library: str = ""
    component_library: str = ""
    lib_reference: str = ""
    workspace_part: bool = False
    height_mil: float | None = None


@dataclass
class PolygonPour:
    """A polygon-pour outline and its copper identity."""

    pour_id: int
    net: int | None
    layer: int | None
    hatch_style: str | None
    # The outline as points, arc edges approximated by chords (shape_points).
    vertices: list[tuple[float, float]]
    # Shelved (Polygon Manager): kept in the file, with any copper, but hidden
    # from DRC and connectivity until restored. No copper for these tools.
    shelved: bool = False
    # Modified since its copper was poured (COPPERINVALIDATE): the saved copper
    # does not follow the outline until it is repoured.
    copper_invalid: bool = False
    # POLYGONTYPE: 'Polygon' for a pour, 'Split Plane' for a split plane on
    # an internal plane layer; None when the record does not say.
    polygon_type: str | None = None
    # The outline as stored: its vertices with their arcs.
    shape: list[ShapeVertex] = field(default_factory=list)
    # NAME, which IsNamedPolygon / InNamedPolygon and polygon class members
    # give; None when the record has none. Pours may share a name.
    name: str | None = None
    # A hatched pour's hatch: GRIDSIZE (the spacing of its tracks, centre to
    # centre) and TRACKWIDTH, mil; None when the record does not say.
    grid_mil: float | None = None
    track_width_mil: float | None = None


@dataclass
class CopperArea:
    """Copper drawn as an area: a Regions6 region (a solid pour's poured
    copper, a free copper region, a footprint's region) or a Fills6 fill.

    outline and each hole are vertex lists in mil, in the absolute frame of
    the other primitives. A solid pour's copper is one region per poured
    island, saved without a net (pour_id names the pour, which holds it).
    """

    kind: str  # "region" | "fill"
    layer: int
    net: int | None
    component: int | None
    outline: list[tuple[float, float]]
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    # Polygons6 index of the pour this is the poured copper of; None for
    # free regions and fills.
    pour_id: int | None = None


# Altium writes int32 max (214748.3647 mil) for an unbounded max width/gap.
_UNBOUNDED_MIL = 214748.0


def _optional_int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _optional_float(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _bounded_mil(raw: str | None) -> float | None:
    value = parse_dim_mil(raw)
    return None if value is None or value >= _UNBOUNDED_MIL else value


def decode_impedance_profiles(properties: dict[str, str]) -> list[ImpedanceProfile]:
    """Decode V9_IMPEDANCEPROFILEn_* and their V9_TRACEIMPEDANCEn_* layers."""
    profiles: dict[str, ImpedanceProfile] = {}
    index = 0
    while f"V9_IMPEDANCEPROFILE{index}_ID" in properties:
        prefix = f"V9_IMPEDANCEPROFILE{index}_"
        profile_id = properties[f"{prefix}ID"]
        profiles[profile_id.upper()] = ImpedanceProfile(
            id=profile_id,
            # Altium shows DISPLAY_NAME (e.g. "D90 (USB)"); NAME is often empty.
            name=(
                properties.get(f"{prefix}DISPLAY_NAME")
                or properties.get(f"{prefix}NAME")
                or profile_id
            ),
            display_name=properties.get(f"{prefix}DISPLAY_NAME") or None,
            target_ohm=_optional_float(properties.get(f"{prefix}IMPEDANCE")),
            tolerance_pct=_optional_float(
                properties.get(f"{prefix}IMPEDANCE_TOLERANCE")
            ),
            differential=properties.get(f"{prefix}ISDIFFPAIR", "").upper() == "TRUE",
        )
        index += 1
    index = 0
    while f"V9_TRACEIMPEDANCE{index}_PROFILE_ID" in properties:
        prefix = f"V9_TRACEIMPEDANCE{index}_"
        index += 1
        profile = profiles.get(properties[f"{prefix}PROFILE_ID"].upper())
        if profile is None:
            continue
        profile.layers.append(
            ImpedanceLayer(
                layer_v7=_optional_int(properties.get(f"{prefix}LAYER_V7ID")),
                ref_top_v7=_optional_int(properties.get(f"{prefix}REF_TOP_V7ID")),
                ref_bottom_v7=_optional_int(properties.get(f"{prefix}REF_BOT_V7ID")),
                substack_id=properties.get(f"{prefix}SUBSTACK_ID") or None,
                width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_WIDTH")),
                min_width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_MINWIDTH")),
                max_width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_MAXWIDTH")),
                gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_GAP")),
                min_gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_MINGAP")),
                max_gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_MAXGAP")),
                calculated_ohm=_optional_float(properties.get(f"{prefix}CALC_IMPEDANCE")),
                error_pct=_optional_float(properties.get(f"{prefix}IMPEDANCE_ERR")),
                propagation_speed=_optional_float(
                    properties.get(f"{prefix}PROPAGATION_SPEED")
                ),
                enabled=properties.get(f"{prefix}ENABLED", "TRUE").upper() != "FALSE",
            )
        )
    return list(profiles.values())


def _copper_v6(layer_id: int | None) -> int | None:
    """The v6 id of a copper stack layer from its V7 LAYERID: signal layers
    0x0100nnnn (n, 0xFFFF = Bottom = 32), internal planes 0x0101nnnn (38 + n).

    Names cannot be trusted for this: boards keep stale LAYERnNAME entries
    (e.g. old plane slots) that repeat a signal layer's name."""
    if layer_id is None:
        return None
    kind, number = layer_id >> 16, layer_id & 0xFFFF
    if kind == 0x0100:
        if number == 0xFFFF:
            return 32
        return number if 1 <= number <= 31 else None
    if kind == 0x0101 and 1 <= number <= 16:
        return 38 + number
    return None


_STACK_CONTEXT = re.compile(r"V9_STACK_LAYER(\d+)_(\{[^}]*\})CONTEXT")


def decode_board(records: list[dict[str, str]]) -> BoardInfo:
    """Decode Board6 text properties into coordinate and stackup metadata."""
    properties = records[0] if records else {}
    name_to_v6: dict[str, int] = {}
    for key, value in properties.items():
        match = re.fullmatch(r"LAYER(\d+)NAME", key)
        if match:
            name_to_v6[value.lower()] = int(match.group(1))

    layers: list[PcbLayer] = []
    plane_pullbacks: dict[int, tuple[float, str]] = {}
    # Stack layer order -> {layer stack id: its CONTEXT value} (PcbLayer.substacks).
    stack_contexts: dict[int, dict[str, str]] = {}
    for key, value in properties.items():
        match = _STACK_CONTEXT.fullmatch(key)
        if match:
            stack_contexts.setdefault(int(match.group(1)), {})[match.group(2).upper()] = \
                value.strip()
    order = 0
    stackup_source = "legacy"
    while True:
        prefix = f"V9_STACK_LAYER{order}_"
        if f"{prefix}NAME" not in properties:
            break
        stackup_source = "v9"
        name = properties[f"{prefix}NAME"]
        copper_thickness = parse_dim_mil(
            properties.get(f"{prefix}COPTHICK")
        )
        dielectric_height = parse_dim_mil(
            properties.get(f"{prefix}DIELHEIGHT")
        )
        if copper_thickness is not None:
            kind = "copper"
        elif (
            dielectric_height is not None
            or f"{prefix}DIELTYPE" in properties
        ):
            kind = "dielectric"
        else:
            kind = "non-stack"
        dielectric_constant = properties.get(f"{prefix}DIELCONST")
        layer_id = _optional_int(properties.get(f"{prefix}LAYERID"))
        v6_id = _copper_v6(layer_id) if kind == "copper" else None
        if v6_id is not None and _plane_layer(v6_id):
            pullback = parse_dim_mil(properties.get(f"{prefix}PULLBACKDISTANCE"))
            if pullback is not None and pullback >= 0:
                plane_pullbacks[v6_id] = (pullback, "stack")
        contexts = stack_contexts.get(order, {})
        layers.append(
            PcbLayer(
                v6_id=v6_id if v6_id is not None else name_to_v6.get(name.lower()),
                name=name,
                kind=kind,
                stack_order=order,
                copper_thick_mil=copper_thickness,
                diel_height_mil=dielectric_height,
                diel_const=(
                    float(dielectric_constant)
                    if dielectric_constant
                    else None
                ),
                material=properties.get(f"{prefix}DIELMATERIAL"),
                v7_id=layer_id,
                substacks=frozenset(stack for stack, value in contexts.items() if value == "0")
                if contexts else None,
            )
        )
        order += 1

    stack_names = {layer.name.lower() for layer in layers}
    stack_ids = {layer.v6_id for layer in layers}
    for layer_name, v6_id in sorted(
        name_to_v6.items(),
        key=lambda item: item[1],
    ):
        if layer_name not in stack_names and v6_id not in stack_ids:
            layers.append(
                PcbLayer(
                    v6_id=v6_id,
                    name=properties[f"LAYER{v6_id}NAME"],
                    kind="non-stack",
                    stack_order=None,
                )
            )

    if stackup_source == "legacy":
        layers = [
            PcbLayer(
                v6_id=v6_id,
                name=properties[f"LAYER{v6_id}NAME"],
                kind="copper" if v6_id <= 32 else "non-stack",
                stack_order=None,
            )
            for _layer_name, v6_id in sorted(
                name_to_v6.items(),
                key=lambda item: item[1],
            )
        ]

    outline_shape = shape_from_properties(properties)

    substacks: list[dict] = []
    substack_index = 0
    while f"V9_SUBSTACK{substack_index}_ID" in properties:
        prefix = f"V9_SUBSTACK{substack_index}_"
        substacks.append(
            {
                "id": properties[f"{prefix}ID"],
                "name": properties.get(f"{prefix}NAME")
                or properties[f"{prefix}ID"],
                "is_flex": properties.get(f"{prefix}ISFLEX", "").upper()
                == "TRUE",
            }
        )
        substack_index += 1

    plane_nets: dict[int, str] = {}
    for key, value in properties.items():
        match = re.fullmatch(r"PLANE(\d+)NETNAME", key)
        net_name = (value or "").strip()
        if match and 1 <= int(match.group(1)) <= 16 and net_name and net_name != "(No Net)":
            plane_nets[38 + int(match.group(1))] = net_name
    for plane in range(1, 17):
        pullback = parse_dim_mil(properties.get(f"PLANE{plane}PULLBACK"))
        if pullback is not None and pullback >= 0:
            plane_pullbacks.setdefault(38 + plane, (pullback, "plane"))
    for key, value in properties.items():
        match = re.fullmatch(r"V9_CACHE_LAYER(\d+)_PULLBACKDISTANCE", key)
        if not match:
            continue
        v6_id = _copper_v6(_optional_int(properties.get(f"V9_CACHE_LAYER{match.group(1)}_LAYERID")))
        pullback = parse_dim_mil(value)
        if v6_id is not None and _plane_layer(v6_id) and pullback is not None and pullback >= 0:
            plane_pullbacks.setdefault(v6_id, (pullback, "cache"))

    return BoardInfo(
        origin_x=parse_dim_mil(properties.get("ORIGINX")),
        origin_y=parse_dim_mil(properties.get("ORIGINY")),
        sheet_x=parse_dim_mil(properties.get("SHEETX")),
        sheet_y=parse_dim_mil(properties.get("SHEETY")),
        sheet_width=parse_dim_mil(properties.get("SHEETWIDTH")),
        sheet_height=parse_dim_mil(properties.get("SHEETHEIGHT")),
        display_unit=(
            "mil" if properties.get("DISPLAYUNIT", "1") == "1" else "mm"
        ),
        layers=layers,
        stackup_source=stackup_source,
        outline_vertices=shape_points(outline_shape),
        substacks=substacks,
        impedance_profiles=decode_impedance_profiles(properties),
        drill_pairs=decode_drill_pairs(properties),
        outline_shape=outline_shape,
        health_checks=_health_check_storage(properties.get("HEALTHCHECKSTORAGE")),
        plane_nets=plane_nets,
        plane_pullbacks=plane_pullbacks,
    )


def decode_nets(records: list[dict[str, str]]) -> list[str]:
    """Decode Nets6 record order into the PCB net-name table."""
    return [
        record.get("NAME", f"__unnamed_{index}")
        for index, record in enumerate(records)
    ]


def decode_components(
    records: list[dict[str, str]],
) -> list[PcbComponent]:
    """Decode Components6 placement and schematic-source metadata."""
    components: list[PcbComponent] = []
    for index, record in enumerate(records):
        source_uid = record.get("SOURCEUNIQUEID", "")
        rotation_raw = record.get("ROTATION")
        try:
            rotation = float(rotation_raw) if rotation_raw else None
        except ValueError:
            rotation = None
        components.append(
            PcbComponent(
                index=index,
                refdes=record.get("SOURCEDESIGNATOR") or None,
                source_uid_tail=(
                    source_uid.rsplit("\\", 1)[-1] if source_uid else None
                ),
                channel_path=(
                    record.get("SOURCEHIERARCHICALPATH") or None
                ),
                x=parse_dim_mil(record.get("X")),
                y=parse_dim_mil(record.get("Y")),
                rotation=rotation,
                side=(
                    "bottom"
                    if record.get("LAYER", "").upper() == "BOTTOM"
                    else "top"
                ),
                footprint=record.get("PATTERN") or None,
                kind=_component_kind(record),
                source_unique_id=source_uid or None,
                layer_name=record.get("LAYER", "").strip().upper(),
                footprint_library=record.get("SOURCEFOOTPRINTLIBRARY", "").strip(),
                component_library=record.get("SOURCECOMPONENTLIBRARY", "").strip(),
                lib_reference=record.get("SOURCELIBREFERENCE", "").strip(),
                workspace_part=bool(record.get("VAULTGUID") or record.get("ITEMGUID")),
                height_mil=parse_dim_mil(record.get("HEIGHT")),
            )
        )
    return components


def _component_kind(record: dict[str, str]) -> int:
    """ComponentKind: 0 Standard, 1 Mechanical, 2 Graphical, 3 Net Tie (In BOM),
    4 Net Tie, 5 Standard (No BOM), 6 Jumper. Altium writes the newer kinds
    backward-compatibly: Standard (No BOM) as ComponentKind=0 with
    ComponentKindVersion2=5, Jumper with ComponentKindVersion3=6; it reads them back
    in that order, as here."""
    return component_kind(
        record.get("COMPONENTKIND", ""),
        record.get("COMPONENTKINDVERSION2", ""),
        record.get("COMPONENTKINDVERSION3", ""),
    )


def component_kind(kind: str, version2: str, version3: str) -> int:
    """The component kind from ComponentKind and its two version keys."""
    def number(value: str) -> int:
        value = value.strip()
        return int(value) if value.isdigit() else 0

    if number(version3) == 6:
        return 6
    if number(version2) >= 5:
        return number(version2)
    return number(kind)


def _positional_layer(token: str) -> int | None:
    """v6 id of Altium's positional layer tokens (TOP, MIDn, BOTTOM, PLANEn),
    which a user's layer names must not shadow; None for other names."""
    match = re.fullmatch(r"(top|bottom|mid(\d+)|plane(\d+))", token)
    if not match:
        return None
    if match[1] == "top":
        return 1
    if match[1] == "bottom":
        return 32
    if match[2] is not None:
        number = int(match[2])
        return number + 1 if 1 <= number <= 30 else None
    number = int(match[3])
    return 38 + number if 1 <= number <= 16 else None


_CHAR_CODES_RE = re.compile(r"\d+(?:,\d+)*")


def _polygon_name(raw: str | None) -> str | None:
    """A Polygons6 NAME, None when missing or empty. Altium saves it as
    comma-separated UTF-16 code units ("71,78,68" is GND), as on every named
    pour of 89 boards (the public examples among them). A value in any other
    form, or whose codes would be control characters (a plain numeric name),
    is kept as written."""
    if not raw:
        return None
    if not _CHAR_CODES_RE.fullmatch(raw):
        return raw
    try:
        codes = [int(code) for code in raw.split(",")]
    except ValueError:  # more digits than int() reads
        return raw
    if any(code < 32 or code > 0xFFFF for code in codes):
        return raw
    # A surrogate pair is one character beyond the BMP; a lone one is U+FFFD.
    text = "".join(map(chr, codes))
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def decode_polygons(
    records: list[dict[str, str]],
    layer_name_to_v6: dict[str, int],
) -> tuple[list[PolygonPour], list[Primitive]]:
    """Decode pour outlines for containment and proximity queries.

    The outline (vertices, and its zero-width rows) follows arc edges by
    chords (shape_points). A shelved pour gets no outline rows: it has no
    copper for the proximity queries those rows serve.
    """
    pours: list[PolygonPour] = []
    edges: list[Primitive] = []
    for pour_id, record in enumerate(records):
        shape = shape_from_properties(record)
        vertices = shape_points(shape)
        net_raw = record.get("NET")
        token = record.get("LAYER", "").strip().lower()
        layer = _positional_layer(token)
        if layer is None:
            layer = layer_name_to_v6.get(token)
        pour = PolygonPour(
            pour_id=pour_id,
            net=int(net_raw) if net_raw and net_raw.isdigit() else None,
            layer=layer,
            hatch_style=record.get("HATCHSTYLE"),
            vertices=vertices,
            shelved=record.get("SHELVED", "").strip().upper() == "TRUE",
            copper_invalid=record.get("COPPERINVALIDATE", "").strip().upper() == "TRUE",
            polygon_type=(record.get("POLYGONTYPE") or "").strip() or None,
            shape=shape,
            name=_polygon_name(record.get("NAME")),
            grid_mil=parse_dim_mil(record.get("GRIDSIZE")),
            track_width_mil=parse_dim_mil(record.get("TRACKWIDTH")),
        )
        pours.append(pour)
        if layer is None or len(vertices) < 3 or pour.shelved:
            continue
        closed = vertices + [vertices[0]]
        for (x1, y1), (x2, y2) in zip(closed, closed[1:]):
            edges.append(
                Primitive(
                    kind="track",
                    layer=layer,
                    net=pour.net,
                    component=None,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=0.0,
                    pour_id=pour_id,
                )
            )
    return pours, edges


def _signal_layer(layer: int) -> bool:
    """A signal layer (1-32). Internal planes (39-54) are drawn in negative:
    a region or fill there is a void in the plane, not copper."""
    return 1 <= layer <= 32


def _plane_layer(layer: int) -> bool:
    """An internal plane layer (39-54, Internal Plane 1-16)."""
    return 39 <= layer <= 54


# Regions6 / Fills6 keepouts: flags2 (payload byte 2) is 2, as in KiCad's
# AREGION6 / AFILL6 (byte 1 holds the lock bits).
_KEEPOUT = 2
# A vertex beyond the int32 range of Altium's coordinates is corrupt.
_MAX_COORDINATE_MIL = 2**31 / ALTIUM_UNITS_PER_MIL


def _records_with_rest(data: bytes):
    """iter_binary_records, then (None, unread bytes) when a record's length
    runs past the stream (iter_binary_records stops there silently)."""
    used = 0
    for record_type, payload in iter_binary_records(data):
        used += 5 + len(payload)
        yield record_type, payload
    if used < len(data):
        yield None, data[used:]


def _read_vertices(payload: bytes, at: int) -> tuple[list[tuple[float, float]], int]:
    """(vertices in mil, offset after them) of a u32 count + count x (double x,
    double y) list in Altium units; ValueError when the payload is short."""
    if at + 4 > len(payload):
        raise ValueError("vertex count past the record")
    (count,) = struct.unpack_from("<I", payload, at)
    at += 4
    if at + 16 * count > len(payload):
        raise ValueError("vertices past the record")
    values = struct.unpack_from(f"<{2 * count}d", payload, at)
    if not all(
        math.isfinite(value) and abs(value) < _MAX_COORDINATE_MIL * ALTIUM_UNITS_PER_MIL
        for value in values
    ):
        raise ValueError("vertex outside the board coordinate range")
    vertices = [
        (values[i] / ALTIUM_UNITS_PER_MIL, values[i + 1] / ALTIUM_UNITS_PER_MIL)
        for i in range(0, 2 * count, 2)
    ]
    return vertices, at + 16 * count


# Regions6, ShapeBasedRegions6 and BoardRegions records (KiCad's AREGION6), u8
# type 11 and u32 length, then a head: layer u8 at 0, flags1 (lock bits) at 1,
# flags2 at 2 (_KEEPOUT), net u16 at 3, polygon u16 at 5 (the Polygons6 index of
# the pour the region is copper of), component u16 at 7, hole count u16 at 14,
# u32 text length at 18 and |KEY=VALUE text (KIND, ISBOARDCUTOUT, NAME, ...).
# The outline follows, then each hole as u32 count + (double x, double y).
_REGION_HEAD = 22


@dataclass(frozen=True)
class _RegionHead:
    layer: int
    keepout: bool
    net: int | None
    polygon: int | None  # 0xFFFF is none
    component: int | None
    hole_count: int
    properties: dict[str, str]
    shape_at: int  # offset of the outline


def _region_heads(data: bytes):
    """(record index, payload, head) per record of a region stream; head is None
    for a record that is not a region (another type, or shorter than the head)
    and for a record running past the stream, which the decoders count as
    skipped."""
    for index, (record_type, payload) in enumerate(_records_with_rest(data)):
        if record_type != 11 or len(payload) < _REGION_HEAD:
            yield index, payload, None
            continue
        net, polygon, component = struct.unpack_from("<HHH", payload, 3)
        (hole_count,) = struct.unpack_from("<H", payload, 14)
        (text_length,) = struct.unpack_from("<I", payload, 18)
        yield index, payload, _RegionHead(
            layer=payload[0],
            keepout=payload[2] == _KEEPOUT,
            net=_optional_index(net, NO_NET),
            polygon=_optional_index(polygon, 0xFFFF),
            component=_optional_index(component, NO_COMPONENT),
            hole_count=hole_count,
            properties=parse_text_properties(
                payload[_REGION_HEAD : _REGION_HEAD + text_length]
            ),
            shape_at=_REGION_HEAD + text_length,
        )


def decode_regions(data: bytes) -> tuple[list[CopperArea], int]:
    """Decode the copper of Regions6 records (_region_heads): kind 0 regions on
    signal layers that are not keepouts or board cutouts, the outline as u32
    count + (double x, double y) vertices.

    Polygon cutouts (KIND 1) and other kinds are not copper. A region of
    fewer than 3 vertices has no area and is left out; unreadable records
    (a vertex not finite or past the coordinate range, a record running past
    the stream) are counted as skipped.
    """
    areas: list[CopperArea] = []
    skipped = 0
    for _, payload, head in _region_heads(data):
        if head is None:
            skipped += 1
            continue
        if not _signal_layer(head.layer) or head.keepout:
            continue
        if (
            head.properties.get("KIND", "0").strip() != "0"
            or head.properties.get("ISBOARDCUTOUT", "").strip().upper() == "TRUE"
        ):
            continue
        try:
            outline, at = _read_vertices(payload, head.shape_at)
            holes = []
            for _ in range(head.hole_count):
                hole, at = _read_vertices(payload, at)
                if len(hole) >= 3:
                    holes.append(hole)
        except (ValueError, struct.error):
            skipped += 1
            continue
        if len(outline) < 3:
            continue
        areas.append(
            CopperArea(
                kind="region",
                layer=head.layer,
                net=head.net,
                component=head.component,
                outline=outline,
                holes=holes,
                pour_id=None if head.polygon in _NO_POLYGON else head.polygon,
            )
        )
    return areas, skipped


@dataclass
class PcbRegion:
    """A ShapeBasedRegions6, Regions6 or BoardRegions record (mil)."""

    index: int
    layer: int
    net: int | None
    polygon: int | None  # Polygons6 index of the pour it is copper of
    component: int | None
    keepout: bool
    kind: int  # KIND: 0 copper or graphics, 1 polygon cutout, 2 dashed, 3 board cutout
    board_cutout: bool
    vertices: list[ShapeVertex]
    holes: list[list[tuple[float, float]]]
    name: str = ""
    # The mechanical layer number its V7_LAYER names (MECHANICAL17), None when unset.
    mechanical: int | None = None
    # BoardRegions records: the layer stack (V9_SUBSTACKn_ID, BoardInfo.substacks) the
    # region uses (LAYERSTACKID) and its bending line count (BENDINGLINECOUNT); None and 0
    # elsewhere.
    layer_stack_id: str | None = None
    bending_lines: int = 0


_V7_LAYER_NAME = re.compile(r"MECHANICAL(\d+)", re.IGNORECASE)


def v7_layer_mechanical(layer_name: str | None) -> int | None:
    """The mechanical layer number of a V7_LAYER name such as MECHANICAL17."""
    match = _V7_LAYER_NAME.fullmatch((layer_name or "").strip())
    return int(match.group(1)) if match else None


def _read_shape_vertices(payload: bytes, at: int) -> tuple[list[ShapeVertex], int]:
    """(vertices, offset after them) of a ShapeBasedRegions6 outline: u32 count,
    then count + 1 of (u8 round, int32 x, y, cx, cy, r, double start, end angle)."""
    (count,) = struct.unpack_from("<I", payload, at)
    at += 4
    vertices: list[ShapeVertex] = []
    for _ in range(count + 1):
        round_flag = payload[at]
        x, y, cx, cy, radius = struct.unpack_from("<iiiii", payload, at + 1)
        start, end = struct.unpack_from("<dd", payload, at + 21)
        at += 37
        arc = (
            (_units_to_mil(cx), _units_to_mil(cy), _units_to_mil(radius), start, end)
            if round_flag and radius > 0 else None
        )
        vertices.append(ShapeVertex(_units_to_mil(x), _units_to_mil(y), arc))
    # The closing copy of the first vertex (written without its arc when the
    # first vertex is one).
    if len(vertices) > 1 and (vertices[-1].x, vertices[-1].y) == (
        vertices[0].x, vertices[0].y
    ) and vertices[-1].arc in (None, vertices[0].arc):
        vertices.pop()
    return vertices, at


def decode_region_records(data: bytes, shape_based: bool,
                          layers: set[int] | None = None) -> tuple[list[PcbRegion], int]:
    """Region records (_region_heads) with their shapes, on any layer: the outline
    as u32 count + (double x, double y) vertices (Regions6, BoardRegions) or as
    _read_shape_vertices reads it (shape_based: ShapeBasedRegions6, with arcs).
    Unreadable records and records on no valid layer are counted as skipped. layers, when
    given, keeps the records on those v6 layers only (the others are not read)."""
    regions: list[PcbRegion] = []
    skipped = 0
    for index, payload, head in _region_heads(data):
        if head is None or not _valid_layer(head.layer):
            skipped += 1
            continue
        if layers is not None and head.layer not in layers:
            continue
        try:
            if shape_based:
                vertices, at = _read_shape_vertices(payload, head.shape_at)
            else:
                points, at = _read_vertices(payload, head.shape_at)
                vertices = [ShapeVertex(x, y) for x, y in points]
                if len(vertices) > 1 and vertices[-1] == vertices[0]:
                    vertices.pop()  # the closing copy of the first vertex
            holes = []
            for _ in range(head.hole_count):
                hole, at = _read_vertices(payload, at)
                holes.append(hole)
        except (ValueError, struct.error, IndexError):
            skipped += 1
            continue
        kind = _optional_int(head.properties.get("KIND")) or 0
        regions.append(PcbRegion(
            index=index,
            layer=head.layer,
            net=head.net,
            polygon=head.polygon,
            component=head.component,
            keepout=head.keepout,
            kind=kind,
            board_cutout=head.properties.get("ISBOARDCUTOUT", "").upper() == "TRUE" or kind == 3,
            vertices=vertices,
            holes=holes,
            name=head.properties.get("NAME", ""),
            mechanical=v7_layer_mechanical(head.properties.get("V7_LAYER")),
            layer_stack_id=head.properties.get("LAYERSTACKID", "").strip() or None,
            bending_lines=_optional_int(head.properties.get("BENDINGLINECOUNT")) or 0,
        ))
    return regions, skipped


def read_board_cutouts(pcb: "PcbData") -> tuple[list[PcbRegion], int]:
    """(board cutouts, region records that could not be read) of a parsed board: its
    ShapeBasedRegions6 shapes with their arcs, or Regions6 without that stream (never
    BoardRegions). The DFM edge checks and the board view read the cutouts here."""
    raw = pcb.raw or {}
    shape_based = "ShapeBasedRegions6/Data" in raw
    data = raw.get("ShapeBasedRegions6/Data") or raw.get("Regions6/Data") or b""
    regions, unread = decode_region_records(data, shape_based)
    return [region for region in regions if region.board_cutout], unread


@dataclass
class ComponentBody:
    """A ComponentBodies6 record (or a footprint's body record in a PcbLib): a 3D body's
    2D outline (mil, absolute on a board, footprint-relative in a library) and heights."""

    component: int | None  # Components6 index; None for a free body
    layer: int  # v6 layer (a mechanical layer)
    outline: list[tuple[float, float]]
    overall_height_mil: float | None  # OVERALLHEIGHT
    standoff_height_mil: float | None  # STANDOFFHEIGHT
    projection: str | None  # BODYPROJECTION 0 "top", 1 "bottom"; None when unset
    model_type: int | None  # MODEL.MODELTYPE: 0 extruded, 1 STEP, 2 cylinder, 3 sphere
    # The 3D model: MODEL.NAME (the STEP file's name; None for a body without a model),
    # MODELID (the model's GUID) and MODEL.EMBED (the model is embedded in the file).
    model_name: str | None = None
    model_id: str | None = None
    model_embedded: bool | None = None
    # V7_LAYER as written (MECHANICAL1 ... MECHANICAL1024; None on older files): the
    # layer of a body on a mechanical layer above 16, which `layer` cannot name.
    v7_layer: str | None = None


# ComponentBodies6 records are type 12 with the region layout (_RegionHead): layer u8
# at 0, component u16 at 7, hole count u16 at 14, u32 text length at 18 and the text,
# then the outline as u32 count + (double x, double y) and each hole likewise.
_BODY_RECORD = 12
_BODY_PROJECTIONS = {"0": "top", "1": "bottom"}


def decode_component_bodies(data: bytes) -> tuple[list[ComponentBody], int]:
    """(bodies, skipped) of a ComponentBodies6 stream. Records of another type, shorter
    than the head, running past the stream or with unreadable vertices are skipped. An
    outline of fewer than 3 vertices is kept as read (it has no area)."""
    bodies: list[ComponentBody] = []
    skipped = 0
    for record_type, payload in _records_with_rest(data):
        if record_type != _BODY_RECORD or len(payload) < _REGION_HEAD:
            skipped += 1
            continue
        (component,) = struct.unpack_from("<H", payload, 7)
        (hole_count,) = struct.unpack_from("<H", payload, 14)
        (text_length,) = struct.unpack_from("<I", payload, 18)
        properties = parse_text_properties(payload[_REGION_HEAD : _REGION_HEAD + text_length])
        try:
            outline, at = _read_vertices(payload, _REGION_HEAD + text_length)
            for _ in range(hole_count):
                _, at = _read_vertices(payload, at)
        except (ValueError, struct.error):
            skipped += 1
            continue
        if len(outline) > 1 and outline[-1] == outline[0]:
            outline.pop()  # a closing copy of the first vertex
        overall = parse_dim_mil(properties.get("OVERALLHEIGHT"))
        standoff = parse_dim_mil(properties.get("STANDOFFHEIGHT"))
        if ("MODEL.EXTRUDED.MAXZ" not in properties and overall is not None
                and standoff is not None and standoff > overall):
            # An older record (no MODEL.EXTRUDED.MAXZ) may hold the two heights the other
            # way round: Altium places it from the lower to the higher (the SpiritLevel
            # example's library bodies against their placed copies).
            overall, standoff = standoff, overall
        bodies.append(ComponentBody(
            component=_optional_index(component, NO_COMPONENT),
            layer=payload[0],
            outline=outline,
            overall_height_mil=overall,
            standoff_height_mil=standoff,
            projection=_BODY_PROJECTIONS.get(properties.get("BODYPROJECTION", "").strip()),
            model_type=_optional_int(properties.get("MODEL.MODELTYPE")),
            model_name=properties.get("MODEL.NAME", "").strip() or None,
            model_id=properties.get("MODELID", "").strip() or None,
            model_embedded=(
                properties["MODEL.EMBED"].strip().upper() == "TRUE"
                if "MODEL.EMBED" in properties else None
            ),
            v7_layer=properties.get("V7_LAYER", "").strip() or None,
        ))
    return bodies, skipped


@dataclass
class PcbFill:
    """A Fills6 rectangle (mil): corners before rotation about their centre."""

    layer: int
    net: int | None
    component: int | None
    keepout: bool
    x1: float
    y1: float
    x2: float
    y2: float
    rotation: float
    mechanical: int | None = None  # as on Primitive (v7_mechanical)


def _fill_records(data: bytes):
    """Each Fills6 record (KiCad's AFILL6) as a PcbFill, rotation as stored (it may
    not be finite): layer u8 at 0, flags2 at 2 (_KEEPOUT), net u16 at 3, component
    u16 at 7, two corners (int32 x, y) at 13 and 21, a rotation (double, degrees,
    about the centre) at 29 and the V7 layer id at 42. None for a record that is not a
    fill (another type, or shorter than one) and for a record running past the stream,
    which the decoders count as skipped."""
    for record_type, payload in _records_with_rest(data):
        if record_type != 6 or len(payload) < 37:
            yield None
            continue
        net, _, component = struct.unpack_from("<HHH", payload, 3)
        x1, y1, x2, y2 = (
            _units_to_mil(value) for value in struct.unpack_from("<iiii", payload, 13)
        )
        (rotation,) = struct.unpack_from("<d", payload, 29)
        yield PcbFill(
            layer=payload[0],
            net=_optional_index(net, NO_NET),
            component=_optional_index(component, NO_COMPONENT),
            keepout=payload[2] == _KEEPOUT,
            x1=x1, y1=y1, x2=x2, y2=y2,
            rotation=rotation,
            mechanical=v7_mechanical(payload, _FILL_V7_AT),
        )


def fill_outline(fill: PcbFill) -> list[tuple[float, float]] | None:
    """A fill's rectangle rotated about its centre (mil); None for a fill with no
    area or a rotation that is not finite."""
    x1, y1, x2, y2, rotation = fill.x1, fill.y1, fill.x2, fill.y2, fill.rotation
    if x1 == x2 or y1 == y2 or not math.isfinite(rotation):
        return None
    center_x, center_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    cosine = math.cos(math.radians(rotation))
    sine = math.sin(math.radians(rotation))
    outline = []
    for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        dx, dy = x - center_x, y - center_y
        outline.append((
            center_x + dx * cosine - dy * sine,
            center_y + dx * sine + dy * cosine,
        ))
    return outline


def decode_fills(data: bytes) -> tuple[list[CopperArea], int]:
    """Decode copper Fills6 records (_fill_records): fills on signal layers that
    are not keepouts, as their rotated rectangle."""
    areas: list[CopperArea] = []
    skipped = 0
    for fill in _fill_records(data):
        if fill is None:
            skipped += 1
            continue
        if not _signal_layer(fill.layer) or fill.keepout:
            continue
        outline = fill_outline(fill)
        if outline is None:
            continue
        areas.append(
            CopperArea(
                kind="fill",
                layer=fill.layer,
                net=fill.net,
                component=fill.component,
                outline=outline,
            )
        )
    return areas, skipped


def decode_fill_records(data: bytes) -> tuple[list[PcbFill], int]:
    """Fills6 records (_fill_records) on any valid layer, a rotation that is not
    finite read as 0; records on no valid layer are counted as skipped."""
    fills: list[PcbFill] = []
    skipped = 0
    for fill in _fill_records(data):
        if fill is None or not _valid_layer(fill.layer):
            skipped += 1
            continue
        if not math.isfinite(fill.rotation):
            fill.rotation = 0.0
        fills.append(fill)
    return fills, skipped


def _component_texts(data: bytes):
    """Yield (first sub-record, text record) for each Texts6 text owned by a component.

    Each record is u8 type, u32 length, a fixed sub-record (component index
    u16 at offset 7, IsComment flag at offset 40, IsDesignator at 41), then u32
    length and a Pascal string holding the text.
    """
    offset = 0
    while offset + 5 <= len(data):
        (first_length,) = struct.unpack_from("<I", data, offset + 1)
        second_at = offset + 5 + first_length
        if second_at + 4 > len(data):
            break
        (second_length,) = struct.unpack_from("<I", data, second_at)
        if second_at + 4 + second_length > len(data):
            break
        first = data[offset + 5 : second_at]
        text = data[second_at + 4 : second_at + 4 + second_length]
        offset = second_at + 4 + second_length
        if first_length < 42 or not text:
            continue
        (component,) = struct.unpack_from("<H", first, 7)
        if component != NO_COMPONENT:
            yield component, first, text


def decode_designators(data: bytes) -> dict[int, str]:
    """Map component index -> designator text from Texts6."""
    designators: dict[int, str] = {}
    for component, first, text in _component_texts(data):
        if not first[41]:
            continue
        designator = text[1 : 1 + text[0]].decode("latin-1", "replace")
        if designator:
            designators.setdefault(component, designator)
    return designators


# Texts6 sub-record offset of the text's WideStrings6 index.
_WIDE_STRING_INDEX_AT = 115


def decode_wide_strings(data: bytes) -> dict[int, str]:
    """WideStrings6: u32 index, u32 byte length, UTF-16LE text ending in a NUL.

    Some files hold a record declared 2 bytes long with no body, the next record's
    header (index + 1) following at once; it is read as empty. A record that is
    otherwise malformed ends the walk, keeping what was read.
    """
    strings: dict[int, str] = {}
    offset = 0
    while offset + 8 <= len(data):
        index, length = struct.unpack_from("<II", data, offset)
        body = data[offset + 8 : offset + 8 + length]
        if length % 2 == 0 and len(body) == length and body.endswith(b"\x00\x00"):
            strings[index] = body.decode("utf-16-le", "replace").rstrip("\x00")
            offset += 8 + length
            continue
        if offset + 12 <= len(data) and struct.unpack_from(
            "<I", data, offset + 8
        )[0] == index + 1:
            strings[index] = ""
            offset += 8
            continue
        break
    return strings


def _same_text(narrow: str, wide: str) -> bool:
    """The Pascal string is the wide text with characters outside its code page
    replaced: they agree everywhere else."""
    return len(narrow) == len(wide) and all(
        a == b or a == "?" or ord(b) > 127 for a, b in zip(narrow, wide)
    )


def decode_comments(data: bytes, wide_strings: dict[int, str]) -> dict[int, str]:
    """Map component index -> comment text from Texts6.

    The Pascal string holds UTF-8 in recent files and an 8-bit code page in
    older ones, where characters such as Ω are lost; the full text is the
    WideStrings6 entry the sub-record names, used when it agrees with the
    Pascal string.
    """
    comments: dict[int, str] = {}
    for component, first, text in _component_texts(data):
        if not first[40] or component in comments:
            continue
        raw = text[1 : 1 + text[0]]
        narrow = raw.decode("latin-1")
        try:
            comment = raw.decode("utf-8")
        except UnicodeDecodeError:
            comment = narrow
        if len(first) >= _WIDE_STRING_INDEX_AT + 4:
            (index,) = struct.unpack_from("<I", first, _WIDE_STRING_INDEX_AT)
            wide = wide_strings.get(index)
            if wide and (wide == comment or _same_text(narrow, wide)):
                comment = wide
        comments[component] = comment
    return comments


@dataclass
class PcbRule:
    """One Rules6 design rule (rooms are RoomDefinition rules).

    properties keeps the kind-specific keys verbatim; services/pcb_rules
    turns them into values. Lower priority numbers win within a kind.
    """

    name: str
    kind: str
    kind_id: int
    enabled: bool
    priority: int
    scope1: str
    scope2: str
    net_scope: str | None
    comment: str
    unique_id: str | None
    from_schematic: bool
    properties: dict[str, str] = field(default_factory=dict)
    # The record had no NAME (name above is then '<kind> #n').
    unnamed: bool = False


@dataclass
class PcbClass:
    """A Classes6 / SignalClasses object class and its members.

    Superclasses ("All Nets", ...) have implicit membership and no members.
    Component and pad members use PCB designators ("U1", "U1-5").
    """

    name: str
    kind: str
    members: list[str]
    superclass: bool
    auto_generated: bool
    unique_id: str | None


@dataclass
class DiffPair:
    """A DifferentialPairs6 pair; nets are Nets6 names."""

    name: str
    positive_net: str
    negative_net: str
    unique_id: str | None


# Rules6 keys shared by every rule kind; the rest are kind-specific values.
# The primitive-base keys (SELECTION ... UNIONINDEX) carry no rule meaning,
# except LAYER on RoomDefinition, where it is the room's side.
_RULE_GENERIC_KEYS = frozenset({
    "SELECTION", "LAYER", "LOCKED", "POLYGONOUTLINE", "USERROUTED",
    "KEEPOUT", "UNIONINDEX", "RULEKIND", "NETSCOPE", "LAYERKIND",
    "SCOPE1EXPRESSION", "SCOPE2EXPRESSION", "NAME", "ENABLED", "PRIORITY",
    "COMMENT", "UNIQUEID", "DEFINEDBYLOGICALDOCUMENT",
})

# TClassMemberKind ordinals (KIND=); xSignal classes live in SignalClasses.
CLASS_KINDS = {
    0: "net",
    1: "component",
    2: "from_to",
    3: "pad",
    4: "layer",
    5: "design_channel",
    6: "diff_pair",
    7: "polygon",
    8: "split_plane",
    9: "object",
    10: "xsignal",
    11: "xnet",
    12: "xnet_class",
}

_MEMBER_KEY_RE = re.compile(r"M(\d+)")


def decode_rules(
    records: list[tuple[int, dict[str, str]]],
) -> list[PcbRule]:
    """Decode Rules6 records into rules, keeping kind values raw."""
    rules: list[PcbRule] = []
    for kind_id, record in records:
        kind = record.get("RULEKIND") or f"Kind{kind_id}"
        try:
            priority = int(record.get("PRIORITY", ""))
        except ValueError:
            priority = 1_000_000
        properties = {
            key: value
            for key, value in record.items()
            if key not in _RULE_GENERIC_KEYS
        }
        if kind == "RoomDefinition" and "LAYER" in record:
            properties["LAYER"] = record["LAYER"]
        rules.append(
            PcbRule(
                name=record.get("NAME") or f"{kind} #{len(rules) + 1}",
                unnamed=not (record.get("NAME") or "").strip(),
                kind=kind,
                kind_id=kind_id,
                enabled=record.get("ENABLED", "TRUE").upper() != "FALSE",
                priority=priority,
                scope1=record.get("SCOPE1EXPRESSION") or "All",
                scope2=record.get("SCOPE2EXPRESSION") or "All",
                net_scope=record.get("NETSCOPE") or None,
                comment=record.get("COMMENT", ""),
                unique_id=record.get("UNIQUEID") or None,
                from_schematic=(
                    record.get("DEFINEDBYLOGICALDOCUMENT", "").upper()
                    == "TRUE"
                ),
                properties=properties,
            )
        )
    return rules


def decode_classes(records: list[dict[str, str]]) -> list[PcbClass]:
    """Decode Classes6 / SignalClasses records."""
    classes: list[PcbClass] = []
    for record in records:
        kind_raw = record.get("KIND", "")
        kind = (
            CLASS_KINDS.get(int(kind_raw), f"unknown_{kind_raw}")
            if kind_raw.isdigit()
            else f"unknown_{kind_raw}"
        )
        indexed = sorted(
            (int(match.group(1)), value)
            for key, value in record.items()
            if (match := _MEMBER_KEY_RE.fullmatch(key))
        )
        # Duplicate members occur in real files; keep the first occurrence.
        members = list(dict.fromkeys(value for _, value in indexed if value))
        classes.append(
            PcbClass(
                name=record.get("NAME", ""),
                kind=kind,
                members=members,
                superclass=record.get("SUPERCLASS", "").upper() == "TRUE",
                auto_generated=(
                    record.get("AUTOGENERATEDCLASS", "").upper() == "TRUE"
                ),
                unique_id=record.get("UNIQUEID") or None,
            )
        )
    return classes


@dataclass
class XSignal:
    """A Board6 xSignal (PINPAIR*): a pad chain such as U1-W1,R4-1,R4-2,J1-3.

    Consecutive pads on one net are a routed leg; consecutive pads on one
    component are a hop through a series part.
    """

    name: str
    pins: list[str]


def decode_xsignals(properties: dict[str, str]) -> list[XSignal]:
    """Decode the xSignal definitions held in the Board6 record."""
    try:
        count = int(properties.get("PINPAIRCOUNT", "0") or 0)
    except ValueError:
        count = 0
    xsignals: list[XSignal] = []
    for index in range(count):
        name = properties.get(f"PINPAIRNAME{index}")
        pins = [
            pin.strip()
            for pin in properties.get(f"PINPAIRPINS{index}", "").split(",")
            if pin.strip()
        ]
        if name and pins:
            xsignals.append(XSignal(name=name, pins=pins))
    return xsignals


@dataclass
class DrcOptions:
    """The board's Design Rule Checker settings (Design Rule Checker Options6)."""

    max_violations: int | None = None  # 'Stop when n violations found' (default 500)
    batch_kinds: set[int] | None = None  # rule kind ids the batch DRC checks
    online_kinds: set[int] | None = None
    make_report: bool | None = None  # 'Create Report File'
    include_health: bool | None = None  # 'Report PCB Health Issues'
    sub_net_details: bool | None = None
    broken_planes: bool | None = None
    dead_copper: bool | None = None


def _kind_set(value: str | None) -> set[int] | None:
    if value is None:
        return None
    return {int(part) for part in value.split(",") if part.strip().isdigit()}


def _flag(value: str | None) -> bool | None:
    return None if value is None else value.strip().upper() == "TRUE"


def decode_drc_options(records: list[dict[str, str]]) -> DrcOptions | None:
    """DrcOptions from the Design Rule Checker Options6 record, None without one."""
    if not records:
        return None
    record = records[0]
    limit = record.get("MAXVIOLATIONCOUNT", "").strip()
    return DrcOptions(
        max_violations=int(limit) if limit.isdigit() else None,
        batch_kinds=_kind_set(record.get("RULESETTOCHECK")),
        online_kinds=_kind_set(record.get("ONLINERULESETTOCHECK")),
        make_report=_flag(record.get("DOMAKEDRCFILE")),
        include_health=_flag(record.get("INCLUDEPCBHEALTH")),
        sub_net_details=_flag(record.get("DOSUBNETDETAILS")),
        broken_planes=_flag(record.get("REPORTBROKENPLANES")),
        dead_copper=_flag(record.get("REPORTDEADCOPPER")),
    )


def decode_drc_rule_kinds(records: list[dict[str, str]]) -> set[int] | None:
    """Rule kind ids the board's batch DRC checks (RULESETTOCHECK)."""
    if not records or "RULESETTOCHECK" not in records[0]:
        return None
    return {
        int(part)
        for part in records[0]["RULESETTOCHECK"].split(",")
        if part.strip().isdigit()
    }


def decode_diff_pairs(records: list[dict[str, str]]) -> list[DiffPair]:
    """Decode DifferentialPairs6 records."""
    return [
        DiffPair(
            name=record.get("NAME", ""),
            positive_net=record.get("POSITIVENETNAME", ""),
            negative_net=record.get("NEGATIVENETNAME", ""),
            unique_id=record.get("UNIQUEID") or None,
        )
        for record in records
        if record.get("NAME")
    ]


# Streams kept raw on PcbData and decoded only by the checks that need them (they
# are large on poured boards): region shapes, fills and the board's stack regions.
LAZY_STREAMS = ("ShapeBasedRegions6/Data", "Regions6/Data", "Fills6/Data", "BoardRegions/Data")


@dataclass
class PcbData:
    """Parsed PCB content ready for indexing."""

    board: BoardInfo
    nets: list[str]
    components: list[PcbComponent]
    primitives: list[Primitive]
    pours: list[PolygonPour]
    layer_name_to_v6: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    skipped_records: dict[str, int] = field(default_factory=dict)
    rules: list[PcbRule] = field(default_factory=list)
    classes: list[PcbClass] = field(default_factory=list)
    diff_pairs: list[DiffPair] = field(default_factory=list)
    xsignals: list[XSignal] = field(default_factory=list)
    # Rules6 kind ids checked by the board's batch DRC; None when unknown.
    drc_rule_kinds: set[int] | None = None
    drc_options: DrcOptions | None = None
    # LAZY_STREAMS as read, for decode_region_records and decode_fill_records.
    raw: dict[str, bytes] = field(default_factory=dict)
    # Copper regions and fills (a shelved pour's dropped, a pour's copper
    # given its net); area_streams names the streams read ('Regions6',
    # 'Fills6'), so a missing stream is not mistaken for no area copper.
    areas: list[CopperArea] = field(default_factory=list)
    area_streams: tuple[str, ...] = ()
    # Pads6 pads without copper (mounting holes drawn as bare holes); decode_pads
    # counts them under skipped_records["Pads6"].
    bare_holes: list[BareHole] = field(default_factory=list)
    # EmbeddedBoards6 records: a panel's board arrays, whose boards are not read.
    embedded_boards: int = 0
    # ComponentBodies6: the parts' 3D bodies (outlines and heights).
    bodies: list[ComponentBody] = field(default_factory=list)


def _build_layer_aliases(board: BoardInfo) -> dict[str, int]:
    aliases = {
        layer.name.lower(): layer.v6_id
        for layer in board.layers
        if layer.v6_id is not None
    }
    aliases.setdefault("top", 1)
    aliases.setdefault("bottom", 32)
    # Altium's V6 signal-layer numbering is fixed regardless of stack size:
    # 1 = Top, 2..31 = Mid1..Mid30, 32 = Bottom. Polygons6 (and other text
    # streams) reference inner copper layers by this positional alias rather
    # than by LAYERnNAME, so it must be resolvable even though no board we've
    # seen actually populates all 30 mid slots.
    for mid_index in range(1, 31):
        aliases.setdefault(f"mid{mid_index}", mid_index + 1)
    # Internal planes likewise: Polygons6 names a split plane's layer PLANEn
    # (v6 38 + n).
    for plane_index in range(1, 17):
        aliases.setdefault(f"plane{plane_index}", 38 + plane_index)
    return aliases


def _resolve_pour_copper(
    primitives: list[Primitive], pours: list[PolygonPour]
) -> tuple[list[Primitive], int]:
    """(primitives, dropped records): poured copper gets its pour's net, and
    a shelved pour's is dropped (an arc, all its chords, is one record).

    Altium saves a hatched pour's tracks and arcs without a net (the pour
    holds it); they are that net's copper. A shelved pour keeps its copper in
    the file but hidden from DRC and connectivity. A pour index past the
    Polygons6 records keeps no net.
    """
    kept: list[Primitive] = []
    dropped: set = set()
    for primitive in primitives:
        if primitive.poured_by is not None and primitive.poured_by < len(pours):
            pour = pours[primitive.poured_by]
            if pour.shelved:
                dropped.add(
                    ("arc", primitive.source_id)
                    if primitive.kind == "arc"
                    else ("track", id(primitive))
                )
                continue
            if primitive.net is None:
                primitive.net = pour.net
        kept.append(primitive)
    return kept, len(dropped)


def _resolve_pour_areas(
    areas: list[CopperArea], pours: list[PolygonPour]
) -> tuple[list[CopperArea], int]:
    """(areas, dropped): a solid pour's regions get its net, and a shelved
    pour's are dropped, as _resolve_pour_copper does for hatched copper."""
    kept: list[CopperArea] = []
    dropped = 0
    for area in areas:
        if area.pour_id is not None and area.pour_id < len(pours):
            pour = pours[area.pour_id]
            if pour.shelved:
                dropped += 1
                continue
            if area.net is None:
                area.net = pour.net
        kept.append(area)
    return kept, dropped


def parse_streams(streams: dict[str, bytes]) -> PcbData:
    """Assemble parsed PCB data from raw OLE stream bytes."""
    warnings: list[str] = []
    skipped_records: dict[str, int] = {}

    def text(stream: str) -> list[dict[str, str]]:
        if stream not in streams:
            warnings.append(f"{stream.split('/')[0]} stream missing")
            return []
        try:
            return read_text_records(streams[stream])
        except Exception as error:
            warnings.append(
                f"{stream.split('/')[0]} parse failed: {error}"
            )
            return []

    board_records = text("Board6/Data")
    try:
        board = decode_board(board_records)
    except Exception as error:
        warnings.append(f"Board6 parse failed: {error}")
        board = decode_board([])
    try:
        xsignals = decode_xsignals(board_records[0] if board_records else {})
    except Exception as error:
        warnings.append(f"xSignals parse failed: {error}")
        xsignals = []
    try:
        nets = decode_nets(text("Nets6/Data"))
    except Exception as error:
        warnings.append(f"Nets6 parse failed: {error}")
        nets = []
    try:
        components = decode_components(text("Components6/Data"))
    except Exception as error:
        warnings.append(f"Components6 parse failed: {error}")
        components = []
    layer_aliases = _build_layer_aliases(board)

    primitives: list[Primitive] = []
    binary_decoders = (
        ("Tracks6/Data", decode_tracks),
        ("Vias6/Data", decode_vias),
        ("Arcs6/Data", decode_arcs),
        ("Pads6/Data", decode_pads),
    )
    for stream, decoder in binary_decoders:
        stream_name = stream.split("/")[0]
        if stream not in streams:
            warnings.append(f"{stream_name} stream missing")
            continue
        try:
            decoded, skipped = decoder(streams[stream])
        except Exception as error:
            warnings.append(f"{stream_name} parse failed: {error}")
            continue
        primitives.extend(decoded)
        if skipped:
            skipped_records[stream_name] = skipped
    bare_holes: list[BareHole] = []
    if "Pads6/Data" in streams:
        try:
            bare_holes = decode_bare_holes(streams["Pads6/Data"])
        except Exception as error:
            warnings.append(f"Pads6 bare holes parse failed: {error}")
    embedded_boards = 0
    if "EmbeddedBoards6/Data" in streams:
        try:
            embedded_boards = len(read_text_records(streams["EmbeddedBoards6/Data"]))
        except Exception as error:
            warnings.append(f"EmbeddedBoards6 parse failed: {error}")
    bodies: list[ComponentBody] = []
    if "ComponentBodies6/Data" in streams:  # optional: a board may have no bodies
        try:
            bodies, skipped = decode_component_bodies(streams["ComponentBodies6/Data"])
            if skipped:
                skipped_records["ComponentBodies6"] = skipped
        except Exception as error:
            warnings.append(f"ComponentBodies6 parse failed: {error}")

    pours: list[PolygonPour] = []
    if "Polygons6/Data" in streams:
        try:
            pours, edges = decode_polygons(
                text("Polygons6/Data"),
                layer_aliases,
            )
            primitives.extend(edges)
        except Exception as error:
            warnings.append(f"Polygons6 parse failed: {error}")
    else:
        warnings.append("Polygons6 stream missing")
    primitives, shelved_copper = _resolve_pour_copper(primitives, pours)

    areas: list[CopperArea] = []
    area_streams: list[str] = []
    for stream, decoder in (
        ("Regions6/Data", decode_regions),
        ("Fills6/Data", decode_fills),
    ):
        # Optional: boards without regions or fills may lack the stream.
        if stream not in streams:
            continue
        stream_name = stream.split("/")[0]
        try:
            decoded, skipped = decoder(streams[stream])
        except Exception as error:
            warnings.append(f"{stream_name} parse failed: {error}")
            continue
        areas.extend(decoded)
        if skipped:
            # Some copper could not be read: the stream does not count as read,
            # so a pour without readable copper is not taken for an unpoured one.
            skipped_records[stream_name] = skipped
            warnings.append(f"{stream_name}: {skipped} records could not be read")
        else:
            area_streams.append(stream_name)
    areas, shelved_areas = _resolve_pour_areas(areas, pours)
    shelved_copper += shelved_areas
    if shelved_copper:
        skipped_records["shelved_pour_copper"] = shelved_copper

    valid_areas = [area for area in areas if area.net is None or area.net < len(nets)]
    invalid_net_count = len(areas) - len(valid_areas)
    valid_primitives = []
    for primitive in primitives:
        if primitive.net is not None and primitive.net >= len(nets):
            invalid_net_count += 1
            continue
        valid_primitives.append(primitive)
    if invalid_net_count:
        skipped_records["net_range"] = invalid_net_count

    if "Texts6/Data" in streams:
        try:
            designators = decode_designators(streams["Texts6/Data"])
            for component in components:
                component.designator = designators.get(component.index)
        except Exception as error:
            warnings.append(f"Texts6 parse failed: {error}")
        try:
            wide_strings = (
                decode_wide_strings(streams["WideStrings6/Data"])
                if "WideStrings6/Data" in streams
                else {}
            )
            comments = decode_comments(streams["Texts6/Data"], wide_strings)
            for component in components:
                component.comment = comments.get(component.index)
        except Exception as error:
            warnings.append(f"Texts6 comment parse failed: {error}")

    # Rules, classes and pairs are optional: older or simple boards may lack
    # a stream, and an empty DifferentialPairs6 is normal. Only failures warn.
    rules: list[PcbRule] = []
    if "Rules6/Data" in streams:
        try:
            rule_records, _ = read_kind_prefixed_text_records(
                streams["Rules6/Data"]
            )
            rules = decode_rules(rule_records)
            header = streams.get("Rules6/Header", b"")
            if len(header) >= 4:
                (declared,) = struct.unpack_from("<I", header, 0)
                if declared > len(rules):
                    skipped_records["Rules6"] = declared - len(rules)
        except Exception as error:
            warnings.append(f"Rules6 parse failed: {error}")
    classes: list[PcbClass] = []
    for stream in ("Classes6/Data", "SignalClasses/Data"):
        if stream not in streams:
            continue
        try:
            classes.extend(decode_classes(read_text_records(streams[stream])))
        except Exception as error:
            warnings.append(f"{stream.split('/')[0]} parse failed: {error}")
    drc_rule_kinds: set[int] | None = None
    drc_options: DrcOptions | None = None
    if "Design Rule Checker Options6/Data" in streams:
        try:
            records = read_text_records(streams["Design Rule Checker Options6/Data"])
            drc_rule_kinds = decode_drc_rule_kinds(records)
            drc_options = decode_drc_options(records)
        except Exception as error:
            warnings.append(f"Design Rule Checker Options6 parse failed: {error}")
    diff_pairs: list[DiffPair] = []
    if "DifferentialPairs6/Data" in streams:
        try:
            diff_pairs = decode_diff_pairs(
                read_text_records(streams["DifferentialPairs6/Data"])
            )
        except Exception as error:
            warnings.append(f"DifferentialPairs6 parse failed: {error}")

    return PcbData(
        board=board,
        nets=nets,
        components=components,
        primitives=valid_primitives,
        pours=pours,
        layer_name_to_v6=layer_aliases,
        warnings=warnings,
        skipped_records=skipped_records,
        rules=rules,
        classes=classes,
        diff_pairs=diff_pairs,
        xsignals=xsignals,
        drc_rule_kinds=drc_rule_kinds,
        drc_options=drc_options,
        raw={stream: streams[stream] for stream in LAZY_STREAMS if stream in streams},
        areas=valid_areas,
        area_streams=tuple(area_streams),
        bare_holes=bare_holes,
        embedded_boards=embedded_boards,
        bodies=bodies,
    )


_WANTED_STREAMS = (
    "Board6/Data",
    "Nets6/Data",
    "Components6/Data",
    "Tracks6/Data",
    "Vias6/Data",
    "Arcs6/Data",
    "Pads6/Data",
    "Polygons6/Data",
    "Regions6/Data",
    "Fills6/Data",
    "Texts6/Data",
    "WideStrings6/Data",
    "Rules6/Data",
    "Rules6/Header",
    "Classes6/Data",
    "SignalClasses/Data",
    "DifferentialPairs6/Data",
    "Design Rule Checker Options6/Data",
    "EmbeddedBoards6/Data",
    "ComponentBodies6/Data",
)


def parse_pcb_doc(path: str) -> PcbData:
    """Read a .PcbDoc OLE file and parse the streams used by PCB tools."""
    streams: dict[str, bytes] = {}
    read_warnings: list[str] = []
    with olefile.OleFileIO(path) as ole:
        available = {"/".join(stream) for stream in ole.listdir()}
        wanted = [*_WANTED_STREAMS,
                  *(stream for stream in LAZY_STREAMS if stream not in _WANTED_STREAMS)]
        for stream in wanted:
            if stream in available:
                try:
                    streams[stream] = ole.openstream(stream).read()
                except Exception as error:
                    read_warnings.append(
                        f"{stream.split('/')[0]} read failed: {error}"
                    )
    parsed = parse_streams(streams)
    parsed.warnings = read_warnings + parsed.warnings
    return parsed
