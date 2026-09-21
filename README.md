# VerifyTheseNuts

SAT-based ATPG (Automatic Test Pattern Generation) for digital logic circuits.

A manufactured chip can come off the line with a wire shorted to power or
ground, so that it reads permanently 1 or permanently 0 no matter what drives
it. That is a **stuck-at fault**. To catch one you need an input pattern that
makes the broken circuit produce a different output from a working one. This
tool finds those patterns — or proves that none exists — by turning the question
into Boolean satisfiability and handing it to a SAT solver.

## What it does

For every wire in a circuit, and for both stuck-at-0 and stuck-at-1:

1. **Parses** the netlist — ISCAS85 `.bench` files, or Yosys JSON from your own
   Verilog (generic gates or Nangate 45nm standard cells, with complex cells
   like `AOI21` / `OAI21` / `MUX2` decomposed into primitives).
2. **Encodes** two copies of the circuit into CNF via the Tseitin
   transformation — one fault-free, one with the fault injected — sharing the
   same primary inputs.
3. **Builds a miter**: asserts that at least one primary output differs between
   the two copies.
4. **Solves** it with PySAT (Glucose, CaDiCaL or MiniSat).
   - **SAT** → the satisfying assignment *is* a test vector that detects the fault.
   - **UNSAT** → the fault is redundant; no input pattern can ever expose it.
5. **Verifies** every test vector by simulating the good and faulty circuits
   independently and confirming the outputs really differ. A test vector nobody
   simulates is a claim, not a result.
6. **Reports** fault coverage, per-fault solver statistics (decisions,
   conflicts, propagations, restarts, solve time), a colour-coded terminal view
   of the fault propagating through the gates, and CSV files per circuit plus a
   cross-circuit summary.

Undetectable faults are the interesting output as much as the detectable ones:
they pinpoint redundant logic. Of the shipped benchmarks, `c432` has 3 and
`c1908` has 2; the rest are fully testable.

## Requirements

- Python 3.10+
- `python-sat` and `pydantic` (see `requirements.txt`)
- **Yosys** — only for `--verilog`. Not needed for `--bench` or `--json`.
  `sudo pacman -S yosys` / `sudo apt install yosys` / `brew install yosys`

## Setup

```bash
python3 -m venv .verify
source .verify/bin/activate
pip install -r requirements.txt
```

## Running it

Run one ISCAS85 benchmark:

```bash
python main.py --bench c17
```

Several, or the whole suite:

```bash
python main.py --bench c17 c432 c880
python main.py --bench all
```

Your own Verilog — Yosys synthesises it to a netlist first:

```bash
python main.py --verilog design.v              # mapped to Nangate 45nm cells
python main.py --verilog design.v --no-liberty # generic gates instead
```

An existing Yosys JSON netlist, skipping synthesis:

```bash
python main.py --json examples/c17_nangate.json
```

Test a single fault you choose, instead of all of them:

```bash
python main.py --bench c17 --interactive
```

Run `python main.py` with no arguments for the full option list and the
benchmark circuits currently available.

### Useful options

| Option | Effect |
|---|---|
| `--solver {g3,g4,cd,cd15,m22,mgh,lgl,mcb,mpl}` | SAT solver; default `g4` (Glucose 4) |
| `--verbose` | One line per fault as it is solved |
| `--no-visual` | Skip the circuit drawing, print the summary only |
| `--force-visual` | Draw every fault even on large circuits |
| `--no-verify` | Skip the simulation cross-check (only for timing the solver) |
| `--proof` | Collect DRUP proof lines for UNSAT results |
| `--top MODULE` | Name the top module explicitly |
| `--bench-dir` / `--output-dir` / `--jsons-dir` | Override the default paths |

## Output

Results go to `results/`:

- `<circuit>_results.csv` — one row per fault: the fault site, whether it was
  detected, the test vector, whether that vector was verified, and the solver
  statistics for that call.
- `benchmark_summary.csv` — one row per circuit: fault coverage, totals, and
  average/max solver effort.

The terminal view shows each gate with its good and faulty value side by side,
so you can follow the fault effect from the stuck wire to the output that
exposes it.

## Layout

```
main.py             CLI entry point
atpgEngine.py       the per-fault loop, results, and the verification pass
miter.py            two circuit copies + fault injection + output-difference assertion
cnfEncoder.py       Tseitin CNF encoding, one SAT variable per wire
satSolver.py        PySAT wrapper and metric extraction
simulator.py        logic simulator used to check every SAT result
circuitViz.py       terminal circuit and fault rendering
runBenchmarks.py    benchmark suite runner and CSV writers
parser/             .bench and Yosys JSON front-ends, Nangate cell mapping
benchmarks/         ISCAS85 circuits (.bench and .v)
examples/           sample Yosys netlists, for trying --json without Yosys
```
