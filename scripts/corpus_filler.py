"""Deterministic filler corpus.

Rules this module must respect (enforced by tests/test_corpus.py):
  * Never contradict a planted fact F1-F8.
  * Never introduce an alarm code near E-417. The only neighbour allowed in the
    whole corpus is E-471, which is planted on purpose in MAN-CNC-04. So filler
    uses no E-4xx code at all.
  * Never mention a price, a supplier status or deviation content - those live
    only in the restricted documents.
All filler is all-roles and line-scoped.
"""
from __future__ import annotations

import random

from corpus_docs import ALL, doc

SEED = 20260801

# Alarm / fault codes that are safe to reuse in filler: nothing in the E-4xx
# band, so no near-duplicate of E-417 or E-471 can be created by accident.
SAFE_CODES = [
    "E-105", "E-118", "E-212", "E-237", "E-308", "E-321", "E-526", "E-533",
    "E-630", "E-651", "E-744", "E-758", "E-801", "E-815",
    "W-210", "W-233", "W-307", "M-110", "M-145",
]

# (line, doc_id, title, topic, equipment)
FILLER_SPECS = [
    # ---- Line 1 -----------------------------------------------------------
    ("L1", "SOP-L1-005", "Line 1 CNC: Daily Machine Start-Up", "startup",
     "CNC-L1-01 through CNC-L1-06"),
    ("L1", "SOP-L1-006", "Line 1 CNC: Chip Conveyor and Swarf Disposal", "swarf",
     "CNC-L1-01 through CNC-L1-06"),
    ("L1", "SOP-L1-007", "Line 1 CNC: Tool Crib Issue and Return", "toolcrib",
     "Line 1 tool crib"),
    ("L1", "SOP-L1-008", "Line 1 CNC: Way Lubrication Check", "waylube",
     "CNC-L1-01 through CNC-L1-06"),
    ("L1", "SOP-L1-009", "Line 1 CNC: Probe Calibration", "probe",
     "CNC-L1-03, CNC-L1-05"),
    ("L1", "SOP-L1-010", "Line 1 CNC: Spindle Warm-Up Cycle", "warmup",
     "CNC-L1-01 through CNC-L1-06"),
    ("L1", "SOP-L1-011", "Line 1 CNC: Shift Handover", "handover", "Line 1"),
    ("L1", "SOP-L1-012", "Line 1 CNC: Deburring and Part Presentation", "deburr",
     "Line 1 deburr bench"),
    ("L1", "WI-L1-021", "Line 1 Work Instruction: Fixture Gauge Check", "gaugecheck",
     "Line 1 fixtures"),
    ("L1", "WI-L1-022", "Line 1 Work Instruction: Air Blow-Off Discipline", "blowoff",
     "Line 1"),
    # ---- Line 2 -----------------------------------------------------------
    ("L2", "SOP-L2-013", "Line 2: Thermocouple Replacement", "thermocouple",
     "FUR-L2-01, FUR-L2-02"),
    ("L2", "SOP-L2-014", "Line 2: Atmosphere Control and Carbon Potential", "atmosphere",
     "FUR-L2-01, FUR-L2-02"),
    ("L2", "SOP-L2-015", "Line 2: Basket and Fixture Inspection", "basket",
     "Line 2 fixtures"),
    ("L2", "SOP-L2-016", "Line 2: Post-Quench Hardness Check", "hardness",
     "Line 2 hardness bench"),
    ("L2", "SOP-L2-017", "Line 2: Furnace Shutdown and Cool-Down", "shutdown",
     "FUR-L2-01, FUR-L2-02"),
    ("L2", "SOP-L2-018", "Line 2: Tempering Cycle Control", "temper",
     "Line 2 temper oven"),
    ("L2", "SOP-L2-019", "Line 2: Batch Record Completion", "batchrecord", "Line 2"),
    ("L2", "SOP-L2-020", "Line 2: Shift Handover", "handover", "Line 2"),
    ("L2", "WI-L2-031", "Line 2 Work Instruction: Charge Fork Handling", "chargefork",
     "Line 2"),
    ("L2", "WI-L2-032", "Line 2 Work Instruction: Oxygen Probe Cleaning", "oxyprobe",
     "FUR-L2-01, FUR-L2-02"),
    # ---- Line 3 -----------------------------------------------------------
    ("L3", "SOP-L3-008", "Line 3 Assembly: Serial Number Marking", "marking",
     "Line 3 marking station"),
    ("L3", "SOP-L3-009", "Line 3 Assembly: Seal Fitting", "seal", "PRS-L3-01"),
    ("L3", "SOP-L3-010", "Line 3 Assembly: Rework Bench Routing", "rework",
     "Line 3 rework bench"),
    ("L3", "SOP-L3-011", "Line 3 Assembly: Final Visual Inspection", "visual",
     "Line 3 inspection bench"),
    ("L3", "SOP-L3-012", "Line 3 Assembly: Packing and Despatch Presentation", "packing",
     "Line 3 packing"),
    ("L3", "SOP-L3-013", "Line 3 Assembly: Torque Wrench Calibration Handling", "wrenchcal",
     "Line 3 torque station"),
    ("L3", "SOP-L3-014", "Line 3 Assembly: Shift Handover", "handover", "Line 3"),
    ("L3", "SOP-L3-015", "Line 3 Assembly: Station Clearance Between Part Numbers", "clearance",
     "Line 3"),
    ("L3", "WI-L3-041", "Line 3 Work Instruction: Red Tag Discipline", "redtag", "Line 3"),
    ("L3", "WI-L3-042", "Line 3 Work Instruction: Master Part Storage", "masterpart",
     "LKT-L3-01"),
    # ---- Plant-wide -------------------------------------------------------
    ("plant", "SOP-GEN-001", "Plant: Lockout and Tagout", "loto", "Whole plant"),
    ("plant", "SOP-GEN-002", "Plant: Personal Protective Equipment", "ppe", "Whole plant"),
    ("plant", "SOP-GEN-003", "Plant: Raising a Maintenance Call", "maintcall", "Whole plant"),
    ("plant", "SOP-GEN-004", "Plant: Gauge and Instrument Calibration Control", "calcontrol",
     "Whole plant"),
    ("plant", "SOP-GEN-005", "Plant: Housekeeping and 5S Standard", "fives", "Whole plant"),
    ("plant", "SOP-GEN-006", "Plant: Compressed Air System Checks", "air", "Whole plant"),
    ("plant", "SOP-GEN-007", "Plant: Waste Segregation", "waste", "Whole plant"),
    ("plant", "SOP-GEN-008", "Plant: Internal Training and Competence Records", "training",
     "Whole plant"),
    ("plant", "SOP-GEN-009", "Plant: Document Control and Revision Handling", "doccontrol",
     "Whole plant"),
    ("plant", "SOP-GEN-010", "Plant: Escalation of Production Stoppages", "escalation",
     "Whole plant"),
]

PURPOSE = {
    "startup": "To bring a Line 1 machining centre from cold to production-ready in a repeatable order so that the first part of the shift is in tolerance.",
    "swarf": "To keep swarf clear of the work zone and the conveyor so that chip packing does not cause an axis fault or a dimensional error.",
    "toolcrib": "To control the issue and return of cutting tools so that a tool of unknown condition never reaches a machine.",
    "waylube": "To confirm the way lubrication system is delivering lubricant to every slide before production starts.",
    "probe": "To calibrate the touch probe so that in-process measurement can be trusted.",
    "warmup": "To bring the spindle to thermal equilibrium before precision work, so that bore sizes do not drift through the first hour.",
    "handover": "To pass on the state of the line at shift change so that the incoming shift does not repeat work or miss a held batch.",
    "deburr": "To present parts to inspection and assembly free of burrs and sharp edges without altering a controlled dimension.",
    "gaugecheck": "To confirm fixtures still locate parts within the designed tolerance.",
    "blowoff": "To control the use of compressed air for cleaning so that swarf is not blown into guards, slides or neighbouring stations.",
    "thermocouple": "To replace a load or control thermocouple without compromising the integrity of the temperature record.",
    "atmosphere": "To control furnace atmosphere and carbon potential so that case depth and surface carbon meet specification.",
    "basket": "To inspect baskets and fixtures for distortion and cracking before they carry a load.",
    "hardness": "To check hardness after quench and temper so that a non-conforming batch is caught before despatch.",
    "shutdown": "To shut a furnace down and cool it in a controlled way that protects the refractory and the heating elements.",
    "temper": "To control the tempering cycle so that hardness and toughness both meet specification.",
    "batchrecord": "To complete the batch record so that every load is traceable to its material, its cycle and its operator.",
    "chargefork": "To handle the charge fork safely when loading and unloading a hot furnace.",
    "oxyprobe": "To clean and verify the oxygen probe so that the atmosphere reading can be trusted.",
    "marking": "To mark serial numbers legibly and in the correct position without damaging the part.",
    "seal": "To fit seals without rolling, nicking or stretching them, because a damaged seal is the most common cause of a leak test failure.",
    "rework": "To route a failed assembly to rework with the information the rework bench needs.",
    "visual": "To carry out the final visual inspection against a defined standard rather than personal judgement.",
    "packing": "To pack finished assemblies so that they reach the customer undamaged and correctly identified.",
    "wrenchcal": "To manage torque wrench calibration status at the station so that an out-of-calibration wrench is never used on a production joint.",
    "clearance": "To clear a station of the outgoing part number before the incoming part number starts, so that parts cannot be mixed.",
    "redtag": "To control non-conforming parts with a red tag so that they cannot re-enter the flow.",
    "masterpart": "To store and control leak test master parts so that the daily tester check stays meaningful.",
    "loto": "To isolate equipment from every energy source before anyone works inside a guard.",
    "ppe": "To define the personal protective equipment required in each area of the plant.",
    "maintcall": "To raise a maintenance call with enough information for maintenance to arrive prepared.",
    "calcontrol": "To control the calibration status of gauges and instruments across the plant.",
    "fives": "To define the housekeeping standard expected at every station.",
    "air": "To check the compressed air system so that pressure, moisture and oil carry-over stay within limits.",
    "waste": "To segregate waste streams correctly, including controlled wastes such as used quench oil and spent coolant.",
    "training": "To record competence so that nobody operates equipment they have not been signed off on.",
    "doccontrol": "To control document revisions so that only the current version of a procedure is in use at a station.",
    "escalation": "To escalate a production stoppage to the right person within a defined time.",
}

TABLES = {
    "startup": ("Step", "Check", [
        ("1", "Main isolator on, control booted without fault"),
        ("2", "Air pressure at the machine above 5.5 bar"),
        ("3", "Coolant level above the minimum sight-glass mark"),
        ("4", "Way lubrication reservoir above minimum"),
        ("5", "Guards in place, interlocks healthy"),
        ("6", "Reference all axes, confirm home position"),
        ("7", "Spindle warm-up cycle per SOP-L1-010"),
    ]),
    "handover": ("Item", "What to pass on", [
        ("Running work order", "Number, part, quantity completed, quantity remaining"),
        ("Held batches", "Work order, reason for hold, who is deciding"),
        ("Open maintenance calls", "Call number, machine, what was reported"),
        ("Consumables", "Anything below reorder point"),
        ("Quality concerns", "Any trend seen during the shift"),
        ("Safety", "Anything that could injure the incoming shift"),
    ]),
    "loto": ("Energy source", "Isolation method", [
        ("Electrical", "Isolator locked off, own padlock, own tag"),
        ("Pneumatic", "Supply valve locked off, downstream vented to zero"),
        ("Hydraulic", "Pump isolated, accumulator bled down"),
        ("Stored mechanical", "Axis or ram blocked or lowered to rest"),
        ("Thermal", "Allowed to cool below 40 C, confirmed with a probe"),
    ]),
    "ppe": ("Area", "Required PPE", [
        ("Line 1 CNC", "Safety glasses, safety shoes, no loose sleeves"),
        ("Line 2 heat treatment", "Face shield, heat-resistant gloves, cotton overalls"),
        ("Line 3 assembly", "Safety glasses, safety shoes, cut-resistant gloves at deburr"),
        ("Quench bay", "Face shield, apron, heat-resistant gauntlets"),
        ("Despatch", "Safety shoes, high-visibility vest"),
    ]),
}

GENERIC_TABLE = ("Parameter", "Specification", [
    ("Frequency", "Every shift unless stated otherwise"),
    ("Recorded on", "Station log, against the work order number"),
    ("Retention", "24 months"),
    ("Authority to deviate", "Line supervisor, in writing"),
    ("Escalation", "Line supervisor, then plant head"),
])


# Synthetic signatories. These exist so the Presidio masking stage has real
# work to do: names, phone numbers, emails and employee numbers must come back
# as placeholders, while 'Arcflow', part numbers and alarm codes must not.
# No resemblance to any real person is intended.
SIGNATORIES = [
    "Anand Krishnan", "Meera Balasubramanian", "Rajesh Iyer", "Divya Shankar",
    "Suresh Mani", "Lakshmi Venkatesh", "Arun Prakash", "Nandini Rao",
    "Ganesh Murthy", "Sripriya Kumar", "Harish Chandran", "Vidya Narayanan",
]


def _body(spec, rng: random.Random) -> str:
    scope, doc_id, title, topic, equip = spec
    purpose = PURPOSE[topic]
    prep, appr = rng.sample(SIGNATORIES, 2)
    prep_phone = f"+91 {rng.randint(70000, 99999)} {rng.randint(10000, 99999)}"
    appr_email = (
        appr.lower().replace(" ", ".") + "@ironvale-components.example"
    )
    prep_id = rng.randint(10000, 99999)
    appr_id = rng.randint(10000, 99999)
    hdr, col2, rows = TABLES.get(topic, GENERIC_TABLE)
    codes = rng.sample(SAFE_CODES, 4)
    ver = f"v{rng.randint(1, 5)}"
    month = rng.randint(1, 10)
    day = rng.randint(1, 28)
    eff = f"2026-{month:02d}-{day:02d}"

    table_md = f"| {hdr} | {col2} |\n|---|---|\n" + "\n".join(
        f"| {a} | {b} |" for a, b in rows
    )
    fault_md = "\n".join(
        f"| {c} | {t} | {a} |"
        for c, t, a in zip(
            codes,
            [
                "Condition detected during the check",
                "Reading outside the control band",
                "Device did not prove within the expected time",
                "Consumable below reorder point",
            ],
            [
                "Correct the condition, then reset",
                "Stop and raise a maintenance call",
                "Investigate the first-out device",
                "Replenish and record on the station log",
            ],
        )
    )

    return f"""# {doc_id} - {title}

| Field | Value |
|---|---|
| Document ID | {doc_id} |
| Version | {ver} |
| Effective date | {eff} |
| Owner | Manufacturing Engineering, Hosur plant |
| Applies to | {equip} |

## 1. Purpose

{purpose}

## 2. Responsibilities

| Role | Responsibility |
|---|---|
| Operator | Carries out the steps in section 4 and records the result |
| Line supervisor | Confirms the record is complete before the shift closes |
| Maintenance | Attends any call raised under section 5 |
| Quality Engineering | Reviews records during internal audit |

## 3. Requirements

{table_md}

## 4. Procedure

1. Confirm the station is clear and the work order is the one on the schedule.
2. Confirm you are working to the current revision of this procedure. If the
   station copy does not match the document control register, stop and get the
   current copy.
3. Carry out the checks in section 3 in the order listed.
4. Record the result on the station log, with actual readings where a reading
   is taken, not just a tick.
5. Where a check fails, apply section 5 before continuing.
6. Sign the record and hand over any open item at shift change.

## 5. If a check fails

| Code | Condition | Action |
|---|---|---|
{fault_md}

A failed check does not authorise a workaround. Where this procedure and a
machine manual disagree, stop and ask the line supervisor; do not choose
between them yourself.

## 6. Related documents

This procedure sits alongside the other procedures for {equip}. Values that are
controlled elsewhere are not repeated here: torque values are controlled by
SOP-L3-007, soak times by SOP-L2-011, and alarm meanings by the relevant
equipment manual. Where this procedure refers to such a value, the controlling
document is the authority.

## 7. Records

Records are retained for 24 months and are available for internal and customer
audit on request.

## 8. Prepared and approved

| Role | Name | Contact | Employee number |
|---|---|---|---|
| Prepared by | {prep} | {prep_phone} | EMP-{prep_id} |
| Approved by | {appr} | {appr_email} | EMP-{appr_id} |

Queries about this procedure go to {prep} on {prep_phone}.
"""


def build_filler() -> None:
    rng = random.Random(SEED)
    for spec in FILLER_SPECS:
        scope, doc_id, title, topic, equip = spec
        body = _body(spec, rng)
        # Spread formats so every parser gets filler volume too.
        h = sum(ord(c) for c in doc_id)
        fmt = ["md", "md", "md", "docx", "html"][h % 5]
        doc(
            doc_id=doc_id,
            title=title,
            fmt=fmt,
            access=ALL,
            scope=scope,
            version="v1",
            effective_date="2026-06-01",
            body=body,
            filler=True,
        )


build_filler()
