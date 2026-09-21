# runBenchmarks.py
# ISCAS85 benchmark suite runner — driven by main.py.
#
# Public API:
#   run_suite()          - run ATPG over a list of circuits, write CSVs
#   load_circuit()       - load one circuit by name
#   write_circuit_csv()  - per-circuit CSV, one row per fault
#   write_summary_csv()  - cross-circuit summary CSV
#   available_circuits() - circuit names with a .bench file present

from __future__ import annotations

import csv
import os
import statistics
from pathlib import Path
from typing import List, Optional

from atpgEngine import AtpgResult, run_atpg
from circuitViz import draw_atpg_run
from parser.benchParser import parse_bench_file
from parser.yosysModels import ParsedNetlist

# -----------------------------------------------------------------------
#  ISCAS85 circuit registry, ordered by gate count
# -----------------------------------------------------------------------

ISCAS85 = [
    "c17",   "c432",  "c499",  "c880",  "c1355",
    "c1908", "c2670", "c3540", "c5315", "c6288", "c7552",
]

# Above this many fault sites the per-fault visualiser prints thousands of
# screens of output, so it is suppressed unless explicitly forced.
VIZ_FAULT_LIMIT = 64


# -----------------------------------------------------------------------
#  Circuit loading
# -----------------------------------------------------------------------

def available_circuits(bench_dir: str) -> List[str]:
    """ISCAS85 names that actually have a .bench file in bench_dir, in size order."""
    present = {p.stem for p in Path(bench_dir).glob("*.bench")}
    known = [name for name in ISCAS85 if name in present]
    extra = sorted(present - set(ISCAS85))
    return known + extra


def load_circuit(name: str, bench_dir: str) -> Optional[ParsedNetlist]:
    """Load one circuit by name. Returns None when no .bench file exists."""
    bench_file = Path(bench_dir) / f"{name}.bench"
    if not bench_file.exists():
        print(f"  Skipping {name} — no {bench_file}")
        return None
    print(f"  Loading bench file  :  {bench_file}")
    return parse_bench_file(str(bench_file))


# -----------------------------------------------------------------------
#  CSV writers
# -----------------------------------------------------------------------

def write_circuit_csv(result: AtpgResult, output_dir: str) -> Optional[str]:
    """Write one CSV per circuit, one row per fault site."""
    rows = result.to_rows()
    if not rows:
        return None
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{result.module_name}_results.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_summary_csv(summary_rows: List[dict], output_dir: str) -> Optional[str]:
    """Write the cross-circuit summary CSV."""
    if not summary_rows:
        return None
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "benchmark_summary.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    return path


# -----------------------------------------------------------------------
#  Summary row
# -----------------------------------------------------------------------

def _build_summary_row(result: AtpgResult, solver: str) -> dict:
    rows = result.fault_results
    if not rows:
        return {}

    times    = [r.metrics.solver_time_sec for r in rows]
    decs     = [r.metrics.decisions       for r in rows]
    confs    = [r.metrics.conflicts       for r in rows]
    props    = [r.metrics.propagations    for r in rows]
    restarts = [r.metrics.restarts        for r in rows]
    verified = sum(1 for r in rows if r.verified is not None)

    return {
        "circuit":                result.module_name,
        "total_faults":           result.total_faults,
        "detected_faults":        result.detected_faults,
        "undetectable_faults":    result.undetectable_faults,
        "error_faults":           result.error_faults,
        "fault_coverage_pct":     round(result.fault_coverage, 2),
        "vectors_verified":       verified,
        "vectors_failed_verify":  len(result.unverified_faults),
        "total_time_seconds":     round(result.total_time_sec, 4),
        "average_solve_ms":       round(statistics.mean(times) * 1000, 4),
        "max_solve_ms":           round(max(times) * 1000, 4),
        "average_decisions":      round(statistics.mean(decs), 2),
        "max_decisions":          max(decs),
        "total_decisions":        sum(decs),
        "average_conflicts":      round(statistics.mean(confs), 2),
        "max_conflicts":          max(confs),
        "total_conflicts":        sum(confs),
        "total_propagations":     sum(props),
        "average_restarts":       round(statistics.mean(restarts), 2),
        "solver":                 solver,
    }


# -----------------------------------------------------------------------
#  Suite runner
# -----------------------------------------------------------------------

def run_suite(
    names:         Optional[List[str]],
    bench_dir:     str,
    output_dir:    str,
    solver:        str = "g4",
    extract_proof: bool = False,
    visualize:     bool = False,
    verbose:       bool = False,
    verify:        bool = True,
    force_visual:  bool = False,
) -> List[dict]:
    """
    Run ATPG on each named circuit, or on every available circuit when names is
    None or ["all"]. Writes one CSV per circuit plus a summary CSV, and returns
    the summary rows.
    """
    if names is None or "all" in names:
        targets = available_circuits(bench_dir)
        if not targets:
            print(f"  No .bench files found in {bench_dir}")
            return []
    else:
        targets = names

    summary_rows: List[dict] = []

    for name in targets:
        print("\n" + "=" * 68)
        print(f"  Circuit : {name.upper()}")
        print("=" * 68)

        parsed = load_circuit(name, bench_dir)
        if parsed is None:
            continue

        result = run_atpg(
            parsed,
            solver_name=solver,
            extract_proof=extract_proof,
            verbose=verbose,
            verify=verify,
        )

        # The per-fault visualiser is unusable past a few dozen faults.
        if visualize:
            if force_visual or result.total_faults <= VIZ_FAULT_LIMIT:
                draw_atpg_run(parsed, result)
            else:
                print(f"\n  ({result.total_faults} fault sites — per-fault "
                      f"visualisation suppressed; pass --force-visual to print "
                      f"it anyway)")

        csv_path = write_circuit_csv(result, output_dir)
        print()
        result.print_summary()
        if csv_path:
            print(f"\n  Results CSV -> {csv_path}")

        row = _build_summary_row(result, solver)
        if row:
            summary_rows.append(row)

    summary_path = write_summary_csv(summary_rows, output_dir)
    if summary_path:
        print(f"\n  Summary CSV -> {summary_path}")

    return summary_rows
