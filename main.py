#!/usr/bin/env python3
# main.py
# Single entry point for SAT-ATPG.
#
# Modes:
#   --bench c17 c432 ...   run ISCAS85 .bench circuits by name, or 'all'
#   --verilog FILE.v       synthesise with Yosys, then run ATPG
#   --json FILE.json       run ATPG on an existing Yosys JSON netlist
#   --interactive          pick one wire and stuck-at value instead of all faults

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional

from atpgEngine import run_atpg
from circuitViz import draw_atpg_run, draw_circuit
from parser import ParsedNetlist, parse_yosys_file
from runBenchmarks import (
    VIZ_FAULT_LIMIT,
    available_circuits,
    load_circuit,
    run_suite,
    write_circuit_csv,
)
from satSolver import PROOF_CAPABLE, SOLVERS

# -----------------------------------------------------------------------------
# Defaults — every one of these is overridable from the command line
# -----------------------------------------------------------------------------

BENCH_DIR  = "./benchmarks"
OUTPUT_DIR = "./results"
JSONS_DIR  = "./jsons"

# Nangate cell library, used to tech-map Verilog to real standard cells.
LIBERTY_PATH    = "./parser/NangateOpenCellLibrary_typical.lib"
BLACKBOX_V_PATH = "./parser/NangateOpenCellLibrary.blackbox.v"


# -----------------------------------------------------------------------------
# Yosys synthesis
# -----------------------------------------------------------------------------

def build_yosys_script(
    verilog_path: str,
    json_path:    str,
    use_liberty:  bool,
    top:          Optional[str],
) -> str:
    """
    Build the Yosys command script.

    The top module is chosen with `hierarchy -auto-top` unless --top names one.
    Hardcoding a module name here only ever worked for a single design.
    """
    top_arg = f"-top {top}" if top else "-auto-top"
    steps = []

    if use_liberty:
        # -lib marks the cell library as blackboxes so it stays out of the JSON.
        steps.append(f"read_verilog -lib {BLACKBOX_V_PATH}")

    steps += [
        f"read_verilog {verilog_path}",
        f"hierarchy -check {top_arg}",
        "proc",
        "opt",
        "flatten",
        "opt",
        "techmap",
        "opt",
    ]

    if use_liberty:
        steps.append(f"abc -liberty {LIBERTY_PATH}")

    steps += ["opt_clean", f"write_json {json_path}"]
    return "; ".join(steps)


def run_yosys(
    verilog_path: str,
    use_liberty:  bool = True,
    top:          Optional[str] = None,
    jsons_dir:    str = JSONS_DIR,
) -> str:
    """
    Synthesise a Verilog file with Yosys and return the JSON netlist path.
    """
    if not os.path.isfile(verilog_path):
        raise FileNotFoundError(f"Verilog file not found: {verilog_path}")

    if use_liberty:
        missing = [
            p for p in (LIBERTY_PATH, BLACKBOX_V_PATH) if not os.path.isfile(p)
        ]
        if missing:
            print(f"  Cell library not found ({', '.join(missing)}) — "
                  f"falling back to generic synthesis.")
            use_liberty = False

    os.makedirs(jsons_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(verilog_path))[0]
    suffix = "_techmapped" if use_liberty else "_generic"
    json_path = os.path.join(jsons_dir, f"{base}{suffix}.json")

    script = build_yosys_script(verilog_path, json_path, use_liberty, top)

    print(f"  Yosys mode    : "
          f"{'tech-mapped (Nangate liberty)' if use_liberty else 'generic'}")
    print(f"  Input Verilog : {verilog_path}")
    print(f"  Output JSON   : {json_path}\n")

    try:
        result = subprocess.run(
            ["yosys", "-p", script], capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Yosys not found on PATH. Install it with one of:\n"
            "    sudo pacman -S yosys        # Arch\n"
            "    sudo apt install yosys      # Debian/Ubuntu\n"
            "    brew install yosys          # macOS\n"
            "Or skip synthesis and pass an existing netlist with --json."
        ) from None

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Yosys synthesis failed for {verilog_path}.")

    print("  Yosys JSON generated.\n")
    return json_path


# -----------------------------------------------------------------------------
# Interactive single-fault mode
# -----------------------------------------------------------------------------

def mode_interactive(parsed: ParsedNetlist, args) -> None:
    """List the fault sites, prompt for one, and run ATPG on it alone."""
    pi_set = set(parsed.primary_inputs)
    po_set = set(parsed.primary_outputs)
    sites = list(parsed.primary_inputs) + [
        n for n in parsed.gates if n not in pi_set
    ]
    if not sites:
        print("  This circuit has no fault sites.")
        return

    col = max(len(s) for s in sites) + 4

    print()
    print("  " + "━" * 64)
    print("  Available fault sites")
    print("  " + "━" * 64)
    print(f"\n  {'Wire':<{col}}  {'Gate type':<10}  Role")
    print(f"  {'-' * col}  {'-' * 10}  {'-' * 16}")

    for site in sites:
        gate = parsed.gates.get(site)
        gtype = gate.gtype.value if gate else "INPUT"
        role = ("primary input" if site in pi_set
                else "primary output" if site in po_set
                else "internal wire")
        print(f"  {site:<{col}}  {gtype:<10}  {role}")

    print()
    try:
        while True:
            wire = input("  Enter wire name        : ").strip()
            if wire in parsed.gates:
                break
            print(f"  Wire '{wire}' not found. Choose from the list above.")

        while True:
            value = input("  Stuck-at value (0 / 1) : ").strip()
            if value in ("0", "1"):
                stuck_value = int(value)
                break
            print("  Please enter 0 or 1.")
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    print(f"\n  Running ATPG for  :  {wire} / stuck-at-{stuck_value}\n")

    result = run_atpg(
        parsed,
        solver_name=args.solver,
        extract_proof=args.proof,
        verbose=args.verbose,
        verify=not args.no_verify,
        fault_filter=[(wire, stuck_value)],
    )

    if not args.no_visual:
        draw_atpg_run(parsed, result)
    else:
        result.print_summary()

    csv_path = write_circuit_csv(result, args.output_dir)
    if csv_path:
        print(f"\n  Results CSV  →  {csv_path}")


# -----------------------------------------------------------------------------
# Netlist mode (shared by --verilog and --json)
# -----------------------------------------------------------------------------

def run_netlist(parsed: ParsedNetlist, args) -> None:
    """Run ATPG on an already-parsed netlist and write the CSV."""
    print(f"  Module          : {parsed.module_name}")
    print(f"  Primary inputs  : {', '.join(parsed.primary_inputs)}")
    print(f"  Primary outputs : {', '.join(parsed.primary_outputs)}")
    print(f"  Gate count      : {parsed.gate_count}")

    if args.interactive:
        if not args.no_visual:
            draw_circuit(parsed)
        mode_interactive(parsed, args)
        return

    result = run_atpg(
        parsed,
        solver_name=args.solver,
        extract_proof=args.proof,
        verbose=args.verbose,
        verify=not args.no_verify,
    )

    if not args.no_visual:
        if args.force_visual or result.total_faults <= VIZ_FAULT_LIMIT:
            draw_atpg_run(parsed, result)
        else:
            print(f"\n  ({result.total_faults} fault sites — per-fault "
                  f"visualisation suppressed; pass --force-visual to print it)")
            print()
            result.print_summary()
    else:
        print()
        result.print_summary()

    csv_path = write_circuit_csv(result, args.output_dir)
    if csv_path:
        print(f"\n  Results CSV  →  {csv_path}")


def mode_verilog(args) -> None:
    json_path = run_yosys(
        args.verilog,
        use_liberty=not args.no_liberty,
        top=args.top,
        jsons_dir=args.jsons_dir,
    )
    run_netlist(parse_yosys_file(json_path), args)


def mode_json(args) -> None:
    print(f"  Netlist JSON  : {args.json}\n")
    run_netlist(parse_yosys_file(args.json, module_name=args.top), args)


# -----------------------------------------------------------------------------
# Benchmark mode
# -----------------------------------------------------------------------------

def mode_bench(args) -> None:
    os.makedirs(args.output_dir, exist_ok=True)

    if args.interactive:
        # Interactive mode tests one fault in one circuit.
        if "all" in args.bench:
            raise ValueError(
                "--interactive needs a single circuit name, not 'all'. "
                f"Available: {', '.join(available_circuits(args.bench_dir))}"
            )
        if len(args.bench) > 1:
            print(f"  --interactive uses one circuit; taking {args.bench[0]} "
                  f"and ignoring {', '.join(args.bench[1:])}.")
        name = args.bench[0]
        parsed = load_circuit(name, args.bench_dir)
        if parsed is None:
            return
        if not args.no_visual:
            draw_circuit(parsed)
        mode_interactive(parsed, args)
        return

    run_suite(
        names=args.bench,
        bench_dir=args.bench_dir,
        output_dir=args.output_dir,
        solver=args.solver,
        extract_proof=args.proof,
        visualize=not args.no_visual,
        verbose=args.verbose,
        verify=not args.no_verify,
        force_visual=args.force_visual,
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="SAT-ATPG — stuck-at test pattern generation via SAT solving",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --bench c17
  python main.py --bench c17 c432 c880
  python main.py --bench all
  python main.py --bench c17 --interactive
  python main.py --verilog benchmarks/c17.v
  python main.py --verilog design.v --no-liberty
  python main.py --json jsons/eval_techmap.json
""",
    )

    source = p.add_argument_group("circuit source (pick at least one)")
    source.add_argument(
        "--bench", metavar="NAME", nargs="+",
        help="ISCAS85 circuit names (e.g. c17 c432), or 'all' for every "
             "circuit with a .bench file.",
    )
    source.add_argument(
        "--verilog", metavar="FILE.v",
        help="Verilog source file. Yosys synthesises it to a JSON netlist first.",
    )
    source.add_argument(
        "--json", metavar="FILE.json",
        help="An existing Yosys JSON netlist. Skips synthesis, so Yosys is not "
             "needed.",
    )

    run = p.add_argument_group("run options")
    run.add_argument(
        "--interactive", "--inter", action="store_true", dest="interactive",
        help="Prompt for one wire and stuck-at value, and test only that fault.",
    )
    run.add_argument(
        "--solver", default="g4", choices=sorted(SOLVERS),
        help="PySAT solver (default: g4, Glucose 4).",
    )
    run.add_argument(
        "--proof", action="store_true",
        help="Collect DRUP proof lines for UNSAT results. Slower.",
    )
    run.add_argument(
        "--no-verify", action="store_true",
        help="Skip simulating each test vector to confirm it exposes its fault. "
             "The check is cheap and catches encoding errors; only skip it when "
             "benchmarking raw solve time.",
    )
    run.add_argument(
        "--no-liberty", action="store_true",
        help="Verilog mode: skip Nangate cell mapping and use generic synthesis.",
    )
    run.add_argument(
        "--top", metavar="MODULE",
        help="Name of the top module. Default: let Yosys choose (--verilog), "
             "or use the module with the most cells (--json).",
    )

    out = p.add_argument_group("output options")
    out.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print one line per fault while solving.",
    )
    out.add_argument(
        "--no-visual", action="store_true",
        help="Skip the terminal circuit drawing; print the summary only.",
    )
    out.add_argument(
        "--force-visual", action="store_true",
        help=f"Draw every fault even above {VIZ_FAULT_LIMIT} fault sites.",
    )
    out.add_argument("--bench-dir", default=BENCH_DIR,
                     help=f"Directory of .bench files (default: {BENCH_DIR}).")
    out.add_argument("--output-dir", default=OUTPUT_DIR,
                     help=f"CSV output directory (default: {OUTPUT_DIR}).")
    out.add_argument("--jsons-dir", default=JSONS_DIR,
                     help=f"Where Yosys JSON is written (default: {JSONS_DIR}).")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Catch an unusable solver/proof pairing once here, rather than letting
    # every individual fault fail with the same message.
    if args.proof and args.solver not in PROOF_CAPABLE:
        parser.error(
            f"--proof needs a solver that can emit DRUP proofs; "
            f"'{args.solver}' cannot. Use one of: "
            f"{', '.join(sorted(PROOF_CAPABLE))}."
        )

    if not (args.bench or args.verilog or args.json):
        parser.print_help()
        print("\n  Available benchmark circuits: "
              f"{', '.join(available_circuits(args.bench_dir)) or '(none found)'}")
        return 0

    os.makedirs(args.output_dir, exist_ok=True)

    try:
        if args.verilog:
            mode_verilog(args)
        if args.json:
            mode_json(args)
        if args.bench:
            mode_bench(args)
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Interrupted.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
