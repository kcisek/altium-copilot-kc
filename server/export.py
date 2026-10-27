import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# IMPORTANT: Bump this — and SUPPORTED_SCHEMA_VERSION in pcb-copilot — when an existing
# table or column changes or goes away. A new table that older readers can ignore (like
# `notes` or `firmware_pins`) does not need a bump: pcb-copilot refuses any version it
# does not know. See CONTRIBUTING.md.
SCHEMA_VERSION = 1

HIGH_FANOUT_THRESHOLD = 25


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE project (
          id               INTEGER PRIMARY KEY,
          name             TEXT NOT NULL,
          root_dir         TEXT,
          exported_at      TEXT NOT NULL,
          exported_by      TEXT NOT NULL,
          schema_version   INTEGER NOT NULL,
          sheet_count      INTEGER NOT NULL,
          component_count  INTEGER NOT NULL,
          net_count        INTEGER NOT NULL
        );
        CREATE TABLE sheets (
          id   INTEGER PRIMARY KEY,
          name TEXT NOT NULL
        );
        CREATE TABLE variants (
          id         INTEGER PRIMARY KEY,
          name       TEXT NOT NULL,
          dnp_refdes TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE components (
          id          INTEGER PRIMARY KEY,
          refdes      TEXT NOT NULL UNIQUE,
          mpn         TEXT,
          description TEXT,
          value       TEXT,
          sheet_id    INTEGER REFERENCES sheets(id)
        );
        CREATE TABLE nets (
          id        INTEGER PRIMARY KEY,
          name      TEXT NOT NULL UNIQUE,
          pin_count INTEGER NOT NULL
        );
        CREATE TABLE pins (
          id           INTEGER PRIMARY KEY,
          component_id INTEGER NOT NULL REFERENCES components(id),
          pin_number   TEXT NOT NULL,
          pin_name     TEXT NOT NULL,
          net_name     TEXT REFERENCES nets(name)
        );
        -- Designer-added information, as get_design_notes reports it. Pictures are
        -- described in data (file name, size, index), their image bytes are not stored.
        --   project:   parameter
        --   sheet:     note, text_frame, label, image, directive, title_block, template
        --   draftsman: drawing (source PcbDoc, pages), parameter, note, text, table,
        --              picture, view, drill, layer_note
        CREATE TABLE notes (
          id            INTEGER PRIMARY KEY,
          document_kind TEXT NOT NULL,             -- 'project' | 'sheet' | 'draftsman'
          document      TEXT NOT NULL,             -- project, sheet or Draftsman file name
          sheet_id      INTEGER REFERENCES sheets(id),
          page          INTEGER,                   -- Draftsman page, 1-based
          kind          TEXT NOT NULL,
          title         TEXT,                      -- note/table/view title, parameter name
          text          TEXT NOT NULL DEFAULT '',
          author        TEXT,
          near_refdes   TEXT NOT NULL DEFAULT '[]',  -- components drawn next to it
          data          TEXT NOT NULL DEFAULT '{}'   -- kind-specific fields, JSON
        );
        CREATE INDEX idx_pins_net_name       ON pins(net_name);
        CREATE INDEX idx_pins_component_id   ON pins(component_id);
        CREATE INDEX idx_components_sheet_id ON components(sheet_id);
        CREATE INDEX idx_notes_sheet_id      ON notes(sheet_id);
    """)


def _create_derived_schema(conn: sqlite3.Connection) -> None:
    """Tables computed from the design by the copilot's analyses rather than read from
    it. They are new tables older readers ignore, so they need no SCHEMA_VERSION bump."""
    conn.executescript("""
        -- The firmware pin map (get_firmware_pin_map) of each processor it picks: one row
        -- per pin. A heuristic from pin and net names: function and port are a starting
        -- point for the firmware team, not a verified pin-mux.
        CREATE TABLE firmware_pins (
          id          INTEGER PRIMARY KEY,
          mcu_refdes  TEXT NOT NULL,
          pin_number  TEXT NOT NULL,
          pin_name    TEXT NOT NULL DEFAULT '',
          port        TEXT,                        -- 'A', '0'; NULL when the name gives none
          bit         INTEGER,                     -- the port bit, or the GPIO number
          net_name    TEXT,
          function    TEXT NOT NULL,               -- uart, spi, i2c, swd, gpio, power ...
          role        TEXT,                        -- tx, rx, sck, mosi, sda ...
          pull        TEXT,                        -- 'up 10k to 3V3 (R5)'
          peers       TEXT NOT NULL DEFAULT '[]'   -- parts the pin reaches, JSON
        );
        CREATE INDEX idx_firmware_pins_mcu ON firmware_pins(mcu_refdes);
        -- What each variant changes besides Not Fitted (variants.dnp_refdes): one row per
        -- varied parameter and per alternate part, bound to the parts by UniqueId like
        -- the BOM (compare_variants).
        CREATE TABLE variant_variations (
          id          INTEGER PRIMARY KEY,
          variant     TEXT NOT NULL,
          refdes      TEXT NOT NULL,
          kind        TEXT NOT NULL,              -- 'parameter' | 'alternate_part'
          parameter   TEXT,                       -- parameter name as written
          value       TEXT,                       -- the variant's value; alternate: its
                                                  -- library reference
          base_value  TEXT,                       -- the base design's
          data        TEXT NOT NULL DEFAULT '{}'  -- alternate part: library, footprint, JSON
        );
        CREATE INDEX idx_variant_variations_refdes ON variant_variations(refdes);
        -- The mechanical handoff (get_mechanical_handoff) of the project's PcbDoc, for the
        -- mechanical team: lengths in mm, coordinates relative to the board origin, viewed
        -- from the top; polygons are JSON [[x, y], ...]. Plan view only (not STEP / IDX).
        CREATE TABLE mech_board (
          id            INTEGER PRIMARY KEY,
          pcb_doc       TEXT,
          thickness_mm  REAL,                     -- from the layer stack; NULL when unknown
          width_mm      REAL,
          height_mm     REAL,
          area_mm2      REAL,
          origin_x_mm   REAL,                     -- the board origin, absolute
          origin_y_mm   REAL,
          data          TEXT NOT NULL DEFAULT '{}'  -- per-side summaries, JSON
        );
        CREATE TABLE mech_outline (
          id       INTEGER PRIMARY KEY,
          kind     TEXT NOT NULL,                 -- 'outline' | 'cutout' | 'board_region'
          name     TEXT,
          polygon  TEXT NOT NULL,
          data     TEXT NOT NULL DEFAULT '{}'     -- board regions: layer_stack, is_flex
        );
        CREATE TABLE mech_mounting_holes (
          id                INTEGER PRIMARY KEY,
          refdes            TEXT,                 -- the mounting-hole part; NULL: a free hole
          x_mm              REAL NOT NULL,
          y_mm              REAL NOT NULL,
          drill_mm          REAL NOT NULL,
          slot_length_mm    REAL,
          plated            INTEGER,
          net_name          TEXT,
          pad_mm            REAL,
          screw             TEXT,                 -- 'M3': the largest thread that passes
          free_radius_mm    REAL,                 -- to the nearest copper, body or edge
          limited_by        TEXT,
          edge_distance_mm  REAL,                 -- hole wall to the board edge
          fitted            INTEGER NOT NULL DEFAULT 1  -- 0: its part is Not Fitted (the
                                                         -- hole is drilled all the same)
        );
        CREATE TABLE mech_connectors (
          id              INTEGER PRIMARY KEY,
          refdes          TEXT NOT NULL,
          x_mm            REAL,
          y_mm            REAL,
          rotation        REAL,
          side            TEXT,
          edge            TEXT,                   -- left | right | top | bottom | outline | cutout
          edge_offset_mm  REAL,                   -- negative: past the edge (overhang)
          overhang_mm     REAL,
          facing          TEXT,                   -- '+X', '-Y' ... when at the edge
          kind            TEXT,                   -- external | internal | unknown
          data            TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE mech_regions (
          id             INTEGER PRIMARY KEY,
          kind           TEXT NOT NULL,           -- keepout_layer, copper_keepout, room ...
          name           TEXT,
          side           TEXT,
          polygon        TEXT NOT NULL,
          max_height_mm  REAL,
          data           TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE mech_parts (
          id             INTEGER PRIMARY KEY,
          refdes         TEXT NOT NULL,
          side           TEXT,
          x_mm           REAL,
          y_mm           REAL,
          rotation       REAL,
          height_mm      REAL,
          standoff_mm    REAL,
          height_source  TEXT NOT NULL,           -- body | footprint_height | library_body
                                                  -- | none | not_fitted
          fitted         INTEGER NOT NULL,
          extent         TEXT                     -- [min x, min y, max x, max y], JSON
        );
        CREATE INDEX idx_mech_parts_refdes ON mech_parts(refdes);
        -- The project's ActiveBOM document (get_active_bom) for the active variant: per BOM
        -- line its ranked solutions, one row per part choice (supplier part); a solution
        -- without part choices (from a Solution record or the parts' MPN parameter) once.
        -- Stock, price and lifecycle are the BomDoc's cache as of cache_date.
        CREATE TABLE bom_solutions (
          id                  INTEGER PRIMARY KEY,
          line                INTEGER,             -- the BomDoc line number; NULL: unnumbered
          design_item_id      TEXT,
          designators         TEXT NOT NULL DEFAULT '[]',  -- fitted parts, JSON
          position            INTEGER NOT NULL,    -- 1: the chosen solution
          rank                INTEGER,             -- ActiveBOM's rank (higher first)
          manufacturer        TEXT,
          mpn                 TEXT,
          source              TEXT NOT NULL,       -- part_choices | solution_record |
                                                   -- component_parameters
          solution_lifecycle  TEXT,                -- the worst over its part choices
          supplier            TEXT,
          sku                 TEXT,
          stock               INTEGER,             -- NULL: none cached (or no supplier)
          unit_price          REAL,                -- at the production need
          price_break_quantity REAL,               -- the price break that price is from
          below_minimum_order INTEGER,             -- 1: the need is under the first break
          extended_price      REAL,                -- unit_price x the need (x the first
                                                   -- break's quantity when below it)
          currency            TEXT,
          lifecycle           TEXT,
          cache_date          TEXT                 -- when this part choice was cached
        );
        CREATE INDEX idx_bom_solutions_line ON bom_solutions(line);
        -- BOM items added in the BomDoc with no design parts (bare board, screws, glue).
        CREATE TABLE bom_non_pcb_lines (
          id              INTEGER PRIMARY KEY,
          line            INTEGER,
          design_item_id  TEXT,
          description     TEXT,
          comment         TEXT,
          item_type       TEXT,
          quantity        REAL,
          manufacturer    TEXT,
          mpn             TEXT,
          note            TEXT
        );
    """)


# Columns of the mech_* tables in insert order; JSON columns are dumped.
MECHANICAL_TABLES = {
    "mech_board": ("pcb_doc", "thickness_mm", "width_mm", "height_mm", "area_mm2",
                   "origin_x_mm", "origin_y_mm", "data"),
    "mech_outline": ("kind", "name", "polygon", "data"),
    "mech_mounting_holes": ("refdes", "x_mm", "y_mm", "drill_mm", "slot_length_mm", "plated",
                            "net_name", "pad_mm", "screw", "free_radius_mm", "limited_by",
                            "edge_distance_mm", "fitted"),
    "mech_connectors": ("refdes", "x_mm", "y_mm", "rotation", "side", "edge", "edge_offset_mm",
                        "overhang_mm", "facing", "kind", "data"),
    "mech_regions": ("kind", "name", "side", "polygon", "max_height_mm", "data"),
    "mech_parts": ("refdes", "side", "x_mm", "y_mm", "rotation", "height_mm", "standoff_mm",
                   "height_source", "fitted", "extent"),
}
_JSON_COLUMNS = {"data", "polygon", "extent"}
# Columns of the bom_* tables in insert order (designators is JSON).
BOM_DOC_TABLES = {
    "bom_solutions": ("line", "design_item_id", "designators", "position", "rank",
                      "manufacturer", "mpn", "source", "solution_lifecycle", "supplier", "sku",
                      "stock", "unit_price", "price_break_quantity", "below_minimum_order",
                      "extended_price", "currency", "lifecycle", "cache_date"),
    "bom_non_pcb_lines": ("line", "design_item_id", "description", "comment", "item_type",
                          "quantity", "manufacturer", "mpn", "note"),
}


def export_project(
    project: dict,
    netlist: dict,
    variant_state,
    version: str,
    notes: list[dict] | None = None,
    firmware_pins: list[dict] | None = None,
    variant_variations: list[dict] | None = None,
    mechanical: dict[str, list[dict]] | None = None,
    bom_doc: dict[str, list[dict]] | None = None,
) -> str:
    """Write in-memory project to a pcb-copilot .db file next to the .PrjPcb.

    notes are rows for the notes table (services.design_notes.note_rows); sheet notes
    are linked to their sheet by name. firmware_pins are rows for the firmware_pins table
    (services.firmware_pins.export_rows). variant_variations are rows for the
    variant_variations table (services.variants.export_rows). mechanical holds the mech_*
    tables' rows by table (services.mechanical_fit.export_rows). bom_doc holds the
    bom_solutions and bom_non_pcb_lines rows (services.active_bom.export_rows).
    Returns the absolute path to the written file.
    Raises ValueError if a duplicate refdes is detected during INSERT.
    Always overwrites any existing file at the same path.
    """
    db_path = str(Path(project["root_dir"]) / f"{project['name']}-pcb-copilot.db")
    components = netlist.get("components", {})
    nets_raw = netlist.get("nets", {})

    if Path(db_path).exists():
        Path(db_path).unlink()

    conn = sqlite3.connect(db_path)
    _committed = False
    try:
        _create_schema(conn)
        _create_derived_schema(conn)

        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO project"
            " (name, root_dir, exported_at, exported_by, schema_version,"
            " sheet_count, component_count, net_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                project["name"],
                project["root_dir"],
                exported_at,
                f"altium-copilot v{version}",
                SCHEMA_VERSION,
                len(project["sheets"]),
                len(components),
                len(nets_raw),
            ),
        )

        sheet_id_map: dict[str, int] = {}
        for sheet in project["sheets"]:
            cur = conn.execute("INSERT INTO sheets (name) VALUES (?)", (sheet["name"],))
            sheet_id_map[sheet["name"].lower()] = cur.lastrowid

        for variant in variant_state._variants:
            conn.execute(
                "INSERT INTO variants (name, dnp_refdes) VALUES (?,?)",
                (variant.name, json.dumps(variant.dnp_refdes)),
            )

        for net_name, connections in nets_raw.items():
            pin_count = len(connections)
            conn.execute(
                "INSERT INTO nets (name, pin_count) VALUES (?,?)",
                (net_name, pin_count),
            )

        for refdes, comp in components.items():
            sheet_name = comp.get("sheet", "")
            sheet_id = sheet_id_map.get(sheet_name.lower())
            try:
                cur = conn.execute(
                    "INSERT INTO components (refdes, mpn, description, value, sheet_id)"
                    " VALUES (?,?,?,?,?)",
                    (
                        refdes,
                        comp.get("mpn"),
                        comp.get("description"),
                        comp.get("value"),
                        sheet_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Duplicate refdes detected: {refdes}")
            comp_id = cur.lastrowid
            for pin_num, pin_obj in comp.get("pins", {}).items():
                if isinstance(pin_obj, dict):
                    pin_name = pin_obj.get("name", pin_num)
                    net_name = pin_obj.get("net") or None
                else:
                    pin_name = pin_num
                    net_name = pin_obj or None
                conn.execute(
                    "INSERT INTO pins (component_id, pin_number, pin_name, net_name)"
                    " VALUES (?,?,?,?)",
                    (comp_id, pin_num, pin_name, net_name),
                )

        for note in notes or []:
            sheet_id = (
                sheet_id_map.get(note["document"].lower())
                if note["document_kind"] == "sheet" else None
            )
            conn.execute(
                "INSERT INTO notes (document_kind, document, sheet_id, page, kind, title,"
                " text, author, near_refdes, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    note["document_kind"],
                    note["document"],
                    sheet_id,
                    note.get("page"),
                    note["kind"],
                    note.get("title"),
                    note.get("text") or "",
                    note.get("author"),
                    json.dumps(note.get("near_refdes") or []),
                    json.dumps(note.get("data") or {}),
                ),
            )

        for row in firmware_pins or []:
            conn.execute(
                "INSERT INTO firmware_pins (mcu_refdes, pin_number, pin_name, port, bit,"
                " net_name, function, role, pull, peers) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["mcu"],
                    row["pin"],
                    row.get("name") or "",
                    row.get("port"),
                    row.get("bit"),
                    row.get("net"),
                    row["function"],
                    row.get("role"),
                    row.get("pull"),
                    json.dumps(row.get("peers") or []),
                ),
            )

        for row in variant_variations or []:
            conn.execute(
                "INSERT INTO variant_variations (variant, refdes, kind, parameter, value,"
                " base_value, data) VALUES (?,?,?,?,?,?,?)",
                (
                    row["variant"],
                    row["refdes"],
                    row["kind"],
                    row.get("parameter"),
                    row.get("value"),
                    row.get("base_value"),
                    json.dumps(row.get("data") or {}),
                ),
            )

        for table, columns in MECHANICAL_TABLES.items():
            for row in (mechanical or {}).get(table) or []:
                values = []
                for column in columns:
                    value = row.get(column)
                    if column in _JSON_COLUMNS and not (column == "extent" and value is None):
                        value = json.dumps(value if value is not None else {})
                    values.append(value)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})", values)

        for table, columns in BOM_DOC_TABLES.items():
            for row in (bom_doc or {}).get(table) or []:
                values = [json.dumps(row.get(column) or []) if column == "designators"
                          else row.get(column) for column in columns]
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})", values)

        conn.commit()
        _committed = True
    finally:
        conn.close()
        if not _committed:
            Path(db_path).unlink(missing_ok=True)
    return db_path
