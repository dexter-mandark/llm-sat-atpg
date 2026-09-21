# yosysModels.py
# Pydantic v2 models that map 1-to-1 onto the Yosys JSON schema.
# Reference: https://yosyshq.readthedocs.io/projects/yosys/en/latest/

from __future__ import annotations

import json
from enum import Enum
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, PrivateAttr, model_validator


#-----------------------------------------------------------------------------
#  Primitive types
#-----------------------------------------------------------------------------

class PortDirection(str, Enum):
    """Direction of a module-level port or a cell port."""
    INPUT  = "input"
    OUTPUT = "output"
    INOUT  = "inout"


# A bit in Yosys JSON is either an integer wire-ID or a constant string.
# Yosys writes "0"/"1" for tie-offs and also "x" (don't-care) / "z" (high-Z)
# for unconnected pins, so all four have to validate.
BitID = Union[int, Literal["0", "1", "x", "z", "X", "Z"]]


class GateType(str, Enum):
    """
    Canonical gate types used by the ATPG pipeline.
    Both Yosys internal primitives ($_AND_) and liberty cells (AND2_X1)
    are normalised into one of these values by the parser.
    """
    # INPUT is just a placeholder for primary inputs; not an actual Yosys cell type
    INPUT   = "INPUT" 
    # OUTPUT is a placeholder for primary ouputs 
    OUTPUT  = "OUTPUT"
    AND     = "AND"
    OR      = "OR"
    NOT     = "NOT"
    NAND    = "NAND"
    NOR     = "NOR"
    XOR     = "XOR"
    XNOR    = "XNOR"
    ANDNOT = "ANDNOT"   # Y = A AND NOT(B)
    ORNOT  = "ORNOT"    # Y = A OR NOT(B)
    # Constant tie-offs. A cell input wired to a literal 0/1 becomes one of
    # these, so the encoder can pin the wire instead of leaving it free.
    CONST0  = "CONST0"
    CONST1  = "CONST1"
    # Buffer, does absolutely nothing — used for clock buffers, power-gating cells, etc.
    BUFF    = "BUFF" 
    # D flip-flop, 1 bit memory element. 
    #Both positive and negative edge-triggered DFFs are normalised to this type.
    DFF     = "DFF"
    # For any cell types that don't fit into the above categories (e.g. black-box cells),
    UNKNOWN = "UNKNOWN"


#-----------------------------------------------------------------------------
#  Yosys JSON TopLevel Models
#-----------------------------------------------------------------------------

class YosysModule(BaseModel):
    """
    One module inside the top-level Yosys JSON.
    Each module has its own ports, cells, and netnames.

    --------------
    {
        "ports":    { <port_name>: YosysPort, ... },
        "cells":    { <cell_name>: YosysCell, ... },
        "netnames": { <net_name>:  YosysNetname, ... }
    }
    """
    ports:    Dict[str, YosysPort]    = Field(default_factory=dict)
    cells:    Dict[str, YosysCell]    = Field(default_factory=dict)
    netnames: Dict[str, YosysNetname] = Field(default_factory=dict)

    @property
    def primary_input_names(self) -> List[str]:
        """Names of all input ports, in declaration order."""
        return [name for name, port in self.ports.items()
                if port.direction == PortDirection.INPUT]

    @property
    def primary_output_names(self) -> List[str]:
        """Names of all output ports, in declaration order."""
        return [name for name, port in self.ports.items()
                if port.direction == PortDirection.OUTPUT]

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def net_count(self) -> int:
        return len(self.netnames)


class YosysNetlist(BaseModel):
    """
    Top-level object produced by:
        yosys -p "... write_json <file>"
    Represents the entire JSON file, which may contain multiple modules.
    {
        "creator": "Yosys 0.40 ...",
        "modules": {
            "<module_name>": YosysModule,
            ...
        }
    }

    Usage
        netlist = YosysNetlist.from_file("netlist_mapped.json")
        module  = netlist.top_module
    """
    creator: Optional[str] = None
    modules: Dict[str, YosysModule]

    @classmethod
    def from_file(cls, filepath: str) -> "YosysNetlist":
        """Load and validate a Yosys-generated JSON file."""
        with open(filepath) as f:
            return cls.model_validate(json.load(f))

    @classmethod
    def from_json_string(cls, json_str: str) -> "YosysNetlist":
        """Load and validate from a raw JSON string."""
        return cls.model_validate(json.loads(json_str))

    @property
    def top_module(self) -> YosysModule:
        """
        The first module in the JSON — the design top after flattening.
        For multi-module JSON (no -flatten), iterate self.modules directly.
        """
        return next(iter(self.modules.values()))

    @property
    def top_module_name(self) -> str:
        return next(iter(self.modules.keys()))

    @property
    def module_names(self) -> List[str]:
        return list(self.modules.keys())

#-----------------------------------------------------------------------------
# Yosys Module specific models (ports, cells, netnames)
#-----------------------------------------------------------------------------

class YosysPort(BaseModel):
    """
    Represents a primary input, output, or inout of the module.
    Has a direction and one or more bits (for buses).
    ------------
    "a": { "direction": "input", "bits": [2] }
    "data": { "direction": "input", "bits": [10, 11, 12, 13] }
    """
    direction: PortDirection
    bits: List[BitID]

    @property
    def is_bus(self) -> bool:
        """True when the port is a multi-bit vector / bus."""
        return len(self.bits) > 1

    @property
    def width(self) -> int:
        """Bit-width of the port."""
        return len(self.bits)


class YosysCell(BaseModel):
    """
    Represents a single gate instance after synthesis.
    Each cell has a type, a set of ports with directions, and connections to bits.
    ------------
    - `parameters` and `attributes` are optional Yosys fields;
    they are preserved but not used by the parser.
    - `port_directions` may be absent for black-box cells;
      the parser falls back to an output-name heuristic in that case.
    ------------
    "g1": {
        "type": "AND2_X1",
        "port_directions": { "A": "input", "B": "input", "ZN": "output" },
        "connections":     { "A": [2],     "B": [3],     "ZN": [5] }
    }
    """
    type: str
    port_directions: Dict[str, PortDirection] = Field(default_factory=dict)
    connections:     Dict[str, List[BitID]]   = Field(default_factory=dict)
    # Yosys writes parameter/attribute values as either strings or ints
    # depending on the parameter, so neither is pinned to one type here.
    parameters:      Dict[str, Union[str, int]] = Field(default_factory=dict)
    attributes:      Dict[str, Union[str, int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_connections_covered(self) -> "YosysCell":
        """
        every port in connections should appear in port_directions.
        only warn (don't raise) because black-box cells may omit directions.
        """
        if self.port_directions:
            unknown = set(self.connections) - set(self.port_directions)
            if unknown:
                # Store for debugging; do not raise — allow black-box cells
                object.__setattr__(self, "_undirected_ports", unknown)
        return self

    @property
    def output_ports(self) -> Dict[str, List[BitID]]:
        """Subset of connections whose direction is OUTPUT."""
        return {
            port_name: bits for port_name, bits in self.connections.items()
            if self.port_directions.get(port_name) == PortDirection.OUTPUT
        }

    @property
    def input_ports(self) -> Dict[str, List[BitID]]:
        """Subset of connections whose direction is INPUT or INOUT."""
        return {
            port_name: bits for port_name, bits in self.connections.items()
            if self.port_directions.get(port_name) != PortDirection.OUTPUT
        }


class YosysNetname(BaseModel):
    """
    Associates a human-readable name with one or more bit-IDs.
    Yosys generates internal netnames for every signal, including primary ports.
    ------------
    "g1_out":      { "bits": [5] }
    "internal_bus":{ "bits": [6, 7, 8, 9], "hide_name": 0 }
    -----
    - hide_name == 1 means the name was auto-generated by Yosys
      (e.g. "$and$design.v$1_Y"). These are usually skipped in display.
    - A net that is also a primary port will appear in both "ports"
      and "netnames" with the same bit-IDs.
    """
    bits:      List[BitID]
    hide_name: int = 0

    @property
    def is_hidden(self) -> bool:
        """True for Yosys-generated internal names."""
        return self.hide_name == 1


#-----------------------------------------------------------------------------
#  Parser output models
#-----------------------------------------------------------------------------

class Gate(BaseModel):
    """
    Canonical gate representation used by the ATPG / simulation pipeline.

    Keyed by the signal name the gate *drives* (its output wire), NOT by the
    Yosys cell-instance name. This allows O(1) lookup of "which gate drives
    signal X?" — the core operation in fault simulation and ATPG.

    Fields
    ------
    name   : output signal name  (e.g. "g1_out", "y", "a")
    gtype  : canonical gate type (GateType enum)
    inputs : ordered list of input signal names
    """
    name:   str
    gtype:  GateType
    inputs: List[str] = Field(default_factory=list)

    @property
    def is_primary_input(self) -> bool:
        return self.gtype == GateType.INPUT

    @property
    def is_primary_output(self) -> bool:
        return self.gtype == GateType.OUTPUT

    @property
    def fanin(self) -> int:
        """Number of input signals."""
        return len(self.inputs)



class ParsedNetlist(BaseModel):
    """
    Fully parsed, ATPG-ready netlist produced by yosysParser

    Fields
    ------
    module_name     : name of the parsed Yosys module
    gates           : signal_name -> Gate (complete gate graph)
    primary_inputs  : ordered PI signal names  (match Verilog port order)
    primary_outputs : ordered PO signal names  (match Verilog port order)

    Methods
    -------
    gate_count  — number of logic gates (excludes INPUT pseudo-gates)
    fanout()    — gates that consume a given signal
    topo_order()— Kahn BFS topological sort, PIs first, POs last
    """
    module_name:     str
    gates:           Dict[str, Gate]
    primary_inputs:  List[str]
    primary_outputs: List[str]

    # Derived structure, rebuilt on demand. A ParsedNetlist is built once by a
    # parser and then read-only; call invalidate_caches() if you ever mutate
    # `gates` after construction.
    _topo_cache:   Optional[List["Gate"]]      = PrivateAttr(default=None)
    _fanout_cache: Optional[Dict[str, List[str]]] = PrivateAttr(default=None)

    def invalidate_caches(self) -> None:
        """Drop the cached topological order and fanout index."""
        self._topo_cache   = None
        self._fanout_cache = None

    @property
    def gate_count(self) -> int:
        return sum(1 for gate in self.gates.values()
                   if not gate.is_primary_input and not gate.is_primary_output)

    def fanout(self, signal: str) -> List[str]:
        """
        Return output-signal names of every gate that has `signal` as an input.
        O(n) — build a reverse-adjacency dict if calling this in a hot loop.
        """
        return list(self.build_fanout_index().get(signal, []))

    def build_fanout_index(self) -> Dict[str, List[str]]:
        """
        Pre-build a full signal -> [consumer gates] index.
        Call once; reuse for all fanout queries during simulation.
        """
        if self._fanout_cache is not None:
            return self._fanout_cache
        index: Dict[str, List[str]] = {k: [] for k in self.gates}
        for gate in self.gates.values():
            for inp in gate.inputs:
                if inp in index:
                    index[inp].append(gate.name)
        self._fanout_cache = index
        return index

    def dangling_inputs(self) -> Dict[str, List[str]]:
        """
        Gate inputs that no gate in this netlist drives.
        A non-empty result means the netlist is incomplete — the encoder would
        turn each such wire into an unconstrained free SAT variable, which
        silently invalidates every result that depends on it.
        """
        missing: Dict[str, List[str]] = {}
        for gate in self.gates.values():
            absent = [inp for inp in gate.inputs if inp not in self.gates]
            if absent:
                missing[gate.name] = absent
        return missing

    def topo_order(self) -> List[Gate]:
        """
        Kahn's BFS topological sort.
        Returns gates in a valid evaluation order:
          PIs (fanin=0) first -> combinational logic -> POs last.
        Raises ValueError on genuine combinational loops.

        The result is cached: build_miter() sorts the same netlist twice per
        fault, so recomputing it per call dominated runtime on large circuits.
        """
        if self._topo_cache is not None:
            return self._topo_cache

        from collections import deque

        # Only count dependencies that actually exist in this netlist.
        # Counting absent drivers (constants, undeclared wires) made their
        # consumers unreachable and reported a phantom combinational loop.
        in_deg = {
            name: sum(1 for inp in gate.inputs if inp in self.gates)
            for name, gate in self.gates.items()
        }
        consumers = self.build_fanout_index()

        queue = deque(n for n, d in in_deg.items() if d == 0)
        order: List[Gate] = []

        while queue:
            name = queue.popleft()
            order.append(self.gates[name])
            for consumer in consumers[name]:
                in_deg[consumer] -= 1
                if in_deg[consumer] == 0:
                    queue.append(consumer)

        if len(order) != len(self.gates):
            remaining = set(self.gates) - {g.name for g in order}
            raise ValueError(
                f"Combinational loop detected involving {len(remaining)} "
                f"signal(s): {sorted(remaining)[:10]}"
            )

        self._topo_cache = order
        return order
