"""The netlist of one generated module: its parameters, ports, nets, connections and instances.

It holds what a module is, with no text of a hardware description language in it. The wiring (wiring.py)
builds one from an elaborated design, having decided what drives what and refused what it cannot wire, and
a printer (systemverilog.py) writes it out. Anything else can read it as well: a printer for another
language, a comparison of two designs, a report of what drives what.

    from xactflow_rtl_gen import generate_netlists

    for name, module in generate_netlists(linked).items():
        for block in module.blocks:
            for assign in block.assigns:
                assign.sink, assign.source     # a part of a net, and the part of a net or the constant driving it

Three things in it are still as SystemVerilog has them, because IP-XACT has them so. A parameter's value is
an expression in that syntax. A type is called what the component's view calls it, with plain bits called
`logic`, or `wire` on a net that inouts share. And every name was checked to be a SystemVerilog identifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

__all__ = [
    "Assign",
    "Block",
    "Connection",
    "Constant",
    "Instance",
    "Module",
    "Net",
    "Parameter",
    "Port",
    "PortGroup",
    "Reference",
    "Select",
    "SignalType",
    "WidthCheck",
]

Bounds = Tuple[int, int]  # the left and right of a range, as written: [left:right]

IN, OUT, INOUT = "in", "out", "inout"  # the direction of a module's port


def one_line(text: object) -> str:
    """Text for a comment or a message, which end where their line does: a value may be written over several."""
    return " ".join(str(text).split())


@dataclass(frozen=True)
class SignalType:
    """The type of a port or a net: the name of a type, and its packed dimensions, outermost first."""

    name: str  # logic, wire, a type with a fixed size such as int, or a typedef such as pkg::req_t
    dimensions: Tuple[Bounds, ...] = ()

    def __str__(self) -> str:
        """As a message says it: `logic [15:0]`."""
        return self.name + (" " if self.dimensions else "") + "".join(f"[{left}:{right}]" for left, right in self.dimensions)


@dataclass(frozen=True)
class Select:
    """A part of a signal: a range of its bits or of its elements, one element of an array of a type, a member
    inside the signal or inside that element, or, with nothing set, all of it."""

    bounds: Optional[Bounds] = None  # [left:right]
    element: Optional[int] = None  # [index]: one element, which may have members
    path: str = ""  # .member.inner

    def __bool__(self) -> bool:
        return self.bounds is not None or self.element is not None or bool(self.path)

    def __str__(self) -> str:
        """As a message says it: `[7:4]`, `[3].addr`."""
        if self.bounds is not None:
            return f"[{self.bounds[0]}:{self.bounds[1]}]{self.path}"
        return (f"[{self.element}]" if self.element is not None else "") + self.path


@dataclass(frozen=True)
class Reference:
    """One end of a connection: a net or a port of the module by its name, or a part of it. `invert` is for
    the driving end, where every bit is inverted on the way."""

    net: str
    select: Select = Select()
    invert: bool = False


@dataclass(frozen=True)
class Constant:
    """A number driven onto a net. `width` is how many bits it drives, or None where only a compiler knows the
    width of what it drives: -1 is then all ones, at whatever width that is."""

    value: int
    width: Optional[int] = None


@dataclass(frozen=True)
class WidthCheck:
    """What only a compiler can check: that what a constant drives is at least `bits` bits wide. `message`
    is what to say when it is not."""

    bits: int
    message: str


@dataclass
class Assign:
    """One driver of one part of a net."""

    sink: Reference
    source: Union[Reference, Constant]
    note: str = ""  # said next to it: the logical port, or where a constant comes from
    cast: Optional[str] = None  # the named type the value is converted to, as an enum takes no plain number
    check: Optional[WidthCheck] = None


@dataclass
class Block:
    """The assigns of one connection of the design, under the comment that says which."""

    comment: List[str]
    assigns: List[Assign] = field(default_factory=list)


@dataclass
class Parameter:
    """A parameter of the module, with the one value the module is generated for: any other has to be refused
    by whatever reads the module (D36)."""

    name: str
    value: str  # an expression, as SystemVerilog and IP-XACT write one
    type_name: str = ""  # a typedef named with its package, or nothing: the parameter then takes its value's type
    integral: bool = True  # False for a real number or a string


@dataclass
class Port:
    """A port of the module."""

    name: str
    direction: str  # IN, OUT or INOUT
    type: SignalType


@dataclass
class Net:
    """A net inside the module."""

    name: str
    type: SignalType


@dataclass
class Connection:
    """A port of an instance, and the net or port of the module it connects to. None leaves it open."""

    port: str
    net: Optional[str]
    note: str = ""  # said next to it


@dataclass
class PortGroup:
    """Ports of an instance that belong together, under a heading: the bus interface they are mapped in."""

    heading: Optional[str]
    connections: List[Connection]


@dataclass
class Instance:
    """An instance of a module, with the value of each of its parameters in this design."""

    name: str
    module: str
    parameters: List[Tuple[str, str]]  # a module parameter and its value, an expression like a Parameter's
    groups: List[PortGroup]
    comments: List[str] = field(default_factory=list)  # said above it: where it comes from

    @property
    def connections(self) -> List[Connection]:
        """Every port of the instance, in the order they are written."""
        return [connection for group in self.groups for connection in group.connections]


@dataclass
class Module:
    """One module, as it is to be written."""

    name: str
    source: str  # what it was generated from: the design's VLNV
    parameters: List[Parameter]
    ports: List[Port]
    nets: List[Net]
    blocks: List[Block]
    instances: List[Instance]
    remarks: List[str] = field(default_factory=list)  # said under the header: which version of a component it is (D26)
    notes: List[str] = field(default_factory=list)  # said before the nets: what the design leaves undriven (D38)
