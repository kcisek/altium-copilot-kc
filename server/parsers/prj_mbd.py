"""Reader for Altium multi-board project files (.PrjMbd).

A .PrjMbd is the same INI layout as a .PrjPcb (read with parsers/prj_pcb.py's section
splitter): [Design] holds the project options, [Electrical Rules Check] the report level of
each violation type (TypeN, as in a .PrjPcb; the multi-board ones are Type114 Different Net
Names, Type115 Entry Is Empty, Type116 No Mated Part, Type117 No Net and Type118 Unresolved
Conflict), and one [DocumentN] per document
with DocumentPath relative to the .PrjMbd: the child board projects (.PrjPcb), the
multi-board schematic (.MbsDoc), the multi-board assembly (.MbaDoc), harness projects
(.PrjHar), an ActiveBOM document and libraries. A document entry may carry a ProjectName
(child projects in a Workspace) and a DocumentUniqueId.

Pure parsing: paths are resolved against the .PrjMbd's folder, nothing is opened."""

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.prj_pcb import _fields, _section, split_sections
except ImportError:
    from server.parsers.prj_pcb import _fields, _section, split_sections

MAX_BYTES = 8 << 20

# Document kinds by extension (lower case).
KINDS = {
    ".prjpcb": "board_project",
    ".mbsdoc": "schematic",
    ".mbadoc": "assembly",
    ".prjhar": "harness_project",
    ".bomdoc": "bom",
}


class PrjMbdError(ValueError):
    """The file is not a readable .PrjMbd."""


@dataclass
class MbdDocument:
    """A [DocumentN] entry of the .PrjMbd."""
    section: str            # e.g. Document3
    raw: str                # DocumentPath as written
    path: str               # resolved against the .PrjMbd's folder
    kind: str               # board_project, schematic, assembly, harness_project, bom, other
    project_name: str = ""  # ProjectName, when written
    unique_id: str = ""     # DocumentUniqueId, when written

    @property
    def exists(self) -> bool:
        return Path(self.path).is_file()


@dataclass
class PrjMbd:
    path: str
    name: str
    design: dict[str, str] = field(default_factory=dict)      # [Design] as written
    erc_levels: dict[str, str] = field(default_factory=dict)  # [Electrical Rules Check]
    documents: list[MbdDocument] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)  # [ParameterN] Name/Value
    variants: list[str] = field(default_factory=list)         # [ProjectVariantN] Description

    def of_kind(self, kind: str) -> list[MbdDocument]:
        return [document for document in self.documents if document.kind == kind]

    @property
    def board_projects(self) -> list[MbdDocument]:
        return self.of_kind("board_project")

    @property
    def schematics(self) -> list[MbdDocument]:
        return self.of_kind("schematic")

    @property
    def assemblies(self) -> list[MbdDocument]:
        return self.of_kind("assembly")


def document_kind(raw: str) -> str:
    return KINDS.get(Path(raw.replace("\\", "/")).suffix.lower(), "other")


def parse_prj_mbd_text(text: str, path: str) -> PrjMbd:
    base = Path(path).parent
    sections = split_sections(text)
    project = PrjMbd(path=str(path), name=Path(path).stem,
                     design=_section(sections, "Design"),
                     erc_levels=_section(sections, "Electrical Rules Check"))
    for header, lines in sections:
        fields = _fields(lines)
        if re.match(r"^Document\d+$", header, re.IGNORECASE):
            raw = fields.get("DocumentPath", "")
            if not raw:
                continue
            resolved = str((base / raw.replace("\\", "/")).resolve())
            project.documents.append(MbdDocument(
                section=header, raw=raw, path=resolved, kind=document_kind(raw),
                project_name=fields.get("ProjectName", ""),
                unique_id=fields.get("DocumentUniqueId", "")))
        elif re.match(r"^Parameter\d+$", header, re.IGNORECASE):
            name, value = fields.get("Name", ""), fields.get("Value", "")
            if name:
                project.parameters[name] = value
        elif re.match(r"^ProjectVariant\d+$", header, re.IGNORECASE):
            description = fields.get("Description", "")
            if description:
                project.variants.append(description)
    return project


def parse_prj_mbd(path: str) -> PrjMbd:
    """The .PrjMbd at path. Raises PrjMbdError when it cannot be read."""
    file = Path(path)
    try:
        size = file.stat().st_size
        if size > MAX_BYTES:
            raise PrjMbdError(f"the file is larger than {MAX_BYTES >> 20} MB")
        data = file.read_bytes()
    except OSError as error:
        raise PrjMbdError(f"cannot read the file ({type(error).__name__})") from error
    text = data.decode("latin-1")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if "[Design]" not in text and not re.search(r"^\[Document\d+\]", text, re.MULTILINE):
        raise PrjMbdError("no [Design] or [Document<n>] section: not a .PrjMbd")
    return parse_prj_mbd_text(text, str(file.resolve()))
