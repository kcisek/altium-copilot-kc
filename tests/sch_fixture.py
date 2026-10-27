"""Synthetic schematic projects for the netlist compiler's tests: .SchDoc records written
the way Altium writes them (a FileHeader stream, and an Additional one for harnesses), a
.PrjPcb on disk, and a read_sheet that serves the streams to compile_netlist.

Coordinates are in Altium's record units of 10 mil. A pin is given by its electrical end
(PinLength 0), so wiring a pin means running a wire to that point."""
import struct
from pathlib import Path

from parsers.sch_doc import connectivity_from_stream


def record(body: str) -> bytes:
    data = body.encode("latin-1") + b"\0"
    return struct.pack("<I", len(data)) + data


def stream(records: list[str], sheet: str = "") -> bytes:
    """A stream: the header, the sheet record (position 1, with sheet's extra fields such
    as |SNAPGRIDSIZE=10), then the records, the first at position 2 (so its children
    have OwnerIndex=1)."""
    out = record("|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0")
    out += record(f"|RECORD=31|FONTIDCOUNT=1|SIZE1=10{sheet}")
    for body in records:
        out += record(body)
    return out


def _points(points) -> str:
    return "".join(f"|X{i}={x}|Y{i}={y}" for i, (x, y) in enumerate(points, 1))


class Sheet:
    """One sheet's records, built in order; children own to the record that adds them."""

    def __init__(self, header: str = ""):
        self.header = header  # extra fields of the sheet record (RECORD=31)
        self.records: list[str] = []
        self.additional: list[str] = []
        self._uid = 0

    def _add(self, body: str) -> int:
        self.records.append(body)
        return len(self.records) + 1  # its record position

    def _unique_id(self, prefix: str) -> str:
        self._uid += 1
        return f"{prefix}{self._uid:07d}"[:8].upper()

    def part(self, designator: str, pins, uid: str | None = None, part_id: int = 1,
             part_count: int = 2, at=(0, 0), comment: str | None = None,
             footprint: str | None = None, params: dict | None = None,
             kind: int | None = None) -> str:
        """A placed part with pins (designator, x, y) or (designator, x, y, extra fields);
        returns its UniqueId."""
        uid = uid or self._unique_id("P")
        kind_field = f"|COMPONENTKIND={kind}" if kind is not None else ""
        position = self._add(
            f"|RECORD=1|LIBREFERENCE=LIB_{designator}|CURRENTPARTID={part_id}"
            f"|PARTCOUNT={part_count}|UNIQUEID={uid}|LOCATION.X={at[0]}|LOCATION.Y={at[1]}"
            f"{kind_field}")
        owner = position - 1
        for pin in pins:
            name, x, y = pin[:3]
            extra = pin[3] if len(pin) > 3 else ""  # e.g. |PINCONGLOMERATE=4 (hidden)
            flags = "" if "PINCONGLOMERATE" in extra.upper() else "|PINCONGLOMERATE=0"
            self._add(f"|RECORD=2|OWNERINDEX={owner}|OWNERPARTID={part_id}|DESIGNATOR={name}"
                      f"|NAME=P{name}|PINLENGTH=0{flags}|LOCATION.X={x}|LOCATION.Y={y}{extra}")
        self._add(f"|RECORD=34|OWNERINDEX={owner}|NAME=Designator|TEXT={designator}")
        for name, text in {**({"Comment": comment} if comment else {}), **(params or {})}.items():
            self._add(f"|RECORD=41|OWNERINDEX={owner}|NAME={name}|TEXT={text}")
        if footprint:
            models = self._add(f"|RECORD=44|OWNERINDEX={owner}")
            self._add(f"|RECORD=45|OWNERINDEX={models - 1}|MODELNAME={footprint}"
                      "|MODELTYPE=PCBLIB|ISCURRENT=T")
        return uid

    def wire(self, *points) -> None:
        self._add(f"|RECORD=27|LOCATIONCOUNT={len(points)}{_points(points)}")

    def bus(self, *points) -> None:
        self._add(f"|RECORD=26|LOCATIONCOUNT={len(points)}{_points(points)}")

    def junction(self, x, y, extra: str = "") -> None:
        self._add(f"|RECORD=29|LOCATION.X={x}|LOCATION.Y={y}{extra}")

    def label(self, text: str, x, y) -> None:
        self._add(f"|RECORD=25|LOCATION.X={x}|LOCATION.Y={y}|TEXT={text}")

    def power(self, text: str, x, y) -> None:
        self._add(f"|RECORD=17|LOCATION.X={x}|LOCATION.Y={y}|TEXT={text}|STYLE=1")

    def off_sheet(self, text: str, x, y) -> None:
        self._add(f"|RECORD=17|LOCATION.X={x}|LOCATION.Y={y}|TEXT={text}"
                  "|ISCROSSSHEETCONNECTOR=T")

    def no_erc(self, x, y) -> None:
        self._add(f"|RECORD=22|LOCATION.X={x}|LOCATION.Y={y}|ISACTIVE=T|SUPPRESSALL=T")

    def parameter_set(self, x, y, parameters, name: str = "Parameter Set",
                      uid: str | None = None) -> None:
        """A Parameter Set directive at (x, y) with (name, value) or (name, value, unique
        ID) parameters."""
        extra = f"|UNIQUEID={uid}" if uid else ""
        position = self._add(f"|RECORD=43|LOCATION.X={x}|LOCATION.Y={y}|NAME={name}{extra}")
        for key, value, *ids in parameters:
            extra = f"|UNIQUEID={ids[0]}" if ids else ""
            self._add(f"|RECORD=41|OWNERINDEX={position - 1}|NAME={key}|TEXT={value}{extra}")

    def blanket(self, *points) -> None:
        """A blanket (RECORD=225) with these outline vertices."""
        self._add(f"|RECORD=225|LOCATIONCOUNT={len(points)}{_points(points)}")

    def mask(self, x0, y0, x1, y1) -> None:
        self._add(f"|RECORD=211|LOCATION.X={x0}|LOCATION.Y={y0}|CORNER.X={x1}|CORNER.Y={y1}")

    def port(self, name: str, x, y, width: int = 10, harness: str = "",
             io: int | None = None) -> tuple[int, int]:
        """A horizontal port from (x, y) to (x + width, y), of IOTYPE io when given (1
        Output, 2 Input, 3 Bidirectional); returns its right end."""
        extra = f"|HARNESSTYPE={harness}" if harness else ""
        extra += f"|IOTYPE={io}" if io is not None else ""
        self._add(f"|RECORD=18|STYLE=3|WIDTH={width}|LOCATION.X={x}|LOCATION.Y={y}"
                  f"|NAME={name}{extra}")
        return x + width, y

    def symbol(self, designator: str, file_name: str, entries=(), at=(100, 500),
               size=(100, 60), uid: str | None = None) -> list[tuple[int, int]]:
        """A sheet symbol, top-left at `at`, with entries (name, distance from the top in
        100 mil) on its right edge, or (name, distance, extra fields); returns each
        entry's point."""
        uid = uid or self._unique_id("S")
        position = self._add(f"|RECORD=15|LOCATION.X={at[0]}|LOCATION.Y={at[1]}"
                             f"|XSIZE={size[0]}|YSIZE={size[1]}|UNIQUEID={uid}"
                             "|SYMBOLTYPE=Normal")
        owner = position - 1
        points = []
        for entry in entries:
            name, distance = entry[:2]
            extra = entry[2] if len(entry) > 2 else ""
            self._add(f"|RECORD=16|OWNERINDEX={owner}|SIDE=1|DISTANCEFROMTOP={distance}"
                      f"|NAME={name}{extra}")
            points.append((at[0] + size[0], at[1] - distance * 10))
        self._add(f"|RECORD=32|OWNERINDEX={owner}|TEXT={designator}")
        self._add(f"|RECORD=33|OWNERINDEX={owner}|TEXT={file_name}")
        return points

    def harness_line(self, *points) -> None:
        self.additional.append(f"|RECORD=218|LOCATIONCOUNT={len(points)}{_points(points)}")

    def harness_connector(self, at, size, entries, side: int = 1, primary: int = 10,
                          harness: str = "H") -> tuple[tuple[int, int], list[tuple[int, int]]]:
        """A harness connector, top-left at `at`, taking its harness on `side` (1 right)
        `primary` units down, with entries (name, distance from the top in 100 mil) on the
        opposite side; returns the harness point and each entry's point."""
        x, y = at
        self.additional.append(
            f"|RECORD=215|LOCATION.X={x}|LOCATION.Y={y}|XSIZE={size[0]}|YSIZE={size[1]}"
            f"|HARNESSCONNECTORSIDE={side}|PRIMARYCONNECTIONPOSITION={primary}")
        points = []
        for name, distance in entries:
            self.additional.append(f"|RECORD=216|OWNERINDEXADDITIONALLIST=T|NAME={name}"
                                   f"|DISTANCEFROMTOP={distance}")
            points.append((x, y - distance * 10))  # side 1: entries on the left edge
        self.additional.append(f"|RECORD=217|OWNERINDEXADDITIONALLIST=T|TEXT={harness}")
        return (x + size[0], y - primary), points

    def streams(self) -> tuple[bytes, bytes]:
        return (stream(self.records, self.header),
                stream(self.additional) if self.additional else b"")


def project(folder: Path, sheets: dict[str, Sheet], design: str = "", devices=(),
            annotation: str | None = None):
    """A .PrjPcb listing the sheets ([DeviceSheetN] for those in devices), [Design] keys
    from design ("Key=Value" lines), an optional .Annotation file's text, and placeholder
    sheet files. Returns (prj path, read_sheet)."""
    folder.mkdir(parents=True, exist_ok=True)
    names = [name for name in sheets if name not in devices]
    docs = "".join(f"[Document{i}]\nDocumentPath={name}\n" for i, name in enumerate(names, 1))
    docs += "".join(f"[DeviceSheet{i}]\nDocumentPath={name}\n"
                    for i, name in enumerate(devices, 1))
    if annotation is not None:
        (folder / "Board.Annotation").write_text(annotation, encoding="utf-8")
        docs += f"[Document{len(names) + 1}]\nDocumentPath=Board.Annotation\n"
    prj = folder / "Board.PrjPcb"
    prj.write_text(f"[Design]\n{design}{docs}", encoding="utf-8")
    streams = {}
    for name, sheet in sheets.items():
        (folder / name).write_bytes(b"")
        streams[name.lower()] = sheet.streams()

    def read_sheet(path):
        data, additional = streams[Path(path).name.lower()]
        return connectivity_from_stream(data, additional, source=str(path))

    return str(prj), read_sheet
