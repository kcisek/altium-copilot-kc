# Altium Copilot (KC)

> **This is the KC fork** of [ee-in-a-box/altium-copilot](https://github.com/ee-in-a-box/altium-copilot), with many more tools (see the CHANGELOG). It installs as its own MCP server, `altium-copilot-kc` (in `%LOCALAPPDATA%\altium-copilot-kc`, with its own state file and update check), so it can sit beside the upstream `altium-copilot` and Claude always shows which one a tool comes from.

[![CI](https://github.com/kcisek/altium-copilot-kc/actions/workflows/ci.yml/badge.svg)](https://github.com/kcisek/altium-copilot-kc/actions/workflows/ci.yml)
[![Downloads](https://img.shields.io/github/downloads/kcisek/altium-copilot-kc/total?color=brightgreen)](https://github.com/kcisek/altium-copilot-kc/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

**Software engineers have Claude Code. Hardware engineers have been pasting netlists into chat.**

> Read the full writeup → [maisamp.github.io](https://maisamp.github.io/posts/altium-copilot/)

Altium Copilot gives Claude read access to your Altium project: schematics, board, design rules, BOM, DRC report and design notes. Ask it about component values, trace a net across sheets, check a net's routing against its rules, pull a per-variant BOM, review your schematic, brainstorm a circuit — all from your Claude chat interface, grounded in your actual design files.

For now, the copilot is read-only. It only extracts data. It cannot modify your schematic, move components, or overwrite your `.PrjPcb` file, **more features coming soon.** The only files it creates are the Protel netlist it asks Altium to export and, when you ask for one, a `package_for_xfn` snapshot.

Part of the [ee-in-a-box](https://github.com/ee-in-a-box) suite.

![Demo](assets/Intro.gif)



---

## What it does

| What you ask | What happens | Tool |
|---|---|---|
| *"What's on the power sheet?"* | Every component on the sheet with its pins and nets, one-hop cross-sheet neighbours, and the sheet's notes | `get_sheet_context` |
| *"What's connected to the 3.3V net?"* | Traces the net across all sheets | `query_net` |
| *"What's different in the Prototype variant? Is each variant documented?"* | Every difference between two variants (or a variant and the base design): parts fitted in one only, varied Value and Comment, other varied parameters, alternate parts (library reference, library and footprint, with their copy in the project's `.PrjPcbVariants` file) and variant parameters, bound to the parts by UniqueId like the BOM; and per variant, whether a connected BOM, pick-and-place and assembly-drawing output is set to it and which Draftsman drawings are set to it or name it. `get_component`, `get_sheet_context` and `search_components` show the active variant's values, `get_component` with the base design's | `compare_variants`, `list_variants` |
| *"Review this power supply circuit"* | Structured multi-phase schematic review | `schematic_review` |
| *"I'm new to this board, walk me through it"* | A guided tour from the sheet hierarchy to the PCB, outputs, variants and notes, ending in a project primer you keep | `project_tour` |
| *"What does U12 do?"* | Pulls component, MPN, value, DNP status, all pins, connected nets | `get_component` |
| *"Find every 10k resistor"* | Regex search over refdes, MPN or description, grouped by MPN | `search_components` |
| *"Where does RMII_REF_CLK route?"* | Layers, routed length per layer, vias, endpoints | `get_net_pcb` |
| *"What runs within 10 mil of CLK on L3?"* | Crosstalk proximity with parallel run lengths | `get_net_neighbors` |
| *"Describe my stackup"* | Layer-by-layer: copper weight, dielectric, material | `get_board_info` |
| *"Give me the BOM for the Lite variant"* | Per-variant BOM from the compiled parts with MPN and manufacturer, variant differences, CSV; `check_bom` for missing or inconsistent part numbers | `get_bom`, `check_bom` |
| *"Are my ICs decoupled? Is C12 close enough to U3?"* | Decoupling coverage per rail and IC: rails with no capacitor to ground, only Not Fitted ones, or fed through a ferrite or resistor without their own; with the PcbDoc, each IC rail's nearest capacitors and their distance, and a warning above 5 mm. Advice, not DRC: rails come from net and pin names | `check_decoupling` |
| *"Are my pull-ups right? Is anything floating? Are the I2C pull-ups the right value?"* | Signals that need a defined state, found from net and pin names and the pins' electrical types: reset, enable, shutdown, boot, strap and mode nets, open-drain nets (OpenCollector pins, I2C, interrupt, alert, power-good, fault), MOSFET gates and transistor bases driven from an IC, comparator outputs, and on request connector pins wired straight to IC inputs; each with its pull-ups and pull-downs fitted in the active variant (nets joined by a series resistor under 1 kohm count as one signal). Findings for a missing pull, pulls that are all Not Fitted, both up and down, and pull-ups in parallel. I2C buses pair SDA and SCL and size the pull-up per NXP UM10204: effective resistance, Rmin = (VDD - 0.4 V) / IOL with VDD from the rail's name, Rmax = tr / (0.8473 x Cb) for standard, fast or fast-plus mode, with Cb given or estimated (10 pF per pin, plus routing when the PcbDoc loads) and a verdict. Advice, not ERC: pull-ups inside ICs are invisible | `check_pull_resistors` |
| *"Are my connectors ESD-protected? Is the USB shield tied right?"* | Per connector, whether it looks user-facing (USB, RJ45, D-Sub, terminal block, jack, coax, SFP port: external; board-to-board, FPC, debug header, card edge, configuration jumper: internal; else unknown, with the basis, and overridable) and a per-pin table: the pin's class (signal, ground, power, shield, unconnected), and for a signal the walk through series resistors, 0 ohm links, ferrites, common-mode chokes and series capacitors to the first IC pin, with the ESD/TVS devices met (on the connector side or behind a series part), series parts, capacitors to ground and pulls. Findings: exposed pins with no ESD/TVS (warning on external connectors), a shield tied to ground only through an RC network or left floating, a ground pin that reaches ground only through a capacitor or ferrite, sparse ground pins, low-speed off-board lines with no series resistor or 0 ohm footprint, and with the PcbDoc a TVS nearer the IC than the connector or far from it (plan view, the opposite side marked). Advice from part prefixes and text, after Altium's schematic review checklist and its TVS layout and EMC articles; choosing the TVS part is left to its datasheet | `check_interfaces` |
| *"How is this board powered? Draw the power tree. Is the input protected?"* | The power tree from the input connectors and batteries through their protection (fuse, reverse-polarity diode or P-FET, TVS, bulk capacitance) to each converter and rail: converters recognised by their words, part numbers and pin names (VIN, VOUT, SW, FB, EN, PG), each with its type and basis, EN state (tied, pulled, undervoltage divider, GPIO, another regulator's PG, floating, held off), PG use, feedback divider with the Vref the rail name implies (or the output a given Vref sets, and a leg a jumper switches in), input and output capacitors and the LC or pi filter on a switcher's input; rails with what feeds them and their load counts. Findings: rails with no source (or fed only through a resistor above 100 ohm), floating enables and enables held off with nothing driving them, divider mismatches, missing fuse, reverse-polarity (a P-FET's orientation read from its pins) or TVS on external inputs, unfiltered SMPS inputs, unused power-good, sequencing chains. JSON, or a Mermaid flowchart Claude can render. A draft read from names, after Altium's schematic review checklist and its power-supply and EMC articles: Claude checks each converter against its datasheet | `get_power_tree` |
| *"Are my capacitors rated high enough? Any DC-bias problems? Is this resistor running too hot?"* | Each fitted capacitor's voltage rating against the voltage across it: ceramics above 75% of their rating (50% on a switching converter's input or output, the ripple nets), tantalums and electrolytics above 80%, anything above its rating; small high-value Class II ceramics at 30% of their rating or more get an approximate DC-bias loss band; resistors get their power (dV^2/R) and working voltage against the stated or typical rating of their size, and LED, shunt and series-supply resistors are listed with what to look up. Net voltages come from rails you give, net names (3V3, -12V), the power tree (outputs set by a given Vref, rails joined through 0 ohm links, ferrites, fuses and inductors) and the midpoints of resistor dividers, each with its source and confidence; ratings, dielectric and size from parameters, text, decoded part numbers (Murata, TDK, Samsung, KEMET, Yageo, AVX, Taiyo Yuden, Walsin, tantalum and resistor families) and footprints, each with its source and any disagreement. Thresholds from a policy table (defaults after Altium's derating article and schematic review checklist, `derating_policy.json` in the project folder or a per-call override). A heuristic: Claude confirms findings in datasheets | `check_derating` |
| *"Do my logic levels match? Can this 5 V output hurt the 3.3 V MCU? Are the FPGA banks right?"* | For every signal that links ICs powered from different rails, each pin's I/O supply with its basis and confidence (an FPGA pin's bank supply from its name, such as VCCO_14 for IO_L1P_T0_14 or VCCIO3G for LVDS3G_6p; a level translator's or isolator's side supply; a clock part's output supply; else the part's VDDIO-style I/O supply, its only supply or its digital supply, a regulator's VIN only when nothing else is named) and each pin's direction (electrical type, else its name for the part's class: TX, MOSI, SCK and CS drive from a processor; a peripheral's SDO and INT drive; I2C lines are open-drain with the pull-up's rail as their high level). Candidates: a push-pull driver's rail below 70% of a receiver's (likely VOH < VIH; a warning at or below 50%, such as 1.8 V into 5 V), a driver or a pull-up more than 0.3 V above a receiver's supply, or a pull-up above an output's (softened for 5 V tolerant pins and series resistors), open-drain pull-up rails above or below a receiver's, the rails of unknown voltage to give in `rails` (all of them in one call), and level translators and isolators listed as handled. Crystal pins an oscillator drives are judged; crystals, differential pairs, CAN and RS-485 line pins and analog nets are left out. Voltages from rails you give, net names and the power tree. After Altium's common-errors article and schematic review checklist: candidates only, Claude confirms VOH, VIH and absolute maximum ratings in the datasheets | `check_logic_levels` |
| *"Check my dividers. What does this feedback divider set? How much current through this LED? Are the ADC inputs filtered?"* | Calculations a review recalculates, each with its inputs (and where each came from), formula, result and confidence: resistor dividers (two resistors, parallel or series-chained legs, also through a test point or header pin; each tap of a resistor stack, such as a window comparator's or an EN pin with a hysteresis tap) with their ratio, unloaded output, source impedance, current and a tolerance range when the resistors' tolerances are read, legs a jumper selects, and what each feeds (a converter's FB with Vout/Vref and the Vref the top's voltage implies, an EN/UVLO pin with the input voltage at which a threshold trips, an ADC input, a comparator, op-amp or reference); each LED's (a display's backlight pins too) and optocoupler input's current from its supply (a rail, a GPIO's I/O rail, a transistor), Vf (given, stated, or typical for its colour) and series resistor (parallel resistors combined, a resistor across the diode taking its share), with the resistors' power and, for optocouplers, the R or C across the input, the output pull-up and the CTR it needs; each ADC input's series R and shunt C with its cutoff, and other RC filters. Pull-up/pull-down pairs on driven lines and series terminations into a pull-down are not dividers. Findings for LED overcurrent, an LED or optocoupler input held off by the resistor across it, a divider above the pin's rail + 0.3 V or below ground, resistor overpower, unfiltered ADC inputs and optocouplers with nothing across the input. After Altium's schematic review checklist: Vf, Vref and thresholds are datasheet values, so these are calculations to check | `get_circuit_calculations` |
| *"Will this regulator run too hot? Are there enough thermal vias under the exposed pad? What sits next to the hot parts?"* | The parts that dissipate, each with its basis: the power tree's converters (LDOs, switchers, modules, chargers, load switches, eFuses), the FETs a converter's gate pins drive and the diodes on its switch node, FETs and diodes in series with a rail, current-sense shunts, resistors computed at 0.25 W or half their rating, power LEDs, processors, FPGAs, Ethernet PHYs, LED and motor drivers, and with the PcbDoc every part with an exposed pad or thermal tab. Dissipation where the inputs allow (LDO (Vin - Vout) x I_out + Vin x Iq, switcher Vout x I_out x (1/eta - 1), shunt I^2 x R, diode Vf x I with its stated or a typical Vf, resistor V^2/R from the net voltages, LED Vf x I) with loads, efficiencies and powers you give, never guessed, and Tj = Ta + thetaJA x P at the typical and worst-case ambient against Tj max. On the PcbDoc: the vias whose centre lies in each exposed pad or tab, or in a paddle the footprint draws as a fill or region (count against a common one-per-1.5 mm^2 guideline, drill, vias on another net, the planes and pours of its net they reach; a pad array's ground pads as one; shelved and unpoured pours listed apart), and hot parts within 5 mm of each other (not the parts of one power stage) or of electrolytics, crystals, references, temperature sensors and batteries. After Altium's IC thermal management article and 'Beyond the PCB review': an estimate, the datasheets and a thermal simulation decide | `check_thermal` |
| *"Is the 400 V copper far enough from the rest? What spacing does mains need? Check my creepage and clearance"* | For every pair of nets whose voltages differ by more than 15 V (peak), the smallest copper-to-copper distance on each layer (tracks, arcs, pads and vias with their copper on the layer, poured copper, regions and fills; shelved and unpoured pours ignored) with its location, against IPC-2221B Table 6-1: B1 on internal layers, B2 on external ones, B3 above 3050 m, B4 with a permanent coating, the A columns for assemblies (A6/A7 at component pads, A5 coated), and the per-volt increment above 500 V; or IPC-9592B's formulas; or your own figure (min_distance, for IEC 62368-1 or 60664-1 clearances, per pair, net or board). The board's own Clearance rule for each pair sits beside it (and is flagged when it allows less than the standard). Voltages from the working voltages you give (hv=, AC RMS judged at its peak, with phases), rails, net names and the power tree, each with its source and confidence; a switch node swings from 0 V to its peak; isolated groups (primary and secondary) take the barrier's working voltage across them. Nets that look like high voltage (HV, mains words, a connector's power input, a mains connector, varistors and X/Y capacitors, a converter's switch node) with no known voltage are listed to ask about, with the unknown-voltage copper near the high-voltage nets. Slots, holes and cutouts in the path of a finding are listed: creepage around them is not computed. Table values cross-checked across public sources (cited in the tool). A screening tool: spacing and safety standards decide | `check_hv_clearance` |
| *"Give me the MCU's pin map for firmware. Are TX and RX swapped? Can we program it?"* | The processor (MCU, SoC, FPGA or radio module) found by its GPIO-named pins (PA0, P0.12, GPIO12 ...), IO-typed pins and processor words, candidates ranked with scores. Per pin: number, full name, port and bit parsed from the name, electrical type, net, the parts it reaches directly and through up to two series parts, its pull and a likely function (UART, SPI, QSPI, I2C, SWD, JTAG, USB, CAN, Ethernet, analog, PWM, crystal, boot/strap, reset, LED, button ...) with its basis; power and ground pins summarised. As JSON rows, CSV, a C header (`#define NET_PORT` / `NET_PIN`) or a Zephyr-style devicetree fragment (vendor port names only for the vendors it knows, such as STM32's `GPIOA` and `&gpioa`; other ports as plain letters or comments). Findings after Altium's design review and schematic checklist articles: UART TX meeting TX or RX meeting RX (also on a CAN transceiver's lines), debug or programming pins (SWD, JTAG, UPDI, ICSP, a UART bootloader) that reach no header or test pad (or only another processor's GPIOs), boot and strap pins with their resistor and default level (tied, pulled, a divider with its voltage, driven, the internal default) and an STM32's BOOT0 or an ESP32's GPIO0 held at its bootloader level, the bus, chip select, WP/HOLD state and outside access of external flash, EEPROM or an FPGA's configuration memory, unused GPIO pins, two MCU pins on one net or a net named for a function the pin does not list, and pins whose parts change between variants. A heuristic from names: a starting point for the firmware team | `get_firmware_pin_map` |
| *"Why won't my project compile? What does ERC say?"* | An offline approximation of Altium's ERC (Altium never saves its own result): the saved sheets compiled with the project's net scope, then checked the way the Messages panel reports, with Altium's violation names, groups and wording, the project's Error Reporting levels (Altium's defaults where unset) and its Connection Matrix: nets with only one pin, nets with multiple names, duplicate nets, power ports made local by a port or off-sheet connector, floating net labels and power ports, unconnected wires, ports and sheet entries with no match (and ports with no matching port under a flat or global scope), missing child sheets, duplicate ports, entries, pins, sheet numbers and sheet file names, duplicate designators (with a note when the .Annotation file makes the clash) and unannotated ones, unused or duplicate parts of multi-part components, pin, port and sheet-entry type conflicts, unconnected pins, ports and sheet entries, floating input pins, nets with no driving source, empty net parameters, floating directives, differential pairs missing a partner, objects off the sheet's snap grid or off the sheet (one row per sheet with a count). No ERC markers (on the object, or anywhere on the net for a violation about the whole net) and compile masks are honoured and what they suppress is counted; each violation has its sheet, object, location and a fix. Advisories apart: rails named for one voltage under different names (3V3 / 3.3V / VCC_3V3), names differing only in case, wire ends that stop a few mil short of a pin, and the ground nets with their pin counts; a single-pin-net report. schematic_review runs it first, and get_project_options and check_release_readiness cite its counts (the release's ERC gate stays not verified) | `run_erc` |
| *"What vias does this board use? Is anything back drilled?"* | Via types by span, hole and pad with counts per type and net class, depth and aspect ratio from the stackup, annular ring, vias in pads, tenting, drill pairs and back drilling, and the via stubs left on differential pairs | `check_vias` |
| *"Is this board manufacturable? Can my fab make it?"* | A design-for-manufacture check against a fabricator's capability limits: Altimade's published limits by default, or your fab's numbers from a `fab_profile.json` in the project folder (template: [examples/fab_profile.json](examples/fab_profile.json)) or given in the chat. Trace width, copper spacing, hole sizes, annular rings, aspect ratios, hole to hole, hole walls to copper, copper and holes to the board edge (edge-connector fingers and edge-plated holes noted), layer count, copper weight, board size, drill count, and board rules looser than the fab; each check's status (fail, marginal, advisory when only the default profile's ordering limits are missed, or pass), severity, limit and worst value, spacing counted in places, overlapping holes whatever the fab, the board's own minimums whatever the fab, and the drill table. A miss within 2% of a limit (at most 1 mil) is a warning; solder mask, silkscreen, plane pullback, panels, courtyards and paste are listed as not checked | `check_dfm` |
| *"Can it be assembled? Any tombstoning risk? Enough fiducials?"* | Assembly (DFA) checks for the active variant, Not Fitted parts left out: placements and unique parts against the assembler's maximums (Altimade's 600 and 200 by default; unique parts are the BOM's lines with a netlist), SMD on both sides and through-hole parts on the bottom (extra process steps), part spacing between 3D body outlines and pad copper (the pads' extent for a part without a body) with overlapping parts, parts near the board edge, global and local fiducials, small passives whose pads meet their copper unequally (tombstoning), fine pitch, and the pick-and-place data (Center-X/Y computed as Altium writes it: parts without pads, which land at the absolute origin, asymmetric parts whose centre is off their body, and a board origin away from the lower-left corner). A limit your assembler did not give is measured, not judged | `check_dfm` |
| *"Can this board be tested? Which nets have testpoints?"* | Test access for in-circuit test (bed of nails) or flying probe, from the PcbDoc alone: probe sites (pads and vias marked as Fabrication Testpoints in Altium, and TP parts) per side, the share of nets with a site and which side a single-sided fixture should probe, nets that a Testpoint Usage rule or your list of critical nets needs without access, rails with fewer than 2 or more than 10 sites, ground with about one site per 20 connections, and on the side the fixture probes: site size (1.0 mm preferred, 0.9 mm acceptable), centre spacing (2.5 mm, 1.8 mm minimum), 3.2 mm from the board edge, sites under component bodies or near tall parts; tented testpoint vias, the Testpoint rule's sizes and sides, and tooling holes. The numbers are guidance in an `ict` or `flying_probe` profile and take your test house's values; Assembly Testpoint marks are not read | `check_testpoints` |
| *"How do I hand-solder the prototype? Give me an assembly checklist"* | An assembly sequence for the variant: the SMD of both sides first (the side with shorter parts first, and why), then through-hole parts, short to tall on each side, in steps per BOM line (by footprint and Comment without a netlist, marked approximate). Each step has its quantity, Comment, footprint, MPN and height, and per part the designator, Center-X/Y as Altium's pick-and-place file writes it, Ref-X/Y, rotation and a pin-1 or polarity hint (*pin 1 top-left*, *cathode (K) left*, seen from the side being assembled). Not Fitted parts, fiducials, mounting holes and no-BOM parts are left out and listed. A printable Markdown checklist with Done and Skipped columns, or CSV; no board map is drawn | `get_assembly_guide` |
| *"Is the board fully routed? What is left to route?"* | Per net, whether the saved copper (tracks, vias, pads, poured copper, regions, fills, internal planes) joins all its pads: routing progress with percentages, each missing connection between the nearest pads with its distance and near-miss copper gap, floating copper, and net antennae against the board's Net Antennae rule, beside Altium's last DRC report | `check_connectivity` |
| *"Run a health check on the board"* | Altium's PCB Health Check Monitor offline, with Altium's categories and levels: shelved or modified polygons, duplicate designators, zero-area and self-intersecting shapes, micro-segments, slot holes, xSignals, 360-degree parts, reference points outside the part; plus parts outside the board shape. Compared with the last DRC report's health section | `check_pcb_health` |
| *"What does my DRC report say? Any shorts?"* | Altium's last Batch DRC report grouped by rule, component or net, with a warning when it stopped at the violation limit, skipped checks or is older than the board | `get_drc_report` |
| *"Is the PCB in sync with the schematic?"* | Offline Show Differences: missing or extra parts, footprint and comment changes, pads on the wrong net, and pins Update PCB would fail on | `check_sch_pcb_sync` |
| *"Is my USB pair routed for 90 ohm?"* | Impedance profiles: target, solved width/gap and calculated impedance per layer, and which rules use them; the pair's routed widths checked against its rule | `get_board_info`, `check_rule_compliance` |
| *"Where is U12? What's next to it?"* | Placement, side, rotation, nearest parts | `get_component_placement` |
| *"What's at X 1200, Y 850?"* | Nets, components and saved pour outlines near a board coordinate | `query_pcb_region` |
| *"What width and clearance apply to USB_D_P?"* | The effective design rule per kind, resolved by priority, with values per layer | `get_rules_for_net` |
| *"Which rules point at classes that no longer exist?"* | Every rule's kind, priority, scope and values, with scope names that match no net, class, pair or room | `get_design_rules` |
| *"Which nets are in the HighSpeed class?"* | Net, component, diff-pair, pad, polygon and xSignal classes with their members | `get_classes` |
| *"List my differential pairs and their skew"* | Pairs, classes, length per leg (pad-to-pad path plus via barrels), and the routing rule for each | `list_diff_pairs` |
| *"Do my tracks and vias meet their rules?"* | Widths per layer, via sizes, holes, annular rings, routing layers and length windows checked against the rule that applies to each object; optional approximate clearance spot-check | `check_rule_compliance` |
| *"Are my DDR length groups within tolerance?"* | Length and matched-length rules evaluated like Altium, xSignals through series parts included | `check_lengths` |
| *"How long is the path from U1-5 to J1-3?"* | One net, pair, xSignal or pin-to-pin path measured on the saved copper, via barrels included | `get_signal_length` |
| *"Any polarity swaps or missing AC caps on my pairs?"* | Undeclared pairs, swapped P/N at pins, asymmetric series parts, in-pair skew against the rule | `check_diff_pairs` |
| *"What did we note about the power input?"* | Schematic notes, text frames and labels, with the parts they sit next to | `get_design_notes` |
| *"What are the fabrication notes?"* | Draftsman notes, tables, view titles, drill table and layer stack notes | `get_design_notes` |
| *"Show me the picture on the PSU sheet"* | Pasted datasheet excerpts and diagrams, returned as images | `get_design_image` |
| *"How is the design structured? Why is this net called VBAT on the child sheet?"* | An offline Report Project Hierarchy: the sheet tree from the top sheet through sheet symbols (device sheets, Repeat channels and their counts), each sheet's ports and its symbols' entries with I/O types and harnesses, the project's Net Identifier Scope (and what Automatic resolves to) with how nets cross sheets and how they are named; with a netlist, the net each port and entry joins. Flags symbols pointing at missing files, entries without a port and ports without an entry, I/O mismatches, ports on a top sheet, duplicate symbols and cycles. Works without Altium | `get_design_hierarchy` |
| *"Is ERC set up right? Is a No ERC hiding a problem? Where do outputs go?"* | The project settings that change how the design is read, from the .PrjPcb: Net Identifier Scope and net naming, diff-pair suffixes, class generation, multi-channel naming, the report level of every ERC violation type, the Connection Matrix, the Comparator's matching criteria and the output path. Audits ERC checks switched off or downgraded from Altium's default, relaxed Connection Matrix cells for driver conflicts and unconnected inputs, and every No ERC directive with the net it sits on, flagged when an active one is on a power net or a net with several pins; cites the offline ERC's counts (`run_erc`) and how many violations each switched-off check hides in this design. Works without Altium | `get_project_options` |
| *"Update PCB wants to delete all my parts. Why won't Constraint Manager save? Why do the compiled designators differ from the sheet?"* | Problems in the project files that silently break compile, sync or saving, each with its severity, the affected objects (sheet, designator, net, port or parameter, location), how to fix it in Altium and the Altium knowledge-base article behind it: schematics with legacy extensions (.S01, .S02, .Sch), documents missing on disk, a leftover .Annotation file, parts outside the sheet, compile masks hiding parts from the PCB, variant entries whose designator or UniqueId is gone, hidden, non-ASCII or edge-space characters in net labels, ports, designators and parameters, bus ranges Altium cannot read, local parameters ActiveBOM reserves ('Manufacturer 1', 'Manufacturer Part Number 1') and a multi-channel designator format that breaks bus names. Works without Altium | `check_project_integrity` |
| *"Is my OutJob complete? Is anything missing for fab or assembly? Are my Gerbers up to date?"* | Each Output Job of the project (and .OutJob files in its folder it does not list): its containers (PDF, folder structure, video, print job) with their path and naming, and its outputs by category with the containers they are connected to and their order, data source and variant. Coverage of fabrication data (Gerber, Gerber X2, ODB++, IPC-2581), NC drill, pick-and-place, assembly drawings, BOM and ERC/DRC validation, and findings with severity, an Altium fix and the knowledge-base article behind them: gaps, BOM and pick-and-place on different variants, a variant without an assembly drawing, data sources missing or outside the project, ERC validation levels that differ from Project Options, output or ECO log paths on another drive or near Windows' 260-character limit, Gerbers on the absolute origin next to ODB++ or a drill file on another origin, overlays missing from the layer stack, board cutouts with no mechanical layer plotted, Draftsman views with their own variant, and generated files older than the design. Works without Altium | `get_output_jobs` |
| *"Are my outputs right? Does the pick-and-place match the board? Check my release zip"* | The files Altium generated (the OutJob's container folders, the newest generation of a date-named one, and the Output Path; or a folder or release zip you name, read in memory) cross-checked against the saved design: pick-and-place parts present once per variant, Not Fitted parts absent, side, rotation and Center-X/Y (the pad-centre midpoint relative to the board origin, as Altium writes it), Ref-X/Y and Pad-X/Y, footprint; BOM designators against the variant's BOM parts, Not Fitted lines, quantity against the designator count, and the Comment, Description, Value, Footprint, Manufacturer and part number columns (.csv and .xlsx; part numbers from an ActiveBOM document's part choices are not read, so their differences are info); IPC-D-356A and IPC-2581 netlists pin by pin against the board's pad nets (pins missing, extra or unconnected on one side, nets split, merged or renamed, with designator, pin and XY); the IPC-2581 stackup and the drill layer pairs; NC drill tools, plating, hole and slot counts and hole positions per drill set (a .DRR report with the drill files it names), and the .DRR report against its drill files (a report for another PcbDoc is called out, not compared); files older than the design or the variants. A difference is reported with its file and examples, not judged | `verify_outputs` |
| *"Do my footprints still match the library? Run the component rule check. Can I swap U3 to another footprint? Where else is this part used?"* | The libraries behind the parts, read offline (the project's SchLibs, PcbLibs and IntLibs, the installed libraries and its search paths, searched in that order as Altium does): parts placed from a library the project no longer has (with a same-name symbol found elsewhere), names not found, footprints taken from another library than the named one; each placed footprint against its library footprint, as Altium's Footprint Comparison Report (pad count, pads missing or extra, every pad offset from the reference point, the footprint turned or mirrored, pads moved, pad size, angle, shape, hole, slot, plating and layer; Height, 3D bodies and other primitives as info), with a bottom-side part flipped back (mask and paste expansions, hole offsets and texts are not compared); every pin on a net having a copper pad on the board and in the library (a bare hole does not count), and whether a proposed footprint keeps them all; Altium's SchLib and PcbLib Component Rule Checks on the items the project uses or on a whole library (duplicate or missing pin and pad designators, pin numbering gaps, pins without pads and pads without pins, an exposed pad with no pin, duplicate primitives, mirrored footprints, shorted and unconnected copper, missing description, default designator, footprint or courtyard); net-tie footprints (their copper joins the pads, schematic, PCB and library agree on the type) and parts that are not net ties whose footprint joins pads on different nets (on the board now, or after an update from the library); parts with a 3D model, only the fallback Height or neither; where-used across the registered projects, optionally only parts placed from one library. A symbol or footprint, its pins with electrical types and its pads with their sizes and holes: `get_library_part`. Workspace (Altium 365) and DbLib parts cannot be read offline: they are listed as such | `check_libraries` |
| *"Is my fab drawing complete? Does it match the board? Is it up to date?"* | Each Draftsman drawing classified (fabrication, assembly or other) and checked for what a fabricator or assembler needs: IPC class, laminate, Tg, UL 94 flammability, surface finish, mask and silkscreen colours, board thickness and tolerance, copper weights, impedance, drill table, registration and tolerances, electrical test and, on a flex stack, the rigid-flex items (IPC-A-610 class, BOM table and both-side views for assembly drawings). Each item is found (the note line quoted, with its page, note and line) or missing (with a suggested note line using the design's numbers, `<...>` where a value must be agreed). Cross-checks what the drawing states or caches against the design: thickness and layer count against the layer stack, materials, copper weights, impedance notes and the impedance table against the Layer Stack Manager profiles, the drill table against the board's holes, the layer stack legend against the stack, the BOM table's variant against the OutJob and its parts against the BOM (with a netlist), the board it was made from, a board saved after its last update, and notes that contradict each other. Works without Altium (the BOM parts comparison needs the netlist) | `check_fab_drawing` |
| *"Is this ready to release? Is it ready for review or fab?"* | One table of release gates modelled on Altium's Project Releaser, each pass, fail, warn, not verified or not applicable, with a blocking flag, one line of evidence and the tool to ask for details: Batch DRC report clean and newer than the board, ERC (never verifiable from the files: it cites the offline ERC's counts from `run_erc` as evidence and says whether an OutJob ERC validation output makes the Releaser run it), netlist newer than the sheets, schematic and PCB in sync, no unrouted nets, polygons repoured, board outline, fiducials, mounting holes, Not Fitted parts left out of the BOM and pick-and-place outputs, OutJob complete for every variant and its outputs newer than the design, Draftsman fab notes against the stackup, title-block revision and date, project integrity, the Altium 365 copy and test access (advisory, on ICT guidance); footprints against the libraries, lifecycle and compliance, teardrops, via stitching and the team template are listed as not verified. One failed blocking gate fails the release; a missing data source is never reported as passed | `check_release_readiness` |
| *"Run our team checks. Does the board pass our rules? Run rule TB-HV-01."* | Your team's own deterministic rules, kept with the design as check packs (see [Custom check packs](#custom-check-packs)): select parts, nets or pins (designator, kind such as capacitor, TVS, IC or connector, fitted in the active variant, parameters, text, PCB side, case size, net class such as rail, ground, I2C, differential pair, off-board or user-facing connector, voltage) and assert on each one (a parameter present or matching, a count of parts of a kind on the net, a part of a kind within N series parts, the nearest part of a kind on the PCB within N mm, a capacitor's rated voltage at least N times the voltage across it, side, case size, or none allowed), or run an existing tool and turn its findings into a pass/fail gate with your severity. ERC-style findings (rule, severity, designators, nets, sheet and PCB position) with a verdict and a status per rule, including the rules that could not be evaluated and why. Opt-in Python plugins. A review checklist item can be a pack rule | `run_custom_checks` |
| *"Run the schematic checklist. What is left before sign-off? Write the review record. What changed since the last review?"* | A design-review checklist run against the saved project: schematic (power, signals and interfaces, components and ratings, connectors and protection, testability and production, style and documentation), PCB (placement, power and thermal, high speed and constraints, manufacturing, testability), manufacturing and release, and supply chain (BOM), written in this project's own words on the topics of Altium's schematic and PCB review checklists, or your team's own (see [Review checklists](#review-checklists)). Each item is auto (a deterministic tool decides: Yes, No or N/A), assisted (a tool gives the evidence and leans yes or no; the datasheet or the reviewer decides) or judgment (a person decides), and carries its evidence: the tool and arguments, the finding ids and the designators, nets and sheets they name. A coverage table counts the items by status per section; the design state is the Git commit (and whether design files changed since it) or the SHA1s of the project files, so a sign-off belongs to one state. The review record is Markdown with a sign-off table and a machine-readable block: given back as `previous=`, the next run says what newly fails, what was fixed and what is still open. Nothing is written | `run_review_checklist` |
| *"What changed since the last release? What did this commit change?"* | An ECO-style changelog between two revisions of the project's Git clone (commits, tags, branches or the files on disk; with none given, the recent commits to pick from) or two project folders: parts added, removed, re-designated or revalued, footprint and parameter changes, variant and per-variant BOM line changes, nets added, removed, renamed, merged or split and pins moved (from the PcbDoc), parts moved, rotated or flipped, rule, class, diff-pair and stackup changes, and routed-length and via deltas per net. Warns about ICT-fixture hazards: nets that kept their name while their pins changed, and designators now on another part. Revisions are read with read-only `git` commands into a temporary folder; the clone is never written. Works without Altium | `diff_design` |
| *"Is my copy of this Altium 365 project up to date?"* | Where the local Git clone stands, read offline: when it was last updated from Altium 365, commits ahead or behind as of that update, design files changed locally, the last commit. Never fetches | `get_clone_status` |
| *"Package this project for the firmware team"* | A SQLite snapshot your teammates open without Altium (see [Sharing with your team](#-new-sharing-with-your-team)) | `package_for_xfn` |

---

## Specialist modes

Claude activates these automatically based on what you say — no commands needed.

| Mode | Triggers when you say... | What happens |
|---|---|---|
| `schematic_review` | *"review this"*, *"is this correct?"*, *"check my power supply"* | 3-phase structured audit, starting from the offline ERC (`run_erc`) and the schematic checklist (`run_review_checklist`): Claude reads datasheets and confirms its understanding before flagging anything. Every finding cites a datasheet value or a netlist fact. Ends with the checklist's coverage table and review record. Ask Claude to write the report to a file if you want one. |
| `constraints_review` | *"check my high-speed constraints"*, *"are my DDR length rules right?"*, *"review the diff pairs before fab"* | 4-phase PCB constraints audit: Claude identifies each interface from its parts and pins, takes targets from the chip's own layout guide (typical values only as a labelled fallback), checks that pairs, classes, impedance profiles and rules encode them, then spot-checks widths, lengths, skew and neighbours on the saved copper. Checks the saved rules and copper; quotes Altium's last DRC report only with its assessment. Runs the pcb checklist's high-speed items (`run_review_checklist`) and ends with their coverage table and review record. |
| `pcb_manufacturing_review` | *"is this board ready to order?"*, *"review for manufacturing"*, *"go/no-go before fab"* | 4-phase manufacturing-readiness review: Claude asks which fabricator and assembler (their numbers or a `fab_profile.json`), confirms the profile, checks fabrication (`check_dfm`, vias, health, connectivity, Altium's DRC report with its assessment) then assembly (`check_dfm`'s DFA checks, the BOM), and ends in a go/no-go that lists what was not checked. Never presents Altimade's limits as another fab's. Runs the pcb checklist's manufacturing and testability items and the bom checklist (`run_review_checklist`) and ends with their coverage tables and review records. |
| `release_review` | *"is this ready to release?"*, *"release check"*, *"go/no-go before release"* | 3-phase release review: Claude runs `check_release_readiness`, calls each failing gate's source tool for the cause and the fix in Altium, and ends in a go / no-go with the blocking items, what was not verified (never counted as passed) and the warnings. Runs the release checklist (`run_review_checklist`) and ends with its coverage table and review record. |
| `project_tour` | *"walk me through this project"*, *"I'm new to this board"*, *"write a project primer"* | 5-phase guided tour in the order Altium teaches a design: project and sheet hierarchy, each schematic block and the power rails (read from the sheets and nets: there is no power-tree tool), PCB stackup, placement and key nets, outputs and manufacturing state (DRC report, health, BOM), then variants and design notes. Ends in a Markdown project primer with a fixed outline; Claude shows it and saves it only where you agree. Adapts to what is loaded: a netlist-free load, or a project without a PcbDoc or without schematic sheets, skips what it cannot read and says so. |
| `brainstorm_circuits` | *"I want to add a new circuit, lets brainstorm"*, *"How would i improve this circuit"*, *"What power supply topology should I use here?"*  | 5-phase design session: reads your loaded schematic first (it also works without a loaded project), then asks one question at a time — problem, constraints, proposes 2–3 topologies with tradeoffs, checks your existing design before recommending anything new. |

---

## 🆕 PCB awareness

Altium Copilot now reads your `.PcbDoc` directly (the first one listed in the project).
Ask where nets route, what's near what (crosstalk), your full stackup and impedance
profiles, and where components sit.
It also reads the board's design rules, classes, differential pairs and xSignals, and works out
which rule applies to a net the way Altium does — including rules that only apply to
some of a net's objects (its vias, one layer, a room) and scopes that point at classes
or pairs that no longer exist. It also checks the saved copper against those rules —
widths, vias, holes, annular rings, routing layers, length groups and diff-pair
consistency — and on the DRC reports we compared, its width, via, hole and
routing-layer counts matched Altium's. `check_sch_pcb_sync` compares the board with
the compiled schematic the way Show Differences does, without running an ECO.
It reads the saved file and never runs Altium's DRC; `get_drc_report` reads the last
DRC report Altium saved and says when it should not be trusted.
Data refreshes automatically every time you save the board in Altium. Read-only,
like everything else.

Not modelled by the rule checks (`check_rule_compliance`, `check_lengths`,
`check_diff_pairs`): solid pours' copper (a pour is its saved outline, plus its tracks
and arcs when hatched, so repour and save before asking about pours; shelved pours have
no copper), fills, regions and planes, pad slot holes and per-layer pad sizes (pad holes
and rings are not checked), and diff-pair uncoupled length. `check_connectivity` and
`check_dfm` read the copper a pour was last poured into, regions and fills, and
`check_dfm` checks pad holes, slots and rings per layer. The checks list what they left
out in their results.

---

## Design notes

Claude also reads what you wrote into the project besides the circuit: notes (with
author and collapsed state), text frames, free text, pictures, directives and title
blocks on every schematic sheet, project parameters, and the notes, tables, view titles,
drill table and layer stack notes in your Draftsman (`.PCBDwf`) drawings. Pictures can be
shown to Claude as images. `package_for_xfn` snapshots carry the same notes (pictures are
described, not embedded). Review comments made in Altium 365 are stored in your cloud
workspace, not in the project files, so they are not available.

---

## What it reads

Everything comes from the files Altium saves. Nothing is written back to your design.

| File | What is read |
|---|---|
| `.PrjPcb` | Sheet list (subfolders, `..\` paths and device sheets included), variants, project parameters, output folder, Comparator settings, Project Options (ERC report levels, Connection Matrix, class generation, multi-channel naming, Net Identifier Scope) |
| `.SchDoc` | Components, parameters, pins, footprints, compile masks, notes, text frames, labels, pictures, directives, title blocks, No ERC directives, sheet size, names as written (for `check_project_integrity`); for the [offline netlist](#offline-netlist) also wires, buses, junctions, net labels, power ports, ports, sheet symbols and entries, off-sheet connectors, signal harnesses with their connectors and entries |
| Protel netlist (`.NET`) | Connectivity and the compiled part list. Altium writes it: when it is missing or older than the sheets, the copilot sends Altium the keystrokes for Design » Netlist For Project » Protel. With Altium closed the copilot compiles the netlist itself from the sheets (see [Offline netlist](#offline-netlist)) |
| `.Annotation` | Board designators of board-annotated and multi-channel designs |
| `.PcbDoc` | Tracks, arcs, vias, pads, saved pour outlines (shelved or not), the copper a pour was last poured into (solid pours' regions, hatched pours' tracks and arcs), copper regions and fills, internal plane nets, components and designators, stackup, impedance profiles, rules, classes, differential pairs, xSignals, DRC settings; for the health check also region shapes, the board shape and Health Check settings; for the DFM check also pad holes (plated or not, a slot's angle, whether the hole is offset from the pad), bare holes such as mounting holes, board cutouts and the number of embedded board arrays (panels) |
| `review_checklists.json`, `review_checklists/*.json` (optional) | A team's changes to the bundled review checklists for `run_review_checklist`, from the project folder or the root of the Git repository above it. See [Review checklists](#review-checklists) |
| `check_packs.json`, `check_packs/*.json` (optional) | A team's deterministic rules for `run_custom_checks`, from the project folder or the root of the Git repository above it; `check_packs/plugins/*.py` (project folder only) are Python plugins, run only when allowed twice. See [Custom check packs](#custom-check-packs) |
| `fab_profile.json` (optional) | A fabricator's limits for `check_dfm`, from the project folder (or `<name>.json` named in the chat): name, source, `based_on` and limits in mm or mil. See [examples/fab_profile.json](examples/fab_profile.json) |
| `.PCBDwf` (Draftsman) | Notes, free text, tables, view titles, pictures, drill table, layer stack notes, the drawing's variant and the views' own variants; for `check_fab_drawing` also the layer stack (materials, thicknesses, weights, Tg), impedance lines and BOM table rows the drawing cached at its last update, and the BOM views' data source |
| `.OutJob` | Containers and their path and naming settings, outputs with their category, data source, variant, container links and order, and the setup of Gerber, Gerber X2, ODB++, IPC-2581, NC Drill, pick-and-place and ERC outputs (`get_output_jobs`). Generated output files are only listed for their dates, never opened |
| Generated outputs | For `verify_outputs`: pick and place (`.txt`, `.csv`), BOM (`.csv`, `.xlsx`; `.xls` is listed, not read), IPC-D-356A (`.ipc`), IPC-2581 (`.cvg`, `.xml`), Excellon NC drill (`.txt`), the NC drill report (`.DRR`) and layer pairs (`.LDP`), from the OutJob container folders and the Output Path, or a folder or `.zip` you name (zips are read in memory, one nested zip deep; nothing is extracted). Gerber and ODB++ files are counted, not read |
| `.SchLib`, `.PcbLib`, `.IntLib` | For `check_libraries` and `get_library_part`: the project's libraries ([DocumentN]), the installed libraries and its search paths (Altium's library folders and the existing files of `IntLib_Installed.ini`). Symbols: pins (designator, name, electrical type, part, display mode, hidden), parameters, description, default designator, type, footprint and other model links. Footprints: pads, bare holes, tracks, arcs, fills, regions, texts, 3D bodies with their models, Height and description, and the mechanical layer kinds (courtyard, assembly, 3D body). IntLibs are inflated in memory. `.DbLib`/`.SVNDbLib` (roadmap #53), `.LibPkg` and Workspace (Altium 365) libraries are not read |
| `Design Rule Check - <PcbDoc>.html` or `.drc` | Altium's last Batch DRC report, from the project's output folder |
| Git clone of an Altium 365 project (`.git`) | When the origin is `https://<workspace>.365.altium.com/git/<project GUID>.git`: the project GUID, the last fetch time (`FETCH_HEAD`), the local commit against the remote-tracking branch as of that fetch, uncommitted changes and the last commit. Read with local `git` commands that cannot open a connection (`protocol.allow=never`), do not rewrite the index (`--no-optional-locks`) and start no program the repository's configuration names (filters, fsmonitor, signature checks); nothing is fetched and no credential is used |
| Git history of the project's clone (`.git`, any origin) | For `diff_design`: the commits that changed the design files (`git log`), the files of each revision (`git ls-tree`, `git cat-file`, no filters run) written to a temporary folder outside the clone and deleted after the comparison. The same hardening as the Altium 365 clone above: no connection, no lazy fetch, no optional locks, no prompts; no command that writes is run |

Not read: ERC results, the contents of generated outputs and the DRC XML report, ActiveBOM
and supply-chain data (lifecycle, stock, price), and Altium 365 review comments. The
copilot never runs Altium's ERC or DRC. Altium's own netlist is preferred whenever it is up
to date or Altium has the project open; the sheets' wiring is compiled only without it.

### Review checklists

`run_review_checklist` ships four checklists (`server/checklists/*.json`): `schematic`,
`pcb`, `release` (manufacturing and release) and `bom` (supply chain). Called with no
checklist it lists them, with their sections and item counts. A team changes them with
`review_checklists.json`, or any `*.json` in a `review_checklists` folder, next to the
`.PrjPcb` or in the root of the Git repository above it (the repository's files apply
first, then the project folder's; in each folder the single file first, then the folder's
files by name). Template: [examples/review_checklists.json](examples/review_checklists.json).

```json
{
  "name": "Our team's review rules",
  "disable": ["SCH-STY-04"],
  "add_sections": [{"checklist": "schematic", "id": "team", "title": "Team rules"}],
  "add": [
    {"checklist": "schematic", "id": "TEAM-01", "section": "team", "kind": "auto",
     "text": "The offline ERC reports no warnings either.",
     "evidence": {"tool": "run_erc", "args": {},
                  "no": [{"path": "summary.by_level.Warning", "op": ">", "value": 0,
                          "label": "ERC warnings"}]}},
    {"checklist": "schematic", "id": "TEAM-02", "section": "team", "kind": "judgment",
     "text": "Every connector's mating part is named in the design notes."}
  ]
}
```

- `checklists`: whole checklists (`id`, `title`, `scope` one of schematic, pcb, release,
  bom, `sections` and `items`, as in the bundled files): one with a bundled id replaces it,
  a new id adds a checklist.
- `add_sections` and `add`: sections and items for an existing checklist (`checklist`
  names it). Item ids are unique across all checklists.
- `disable`: item ids to leave out; the record lists them.
- An item: `id`, `section`, `text` (your words), `kind` (`auto`, `assisted`, `judgment`
  or `custom`), optional `hint` and `source`; a custom item names a rule of your check
  packs (`"rule": "TB-HV-01"`): its pass is Yes, its fail No, not applicable N/A, and a
  rule that could not be evaluated needs judgment (plugins are not run from a checklist);
  auto and assisted items have `evidence`:
  `tool` (a read-only tool such as `run_erc`, `check_bom`, `check_dfm` or
  `check_release_readiness`), `args` (that tool's arguments) and condition lists tried in
  order: `na` (N/A), `no` (No), `judge` (needs judgment), then `yes` (all must hold; left
  out, Yes). A condition is `{"list": "findings", "where": {"check": ["no_esd"],
  "severity": "warning"}}` (the matching rows; any one by default, `op` and `value` change
  it), `{"path": "summary.errors", "op": ">", "value": 0}`, or `{"all": [...]}` /
  `{"any": [...]}`, each with a `label` for the evidence line. A `where` field can also be
  compared with another field of the same row (`{"op": ">", "field":
  "summary.marginal_violations"}`); a field a row does not have counts as `falsy`.
  `na_errors` lists the tool errors that mean N/A (default `no_pcb_document`); `anchors`
  lists `{"list": ...}` rows to take designators, nets and sheets from.
- A path or list a condition reads that is not in the tool's result (a misspelt path, a
  field the tool no longer gives) makes the item needs judgment with `unresolved`, never
  Yes; `exists`, `missing`, `truthy` and `falsy` may test fields that are left out.

Each file is checked like a fab profile: an unknown key, tool, operator, section or item
id, a duplicate id (ids ignore case), an argument the tool does not take or bad JSON is
reported with the file, the place in it and what to fix, and the review does not run
until it is fixed; `overrides=false` runs the bundled checklists only.

### Custom check packs

`run_custom_checks` runs your team's own rules: JSON check packs kept with the design,
`check_packs.json` or any `*.json` in a `check_packs` folder, next to the `.PrjPcb` or in
the root of the Git repository above it (the repository's files first, then the project
folder's; a pack whose `id` was already loaded replaces it), plus a pack file passed as
`path=`. Template, with the six example rules below and a tool rule:
[examples/check_packs.json](examples/check_packs.json).

```json
{
  "id": "team-basics",
  "title": "Team basics",
  "version": "1",
  "rails": {"VIN": 24},
  "rules": [
    {"id": "TB-BOM-01", "title": "Every part has a manufacturer and an MPN",
     "select": {"parts": {"needs_mpn": true, "not": {"kind": ["test_point", "fiducial"]}}},
     "assert": [{"has": ["manufacturer", "mpn"]}]},
    {"id": "TB-HV-01", "title": "Capacitors above 24 V are rated at least 2x", "severity": "error",
     "select": {"parts": {"kind": "capacitor", "net_voltage": {"above": 24}}},
     "assert": [{"rating_ratio": {"min": 2}}],
     "message": "{designator} on {net}: rated {rated_voltage} V for {voltage} V ({ratio:.2f}x)"},
    {"id": "TB-TOOL-01", "title": "No user-facing line without ESD", "severity": "error",
     "tool": "check_interfaces", "args": {"checks": ["no_esd"]},
     "fail": [{"list": "findings", "where": {"check": "no_esd", "exposure": "external"},
               "label": "user-facing lines without ESD"}]}
  ]
}
```

- A pack: `id` (lower case), `title`, `version`, optional `description`, `source` and
  `rails` (net voltages its rules assume, which win over the voltages read from net names
  and the power tree), and `rules`. Keys starting with `_` are comments.
- A rule: `id` (unique across packs, ignoring case; a checklist item names it), `title`,
  optional `description`, `severity` (`error`, `warning` (default) or `info`), `message`
  (a template: `{designator}`, `{net}`, `{pin}`, `{pin_name}`, `{value}`, `{footprint}`,
  `{mpn}`, `{case_size}`, `{side}`, `{voltage}`, `{rated_voltage}`, `{ratio}`,
  `{distance_mm}`, `{nearest}`, `{count}`, `{missing}`, `{connectors}` (those its nets
  leave the board through, the user-facing ones first), `{failed}` (what failed, in
  words) ...; `{ratio:.2f}` formats; a value not known shows `?`), `on_unknown` (`skip`,
  the default, counts an object that cannot be evaluated as not evaluated; `fail` makes
  it a finding) and `disabled` (runs only when named in `rules=`).
- Select-and-assert rules: `select` is `{"parts": filter}`, `{"nets": filter}` or
  `{"pins": filter}`; `assert` lists what every selected object must meet.
  - Part filters: `designator`, `value`, `comment`, `description`, `footprint`, `mpn`,
    `manufacturer`, `lib_reference`, `sheet`, `text` (regular expressions, any case),
    `prefix`, `kind` (resistor, capacitor, inductor, ferrite, choke, diode, zener, led,
    tvs (ESD and TVS parts, varistors), ic, connector, transistor, transformer, crystal,
    fuse, switch, relay, test_point, fiducial, mechanical, battery, jumper, thermistor,
    other: check_interfaces' reading of the designator prefix and the part's words),
    `fitted` (default true: fitted in the active variant; `null` for every part),
    `in_bom`, `needs_mpn` (in the BOM, not mechanical, not marked N/A on purpose, as
    `check_bom` reads it), `parameter` (`{"Name": "regex"}`, `true` present, `false`
    missing), `side` (`top` or `bottom`, from the PcbDoc; without one the part is not
    evaluated), `case_size` (`"0402"`, a list, or `{"in", "not_in", "min", "max"}` of
    imperial codes, read from a Case/Package parameter, the text or the footprint name
    of passives and diodes), `net` (a net filter: a part on
    such a net), `net_voltage` (`{"min", "max", "above", "below"}` on the highest
    voltage of its nets, as `check_derating` resolves them) and `not` (a filter the part
    must not match). A part, net or pin on a rail whose voltage neither its name, the
    power tree nor the pack's `rails` gives is counted as not evaluated when that could
    change the answer, wherever the voltage filter sits (a pin's or part's net,
    `has_part`, `not`, an assertion's parts filter), never as in or out of range; signal
    nets of unknown voltage do not count (a part on such nets only is in no range).
  - Net filters: `name`, `class` (ground, rail, supply (a rail, or a net a GPIO switches
    that feeds two or more pull-ups), signal, i2c, open_drain, diff_pair (signals only,
    never a split rail such as V+ and V-), connector, connector_power (a connector's power
    pin), off_board (a line into the board: a connector signal pin whose line reaches an
    IC, a transistor or another active part, the lines `check_interfaces`' no_esd asks
    ESD protection for; not a line into a transformer or another connector, not a LED or
    centre-tap pin), external (the same on a connector `check_interfaces` judges
    user-facing), single_pin), `voltage`, `has_part` (a part filter), `pin_count`, `not`,
    and `one_per_line` (`true`, in the rule's own nets filter: one net of each line, a
    line being the net and the nets joined to it by fitted series resistors under 1 kohm,
    as `check_pull_resistors` joins a signal; a net named by the designer is kept before
    an automatic Net... name).
  - Pin filters: `name`, `number`, `type` (Input, IO, Output, OpenCollector, Passive,
    HiZ, OpenEmitter, Power), `role` (supply, ground, ref, cap, out, in, other, none:
    the pin name's first function), `part` (a part filter), `net` (a net filter),
    `connected` (default true), `not`.
  - Assertions: `has_parameter` (a name or a list, all non-empty; `{"any": [...]}` for
    spellings), `has` (fields: value, comment, description, footprint, mpn, manufacturer,
    lib_reference, sheet, case_size), `parameter_matches` and `matches` (`{"name":
    "regex"}`), `count` (`{"parts": filter, "min", "max", "within_hops", "through",
    "on_line"}`: the parts of a kind on the net, or on the nets joined to it through that
    many series parts; `"on_line": true` starts from every net of its line, so a pull-up
    behind a 0 ohm link or a damping resistor counts), `connected` (`{"parts": filter,
    "within_hops": 1, "through": [...], "on_line"}`: at
    least one such part; series parts are 0 ohm links and resistors, ferrites, inductors,
    common-mode chokes and series capacitors, fitted; rails and grounds are never
    crossed), `nearest` (`{"parts": filter, "max_mm", "same_net": true}`: the nearest
    such part on the PCB, pad edge to pad edge, straight line), `rating_ratio`
    (`{"min": 2}`: a capacitor's rated voltage over the voltage across it, as
    `check_derating` reads both), `side`, `case_size`, and `none` (every selected object
    is a finding). In an assertion's parts filter, `other_end` tests the far side of a
    part found on the net (`"other_end": "ground"` for a capacitor to ground,
    `"supply"` for a pull-up).
- Tool rules: `tool` (a read-only tool a review checklist can call, such as `run_erc`,
  `check_interfaces`, `check_bom`, `check_dfm` or `check_pull_resistors`), `args`, and
  the review checklists' conditions: `fail` (each matching row of a `list` condition is a
  finding with its designators and nets; a `path` condition that holds is one), `pass`
  (all must hold), `na` (the rule does not apply) and `na_errors` (tool errors that mean
  it does not apply, default `no_pcb_document`). The tool runs in-process with the active
  variant, as a direct call would. A count missing from a summary of counts (tools list
  only the checks that found something, `{}` on a clean board) is 0, named in the row's
  `counted_as_zero`; any other field a condition reads that the tool does not give leaves
  the rule could_not_evaluate, never passed (the findings other conditions read still
  count).
- Python plugins: `check_packs/plugins/*.py` in the project folder (never anywhere
  else: a `check_packs` or `plugins` folder linked to another place gives none) each
  define `check(api)`, which returns a list of findings: dicts with
  `message`, optional `severity`, `designators`, `nets`, `pins`, `location` and `check`.
  `api` is the read-only design API (`server/services/design_api.py`: `api.parts`,
  `api.nets`, `api.part("U1")`, `api.net("3V3")`, each part's kind, fitted, parameters,
  pins, case size, side and position, each net's classes, voltage, line and connectors,
  `api.reachable`, `api.pads`, `api.distance_mm` and `api.nearest`). **Plugins run
  repository code with the server's
  permissions**, so they run only when the call passes `allow_plugins=true` **and** the
  server was started with `ALTIUM_COPILOT_ALLOW_PLUGINS=1` in its environment (set it
  where the server is configured, for example the MCP client's `env`); otherwise they are
  listed, never imported. A plugin that raises, calls `sys.exit`, returns something else
  or prints is reported in its rule row; it never stops the server. A plugin has no time
  limit (one that never returns blocks the call) and output it writes past
  `sys.stdout` (`os.write`, `sys.__stdout__`) is not captured: review plugins as you
  review the rest of the repository's code. A pack cannot be named `plugins`.

Results: `verdict` (fail: an error finding; warn; incomplete: a rule could not be
evaluated or left objects not evaluated; pass), `severity_counts`, a summary by pack and
severity, one row per rule (status pass, fail, not_applicable, could_not_evaluate with
the reason, objects checked and not evaluated; every rule keeps its row on every page,
shortened when a page must shrink) and the findings, errors first, paginated (the run is
reused for the next pages for 5 minutes while the project folder and the packs are
unchanged). Each pack file is checked
like a checklist override: an unknown key, kind, class, assertion, message field, tool or
argument, a bad regular expression or bad JSON is reported with the file and the place
in it, and nothing runs until it is fixed. Run on Altium's public examples, the example
pack's rules each select what they describe: see the calibration notes in CHANGELOG.

### Offline netlist

With Altium closed and no up-to-date `.NET`, `set_project_dir` compiles the netlist from
the saved sheets (`load_mode: offline_netlist`, `netlist_source: offline_compiler`), and
`refresh_netlist` compiles it again after a save. It follows Altium's compile rules: the
project's Net Identifier Scope (Flat, Global, Hierarchical, Strict Hierarchical, or what
Automatic resolves to), power ports global (local under Strict Hierarchical, and under
Hierarchical when wired to a port), a net label and a power port of one name joined on a
sheet, sheet entries and ports, off-sheet connectors, buses as their own layer (a wire or
a junction never joins one) with ranged names (`D[0..7]`) standing for their members along
the bus's wiring, junctions and pin ends within 3 mil of a wire, signal harnesses joined entry by entry,
multi-part components across sheets, compile masks, hidden pins only through a legacy
hidden net name, sheets placed several times or by `Repeat(...)` compiled once per channel
with the `.Annotation` file's designators (else the project's multi-channel format) and
channel-suffixed local net names, and the netlist naming options (power port priority,
ports and sheet entries naming nets, higher-level names first, `Net<designator>_<pin>`
otherwise). The result has the shape of Altium's netlist, so every tool reads it the same
way; `compile_warnings` in the load response lists what the files cannot settle
(duplicate designators or net names, unconnected sheet entries, unvalidated naming and
`Repeat(...)` entries).

Validated against Altium's own output: 4 private projects' `.NET` files match exactly
(208 of 208 nets with the same members and names, 235 of 235 parts with the same
footprint and comment); on Altium's 5 public AD25 example boards every pin lands on the
PcbDoc's net and all 1,274 nets carry the PCB's name; on 63 private projects 62 match their
PcbDoc's pad nets on every pin, 13,623 of 13,639 pins agree (the other 16 are on a board
whose PCB lacks 30 of the sheets' parts) and net names agree except where the PCB renamed
a net. A project of 46 sheets compiles in about a second. Not validated (no sample):
`Repeat(...)` channel designators and member distribution, names of harness nets no label
names, off-sheet connectors, and Append Sheet Numbers to Local Nets (reported, not applied).
No ERC runs offline: open the project in Altium for its compile messages.

---

## Requirements

- Windows
- [Altium Designer](https://www.altium.com/altium-designer) — running with the project open and a schematic sheet active whenever the netlist has to be generated: the first time a project is loaded and after schematic changes
  - A project whose netlist is still up to date loads with Altium closed; the board, rule, DRC report, BOM and notes tools only read the saved files
  - Without Altium and without an up-to-date netlist (for example a fresh copy of an Altium 365 project, which never contains `Project Outputs`), the copilot compiles the netlist from the saved sheets and every tool works (see [Offline netlist](#offline-netlist)). Only when no sheet can be compiled does the project load partially: the board, rule, length, differential-pair, via, connectivity, health, DFM, DRC report, notes, picture and variant tools work, and the netlist tools ask for the project to be opened in Altium once
  - Altium Designer 17 and older (`DXP.exe`) are detected too
- [Git](https://git-scm.com/download/win) (optional) — only to report where an Altium 365 project's local clone stands; without it everything else works
- [Claude Desktop](https://claude.com/download) or [Claude Code](https://claude.com/product/claude-code) — works with Pro and Enterprise subscriptions

---

## Install

1. Install [Claude Desktop](https://claude.com/download) or [Claude Code](https://claude.com/product/claude-code)
2. Open **PowerShell** (search `powershell` in the Start menu) and run:
   ```powershell
   irm https://raw.githubusercontent.com/kcisek/altium-copilot-kc/main/install.ps1 | iex
   ```
3. Open Altium Designer with your project loaded
4. Start asking
   
---

## Usage

Once installed, open Claude and ask anything about your schematic — the copilot detects your running Altium instance and orients itself to your project automatically. With Altium closed, it offers the projects you loaded before. It asks which variant to work in, and every answer follows that variant's fitted and Not Fitted parts.

After you save schematic changes in Altium, tell Claude: it calls `refresh_netlist` to pick them up. Board changes are picked up on the next question after you save the `.PcbDoc`.

**Example prompts:**
- *"What's connected to the 3.3V net?"*
- *"What's U4 doing on this page?"*
- *"What's different between my schematic variants?"*
- *"How should i improve this circuit?"*
- *"Is this power supply circuit correct?"* — triggers a structured design review automatically
- *"Is the PCB in sync with the schematic?"*
- *"Give me the BOM for this variant as CSV"*

---

## Privacy

Your data stays yours. The MCP server runs locally and sends no telemetry: its only network request is a check for a new release on GitHub, at most once a day, and it carries no project data. For Altium 365 projects it runs local `git` commands in the project's clone, which are blocked from contacting any server. It keeps the list of projects you loaded in `%USERPROFILE%\.ee-in-a-box\`, and its warnings go to your MCP client's log. All schematic data is sent only to Claude via your own Enterprise or Pro subscription.

---

## 🚀 NEW: Sharing with your team

Want to share the same capabilities with firmware, mechanical, and test engineers without them needing an Altium license?

Ask Claude to package your project:

> *"Package this Altium project to share with the cross functional team"* / *"Export this Altium project for sharing"*

Claude calls `package_for_xfn`, which writes a portable SQLite snapshot (`<project>-pcb-copilot.db`) next to your `.PrjPcb`. Share that file with your team via Confluence, Slack, or a shared drive.

The snapshot holds the schematic side: sheets, variants, components, nets, pins and the design notes (pictures as metadata only, and file names instead of local folders), plus the firmware pin map of each MCU, radio or module `get_firmware_pin_map` finds fitted in the active variant (at most four, else its top candidate; the `firmware_pins` table: a heuristic starting point). Board, rule, BOM and DRC data are not in it.

Your teammates open it with **[pcb-copilot](https://github.com/ee-in-a-box/pcb-copilot)** — a companion MCP server that gives Claude the same schematic Q&A tools against the snapshot, no Altium license required. Pairs well with the free [Altium 365 Viewer](https://www.altium.com/altium-365/viewer) for visual reference alongside the AI Q&A.

---

## Give Claude system-level context

The schematic tools tell Claude what's in your design. A `CLAUDE.md` in your project folder tells it *why*.

Drop a `CLAUDE.md` file in the same folder as your `.PrjPcb` file — it's loaded automatically when the project opens. Put in what the schematic can't tell Claude:

- What the board does and where it lives in the system
- What it interfaces with upstream and downstream
- Terms that look similar but mean different things (board vs. assembly, vendor naming vs. internal naming)
- Key requirements — voltage rails, power budget, timing constraints, isolation barriers
- Pointers to your requirements doc, Confluence page, or block diagram

**Don't have time to write one?** Download the [template](examples/CLAUDE.md), upload it to Claude, and say: *"Use this template and what you can read from my schematic to draft a CLAUDE.md — then ask me one question at a time for anything you can't figure out from the netlist."*

---

## What i'm experimenting with next

- **Simulator** — walks you through simulating and validating your design before you spin a board
- **Librarian** — periodic audits of your component libraries, flags stale or discontinued entries
- **Supply Chain Expert** — component availability, lead times, alternatives, and sourcing risk

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

For bug reports and support, open an issue at [github.com/ee-in-a-box/altium-copilot/issues](https://github.com/ee-in-a-box/altium-copilot/issues).

---

## License

MIT © Maisam Pyarali
