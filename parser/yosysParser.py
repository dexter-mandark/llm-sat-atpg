# yosysParser.py
# Yosys JSON netlist -> ParsedNetlist
#
# Handles, in this order of preference:
#   • Yosys internal primitives      ($_AND_, $_NOT_, $_ANDNOT_, $_MUX_, ...)
#   • Yosys word-level operators     ($and, $or, $xor, $not — expanded per bit)
#   • Nangate OpenCellLibrary cells  (AND2_X1, INV_X4, ... via nangateMap)
#   • Nangate complex cells          (AOI21_X1, OAI22_X1, MUX2_X1 — decomposed)
#   • Flip-flops                     (cut, so combinational ATPG can run)
#
# A cell type that matches none of the above is reported loudly rather than
# quietly mapped to UNKNOWN: an unknown gate function makes every downstream
# ATPG result meaningless (see CNFEncoder.encode_gate).

from __future__ import annotations

import warnings
from typing import Dict, List, Optional, Sequence, Tuple

from .nangateMap import decompose_complex, resolve_simple
from .yosysModels import (
    BitID,
    Gate,
    GateType,
    ParsedNetlist,
    PortDirection,
    YosysCell,
    YosysModule,
    YosysNetlist,
)

# Signal names used for literal 0 / 1 tie-offs.
CONST0_SIGNAL = "CONST_0"
CONST1_SIGNAL = "CONST_1"


# -------------------------------------------------------------------------
#  Cell-type tables
# -------------------------------------------------------------------------

# Yosys single-bit internal primitives. Output pin is always Y.
CELL_TYPE_MAP: Dict[str, GateType] = {
    "$_AND_":    GateType.AND,
    "$_OR_":     GateType.OR,
    "$_NOT_":    GateType.NOT,
    "$_NAND_":   GateType.NAND,
    "$_NOR_":    GateType.NOR,
    "$_XOR_":    GateType.XOR,
    "$_XNOR_":   GateType.XNOR,
    "$_BUF_":    GateType.BUFF,
    "$_ANDNOT_": GateType.ANDNOT,
    "$_ORNOT_":  GateType.ORNOT,
}

# Yosys word-level operators. These may be wider than one bit, so each output
# bit becomes its own gate driven by the matching input bits.
WORD_OP_MAP: Dict[str, GateType] = {
    "$and":  GateType.AND,
    "$or":   GateType.OR,
    "$xor":  GateType.XOR,
    "$xnor": GateType.XNOR,
    "$not":  GateType.NOT,
    "$buf":  GateType.BUFF,
    "$pos":  GateType.BUFF,
}

# Reduction operators: N input bits collapse to a single output bit.
REDUCE_OP_MAP: Dict[str, GateType] = {
    "$reduce_and":  GateType.AND,
    "$reduce_or":   GateType.OR,
    "$reduce_xor":  GateType.XOR,
    "$reduce_xnor": GateType.XNOR,
    "$reduce_bool": GateType.OR,
}

# Flip-flops and latches. These are cut rather than encoded — see _cut_flipflop.
SEQUENTIAL_CELLS = frozenset({
    "$dff", "$adff", "$sdff", "$dffe", "$adffe", "$sdffe", "$dlatch", "$adlatch",
    "$_DFF_P_", "$_DFF_N_",
    "$_DFF_PP0_", "$_DFF_PP1_", "$_DFF_PN0_", "$_DFF_PN1_",
    "$_DFF_NP0_", "$_DFF_NP1_", "$_DFF_NN0_", "$_DFF_NN1_",
    "$_DFFE_PP_", "$_DFFE_PN_", "$_DFFE_NP_", "$_DFFE_NN_",
    "$_SDFF_PP0_", "$_SDFF_PP1_", "$_SDFF_NP0_", "$_SDFF_NP1_",
    "$_DLATCH_P_", "$_DLATCH_N_",
    "DFF_X1", "DFF_X2", "DFFR_X1", "DFFR_X2", "DFFS_X1", "DFFS_X2",
    "DFFRS_X1", "DFFRS_X2", "SDFF_X1", "SDFF_X2",
    "DLH_X1", "DLH_X2", "DLL_X1", "DLL_X2",
})

# Cells carrying no logic at all — dropped silently.
INERT_CELLS = frozenset({
    "$initstate", "$assert", "$assume", "$cover", "$live", "$fair", "$check",
    "$scopeinfo", "$print",
    "FILLCELL_X1", "FILLCELL_X2", "FILLCELL_X4",
    "FILLCELL_X8", "FILLCELL_X16", "FILLCELL_X32",
    "ANTENNA_X1", "TIEHI", "TIELO",
})

# Output pin names, used when a cell declares no port_directions.
_OUTPUT_PORT_NAMES = ("Y", "ZN", "Z", "Q", "QN", "S", "CO")

# Input pin order for Yosys primitives that care about operand order.
_PRIMITIVE_INPUT_PINS = ("A", "B", "C", "D", "E", "F", "S")


# -------------------------------------------------------------------------
#  Net-name resolution
# -------------------------------------------------------------------------

def _bit_name(base: str, index: int, width: int) -> str:
    """Name one bit of a signal. Single-bit signals keep their bare name."""
    return base if width == 1 else f"{base}[{index}]"


def _build_net_index(module: YosysModule) -> Dict[int, str]:
    """
    Build { bit_id: signal_name }.

    Priority, highest first:
      1. Primary port names (first declaration wins, so an input/output pair
         sharing a bit resolves to the input and the output becomes an alias)
      2. Netnames with hide_name == 0  (user-visible internal names)
      3. Netnames with hide_name == 1  (Yosys auto-generated, last resort)

    Multi-bit ports and nets are expanded, so `data` of width 4 yields
    data[0]..data[3] rather than collapsing four distinct wires into one.
    """
    index: Dict[int, str] = {}

    for port_name, port in module.ports.items():
        width = len(port.bits)
        for i, bit in enumerate(port.bits):
            if isinstance(bit, int):
                index.setdefault(bit, _bit_name(port_name, i, width))

    for hidden in (False, True):
        for net_name, net in module.netnames.items():
            if net.is_hidden is not hidden:
                continue
            width = len(net.bits)
            for i, bit in enumerate(net.bits):
                if isinstance(bit, int):
                    index.setdefault(bit, _bit_name(net_name, i, width))

    return index


def _resolve_bit(bit: BitID, index: Dict[int, str], consts: set) -> str:
    """
    Convert one BitID to a signal name.

    Integers look up in the net index; a literal "0"/"1" becomes a constant
    tie-off signal. Yosys also emits "x" (don't-care) and "z" (high-impedance)
    for unconnected pins; both are pinned to 0 so they never become free
    variables, and the caller is warned.
    """
    if isinstance(bit, int):
        return index.get(bit, f"net_{bit}")

    token = str(bit).lower()
    if token == "1":
        consts.add(CONST1_SIGNAL)
        return CONST1_SIGNAL
    if token == "0":
        consts.add(CONST0_SIGNAL)
        return CONST0_SIGNAL

    consts.add(CONST0_SIGNAL)
    consts.add(f"__undriven__{token}")
    return CONST0_SIGNAL


# -------------------------------------------------------------------------
#  Cell pin access
# -------------------------------------------------------------------------

def _cell_output_pin(cell: YosysCell) -> Optional[str]:
    """Name of the cell's output pin, from port_directions or the name heuristic."""
    for pin, direction in cell.port_directions.items():
        if direction == PortDirection.OUTPUT and pin in cell.connections:
            return pin
    for pin in _OUTPUT_PORT_NAMES:
        if pin in cell.connections:
            return pin
    return None


def _ordered_input_pins(cell: YosysCell, output_pin: str) -> List[str]:
    """
    Input pins in operand order.

    Named pins (A, B, C, ...) come first in that fixed order because gates like
    ANDNOT are not symmetric; anything left over is appended in declaration
    order. Relying on dict order alone silently swapped operands.
    """
    pins = [p for p in _PRIMITIVE_INPUT_PINS if p in cell.connections and p != output_pin]
    pins += [
        p for p in cell.connections
        if p != output_pin and p not in pins
        and cell.port_directions.get(p) != PortDirection.OUTPUT
    ]
    return pins


# -------------------------------------------------------------------------
#  Cell -> gate conversion
# -------------------------------------------------------------------------

class _GateCollector:
    """Accumulates gates while parsing cells, and records what it could not map."""

    def __init__(self, net_index: Dict[int, str]) -> None:
        self.net_index = net_index
        self.gates: Dict[str, Gate] = {}
        self.consts: set = set()
        self.unknown_cells: List[str] = []
        self.cut_flipflops: List[Tuple[str, str]] = []   # (q_signal, d_signal)

    # -- helpers

    def sig(self, bit: BitID) -> str:
        return _resolve_bit(bit, self.net_index, self.consts)

    def sigs(self, bits: Sequence[BitID]) -> List[str]:
        return [self.sig(b) for b in bits]

    def add(self, name: str, gtype: GateType, inputs: List[str]) -> None:
        self.gates[name] = Gate(name=name, gtype=gtype, inputs=inputs)

    # -- cell dispatch

    def add_cell(self, inst: str, cell: YosysCell) -> None:
        ctype = cell.type

        if ctype in INERT_CELLS:
            return
        if ctype in SEQUENTIAL_CELLS:
            self._cut_flipflop(cell)
            return
        if ctype in CELL_TYPE_MAP and self._add_primitive(cell, CELL_TYPE_MAP[ctype]):
            return
        if ctype in WORD_OP_MAP and self._add_word_op(cell, WORD_OP_MAP[ctype]):
            return
        if ctype in REDUCE_OP_MAP and self._add_reduce_op(cell, REDUCE_OP_MAP[ctype]):
            return
        if ctype in ("$mux", "$_MUX_") and self._add_mux(inst, cell):
            return
        if self._add_nangate_simple(cell):
            return
        if self._add_nangate_complex(inst, cell):
            return

        self.unknown_cells.append(f"{inst} (type {ctype})")

    # -- concrete cell kinds

    def _add_primitive(self, cell: YosysCell, gtype: GateType) -> bool:
        out_pin = _cell_output_pin(cell)
        if out_pin is None or not cell.connections.get(out_pin):
            return False
        out = self.sig(cell.connections[out_pin][0])
        ins = [
            self.sig(cell.connections[p][0])
            for p in _ordered_input_pins(cell, out_pin)
            if cell.connections.get(p)
        ]
        if not ins:
            return False
        self.add(out, gtype, ins)
        return True

    def _add_word_op(self, cell: YosysCell, gtype: GateType) -> bool:
        """Expand a word-level operator into one gate per output bit."""
        out_bits = cell.connections.get("Y") or []
        if not out_bits:
            return False
        operands = [cell.connections.get(p) or [] for p in ("A", "B")]
        operands = [o for o in operands if o]
        if not operands:
            return False

        for i, out_bit in enumerate(out_bits):
            # Yosys zero-extends narrower operands; a missing bit reads as 0.
            ins = [
                self.sig(op[i]) if i < len(op) else CONST0_SIGNAL
                for op in operands
            ]
            if any(i >= len(op) for op in operands):
                self.consts.add(CONST0_SIGNAL)
            self.add(self.sig(out_bit), gtype, ins)
        return True

    def _add_reduce_op(self, cell: YosysCell, gtype: GateType) -> bool:
        """Collapse every input bit of a reduction operator into one gate."""
        out_bits = cell.connections.get("Y") or []
        in_bits = cell.connections.get("A") or []
        if not out_bits or not in_bits:
            return False
        self.add(self.sig(out_bits[0]), gtype, self.sigs(in_bits))
        # A reduction wider than its 1-bit result leaves the upper output bits
        # at zero.
        for extra in out_bits[1:]:
            self.consts.add(CONST0_SIGNAL)
            self.add(self.sig(extra), GateType.BUFF, [CONST0_SIGNAL])
        return True

    def _add_mux(self, inst: str, cell: YosysCell) -> bool:
        """Y = S ? B : A, expanded to (A & ~S) | (B & S) per output bit."""
        out_bits = cell.connections.get("Y") or []
        a_bits = cell.connections.get("A") or []
        b_bits = cell.connections.get("B") or []
        s_bits = cell.connections.get("S") or []
        if not (out_bits and a_bits and b_bits and s_bits):
            return False

        s = self.sig(s_bits[0])
        not_s = f"__mux_{inst}_ns"
        self.add(not_s, GateType.NOT, [s])

        for i, out_bit in enumerate(out_bits):
            if i >= len(a_bits) or i >= len(b_bits):
                break
            a, b = self.sig(a_bits[i]), self.sig(b_bits[i])
            and_a, and_b = f"__mux_{inst}_{i}_a", f"__mux_{inst}_{i}_b"
            self.add(and_a, GateType.AND, [a, not_s])
            self.add(and_b, GateType.AND, [b, s])
            self.add(self.sig(out_bit), GateType.OR, [and_a, and_b])
        return True

    def _add_nangate_simple(self, cell: YosysCell) -> bool:
        resolved = resolve_simple(cell.type)
        if resolved is None:
            return False
        gtype_name, out_pin, in_pins = resolved
        out_bits = cell.connections.get(out_pin) or []
        if not out_bits:
            return False
        ins = [
            self.sig(cell.connections[p][0])
            for p in in_pins
            if cell.connections.get(p)
        ]
        if not ins:
            return False
        self.add(self.sig(out_bits[0]), GateType[gtype_name], ins)
        return True

    def _add_nangate_complex(self, inst: str, cell: YosysCell) -> bool:
        pin_to_signal = {
            pin: self.sig(bits[0])
            for pin, bits in cell.connections.items() if bits
        }
        decomposed = decompose_complex(cell.type, inst, pin_to_signal)
        if decomposed is None:
            return False
        for out_net, gtype_name, in_nets in decomposed:
            if out_net and all(in_nets):
                self.add(out_net, GateType[gtype_name], list(in_nets))
        return True

    def _cut_flipflop(self, cell: YosysCell) -> None:
        """
        Break a flip-flop open so a combinational engine can work on the design.

        The Q output becomes a pseudo primary input and the D input a pseudo
        primary output — the standard full-scan assumption. Encoding a DFF as a
        buffer instead (what this code used to do) closes the feedback path and
        turns every sequential design into a combinational loop.
        """
        q_bits = cell.connections.get("Q") or []
        d_bits = cell.connections.get("D") or []
        if not q_bits:
            return
        q = self.sig(q_bits[0])
        d = self.sig(d_bits[0]) if d_bits else ""
        self.add(q, GateType.INPUT, [])
        self.cut_flipflops.append((q, d))


# -------------------------------------------------------------------------
#  Module selection
# -------------------------------------------------------------------------

def _select_module(
    netlist: YosysNetlist,
    module_name: Optional[str],
) -> Tuple[str, YosysModule]:
    """
    Pick the design module.

    Preference: an explicit name, then the module Yosys marked `top`, then the
    one with the most cells. Taking the first module in the file (what this used
    to do) picks a library blackbox stub whenever the JSON was produced with
    `read_verilog -lib`, and yields an empty netlist with no error.
    """
    if module_name is not None:
        if module_name not in netlist.modules:
            raise KeyError(
                f"Module '{module_name}' not found. "
                f"Available: {netlist.module_names}"
            )
        return module_name, netlist.modules[module_name]

    if not netlist.modules:
        raise ValueError("Yosys JSON contains no modules.")

    with_cells = {n: m for n, m in netlist.modules.items() if m.cells}
    if not with_cells:
        raise ValueError(
            f"None of the {len(netlist.modules)} module(s) in this JSON "
            f"contain any cells: {netlist.module_names[:10]}.\n"
            f"This usually means only library blackbox stubs were written. "
            f"Read the cell library with 'read_verilog -lib' so Yosys keeps "
            f"it out of the JSON."
        )

    best = max(with_cells, key=lambda n: len(with_cells[n].cells))
    return best, with_cells[best]


# -------------------------------------------------------------------------
#  Post-processing
# -------------------------------------------------------------------------

def _is_generated_name(name: str) -> bool:
    """True for Yosys/ABC auto-generated wire names."""
    return "$" in name or ":" in name or name.startswith("__")


def _rename_internal_wires(
    gates: Dict[str, Gate],
    keep: set,
) -> Dict[str, Gate]:
    """
    Rewrite auto-generated internal wire names to n1, n2, n3, ...

    Primary inputs and outputs keep their names. Renaming is applied to gate
    names and to every input list so the graph stays consistent.
    """
    rename: Dict[str, str] = {}
    for i, name in enumerate(n for n in gates if n not in keep and _is_generated_name(n)):
        rename[name] = f"n{i + 1}"

    if not rename:
        return gates

    return {
        rename.get(name, name): Gate(
            name=rename.get(name, name),
            gtype=gate.gtype,
            inputs=[rename.get(inp, inp) for inp in gate.inputs],
        )
        for name, gate in gates.items()
    }


# -------------------------------------------------------------------------
#  Public API
# -------------------------------------------------------------------------

def parse_yosys_netlist(
    netlist: YosysNetlist,
    module_name: Optional[str] = None,
    rename_internal: bool = True,
) -> ParsedNetlist:
    """
    Convert a validated YosysNetlist into a ParsedNetlist.

    Parameters
    ----------
    netlist         : YosysNetlist
    module_name     : which module to parse; default picks the design top
    rename_internal : rewrite Yosys/ABC generated wire names to n1, n2, ...

    Raises
    ------
    ValueError : if any cell type could not be mapped to a gate function, or if
                 the JSON holds no module with cells. Both cases would otherwise
                 produce free SAT variables and meaningless ATPG results.
    """
    mod_name, module = _select_module(netlist, module_name)
    net_index = _build_net_index(module)

    collector = _GateCollector(net_index)

    # Primary inputs, expanded per bit for buses.
    primary_inputs: List[str] = []
    for port_name, port in module.ports.items():
        if port.direction != PortDirection.INPUT:
            continue
        width = len(port.bits)
        for i in range(width):
            name = _bit_name(port_name, i, width)
            primary_inputs.append(name)
            collector.add(name, GateType.INPUT, [])

    for inst_name, cell in module.cells.items():
        collector.add_cell(inst_name, cell)

    if collector.unknown_cells:
        listed = "\n  ".join(collector.unknown_cells[:10])
        more = (
            "" if len(collector.unknown_cells) <= 10
            else f"\n  ... and {len(collector.unknown_cells) - 10} more"
        )
        raise ValueError(
            f"{len(collector.unknown_cells)} cell(s) in module '{mod_name}' "
            f"have no known gate function:\n  {listed}{more}\n"
            f"Add them to CELL_TYPE_MAP in parser/yosysParser.py, or to "
            f"parser/nangateMap.py if they are library cells."
        )

    gates = collector.gates

    # Constant tie-off gates for any literal 0/1 that a cell read.
    undriven = sorted(c for c in collector.consts if c.startswith("__undriven__"))
    if CONST0_SIGNAL in collector.consts:
        gates[CONST0_SIGNAL] = Gate(name=CONST0_SIGNAL, gtype=GateType.CONST0, inputs=[])
    if CONST1_SIGNAL in collector.consts:
        gates[CONST1_SIGNAL] = Gate(name=CONST1_SIGNAL, gtype=GateType.CONST1, inputs=[])
    if undriven:
        warnings.warn(
            f"Module '{mod_name}' has pins driven by "
            f"{', '.join(t.replace('__undriven__', '') for t in undriven)}; "
            f"treated as constant 0.",
            UserWarning, stacklevel=2,
        )

    # Primary outputs, expanded per bit.
    primary_outputs: List[str] = []
    for port_name, port in module.ports.items():
        if port.direction != PortDirection.OUTPUT:
            continue
        width = len(port.bits)
        for i, bit in enumerate(port.bits):
            name = _bit_name(port_name, i, width)
            primary_outputs.append(name)
            if name in gates:
                continue
            # Nothing drives this output under its own name — it is an alias for
            # whatever signal owns the bit (an input feeding straight through, or
            # a net that won the naming race). Wire it up with a buffer rather
            # than leaving it as a free variable.
            driver = collector.sig(bit)
            if driver != name and driver in gates:
                gates[name] = Gate(name=name, gtype=GateType.BUFF, inputs=[driver])

    # Flip-flop D inputs become pseudo primary outputs (full-scan cut).
    for _q, d in collector.cut_flipflops:
        if d and d in gates and d not in primary_outputs:
            primary_outputs.append(d)

    if rename_internal:
        gates = _rename_internal_wires(gates, set(primary_inputs) | set(primary_outputs))

    parsed = ParsedNetlist(
        module_name=mod_name,
        gates=gates,
        primary_inputs=primary_inputs,
        primary_outputs=primary_outputs,
    )

    dangling = parsed.dangling_inputs()
    if dangling:
        shown = "; ".join(f"{g} reads {ins}" for g, ins in list(dangling.items())[:5])
        raise ValueError(
            f"Module '{mod_name}': {len(dangling)} gate(s) read wires that "
            f"nothing drives ({shown}). The netlist is incomplete."
        )

    if collector.cut_flipflops:
        warnings.warn(
            f"Module '{mod_name}': cut {len(collector.cut_flipflops)} flip-flop(s). "
            f"Each Q is now a pseudo primary input and each D a pseudo primary "
            f"output (full-scan assumption).",
            UserWarning, stacklevel=2,
        )

    return parsed


def parse_yosys_file(
    filepath: str,
    module_name: Optional[str] = None,
    rename_internal: bool = True,
) -> ParsedNetlist:
    """
    Load a Yosys JSON file and parse it into a ParsedNetlist.

    Parameters
    ----------
    filepath        : path to the Yosys-generated .json file
    module_name     : optional — which module to parse (default = design top)
    rename_internal : rewrite Yosys/ABC generated wire names to n1, n2, ...
    """
    netlist = YosysNetlist.from_file(filepath)
    return parse_yosys_netlist(
        netlist, module_name=module_name, rename_internal=rename_internal
    )
