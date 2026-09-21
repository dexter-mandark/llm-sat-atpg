# atpgEngine.py
# SAT-based ATPG engine.
#
# For every stuck-at fault site it builds a miter, solves it, and collects the
# result. Encoding and solving live in miter.py and satSolver.py; this module
# is the loop, the bookkeeping, and the self-check.

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from miter import MiterResult, build_miter, enumerate_faults
from parser.yosysModels import ParsedNetlist
from satSolver import SolveMetrics, solve
from simulator import detects_fault

Fault = Tuple[str, int]


# -----------------------------------------------------------------------------
# Result models
# -----------------------------------------------------------------------------

class FaultResult(BaseModel):
    """
    Result for a single stuck-at fault.

    verified is None when no check was run, True when simulating the returned
    test vector confirmed the fault is exposed at a primary output, and False
    when it did not — which means the CNF encoding disagrees with the circuit
    and the whole run should be treated as suspect.
    """
    fault_signal: str
    fault_value:  int
    metrics:      SolveMetrics = Field(default_factory=SolveMetrics)
    verified:     Optional[bool] = None
    error:        Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.fault_signal}/SA{self.fault_value}"

    # circuitViz refers to fault_label; keep both names pointing at one value.
    @property
    def fault_label(self) -> str:
        return self.label

    @property
    def detected(self) -> bool:
        return self.metrics.satisfiable

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        return "DETECTED" if self.detected else "UNDETECTABLE"

    @property
    def test_vector(self) -> Dict[str, int]:
        return self.metrics.test_vector

    def to_row(self) -> dict:
        m = self.metrics
        return {
            "fault":           self.label,
            "signal":          self.fault_signal,
            "stuck_at":        self.fault_value,
            "status":          self.status,
            "detected":        self.detected,
            "verified":        "" if self.verified is None else self.verified,
            "test_vector":     _format_vector(self.test_vector),
            "decisions":       m.decisions,
            "conflicts":       m.conflicts,
            "propagations":    m.propagations,
            "restarts":        m.restarts,
            "learned_clauses": m.learned_clauses,
            "solver_time_ms":  round(m.solver_time_sec * 1000, 4),
            "wall_time_ms":    round(m.wall_time_sec * 1000, 4),
            "num_vars":        m.num_vars,
            "num_clauses":     m.num_clauses,
            "error":           self.error or "",
        }


class AtpgResult(BaseModel):
    """Complete ATPG result for one circuit."""
    module_name:    str
    fault_results:  List[FaultResult] = Field(default_factory=list)
    total_time_sec: float = 0.0

    @property
    def total_faults(self) -> int:
        return len(self.fault_results)

    @property
    def detected_faults(self) -> int:
        return sum(1 for r in self.fault_results if r.detected)

    @property
    def undetectable_faults(self) -> int:
        return sum(1 for r in self.fault_results
                   if not r.detected and r.error is None)

    @property
    def error_faults(self) -> int:
        return sum(1 for r in self.fault_results if r.error is not None)

    @property
    def unverified_faults(self) -> List[FaultResult]:
        """Detected faults whose test vector failed the simulation check."""
        return [r for r in self.fault_results if r.verified is False]

    @property
    def fault_coverage(self) -> float:
        if self.total_faults == 0:
            return 0.0
        return self.detected_faults / self.total_faults * 100.0

    def print_summary(self) -> None:
        print(f"  Circuit           : {self.module_name}")
        print(f"  Total faults      : {self.total_faults}")
        print(f"  Detected          : {self.detected_faults}")
        print(f"  Not detectable    : {self.undetectable_faults}")
        if self.error_faults:
            print(f"  Errors            : {self.error_faults}")
        print(f"  Fault coverage    : {self.fault_coverage:.2f}%")
        checked = sum(1 for r in self.fault_results if r.verified is not None)
        if checked:
            failed = len(self.unverified_faults)
            state = "all confirmed" if failed == 0 else f"{failed} FAILED"
            print(f"  Vectors verified  : {checked} simulated, {state}")
        print(f"  Total wall time   : {self.total_time_sec:.4f}s")

    def to_rows(self) -> List[dict]:
        return [r.to_row() for r in self.fault_results]


def _format_vector(vector: Dict[str, int]) -> str:
    """Render a test vector compactly: 'a=0 b=1 c=1'."""
    return " ".join(f"{k}={v}" for k, v in vector.items())


# -----------------------------------------------------------------------------
# Core ATPG engine
# -----------------------------------------------------------------------------

def run_atpg(
    netlist:       ParsedNetlist,
    solver_name:   str = "g4",
    extract_proof: bool = False,
    verbose:       bool = False,
    verify:        bool = True,
    fault_filter:  Optional[Sequence[Fault]] = None,
    progress:      bool = True,
) -> AtpgResult:
    """
    Run stuck-at ATPG over every fault site in the netlist.

    Parameters
    ----------
    netlist       : parsed circuit
    solver_name   : PySAT solver key — see satSolver.SOLVERS
    extract_proof : collect DRUP proof lines for UNSAT results
    verbose       : print one line per fault while solving
    verify        : simulate each test vector to confirm it exposes the fault.
                    Cheap next to the SAT call, and the only thing that catches
                    a bad encoding — a wrong encoder reports coverage just as
                    confidently as a right one.
    fault_filter  : only solve the listed (signal, value) pairs, e.g. [("N11", 0)]
    progress      : print the circuit header before solving

    Returns
    -------
    AtpgResult with per-fault metrics and summary statistics.
    """
    faults: List[Fault] = enumerate_faults(netlist)

    if fault_filter is not None:
        wanted = {(str(s), int(v)) for s, v in fault_filter}
        unknown = wanted - set(faults)
        if unknown:
            raise KeyError(
                f"No such fault site(s) in '{netlist.module_name}': "
                f"{sorted(unknown)}"
            )
        faults = [f for f in faults if f in wanted]

    total = len(faults)
    results = AtpgResult(module_name=netlist.module_name)

    if progress:
        print(
            f"\n  [{netlist.module_name}]  "
            f"{netlist.gate_count} gates  "
            f"{len(netlist.primary_inputs)} primary inputs  "
            f"{len(netlist.primary_outputs)} primary outputs  "
            f"{total} fault sites\n"
        )

    suite_start = time.perf_counter()

    for idx, (signal, fval) in enumerate(faults, start=1):
        fr = FaultResult(fault_signal=signal, fault_value=fval)

        try:
            miter: MiterResult = build_miter(netlist, signal, fval)
            fr.metrics = solve(
                miter=miter,
                solver_name=solver_name,
                extract_proof=extract_proof,
            )
            if verify and fr.metrics.satisfiable:
                confirmed, _good, _faulty = detects_fault(
                    netlist, fr.metrics.test_vector, signal, fval
                )
                fr.verified = confirmed
        except Exception as exc:                      # noqa: BLE001 — recorded, not hidden
            fr.error = f"{type(exc).__name__}: {exc}"

        results.fault_results.append(fr)

        if verbose:
            _print_fault_line(idx, total, fr)

    results.total_time_sec = time.perf_counter() - suite_start

    failed = results.unverified_faults
    if failed:
        print(
            f"\n  WARNING: {len(failed)} test vector(s) did not expose their "
            f"fault when simulated, so the CNF encoding disagrees with the "
            f"circuit. Affected: "
            f"{', '.join(r.label for r in failed[:5])}"
            f"{' ...' if len(failed) > 5 else ''}"
        )

    errors = [r for r in results.fault_results if r.error]
    if errors:
        print(f"\n  WARNING: {len(errors)} fault(s) could not be solved.")
        for r in errors[:3]:
            print(f"    {r.label}: {r.error}")

    return results


def _print_fault_line(idx: int, total: int, fr: FaultResult) -> None:
    m = fr.metrics
    if fr.error:
        print(f"  [{idx:>4}/{total}]  {fr.label:<24}  ERROR  {fr.error}")
        return

    mark = {True: " ok", False: " FAILED VERIFY", None: ""}[fr.verified]
    print(
        f"  [{idx:>4}/{total}]  {fr.label:<24}  {fr.status:<14}"
        f"  decisions = {m.decisions:>6}"
        f"  conflicts = {m.conflicts:>6}"
        f"  solver time = {m.solver_time_sec * 1000:>7.2f} ms{mark}"
    )
