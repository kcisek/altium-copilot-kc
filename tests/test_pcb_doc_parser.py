import math
import struct
from pathlib import Path

import pytest

from server.parsers.pcb_doc import (
    ARC_MAX_SAGITTA_MIL,
    NO_NET,
    PAD_INNER_LAYERS,
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    PAD_SHAPE_ROUNDRECT,
    PadCopper,
    PcbData,
    pad_copper_on,
    decode_arcs,
    decode_bare_holes,
    decode_board,
    decode_components,
    decode_nets,
    decode_pads,
    decode_polygons,
    decode_tracks,
    decode_vias,
    decode_xsignals,
    iter_binary_records,
    parse_dim_mil,
    decode_classes,
    decode_comments,
    decode_designators,
    decode_diff_pairs,
    decode_drc_rule_kinds,
    decode_drc_options,
    DrcOptions,
    decode_rules,
    parse_streams,
    read_kind_prefixed_text_records,
    read_text_records,
    decode_wide_strings,
)
from server.parsers.pcb_doc import _build_layer_aliases
from server.services.pcb_geometry import point_in_polygon


def _text_record(props: dict[str, str]) -> bytes:
    body = (
        ("|" + "|".join(f"{k}={v}" for k, v in props.items())).encode("latin-1")
        + b"\x00"
    )
    return struct.pack("<I", len(body)) + body


def test_read_text_records_parses_props():
    data = _text_record({"NAME": "GND"}) + _text_record({"NAME": "3V3", "X": "5"})
    recs = read_text_records(data)
    assert recs == [{"NAME": "GND"}, {"NAME": "3V3", "X": "5"}]


def test_read_text_records_stops_on_truncated_record():
    data = _text_record({"NAME": "GND"}) + struct.pack("<I", 9999) + b"|A=B"
    assert read_text_records(data) == [{"NAME": "GND"}]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5mil", 5.0),
        ("2.7559mil", 2.7559),
        ("0.127mm", 5.0),
        ("1000", 1000.0),
        (" 1.80000000000000E+0002", 180.0),
    ],
)
def test_parse_dim_mil(raw, expected):
    assert parse_dim_mil(raw) == pytest.approx(expected, abs=1e-3)


def test_parse_dim_mil_invalid_returns_none():
    assert parse_dim_mil("") is None
    assert parse_dim_mil("abc") is None
    assert parse_dim_mil(None) is None


def _bin_record(record_type: int, payload: bytes) -> bytes:
    return bytes([record_type]) + struct.pack("<I", len(payload)) + payload


def _track_payload(
    layer=1,
    net=0,
    component=0xFFFF,
    x1=1000_0000,
    y1=1000_0000,
    x2=1050_0000,
    y2=1000_0000,
    width=6_0000,
    polygon=0xFFFF,
) -> bytes:
    payload = bytearray(49)
    payload[0] = layer
    struct.pack_into("<HHH", payload, 3, net, polygon, component)
    struct.pack_into("<iiiii", payload, 13, x1, y1, x2, y2, width)
    return bytes(payload)


def _via_payload(layer_from=1, layer_to=32, flags1=0, flags2=0) -> bytes:
    payload = bytearray(80)
    payload[1], payload[2] = flags1, flags2
    struct.pack_into("<H", payload, 3, 5)
    struct.pack_into("<H", payload, 7, 0xFFFF)
    struct.pack_into(
        "<iiii",
        payload,
        13,
        200_0000,
        300_0000,
        20_0000,
        8_0000,
    )
    payload[29], payload[30] = layer_from, layer_to
    return bytes(payload)


def _arc_payload(layer=1, radius=1_0000, net=2, polygon=0xFFFF) -> bytes:
    payload = bytearray(45)
    payload[0] = layer
    struct.pack_into("<HH", payload, 3, net, polygon)
    struct.pack_into("<ii", payload, 13, 100_0000, 100_0000)
    struct.pack_into("<i", payload, 21, radius)
    struct.pack_into("<dd", payload, 25, 0.0, 90.0)
    struct.pack_into("<i", payload, 41, 2000)
    return bytes(payload)


def test_iter_binary_records_frames_correctly():
    data = _bin_record(4, b"\x01" * 49) + _bin_record(4, b"\x02" * 49)
    records = list(iter_binary_records(data))
    assert len(records) == 2
    assert records[0] == (4, b"\x01" * 49)


def test_iter_binary_records_stops_on_truncation():
    data = _bin_record(4, b"\x01" * 49) + b"\x04" + struct.pack("<I", 9999)
    assert len(list(iter_binary_records(data))) == 1


def test_decode_tracks():
    data = _bin_record(4, _track_payload(layer=3, net=7, width=6_0000))
    primitives, skipped = decode_tracks(data)
    assert skipped == 0
    track = primitives[0]
    assert (track.kind, track.layer, track.net, track.width) == (
        "track",
        3,
        7,
        6.0,
    )
    assert (track.x1, track.y1, track.x2, track.y2) == (
        1000.0,
        1000.0,
        1050.0,
        1000.0,
    )


def test_decode_tracks_no_net_and_short_payload_skipped():
    good = _bin_record(4, _track_payload(net=NO_NET))
    short = _bin_record(4, b"\x01" * 10)
    primitives, skipped = decode_tracks(good + short)
    assert primitives[0].net is None
    assert skipped == 1


def test_decode_tracks_out_of_range_layer_skipped():
    primitives, skipped = decode_tracks(_bin_record(4, _track_payload(layer=0)))
    assert primitives == []
    assert skipped == 1


def test_decode_tracks_rejects_wrong_type_and_nonpositive_width():
    wrong_type = _bin_record(3, _track_payload())
    negative_width = _bin_record(4, _track_payload(width=-1))
    primitives, skipped = decode_tracks(wrong_type + negative_width)
    assert primitives == []
    assert skipped == 2


def test_decode_tracks_reads_the_polygon_index():
    # Offset 5: the Polygons6 index of a hatched pour the track is copper of;
    # 0xFFFF is routing, 0xFFFE (board outline, as KiCad reads it) is too.
    data = b"".join(
        _bin_record(4, _track_payload(net=NO_NET, polygon=polygon))
        for polygon in (3, 0xFFFF, 0xFFFE)
    )
    primitives, skipped = decode_tracks(data)
    assert skipped == 0
    assert [track.poured_by for track in primitives] == [3, None, None]
    assert all(track.pour_id is None for track in primitives)


def test_decode_arcs_reads_the_polygon_index():
    poured, _ = decode_arcs(_bin_record(1, _arc_payload(net=NO_NET, polygon=2)))
    routed, _ = decode_arcs(_bin_record(1, _arc_payload()))
    assert poured and {chord.poured_by for chord in poured} == {2}
    assert {chord.poured_by for chord in routed} == {None}


def test_decode_vias():
    primitives, skipped = decode_vias(_bin_record(3, _via_payload()))
    via = primitives[0]
    assert (via.kind, via.net, via.layer, via.layer_to) == ("via", 5, 1, 32)
    assert (via.x1, via.y1, via.width) == (200.0, 300.0, 20.0)
    assert via.hole == 8.0
    assert skipped == 0


def test_decode_vias_out_of_range_layer_skipped():
    primitives, skipped = decode_vias(
        _bin_record(3, _via_payload(layer_from=200))
    )
    assert primitives == []
    assert skipped == 1


def test_decode_arcs_explodes_to_segments():
    primitives, skipped = decode_arcs(_bin_record(1, _arc_payload()))
    assert skipped == 0
    assert all(segment.kind == "arc" for segment in primitives)
    assert {segment.source_id for segment in primitives} == {0}
    assert len(primitives) >= 2
    first, last = primitives[0], primitives[-1]
    assert (first.x1, first.y1) == (
        pytest.approx(101.0),
        pytest.approx(100.0),
    )
    assert (last.x2, last.y2) == (
        pytest.approx(100.0),
        pytest.approx(101.0),
    )


def test_decode_arcs_keeps_the_arc_on_every_chord():
    primitives, _ = decode_arcs(_bin_record(1, _arc_payload(radius=25_0000)))
    assert {(chord.arc_x, chord.arc_y, chord.arc_radius) for chord in primitives} == {
        (100.0, 100.0, 25.0)
    }
    for chord in primitives:
        for x, y in ((chord.x1, chord.y1), (chord.x2, chord.y2)):
            assert math.hypot(x - 100.0, y - 100.0) == pytest.approx(25.0)


def test_decode_arcs_out_of_range_layer_skipped():
    primitives, skipped = decode_arcs(_bin_record(1, _arc_payload(layer=200)))
    assert primitives == []
    assert skipped == 1


def test_decode_arcs_skips_nan_record_without_losing_healthy_records():
    malformed = bytearray(_arc_payload())
    struct.pack_into("<d", malformed, 25, float("nan"))
    data = _bin_record(1, bytes(malformed)) + _bin_record(1, _arc_payload())
    primitives, skipped = decode_arcs(data)
    assert skipped == 1
    assert primitives


def test_decode_arcs_adapts_chords_for_large_radius_length_accuracy():
    primitives, skipped = decode_arcs(
        _bin_record(1, _arc_payload(radius=1000_0000))
    )
    length = sum(
        math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1)
        for segment in primitives
    )
    assert skipped == 0
    assert length == pytest.approx(math.pi * 500.0, abs=0.5)


def _pad_record(
    name="1",
    layer=1,
    net=3,
    component=5,
    x=150_0000,
    y=250_0000,
    sx=2_4000,
    sy=3_5000,
    hole=0,
    rotation=90.0,
    shape=2,
    extra_sub=b"",
    mode=0,
    bottom_size=None,
    bottom_shape=None,
    mid_size=None,
    mid_shape=None,
    plated=None,
    flags1=0,
    flags2=0,
) -> bytes:
    name_bytes = name.encode("latin-1")
    name_blob = bytes([len(name_bytes)]) + name_bytes
    main = bytearray(185)
    main[0] = layer
    main[1], main[2] = flags1, flags2
    struct.pack_into("<H", main, 3, net)
    struct.pack_into("<H", main, 7, component)
    struct.pack_into("<ii", main, 13, x, y)
    bottom_x, bottom_y = bottom_size or (sx, sy)
    mid_x, mid_y = mid_size or (sx, sy)
    struct.pack_into(
        "<iiiiiii", main, 21, sx, sy, mid_x, mid_y, bottom_x, bottom_y, hole
    )
    main[49] = shape
    main[50] = shape if mid_shape is None else mid_shape
    main[51] = shape if bottom_shape is None else bottom_shape
    struct.pack_into("<d", main, 52, rotation)
    if plated is not None:
        main[60] = int(plated)
    main[62] = mode
    subrecords = [b"\x00", b"\x00" * 5, b"\x00", bytes(main), extra_sub]
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in subrecords:
        body += struct.pack("<I", len(subrecord)) + subrecord
    return b"\x02" + body


def test_decode_pads():
    data = _pad_record(name="A7", net=3, component=5, rotation=90.0)
    primitives, skipped = decode_pads(data)
    assert skipped == 0
    pad = primitives[0]
    assert (pad.kind, pad.pad_name, pad.layer, pad.net, pad.component) == (
        "pad",
        "A7",
        1,
        3,
        5,
    )
    assert (pad.x1, pad.y1) == (150.0, 250.0)
    assert (pad.width, pad.height) == (2.4, 3.5)
    assert (pad.shape, pad.rotation) == (2, 90.0)
    assert pad.hole == 0.0


def test_decode_pads_with_extended_subrecord():
    data = _pad_record(extra_sub=b"\x00" * 651) + _pad_record(name="2")
    primitives, skipped = decode_pads(data)
    assert [pad.pad_name for pad in primitives] == ["1", "2"]
    assert skipped == 0


def test_decode_pads_short_main_subrecord_skipped():
    name_blob = b"\x011"
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in [b"\x00", b"\x00" * 5, b"\x00", b"\x00" * 20, b""]:
        body += struct.pack("<I", len(subrecord)) + subrecord
    primitives, skipped = decode_pads(b"\x02" + body)
    assert primitives == []
    assert skipped == 1


def test_decode_pads_out_of_range_layer_skipped():
    primitives, skipped = decode_pads(_pad_record(layer=200))
    assert primitives == []
    assert skipped == 1


def _size_and_shape(alt=None, radius=None, length=651, inner=None) -> bytes:
    """A size-and-shape subrecord: {slot: alt shape} at 532 and {slot:
    corner radius percent} at 564 (slot 0 top, 31 bottom), and {k: (x, y,
    shape)} of mid layer k + 2 (x at 4k, y at 116 + 4k, shape at 232 + k)."""
    record = bytearray(length)
    for k, (size_x, size_y, shape) in (inner or {}).items():
        struct.pack_into("<i", record, 4 * k, size_x)
        struct.pack_into("<i", record, 116 + 4 * k, size_y)
        record[232 + k] = shape
    for slot, value in (alt or {}).items():
        record[532 + slot] = value
    for slot, value in (radius or {}).items():
        record[564 + slot] = value
    return bytes(record)


def test_decode_pads_rounded_rectangle():
    data = _pad_record(
        sx=40_0000,
        sy=60_0000,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 25}),
    )
    (pad,), skipped = decode_pads(data)
    assert skipped == 0
    assert (pad.shape, pad.width, pad.height) == (
        PAD_SHAPE_ROUNDRECT,
        40.0,
        60.0,
    )
    # 25 % of half the shorter side.
    assert pad.corner_radius == pytest.approx(5.0)


@pytest.mark.parametrize(
    "shape, size_and_shape",
    [
        (1, _size_and_shape(alt={0: 1})),
        # Only a round main shape becomes a rounded rectangle (as in KiCad).
        (2, _size_and_shape(alt={0: 9}, radius={0: 25})),
        # Too short to hold the alt shapes.
        (1, _size_and_shape(alt={0: 9}, radius={0: 25}, length=595)),
        (1, b""),
    ],
    ids=["round", "rect", "short-subrecord", "no-subrecord"],
)
def test_decode_pads_without_rounded_corners(shape, size_and_shape):
    (pad,), _ = decode_pads(
        _pad_record(shape=shape, extra_sub=size_and_shape)
    )
    assert (pad.shape, pad.corner_radius) == (shape, None)


def test_decode_pads_corner_radius_capped_at_oblong():
    data = _pad_record(
        sx=40_0000,
        sy=60_0000,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 150}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.corner_radius == pytest.approx(20.0)


def test_decode_pads_full_corner_radius_is_exactly_half_the_short_side():
    data = _pad_record(
        sx=78,
        sy=100,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 100}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.corner_radius == pad.width / 2.0


def test_decode_pads_bottom_pad_of_full_stack_uses_bottom_slot():
    # Altium leaves the top slot of such a pad empty: it was dropped before.
    data = _pad_record(
        layer=32,
        sx=0,
        sy=0,
        shape=1,
        mode=2,
        bottom_size=(40_0000, 60_0000),
        bottom_shape=1,
        extra_sub=_size_and_shape(
            alt={0: 1, 31: 9}, radius={31: 50}, length=596
        ),
    )
    (pad,), skipped = decode_pads(data)
    assert skipped == 0
    assert (pad.layer, pad.width, pad.height, pad.shape) == (
        32,
        40.0,
        60.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert pad.corner_radius == pytest.approx(10.0)


def _stack_pad(layer, mode, sx=2_4000, sy=3_5000, bottom_size=(40_0000, 60_0000)):
    """A pad whose top slot is a 2.4 x 3.5 mil round pad and whose other
    slots are rounded rectangles: mid 20 x 30 (50 %), a full stack's layer 4
    (mid layer 3) 50 x 70 (20 %), bottom 40 x 60 (50 %)."""
    return _pad_record(
        layer=layer,
        sx=sx,
        sy=sy,
        shape=1,
        mode=mode,
        mid_size=(20_0000, 30_0000),
        bottom_size=bottom_size,
        extra_sub=_size_and_shape(
            alt={0: 1, 1: 9, 3: 9, 31: 9},
            radius={1: 50, 3: 20, 31: 50},
            inner={1: (50_0000, 70_0000, 1)},
        ),
    )


@pytest.mark.parametrize(
    "layer, mode, expected",
    [
        (32, 1, (40.0, 60.0, PAD_SHAPE_ROUNDRECT, 10.0)),
        (32, 2, (40.0, 60.0, PAD_SHAPE_ROUNDRECT, 10.0)),
        # Only modes 1 and 2 keep per-layer copper.
        (32, 0, (2.4, 3.5, 1, None)),
        (32, 7, (2.4, 3.5, 1, None)),
        (1, 2, (2.4, 3.5, 1, None)),
        (74, 1, (2.4, 3.5, 1, None)),
        # Inner layers: mid slot (alt slot 1) in mode 1 and for mid layer 1.
        (5, 1, (20.0, 30.0, PAD_SHAPE_ROUNDRECT, 5.0)),
        (2, 2, (20.0, 30.0, PAD_SHAPE_ROUNDRECT, 5.0)),
        # Mid layer 3 of a full stack: inner size 1, alt slot 3.
        (4, 2, (50.0, 70.0, PAD_SHAPE_ROUNDRECT, 5.0)),
    ],
)
def test_decode_pads_per_layer_stack(layer, mode, expected):
    (pad,), skipped = decode_pads(_stack_pad(layer, mode))
    assert skipped == 0
    assert (pad.width, pad.height, pad.shape) == pytest.approx(expected[:3])
    if expected[3] is None:
        assert pad.corner_radius is None
    else:
        assert pad.corner_radius == pytest.approx(expected[3])


@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize(
    "top_shape, bottom_shape, expected",
    [(2, 1, PAD_SHAPE_ROUNDRECT), (1, 2, 2)],
)
def test_decode_pads_bottom_pad_takes_the_bottom_shape(
    mode, top_shape, bottom_shape, expected
):
    data = _pad_record(
        layer=32,
        shape=top_shape,
        mode=mode,
        bottom_size=(40_0000, 60_0000),
        bottom_shape=bottom_shape,
        extra_sub=_size_and_shape(alt={0: 9, 31: 9}, radius={0: 50, 31: 50}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.shape == expected


def test_decode_pads_per_layer_stack_with_empty_slot_takes_a_filled_one():
    (bottom,), _ = decode_pads(_stack_pad(32, 1, bottom_size=(0, 0)))
    assert (bottom.width, bottom.height, bottom.corner_radius) == (
        2.4,
        3.5,
        None,
    )
    (multi,), skipped = decode_pads(_stack_pad(74, 2, sx=0, sy=0))
    assert skipped == 0
    assert (multi.width, multi.height, multi.shape) == (
        20.0,
        30.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert multi.corner_radius == pytest.approx(5.0)


def test_decode_pads_bottom_pad_of_simple_stack_uses_top_slot():
    data = _pad_record(
        layer=32,
        sx=40_0000,
        sy=60_0000,
        shape=1,
        bottom_size=(1, 1),
        bottom_shape=2,
        extra_sub=_size_and_shape(alt={0: 9, 31: 1}, radius={0: 50}),
    )
    (pad,), _ = decode_pads(data)
    assert (pad.width, pad.height, pad.shape) == (
        40.0,
        60.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert pad.corner_radius == pytest.approx(10.0)


STACK_MID = PadCopper(20.0, 30.0, PAD_SHAPE_ROUNDRECT, 5.0)
STACK_BOTTOM = PadCopper(40.0, 60.0, PAD_SHAPE_ROUNDRECT, 10.0)
STACK_MID_LAYER_3 = PadCopper(50.0, 70.0, PAD_SHAPE_ROUNDRECT, 5.0)


def test_decode_pads_multi_layer_top_mid_bottom_keeps_per_layer_copper():
    (pad,), skipped = decode_pads(_stack_pad(74, 1))
    assert skipped == 0
    # The main copper is the top slot's.
    assert (pad.layer, pad.width, pad.height, pad.shape) == (74, 2.4, 3.5, 1)
    assert pad.corner_radius is None
    assert pad.layer_copper == {PAD_INNER_LAYERS: STACK_MID, 32: STACK_BOTTOM}
    # Every inner layer (internal planes too) has the mid copper; the full
    # stack's inner slots do not count in a top-middle-bottom stack.
    assert [
        pad_copper_on(pad.layer_copper, layer) for layer in (1, 2, 4, 31, 32, 39)
    ] == [None, STACK_MID, STACK_MID, STACK_MID, STACK_BOTTOM, STACK_MID]


def test_decode_pads_multi_layer_full_stack_keeps_each_inner_slot():
    (pad,), skipped = decode_pads(_stack_pad(74, 2))
    assert skipped == 0
    assert (pad.width, pad.height, pad.shape) == (2.4, 3.5, 1)
    # Mid-Layer 3 (v6 4) has its own slot; Mid-Layer 1 is the main
    # subrecord's mid, and the fixture's empty slots inherit it.
    assert pad.layer_copper == {
        PAD_INNER_LAYERS: STACK_MID,
        4: STACK_MID_LAYER_3,
        32: STACK_BOTTOM,
    }
    assert [
        pad_copper_on(pad.layer_copper, layer) for layer in (1, 2, 3, 4, 5, 32)
    ] == [None, STACK_MID, STACK_MID, STACK_MID_LAYER_3, STACK_MID, STACK_BOTTOM]


def test_decode_pads_full_stack_inner_slot_like_the_top_is_kept():
    # An inner slot equal to the top copper still differs from the mid one.
    (pad,), _ = decode_pads(
        _pad_record(
            layer=74,
            sx=60_0000,
            sy=60_0000,
            shape=1,
            mode=2,
            mid_size=(40_0000, 40_0000),
            extra_sub=_size_and_shape(
                inner={k: (40_0000, 40_0000, 1) for k in range(29)}
                | {0: (60_0000, 60_0000, 1)},
                length=596,
            ),
        )
    )
    mid = PadCopper(40.0, 40.0, PAD_SHAPE_ROUND)
    assert pad.layer_copper == {
        PAD_INNER_LAYERS: mid,
        3: PadCopper(60.0, 60.0, PAD_SHAPE_ROUND),
    }
    assert pad_copper_on(pad.layer_copper, 3) == PadCopper(60.0, 60.0, 1)
    assert pad_copper_on(pad.layer_copper, 2) == mid


def test_decode_pads_multi_layer_rect_top_round_mid():
    (pad,), _ = decode_pads(
        _pad_record(
            layer=74, sx=60_0000, sy=60_0000, shape=2, mode=1,
            mid_size=(50_0000, 50_0000), mid_shape=1,
        )
    )
    assert (pad.width, pad.shape) == (60.0, PAD_SHAPE_RECT)
    assert pad.layer_copper == {
        PAD_INNER_LAYERS: PadCopper(50.0, 50.0, PAD_SHAPE_ROUND),
    }


@pytest.mark.parametrize(
    "record",
    [
        # A simple stack has one copper on every layer.
        _stack_pad(74, 0),
        # A per-layer stack whose slots all hold the top copper.
        _pad_record(layer=74, mode=1),
        # Single-layer pads keep only their own slot.
        _stack_pad(5, 1),
        _stack_pad(32, 2),
    ],
    ids=["simple", "uniform", "inner-pad", "bottom-pad"],
)
def test_decode_pads_without_per_layer_copper(record):
    (pad,), _ = decode_pads(record)
    assert pad.layer_copper is None
    assert pad_copper_on(pad.layer_copper, 3) is None


def _pad_record_with_main(main_length: int, pin_package: int | None) -> bytes:
    """A pad record whose main subrecord has main_length bytes; pin_package
    (internal units) is written at offset 158 when given."""
    main = bytearray(main_length)
    main[0] = 1
    struct.pack_into("<H", main, 3, 3)
    struct.pack_into("<H", main, 7, 5)
    struct.pack_into("<ii", main, 13, 150_0000, 250_0000)
    struct.pack_into("<iiiiiii", main, 21, 2_4000, 3_5000, 0, 0, 0, 0, 0)
    main[49] = 2
    if pin_package is not None:
        struct.pack_into("<i", main, 158, pin_package)
    name_blob = b"\x011"
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in [b"\x00", b"\x00" * 5, b"\x00", bytes(main), b""]:
        body += struct.pack("<I", len(subrecord)) + subrecord
    return b"\x02" + body


def test_decode_pads_pin_package_length():
    # 194- and 202-byte main subrecords: int32 at offset 158, 1e-4 mil.
    for main_length in (194, 202):
        primitives, skipped = decode_pads(_pad_record_with_main(main_length, 1234_5000))
        assert skipped == 0
        assert primitives[0].pin_package == pytest.approx(1234.5)
    # Unset is 0; the 185-byte layout carries the field too.
    assert decode_pads(_pad_record())[0][0].pin_package == 0.0


def test_decode_pads_pin_package_absent_or_invalid():
    # Too short to hold the field, or a negative (meaningless) value.
    short, _ = decode_pads(_pad_record_with_main(120, None))
    assert short[0].pin_package is None
    negative, _ = decode_pads(_pad_record_with_main(194, -5))
    assert negative[0].pin_package is None


BOARD_PROPS = {
    "ORIGINX": "7283.4648mil",
    "ORIGINY": "7775.5913mil",
    "SHEETX": "1000mil",
    "SHEETY": "1000mil",
    "SHEETWIDTH": "10000mil",
    "SHEETHEIGHT": "8000mil",
    "DISPLAYUNIT": "1",
    "LAYER1NAME": "Top Layer",
    "LAYER2NAME": "L2-GND",
    "LAYER32NAME": "Bottom Layer",
    "LAYER33NAME": "Top Overlay",
    "V9_STACK_LAYER0_NAME": "Top Layer",
    "V9_STACK_LAYER0_LAYERID": "16777217",
    "V9_STACK_LAYER0_COPTHICK": "2.7559mil",
    "V9_STACK_LAYER1_NAME": "Dielectric1",
    "V9_STACK_LAYER1_LAYERID": "17039361",
    "V9_STACK_LAYER1_DIELTYPE": "1",
    "V9_STACK_LAYER1_DIELCONST": "3.800",
    "V9_STACK_LAYER1_DIELHEIGHT": "5mil",
    "V9_STACK_LAYER1_DIELMATERIAL": "Isola FR370HR",
    "V9_STACK_LAYER2_NAME": "L2-GND",
    "V9_STACK_LAYER2_LAYERID": "16777218",
    "V9_STACK_LAYER2_COPTHICK": "2.7559mil",
}


def test_decode_board_stackup_and_origin():
    board = decode_board([BOARD_PROPS])
    assert board.origin_x == pytest.approx(7283.4648)
    assert board.display_unit == "mil"
    top = next(layer for layer in board.layers if layer.name == "Top Layer")
    assert (top.v6_id, top.kind, top.stack_order) == (1, "copper", 0)
    assert top.copper_thick_mil == pytest.approx(2.7559)
    dielectric = next(
        layer for layer in board.layers if layer.name == "Dielectric1"
    )
    assert (
        dielectric.kind,
        dielectric.diel_const,
        dielectric.material,
    ) == ("dielectric", 3.8, "Isola FR370HR")
    assert dielectric.diel_height_mil == pytest.approx(5.0)
    ground = next(layer for layer in board.layers if layer.name == "L2-GND")
    assert (ground.v6_id, ground.stack_order) == (2, 2)
    overlay = next(
        layer for layer in board.layers if layer.name == "Top Overlay"
    )
    assert (overlay.v6_id, overlay.kind, overlay.stack_order) == (
        33,
        "non-stack",
        None,
    )


def test_decode_board_legacy_fallback():
    board = decode_board(
        [{"LAYER1NAME": "Top Layer", "LAYER2NAME": "Bottom Layer"}]
    )
    assert board.stackup_source == "legacy"
    assert [layer.name for layer in board.layers][:2] == [
        "Top Layer",
        "Bottom Layer",
    ]


def test_decode_board_outline_vertices():
    board = decode_board([
        {
            **BOARD_PROPS,
            "VX0": "10mil",
            "VY0": "20mil",
            "VX1": "110mil",
            "VY1": "20mil",
            "VX2": "110mil",
            "VY2": "220mil",
        }
    ])
    assert board.outline_vertices == [
        (10.0, 20.0),
        (110.0, 20.0),
        (110.0, 220.0),
    ]


def test_decode_nets():
    assert decode_nets([{"NAME": "GND"}, {"NAME": "3V3"}]) == ["GND", "3V3"]


def test_decode_components():
    components = decode_components(
        [
            {
                "SOURCEDESIGNATOR": "U3",
                "SOURCEUNIQUEID": "\\TTMAPHZK\\HBSRZYDI\\NWGGFMAY",
                "SOURCEHIERARCHICALPATH": "top_sheet\\Voltage Sense\\Top",
                "X": "12952.756mil",
                "Y": "13425.1969mil",
                "ROTATION": " 1.80000000000000E+0002",
                "LAYER": "TOP",
            },
            {
                "LAYER": "BOTTOM",
            },
        ]
    )
    u3 = components[0]
    assert (u3.index, u3.refdes, u3.side, u3.rotation) == (
        0,
        "U3",
        "top",
        180.0,
    )
    assert u3.source_uid_tail == "NWGGFMAY"
    assert u3.channel_path == "top_sheet\\Voltage Sense\\Top"
    assert u3.x == pytest.approx(12952.756)
    assert (components[1].refdes, components[1].side) == (None, "bottom")


def test_decode_polygons():
    pours, edges = decode_polygons(
        [
            {
                "LAYER": "TOP",
                "NET": "5",
                "HATCHSTYLE": "Solid",
                "VX0": "0mil",
                "VY0": "0mil",
                "VX1": "100mil",
                "VY1": "0mil",
                "VX2": "100mil",
                "VY2": "50mil",
            }
        ],
        layer_name_to_v6={"top layer": 1, "top": 1},
    )
    assert pours[0].net == 5
    assert pours[0].layer == 1
    assert pours[0].vertices == [
        (0.0, 0.0),
        (100.0, 0.0),
        (100.0, 50.0),
    ]
    assert len(edges) == 3
    assert all(
        edge.kind == "track" and edge.pour_id == 0 and edge.width == 0.0
        for edge in edges
    )
    assert (edges[-1].x1, edges[-1].y1, edges[-1].x2, edges[-1].y2) == (
        100.0,
        50.0,
        0.0,
        0.0,
    )
    assert (pours[0].grid_mil, pours[0].track_width_mil) == (None, None)


def test_decode_polygons_reads_the_hatch_grid_and_track_width():
    (pour,), _ = decode_polygons(
        [{"LAYER": "TOP", "NET": "1", "HATCHSTYLE": "45Degree", "GRIDSIZE": "0.5mm",
          "TRACKWIDTH": "8mil", "VX0": "0mil", "VY0": "0mil", "VX1": "80mil",
          "VY1": "0mil", "VX2": "80mil", "VY2": "50mil"}],
        layer_name_to_v6={"top": 1},
    )
    assert pour.grid_mil == pytest.approx(0.5 / 0.0254)
    assert pour.track_width_mil == 8.0


@pytest.mark.parametrize("raw, name", [
    ("71,78,68", "GND"),  # Altium saves NAME as character codes
    ("49,95,84,111,112,45,71,78,68", "1_Top-GND"),
    ("80,111,117,114,32,8364", "Pour €"),
    ("49,50", "12"),
    # UTF-16 code units: a surrogate pair is one character, a lone one U+FFFD.
    ("71,55357,56832", "G\U0001F600"),
    ("71,55296", "G�"),
    ("Top Layer-GND", "Top Layer-GND"),  # any other form is kept as written
    ("71,,78", "71,,78"),
    ("12", "12"),  # a plain numeric name, not a control character
    ("99999999999", "99999999999"),  # not a UTF-16 code unit
    ("9" * 5000, "9" * 5000),
    ("", None),
    (None, None),
])
def test_decode_polygons_reads_the_pour_name(raw, name):
    record = {"LAYER": "TOP", "NET": "5", "VX0": "0mil", "VY0": "0mil",
              "VX1": "100mil", "VY1": "0mil", "VX2": "100mil", "VY2": "50mil"}
    if raw is not None:
        record["NAME"] = raw
    (pour,), _ = decode_polygons([record], {"top": 1})
    assert pour.name == name


def test_decode_polygons_shelved_pour_keeps_its_outline_but_no_rows():
    outline = {
        "LAYER": "TOP", "NET": "5", "HATCHSTYLE": "Solid",
        "VX0": "0mil", "VY0": "0mil", "VX1": "100mil", "VY1": "0mil",
        "VX2": "100mil", "VY2": "50mil",
    }
    pours, edges = decode_polygons(
        [{**outline, "SHELVED": "TRUE"}, {**outline, "SHELVED": "FALSE"}],
        layer_name_to_v6={"top": 1},
    )
    assert [pour.shelved for pour in pours] == [True, False]
    assert len(pours[0].vertices) == 3
    # The shelved pour has no copper: its outline gets no proximity rows.
    assert {edge.pour_id for edge in edges} == {1}


def test_build_layer_aliases_resolves_mid_layer_positional_names():
    # Polygons6 (and other text streams) reference inner copper layers by
    # Altium's fixed positional alias (MID1, MID2, ...), not by LAYERnNAME —
    # regression test for a board where an L2-GND pour's layer failed to
    # resolve because only "top"/"bottom" fallbacks existed.
    board = decode_board([BOARD_PROPS])
    aliases = _build_layer_aliases(board)
    assert aliases["mid1"] == 2
    assert aliases["l2-gnd"] == 2
    assert aliases["mid1"] == aliases["l2-gnd"]
    assert aliases["mid30"] == 31


def test_decode_polygons_resolves_inner_layer_via_mid_alias():
    board = decode_board([BOARD_PROPS])
    aliases = _build_layer_aliases(board)
    pours, edges = decode_polygons(
        [
            {
                "LAYER": "MID1",
                "NET": "0",
                "VX0": "0mil", "VY0": "0mil",
                "VX1": "10mil", "VY1": "0mil",
                "VX2": "10mil", "VY2": "10mil",
            }
        ],
        layer_name_to_v6=aliases,
    )
    assert pours[0].layer == 2
    assert len(edges) == 3  # proximity edges must be built, not silently dropped


def _polygon(*vertices, **props) -> dict[str, str]:
    """A Polygons6 record: (x, y) vertices, or (x, y, (cx, cy, r, sa, ea)) for a
    KINDn=1 arc edge from that vertex (mil and degrees, as Altium writes them)."""
    return _shape_props(*vertices, **{"LAYER": "TOP", "NET": "5", "HATCHSTYLE": "Solid", **props})


def _shape_props(*vertices, **props) -> dict[str, str]:
    """VXn/VYn vertices and their KINDn edges, as Polygons6 and Board6 store them."""
    record = dict(props)
    for index, (x, y, *arc) in enumerate(vertices):
        record[f"VX{index}"] = f"{x}mil"
        record[f"VY{index}"] = f"{y}mil"
        if arc:
            cx, cy, radius, start, end = arc[0]
            record.update({
                f"KIND{index}": "1", f"CX{index}": f"{cx}mil", f"CY{index}": f"{cy}mil",
                f"R{index}": f"{radius}mil", f"SA{index}": f"{start}", f"EA{index}": f"{end}",
            })
        else:
            record[f"KIND{index}"] = "0"
    return record


def _assert_follows_arc(points, cx, cy, radius):
    """Each point is on the arc, each chord at most ARC_MAX_SAGITTA_MIL inside it."""
    for x, y in points:
        assert math.dist((x, y), (cx, cy)) == pytest.approx(radius, abs=1e-6)
    for first, second in zip(points, points[1:]):
        middle = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
        assert radius - math.dist(middle, (cx, cy)) <= ARC_MAX_SAGITTA_MIL + 1e-9


# The right edge of a 100 mil square bulges out to x = 150: an arc counter-
# clockwise from 270 to 90 degrees around (100, 50), drawn from vertex 1.
D_SHAPE = (
    (0, 0), (100, 0, (100, 50, 50, 270, 90)), (100, 100), (0, 100), (0, 0),
)


def test_decode_polygons_follows_arc_edges():
    pours, edges = decode_polygons([_polygon(*D_SHAPE)], {"top": 1})
    (pour,) = pours
    vertices = pour.vertices
    assert vertices[:2] == [(0.0, 0.0), (100.0, 0.0)]
    assert vertices[-2:] == [(100.0, 100.0), (0.0, 100.0)]  # no closing copy
    arc = vertices[1:-1]
    assert len(arc) > 3
    _assert_follows_arc(arc, 100.0, 50.0, 50.0)
    assert max(x for x, _ in arc) == pytest.approx(150.0, abs=ARC_MAX_SAGITTA_MIL)
    assert [y for _, y in arc] == sorted(y for _, y in arc)  # bottom to top
    assert point_in_polygon(140.0, 50.0, vertices)  # in the bulge
    assert not point_in_polygon(151.0, 50.0, vertices)
    assert pour.shape[1].arc == (100.0, 50.0, 50.0, 270.0, 90.0)
    # The outline rows follow the same points, closed.
    assert len(edges) == len(vertices)
    closed = vertices + vertices[:1]
    assert [((e.x1, e.y1), (e.x2, e.y2)) for e in edges] == list(zip(closed, closed[1:]))
    assert all(
        e.kind == "track" and e.width == 0.0 and e.pour_id == 0 and e.layer == 1
        for e in edges
    )


def test_decode_polygons_draws_an_arc_from_the_end_at_its_vertex():
    # The same outline clockwise: the arc's end (90 degrees) is at its vertex,
    # so it runs top to bottom, out to x = 150, back to vertex 3.
    record = _polygon(
        (0, 0), (0, 100), (100, 100, (100, 50, 50, 270, 90)), (100, 0),
    )
    ((pour,), _) = decode_polygons([record], {"top": 1})
    vertices = pour.vertices
    assert vertices[:3] == [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)]
    assert vertices[-1] == (100.0, 0.0)
    arc = vertices[2:]
    _assert_follows_arc(arc, 100.0, 50.0, 50.0)
    assert [y for _, y in arc] == sorted((y for _, y in arc), reverse=True)
    assert point_in_polygon(140.0, 50.0, vertices)


def test_decode_polygons_round_pour_mostly_arc():
    # Three quarters of a disc, as Bluetooth Sentinel's round pours are drawn:
    # one 270 degree arc and two straight edges through the centre. Its three
    # vertices alone enclose the missing quarter instead.
    record = _polygon((100, 0, (0, 0, 100, 0, 270)), (0, -100), (0, 0))
    ((pour,), edges) = decode_polygons([record], {"top": 1})
    vertices = pour.vertices
    assert vertices[0] == (100.0, 0.0)
    assert vertices[-2:] == [(0.0, -100.0), (0.0, 0.0)]
    _assert_follows_arc(vertices[:-1], 0.0, 0.0, 100.0)
    assert point_in_polygon(-50.0, 50.0, vertices)
    assert point_in_polygon(50.0, 50.0, vertices)
    assert not point_in_polygon(30.0, -30.0, vertices)
    area = 0.5 * abs(sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1])
    ))
    disc = 0.75 * math.pi * 100.0**2
    assert 0.995 * disc < area < disc  # chords cut a little inside the arc
    assert len(edges) == len(vertices)


def test_decode_polygons_snaps_arc_ends_to_their_vertices():
    # Altium rounds the arc and the vertices on their own: an end a few
    # thousandths of a mil off its vertex is that vertex, not a second point.
    record = _polygon(
        (0, 0), (100.003, 0, (100, 50, 50, 270, 90)), (99.998, 100), (0, 100),
    )
    ((pour,), edges) = decode_polygons([record], {"top": 1})
    vertices = pour.vertices
    assert vertices[1] == (100.003, 0.0)
    assert (99.998, 100.0) in vertices
    assert min(
        math.dist(first, second) for first, second in zip(vertices, vertices[1:])
    ) > 1.0
    assert all(math.hypot(e.x2 - e.x1, e.y2 - e.y1) > 1.0 for e in edges)


def test_decode_polygons_arc_on_the_last_vertex_closes_on_the_first():
    record = _polygon((100, 100), (0, 100), (0, 0), (100, 0, (100, 50, 50, 270, 90)))
    ((pour,), edges) = decode_polygons([record], {"top": 1})
    vertices = pour.vertices
    assert vertices[:4] == [(100.0, 100.0), (0.0, 100.0), (0.0, 0.0), (100.0, 0.0)]
    _assert_follows_arc(vertices[3:] + vertices[:1], 100.0, 50.0, 50.0)
    assert vertices.count((100.0, 100.0)) == 1
    assert all(math.hypot(e.x2 - e.x1, e.y2 - e.y1) > 1.0 for e in edges)
    assert point_in_polygon(140.0, 50.0, vertices)


def test_decode_polygons_consecutive_arcs():
    # A round pour of two half circles, each drawn from its own vertex.
    record = _polygon((50, 0, (50, 50, 50, 270, 90)), (50, 100, (50, 50, 50, 90, 270)))
    ((pour,), edges) = decode_polygons([record], {"top": 1})
    vertices = pour.vertices
    _assert_follows_arc(vertices + vertices[:1], 50.0, 50.0, 50.0)
    assert len(set(vertices)) == len(vertices) == len(edges)
    assert point_in_polygon(50.0, 50.0, vertices)
    assert point_in_polygon(5.0, 50.0, vertices) and point_in_polygon(95.0, 50.0, vertices)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("R1", None),  # KIND1=1 without its radius
        ("EA1", "270"),  # no sweep: KiCad draws it straight too
        ("EA1", "270.1"),
        ("SA1", "nan"),
        ("EA1", "inf"),
        ("CX1", "1e999mil"),
    ],
)
def test_decode_polygons_draws_a_degenerate_arc_straight(key, value):
    record = _polygon(*D_SHAPE)
    if value is None:
        del record[key]
    else:
        record[key] = value
    ((pour,), edges) = decode_polygons([record], {"top": 1})
    assert pour.vertices == [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    assert len(edges) == 4


# Bluetooth Sentinel's outline in small: a strip 300 x 20 mil whose bottom edge
# runs round a circle of radius 50 below it, an arc counter-clockwise from
# 143.13 to 36.87 degrees (3-4-5 triangles) drawn from vertex 1, then closes
# on a copy of vertex 0. Its vertices alone span 20 mil in y; with the arc, 100.
STRIP_WITH_CIRCLE = (
    (0, 0), (110, 0, (150, -30, 50, 143.130102354156, 36.869897645844)), (190, 0),
    (300, 0), (300, 20), (0, 20), (0, 0),
)


def test_decode_board_outline_follows_arc_edges():
    board = decode_board([{**BOARD_PROPS, **_shape_props(*STRIP_WITH_CIRCLE)}])
    shape = board.outline_shape  # kept as stored, arc included
    assert len(shape) == 6
    assert shape[1].arc == pytest.approx((150.0, -30.0, 50.0, 143.130102354156, 36.869897645844))
    points = board.outline_vertices
    assert points[:2] == [(0.0, 0.0), (110.0, 0.0)]
    assert points[-4:] == [(190.0, 0.0), (300.0, 0.0), (300.0, 20.0), (0.0, 20.0)]
    _assert_follows_arc(points[1:-3], 150.0, -30.0, 50.0)
    xs, ys = [x for x, _ in points], [y for _, y in points]
    assert (min(xs), max(xs), max(ys)) == (0.0, 300.0, 20.0)
    assert min(ys) == pytest.approx(-80.0, abs=ARC_MAX_SAGITTA_MIL)
    assert point_in_polygon(150.0, -70.0, points)  # in the circle
    assert point_in_polygon(150.0, 10.0, points)  # in the strip
    assert not point_in_polygon(150.0, -81.0, points)
    assert not point_in_polygon(50.0, -10.0, points)


def test_decode_board_round_outline():
    # Two half circles, each drawn from its own vertex: the two vertices alone
    # are a line.
    board = decode_board([{**BOARD_PROPS, **_shape_props(
        (50, 0, (50, 50, 50, 270, 90)), (50, 100, (50, 50, 50, 90, 270)), (50, 0),
    )}])
    assert len(board.outline_shape) == 2
    points = board.outline_vertices
    _assert_follows_arc(points + points[:1], 50.0, 50.0, 50.0)
    xs, ys = [x for x, _ in points], [y for _, y in points]
    assert (min(ys), max(ys)) == (0.0, 100.0)
    assert min(xs) == pytest.approx(0.0, abs=ARC_MAX_SAGITTA_MIL)
    assert max(xs) == pytest.approx(100.0, abs=ARC_MAX_SAGITTA_MIL)
    assert point_in_polygon(50.0, 50.0, points)


def test_decode_board_draws_an_arc_of_huge_angles_straight():
    # Finite angles whose difference overflows: a straight edge, as other bad
    # arc values give, not a Board6 parse failure that loses the whole board.
    props = _shape_props(*STRIP_WITH_CIRCLE)
    props.update({"SA1": "1e308", "EA1": "-1e308"})
    board = decode_board([{**BOARD_PROPS, **props}])
    assert board.outline_shape[1].arc is None
    assert board.outline_vertices == [
        (0.0, 0.0), (110.0, 0.0), (190.0, 0.0), (300.0, 0.0), (300.0, 20.0), (0.0, 20.0),
    ]
    assert board.layers  # the rest of the board is read


def _stream_text(records: list[dict[str, str]]) -> bytes:
    return b"".join(_text_record(record) for record in records)


def test_parse_streams_assembles_pcbdata():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}, {"NAME": "CLK"}]),
        "Components6/Data": _stream_text(
            [
                {
                    "SOURCEDESIGNATOR": "R1",
                    "X": "0mil",
                    "Y": "0mil",
                    "LAYER": "TOP",
                }
            ]
        ),
        "Tracks6/Data": _bin_record(
            4,
            _track_payload(layer=1, net=1, component=0),
        ),
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": _pad_record(name="1", layer=1, net=0, component=0),
        "Polygons6/Data": _stream_text(
            [
                {
                    "LAYER": "TOP",
                    "NET": "0",
                    "VX0": "0mil",
                    "VY0": "0mil",
                    "VX1": "10mil",
                    "VY1": "0mil",
                    "VX2": "10mil",
                    "VY2": "10mil",
                }
            ]
        ),
    }
    pcb = parse_streams(streams)
    assert isinstance(pcb, PcbData)
    assert pcb.nets == ["GND", "CLK"]
    assert pcb.components[0].refdes == "R1"
    assert {primitive.kind for primitive in pcb.primitives} == {
        "track",
        "pad",
    }
    assert len(pcb.pours) == 1
    assert pcb.skipped_records == {}
    assert pcb.warnings == []


def test_parse_streams_missing_stream_warns_but_continues():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
    }
    pcb = parse_streams(streams)
    assert pcb.nets == ["GND"]
    assert any("Tracks6" in warning for warning in pcb.warnings)


def test_parse_streams_isolates_invalid_board_text_values():
    streams = {
        "Board6/Data": _stream_text([
            {
                **BOARD_PROPS,
                "V9_STACK_LAYER1_DIELCONST": "not-a-number",
            }
        ]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": _stream_text([]),
    }
    pcb = parse_streams(streams)
    assert pcb.nets == ["GND"]
    assert any(
        "Board6 parse failed" in warning
        for warning in pcb.warnings
    )


def test_parse_streams_drops_out_of_range_net_references():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": b"",
        "Tracks6/Data": _bin_record(4, _track_payload(net=9)),
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": b"",
        "Polygons6/Data": b"",
    }
    pcb = parse_streams(streams)
    assert pcb.primitives == []
    assert pcb.skipped_records["net_range"] == 1


def test_parse_streams_gives_pour_copper_its_pour_net():
    # Altium saves a hatched pour's tracks and arcs without a net; they are
    # the pour's copper. A pour index past the Polygons6 records keeps none.
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}, {"NAME": "SIG"}]),
        "Components6/Data": b"",
        "Tracks6/Data": b"".join(
            _bin_record(4, _track_payload(net=net, polygon=polygon))
            for net, polygon in ((NO_NET, 0), (1, 0xFFFF), (NO_NET, 5))
        ),
        "Vias6/Data": b"",
        "Arcs6/Data": _bin_record(1, _arc_payload(net=NO_NET, polygon=0)),
        "Pads6/Data": b"",
        "Polygons6/Data": _stream_text(
            [
                {
                    "LAYER": "TOP", "NET": "0", "HATCHSTYLE": "Horizontal",
                    "VX0": "0mil", "VY0": "0mil", "VX1": "10mil",
                    "VY1": "0mil", "VX2": "10mil", "VY2": "10mil",
                }
            ]
        ),
    }
    pcb = parse_streams(streams)
    tracks = [p for p in pcb.primitives if p.kind == "track" and p.pour_id is None]
    assert [(track.net, track.poured_by) for track in tracks] == [
        (0, 0), (1, None), (None, 5),
    ]
    arcs = [p for p in pcb.primitives if p.kind == "arc"]
    assert arcs and {(arc.net, arc.poured_by) for arc in arcs} == {(0, 0)}


def test_parse_streams_drops_a_shelved_pours_copper():
    # Altium keeps a shelved pour's copper in the file, hidden from DRC and
    # connectivity: not copper for these tools either.
    pour = {
        "LAYER": "TOP", "NET": "0", "HATCHSTYLE": "Horizontal", "SHELVED": "TRUE",
        "VX0": "0mil", "VY0": "0mil", "VX1": "10mil", "VY1": "0mil",
        "VX2": "10mil", "VY2": "10mil",
    }
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}, {"NAME": "SIG"}]),
        "Components6/Data": b"",
        "Tracks6/Data": _bin_record(4, _track_payload(net=NO_NET, polygon=0))
        + _bin_record(4, _track_payload(net=1)),
        "Vias6/Data": b"",
        "Arcs6/Data": _bin_record(1, _arc_payload(net=NO_NET, polygon=0)),
        "Pads6/Data": b"",
        "Polygons6/Data": _stream_text([pour]),
    }
    pcb = parse_streams(streams)
    assert pcb.pours[0].shelved is True
    assert [(p.kind, p.net) for p in pcb.primitives] == [("track", 1)]
    assert pcb.skipped_records == {"shelved_pour_copper": 2}  # a track, an arc


# ---------- Rules6 / Classes6 / SignalClasses / DifferentialPairs6 ----------

def _rule_record(props: dict[str, str], kind_id: int = 0) -> bytes:
    return struct.pack("<H", kind_id) + _text_record(props)


RULE_GENERIC = {
    "SELECTION": "FALSE", "LAYER": "TOP", "LOCKED": "FALSE",
    "POLYGONOUTLINE": "FALSE", "USERROUTED": "TRUE", "KEEPOUT": "FALSE",
    "UNIONINDEX": "0", "NETSCOPE": "AnyNet", "LAYERKIND": "SameLayer",
    "SCOPE2EXPRESSION": "All", "COMMENT": "", "UNIQUEID": "ABCDEFGH",
    "DEFINEDBYLOGICALDOCUMENT": "FALSE",
}


def _rule_props(**props) -> dict[str, str]:
    return {**RULE_GENERIC, **props}


def test_rules6_records_carry_a_kind_prefix():
    data = _rule_record(
        _rule_props(RULEKIND="Width", NAME="Width", SCOPE1EXPRESSION="All",
                    ENABLED="TRUE", PRIORITY="1", MINLIMIT="10mil"),
        kind_id=2,
    ) + _rule_record(
        _rule_props(RULEKIND="Clearance", NAME="Clr", ENABLED="FALSE",
                    PRIORITY="2", SCOPE1EXPRESSION="(ObjectKind = 'Text')"),
        kind_id=0,
    )
    # The plain reader takes kind+length as one huge length and gives up.
    assert read_text_records(data) == []
    records, trailing = read_kind_prefixed_text_records(data)
    assert trailing == 0
    assert [kind_id for kind_id, _ in records] == [2, 0]
    rules = decode_rules(records)
    width, clearance = rules
    assert (width.kind, width.priority, width.enabled) == ("Width", 1, True)
    assert width.properties == {"MINLIMIT": "10mil"}
    assert clearance.enabled is False
    # '=' inside a scope survives the KEY=VALUE split.
    assert clearance.scope1 == "(ObjectKind = 'Text')"


def test_decode_rules_keeps_room_side_and_defaults():
    rules = decode_rules([
        (22, {"RULEKIND": "RoomDefinition", "NAME": "RF", "LAYER": "BOTTOM",
              "PRIORITY": "x", "VX0": "0mil"}),
        (99, {"NAME": "Mystery"}),
    ])
    room, unknown = rules
    assert room.properties["LAYER"] == "BOTTOM"
    assert room.priority == 1_000_000
    assert room.scope1 == "All"
    assert unknown.kind == "Kind99"


def test_decode_classes_members_superclass_and_kinds():
    records = [
        {"NAME": "All Nets", "KIND": "0", "SUPERCLASS": "TRUE"},
        {"NAME": "Clocks", "KIND": "0", "SUPERCLASS": "FALSE",
         **{f"M{i}": f"N{i}" for i in range(12)}, "M12": "N3"},
        {"NAME": "Top Side Components", "KIND": "1", "AUTOGENERATEDCLASS": "TRUE",
         "M0": "U1"},
        {"NAME": "Sig", "KIND": "10", "M0": "CK_PP1"},
        {"NAME": "Odd", "KIND": "42"},
    ]
    all_nets, clocks, top_side, xsignals, odd = decode_classes(records)
    assert all_nets.superclass and all_nets.members == []
    # M10 sorts after M9 (numeric order), and duplicates collapse.
    assert clocks.members == [f"N{i}" for i in range(12)]
    assert top_side.kind == "component" and top_side.auto_generated
    assert xsignals.kind == "xsignal"
    assert odd.kind == "unknown_42"


def test_decode_diff_pairs():
    pairs = decode_diff_pairs([
        {"NAME": "USB_D", "POSITIVENETNAME": "USB_D_P",
         "NEGATIVENETNAME": "USB_D_N", "UNIQUEID": "RNNVMXIH"},
        {"POSITIVENETNAME": "X"},
    ])
    assert len(pairs) == 1
    assert (pairs[0].positive_net, pairs[0].negative_net) == ("USB_D_P", "USB_D_N")


def _minimal_streams() -> dict[str, bytes]:
    return {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": b"",
        "Tracks6/Data": b"",
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": b"",
        "Polygons6/Data": b"",
    }


def test_parse_streams_rules_classes_pairs_are_optional():
    pcb = parse_streams(_minimal_streams())
    assert pcb.rules == [] and pcb.classes == [] and pcb.diff_pairs == []
    assert pcb.warnings == []


def test_parse_streams_reads_rules_classes_and_pairs():
    streams = _minimal_streams()
    streams["Rules6/Data"] = _rule_record(
        _rule_props(RULEKIND="Width", NAME="W", PRIORITY="1",
                    SCOPE1EXPRESSION="All", ENABLED="TRUE"),
        kind_id=2,
    )
    streams["Rules6/Header"] = struct.pack("<I", 3)
    streams["Classes6/Data"] = _stream_text([{"NAME": "HS", "KIND": "6", "M0": "USB_D"}])
    streams["SignalClasses/Data"] = _stream_text([{"NAME": "X", "KIND": "10"}])
    streams["DifferentialPairs6/Data"] = _stream_text([
        {"NAME": "USB_D", "POSITIVENETNAME": "USB_D_P", "NEGATIVENETNAME": "USB_D_N"}
    ])
    pcb = parse_streams(streams)
    assert [rule.name for rule in pcb.rules] == ["W"]
    assert [c.kind for c in pcb.classes] == ["diff_pair", "xsignal"]
    assert pcb.diff_pairs[0].name == "USB_D"
    # Header says 3 records but only 1 decoded.
    assert pcb.skipped_records["Rules6"] == 2
    assert pcb.warnings == []


def test_decode_components_keeps_footprint():
    components = decode_components([{"SOURCEDESIGNATOR": "U1", "PATTERN": "QFN32"}])
    assert components[0].footprint == "QFN32"


def _text6_record(component: int, text: str, designator: bool = True) -> bytes:
    first = bytearray(60)
    struct.pack_into("<H", first, 7, component)
    first[41] = 1 if designator else 0
    second = bytes([len(text)]) + text.encode("latin-1")
    return (
        b"\x05"
        + struct.pack("<I", len(first)) + bytes(first)
        + struct.pack("<I", len(second)) + second
    )


def test_decode_designators_reads_only_component_designators():
    data = (
        _text6_record(0, "U3")
        + _text6_record(1, "Comment text", designator=False)
        + _text6_record(0xFFFF, "Logo")
        + _text6_record(1, "R7")
    )
    assert decode_designators(data) == {0: "U3", 1: "R7"}


def test_parse_streams_attaches_pcb_designators():
    streams = _minimal_streams()
    streams["Components6/Data"] = _stream_text([
        {"SOURCEDESIGNATOR": "U1"}, {"SOURCEDESIGNATOR": "U2"},
    ])
    # Board-level annotation swapped the board designators.
    streams["Texts6/Data"] = _text6_record(0, "U2") + _text6_record(1, "U1")
    pcb = parse_streams(streams)
    assert [(c.refdes, c.designator) for c in pcb.components] == [
        ("U1", "U2"), ("U2", "U1"),
    ]
    assert pcb.warnings == []


def test_decode_board_reads_substacks():
    board = decode_board([{
        **BOARD_PROPS,
        "V9_SUBSTACK0_ID": "{A}", "V9_SUBSTACK0_NAME": "Rigid",
        "V9_SUBSTACK0_ISFLEX": "FALSE",
        "V9_SUBSTACK1_ID": "{B}", "V9_SUBSTACK1_NAME": "Flex",
        "V9_SUBSTACK1_ISFLEX": "TRUE",
    }])
    assert board.substacks == [
        {"id": "{A}", "name": "Rigid", "is_flex": False},
        {"id": "{B}", "name": "Flex", "is_flex": True},
    ]


def test_decode_xsignals_from_board_record():
    xsignals = decode_xsignals({
        "PINPAIRCOUNT": "2",
        "PINPAIRNAME0": "DQ0_PP1", "PINPAIRPINS0": "U1-W1, R4-1,R4-2,J1-3",
        "PINPAIRNAME1": "EMPTY", "PINPAIRPINS1": "",
    })
    assert [(x.name, x.pins) for x in xsignals] == [
        ("DQ0_PP1", ["U1-W1", "R4-1", "R4-2", "J1-3"])
    ]
    assert decode_xsignals({}) == []


def test_decode_drc_rule_kinds():
    assert decode_drc_rule_kinds([{"RULESETTOCHECK": "0,2,11,51"}]) == {0, 2, 11, 51}
    assert decode_drc_rule_kinds([{}]) is None


def test_decode_drc_options():
    options = decode_drc_options([{
        "MAXVIOLATIONCOUNT": "500", "RULESETTOCHECK": "0,15,16", "ONLINERULESETTOCHECK": "0,15",
        "DOMAKEDRCFILE": "TRUE", "INCLUDEPCBHEALTH": "FALSE", "DOSUBNETDETAILS": "TRUE",
    }])
    assert options == DrcOptions(
        max_violations=500, batch_kinds={0, 15, 16}, online_kinds={0, 15}, make_report=True,
        include_health=False, sub_net_details=True,
    )
    assert decode_drc_options([]) is None
    assert decode_drc_options([{}]) == DrcOptions()  # older files: settings unknown


def test_arc_chords_carry_true_arc_length():
    payload = bytearray(60)
    payload[0] = 1
    struct.pack_into("<H", payload, 3, 0xFFFF)
    struct.pack_into("<H", payload, 7, 0xFFFF)
    struct.pack_into("<ii", payload, 13, 0, 0)
    struct.pack_into("<i", payload, 21, 200000)
    struct.pack_into("<dd", payload, 25, 0.0, 90.0)
    struct.pack_into("<i", payload, 41, 50000)
    data = b"\x01" + struct.pack("<I", len(payload)) + bytes(payload)
    arcs, _ = decode_arcs(data)
    total = sum(arc.length for arc in arcs)
    assert total == pytest.approx(math.pi * 20 / 2, rel=1e-6)
    chords = sum(math.hypot(a.x2 - a.x1, a.y2 - a.y1) for a in arcs)
    assert chords < total


# ---------- component kind and comment (sync check) ----------

def test_decode_components_reads_kind():
    components = decode_components([
        {"SOURCEDESIGNATOR": "U1"},
        {"COMPONENTKIND": "1"},
        {"COMPONENTKIND": "0", "COMPONENTKINDVERSION2": "5"},  # Standard (No BOM)
        {"COMPONENTKIND": "4"},
        # Jumper: ComponentKindVersion3=6, written with or without the zeros before it.
        {"COMPONENTKIND": "0", "COMPONENTKINDVERSION2": "0", "COMPONENTKINDVERSION3": "6"},
        {"COMPONENTKINDVERSION3": "6"},
    ])
    assert [component.kind for component in components] == [0, 1, 5, 4, 6, 6]


def test_decode_components_keeps_the_full_unique_id_path():
    (component,) = decode_components([{"SOURCEUNIQUEID": "\\SHEETSYM\\PARTUID"}])
    assert (component.source_unique_id, component.source_uid_tail) == (
        "\\SHEETSYM\\PARTUID", "PARTUID",
    )


def _comment_record(component: int, text: bytes, wide_index: int | None = None) -> bytes:
    first = bytearray(120)
    struct.pack_into("<H", first, 7, component)
    first[40] = 1  # IsComment
    if wide_index is not None:
        struct.pack_into("<I", first, 115, wide_index)
    second = bytes([len(text)]) + text
    return (
        b"\x05"
        + struct.pack("<I", len(first)) + bytes(first)
        + struct.pack("<I", len(second)) + second
    )


def _wide(index: int, text: str) -> bytes:
    body = (text + "\0").encode("utf-16-le")
    return struct.pack("<II", index, len(body)) + body


def test_decode_comments_prefers_the_wide_text():
    wide = decode_wide_strings(_wide(7, "10µF 6.3V") + _wide(8, "100Ω") + _wide(9, "OTHER"))
    data = (
        _comment_record(0, "10µF 6.3V".encode("utf-8"), 7)  # recent files: UTF-8
        + _comment_record(1, b"100?", 8)                     # older: code page, Ω lost
        + _comment_record(2, b"BAV99", 9)                    # an index naming other text
        + _comment_record(3, b"LM358")                       # no wide string
        + _text6_record(4, "R7")                             # a designator, not a comment
    )
    assert decode_comments(data, wide) == {
        0: "10µF 6.3V", 1: "100Ω", 2: "BAV99", 3: "LM358",
    }


def test_parse_streams_attaches_comments():
    streams = _minimal_streams()
    streams["Components6/Data"] = _stream_text([{"SOURCEDESIGNATOR": "C1"}])
    streams["Texts6/Data"] = _comment_record(0, b"4.7?F", 3) + _text6_record(0, "C1")
    streams["WideStrings6/Data"] = _wide(3, "4.7µF")
    pcb = parse_streams(streams)
    assert (pcb.components[0].designator, pcb.components[0].comment) == ("C1", "4.7µF")


def test_decode_wide_strings_steps_over_a_bodyless_record():
    data = (
        _wide(1, "A")
        + struct.pack("<II", 2, 2)  # declared 2 bytes, but index 3's header follows
        + _wide(3, "C")
    )
    assert decode_wide_strings(data) == {1: "A", 2: "", 3: "C"}


def test_decode_wide_strings_stops_at_a_malformed_record():
    data = _wide(1, "A") + struct.pack("<II", 9, 0xFFFFFF) + b"junk"
    assert decode_wide_strings(data) == {1: "A"}


# ---------- keepouts, plated holes, slots, bare holes, panels ----------

def _keepout(payload: bytes) -> bytes:
    flagged = bytearray(payload)
    flagged[2] = 2
    return bytes(flagged)


def test_signal_layer_keepout_tracks_and_arcs_are_not_copper():
    data = (_bin_record(4, _track_payload(layer=1, net=NO_NET))
            + _bin_record(4, _keepout(_track_payload(layer=1, net=NO_NET)))
            + _bin_record(4, _keepout(_track_payload(layer=56, net=NO_NET))))
    tracks, skipped = decode_tracks(data)
    assert [track.layer for track in tracks] == [1, 56] and skipped == 0
    arcs, skipped = decode_arcs(_bin_record(1, _keepout(_arc_payload(layer=1))))
    assert arcs == [] and skipped == 0


def _size_shape(hole_shape=0, slot_length=0, slot_rotation=0.0, offset=(0, 0)) -> bytes:
    size_shape = bytearray(651)
    size_shape[262] = hole_shape
    struct.pack_into("<i", size_shape, 263, slot_length)
    struct.pack_into("<d", size_shape, 267, slot_rotation)
    struct.pack_into("<i", size_shape, 275, offset[0])
    struct.pack_into("<i", size_shape, 403, offset[1])
    return bytes(size_shape)


def test_pad_holes_plated_slots_and_offsets():
    data = (
        _pad_record(name="1", layer=74, hole=1_0000, plated=True, extra_sub=_size_shape())
        + _pad_record(name="2", layer=74, hole=1_0000, plated=False,
                      extra_sub=_size_shape(offset=(0, 5000)))
        + _pad_record(name="3", layer=74, hole=1_0000, plated=True,
                      extra_sub=_size_shape(2, 4_0000, 90.0))
        + _pad_record(name="4", layer=1)
    )
    pads, skipped = decode_pads(data)
    assert skipped == 0
    assert [pad.plated for pad in pads] == [True, False, True, None]
    assert [pad.hole_offset for pad in pads] == [False, True, False, False]
    assert (pads[2].hole_shape, pads[2].slot_length, pads[2].slot_rotation) == (2, 4.0, 90.0)
    assert pads[0].slot_rotation is None  # a round hole has no angle


def test_bare_holes_are_read_beside_the_pads():
    bare = _pad_record(name="MH", layer=74, sx=0, sy=0, hole=12_0000, plated=False,
                       net=NO_NET, component=0xFFFF)
    data = _pad_record(name="1", layer=74, hole=1_0000, plated=True) + bare
    pads, skipped = decode_pads(data)
    assert len(pads) == 1 and skipped == 1  # still counted as skipped
    (hole,) = decode_bare_holes(data)
    assert (hole.pad_name, hole.hole, hole.plated, hole.net, hole.component) == (
        "MH", 12.0, False, None, None)
    assert (hole.x, hole.y) == (150.0, 250.0)
    streams = {**_minimal_streams(), "Pads6/Data": data,
               "EmbeddedBoards6/Data": _stream_text([{"ROWCOUNT": "2"}])}
    pcb = parse_streams(streams)
    assert [h.pad_name for h in pcb.bare_holes] == ["MH"]
    assert pcb.skipped_records["Pads6"] == 1 and pcb.embedded_boards == 1
    assert parse_streams(_minimal_streams()).embedded_boards == 0


def test_bare_hole_selection_and_fields():
    no_hole = _pad_record(name="X", layer=74, sx=0, sy=0, hole=0)
    one_side = _pad_record(name="Y", layer=74, sx=0, sy=3_0000, hole=1_0000, plated=True)
    slot = _pad_record(name="S", layer=74, sx=0, sy=0, hole=1_0000, rotation=30.0,
                       plated=False, extra_sub=_size_shape(2, 4_0000, 45.0))
    data = no_hole + one_side + slot
    pads, skipped = decode_pads(data)
    assert pads == [] and skipped == 3
    holes = decode_bare_holes(data)
    assert [hole.pad_name for hole in holes] == ["Y", "S"]  # a pad with no hole is no hole
    bare_slot = holes[1]
    assert (bare_slot.hole_shape, bare_slot.slot_length, bare_slot.slot_rotation) == (2, 4.0, 45.0)
    assert (bare_slot.rotation, bare_slot.layer, bare_slot.plated) == (30.0, 74, False)


def test_pad_hole_fields_at_record_boundaries():
    short = bytearray(_pad_record_with_main(60, None))
    long = bytearray(_pad_record_with_main(61, None))
    for record in (short, long):
        # hole of 1 mil at offset 45 of the main subrecord (after the 7-byte header,
        # the 3 leading subrecords and the main's length).
        main_at = 1 + 4 + 2 + (4 + 1) + (4 + 5) + (4 + 1) + 4
        struct.pack_into("<i", record, main_at + 45, 1_0000)
    long[main_at + 60] = 1
    assert decode_pads(bytes(short))[0][0].plated is None  # 60 bytes: no plated byte
    assert decode_pads(bytes(long))[0][0].plated is True
    # A record cut short at the end keeps the pads before it.
    good = _pad_record(name="1")
    pads, _ = decode_pads(good + _pad_record(name="2")[:-10])
    assert [pad.pad_name for pad in pads] == ["1"]
    # The X offset alone marks the hole offset; a square hole carries its angle.
    pads, _ = decode_pads(
        _pad_record(name="3", layer=74, hole=1_0000, extra_sub=_size_shape(offset=(5000, 0)))
        + _pad_record(name="4", layer=74, hole=1_0000, extra_sub=_size_shape(1, 2_0000, 30.0)))
    assert pads[0].hole_offset is True
    assert (pads[1].hole_shape, pads[1].slot_rotation) == (1, 30.0)


EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def test_examples_bare_holes_and_panels():
    minipc = EXAMPLES / "Mini PC" / "Mini PC - Motherboard" / "MiniPC.PcbDoc"
    panel = EXAMPLES / "Mini PC" / "Mini PC - WiFi" / "WiFi_panel.PcbDoc"
    if not (minipc.is_file() and panel.is_file()):
        pytest.skip("Altium example boards not installed")
    from collections import Counter

    from server.parsers.pcb_doc import parse_pcb_doc

    pcb = parse_pcb_doc(str(minipc))
    # Altium's drill table (MiniPC_Drill.PCBDwf): 6 plated, 10 non-plated bare holes.
    rows = Counter((hole.plated, round(hole.hole * 0.0254, 2)) for hole in pcb.bare_holes)
    assert rows == {(True, 2.6): 2, (True, 2.9): 4, (False, 1.55): 4, (False, 3.25): 4,
                    (False, 2.35): 2}
    assert pcb.embedded_boards == 0
    assert parse_pcb_doc(str(panel)).embedded_boards == 1


def test_examples_pour_names():
    bluetooth = EXAMPLES / "Bluetooth Sentinel" / "Bluetooth_Sentinel.PcbDoc"
    spirit = EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc"
    if not (bluetooth.is_file() and spirit.is_file()):
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_doc import parse_pcb_doc

    # Pours may share a name: all ten Bluetooth Sentinel pours are GND.
    assert {pour.name for pour in parse_pcb_doc(str(bluetooth)).pours} == {"GND"}
    assert [pour.name for pour in parse_pcb_doc(str(spirit)).pours] == [
        "Top Layer-GND", "Bottom Layer-GND"]


# ---------- ComponentBodies6 ----------


def _body_record(vertices, *, component=0, layer=69, record_type=12, holes=(), close=False,
                 **props) -> bytes:
    """A ComponentBodies6 record: the Regions6 head (layer u8 at 0, net u16 at 3, polygon
    u16 at 5, component u16 at 7, hole count u16 at 14, text length u32 at 18), the text,
    then u32 count + (double x, double y) per vertex in 1e-4 mil, and each hole likewise."""
    text = ("|" + "|".join(f"{key}={value}" for key, value in props.items())).encode() + b"\x00"
    head = bytearray(22)
    head[0] = layer
    struct.pack_into("<HHH", head, 3, NO_NET, 0xFFFF, component)
    struct.pack_into("<H", head, 14, len(holes))
    struct.pack_into("<I", head, 18, len(text))

    def ring(points):
        points = [*points, points[0]] if close else list(points)
        return struct.pack("<I", len(points)) + b"".join(
            struct.pack("<dd", x * 10_000, y * 10_000) for x, y in points)

    payload = bytes(head) + text + ring(vertices) + b"".join(ring(hole) for hole in holes)
    return _bin_record(record_type, payload)


BODY_SQUARE = [(100.0, 200.0), (140.0, 200.0), (140.0, 220.0), (100.0, 220.0)]


def test_decode_component_bodies():
    from server.parsers.pcb_doc import decode_component_bodies

    data = (
        _body_record(BODY_SQUARE, component=3, OVERALLHEIGHT="31.4961mil",
                     STANDOFFHEIGHT="0mil", BODYPROJECTION="1", **{"MODEL.MODELTYPE": "1",
                     "MODEL.NAME": "secret.step", "V7_LAYER": "MECHANICAL13"})
        + _body_record(BODY_SQUARE, component=0xFFFF, layer=57, close=True,
                       holes=[[(110.0, 205.0), (120.0, 205.0), (120.0, 210.0)]],
                       OVERALLHEIGHT="0.5mm")
        + _body_record([(0.0, 0.0), (1.0, 0.0)], BODYPROJECTION="0")  # no area: kept
    )
    bodies, skipped = decode_component_bodies(data)
    assert skipped == 0
    first, free, line = bodies
    assert (first.component, first.layer, first.projection, first.model_type) == (
        3, 69, "bottom", 1)
    assert first.outline == BODY_SQUARE
    assert first.overall_height_mil == pytest.approx(31.4961)
    assert first.standoff_height_mil == 0.0
    # The 3D model's name and the V7 layer are kept; MODELID and MODEL.EMBED are unset.
    assert (first.model_name, first.v7_layer, first.model_id, first.model_embedded) == (
        "secret.step", "MECHANICAL13", None, None)
    # A free body; the closing copy of the first vertex is dropped, holes are read past.
    assert (free.component, free.layer, free.projection, free.model_type) == (
        None, 57, None, None)
    assert free.outline == BODY_SQUARE
    assert free.overall_height_mil == pytest.approx(0.5 / 0.0254)
    assert free.standoff_height_mil is None
    assert (line.outline, line.projection) == ([(0.0, 0.0), (1.0, 0.0)], "top")


def test_decode_component_bodies_skips_unreadable_records():
    from server.parsers.pcb_doc import decode_component_bodies

    good = _body_record(BODY_SQUARE, OVERALLHEIGHT="10mil")
    region = _body_record(BODY_SQUARE, record_type=11)  # a region, not a body
    short = _bin_record(12, b"\x00" * 10)  # shorter than the head
    cut = bytearray(_body_record(BODY_SQUARE))
    struct.pack_into("<I", cut, 5 + 22 + len(b"|\x00"), 99)  # 99 vertices: past the record
    nan = bytearray(_body_record(BODY_SQUARE))
    struct.pack_into("<d", nan, 5 + 22 + len(b"|\x00") + 4, math.nan)
    holes = bytearray(_body_record(BODY_SQUARE, holes=[[(110.0, 205.0), (120.0, 205.0),
                                                        (120.0, 210.0)]]))
    struct.pack_into("<H", holes, 5 + 14, 2)  # two holes, one in the record
    bodies, skipped = decode_component_bodies(
        region + good + short + bytes(cut) + bytes(nan) + bytes(holes) + good + good[:-7])
    assert [body.overall_height_mil for body in bodies] == [10.0, 10.0]
    # region, short, cut, nan, the hole list past the record and the one past the stream
    assert skipped == 6


def test_parse_streams_reads_component_bodies():
    streams = {**_minimal_streams(),
               "ComponentBodies6/Data": _body_record(BODY_SQUARE, OVERALLHEIGHT="10mil")
               + _bin_record(12, b"\x00" * 10)}
    pcb = parse_streams(streams)
    assert [body.outline for body in pcb.bodies] == [BODY_SQUARE]
    assert pcb.skipped_records["ComponentBodies6"] == 1
    # The stream is optional: no warning when it is missing.
    bare = parse_streams(_minimal_streams())
    assert bare.bodies == [] and "ComponentBodies6" not in bare.skipped_records
    assert not any("ComponentBodies6" in warning for warning in bare.warnings)


def test_examples_component_bodies():
    """Every ComponentBodies6 record of the examples decodes (1897 records, 0 skipped);
    each body's projection is its part's side."""
    if not EXAMPLES.is_dir():
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_doc import parse_pcb_doc

    counts = {}
    for path in sorted(EXAMPLES.rglob("*.PcbDoc")):
        pcb = parse_pcb_doc(str(path))
        assert "ComponentBodies6" not in pcb.skipped_records
        assert all(body.projection == pcb.components[body.component].side
                   for body in pcb.bodies if body.component is not None)
        counts[path.name] = len(pcb.bodies)
    if "MiniPC.PcbDoc" not in counts:
        pytest.skip("MiniPC example not installed")
    assert counts["MiniPC.PcbDoc"] == 1055
    assert counts["PC4-SODIMM_V200_RC_A1_20141015.PcbDoc"] == 198


def test_decode_pads_and_vias_read_the_fabrication_testpoint_flags():
    # The header's flags1 0x80 is Fabrication Testpoint top and flags2 0x01 bottom, on a
    # pad's main subrecord as on a via (KiCad's APAD6 / AVIA6); the lock (0x04), tenting
    # (0x20, 0x40) and keepout (flags2 0x02) bits around them are no testpoint.
    flags = [(0x0C, 0x00), (0x8C, 0x00), (0x0C, 0x01), (0x8C, 0x01), (0x6C, 0x02)]
    data = b"".join(
        _pad_record(name=str(number), flags1=first, flags2=second)
        for number, (first, second) in enumerate(flags)
    )
    pads, skipped = decode_pads(data)
    assert skipped == 0
    assert [(pad.test_fab_top, pad.test_fab_bottom) for pad in pads] == [
        (False, False), (True, False), (False, True), (True, True), (False, False),
    ]
    data = b"".join(
        _bin_record(3, _via_payload(flags1=first, flags2=second))
        for first, second in ((0x6C, 0x00), (0x68, 0x01), (0xEC, 0x00))
    )
    vias, _ = decode_vias(data)
    expected = [(False, False), (False, True), (True, False)]
    assert [(via.test_fab_top, via.test_fab_bottom) for via in vias] == expected
    assert [(via.via.test_fab_top, via.via.test_fab_bottom) for via in vias] == expected
    assert vias[2].via.tent_top and vias[2].via.tent_bottom
    tracks, _ = decode_tracks(_bin_record(4, _track_payload()))
    assert not tracks[0].test_fab_top and not tracks[0].test_fab_bottom
