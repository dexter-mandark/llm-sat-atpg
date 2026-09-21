# cnfEncoder.py
# Tseitin CNF encoder for Netlists
#
# Converts a ParsedNetlist into a CNF (Conjunctive Normal Form) formula
# suitable for SAT solver input.
#
# Tseitin transformation guarantees:
#   - One unique SAT variable per wire/signal
#   - Linear blowup in clauses (not exponential like naive truth-table CNF)
#   - Equisatisfiable with the original circuit (same solutions)

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from parser.yosysModels import Gate, GateType, ParsedNetlist


# -------------------------------------------------------------------------
#  Pydantic output model
# -------------------------------------------------------------------------

class CNFFormula(BaseModel):
    """
    A CNF formula produced by the Tseitin encoder.

    Fields
    ------
    var_map   : signal_name to SAT variable integer mapping (1-indexed)
    clauses   : list of clauses; each clause is a list of signed ints
                positive = variable is TRUE, negative = variable is FALSE
    num_vars  : total number of SAT variables allocated
    prefix    : the prefix used when building this formula (for miter setups)

    DIMACS format
    -------------
    Each clause [1, -3, 5] means: (x1 OR NOT x3 OR x5)
    """
    var_map:   Dict[str, int]       = Field(default_factory=dict)
    clauses:   List[List[int]]      = Field(default_factory=list)
    num_vars:  int                  = 0
    prefix:    str                  = ""

    @property
    def num_clauses(self) -> int:
        return len(self.clauses)

    def to_dimacs(self) -> str:
        """
        Serialize the formula to DIMACS CNF format.
        Feed directly to MiniSAT, Glucose, CaDiCaL, or pysat.
        """
        lines = [f"p cnf {self.num_vars} {self.num_clauses}"]
        for clause in self.clauses:
            lines.append(" ".join(map(str, clause)) + " 0")
        return "\n".join(lines)

    def signal_var(self, signal_name: str) -> int:
        """Return the SAT variable integer for a given signal name."""
        key = f"{self.prefix}{signal_name}"
        if key not in self.var_map:
            raise KeyError(
                f"Signal '{signal_name}' (key '{key}') not in var_map. "
                f"Was encode_circuit() called?"
            )
        return self.var_map[key]


# -------------------------------------------------------------------------
#  Tseitin CNF Encoder
# -------------------------------------------------------------------------

class CNFEncoder:
    """
    Tseitin transformation for netlists.

    Each gate output wire gets a unique integer SAT variable (1-indexed).
    The prefix parameter allows building two copies of the same circuit
    with distinct, non-overlapping variable ranges — needed for ATPG miters.

    Tseitin clauses per gate type
    -----------------------------
    For each gate:  o  =  f(i1, i2, ..., in)

    AND:   o <-> (i1 & i2 & ...) --->
               (-o | i1), (-o | i2), ...   [o=1 forces all inputs=1]
               (o | -i1 | -i2 | ...)       [all inputs=1 forces o=1]

    OR:    o <-> (i1 | i2 | ...) --->
               (o | -i1), (o | -i2), ...   [any input=1 forces o=1]
               (-o | i1 | i2 | ...)        [o=1 requires at least one input=1]

    NOT:   o <-> ~i1 --->
               (-o | -i1), (o | i1)

    NAND:  o = NOT(AND(...))  --->  negate AND clauses
    NOR:   o = NOT(OR(...))   --->  negate OR  clauses

    XOR (2-input):   o <-> (a exor b) ---->
               (-o | a | b), (-o | -a | -b),
               (o | -a | b),  (o | a | -b)
    XOR (N-input):  chain pairwise — tmp = XOR(i0, i1), o = XOR(tmp, i2), ...

    XNOR:  o = NOT(XOR(...)) — same chain, negate final variable
    """

    def __init__(self, prefix: str = "", start_var: int = 1,
                 strict: bool = True):
        self.prefix    = prefix
        self.var_map:  Dict[str, int]  = {}   # prefixed_name -> variable int
        self.next_var: int = start_var
        self.clauses:  List[List[int]] = []
        # strict=True refuses to encode a gate whose function is unknown.
        # See encode_gate() for why the permissive path is unsafe.
        self.strict    = strict

    # -- Variable allocation

    def get_var(self, signal_name: str) -> int:
        """
        Return the SAT variable for a signal name, allocating a new one
        if this signal hasn't been seen before.
        Variable IDs are 1-indexed (DIMACS convention).
        """
        key = f"{self.prefix}{signal_name}"
        if key not in self.var_map:
            self.var_map[key] = self.next_var
            self.next_var += 1
        return self.var_map[key]

    def new_aux_var(self) -> int:
        """
        Allocate a fresh auxiliary variable (no signal name).
        Used for intermediate wires in chained XOR/XNOR encodings.
        """
        var = self.next_var
        self.next_var += 1
        return var

    # -- Clause helpers 

    def _add(self, *clauses: List[int]) -> None:
        """Add one or more clauses to the formula."""
        for c in clauses:
            self.clauses.append(list(c))

    def assert_signal(self, signal_name: str, value: int) -> None:
        """
        Force a signal to a fixed value (0 or 1) by adding a unit clause.
        Used to:
          - Set primary input values for a test vector
          - Inject a stuck-at fault (assert faulty signal = 0 or 1)
          - Assert the miter output = 1 (force a difference)
        """
        var = self.get_var(signal_name)
        self._add([var if value == 1 else -var])

    # -- Gate encoding 

    def encode_gate(self, gate: Gate) -> None:
        """
        Add Tseitin clauses for a single gate.
        The output variable is gate.name; inputs are gate.inputs.
        """
        o  = self.get_var(gate.name)
        ii = [self.get_var(inp) for inp in gate.inputs]
        gt = gate.gtype

        # INPUT / OUTPUT — free variables, no constraints on their value
        if gt in (GateType.INPUT, GateType.OUTPUT):
            return

        elif gt == GateType.CONST0:
            self._add([-o])

        elif gt == GateType.CONST1:
            self._add([o])

        elif gt in (GateType.BUFF, GateType.DFF):
            # Buffer and DFF (in combinational mode): o <-> i
            a = ii[0]
            self._add([-o, a], [o, -a])

        elif gt == GateType.NOT:
            # o <-> ~a
            a = ii[0]
            self._add([-o, -a], [o, a])

        elif gt == GateType.AND:
            # o=1 forces all inputs=1
            for a in ii:
                self._add([-o, a])
            # all inputs=1 forces o=1
            self._add([o] + [-a for a in ii])

        elif gt == GateType.NAND:
            # o = NOT(AND) — negate AND clauses
            # o=0 forces all inputs=1
            for a in ii:
                self._add([o, a])
            # all inputs=1 forces o=0
            self._add([-o] + [-a for a in ii])

        elif gt == GateType.OR:
            # any input=1 forces o=1
            for a in ii:
                self._add([o, -a])
            # o=1 requires at least one input=1
            self._add([-o] + ii)

        elif gt == GateType.NOR:
            # o = NOT(OR) — negate OR clauses
            # o=1 forces all inputs=0
            for a in ii:
                self._add([-o, -a])
            # all inputs=0 forces o=1
            self._add([o] + ii)

        elif gt == GateType.XOR:
            self._encode_xor(o, ii)

        elif gt == GateType.XNOR:
            # XNOR = NOT(XOR): encode XOR into a temp var, then invert
            if len(ii) == 2:
                a, b = ii
                # o <-> NOT(a xor b)
                self._add([-o,  a, -b], [-o, -a,  b],
                           [ o,  a,  b], [ o, -a, -b])
            else:
                xor_var = self._encode_xor_chain(ii)
                # o <-> NOT(xor_var)
                self._add([-o, -xor_var], [o, xor_var])
        elif gt == GateType.ANDNOT:
            # Y = A AND NOT(B)  ←→  Y = AND(A, NOT(B))
            a, b = ii[0], ii[1]
            self._add([-o, a], [-o, -b], [o, -a, b])

        elif gt == GateType.ORNOT:
            # Y = A OR NOT(B)  ←→  Y = OR(A, NOT(B))
            a, b = ii[0], ii[1]
            self._add([o, -a], [o, b], [-o, a, -b])

        elif gt == GateType.UNKNOWN:
            # An unknown cell has no Tseitin clauses, so its output stays a
            # free variable. The good and faulty copies of the circuit can then
            # disagree for free, and EVERY fault downstream of it is reported
            # as detected — a silently wrong 100% coverage number.
            # Refuse by default; let callers opt in explicitly.
            if self.strict:
                raise ValueError(
                    f"Gate '{gate.name}' has an unknown function "
                    f"(GateType.UNKNOWN), so it cannot be encoded soundly.\n"
                    f"Its output would become an unconstrained SAT variable "
                    f"and every fault routed through it would be reported as "
                    f"detected.\n"
                    f"Fix: add the cell type to parser/nangateMap.py "
                    f"(_SIMPLE or decompose_complex) or to CELL_TYPE_MAP in "
                    f"parser/yosysParser.py.\n"
                    f"To accept unsound results anyway, build the encoder "
                    f"with strict=False."
                )

        else:
            raise ValueError(
                f"Unhandled GateType '{gt}' for gate '{gate.name}'. "
                f"Add it to CNFEncoder.encode_gate()."
            )

    def _encode_xor(self, o: int, ii: List[int]) -> None:
        """Encode o = XOR(ii) into Tseitin clauses."""
        if len(ii) == 2:
            a, b = ii
            self._add([-o, a,  b], [-o, -a, -b],
                      [ o, -a, b], [ o,  a, -b])
        else:
            xor_var = self._encode_xor_chain(ii)
            self._add([-o, xor_var], [o, -xor_var])

    def _encode_xor_chain(self, ii: List[int]) -> int:
        """
        Encode a multi-input XOR by chaining 2-input XORs.
        Returns the variable representing the final XOR output.
        e.g. XOR(a,b,c) = XOR(XOR(a,b), c)
        """
        cur = ii[0]
        for nxt in ii[1:]:
            tmp = self.new_aux_var()
            self._add([-tmp,  cur,  nxt], [-tmp, -cur, -nxt],
                      [ tmp, -cur,  nxt], [ tmp,  cur, -nxt])
            cur = tmp
        return cur

    # -- Circuit-level encoding

    def encode_circuit(self, netlist: ParsedNetlist) -> "CNFEncoder":
        """
        Encode the full circuit into CNF by processing gates
        in topological order (PIs first, POs last).

        Returns self for chaining:
            enc = CNFEncoder().encode_circuit(parsed)

        Raises ValueError (strict mode) if any gate reads a wire that nothing
        in the netlist drives — such a wire becomes an unconstrained variable
        and quietly invalidates the results that depend on it.
        """
        if self.strict:
            dangling = netlist.dangling_inputs()
            if dangling:
                shown = list(dangling.items())[:5]
                detail = "; ".join(f"{g} reads {ins}" for g, ins in shown)
                more = "" if len(dangling) <= 5 else f" (+{len(dangling) - 5} more)"
                raise ValueError(
                    f"{len(dangling)} gate(s) read undriven wires: "
                    f"{detail}{more}.\n"
                    f"Undriven wires become free SAT variables and make the "
                    f"result meaningless. Check that the parser resolved every "
                    f"cell connection, or build the encoder with strict=False."
                )

        for gate in netlist.topo_order():
            self.encode_gate(gate)
        return self

    # -- Output

    def build(self) -> CNFFormula:
        """
        Freeze the current state into a CNFFormula Pydantic object.
        Safe to call multiple times; returns a snapshot.
        """
        return CNFFormula(
            var_map   = dict(self.var_map),
            clauses   = [list(c) for c in self.clauses],
            num_vars  = self.next_var - 1,
            prefix    = self.prefix,
        )

    def to_dimacs(self) -> str:
        """Convenience: serialize directly to DIMACS string."""
        return self.build().to_dimacs()

