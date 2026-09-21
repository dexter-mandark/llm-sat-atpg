"""Netlist front-ends: Yosys JSON and ISCAS .bench, both producing ParsedNetlist."""

from .yosysModels import (
    BitID,
    Gate,
    GateType,
    ParsedNetlist,
    PortDirection,
    YosysCell,
    YosysModule,
    YosysNetlist,
)

from .benchParser import parse_bench_file, parse_bench_string
from .yosysParser import parse_yosys_file, parse_yosys_netlist

__all__ = [
    "BitID",
    "Gate",
    "GateType",
    "ParsedNetlist",
    "PortDirection",
    "YosysCell",
    "YosysModule",
    "YosysNetlist",
    "parse_bench_file",
    "parse_bench_string",
    "parse_yosys_file",
    "parse_yosys_netlist",
]
