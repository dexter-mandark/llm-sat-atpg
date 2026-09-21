# satSolver.py
# PySAT wrapper with metric extraction.
#
# Two entry points:
#   solve()     — takes a MiterResult directly (primary ATPG path)
#   solve_raw() — takes raw clauses + encoder (general purpose)
#
# Both return a SolveMetrics object holding the solver statistics plus, for SAT
# results, the test vector and the full signal assignment of each circuit copy.

from __future__ import annotations

import time
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from cnfEncoder import CNFEncoder
from miter import MiterResult

# PySAT solver ids accepted by --solver, with the names people actually use.
SOLVERS = {
    "g3":  "Glucose 3",
    "g4":  "Glucose 4",
    "cd":  "CaDiCaL 103",
    "cd15": "CaDiCaL 153",
    "m22": "MiniSat 2.2",
    "mgh": "MiniSat GitHub",
    "lgl": "Lingeling",
    "mcb": "Maplesat CB",
    "mpl": "Maplesat",
}

# Solvers whose PySAT binding supports DRUP proof logging.
PROOF_CAPABLE = frozenset({"g3", "g4", "lgl", "mcb", "mpl", "m22", "mgh"})


# -----------------------------------------------------------------------------
#  Metrics model
# -----------------------------------------------------------------------------

class SolveMetrics(BaseModel):
    """
    Everything one PySAT call produced.

    Solver statistics
    -----------------
    satisfiable     : True if SAT, False if UNSAT
    wall_time_sec   : elapsed time including Python overhead
    solver_time_sec : the solver's own timer
    restarts        : restart count
    decisions       : branching decisions
    conflicts       : conflicts reached — in CDCL one conflict yields one
                      learned clause, so this doubles as the learned-clause count
    propagations    : unit propagation steps

    Problem size
    ------------
    num_vars        : variables in the CNF handed to the solver
    num_clauses     : clauses in the CNF handed to the solver

    Result fields
    -------------
    test_vector   : primary input name -> 0/1 (SAT only). Primary inputs only —
                    this used to contain every wire in the circuit.
    good_values   : every signal of the fault-free copy under that vector
    faulty_values : every signal of the faulty copy under that vector
    model         : raw signed-integer model from the solver
    drup_proof    : DRUP proof lines (UNSAT, proof-capable solvers only)
    unsat_core    : assumption literals responsible for UNSAT
    """
    # Result
    satisfiable:      bool           = False
    # Timing
    wall_time_sec:    float          = 0.0
    solver_time_sec:  float          = 0.0
    # CDCL stats
    restarts:         int            = 0
    decisions:        int            = 0
    conflicts:        int            = 0
    propagations:     int            = 0
    # Problem size
    num_vars:         int            = 0
    num_clauses:      int            = 0
    # SAT result
    test_vector:      Dict[str, int] = Field(default_factory=dict)
    good_values:      Dict[str, int] = Field(default_factory=dict)
    faulty_values:    Dict[str, int] = Field(default_factory=dict)
    model:            List[int]      = Field(default_factory=list)
    # UNSAT result
    drup_proof:       List[str]      = Field(default_factory=list)
    unsat_core:       List[int]      = Field(default_factory=list)
    # Context
    assumptions_used: List[int]      = Field(default_factory=list)
    fault_signal:     Optional[str]  = None
    fault_value:      Optional[int]  = None

    @property
    def learned_clauses(self) -> int:
        """
        CDCL learns one clause per conflict. The old field subtracted two
        nof_clauses() readings, which never counts learned clauses and so
        always reported 0.
        """
        return self.conflicts

    def to_dict(self) -> dict:
        """Flatten for CSV writing, truncating the long list fields."""
        d = self.model_dump()
        d["learned_clauses"] = self.learned_clauses
        d["drup_proof_count"] = len(self.drup_proof)
        d["drup_proof"] = "|".join(self.drup_proof[:5])
        d["unsat_core"] = str(self.unsat_core[:10])
        d["model"] = str(self.model[:10])
        d["test_vector"] = str(self.test_vector)
        d["good_values"] = str(self.good_values)
        d["faulty_values"] = str(self.faulty_values)
        return d


# -----------------------------------------------------------------------------
#  Shared internal solver
# -----------------------------------------------------------------------------

def _solver_kwargs(solver_name: str, extract_proof: bool) -> dict:
    kwargs: dict = {"use_timer": True}
    if extract_proof:
        if solver_name not in PROOF_CAPABLE:
            raise ValueError(
                f"Solver '{solver_name}' cannot emit DRUP proofs. "
                f"Proof-capable solvers: {', '.join(sorted(PROOF_CAPABLE))}."
            )
        kwargs["with_proof"] = True
    return kwargs


def _run_solver(
    clauses:       List[List[int]],
    assumptions:   List[int],
    solver_name:   str,
    extract_proof: bool,
) -> tuple:
    """Run PySAT once and return (metrics, model). Shared by both entry points."""
    try:
        from pysat.formula import CNF as PySATCNF
        from pysat.solvers import Solver
    except ImportError as exc:
        raise ImportError(
            "pysat is required for SAT solving. Install it with:\n"
            "    pip install python-sat"
        ) from exc

    if solver_name not in SOLVERS:
        raise ValueError(
            f"Unknown solver '{solver_name}'. "
            f"Choose one of: {', '.join(sorted(SOLVERS))}."
        )

    metrics = SolveMetrics(assumptions_used=list(assumptions))
    formula = PySATCNF(from_clauses=clauses)
    kwargs = _solver_kwargs(solver_name, extract_proof)

    t_wall = time.perf_counter()
    model: List[int] = []

    with Solver(name=solver_name, bootstrap_with=formula, **kwargs) as s:
        metrics.num_vars = s.nof_vars()
        metrics.num_clauses = s.nof_clauses()

        result = s.solve(assumptions=assumptions)
        metrics.satisfiable = bool(result)
        metrics.wall_time_sec = time.perf_counter() - t_wall
        metrics.solver_time_sec = s.time()

        stats = s.accum_stats()
        metrics.restarts = stats.get("restarts", 0)
        metrics.decisions = stats.get("decisions", 0)
        metrics.conflicts = stats.get("conflicts", 0)
        metrics.propagations = stats.get("propagations", 0)

        if result:
            model = s.get_model() or []
            metrics.model = model
        elif assumptions:
            # get_core() only means anything when the call had assumptions.
            metrics.unsat_core = list(s.get_core() or [])

        if extract_proof:
            metrics.drup_proof = list(s.get_proof() or [])

    return metrics, model


# -----------------------------------------------------------------------------
#  Public API
# -----------------------------------------------------------------------------

def solve(
    miter:         MiterResult,
    assumptions:   Optional[List[int]] = None,
    solver_name:   str = "g4",
    extract_proof: bool = False,
) -> SolveMetrics:
    """
    Run the SAT solver on a MiterResult (the primary ATPG path).

    Parameters
    ----------
    miter         : output of build_miter()
    assumptions   : optional forced literals (signed ints)
    solver_name   : PySAT solver id — see SOLVERS
    extract_proof : collect DRUP proof lines (proof-capable solvers only)

    Returns
    -------
    SolveMetrics. On SAT, test_vector holds the primary input assignment that
    detects the fault, and good_values / faulty_values hold every signal in each
    circuit copy so the propagation path can be inspected.
    """
    metrics, model = _run_solver(
        clauses=miter.formula.clauses,
        assumptions=assumptions or [],
        solver_name=solver_name,
        extract_proof=extract_proof,
    )

    metrics.fault_signal = miter.fault_signal
    metrics.fault_value = miter.fault_value

    if metrics.satisfiable:
        good = miter.values_from_model(model, miter.good_vars)
        metrics.good_values = good
        metrics.faulty_values = miter.values_from_model(model, miter.faulty_vars)
        metrics.test_vector = {
            pi: good[pi] for pi in miter.primary_inputs if pi in good
        }

    return metrics


def solve_raw(
    clauses:        List[List[int]],
    encoder:        CNFEncoder,
    primary_inputs: List[str],
    assumptions:    Optional[List[int]] = None,
    solver_name:    str = "g4",
    extract_proof:  bool = False,
) -> SolveMetrics:
    """
    Run the SAT solver on arbitrary clauses plus the encoder that built them.

    Use this for a CNF that is not a miter — a plain satisfiability check, a
    custom encoding, or any non-fault query.
    """
    metrics, model = _run_solver(
        clauses=clauses,
        assumptions=assumptions or [],
        solver_name=solver_name,
        extract_proof=extract_proof,
    )

    if metrics.satisfiable:
        assigned = set(model)
        metrics.test_vector = {
            pi: (1 if encoder.var_map[key] in assigned else 0)
            for pi in primary_inputs
            if (key := f"{encoder.prefix}{pi}") in encoder.var_map
        }

    return metrics
