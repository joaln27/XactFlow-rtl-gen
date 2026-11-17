"""Wire an elaborated IP-XACT design: build the netlist of its module.

The design comes elaborated, from elaboration.py: its views chosen, its values worked out and its
hierarchy followed. What is here decides what its module is made of, as a netlist (netlist.py) for a
printer to write: its ports and parameters, a net for each connected port, and what drives each one.

Every connected instance port gets its own net, named <instance>_<port>, and each connection is
an assign from the driving net to the receiving one. That one shape covers part selects,
inverted port maps, tie-offs and struct sub-ports without special cases. Two things add a net of
their own: a logical port mapped in slices, which needs somewhere to collect its bits, and an inout,
which can only share a net.

The module's own ports come from the hierarchical component whose view holds the design, when
the library has one. Without one, a port no connection reaches becomes a top-level port, so
nothing floats silently. That component's module parameters become the module's own, each with
the one value the module is generated for: every width and value inside is a number worked out
here, and whatever reads the module is to refuse another value.

Anything this version cannot wire correctly raises ValueError instead of giving a wrong netlist.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

import ipxact
from xactflow import ExpressionError

from .address_map import AddressMap, address_window, build_address_map, for_view, settle_address_map
from .elaboration import ElaboratedDesign, ElaboratedInstance, HeldDesign, refuse_override_on, resolver_for
from .expressions import (
    DIMENSION,
    IDENTIFIER,
    TOKEN,
    Resolver,
    is_typed,
    names_in,
    number_of,
    parameter_of,
    resolved_text,
    says_more_than_numbers,
    text_of,
    two_bounds,
    worked_out,
)
from .keywords import SYSTEMVERILOG_KEYWORDS
from .netlist import (
    IN,
    INOUT,
    OUT,
    Assign,
    Block,
    Connection,
    Constant,
    Instance,
    Module,
    Net,
    Parameter,
    Port,
    PortGroup,
    Reference,
    Select,
    SignalType,
    WidthCheck,
    one_line,
)

__all__ = ["ModuleBuilder"]

_MAX_PORT_BITS = 1 << 20  # far past any real port, and each bit is tracked one by one
_TRANSFORM_NAME = re.compile(r"""transform\s+name=["']([^"']+)["']""")
_BIT_TYPES = {"logic", "wire", "reg", "bit"}  # sized by their vectors
_SIZED_TYPES = {"byte": 8, "shortint": 16, "int": 32, "integer": 32, "longint": 64, "time": 64}
# A type as text: what it is made of, then its packed dimensions. `logic [W-1:0]`, `pkg::req_t`, `T [3:0]`.
_TYPE = re.compile(
    r"(?P<base>(?:logic|bit|reg)(?:\s+unsigned)?|[A-Za-z_][\w$]*(?:::[A-Za-z_][\w$]*)*)\s*(?P<dimensions>(?:\[[^\[\]]*\]\s*)*)"
)
_NAMED_TYPE = re.compile(r"[A-Za-z_][\w$]*(?:::[A-Za-z_][\w$]*)*(?:\s+(?:un)?signed)?")  # a typedef, or int unsigned
_NOT_INTEGRAL = {"real", "shortreal", "realtime", "string"}  # the types of a parameter that `!==` does not compare
_PACKAGE_TYPE = re.compile(r"[A-Za-z_][\w$]*(?:::[A-Za-z_][\w$]*)+")  # a typedef named with its package

_IN = ipxact.Direction.IN
_OUT = ipxact.Direction.OUT
_INOUT = ipxact.Direction.INOUT
_ABSTRACTION_DEFAULT = "abstraction default"
_LEFT_OPEN = "nothing in the design connects this input: left open (unconnected=open)"
_UNDRIVEN = "nothing in the design drives this output: left undriven (unconnected=open)"
_FROM_INSIDE = {_IN: _OUT, _OUT: _IN, _INOUT: _INOUT}  # a module's input drives what is inside it

_Bounds = Tuple[int, int]


# Where a module's name came from, when nothing in the documents names the module itself (D47).
_NAMED_AFTER_COMPONENT = ": that is the component's name, used as no moduleName is given"
_NAMED_AFTER_DESIGN = ": that is the design's name, used as no component of the library holds the design"


def _check_identifier(name: str, what: str, note: str = "") -> None:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"{what} {name!r} is not a valid SystemVerilog identifier{note}")
    if name in SYSTEMVERILOG_KEYWORDS:
        raise ValueError(f"{what} {name!r} is a SystemVerilog keyword{note}")


def _check_fits(value: int, width: int, where: str) -> None:
    """Refuse a constant a port would silently truncate. A negative one has to fit as two's complement."""
    if value >> width if value >= 0 else value < -(1 << (width - 1)):
        raise ValueError(f"{where}: the constant {value} does not fit in {width} bits")


def _fitting_constant(value: int, width: Optional[int], where: str, invert: bool = False) -> Constant:
    """A constant for something of this width, None where only the compiler knows it. One that would be cut
    is refused."""
    if width is not None:
        _check_fits(value, width, where)
    return Constant(~value if invert else value, width)


def _bits_needed(value: int) -> int:
    """The fewest bits that hold a constant without losing any: as unsigned, or as two's complement when negative."""
    return max(1, value.bit_length()) if value >= 0 else (~value).bit_length() + 1


@dataclass
class _Port:
    instance: str
    name: str
    direction: ipxact.Direction  # as seen by the logic around it, so a module's input drives
    type: SignalType  # logic [15:0]
    width: Optional[int]  # None when the width is not known here, as for a struct
    all_keys: Set[object]  # every bit of the port, or {"*"} when its width is not known
    is_top: bool = False  # a port of the module being written, or a net inside it
    driven: Set[object] = field(default_factory=set)  # the bits (or struct paths) assigned so far
    read: bool = False
    left_open: bool = False
    net: Optional[str] = None
    vectors: List[_Bounds] = field(default_factory=list)  # as declared, outermost first
    bits: Optional[int] = None  # the size of a struct, when its sub-ports say
    leaves: List[str] = field(default_factory=list)  # a struct's sub-ports with nothing inside them
    sub_bits: Dict[str, Optional[int]] = field(default_factory=dict)  # the size of each sub-port
    unions: Set[str] = field(default_factory=set)  # paths of unions in it, "" for the port itself
    undeclared: Set[str] = field(default_factory=set)  # paths of structs with no sub-ports listed, "" likewise
    sub_types: Dict[str, str] = field(default_factory=dict)  # sub-ports of a named type, such as an enum
    unpacked: Set[str] = field(default_factory=set)  # paths of unpacked structs, "" likewise
    unsupported: Optional[str] = None  # why nothing can connect to it: it can only be left open
    element: Optional[str] = None  # the type of one element, when the port is a one-dimensional array of a type (D32)
    note: str = ""  # said next to the port where its instance is written

    @property
    def declaration(self) -> str:
        """The port's type as a message says it: `logic [15:0]`."""
        return str(self.type)

    @property
    def label(self) -> str:
        return f"{self.instance}.{self.name}" if self.instance else self.name

    def elements(self) -> List[str]:
        """The elements of an array of a type, as `[3]`; a port that is not one is a single part, ``."""
        if self.width is not None or self.element is None:
            return [""]
        low, high = sorted(self.vectors[0])
        return [f"[{index}]" for index in range(low, high + 1)]

    def undriven(self) -> List[object]:
        """The bits, elements or sub-ports nothing drives yet, as far as this port's declaration can tell."""
        if self.width is not None:
            return sorted(self.all_keys - self.driven)
        if not self.driven:
            return ["the whole port"]
        if "*" in self.driven:
            return []

        def covered(leaf: str, part: str) -> bool:
            return leaf == part or leaf.startswith(f"{part}.") or (leaf in self.undeclared and part.startswith(f"{leaf}."))

        missing: List[object] = []
        for element in self.elements():
            if element and element in self.driven:
                continue
            # What is driven inside this element, as sub-port paths.
            parts = [str(part)[len(element) :] for part in self.driven if str(part).startswith(f"{element}.")]
            if element and not parts:
                missing.append(f"element {element}")
                continue
            missing.extend(f"{element}{leaf}" for leaf in self.leaves if not any(covered(leaf, part) for part in parts))
        return missing


@dataclass
class _Instance:
    name: str
    module: str
    parameters: List[Tuple[str, str]]
    ports: Dict[str, _Port]
    skipped: Set[str]  # ports with no net: phantom ones, and transactional ones, absent from RTL
    resolve: Resolver
    transforms: List[str] = field(default_factory=list)
    vlnv: Optional[ipxact.VLNV] = None
    description: Optional[str] = None
    component: Optional[ipxact.Component] = None
    instantiation: Optional[ipxact.ComponentInstantiation] = None
    view: Optional[ipxact.View] = None
    hierarchical: bool = False


@dataclass
class _Terminal:
    """One end of a connection: a port, or a slice of it."""

    port: _Port
    select: Select
    width: Optional[int]
    keys: Set[object]
    invert: bool = False
    bits: Optional[int] = None  # the size of a struct or sub-port, whose width is None
    unchecked: bool = False  # a sub-port of a struct that doesn't list them: nothing here says its size
    path: str = ""  # the sub-port inside the port, or inside the element of it that `select` starts with
    kind: Optional[Tuple[str, int]] = None  # of a type only the compiler knows: the type, and how many of it

    @property
    def size(self) -> Optional[int]:
        return self.width if self.width is not None else self.bits


@dataclass
class _Assign:
    sink: _Terminal
    source: Union[_Terminal, Constant]
    note: str = ""
    cast: Optional[str] = None  # a typedef the value has to be cast to, as an enum takes no plain number
    needs: Optional[Tuple[int, int, str]] = None  # a constant, its bits and where it comes from, for the compiler to check


@dataclass
class _Block:
    comment: List[str]
    assigns: List[_Assign] = field(default_factory=list)


@dataclass
class _Child:
    """An instance whose view is a design: a module of its own that this one depends on."""

    component: ipxact.Component
    held: HeldDesign  # the design its view holds, and the design configuration that view names
    view: ipxact.View
    overrides: Dict[str, str] = field(default_factory=dict)  # the instance's values for the component's parameters
    module: str = ""  # the module generated for it: one per set of values (D26)


@dataclass
class _Declared:
    """A parameter of the module being written, with the one value the module is generated for (D36)."""

    type_name: str  # a typedef named with its package, or nothing: the parameter then takes its value's type
    name: str
    value: str
    integral: bool  # whether another value is told from this one as bits: not a real number, not a string


def _derived_in_the_hdl(text: str, resolve: Resolver, module_names: Set[str]) -> bool:
    """Whether a module parameter's value is worked out from other module parameters, as an HDL default such as
    B = A + 8'd100 is. The HDL then works it out itself, from the values given to those, exactly as it declares
    it, where the evaluator would simplify SystemVerilog's sign and width rules."""
    source = text.strip()
    if IDENTIFIER.fullmatch(source):  # the parameter its value names, as an importer writes it
        parameter = parameter_of(source, resolve)
        if parameter is None:
            return False
        source = text_of(parameter.value)
    # A keyword names no parameter: a type parameter's `logic [WIDTH-1:0]` is derived from WIDTH alone.
    names = [name for name in names_in(source) if name not in SYSTEMVERILOG_KEYWORDS]
    return bool(names) and all(name in module_names for name in names)


_OUT_OF_BOUNDS = re.compile(r"evaluates to .*, (below its minimum|above its maximum) ")


def _check_bounds_of(parameter: ipxact.Parameter, resolve: Resolver, where: str) -> None:
    """A parameter left to the HDL still has to keep to its minimum and maximum, and to those of the
    parameters its value goes through. Working it out checks them; anything else it says is the HDL's."""
    try:
        worked_out(parameter, resolve)
    except ExpressionError as exc:
        if _OUT_OF_BOUNDS.search(f"{exc} "):
            raise ValueError(f"{where}: {exc}") from exc
    except (OverflowError, ValueError, MemoryError, RecursionError):
        pass


def _instance_parameters(
    where: str,
    parameters: Sequence[ipxact.Parameter],
    instantiation: Optional[ipxact.ComponentInstantiation],
    overrides: Dict[str, str],
    resolve: Resolver,
    given: Dict[str, str],
    every_one: bool = False,
    reaching: bool = True,
) -> List[Tuple[str, str]]:
    """The #(...) of an instance: every module parameter of its view, with its value in this design.
    The HDL's own default can differ from the component's value, so none is left to it, except one the
    HDL derives from the others (D19 in docs/decisions.md). `overrides` are the values the design gives, and
    `resolve` reads a parameter with them applied, both from the elaboration. `given` holds values that are
    already final.

    `every_one` is for the module being generated, which has no HDL to derive anything: it declares each
    parameter with its value (D36). `reaching` is whether every override has to reach a module parameter; on
    an instance whose module is generated here, an override may change only what is inside it."""
    module_parameters = instantiation.module_parameters if instantiation is not None else []
    module_names = {p.name for p in module_parameters}
    result = []
    for parameter in module_parameters:
        _check_identifier(parameter.name, f"{where}: module parameter")
        if parameter.name in given:
            result.append((parameter.name, given[parameter.name]))
            continue
        text = text_of(parameter.value)
        if parameter.usage_type == "runtime":  # read by the simulation as it runs, not set on the instance
            if parameter.parameter_id in overrides or text in overrides:
                raise ValueError(f"{where}: {parameter.name} is a runtime parameter, so an override cannot reach it")
            continue
        identities = {n for n in (parameter.parameter_id, text.strip()) if n}
        if not every_one and not identities & set(overrides) and _derived_in_the_hdl(text, resolve, module_names):
            _check_bounds_of(parameter, resolve, f"{where}: module parameter {parameter.name}")
            continue
        # An override replaces a parameter's value in `resolve`, which keeps that parameter's type. Going
        # through it, rather than taking the override's text, is what applies the type.
        overridden = parameter.parameter_id in overrides
        value = parameter.parameter_id if overridden else text
        label = f"{where}: module parameter {parameter.name}"
        written = None
        if is_typed(parameter) and [n for n in names_in(overrides[parameter.parameter_id] if overridden else text) if parameter_of(n, resolve)]:
            # Worked out as the module parameter itself, so its own type applies, as the HDL's declaration would.
            try:
                written = worked_out(resolve(parameter.parameter_id) if overridden else parameter, resolve)
            except ExpressionError as exc:
                if not says_more_than_numbers(overrides[parameter.parameter_id] if overridden else text, resolve):
                    raise ValueError(f"{label}: {exc}") from exc
                written = None  # a string or a package name: as for any other value below
            except (OverflowError, ValueError, MemoryError, RecursionError) as exc:
                raise ValueError(f"{label}: not a usable number: {exc}") from exc
        result.append((parameter.name, written or resolved_text(value, resolve, label, own=parameter.name)))
    if not reaching:
        return result

    # An override that reaches no module parameter would not change the hardware.
    def uses(p: ipxact.Parameter) -> str:
        """Everything a parameter's value depends on: its value, and the bounds of its type's vectors."""
        bounds = " ".join(f"{text_of(v.left)} {text_of(v.right)}" for v in p.vectors)
        return f"{text_of(p.value)} {overrides.get(p.parameter_id, '')} {bounds}"

    by_id = {p.parameter_id: uses(p) for p in parameters if p.parameter_id}
    pending = [name for p in module_parameters for name in names_in(uses(p))]
    reached = {p.parameter_id for p in module_parameters if p.parameter_id}
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(names_in(by_id.get(name, "")))
    unused = sorted(set(overrides) - reached)
    if unused:
        raise ValueError(f"{where}: parameter overrides reach no module parameter: {', '.join(unused)}")
    return result


def _vector_bounds(vectors: Sequence[ipxact.Vector], resolve: Resolver, where: str) -> List[_Bounds]:
    return [
        (number_of(v.left, resolve, f"{where} left bound"), number_of(v.right, resolve, f"{where} right bound"))
        for v in vectors
    ]


def _size_of_bounds(bounds: Sequence[_Bounds]) -> int:
    size = 1
    for left, right in bounds:
        size *= abs(left - right) + 1
    return size


def _wire_type(wire: ipxact.WirePort, view: Optional[ipxact.View]) -> str:
    """The type to declare a wire port with: logic, unless its wireTypeDef says something else."""
    definitions = for_view(wire.wire_type_defs, view)
    declared = text_of(definitions[0].type_name) if definitions and definitions[0].type_name else None
    return "logic" if declared is None or declared in _BIT_TYPES else declared


def _type_parameters(instantiation: Optional[ipxact.ComponentInstantiation]) -> Dict[str, ipxact.ModuleParameter]:
    """The module parameters that stand for a type, as SystemVerilog's `parameter type` does, by name."""
    if instantiation is None:
        return {}
    return {p.name: p for p in instantiation.module_parameters if (p.data_type or "").strip() == "type"}


def _type_parameter_value(parameter: ipxact.ModuleParameter, resolve: Resolver) -> str:
    """The type a type parameter stands for in this instance, as text: its own value, or that of the
    parameter its value names, as an importer writes it, with any override on either."""
    text = text_of(parameter.value).strip()
    own = parameter_of(parameter.parameter_id, resolve) if parameter.parameter_id else None
    if own is not None:
        text = text_of(own.value).strip()
    followed: Set[str] = set()
    while IDENTIFIER.fullmatch(text) and text not in followed:
        named = parameter_of(text, resolve)
        if named is None:
            break
        followed.add(text)
        text = text_of(named.value).strip()
    return text


def _bounds_in(dimensions: str, resolve: Resolver, where: str) -> List[_Bounds]:
    """The packed dimensions written in a type, `[7:0][3:0]`, outermost first."""
    bounds = []
    for dimension in DIMENSION.finditer(dimensions):
        sides = two_bounds(dimension.group(1))
        if sides is None:
            raise ValueError(f"{where}: cannot work out the dimension {dimension.group(0)}")
        bounds.append((number_of(sides[0], resolve, f"{where} left bound"), number_of(sides[1], resolve, f"{where} right bound")))
    return bounds


def _type_in_this_instance(
    type_name: str, types: Dict[str, ipxact.ModuleParameter], resolve: Resolver, where: str, seen: Tuple[str, ...] = ()
) -> Tuple[str, List[_Bounds]]:
    """The type a port is declared with, and the packed dimensions written in it, which an importer may leave
    in the type's name: `pkg::req_t [N-1:0]`. Where the type is a type parameter, it is the type this instance
    gives that parameter, with the dimensions that type brings (D31). Text that is not a type and dimensions
    comes back as it is."""
    match = _TYPE.fullmatch(type_name.strip())
    if match is None:
        return type_name, []
    name = match.group("base")
    own = _bounds_in(match.group("dimensions"), resolve, where)
    if name not in types:
        return name, own
    if name in seen:
        raise ValueError(f"{where}: type parameter {' -> '.join((*seen, name))} refers to itself")
    value = _type_parameter_value(types[name], resolve)
    inner = _TYPE.fullmatch(value)
    if inner is None:
        raise ValueError(f"{where}: cannot work out {value!r}, the type that type parameter {name} stands for here")
    if inner.group("base") in types:
        base, bounds = _type_in_this_instance(value, types, resolve, where, (*seen, name))
    else:
        base, bounds = inner.group("base"), _bounds_in(inner.group("dimensions"), resolve, f"{where}: type parameter {name}")
    return base, [*own, *bounds]


def _is_integral(data_type: str, value: str) -> bool:
    """Whether another value can be told from this one as bits, x and z included. A real number and a string
    cannot: not a value that is one, and not what an instance gives a parameter whose HDL type is one."""
    kinds = {token.lastgroup for token in TOKEN.finditer(value)}
    return not ((data_type.split() or [""])[0] in _NOT_INTEGRAL or kinds & {"string", "real"})


@dataclass
class _Shape:
    """What a struct's declaration says about it."""

    bits: Optional[int] = None
    leaves: List[str] = field(default_factory=list)
    sub_bits: Dict[str, Optional[int]] = field(default_factory=dict)
    unions: Set[str] = field(default_factory=set)
    undeclared: Set[str] = field(default_factory=set)
    sub_types: Dict[str, str] = field(default_factory=dict)
    unpacked: Set[str] = field(default_factory=set)


def _struct_shape(fields: ipxact.StructuredPort, resolve: Resolver, view: Optional[ipxact.View], where: str, prefix: str = "") -> _Shape:
    """An importer may leave the sub-ports out, as xactflow-sv does, and then none of this is known."""
    if not fields.sub_ports:
        return _Shape(undeclared={prefix}, unpacked={prefix} if fields.packed is False else set())
    shape = _Shape(unpacked={prefix} if fields.packed is False else set())
    sizes: List[Optional[int]] = []
    for sub_port in fields.sub_ports:
        path = f"{prefix}.{sub_port.name}"
        size: Optional[int] = None
        if sub_port.wire is not None:
            type_name = _wire_type(sub_port.wire, view)
            if type_name in _SIZED_TYPES:
                size = _SIZED_TYPES[type_name]
            elif type_name == "logic":
                size = _size_of_bounds(_vector_bounds(sub_port.wire.vectors, resolve, f"{where}{path}"))
            else:  # a typedef, such as an enum: a constant into it needs a cast
                bounds = _vector_bounds(sub_port.wire.vectors, resolve, f"{where}{path}")
                shape.sub_types[path] = type_name + (" " if bounds else "") + "".join(f"[{left}:{right}]" for left, right in bounds)
            shape.leaves.append(path)
        elif sub_port.structured is not None:
            inner = _struct_shape(sub_port.structured, resolve, view, f"{where}{path}", path)
            size = inner.bits
            shape.leaves.extend(inner.leaves or [path])
            shape.sub_bits.update(inner.sub_bits)
            shape.unions |= inner.unions
            shape.undeclared |= inner.undeclared
            shape.sub_types.update(inner.sub_types)
            shape.unpacked |= inner.unpacked
        else:
            shape.leaves.append(path)
        shape.sub_bits[path] = None if sub_port.arrays else size
        sizes.append(shape.sub_bits[path])
    # An unpacked struct is not a row of bits, so a width says nothing about what it connects to.
    if fields.packed is not False and None not in sizes:
        total = max(sizes) if fields.struct_type == "union" else sum(sizes)
        shape.bits = total * _size_of_bounds(_vector_bounds(fields.vectors, resolve, where))
    if fields.struct_type == "union":
        shape.unions.add(prefix)
        shape.leaves = []  # one member drives all of it, so there is no list of parts to check it against
    return shape


def _build_port(
    owner: str,
    port: ipxact.Port,
    resolve: Resolver,
    is_top: bool = False,
    view: Optional[ipxact.View] = None,
    types: Optional[Dict[str, ipxact.ModuleParameter]] = None,
) -> Optional[_Port]:
    """The port as this generator sees it, or None when it has no net in RTL. `types` holds the type
    parameters of the module the port belongs to, which its type may be one of."""
    where = f"{owner}.{port.name}"
    _check_identifier(port.name, f"{owner}: port name")
    if port.transactional is not None:
        return None  # a TLM port: an RTL view of the component does not have it
    if port.arrays:
        if is_top:
            raise ValueError(f"{where}: unpacked array ports are not supported yet")
        # An instance's output can still be left open, which needs nothing of its shape.
        built = _build_port(owner, dataclasses.replace(port, arrays=[]), resolve, is_top, view, types)
        if built is not None:
            built.unsupported = "unpacked array ports are not supported yet, except an output left open"
        return built
    if port.wire is not None:
        direction, vectors, type_name = port.wire.direction, port.wire.vectors, _wire_type(port.wire, view)
    elif port.structured is not None and port.structured.struct_type in ("struct", "union"):
        if port.structured.phantom:
            return None
        direction, vectors = port.structured.direction, port.structured.vectors
        definitions = for_view(port.structured.struct_port_type_defs, view)
        if not definitions:
            raise ValueError(f"{where}: a structured port needs a structPortTypeDef to be declared")
        type_name = text_of(definitions[0].type_name)
    else:
        raise ValueError(f"{where}: interface ports are not supported yet")

    # A type parameter stands for the type this instance gives it, which may be a plain vector of bits (D31).
    is_struct = port.wire is None
    type_name, from_type = _type_in_this_instance(type_name, types or {}, resolve, where)
    if type_name.split()[0] in _BIT_TYPES:
        if is_struct and port.structured.sub_ports:
            raise ValueError(f"{where}: the port lists sub-ports, and its type is {type_name}")
        type_name, is_struct = "logic", False
    elif not _NAMED_TYPE.fullmatch(type_name):
        # It is written into the module as it stands, so it has to be a type's name and nothing else.
        raise ValueError(f"{where}: {type_name!r} is not a type this version can declare")

    if direction is ipxact.Direction.PHANTOM:
        return None
    if direction is None:
        raise ValueError(f"{where}: the port has no direction")
    if direction is _INOUT:
        if is_struct or type_name != "logic":
            raise ValueError(f"{where}: an inout port of type {type_name} is not supported yet")
        type_name = "wire"  # an inout has to be a net, it cannot be a logic variable

    bounds = [*_vector_bounds(vectors, resolve, where), *from_type]
    declaration = SignalType(type_name, tuple(bounds))
    inside = _FROM_INSIDE[direction] if is_top else direction
    net = port.name if is_top else None
    element = type_name if len(bounds) == 1 else None  # what one element is, of a one-dimensional array of a type
    if is_struct:
        shape = _struct_shape(port.structured, resolve, view, where)
        return _Port(
            owner, port.name, inside, declaration, None, {"*"}, is_top=is_top, net=net, vectors=bounds,
            bits=shape.bits * _size_of_bounds(from_type) if shape.bits is not None else None,
            leaves=shape.leaves, sub_bits=shape.sub_bits, unions=shape.unions, undeclared=shape.undeclared,
            sub_types=shape.sub_types, unpacked=shape.unpacked, element=element,
        )
    if type_name in _SIZED_TYPES:
        if bounds:
            raise ValueError(f"{where}: vectors on a port of type {type_name} are not valid SystemVerilog")
        width = _SIZED_TYPES[type_name]
        return _Port(owner, port.name, inside, declaration, width, set(range(width)), is_top=is_top, net=net, vectors=[(width - 1, 0)])
    if type_name not in ("logic", "wire"):  # a typedef: only the compiler knows its width
        return _Port(owner, port.name, inside, declaration, None, {"*"}, is_top=is_top, net=net, vectors=bounds, element=element)
    width = _size_of_bounds(bounds)
    if width > _MAX_PORT_BITS:
        raise ValueError(f"{where}: a port of {width} bits is not supported")
    if len(bounds) == 1:
        low, high = sorted(bounds[0])
        keys: Set[object] = set(range(low, high + 1))
    else:
        keys = set(range(width))
    return _Port(owner, port.name, inside, declaration, width, keys, is_top=is_top, net=net, vectors=bounds)


def _check_type_is_visible(port: _Port) -> None:
    """A generated module imports nothing, so it can only declare a typedef named with its package."""
    type_name = re.split(r"[\s\[]", port.declaration, maxsplit=1)[0]
    if type_name not in ("logic", "wire", *_SIZED_TYPES) and "::" not in type_name:
        raise ValueError(f"{port.label}: the generated module cannot see type {type_name}, which needs its package in its name")


def _parts_alike(first: _Port, second: _Port) -> bool:
    """Whether a part of one port is the same part of the other: by number where both are plain bits, and
    by a member's name only where both are of one type, as two structs can have a member `a` in other bits."""
    if first.vectors != second.vectors:
        return False
    if first.width is not None and second.width is not None:
        return True
    return _plain_type(first.declaration) == _plain_type(second.declaration)


def _plain_type(declaration: str) -> str:
    """A declaration for comparing: spacing doesn't matter, and an array of one is the same bits."""
    return re.sub(r"\s+", "", declaration).replace("[0:0]", "")


def _overlaps(driven: Set[object], keys: Set[object]) -> bool:
    """Whether these bits or sub-ports are driven already, counting a sub-port as part of its parent."""
    if driven & keys or (driven and "*" in driven | keys):
        return True
    paths = [key for key in keys if isinstance(key, str)]
    others = [key for key in driven if isinstance(key, str)]
    return any(a.startswith(f"{b}.") or b.startswith(f"{a}.") for a in paths for b in others)


def _port_groups(instance: _Instance) -> List[Tuple[Optional[str], List[_Port]]]:
    """The instance's ports by the bus interface they belong to, for reading the output."""
    bus_interfaces = instance.component.bus_interfaces if instance.component is not None else []
    owners: Dict[str, List[str]] = {}
    for bus_interface in bus_interfaces:
        for abstraction_type in for_view(bus_interface.abstraction_types, instance.view):
            for port_map in abstraction_type.port_maps:
                if port_map.physical_port and bus_interface.name not in owners.setdefault(port_map.physical_port, []):
                    owners[port_map.physical_port].append(bus_interface.name)
    groups: Dict[Optional[str], List[_Port]] = {bus_interface.name: [] for bus_interface in bus_interfaces}
    groups["several interfaces"] = []
    groups[None] = []
    for port in instance.ports.values():
        names = owners.get(port.name, [])
        groups[names[0] if len(names) == 1 else "several interfaces" if names else None].append(port)
    return [(name, ports) for name, ports in groups.items() if ports]


class ModuleBuilder:
    """Wires one elaborated design. build() gives the netlist of its module. After it, `address_map` is the
    design's address map, if one of its instances is a decoder, `instances` holds its instances as they were wired, and `children`
    the hierarchical ones, each of which needs a module of its own."""

    def __init__(
        self,
        elaborated: ElaboratedDesign,
        common: Sequence[str],
        must_use_common: bool = True,
        module_name: Optional[str] = None,
        variants: Optional[Dict[Tuple[object, ...], Tuple[str, Dict[str, str]]]] = None,
        leave_open: bool = False,
    ) -> None:
        # The design with its views chosen and its values worked out (elaboration.py). What is here wires it.
        self.elaborated = elaborated
        self.linked = linked = elaborated.linked
        self.common = list(common)
        self.top_component = elaborated.component
        self.must_use_common = must_use_common
        self.module_name = module_name
        self.variants = variants if variants is not None else {}
        self.leave_open = leave_open  # whether a port the design connects to nothing stays so, where it would be refused (D38)
        # The view that holds the design, and the module it describes, if it does.
        self.top_view, self.top_instantiation = elaborated.view, elaborated.instantiation
        # The component's parameters, with the values of the instance this module is generated for (D26).
        self.top_parameters, self.top_overrides, self.top_resolve = elaborated.parameters, elaborated.overrides, elaborated.resolve
        self.design_resolve = elaborated.design_resolve  # what an expression written in the design can see
        self.declared: List[_Declared] = []  # the module's own parameters (D36)
        self.undriven: List[str] = []  # the module's outputs that nothing drives, where that is allowed (D38)
        self.net_owners: Dict[str, List[_Port]] = {}  # who holds each net's name, in case it has to change
        self.instances: Dict[str, _Instance] = {}
        self.given_values: List[str] = []  # what the design gives its instances' parameters, as text
        self.children: List[_Child] = []
        self.blocks: List[_Block] = []
        self.abstraction_defaults: List[Tuple[_Block, _Assign]] = []  # what a tiedValue "default" may replace
        self.wires: Dict[str, SignalType] = {}
        self.address_map: Optional[AddressMap] = None
        self.module = linked.vlnv.name  # until a component names the module
        self.top = _Instance(self.module, self.module, [], {}, set(), lambda name: name)

    def build(self) -> Module:
        self._add_top_component()
        _check_identifier(self.module, "design name", "" if self.top_component is not None else _NAMED_AFTER_DESIGN)
        for name in self.common:
            _check_identifier(name, "common port")
        self.address_map = build_address_map(
            self.linked, self.module, lambda name: self.elaborated.configured_view(self.linked.instances[name])[0]
        )
        for instance in self.linked.instances.values():
            self._add_instance(instance)
        # Which instances are decoders is settled by the values the module is written with (D52).
        self.address_map = settle_address_map(
            self.address_map,
            [
                *(parameter.value for parameter in self.declared),
                *(value for instance in self.instances.values() for _, value in instance.parameters),
                *self.given_values,
            ],
        )
        for interconnection in self.linked.interconnections:
            self._wire_interconnection(interconnection)
        for connection in self.linked.design.ad_hoc_connections:
            self._wire_ad_hoc(connection)
        self._connect_top_ports_directly()
        self._name_nets()
        self._check_names()
        return self._netlist()

    def _check_names(self) -> None:
        """Instances, nets and the module's ports share one scope in SystemVerilog. A port can claim a name
        after a net got it, as both come from names joined up. The net is the one that can change."""
        ports = set(self.top.ports)
        parameters = {parameter.name for parameter in self.declared}
        for name in sorted(ports & parameters):
            raise ValueError(f"module {self.module} has a port and a module parameter both called {name!r}")
        for name in sorted(ports & set(self.instances)):
            raise ValueError(f"instance {name!r} has the name of a port of module {self.module}")
        for name in sorted(parameters & set(self.instances)):
            raise ValueError(f"instance {name!r} has the name of a parameter of module {self.module}")
        for name in sorted((ports | parameters) & set(self.wires)):
            new_name = self._free_net_name(name)
            self.wires[new_name] = self.wires.pop(name)
            for port in self.net_owners.get(name, []):
                port.net = new_name

    def _add_top_component(self) -> None:
        component = self.top_component
        if component is None:
            return
        where = f"top component {component.vlnv}"
        view, instantiation = self.top_view, self.top_instantiation
        named = instantiation is not None and bool(instantiation.module_name)
        self.module = instantiation.module_name if named else component.vlnv.name
        if self.module_name:  # the version of it for one set of parameter values (D26)
            self.module = self.module_name
        _check_identifier(self.module, f"{where}: module name", "" if named else _NAMED_AFTER_COMPONENT)
        self.top.name = self.top.module = self.module
        self.top.view = view
        resolve = self.top_resolve
        self.top.resolve = resolve
        self._declare_parameters(where)
        for port in component.model.ports:
            built = _build_port(self.module, port, resolve, is_top=True, view=view)
            if built is None:
                self.top.skipped.add(port.name)
            else:
                _check_type_is_visible(built)
                self.top.ports[port.name] = built

    def _declare_parameters(self, where: str) -> None:
        """The module parameters of the component this module is generated from become the module's own, each
        with the one value the module is generated for, so that what instantiates it by hand still compiles.
        Everything inside is worked out for those values, so the compiler is given a check that refuses any
        other (D36). A parameter is declared with no type, so it takes the type of what it is given, and the
        check compares that with the value as it is written here, nothing converted in between: with the
        component's `dataType` the declaration would convert the value, and the module refuse its own. Only a
        typedef named with its package is kept, as only the compiler knows what its values are, and the check
        then casts the value to it as the declaration does (D41)."""
        instantiation = self.top_instantiation
        if instantiation is None:
            return
        by_name = {parameter.name: parameter for parameter in instantiation.module_parameters}
        values = _instance_parameters(
            where, self.top_parameters, instantiation, self.top_overrides, self.top_resolve, {}, every_one=True, reaching=False
        )
        for name, value in values:
            label = f"{where}: module parameter {name}"
            data_type = " ".join((by_name[name].data_type or "").split())
            if data_type == "type":
                raise ValueError(f"{label}: a type parameter on a generated module is not supported yet")
            if "'{" in value:
                raise ValueError(f"{label}: the value {value!r} cannot be compared with another, so the module cannot check it is given this one")
            if any(name == other.name for other in self.declared):
                raise ValueError(f"{label}: the component has two module parameters of that name")
            type_name = data_type if _PACKAGE_TYPE.fullmatch(data_type) else ""
            self.declared.append(_Declared(type_name, name, value, _is_integral(data_type, value)))

    def _add_instance(self, linked_instance) -> None:
        name = linked_instance.instance_name
        where = f"instance {name!r}"
        _check_identifier(name, "instance name")
        # Its view, and its parameters with the values this design gives them.
        elaborated = self.elaborated.instance(linked_instance)
        component, view, instantiation = elaborated.component, elaborated.view, elaborated.instantiation
        named = instantiation is not None and bool(instantiation.module_name)
        module = instantiation.module_name if named else component.vlnv.name
        _check_identifier(module, f"{where}: module name", "" if named else _NAMED_AFTER_COMPONENT)

        # A decoder's rule count is one more value for its parameters, because port widths may depend on it.
        given, counts = self._address_rules_for(name, instantiation, elaborated.overrides)
        elaborated = elaborated.with_values(counts)
        parameters, overrides, resolve = elaborated.parameters, elaborated.overrides, elaborated.resolve
        self.given_values.extend(overrides.values())

        held = self.elaborated.held_by(elaborated)
        if held is not None:
            if "ADDR_RULES" in given:
                # Its module is generated for one value of each parameter (D36), and would refuse the table.
                raise ValueError(f"{where}: its module is generated here, so it cannot be given the address map's rule table as a parameter")
            version, values = self._module_for_values(elaborated, module)
            if version == module:  # values that change nothing: the module as it is
                self.children.append(_Child(component, held, view))
            else:
                self.children.append(_Child(component, held, view, values, version))
                module = version

        ports: Dict[str, _Port] = {}
        skipped: Set[str] = set()
        types = _type_parameters(instantiation)
        for port in component.model.ports if component.model is not None else []:
            built = _build_port(name, port, resolve, view=view, types=types)
            if built is None:
                skipped.add(port.name)
            else:
                ports[port.name] = built

        self.instances[name] = _Instance(
            name,
            module,
            # A hierarchical instance's values go into the module generated for it, which declares these
            # parameters with the same values (D36), so here they only have to agree with it.
            _instance_parameters(where, parameters, instantiation, overrides, resolve, given, reaching=held is None),
            ports,
            skipped,
            resolve,
            [t for extension in linked_instance.source.vendor_extensions for t in _TRANSFORM_NAME.findall(str(extension))],
            linked_instance.component_vlnv,
            linked_instance.source.description or linked_instance.source.short_description,
            component,
            instantiation,
            view,
            held is not None,
        )

    def _module_for_values(self, instance: ElaboratedInstance, base: str) -> Tuple[str, Dict[str, str]]:
        """The module a hierarchical instance needs (D26), and the values to generate it with. Every value in a
        generated module is worked out, and a parameter it declares only takes the value it is generated for
        (D36), so a set of values that changes any parameter gets a module of its own: `<module>_1`,
        `<module>_2`, ... in the order they turn up. Values that change nothing keep the module.

        Instances whose values come to the same share a module, however each wrote them: `16`, `8+8`, or with
        a default spelled out. The module is generated with the values of the first one to turn up, so that
        it is one text."""
        key = instance.variant()
        if key is None:
            return base, {}
        if key not in self.variants:
            taken = {name for name, _ in self.variants.values()}
            number = 1
            while f"{base}_{number}" in taken:
                number += 1
            self.variants[key] = (f"{base}_{number}", dict(instance.overrides))
        return self.variants[key]

    def _address_rules_for(
        self, name: str, instantiation: Optional[ipxact.ComponentInstantiation], overrides: Dict[str, str]
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """The rule table of a decoder that declares module parameters for it, unless the design set
        those itself. Its count comes back as a value for the instance's parameters too, by parameterId,
        because port widths may depend on it."""
        given: Dict[str, str] = {}
        counts: Dict[str, str] = {}
        if self.address_map is None or instantiation is None or name not in self.address_map.tables:
            return given, counts
        rules = self.address_map.tables[name]
        wanted = {p.name: p for p in instantiation.module_parameters if p.name in ("ADDR_RULES", "NUM_RULES")}
        # Its own id, and the parameter its value names, which is the one port widths use (Kactus2 gives
        # module parameters ids of their own). An override on either means the design set it itself.
        names = {
            key: [n for n in (p.parameter_id, text_of(p.value).strip()) if n and IDENTIFIER.fullmatch(n)] for key, p in wanted.items()
        }
        set_by_design = {key for key, ids in names.items() if any(n in overrides for n in ids)}
        if set_by_design == set(wanted):
            return given, counts
        if "ADDR_RULES" in set_by_design:
            raise ValueError(f"instance {name!r}: the design sets ADDR_RULES itself, so the count of the table here would not be its own")
        if "NUM_RULES" in set_by_design:
            count = next(overrides[n] for n in names["NUM_RULES"] if n in overrides)
            if number_of(count, self.design_resolve, f"instance {name!r}: NUM_RULES") != len(rules):
                raise ValueError(f"instance {name!r}: the design sets NUM_RULES to {count}, but the address map has {len(rules)} rules for it")
        for key in wanted:
            if key in set_by_design:
                continue
            given[key] = f"{self.address_map.package}::{self.address_map.constant(name, key)}"
            if key == "NUM_RULES":
                for n in names[key]:
                    counts[n] = str(len(rules))
        return given, counts

    def _terminal(
        self,
        owner: _Instance,
        port_name: str,
        part_select: Optional[ipxact.PartSelect],
        sub_port_refs: Sequence[ipxact.SubPortReference],
        invert: bool,
        where: str,
        resolve: Optional[Resolver] = None,
    ) -> Optional[_Terminal]:
        """A connection end, or None for a port with no net in RTL. `resolve` is for a part select
        written in the design, which sees the design's parameters and not the instance's."""
        if port_name in owner.skipped:
            return None
        port = owner.ports.get(port_name)
        if port is None:
            raise ValueError(f"{where}: {owner.name} has no port {port_name!r}")
        if port.unsupported:
            raise ValueError(f"{where}: {port.label}: {port.unsupported}")
        resolve = resolve or owner.resolve
        index, picked = Select(), [""]  # the elements a part select picks from an array of a type (D32)
        if part_select is not None and port.width is None:
            index, picked = self._elements_selected(port, part_select, resolve, where)
            part_select = None
        if sub_port_refs:
            if part_select is not None or any(r.part_select is not None for r in sub_port_refs):
                raise ValueError(f"{where}: a part select on a subPort is not supported yet")
            path = "".join(f".{r.sub_port_ref}" for r in sub_port_refs)
            if len(picked) > 1:
                raise ValueError(f"{where}: a sub-port of {port.label}{index}, which is {len(picked)} elements, has no meaning")
            if "[" in port.declaration and not index:
                raise ValueError(f"{where}: a sub-port of {port.label}, an array of {port.declaration}, is not supported yet")
            unchecked = any(path == outer or path.startswith(f"{outer}.") for outer in port.undeclared)
            if path not in port.sub_bits and not unchecked:
                raise ValueError(f"{where}: {port.label} has no sub-port {path!r}")
            # The members of a union are the same bits, so driving one is driving the whole union.
            union = min((u for u in port.unions if path.startswith(f"{u}.")), key=len, default=None)
            keys: Set[object] = {f"{index}{path if union is None else union}" or "*"}
            return _Terminal(port, dataclasses.replace(index, path=path), None, keys, invert, port.sub_bits.get(path), unchecked, path)
        if index:
            each = port.bits // len(port.elements()) if port.bits is not None else None
            bits = each * len(picked) if each is not None else None
            return _Terminal(port, index, None, set(picked), invert, bits, kind=(_plain_type(port.element), len(picked)))
        if part_select is None:
            kind = None
            if port.width is None and port.element is not None:
                kind = (_plain_type(port.element), len(port.elements()))
            elif port.width is None and not port.vectors:
                kind = (_plain_type(port.declaration), 1)
            return _Terminal(port, Select(), port.width, set(port.all_keys), invert, port.bits, kind=kind)
        if part_select.indices or part_select.range is None:
            raise ValueError(f"{where}: an indexed part select is not supported yet")
        if len(port.vectors) > 1:
            raise ValueError(f"{where}: a part select on {port.label}, which has several dimensions, is not supported yet")
        left = number_of(part_select.range.left, resolve, where)
        right = number_of(part_select.range.right, resolve, where)
        low, high = sorted((left, right))
        if not set(range(low, high + 1)) <= port.all_keys:
            raise ValueError(f"{where}: part select [{left}:{right}] is outside port {port.label}")
        if port.vectors and left != right and (left > right) != (port.vectors[0][0] > port.vectors[0][1]):
            raise ValueError(f"{where}: part select [{left}:{right}] runs the other way from port {port.label}, a {port.declaration}")
        select = Select() if port.width == 1 and not port.vectors else Select((left, right))  # a scalar has nothing to select
        return _Terminal(port, select, high - low + 1, set(range(low, high + 1)), invert)

    @staticmethod
    def _elements_selected(port: _Port, part_select: ipxact.PartSelect, resolve: Resolver, where: str) -> Tuple[Select, List[str]]:
        """What a part select picks from a one-dimensional array of a type only the compiler knows (D32):
        `[3]` for one element, which may then have sub-ports, or `[5:2]` for a run of them. Returns what it
        selects, and the elements it covers."""
        if port.element is None:
            raise ValueError(f"{where}: a part select on {port.label}, a {port.declaration}, is not supported yet")
        if part_select.indices or part_select.range is None:
            raise ValueError(f"{where}: an indexed part select is not supported yet")
        left = number_of(part_select.range.left, resolve, where)
        right = number_of(part_select.range.right, resolve, where)
        low, high = sorted((left, right))
        first, last = sorted(port.vectors[0])
        if low < first or high > last:
            raise ValueError(f"{where}: part select [{left}:{right}] is outside port {port.label}, a {port.declaration}")
        if left != right and (left > right) != (port.vectors[0][0] > port.vectors[0][1]):
            raise ValueError(f"{where}: part select [{left}:{right}] runs the other way from port {port.label}, a {port.declaration}")
        return (Select(element=left) if left == right else Select((left, right))), [f"[{i}]" for i in range(low, high + 1)]

    @staticmethod
    def _check_sizes(sink: _Terminal, source: _Terminal, where: str) -> None:
        """Neither tool would stop a truncated connection, so its sizes are compared here, as far as
        the ports' declarations can tell."""
        sink_size, source_size = sink.size, source.size
        sink_name, source_name = f"{sink.port.label}{sink.select}", f"{source.port.label}{source.select}"
        if sink_size is not None and source_size is not None:
            if sink_size != source_size:
                raise ValueError(f"{where}: width mismatch, {source_name} is {source_size} bits and {sink_name} is {sink_size} bits")
            return
        if sink.unchecked or source.unchecked:
            return  # a sub-port of a struct that doesn't list them: nothing here says how wide it is
        if sink.kind is not None and sink.kind == source.kind:
            return  # as many of the same type, whatever its width: an element of an array is one of them
        if not sink.select and not source.select and _plain_type(sink.port.declaration) == _plain_type(source.port.declaration):
            return  # the same type, whatever its width
        raise ValueError(
            f"{where}: cannot check the width between {source_name}, a {source.port.declaration}, and {sink_name}, a {sink.port.declaration}"
        )

    def _assign(
        self, block: _Block, sink: _Terminal, source: Union[_Terminal, Constant], where: str, note: str = "", value: Optional[int] = None
    ) -> None:
        """`value` is the number a constant source stands for, so that it can be checked against the sink."""
        if isinstance(source, _Terminal):
            self._check_sizes(sink, source, where)
            source.port.read = True
        driven = sink.port.driven
        if _overlaps(driven, sink.keys):
            raise ValueError(f"{where}: {sink.port.label}{sink.select} is already driven by another connection")
        cast = needs = None
        if isinstance(source, Constant) or source.invert:
            typed = sink.port.width is None
            if (sink.path or ("" if typed else None)) in sink.port.unpacked:
                raise ValueError(f"{where}: a constant or an inversion into {sink.port.label}{sink.select}, an unpacked struct, is not supported yet")
            # A typedef such as an enum takes no plain number or inverted vector without a cast.
            if sink.path:
                type_name = sink.port.sub_types.get(sink.path)
            elif not typed:
                type_name = None
            elif not sink.select:
                type_name = sink.port.declaration
            elif sink.kind is not None and sink.kind[1] == 1:
                type_name = sink.port.element  # one element of an array of the type
            else:
                type_name = f"{sink.port.element} [{sink.kind[1] - 1}:0]"
            if type_name is not None:
                if not re.fullmatch(r"[\w$]+(::[\w$]+)*", type_name):
                    raise ValueError(f"{where}: a constant or an inversion into {sink.port.label}{sink.select}, a {type_name}, is not supported yet")
                cast = type_name
            if value is not None and sink.size is None and _bits_needed(value) > 1:
                needs = (value, _bits_needed(value), where)  # only the compiler knows the width: it checks the constant fits
        driven |= sink.keys
        block.assigns.append(_Assign(sink, source, note, cast, needs))

    def _shared_abstraction(self, ends, where: str) -> Tuple[ipxact.VLNV, ipxact.AbstractionDefinition]:
        choices = [for_view(bus.abstraction_types, owner.view) for owner, bus, _ in ends]
        for candidate in choices[0]:
            vlnv = candidate.abstraction_ref.vlnv
            if all(any(t.abstraction_ref.vlnv == vlnv for t in others) for others in choices[1:]):
                definition = self.linked.library.get_abstraction_definition(vlnv)
                if definition is None:
                    raise ValueError(f"{where}: abstraction definition {vlnv} is not in the library")
                return vlnv, definition
        raise ValueError(f"{where}: the interfaces share no abstraction definition")

    def _wire_interconnection(self, interconnection) -> None:
        where = f"interconnection {interconnection.name!r}"
        # (instance or the module itself, its bus interface, the logical ports this end excludes)
        ends = [
            (self.instances[e.instance.instance_name], e.bus_interface, {text_of(p) for p in e.source.exclude_ports})
            for e in interconnection.endpoints
        ]
        # As the design wrote them: link() resolves them against the component it found, and leaves out
        # those it could not resolve, while the module's component is the one chosen here (D44).
        for hier in interconnection.source.hier_interfaces:
            if self.top_component is None:
                raise ValueError(f"{where}: a hierInterface needs a hierarchical component in the library, to say which ports it is")
            bus_interface = next((b for b in self.top_component.bus_interfaces if b.name == hier.bus_ref), None)
            if bus_interface is None:
                raise ValueError(f"{where}: top component {self.top_component.vlnv} has no bus interface {hier.bus_ref!r}")
            ends.append((self.top, bus_interface, set()))

        for owner, bus, _ in ends:
            refuse_override_on(bus.bus_type, f"{where}: {owner.name}.{bus.name}")
            for abstraction_type in for_view(bus.abstraction_types, owner.view):
                refuse_override_on(abstraction_type.abstraction_ref, f"{where}: {owner.name}.{bus.name}")
        vlnv, abstraction = self._shared_abstraction(ends, where)
        logical_names = {port.logical_name for port in abstraction.ports}
        with_maps = []
        for owner, bus, excluded in ends:
            # The abstraction type of the view this instance uses: another view's may map differently.
            port_maps = next(t.port_maps for t in for_view(bus.abstraction_types, owner.view) if t.abstraction_ref.vlnv == vlnv)
            for port_map in port_maps:
                if port_map.logical_port not in logical_names:
                    raise ValueError(
                        f"{where}: {owner.name}.{bus.name} maps logical port {port_map.logical_port!r}, "
                        f"which {vlnv} does not define"
                    )
            with_maps.append((owner, port_maps, excluded, bus.mode))

        names = " <-> ".join(f"{owner.name}.{bus.name}" for owner, bus, _ in ends)
        block = _Block([f"{interconnection.name}: {names} ({vlnv})"])
        source = interconnection.source
        for interface in (source.active_interface, *source.other_active_interfaces):
            window = address_window(interface)
            if window:
                shown = ", ".join(f"{key} {value}" for key, value in sorted(window.items()))
                block.comment.append(f"{interface.component_instance_ref}.{interface.bus_ref} address window: {shown}")
        resolve_abstraction = resolver_for(abstraction.parameters, {})
        for logical in abstraction.ports:
            self._wire_logical_port(block, where, interconnection.name, logical, with_maps, resolve_abstraction)
        self.blocks.append(block)

    @staticmethod
    def _logical_bounds(port_map: ipxact.PortMap, owner: _Instance, name: str, where: str) -> Optional[_Bounds]:
        if port_map.logical_range is None:
            return None
        left = number_of(port_map.logical_range.left, owner.resolve, where)
        right = number_of(port_map.logical_range.right, owner.resolve, where)
        if left < right:
            raise ValueError(f"{where}: ascending logical range [{left}:{right}] on {name!r} is not supported yet")
        return left, right

    @staticmethod
    def _check_within_width(
        logical: ipxact.AbstractionPort,
        mode: ipxact.InterfaceMode,
        bounds: Optional[_Bounds],
        terminal: _Terminal,
        resolve_abstraction: Resolver,
        where: str,
    ) -> None:
        """SCR 6.19: the logical bits a port map takes lie within the width its abstraction gives the logical port
        in that mode, where it gives one. With no logical range, a wire port takes [its width - 1:0] (B.1.7 b)).
        Checked only where bits are picked by number (D29): ends that map whole ports of one width pick none, and an
        abstraction's width left at its default then changes nothing. Checking the SCRs is XactFlow's part."""
        wire = logical.wire
        sides = {
            ipxact.InterfaceMode.INITIATOR: wire.on_initiator if wire else None,
            ipxact.InterfaceMode.MIRRORED_INITIATOR: wire.on_initiator if wire else None,
            ipxact.InterfaceMode.TARGET: wire.on_target if wire else None,
            ipxact.InterfaceMode.MIRRORED_TARGET: wire.on_target if wire else None,
        }
        side = sides.get(mode)
        if side is None or side.width is None:
            return
        try:
            width = number_of(side.width, resolve_abstraction, where)
        except ValueError:
            return  # a width it can't work out here: nothing to hold the range to
        if bounds is not None:
            if max(bounds) >= width:
                raise ValueError(
                    f"{where}: logical range [{bounds[0]}:{bounds[1]}] of {logical.logical_name!r} on {terminal.port.label} "
                    f"goes past its width in the abstraction, {width} bits (SCR 6.19)"
                )
        elif terminal.width is not None and terminal.width > width:
            raise ValueError(
                f"{where}: {terminal.port.label}{terminal.select} is {terminal.width} bits, mapped to {logical.logical_name!r} "
                f"with no logical range, which makes it logical bits [{terminal.width - 1}:0]; the abstraction gives it "
                f"{width} (B.1.7 b), SCR 6.19)"
            )

    def _end_would_drive(self, logical: ipxact.AbstractionPort, owner: _Instance, mode: ipxact.InterfaceMode) -> bool:
        """Whether an end with this mode is on the driving side of a logical port. A tie-off there competes
        with a real driver, where one on a receiving end only says that end has nothing to connect."""
        wire = logical.wire
        sides = {
            ipxact.InterfaceMode.INITIATOR: (wire.on_initiator if wire else None, False),
            ipxact.InterfaceMode.TARGET: (wire.on_target if wire else None, False),
            ipxact.InterfaceMode.MIRRORED_INITIATOR: (wire.on_initiator if wire else None, True),
            ipxact.InterfaceMode.MIRRORED_TARGET: (wire.on_target if wire else None, True),
        }
        constraints, mirrored = sides.get(mode, (None, False))
        if constraints is None or constraints.direction is None:
            return True  # can't tell, as for a system interface: better to refuse than to drop a tie-off quietly
        outward = (text_of(constraints.direction) == "out") != mirrored
        return outward != (owner is self.top)  # the module's own interface is seen from inside it

    def _wire_logical_port(
        self, block: _Block, where: str, connection: str, logical: ipxact.AbstractionPort, ends, resolve_abstraction: Resolver
    ) -> None:
        name = logical.logical_name
        mapped: List[Tuple[_Terminal, Optional[_Bounds]]] = []
        modes: List[ipxact.InterfaceMode] = []  # the mode of each mapped end
        whole_ties: List[Tuple[int, str]] = []
        sliced_ties: List[Tuple[int, _Bounds, str, bool]] = []
        tied_in_a_drivers_place: List[str] = []
        for owner, port_maps, excluded, mode in ends:
            if name in excluded:
                continue
            for port_map in port_maps:
                if port_map.logical_port != name or port_map.is_informative:
                    continue
                bounds = self._logical_bounds(port_map, owner, name, where)
                if port_map.physical_port is None:
                    if port_map.logical_tie_off is not None:
                        if port_map.invert:
                            raise ValueError(f"{where}: {owner.name} ties {name!r} off with invert, which the standard gives no meaning to here")
                        value = number_of(port_map.logical_tie_off, owner.resolve, where, constant=True)
                        origin = f"tied off by {owner.name}"
                        drives = self._end_would_drive(logical, owner, mode)
                        if bounds is None:
                            whole_ties.append((value, origin))
                            if drives:
                                tied_in_a_drivers_place.append(owner.name)
                        else:
                            sliced_ties.append((value, bounds, origin, drives))
                    continue
                terminal = self._terminal(
                    owner, port_map.physical_port, port_map.part_select, port_map.sub_port_refs, port_map.invert, where
                )
                if terminal is not None:
                    mapped.append((terminal, bounds))
                    modes.append(mode)

        if len({value for value, _ in whole_ties}) > 1 or (whole_ties and sliced_ties):
            raise ValueError(f"{where}: logical port {name!r} is tied off more than one way")
        driving = [t.port.label for t, _ in mapped if t.port.direction is _OUT]
        if tied_in_a_drivers_place and driving:
            raise ValueError(
                f"{where}: logical port {name!r} is driven by {', '.join(driving)} and tied off by {', '.join(tied_in_a_drivers_place)}"
            )
        if driving:  # a slice tied off on a receiving end only says that end has nothing to connect
            sliced_ties = [tie for tie in sliced_ties if tie[3]]
        wire = logical.wire
        constant = None
        if whole_ties:
            constant = whole_ties[0]
        elif wire is not None and wire.default_value is not None and not sliced_ties:
            constant = (number_of(wire.default_value, resolve_abstraction, where, constant=True), _ABSTRACTION_DEFAULT)

        # IEEE 1685-2022 B.1.7 b): with no logical range, a wire port maps logical bits [its width - 1:0], so ends
        # of different widths share only their low logical bits. The net connects them bit by bit, and refuses a
        # bit read that no end drives, which the standard leaves open (D29).
        unranged = {t.width for t, bounds in mapped if bounds is None}
        uneven = len(unranged) > 1 and None not in unranged
        if uneven or any(bounds is not None for _, bounds in mapped):
            # Bits are picked by their logical number here, so the numbers have to be ones the logical port has.
            for (terminal, bounds), mode in zip(mapped, modes):
                self._check_within_width(logical, mode, bounds, terminal, resolve_abstraction, where)
        if sliced_ties or uneven or any(bounds is not None or t.port.direction is _INOUT for t, bounds in mapped):
            self._wire_through_net(block, where, f"{connection}_{name}", name, mapped, constant, sliced_ties)
            return
        drivers = [t for t, _ in mapped if t.port.direction is _OUT]
        sinks = [t for t, _ in mapped if t.port.direction is _IN]
        if len(drivers) > 1:
            raise ValueError(f"{where}: logical port {name!r} has more than one driver: {', '.join(d.port.label for d in drivers)}")
        if drivers:
            driver = drivers[0]
            if not sinks:
                driver.port.left_open = True  # the bus has no receiver for it, not a top-level signal
            for sink in sinks:
                self._assign(block, sink, dataclasses.replace(driver, invert=driver.invert != sink.invert), where, name)
        elif constant is not None:
            value, origin = constant
            for sink in sinks:
                self._assign(block, sink, _fitting_constant(value, sink.size, where, sink.invert), where, f"{name}, {origin}", value)
                if origin == _ABSTRACTION_DEFAULT:
                    self.abstraction_defaults.append((block, block.assigns[-1]))
        # else no driver on this bus: the receivers stay unconnected and are handled like any other

    def _wire_through_net(
        self,
        block: _Block,
        where: str,
        net_name: str,
        note: str,
        mapped: List[Tuple[_Terminal, Optional[_Bounds]]],
        constant: Optional[Tuple[int, str]],
        sliced_ties: Sequence[Tuple[int, _Bounds, str, bool]] = (),
    ) -> None:
        """Connect through a net of its own: for a signal mapped in slices, or shared by inouts."""
        drivers = [(t, b) for t, b in mapped if t.port.direction is _OUT]
        sinks = [(t, b) for t, b in mapped if t.port.direction is _IN]
        inouts = [(t, b) for t, b in mapped if t.port.direction is _INOUT]
        if not inouts:
            if not sinks:
                for terminal, _ in drivers:
                    terminal.port.left_open = True
                return
            if not drivers and constant is None and not sliced_ties:
                return  # no driver on this bus: the receivers stay unconnected
        if not mapped:
            return  # only tie-offs, with nothing to reach
        if any(t.width is None and b is None for t, b in mapped):
            raise ValueError(f"{where}: a port of unknown width with a logical range or inout is not supported yet")

        spans = [max(b) + 1 if b is not None else t.width for t, b in mapped] + [max(tie[1]) + 1 for tie in sliced_ties]
        width = max(spans)
        if constant is not None and not drivers and not inouts:  # the receivers may each take only part of it
            width = max(width, constant[0].bit_length())
        net = self._new_shared_net(net_name, width, inouts, mapped, where)
        if inouts:  # an inout has to span the whole net, so the rest has to match it as well
            for terminal, bounds in [*drivers, *sinks]:
                if bounds is None and terminal.width != width:
                    raise ValueError(f"{where}: width mismatch, {terminal.port.label}{terminal.select} is {terminal.width} bits and the inout is {width}")

        for terminal, bounds in drivers:
            self._assign(block, self._net_slice(net, bounds, terminal, where), terminal, where, note)
        for value, bounds, origin, _ in sliced_ties:
            part = self._net_slice(net, bounds, None, where)
            self._assign(block, part, _fitting_constant(value, part.width, where), where, f"{note}, {origin}")
        if constant is not None and not drivers:
            value, origin = constant
            # A default only stands in for a missing connection. With another end on the net there is none missing.
            if not inouts or origin != _ABSTRACTION_DEFAULT or (len(inouts) == 1 and not sinks):
                net_terminal = _Terminal(net, Select(), width, set(net.all_keys))
                self._assign(block, net_terminal, _fitting_constant(value, width, where), where, f"{note}, {origin}")
        for terminal, bounds in sinks:
            source = self._net_slice(net, bounds, terminal, where)
            if not inouts and not source.keys <= net.driven:
                missing = sorted(source.keys - net.driven)
                raise ValueError(f"{where}: {terminal.port.label} reads bits {missing} of {note!r}, which nothing drives")
            self._assign(block, terminal, dataclasses.replace(source, invert=terminal.invert), where, note)
            if constant is not None and constant[1] == _ABSTRACTION_DEFAULT and not drivers:
                self.abstraction_defaults.append((block, block.assigns[-1]))

    def _new_shared_net(self, name: str, width: int, inouts, mapped, where: str) -> _Port:
        for terminal, bounds in inouts:
            if bounds is not None or terminal.select:
                raise ValueError(f"{where}: a part select on inout {terminal.port.label} is not supported yet")
            if terminal.invert:
                raise ValueError(f"{where}: an inverted port map on inout {terminal.port.label} is not supported yet")
            if terminal.width != width:
                raise ValueError(f"{where}: width mismatch, inout {terminal.port.label} is {terminal.width} bits and the net is {width}")
            if terminal.port.net is not None and not terminal.port.is_top:
                raise ValueError(f"{where}: inout {terminal.port.label} is already connected to {terminal.port.net}")

        tops = [t.port for t, _ in inouts if t.port.is_top]
        if len(tops) > 1:
            # Two ports of one module can't be the same net without an alias, which tools handle unevenly.
            raise ValueError(f"{where}: connecting two inouts of the module, {', '.join(p.name for p in tops)}, is not supported yet")
        top = tops[0] if tops else None
        if top is not None and top.vectors not in ([], [(width - 1, 0)]) and any(b is not None for _, b in mapped):
            raise ValueError(f"{where}: logical slices on inout {top.name}, a {top.declaration}, are not supported yet")
        declaration = SignalType("wire" if inouts else "logic", () if width == 1 else ((width - 1, 0),))
        if top is not None:
            net_name = top.name
        else:
            net_name = self._free_net_name(re.sub(r"\W", "_", name))
            _check_identifier(net_name, f"{where}: net")
            self.wires[net_name] = declaration
            self.net_owners[net_name] = [t.port for t, _ in inouts]
        for terminal, _ in inouts:
            if not terminal.port.is_top:
                terminal.port.net = net_name
        net = _Port("", net_name, _IN, declaration, width, set(range(width)), is_top=True, net=net_name)
        if top is None:
            self.net_owners[net_name].append(net)
        return net

    @staticmethod
    def _net_slice(net: _Port, bounds: Optional[_Bounds], terminal: Optional[_Terminal], where: str) -> _Terminal:
        """The part of the net a port map uses. With no logical range, that is from bit 0."""
        left, right = bounds if bounds is not None else (terminal.width - 1, 0)
        whole = right == 0 and left == net.width - 1
        return _Terminal(net, Select() if whole else Select((left, right)), left - right + 1, set(range(right, left + 1)))

    def _wire_ad_hoc(self, connection: ipxact.AdHocConnection) -> None:
        where = f"ad hoc connection {connection.name!r}"
        internal = [
            self._terminal(self.instances[r.component_instance_ref], r.port_ref, r.part_select, r.sub_port_refs, False, where, self.design_resolve)
            for r in connection.internal_port_references
        ]
        internal = [t for t in internal if t is not None]
        external = [self._external_terminal(r, internal, where) for r in connection.external_port_references]
        terminals = [*internal, *[t for t in external if t is not None]]
        block = _Block([f"{connection.name}: ad hoc"])

        if connection.tied_value is not None:
            self._wire_tied(connection, terminals, block, where)
        elif any(t.port.direction is _INOUT for t in terminals):
            self._wire_through_net(block, where, connection.name, "", [(t, None) for t in terminals], None)
        else:
            drivers = [t for t in terminals if t.port.direction is _OUT]
            if len(drivers) != 1:
                labels = ", ".join(t.port.label for t in terminals)
                raise ValueError(f"{where}: needs exactly one driver among {labels}, it has {len(drivers)}")
            for sink in terminals:
                if sink.port.direction is _IN:
                    self._assign(block, sink, drivers[0], where)
        self.blocks.append(block)

    def _external_terminal(self, reference: ipxact.ExternalPortReference, internal: List[_Terminal], where: str) -> Optional[_Terminal]:
        """A port of the module itself. With no hierarchical component, the connection defines it."""
        if self.top_component is None and reference.port_ref not in self.top.ports:
            if reference.part_select is not None or reference.sub_port_refs or any(t.select for t in internal):
                raise ValueError(f"{where}: a part select with an external port needs a hierarchical component in the library")
            if not internal:
                raise ValueError(f"{where}: an external port needs at least one instance port")
            if len({t.width for t in internal}) > 1:
                raise ValueError(f"{where}: width mismatch between {', '.join(t.port.label for t in internal)}")
            directions = {t.port.direction for t in internal}
            inside = _INOUT if _INOUT in directions else _IN if _OUT in directions else _OUT
            model = internal[0].port
            _check_identifier(reference.port_ref, f"{where}: top-level port")
            self.top.ports[reference.port_ref] = dataclasses.replace(
                model, instance=self.module, name=reference.port_ref, direction=inside, all_keys=set(model.all_keys),
                is_top=True, net=reference.port_ref, driven=set(), read=False, left_open=False,
            )
        return self._terminal(self.top, reference.port_ref, reference.part_select, reference.sub_port_refs, False, where, self.design_resolve)

    def _wire_tied(self, connection, terminals: List[_Terminal], block: _Block, where: str) -> None:
        text = text_of(connection.tied_value)
        for terminal in terminals:
            port = terminal.port
            if text == "open":
                if terminal.select:
                    # Leaving the port open would leave its other bits open as well, without a word.
                    raise ValueError(f"{where}: only a whole port can be left open, not {port.label}{terminal.select}")
                port.left_open = True
                continue
            if port.direction is _OUT:
                raise ValueError(f"{where}: cannot tie {port.label}, which drives its own value")
            if text == "default":
                value = self._tied_to_default(terminal, where)
                if value is None:
                    continue
            else:
                value = number_of(text, self.design_resolve, where, constant=True)
            self._assign(block, terminal, _fitting_constant(value, terminal.size, where), where, value=value)

    def _tied_to_default(self, terminal: _Terminal, where: str) -> Optional[int]:
        """The value of a tiedValue "default", or None when the abstraction's default already applies.
        IEEE 1685-2022 Table C.2: the port's own default wins over its abstraction's, and the
        abstraction's still applies to a port that has none of its own."""
        port = terminal.port
        if port.width is None:
            return self._whole_default(terminal, where)
        bits = self._port_default_bits(port, where)
        from_abstraction = self._abstraction_default_on(terminal)
        if bits is None:
            if from_abstraction is not None:
                return None
            raise ValueError(f"{where}: tied to 'default', but {port.label} has no default value")
        missing = sorted(key for key in terminal.keys if key not in bits)
        if missing:
            raise ValueError(f"{where}: tied to 'default', but {port.label} has no default value for bits {missing}")
        if from_abstraction is not None:
            self._drop_abstraction_default(from_abstraction)
        ascending = bool(port.vectors) and port.vectors[0][0] < port.vectors[0][1]
        return sum(bits[key] << position for position, key in enumerate(sorted(terminal.keys, reverse=ascending)))

    def _whole_default(self, terminal: _Terminal, where: str) -> Optional[int]:
        """The default of a port whose width only the compiler knows, such as a struct or an enum, tied whole
        (D27). It takes no bits to read: one default for the whole port is one constant, cast to the type and
        checked by the compiler like any other (D20, D21). A default given in parts would need its bits."""
        port = terminal.port
        if terminal.select:
            raise ValueError(f"{where}: tying part of {port.label}, a {port.declaration}, to its default is not supported yet")
        owner, drivers = self._default_drivers(port)
        from_abstraction = self._abstraction_default_on(terminal)
        if not drivers:
            if from_abstraction is not None:
                return None
            raise ValueError(f"{where}: tied to 'default', but {port.label} has no default value")
        if len(drivers) > 1 or drivers[0].range is not None:
            raise ValueError(f"{where}: tying {port.label}, a {port.declaration}, to a default given in parts is not supported yet")
        value = number_of(drivers[0].default_value, owner.resolve, where, constant=True)
        if from_abstraction is not None:
            self._drop_abstraction_default(from_abstraction)
        return value

    def _default_drivers(self, port: _Port) -> Tuple[_Instance, List[ipxact.Driver]]:
        """The drivers with a default value that apply to a port in its owner's view."""
        owner = self.top if port.is_top else self.instances[port.instance]
        component = self.top_component if port.is_top else self.linked.instances[port.instance].component
        if component is None:
            return owner, []
        wire = next((p.wire for p in component.model.ports if p.name == port.name), None)
        return owner, [d for d in for_view(wire.drivers if wire is not None else [], owner.view) if d.default_value is not None]

    def _abstraction_default_on(self, terminal: _Terminal) -> Optional[Tuple[_Block, _Assign]]:
        for entry in self.abstraction_defaults:
            sink = entry[1].sink
            if sink.port is terminal.port and sink.keys == terminal.keys:
                return entry
        return None

    def _drop_abstraction_default(self, entry: Tuple[_Block, _Assign]) -> None:
        block, assign = entry
        block.assigns = [a for a in block.assigns if a is not assign]
        assign.sink.port.driven -= assign.sink.keys
        self.abstraction_defaults.remove(entry)

    def _port_default_bits(self, port: _Port, where: str) -> Optional[Dict[object, int]]:
        """Each bit of a port's own default value, by its index in the port. A driver can cover part
        of the port, and can apply to some views only. None when the port has no default here."""
        component = self.top_component if port.is_top else self.linked.instances[port.instance].component
        if component is None:  # a port of the module with no hierarchical component: only the design defines it
            return None
        owner, drivers = self._default_drivers(port)
        if not drivers:
            return None
        if port.width is None:
            raise ValueError(f"{where}: tying {port.label}, a {port.declaration}, to its default is not supported yet")
        bits: Dict[object, int] = {}
        right_end = port.vectors[0][1] if len(port.vectors) == 1 else min(port.all_keys)
        for driver in drivers:
            value = number_of(driver.default_value, owner.resolve, where, constant=True)
            if driver.range is None:
                _check_fits(value, len(port.all_keys), f"{where}: the default of {port.label}")
                bits.update({key: (value >> abs(key - right_end)) & 1 for key in port.all_keys})
            else:
                left = number_of(driver.range.left, owner.resolve, where)
                right = number_of(driver.range.right, owner.resolve, where)
                low, high = sorted((left, right))
                _check_fits(value, high - low + 1, f"{where}: the default of {port.label}[{left}:{right}]")
                bits.update({key: (value >> abs(key - right)) & 1 for key in range(low, high + 1)})
        return bits

    def _connect_top_ports_directly(self) -> None:
        """A whole port wired to one of the module's own ports needs no net in between. The instance's port
        then goes by the module port's name, so a part of it taken elsewhere, `[3:0]` or `.addr`, has to mean
        the same bits of both: a port that has parts taken only connects directly when the two number their
        bits alike, and are of one type where a part goes by a member's name (D35)."""
        in_parts = set()  # the ports some connection takes a part of
        for block in self.blocks:
            for assign in block.assigns:
                for end in (assign.sink, assign.source):
                    if isinstance(end, _Terminal) and end.select:
                        in_parts.add(id(end.port))
        for block in self.blocks:
            kept = []
            for assign in block.assigns:
                sink, source = assign.sink, assign.source
                whole = isinstance(source, _Terminal) and not (source.invert or source.select or sink.select)
                whole = whole and (_parts_alike(sink.port, source.port) or not {id(sink.port), id(source.port)} & in_parts)
                if whole and self._is_module_port(source.port) and not self._is_module_port(sink.port) and sink.port.net is None and source.port.direction is _OUT:
                    sink.port.net = source.port.net
                elif whole and self._is_module_port(sink.port) and not self._is_module_port(source.port) and source.port.net is None and sink.port.direction is _IN:
                    source.port.net = sink.port.net
                else:
                    kept.append(assign)
            block.assigns = kept

    def _is_module_port(self, port: _Port) -> bool:
        return port.is_top and self.top.ports.get(port.name) is port

    def _free_net_name(self, wanted: str) -> str:
        """A net's name is ours to choose, so one that is taken or a keyword just changes."""
        name, tries = wanted, 1
        while self._is_taken(name):
            name, tries = f"{wanted}_net{tries if tries > 1 else ''}", tries + 1
        return name

    def _is_taken(self, name: str) -> bool:
        """Whether the module's one scope already has this name, or SystemVerilog does."""
        return (
            name in self.wires
            or name in self.top.ports
            or name in self.instances
            or name in SYSTEMVERILOG_KEYWORDS
            or any(name == parameter.name for parameter in self.declared)
        )

    def _new_wire(self, port: _Port) -> str:
        _check_type_is_visible(port)
        name = self._free_net_name(f"{port.instance}_{port.name}")
        self.wires[name] = port.type
        self.net_owners[name] = [port]
        return name

    def _name_nets(self) -> None:
        """Decide each port's net, and what happens to a port no connection reaches."""
        for port in self.top.ports.values():
            missing = port.undriven() if port.direction is _IN else []
            if missing and self.leave_open and not port.driven:
                self.undriven.append(port.name)  # as the design has it, and said under the module's header (D38)
            elif missing:
                what = "output bits" if port.width is not None else "output parts"
                raise ValueError(f"{port.label}: top-level {what} {missing} have no driver")

        inputs = {p.name for i in self.instances.values() for p in i.ports.values() if p.direction is _IN}
        unknown = [name for name in self.common if name not in inputs]
        if unknown and self.must_use_common:
            raise ValueError(f"common ports match no instance input: {', '.join(unknown)}")

        for instance in self.instances.values():
            for port in instance.ports.values():
                if port.net is not None:
                    continue
                if port.unsupported:
                    # Left open it needs no net; an input must be driven, and a module port declared.
                    if self.top_component is None or (port.direction is _IN and not (self.leave_open and not port.driven)):
                        raise ValueError(f"{port.label}: {port.unsupported}, in a module whose ports a component defines")
                    if port.direction is _IN:
                        port.note = _LEFT_OPEN
                    continue
                if port.direction is _IN:
                    self._name_input(port)
                elif port.direction is _INOUT:
                    if port.driven:
                        port.net = self._new_wire(port)  # tied to a constant, by itself
                    elif not port.left_open and self.top_component is None:
                        self._promote(port, shared=False)
                elif port.read:
                    port.net = self._new_wire(port)
                elif not port.left_open and self.top_component is None:
                    self._promote(port, shared=False)

    def _name_input(self, port: _Port) -> None:
        if port.driven:
            missing = port.undriven()
            if missing:
                what = "bits" if port.width is not None else "parts"
                raise ValueError(f"{port.label}: {what} {missing} are not driven by any connection")
            port.net = self._new_wire(port)
        elif port.left_open:
            return
        elif port.name in self.common:
            self._promote(port, shared=True)
        elif self.top_component is None:
            self._promote(port, shared=False)
        elif self.leave_open:
            port.note = _LEFT_OPEN  # as the design has it, and said where the instance is written (D38)
        else:
            # A default value doesn't change this. IEEE 1685-2022 7.5.5: a pin is not connected to its
            # default unless an ad hoc connection ties it to "default".
            raise ValueError(
                f"{port.label} is not connected, and top component {self.top_component.vlnv} defines the module's "
                "ports, so it cannot become one. Connect it, tie it with a tiedValue ('default' for its default "
                "value, 'open' to leave it open), list it in common, or leave every such input open with the "
                "option unconnected=open"
            )

    def _promote(self, port: _Port, shared: bool) -> None:
        """Connect a port no connection reaches to a port of the module. Only shared ones coincide."""
        name = port.name if shared else f"{port.instance}_{port.name}"
        existing = self.top.ports.get(name)
        if existing is None:
            if self.top_component is not None:
                raise ValueError(f"{port.label}: top component {self.top_component.vlnv} has no port {name!r} to share")
            _check_identifier(name, f"{port.label}: top-level port")
            _check_type_is_visible(port)
            self.top.ports[name] = dataclasses.replace(
                port, instance=self.module, name=name, direction=_FROM_INSIDE[port.direction], all_keys=set(port.all_keys),
                is_top=True, net=name, driven=set(), read=False, left_open=False,
            )
        elif not shared:
            raise ValueError(f"{port.label}: top-level port {name!r} is already taken")
        elif existing.direction is not _FROM_INSIDE[port.direction] or existing.declaration != port.declaration:
            raise ValueError(f"{port.label}: shared port {name!r} is {existing.declaration}, and this port is {port.declaration}")
        port.net = name

    def _netlist(self) -> Module:
        """The module as it is wired, for a printer: what there is to write and nothing of how it was worked
        out, with every net under the name it ended up with."""
        remarks = []
        if self.module_name and self.top_component is not None and self.top_overrides:
            names = {p.parameter_id: p.name for p in self.top_parameters}
            given = ", ".join(f"{names[key]} = {one_line(value)}" for key, value in self.top_overrides.items())
            remarks.append(f"The version of {self.top_component.vlnv} with {given}.")
        declared_as = {_OUT: IN, _IN: OUT, _INOUT: INOUT}  # a port's direction is kept here as the inside sees it
        return Module(
            name=self.module,
            source=str(self.linked.vlnv),
            parameters=[Parameter(p.name, p.value, p.type_name, p.integral) for p in self.declared],
            ports=[Port(port.name, declared_as[port.direction], port.type) for port in self.top.ports.values()],
            nets=[Net(name, signal_type) for name, signal_type in self.wires.items()],
            # A connection that came to no assign is left out, unless it is a bus connection or says more.
            blocks=[
                Block([one_line(comment) for comment in block.comment], [self._netlist_assign(assign) for assign in block.assigns])
                for block in self.blocks
                if block.assigns or len(block.comment) > 1 or " <-> " in block.comment[0]
            ],
            instances=[self._netlist_instance(instance) for instance in self.instances.values()],
            remarks=remarks,
            notes=[f"{name}: {_UNDRIVEN}" for name in self.undriven],
        )

    @staticmethod
    def _netlist_assign(assign: _Assign) -> Assign:
        def reference(terminal: _Terminal, invert: bool = False) -> Reference:
            return Reference(str(terminal.port.net), terminal.select, invert)  # every end of an assign has its net by now

        check = None
        if assign.needs is not None:
            # Only the compiler knows how wide a typedef is, so it checks the constant isn't cut short.
            value, bits, where = assign.needs
            message = f"{where}: the constant {value} does not fit in {assign.sink.port.label}{assign.sink.select}"
            check = WidthCheck(bits, message + (f", a {assign.cast}" if assign.cast else ""))
        source = assign.source if isinstance(assign.source, Constant) else reference(assign.source, assign.source.invert)
        return Assign(reference(assign.sink), source, one_line(assign.note) if assign.note else "", assign.cast, check)

    @staticmethod
    def _netlist_instance(instance: _Instance) -> Instance:
        comments = []
        if instance.vlnv is not None:
            comments.append(f"IP-XACT VLNV: {instance.vlnv}")
        if instance.description:
            comments.extend(line.strip() for line in str(instance.description).splitlines() if line.strip())
        if instance.transforms:
            comments.append(f"Transforms recorded but not applied here: {one_line(', '.join(instance.transforms))}")
        # Its ports under the bus interface each belongs to, where the component has any.
        labelled = instance.component is not None and bool(instance.component.bus_interfaces)
        groups = [
            PortGroup(
                ("Interface: " + name if name else "Not in any interface") if labelled else None,
                [Connection(port.name, port.net, port.note) for port in ports],
            )
            for name, ports in _port_groups(instance)
        ]
        return Instance(instance.name, instance.module, list(instance.parameters), groups, comments)
