# benchParser.py
# Parser for ISCAS85-style .bench netlist files, coverts into ParsedNetlist (Pydantic)
#
# .bench format supports two variants:
#   Format A (ISCAS85 standard): output = GATETYPE(in1, in2, ...)
#   Format B (alternate):        GATETYPE(output, in1, in2, ...)

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from .yosysModels import Gate, GateType, ParsedNetlist


# -----------------------------------------------------------------------------
#  Gate-type normalisation table
#
#  Maps every known .bench gate-type string -> GateType enum value.
#  .bench files use uppercase strings; some variants use mixed case.
# -----------------------------------------------------------------------------

BENCH_GATE_TYPE_MAP: Dict[str, GateType] = {
    "AND":   GateType.AND,
    "OR":    GateType.OR,
    "NOT":   GateType.NOT,
    "NAND":  GateType.NAND,
    "NOR":   GateType.NOR,
    "XOR":   GateType.XOR,
    "XNOR":  GateType.XNOR,
    "BUFF":  GateType.BUFF,
    "BUF":   GateType.BUFF,   # alternate spelling
    "WIRE":  GateType.BUFF,   # wire = buffer in .bench
    "DFF":   GateType.DFF,    # D flip-flop (ISCAS89 sequential)
    "INPUT": GateType.INPUT,
    "OUTPUT":GateType.OUTPUT,
}


# -----------------------------------------------------------------------------
#  Compiled regex patterns
# -----------------------------------------------------------------------------

# Format A:  wire_name = GATETYPE(in1, in2, ...)
_FORMAT_A = re.compile(
    r'^(?P<out>\S+)\s*=\s*(?P<gtype>\w+)\s*\((?P<ins>[^)]*)\)$'
)

# Format B:  GATETYPE(out, in1, in2, ...)
_FORMAT_B = re.compile(
    r'^(?P<gtype>\w+)\s*\((?P<args>[^)]*)\)$'
)

# Standalone INPUT/OUTPUT declarations
_INPUT_DECL  = re.compile(r'^INPUT\s*\((?P<name>[^)]+)\)$',  re.IGNORECASE)
_OUTPUT_DECL = re.compile(r'^OUTPUT\s*\((?P<name>[^)]+)\)$', re.IGNORECASE)


# -----------------------------------------------------------------------------
#  Internal helpers
# -----------------------------------------------------------------------------

def _clean_line(raw: str) -> str:
    """Strip inline comments and whitespace from a raw line."""
    if "#" in raw:
        raw = raw[:raw.index("#")]
    return raw.strip()


def _normalise_gate_type(token: str) -> GateType:
    """
    Resolve a .bench gate-type token to a canonical GateType.

    Raises ValueError for a token with no known gate function. Mapping it to
    UNKNOWN instead would leave the encoder with a gate it cannot constrain,
    and every fault behind it would be reported as detected.
    """
    gtype = BENCH_GATE_TYPE_MAP.get(token.upper())
    if gtype is not None:
        return gtype
    try:
        return GateType(token.upper())
    except ValueError:
        raise ValueError(
            f"Unknown .bench gate type {token!r}. Known types: "
            f"{', '.join(sorted(BENCH_GATE_TYPE_MAP))}."
        ) from None


def _parse_signal_list(raw: str) -> List[str]:
    """Split a comma-separated signal list, stripping whitespace."""
    return [s.strip() for s in raw.split(",") if s.strip()]


# -----------------------------------------------------------------------------
#  Core line parser
# -----------------------------------------------------------------------------

def _parse_line(
    line:            str,
    gates:           Dict[str, Gate],
    primary_inputs:  List[str],
    primary_outputs: List[str],
) -> None:
    """
    Parse one .bench line and mutate gates / PI / PO lists in place.

    Handles:
      INPUT(name)                       -> primary input declaration
      OUTPUT(name)                      -> primary output declaration
      out = GATETYPE(in1, in2, ...)     -> Format A gate definition
      GATETYPE(out, in1, in2, ...)      -> Format B gate definition
    """

    # -- INPUT declaration
    m = _INPUT_DECL.match(line)
    if m:
        for name in _parse_signal_list(m.group("name")):
            if name not in primary_inputs:
                primary_inputs.append(name)
            # An INPUT pseudo-gate so the signal exists in the graph
            gates[name] = Gate(name=name, gtype=GateType.INPUT, inputs=[])
        return

    # -- OUTPUT declaration
    m = _OUTPUT_DECL.match(line)
    if m:
        for name in _parse_signal_list(m.group("name")):
            if name not in primary_outputs:
                primary_outputs.append(name)
        return

    # -- Format A: out = GATETYPE(in1, in2, ...)
    m = _FORMAT_A.match(line)
    if m:
        out_name = m.group("out").strip()
        gtype    = _normalise_gate_type(m.group("gtype"))
        inputs   = _parse_signal_list(m.group("ins"))
        gates[out_name] = Gate(name=out_name, gtype=gtype, inputs=inputs)
        return

    # -- Format B: GATETYPE(out, in1, in2, ...)
    m = _FORMAT_B.match(line)
    if m:
        gtype_str = m.group("gtype").upper()
        # Skip — INPUT/OUTPUT already handled above
        if gtype_str in ("INPUT", "OUTPUT"):
            return
        args     = _parse_signal_list(m.group("args"))
        if len(args) < 1:
            return
        out_name = args[0]
        inputs   = args[1:]
        gtype    = _normalise_gate_type(gtype_str)
        gates[out_name] = Gate(name=out_name, gtype=gtype, inputs=inputs)
        return

    # -- Unrecognised line — silently ignore (comments already stripped)


# -----------------------------------------------------------------------------
#  Post-processing
# -----------------------------------------------------------------------------

def _resolve_implicit_inputs(
    gates:          Dict[str, Gate],
    primary_inputs: List[str],
) -> None:
    """
    Some .bench files reference signals as gate inputs without declaring
    them as INPUT() first. Detect these and add implicit INPUT gates.

    This is common in hand-written or converted bench files.
    """
    for gate in list(gates.values()):
        for inp in gate.inputs:
            if inp not in gates:
                # Implicit PI — used as an input but never INPUT-declared.
                # Note "0" and "1" are ordinary wire names in ISCAS .bench
                # files (c17 declares INPUT(1)), so they are not treated as
                # constants here.
                gates[inp] = Gate(name=inp, gtype=GateType.INPUT, inputs=[])
                if inp not in primary_inputs:
                    primary_inputs.append(inp)


# -----------------------------------------------------------------------------
#  Public API
# -----------------------------------------------------------------------------

def parse_bench_file(filepath: str) -> ParsedNetlist:
    """
    Parse an ISCAS85-style .bench file into a ParsedNetlist.

    Parameters
    ----------
    filepath : path to the .bench file

    Returns
    -------
    ParsedNetlist with the complete gate graph, PI list, and PO list.
    The ParsedNetlist.topo_order() method works immediately after parsing.

    Supports
    --------
    - ISCAS85 combinational benchmarks (c17 ... c7552)
    - ISCAS89 sequential benchmarks    (s27 ... s38584) — DFFs treated as PIs
    - Both Format A and Format B gate declarations
    - Inline # comments
    - Implicit primary inputs (signals used but never INPUT-declared)

    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f".bench file not found: {filepath}")

    gates:           Dict[str, Gate] = {}
    primary_inputs:  List[str]       = []
    primary_outputs: List[str]       = []

    with open(path) as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = _clean_line(raw_line)
            if not line:
                continue
            try:
                _parse_line(line, gates, primary_inputs, primary_outputs)
            except ValueError as exc:
                raise ValueError(f"{filepath}:{lineno}: {exc}") from None

    # Fill in any signals used as inputs but never declared
    _resolve_implicit_inputs(gates, primary_inputs)

    return ParsedNetlist(
        module_name     = path.stem, # e.g. "c17" from "c17.bench"
        gates           = gates,
        primary_inputs  = primary_inputs,
        primary_outputs = primary_outputs,
    )


def parse_bench_string(content: str, name: str = "inline") -> ParsedNetlist:
    """
    Parse a .bench circuit from a raw string instead of a file.
    Useful for hardcoded benchmarks (e.g. c17 inline) and unit tests.

    Parameters
    ----------
    content : full .bench file content as a string
    name    : module name to assign (default "inline")

    Example
    -------
    C17 = 
    INPUT(1) ...
    22 = NAND(10, 16)

    parsed = parse_bench_string(C17, name="c17")
    """
    gates:           Dict[str, Gate] = {}
    primary_inputs:  List[str]       = []
    primary_outputs: List[str]       = []

    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        line = _clean_line(raw_line)
        if not line:
            continue
        try:
            _parse_line(line, gates, primary_inputs, primary_outputs)
        except ValueError as exc:
            raise ValueError(f"{name}:{lineno}: {exc}") from None

    _resolve_implicit_inputs(gates, primary_inputs)

    return ParsedNetlist(
        module_name     = name,
        gates           = gates,
        primary_inputs  = primary_inputs,
        primary_outputs = primary_outputs,
    )
