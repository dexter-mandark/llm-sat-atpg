# miter.py
# SAT miter construction for stuck-at fault detection.
#
# A miter encodes two copies of the circuit side by side:
#   Good copy   (prefix "g_") — normal circuit behaviour
#   Faulty copy (prefix "f_") — same circuit with one wire stuck at 0 or 1
#
# Both copies receive the same primary input vector.
# The miter asserts at least one primary output differs between them.
# SAT   → the satisfying assignment IS the test vector that detects the fault.
# UNSAT → the fault is undetectable by any input vector.

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from cnfEncoder import CNFEncoder, CNFFormula
from parser.yosysModels import GateType, ParsedNetlist

GOOD_PREFIX = "g_"
FAULT_PREFIX = "f_"


# -----------------------------------------------------------------------------
#  Miter result — what build_miter() returns
# -----------------------------------------------------------------------------

class MiterResult(BaseModel):
    """
    Everything produced by build_miter().

    Fields
    ------
    formula         : the complete merged clause set. Pass formula.clauses to
                      PySAT, or formula.to_dimacs() to any DIMACS solver.
    good_vars       : signal name -> SAT variable in the fault-free copy
    faulty_vars     : signal name -> SAT variable in the faulty copy
    primary_inputs  : PI names, in circuit order
    primary_outputs : PO names, in circuit order
    fault_signal    : the wire that was stuck
    fault_value     : 0 (SA0) or 1 (SA1)
    diff_vars       : one variable per PO, true when that output differs

    good_vars and faulty_vars are keyed by bare signal name. The encoders' own
    maps use prefixed keys; exposing the bare names keeps callers from having to
    know about the prefixes, and stops PI lookups from accidentally matching
    every internal wire.
    """
    formula:         CNFFormula
    good_vars:       Dict[str, int] = Field(default_factory=dict)
    faulty_vars:     Dict[str, int] = Field(default_factory=dict)
    primary_inputs:  List[str]      = Field(default_factory=list)
    primary_outputs: List[str]      = Field(default_factory=list)
    fault_signal:    str            = ""
    fault_value:     int            = 0
    diff_vars:       List[int]      = Field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.fault_signal}/SA{self.fault_value}"

    def values_from_model(
        self,
        model: List[int],
        var_map: Dict[str, int],
    ) -> Dict[str, int]:
        """
        Read one copy's signal values out of a SAT model.

        `var_map` is either good_vars or faulty_vars. Variables the solver left
        unassigned are reported as 0, matching PySAT's own convention.
        """
        assigned = set(model)
        return {
            signal: (1 if var in assigned else 0)
            for signal, var in var_map.items()
        }


# -----------------------------------------------------------------------------
#  Public API
# -----------------------------------------------------------------------------

def build_miter(
    netlist:      ParsedNetlist,
    fault_signal: str,
    fault_value:  int,
    strict:       bool = True,
) -> MiterResult:
    """
    Build a SAT miter for detecting a single stuck-at fault.

    Parameters
    ----------
    netlist      : parsed circuit (from benchParser or yosysParser)
    fault_signal : name of the wire carrying the stuck-at fault
    fault_value  : 0 for stuck-at-0 (SA0), 1 for stuck-at-1 (SA1)
    strict       : refuse to encode gates with no known function

    Miter structure
    ---------------
    Primary inputs ---> Good circuit  (g_*) --> g_outputs ---|
                   |                                         XOR --> OR = 1
                   ---> Faulty circuit (f_*) --> f_outputs --|
                          |
                        fault injected here
    """
    if fault_value not in (0, 1):
        raise ValueError(f"fault_value must be 0 or 1, got {fault_value!r}")
    if fault_signal not in netlist.gates:
        raise KeyError(
            f"No signal named '{fault_signal}' in module "
            f"'{netlist.module_name}'."
        )

    # -- Good copy -------------------------------------------------------------
    good = CNFEncoder(prefix=GOOD_PREFIX, start_var=1, strict=strict)
    good.encode_circuit(netlist)

    # -- Faulty copy (variables continue where the good copy stopped) ----------
    faulty = CNFEncoder(prefix=FAULT_PREFIX, start_var=good.next_var, strict=strict)

    # Encode every gate EXCEPT the faulted one. Skipping its Tseitin clauses
    # means its backward implications cannot constrain the inputs; the wire is
    # replaced entirely by the unit clause below.
    for gate in netlist.topo_order():
        if gate.name == fault_signal:
            faulty.get_var(gate.name)   # allocate the variable, add no clauses
        else:
            faulty.encode_gate(gate)

    # -- Inject the stuck-at fault --------------------------------------------
    faulty.assert_signal(fault_signal, fault_value)

    clauses = list(good.clauses) + list(faulty.clauses)
    next_var = faulty.next_var

    # -- Tie primary inputs: g_PI <-> f_PI ------------------------------------
    # Both copies must see the same input vector:
    #   (g -> f): (-g | f)      (f -> g): (-f | g)
    #
    # The faulted PI is skipped. Tying it while also forcing f_PI to the stuck
    # value would drag g_PI to that value too, making both copies identical and
    # the fault undetectable by construction.
    for pi in netlist.primary_inputs:
        if pi == fault_signal:
            continue
        g_var = good.var_map[f"{GOOD_PREFIX}{pi}"]
        f_var = faulty.var_map[f"{FAULT_PREFIX}{pi}"]
        clauses.append([g_var, -f_var])
        clauses.append([-g_var, f_var])

    # -- Miter outputs: assert at least one PO differs -------------------------
    diff_vars: List[int] = []
    for po in netlist.primary_outputs:
        g_var = good.var_map[f"{GOOD_PREFIX}{po}"]
        f_var = faulty.var_map[f"{FAULT_PREFIX}{po}"]
        diff = next_var
        next_var += 1
        diff_vars.append(diff)
        # diff <-> (g_PO XOR f_PO)
        clauses += [
            [-diff,  g_var,  f_var],
            [-diff, -g_var, -f_var],
            [ diff, -g_var,  f_var],
            [ diff,  g_var, -f_var],
        ]

    clauses.append(diff_vars)   # at least one output must differ

    formula = CNFFormula(
        var_map={**good.var_map, **faulty.var_map},
        clauses=clauses,
        num_vars=next_var - 1,
        prefix="",
    )

    strip = len(GOOD_PREFIX)
    return MiterResult(
        formula=formula,
        good_vars={k[strip:]: v for k, v in good.var_map.items()},
        faulty_vars={k[strip:]: v for k, v in faulty.var_map.items()},
        primary_inputs=list(netlist.primary_inputs),
        primary_outputs=list(netlist.primary_outputs),
        fault_signal=fault_signal,
        fault_value=fault_value,
        diff_vars=diff_vars,
    )


# -----------------------------------------------------------------------------
#  Fault-site enumeration
# -----------------------------------------------------------------------------

# Gate types that are not real fault sites: a tie-off cannot be driven to the
# opposite value, so a stuck-at fault on one is meaningless.
_UNTESTABLE_TYPES = frozenset({GateType.CONST0, GateType.CONST1})


def enumerate_faults(netlist: ParsedNetlist) -> List[tuple]:
    """
    Every single stuck-at fault site: (signal, 0) and (signal, 1) per wire.

    Fault sites are chosen by gate type, not by name. Selecting them by name
    used to drop any wire literally called "0" or "1" — which is a normal wire
    name throughout the ISCAS benchmarks, so c17's input `1` was never tested
    and coverage was reported over 20 faults instead of 22.
    """
    return [
        (name, value)
        for name, gate in netlist.gates.items()
        if gate.gtype not in _UNTESTABLE_TYPES
        for value in (0, 1)
    ]
