"""Read a generated top-level module and work out what really drives each bit.

This does not understand SystemVerilog in general. It reads what the two generators write: a module
header, net declarations, assign statements and instances. A line it cannot follow is reported as
UNPARSED rather than guessed at, so a comparison can't quietly pass on something it did not read.

The result maps each input bit of each instance, and each output bit of the module, to its source:

    "uart.paddr_i[3]" -> "bus.m0_paddr_o[3]"     an instance output
    "cpu.irq_i[0]"    -> "~uart.irq_o[0]"        through an inversion
    "gpio.clk_i[0]"   -> "top:clk_i[0]"          a module input
    "m.err_i[0]"      -> "0"                     a constant
    "m.err_i[0]"      -> "OPEN"                  nothing

Inouts have no direction to follow, so each inout bit maps to the alphabetically first bit it shares
a net with: two ports that share a net get the same answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_KEYWORDS = {"module", "endmodule", "assign", "input", "output", "inout", "logic", "wire", "package", "endpackage"}
_DECLARATION = re.compile(r"\b(?:logic|wire|\w+::\w+)\s*(\[[^\]]+\])?\s*(\w+)\s*;")
_HEADER_PORT = re.compile(r"\b(input|output|inout)\s+(?:logic\s+|wire\s+)?(?:\w+::\w+\s*)?(\[[^\]]+\])?\s*(\w+)")
_ASSIGN = re.compile(r"assign\s+([^=]+?)\s*=\s*([^;]+);")
_INSTANCE_START = re.compile(r"\b(\w+)\s*(#\s*\(|\w+\s*\()")
_PIN = re.compile(r"\.(\w+)\s*\(([^()]*)\)")
_SLICE = re.compile(r"^(\w+)(?:\[\s*(\d+)(?:\s*:\s*(\d+))?\s*\])?$")
_INDEXED_SLICE = re.compile(r"^(\w+)\[\s*(\d+)\s*\+:\s*(\d+)\s*\]$")  # how PeakRDL-socgen writes one
_LITERAL = re.compile(r"^(?:(\d+)?'[sS]?([bBoOdDhH])([0-9a-fA-F_]+)|(\d+))$")
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}


def _bounds(text: Optional[str]) -> Tuple[int, int]:
    if not text:
        return 0, 0
    left, right = text.strip("[] ").split(":")
    return int(left), int(right)


def _bit_list(left: int, right: int) -> List[int]:
    """Bits from least to most significant as this slice lists them."""
    step = 1 if left >= right else -1
    return list(range(right, left + step, step))


@dataclass
class Module:
    name: str = ""
    widths: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    top_inputs: List[str] = field(default_factory=list)
    top_outputs: List[str] = field(default_factory=list)
    top_inouts: List[str] = field(default_factory=list)
    sources: Dict[Tuple[str, int], object] = field(default_factory=dict)
    pins: List[Tuple[str, str, str]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    unparsed_assigns: List[Tuple[str, str]] = field(default_factory=list)  # (left, right), also in problems

    def bits(self, name: str) -> List[int]:
        return _bit_list(*self.widths.get(name, (0, 0)))


def _expression(module: Module, text: str) -> Optional[Tuple[bool, object]]:
    """(inverted, [(net, bit)...] from least significant) or a constant, or None if unreadable."""
    text = text.strip()
    inverted = text.startswith("~")
    if inverted:
        text = text[1:].strip()
    if text in ("'0", "'1"):
        return inverted, ("all", int(text[1]))
    literal = _LITERAL.match(text)
    if literal:
        if literal.group(4) is not None:
            return inverted, ("const", int(literal.group(4)))
        return inverted, ("const", int(literal.group(3).replace("_", ""), _BASES[literal.group(2).lower()]))
    indexed = _INDEXED_SLICE.match(text)
    if indexed:
        low, width = int(indexed.group(2)), int(indexed.group(3))
        return inverted, [(indexed.group(1), bit) for bit in range(low, low + width)]
    match = _SLICE.match(text)
    if not match:
        return None
    name, left, right = match.groups()
    if left is None:
        return inverted, [(name, bit) for bit in module.bits(name)]
    if right is None:
        return inverted, [(name, int(left))]
    return inverted, [(name, bit) for bit in _bit_list(int(left), int(right))]


def _closing(text: str, opening: int) -> int:
    """Where the parenthesis at `opening` closes, or -1."""
    depth = 0
    for index in range(opening, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return index
    return -1


def _instances(body: str) -> List[Tuple[str, str, str]]:
    """Each (module, instance name, port connections). Parameter values can nest parentheses to any depth."""
    found = []
    position = 0
    while True:
        start = _INSTANCE_START.search(body, position)
        if start is None:
            return found
        position = start.start() + len(start.group(1))
        after = start.end()
        if start.group(2).startswith("#"):
            close = _closing(body, after - 1)
            named = re.match(r"\s*(\w+)\s*\(", body[close + 1 :]) if close != -1 else None
            if named is None:
                continue
            name, opening = named.group(1), close + 1 + named.end() - 1
        else:
            name, opening = start.group(2).split("(")[0].strip(), after - 1
        close = _closing(body, opening)
        if close != -1 and re.match(r"\s*;", body[close + 1 :]):
            found.append((start.group(1), name, body[opening + 1 : close]))
            position = close


def _constant_bit(value, index: int) -> str:
    kind, number = value
    if kind == "all":
        return str(number)
    return str((number >> index) & 1)


def parse(text: str, instance_ports: Dict[str, Dict[str, str]]) -> Module:
    """`instance_ports` gives each instance's port directions: {"uart": {"paddr_i": "in", ...}}."""
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    module = Module()
    header = re.search(r"\bmodule\s+(\w+)\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;", text, re.S)
    if header is None:
        module.problems.append("no module header found")
        return module
    module.name = header.group(1)
    for direction, bounds, name in _HEADER_PORT.findall(header.group(2)):
        module.widths[name] = _bounds(bounds)
        {"input": module.top_inputs, "output": module.top_outputs, "inout": module.top_inouts}[direction].append(name)
    body = text[header.end() :]
    for bounds, name in _DECLARATION.findall(body):
        if name not in module.widths:
            module.widths[name] = _bounds(bounds)

    for left, right in _ASSIGN.findall(body):
        sinks = _expression(module, left)
        source = _expression(module, right)
        if sinks is None or source is None or isinstance(sinks[1], tuple):
            module.problems.append(f"UNPARSED assign {left} = {right}")
            module.unparsed_assigns.append((left.strip(), right.strip()))
            continue
        for index, sink in enumerate(sinks[1]):
            if sink in module.sources:
                module.problems.append(f"driven twice: {sink[0]}[{sink[1]}]")
            module.sources[sink] = _source_bit(source, index)

    seen = set()
    for kind, name, pins in _instances(body):
        if kind in _KEYWORDS or name not in instance_ports:
            continue
        seen.add(name)
        for port, expression in _PIN.findall(pins):
            direction = instance_ports[name].get(port)
            if direction is None:
                module.problems.append(f"{name}.{port}: unknown port")
                continue
            module.pins.append((name, port, expression.strip()))
            if direction == "out" and expression.strip():
                nets = _expression(module, expression)
                if nets is None or isinstance(nets[1], tuple) or nets[0]:
                    module.problems.append(f"UNPARSED output pin {name}.{port}({expression})")
                    continue
                for index, sink in enumerate(nets[1]):
                    if sink in module.sources:
                        module.problems.append(f"driven twice: {sink[0]}[{sink[1]}]")
                    module.sources[sink] = (False, ("pin", name, port, index))
    for name in sorted(set(instance_ports) - seen):
        module.problems.append(f"UNPARSED instance {name}: not found in the module")
    return module


def _source_bit(source, index: int):
    inverted, value = source
    if isinstance(value, tuple):
        return inverted, ("constant", _constant_bit(value, index))
    if index >= len(value):
        return inverted, ("constant", "0")
    return inverted, ("net", value[index])


def _root(module: Module, net: Tuple[str, int], seen=frozenset()) -> str:
    if net in seen:
        return "LOOP"
    source = module.sources.get(net)
    if source is None:
        if net[0] in module.top_inputs or net[0] in module.top_inouts:
            return f"top:{net[0]}[{net[1]}]"
        return "OPEN"
    inverted, value = source
    if value[0] == "constant":
        answer = value[1]
        return ("1" if answer == "0" else "0") if inverted else answer
    if value[0] == "pin":
        answer = f"{value[1]}.{value[2]}[{value[3]}]"
    else:
        answer = _root(module, value[1], seen | {net})
    if not inverted:
        return answer
    if answer in ("0", "1"):
        return "1" if answer == "0" else "0"
    return answer[1:] if answer.startswith("~") else f"~{answer}"


def connectivity(path: Path, instance_ports: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, str], List[str]]:
    module = parse(Path(path).read_text(errors="replace"), instance_ports)
    return resolve(module, instance_ports), module.problems


def resolve(module: Module, instance_ports: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """What drives each bit of a parsed module, as connectivity() describes; problems go to module.problems."""
    result: Dict[str, str] = {}
    shared: Dict[Tuple[str, int], List[str]] = {}

    for instance, port, expression in module.pins:
        direction = instance_ports[instance][port]
        if direction == "out":
            continue
        nets = _expression(module, expression) if expression else None
        if not expression:
            if direction == "in":
                result[f"{instance}.{port}[0]"] = "OPEN"
            continue
        if nets is None:
            module.problems.append(f"UNPARSED input pin {instance}.{port}({expression})")
            continue
        inverted, value = nets
        if isinstance(value, tuple):  # a constant on the pin; its width is the port's, unknown here
            result[f"{instance}.{port}[all]"] = f"const:{value[1]}"
            continue
        for index, net in enumerate(value):
            if direction == "inout":
                shared.setdefault(net, []).append(f"{instance}.{port}[{index}]")
                continue
            root = _root(module, net)
            result[f"{instance}.{port}[{index}]"] = root if not inverted else f"~{root}"

    for name in module.top_outputs:
        for bit in module.bits(name):
            result[f"top:{name}[{bit}]"] = _root(module, (name, bit))
    for name in module.top_inouts:
        for bit in module.bits(name):
            shared.setdefault((name, bit), []).append(f"top:{name}[{bit}]")
    for members in shared.values():
        first = min(members)
        for member in members:
            result[member] = f"shared:{first}"
    return result
