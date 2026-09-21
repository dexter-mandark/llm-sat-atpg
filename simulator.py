# simulator.py
# Plain logic simulator for ParsedNetlist.
#
# This exists to check the SAT results independently. The ATPG engine claims a
# fault is detected by a given input vector; simulating the good and faulty
# circuits on that vector either confirms it or exposes an encoder bug. A test
# vector nobody ever simulates is a claim, not a result.

from __future__ import annotations

from typing import Dict, Optional, Tuple

from parser.yosysModels import GateType, ParsedNetlist


class SimulationError(ValueError):
    """Raised when a netlist cannot be evaluated (unknown gate, missing input)."""


def eval_gate(gtype: GateType, inputs: list[int]) -> int:
    """
    Evaluate one gate. Inputs are 0/1; the result is 0/1.

    Raises SimulationError for gate types with no defined function, rather than
    guessing — a wrong value here would silently corrupt a verification pass.
    """
    if gtype in (GateType.INPUT, GateType.OUTPUT):
        raise SimulationError(
            f"{gtype.value} gates have no function; their value is supplied, "
            f"not computed."
        )

    if gtype == GateType.CONST0:
        return 0
    if gtype == GateType.CONST1:
        return 1

    if not inputs:
        raise SimulationError(f"{gtype.value} gate has no inputs.")

    if gtype in (GateType.BUFF, GateType.DFF):
        return inputs[0]
    if gtype == GateType.NOT:
        return 1 - inputs[0]
    if gtype == GateType.AND:
        return int(all(inputs))
    if gtype == GateType.NAND:
        return int(not all(inputs))
    if gtype == GateType.OR:
        return int(any(inputs))
    if gtype == GateType.NOR:
        return int(not any(inputs))
    if gtype == GateType.XOR:
        result = 0
        for value in inputs:
            result ^= value
        return result
    if gtype == GateType.XNOR:
        result = 0
        for value in inputs:
            result ^= value
        return 1 - result
    if gtype == GateType.ANDNOT:          # Y = A AND NOT(B)
        return int(inputs[0] and not inputs[1])
    if gtype == GateType.ORNOT:           # Y = A OR NOT(B)
        return int(inputs[0] or not inputs[1])

    raise SimulationError(
        f"Gate type {gtype.value} has no simulation model. "
        f"Add one to simulator.eval_gate()."
    )


def simulate(
    netlist: ParsedNetlist,
    pi_values: Dict[str, int],
    stuck_signal: Optional[str] = None,
    stuck_value: Optional[int] = None,
) -> Dict[str, int]:
    """
    Evaluate every signal in the netlist for one input vector.

    Parameters
    ----------
    netlist      : the circuit
    pi_values    : primary input name -> 0/1. Inputs left out default to 0.
    stuck_signal : optional wire to hold at a fixed value (stuck-at fault)
    stuck_value  : 0 or 1, the value that wire is held at

    Returns
    -------
    signal name -> 0/1 for every signal, faulty value included. Because gates
    are evaluated in topological order, a stuck value propagates forward through
    the whole cone of influence — which is the part the old per-gate display
    logic got wrong.
    """
    values: Dict[str, int] = {}

    for gate in netlist.topo_order():
        if gate.gtype == GateType.INPUT:
            value = int(pi_values.get(gate.name, 0)) & 1
        elif gate.gtype in (GateType.CONST0, GateType.CONST1):
            value = eval_gate(gate.gtype, [])
        else:
            operands = []
            for inp in gate.inputs:
                if inp not in values:
                    raise SimulationError(
                        f"Gate '{gate.name}' reads '{inp}', which has no value. "
                        f"The netlist is incomplete or not topologically sorted."
                    )
                operands.append(values[inp])
            value = eval_gate(gate.gtype, operands)

        if gate.name == stuck_signal and stuck_value is not None:
            value = stuck_value
        values[gate.name] = value

    return values


def detects_fault(
    netlist: ParsedNetlist,
    pi_values: Dict[str, int],
    stuck_signal: str,
    stuck_value: int,
) -> Tuple[bool, Dict[str, int], Dict[str, int]]:
    """
    Check whether one input vector actually exposes one stuck-at fault.

    Returns (detected, good_values, faulty_values), where detected is True when
    at least one primary output differs between the two simulations.
    """
    good = simulate(netlist, pi_values)
    faulty = simulate(netlist, pi_values, stuck_signal, stuck_value)
    detected = any(good[po] != faulty[po] for po in netlist.primary_outputs)
    return detected, good, faulty


def truth_table(netlist: ParsedNetlist) -> Dict[Tuple[int, ...], Tuple[int, ...]]:
    """
    Exhaustively evaluate the circuit: PI assignment -> PO values.

    Only usable for small circuits (2**len(primary_inputs) simulations); it is
    here to prove two netlists implement the same function.
    """
    n = len(netlist.primary_inputs)
    if n > 20:
        raise SimulationError(
            f"Refusing to build a truth table over {n} inputs "
            f"({2 ** n} rows). Use ATPG or equivalence checking instead."
        )

    table: Dict[Tuple[int, ...], Tuple[int, ...]] = {}
    for pattern in range(2 ** n):
        assignment = {
            pi: (pattern >> i) & 1
            for i, pi in enumerate(netlist.primary_inputs)
        }
        values = simulate(netlist, assignment)
        key = tuple(assignment[pi] for pi in netlist.primary_inputs)
        table[key] = tuple(values[po] for po in netlist.primary_outputs)
    return table
