# parser/nangateMap.py
# Nangate OpenCellLibrary → GateType mapping + AOI/OAI decomposition.
# Pin names verified against NangateOpenCellLibrary.blackbox.v.

from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple

_DRIVE_RE = re.compile(r"_X\d+$")

def _base(cell_type: str) -> str:
    return _DRIVE_RE.sub("", cell_type)

# ── Simple cells ──────────────────────────────────────────────────────────────
# (gtype_name, output_pin, [input_pins])

_SIMPLE: Dict[str, Tuple[str, str, List[str]]] = {
    "INV":    ("NOT",  "ZN", ["A"]),
    "BUF":    ("BUFF", "Z",  ["A"]),
    "CLKBUF": ("BUFF", "Z",  ["A"]),
    "AND2":   ("AND",  "ZN", ["A1", "A2"]),
    "AND3":   ("AND",  "ZN", ["A1", "A2", "A3"]),
    "AND4":   ("AND",  "ZN", ["A1", "A2", "A3", "A4"]),
    "NAND2":  ("NAND", "ZN", ["A1", "A2"]),
    "NAND3":  ("NAND", "ZN", ["A1", "A2", "A3"]),
    "NAND4":  ("NAND", "ZN", ["A1", "A2", "A3", "A4"]),
    "OR2":    ("OR",   "ZN", ["A1", "A2"]),
    "OR3":    ("OR",   "ZN", ["A1", "A2", "A3"]),
    "OR4":    ("OR",   "ZN", ["A1", "A2", "A3", "A4"]),
    "NOR2":   ("NOR",  "ZN", ["A1", "A2"]),
    "NOR3":   ("NOR",  "ZN", ["A1", "A2", "A3"]),
    "NOR4":   ("NOR",  "ZN", ["A1", "A2", "A3", "A4"]),
    "XOR2":   ("XOR",  "Z",  ["A", "B"]),
    "XNOR2":  ("XNOR", "ZN", ["A", "B"]),
}

# ── Cells to silently skip ────────────────────────────────────────────────────

_SKIP_CELLS = frozenset({
    # Sequential
    "DFF", "DFFR", "DFFS", "DFFRS",
    "SDFF", "SDFFR", "SDFFS", "SDFFRS",
    "DLH", "DLL", "DLATCH",
    # Clock gating
    "CLKGATE", "CLKGATETST",
    # Filler / tie / tri-state
    "FILLCELL", "LOGIC0", "LOGIC1", "TIEHI", "TIELO",
    "TBUF", "TINV", "TLAT",
    # Multi-output (too complex for single-output gate model)
    "FA", "HA",
    # Antenna (no logic)
    "ANTENNA",
})


def resolve_simple(cell_type: str):
    """Return (gtype_name, output_pin, input_pins) or None."""
    base = _base(cell_type)
    if base in _SKIP_CELLS:
        return None
    return _SIMPLE.get(base)          # None if not found


def decompose_complex(
    cell_type:   str,
    inst_name:   str,
    connections: Dict[str, str],      # pin_name → resolved net name
) -> Optional[List[Tuple[str, str, List[str]]]]:
    """
    Decompose a complex Nangate cell into
    [(out_net, gtype_name, [in_nets]), ...] primitives.
    Pin names are verified against NangateOpenCellLibrary.blackbox.v.
    Returns None if unrecognised.
    """
    base = _base(cell_type)
    pfx  = f"__ng_{inst_name}"

    def net(tag: str) -> str:
        return f"{pfx}_{tag}"

    def pin(p: str) -> Optional[str]:
        # Safe lookup — returns None if pin absent (e.g. unconnected constant)
        return connections.get(p)

    def req(*pins) -> bool:
        """Return True only if all required pins are present and non-None."""
        return all(connections.get(p) is not None for p in pins)

    # ── AOI21:  ZN = NOT( (B1 & B2) | A ) ──────────────────────────────────
    # Ports: A, B1, B2, ZN
    if base == "AOI21":
        if not req("A", "B1", "B2", "ZN"): return None
        return [
            (net("and"), "AND", [pin("B1"), pin("B2")]),
            (net("or"),  "OR",  [net("and"), pin("A")]),
            (pin("ZN"),  "NOT", [net("or")]),
        ]

    # ── AOI22:  ZN = NOT( (A1 & A2) | (B1 & B2) ) ──────────────────────────
    # Ports: A1, A2, B1, B2, ZN
    if base == "AOI22":
        if not req("A1", "A2", "B1", "B2", "ZN"): return None
        return [
            (net("and_a"), "AND", [pin("A1"), pin("A2")]),
            (net("and_b"), "AND", [pin("B1"), pin("B2")]),
            (net("or"),    "OR",  [net("and_a"), net("and_b")]),
            (pin("ZN"),    "NOT", [net("or")]),
        ]

    # ── AOI211:  ZN = NOT( (C1 & C2) | B | A ) ─────────────────────────────
    # Ports: A, B, C1, C2, ZN
    if base == "AOI211":
        if not req("A", "B", "C1", "C2", "ZN"): return None
        return [
            (net("and"),  "AND", [pin("C1"), pin("C2")]),
            (net("or1"),  "OR",  [net("and"),  pin("B")]),
            (net("or2"),  "OR",  [net("or1"),  pin("A")]),
            (pin("ZN"),   "NOT", [net("or2")]),
        ]

    # ── AOI221:  ZN = NOT( (B1 & B2) | (C1 & C2) | A ) ─────────────────────
    # Ports: A, B1, B2, C1, C2, ZN
    if base == "AOI221":
        if not req("A", "B1", "B2", "C1", "C2", "ZN"): return None
        return [
            (net("and_b"), "AND", [pin("B1"), pin("B2")]),
            (net("and_c"), "AND", [pin("C1"), pin("C2")]),
            (net("or1"),   "OR",  [net("and_b"), net("and_c")]),
            (net("or2"),   "OR",  [net("or1"),   pin("A")]),
            (pin("ZN"),    "NOT", [net("or2")]),
        ]

    # ── AOI222:  ZN = NOT( (A1&A2) | (B1&B2) | (C1&C2) ) ───────────────────
    # Ports: A1, A2, B1, B2, C1, C2, ZN
    if base == "AOI222":
        if not req("A1", "A2", "B1", "B2", "C1", "C2", "ZN"): return None
        return [
            (net("and_a"), "AND", [pin("A1"), pin("A2")]),
            (net("and_b"), "AND", [pin("B1"), pin("B2")]),
            (net("and_c"), "AND", [pin("C1"), pin("C2")]),
            (net("or1"),   "OR",  [net("and_a"), net("and_b")]),
            (net("or2"),   "OR",  [net("or1"),   net("and_c")]),
            (pin("ZN"),    "NOT", [net("or2")]),
        ]

    # ── OAI21:  ZN = NOT( (B1 | B2) & A ) ──────────────────────────────────
    # Ports: A, B1, B2, ZN
    if base == "OAI21":
        if not req("A", "B1", "B2", "ZN"): return None
        return [
            (net("or"),  "OR",  [pin("B1"), pin("B2")]),
            (net("and"), "AND", [net("or"),  pin("A")]),
            (pin("ZN"),  "NOT", [net("and")]),
        ]

    # ── OAI22:  ZN = NOT( (A1 | A2) & (B1 | B2) ) ──────────────────────────
    # Ports: A1, A2, B1, B2, ZN
    if base == "OAI22":
        if not req("A1", "A2", "B1", "B2", "ZN"): return None
        return [
            (net("or_a"), "OR",  [pin("A1"), pin("A2")]),
            (net("or_b"), "OR",  [pin("B1"), pin("B2")]),
            (net("and"),  "AND", [net("or_a"), net("or_b")]),
            (pin("ZN"),   "NOT", [net("and")]),
        ]

    # ── OAI211:  ZN = NOT( (C1 | C2) & B & A ) ─────────────────────────────
    # Ports: A, B, C1, C2, ZN
    if base == "OAI211":
        if not req("A", "B", "C1", "C2", "ZN"): return None
        return [
            (net("or"),   "OR",  [pin("C1"), pin("C2")]),
            (net("and1"), "AND", [net("or"),   pin("B")]),
            (net("and2"), "AND", [net("and1"), pin("A")]),
            (pin("ZN"),   "NOT", [net("and2")]),
        ]

    # ── OAI221:  ZN = NOT( (B1 | B2) & (C1 | C2) & A ) ─────────────────────
    # Ports: A, B1, B2, C1, C2, ZN
    if base == "OAI221":
        if not req("A", "B1", "B2", "C1", "C2", "ZN"): return None
        return [
            (net("or_b"), "OR",  [pin("B1"), pin("B2")]),
            (net("or_c"), "OR",  [pin("C1"), pin("C2")]),
            (net("and1"), "AND", [net("or_b"), net("or_c")]),
            (net("and2"), "AND", [net("and1"), pin("A")]),
            (pin("ZN"),   "NOT", [net("and2")]),
        ]

    # ── OAI222:  ZN = NOT( (A1|A2) & (B1|B2) & (C1|C2) ) ───────────────────
    # Ports: A1, A2, B1, B2, C1, C2, ZN
    if base == "OAI222":
        if not req("A1", "A2", "B1", "B2", "C1", "C2", "ZN"): return None
        return [
            (net("or_a"), "OR",  [pin("A1"), pin("A2")]),
            (net("or_b"), "OR",  [pin("B1"), pin("B2")]),
            (net("or_c"), "OR",  [pin("C1"), pin("C2")]),
            (net("and1"), "AND", [net("or_a"), net("or_b")]),
            (net("and2"), "AND", [net("and1"), net("or_c")]),
            (pin("ZN"),   "NOT", [net("and2")]),
        ]

    # ── OAI33:  ZN = NOT( (A1|A2|A3) & (B1|B2|B3) ) ────────────────────────
    # Ports: A1, A2, A3, B1, B2, B3, ZN
    if base == "OAI33":
        if not req("A1", "A2", "A3", "B1", "B2", "B3", "ZN"): return None
        return [
            (net("or_a"), "OR",  [pin("A1"), pin("A2"), pin("A3")]),
            (net("or_b"), "OR",  [pin("B1"), pin("B2"), pin("B3")]),
            (net("and"),  "AND", [net("or_a"), net("or_b")]),
            (pin("ZN"),   "NOT", [net("and")]),
        ]

    # ── MUX2:  Z = S ? B : A  → (A & !S) | (B & S) ─────────────────────────
    # Ports: A, B, S, Z
    if base == "MUX2":
        if not req("A", "B", "S", "Z"): return None
        return [
            (net("not_s"), "NOT", [pin("S")]),
            (net("and_a"), "AND", [pin("A"), net("not_s")]),
            (net("and_b"), "AND", [pin("B"), pin("S")]),
            (pin("Z"),     "OR",  [net("and_a"), net("and_b")]),
        ]

    return None   # unrecognised — caller emits warning