# circuitViz.py
# Terminal circuit visualiser for SAT-ATPG.
#
# Good and faulty signal values come straight from the SAT model (see
# satSolver.SolveMetrics.good_values / faulty_values). They used to be
# re-derived here by evaluating each gate against the test vector and
# substituting the stuck value only at the fault site itself, which meant the
# fault effect never propagated past the first gate — downstream outputs were
# shown as matching even when the solver had just proven they differ.

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List

from atpgEngine import AtpgResult, FaultResult
from parser.yosysModels import Gate, ParsedNetlist

# -- ANSI colour codes ---------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    """A colour code, or "" when output is redirected or NO_COLOR is set."""
    return code if _USE_COLOUR else ""


RESET  = _c("\033[0m")
BOLD   = _c("\033[1m")
FAINT  = _c("\033[2m")
RED    = _c("\033[91m")
GREEN  = _c("\033[92m")
YELLOW = _c("\033[93m")
CYAN   = _c("\033[96m")
WHITE  = _c("\033[97m")
BG_RED = _c("\033[41m")

WIDTH     = 100
SEPARATOR = f"\n  {'═' * WIDTH}\n"
DIVIDER   = f"  {'-' * WIDTH}"


# -- helpers -------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Printable length of a string, ignoring ANSI escapes."""
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int) -> str:
    """Left-align to `width` printable columns."""
    return text + " " * max(0, width - _visible_len(text))


def _levelize(netlist: ParsedNetlist) -> Dict[str, int]:
    """
    Logic depth per signal, 0 at the primary inputs.

    One pass over the topological order, rather than the repeat-until-stable
    sweep this used to do.
    """
    depth: Dict[str, int] = {}
    for gate in netlist.topo_order():
        depth[gate.name] = (
            0 if not gate.inputs
            else max((depth.get(i, 0) for i in gate.inputs), default=0) + 1
        )
    return depth


def _logic_gates(netlist: ParsedNetlist) -> List[Gate]:
    """Non-input gates, ordered by logic depth then name."""
    depth = _levelize(netlist)
    return sorted(
        (g for g in netlist.gates.values() if not g.is_primary_input),
        key=lambda g: (depth.get(g.name, 0), g.name),
    )


def _fmt(value) -> str:
    return "?" if value is None else str(value)


# -----------------------------------------------------------------------------
#  draw_circuit — the gate table with no fault annotation
# -----------------------------------------------------------------------------

def draw_circuit(netlist: ParsedNetlist) -> None:
    gates = _logic_gates(netlist)

    print(SEPARATOR)
    print(f"  {BOLD}{WHITE}CIRCUIT : {netlist.module_name.upper()}{RESET}"
          f"  {FAINT}({len(gates)} gates,"
          f" {len(netlist.primary_inputs)} primary inputs,"
          f" {len(netlist.primary_outputs)} primary outputs){RESET}")
    print()
    print(f"  {FAINT}Primary inputs  : {', '.join(netlist.primary_inputs)}{RESET}")
    print(f"  {FAINT}Primary outputs : {', '.join(netlist.primary_outputs)}{RESET}")
    print()
    print(f"  {FAINT}{'Input wires':<32}  {'Gate type':<12}  Output wire{RESET}")
    print(DIVIDER)

    for gate in gates:
        inputs = ", ".join(f"{CYAN}{w}{RESET}" for w in gate.inputs)
        is_po = gate.name in netlist.primary_outputs
        out = (f"{BOLD}{GREEN}{gate.name}{RESET}" if is_po
               else f"{YELLOW}{gate.name}{RESET}")
        print(f"  {_pad(inputs, 32)}  [{gate.gtype.value}]  →  {out}")

    print(SEPARATOR)


# -----------------------------------------------------------------------------
#  draw_fault — the circuit annotated with one stuck-at fault
# -----------------------------------------------------------------------------

def draw_fault(netlist: ParsedNetlist, fr: FaultResult,
               index: int = 0, total: int = 0) -> None:
    metrics = fr.metrics
    stuck_signal = fr.fault_signal
    stuck_value = fr.fault_value
    sat = metrics.satisfiable

    # Values as the solver assigned them, so the fault effect is shown
    # propagating through the whole cone of influence.
    good_values = metrics.good_values
    faulty_values = metrics.faulty_values

    print(SEPARATOR)

    counter = f"{FAINT}[Fault {index} of {total}]{RESET}  " if total else ""
    print(f"  {counter}{BOLD}Fault site : {RED}{fr.label}{RESET}"
          f"   →   wire {CYAN}{stuck_signal}{RESET} is "
          f"{BOLD}{RED}permanently stuck at {stuck_value}{RESET}")

    if fr.error:
        print(f"  {RED}Could not be solved: {fr.error}{RESET}")
        return

    if sat:
        vector = "   ".join(
            f"{CYAN}{sig}{RESET} = {YELLOW}{val}{RESET}"
            for sig, val in metrics.test_vector.items()
        )
        print(f"  Test vector that detects this fault  ▶   {vector}")
        if fr.verified is False:
            print(f"  {BOLD}{RED}This vector did NOT expose the fault when "
                  f"simulated — the encoding is wrong.{RESET}")
    else:
        print(f"  {FAINT}No test vector exists — this fault cannot be detected "
              f"by any input combination{RESET}")

    print()
    print(f"  {FAINT}{'Input':<40}  {'Gate':<16}  "
          f"{'Output':<18}  {'Good':>8}  {'Faulty':>8}{RESET}")
    print(DIVIDER)

    for gate in _logic_gates(netlist):
        is_stuck = gate.name == stuck_signal

        labels = []
        for wire in gate.inputs:
            if not sat:
                labels.append(f"{FAINT}{wire} = unknown{RESET}")
                continue
            good_in = good_values.get(wire)
            faulty_in = faulty_values.get(wire)
            if wire == stuck_signal:
                labels.append(f"{BG_RED}{BOLD}{wire} = {_fmt(faulty_in)} "
                              f"(stuck){RESET}")
            elif good_in != faulty_in:
                # This input already carries the fault effect.
                labels.append(f"{CYAN}{wire}{RESET} = {YELLOW}{_fmt(good_in)}"
                              f"{RESET}{RED}/{_fmt(faulty_in)}{RESET}")
            else:
                labels.append(f"{CYAN}{wire}{RESET} = {YELLOW}{_fmt(good_in)}{RESET}")

        gate_label = f"[{gate.gtype.value}]"
        gate_str = f"{BG_RED}{BOLD}{gate_label}{RESET}" if is_stuck else gate_label

        if is_stuck:
            out_str = f"{BG_RED}{BOLD}{gate.name}  (stuck at {stuck_value}){RESET}"
        elif gate.name in netlist.primary_outputs:
            out_str = f"{BOLD}{GREEN}{gate.name}{RESET}"
        else:
            out_str = f"{YELLOW}{gate.name}{RESET}"

        good_out = good_values.get(gate.name) if sat else None
        faulty_out = faulty_values.get(gate.name) if sat else None
        differs = sat and good_out != faulty_out
        note = ""
        if differs:
            note = (f"  {BOLD}{GREEN}← output mismatch{RESET}"
                    if gate.name in netlist.primary_outputs
                    else f"  {FAINT}← fault effect{RESET}")

        print(f"  {_pad(',  '.join(labels), 40)}  "
              f"{_pad(gate_str, 16)}  "
              f"{_pad(out_str, 18)}  "
              f"{_fmt(good_out):>8}  {_fmt(faulty_out):>8}{note}")

    print()
    if sat:
        verdict = f"{BOLD}{GREEN}✓  FAULT DETECTED{RESET}"
        if fr.verified is True:
            verdict += f"  {FAINT}(vector confirmed by simulation){RESET}"
    else:
        verdict = f"{BOLD}{RED}✗  FAULT NOT DETECTABLE{RESET}"

    print(f"  {verdict}"
          f"   {FAINT}"
          f"decisions = {metrics.decisions}   "
          f"conflicts = {metrics.conflicts}   "
          f"propagations = {metrics.propagations}   "
          f"solver time = {metrics.solver_time_sec * 1000:.2f} ms"
          f"{RESET}")


# -----------------------------------------------------------------------------
#  draw_atpg_run — the circuit once, then every fault block
# -----------------------------------------------------------------------------

def draw_atpg_run(netlist: ParsedNetlist, result: AtpgResult) -> None:
    draw_circuit(netlist)

    total = len(result.fault_results)
    for index, fault_result in enumerate(result.fault_results, 1):
        draw_fault(netlist, fault_result, index=index, total=total)

    coverage = result.fault_coverage
    colour = GREEN if coverage == 100.0 else YELLOW

    print(SEPARATOR)
    print(f"  {BOLD}{WHITE}ATPG COMPLETE  —  {result.module_name.upper()}{RESET}")
    print()
    print(f"  Fault coverage       :  "
          f"{colour}{BOLD}{result.detected_faults} of {result.total_faults}"
          f" faults detected  ({coverage:.1f}%){RESET}")
    print(f"  Detected             :  {GREEN}{result.detected_faults}{RESET}")
    print(f"  Not detectable       :  {RED}{result.undetectable_faults}{RESET}")
    if result.error_faults:
        print(f"  Errors               :  {RED}{result.error_faults}{RESET}")

    checked = sum(1 for r in result.fault_results if r.verified is not None)
    if checked:
        failed = len(result.unverified_faults)
        state = (f"{GREEN}all confirmed{RESET}" if failed == 0
                 else f"{RED}{failed} failed{RESET}")
        print(f"  Vectors verified     :  {checked} simulated, {state}")

    print(f"  Total wall time      :  {result.total_time_sec:.4f} seconds")
    print(SEPARATOR)
