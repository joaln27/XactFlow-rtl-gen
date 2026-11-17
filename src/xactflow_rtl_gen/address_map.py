"""The address map of a design, as a SystemVerilog package with one rule table per decoder.

The rule shape, an index with a start address and an exclusive end address, is the one PULP's
crossbars and common_cells' addr_decode take, so a table can be handed to them directly.

A decoder is an instance that addresses pass through (D51): it has windows behind its interfaces that send
addresses out, and something connects to one that takes them in, or it declares ADDR_RULES or NUM_RULES, or
a parameter value of the module names its table (D52). A window on anything else, an initiator wired straight to a
target or the module's own interface, is in no table, and a design with no decoder has no package. Every
window is read and checked all the same, whether it ends up in a table or not (D52).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Union

import ipxact
from xactflow import ExpressionError, LinkedDesign, evaluate, parameter_resolver

from .keywords import SYSTEMVERILOG_KEYWORDS

_ATTRIBUTE = re.compile(r"""(baseAddr|size)=["']([^"']+)["']""")
_DEFAULT_WIDTH = 32
_OUTBOUND = (ipxact.InterfaceMode.INITIATOR, ipxact.InterfaceMode.MIRRORED_TARGET)  # what a decoder sends through
_INBOUND = (ipxact.InterfaceMode.TARGET, ipxact.InterfaceMode.MIRRORED_INITIATOR)  # what brings it addresses
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def for_view(items: Iterable, view: Optional[ipxact.View]) -> List:
    """What applies to a view: an item with no viewRef applies to all of them."""
    if view is None:
        return list(items)
    return [item for item in items if not item.view_refs or view.name in {getattr(ref, "value", ref) for ref in item.view_refs}]


def address_window(interface: object) -> Optional[Dict[str, str]]:
    """The <xactflow:addressMapping> attributes on an active interface, as written, if any."""
    for extension in getattr(interface, "vendor_extensions", []):
        if "addressMapping" in str(extension):
            return dict(_ATTRIBUTE.findall(str(extension)))
    return None


@dataclass
class Rule:
    decoder: str  # the instance on the side that sends the address, or the module for a window from outside
    index: int  # which of its initiator interfaces the window is reached through
    interface: str  # that interface's name
    target: str  # "<instance>.<bus interface>" of the target
    base: int
    size: int
    width: Optional[int] = None  # of this window's own bus, when its abstraction says
    name: str = ""  # what its constants are called
    from_outside: bool = False  # reached through one of the module's own interfaces: checked, and in no table

    @property
    def end(self) -> int:
        return self.base + self.size


@dataclass
class AddressMap:
    module: str
    package: str
    width: int
    tables: Dict[str, List[Rule]]
    hosts: Dict[str, int] = field(default_factory=dict)  # how many of each decoder's interfaces that take addresses in are connected
    # The windows of each instance that is a decoder only if a value of the module names its table (D52),
    # until settle_address_map() has seen the values.
    candidates: Dict[str, List[Rule]] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)  # the instances with windows, in the design's order

    def constant(self, decoder: str, what: str) -> str:
        return f"{_constant_name(decoder)}_{what}"


def _constant_name(*parts: str) -> str:
    return "_".join(re.sub(r"\W", "_", part).upper() for part in parts)


def _abstraction_width(linked: LinkedDesign, bus_interface: ipxact.BusInterface, view: Optional[ipxact.View]) -> Optional[int]:
    """The width of the logical port the abstraction of this view marks as the address, when it says."""
    for abstraction_type in for_view(bus_interface.abstraction_types, view):
        definition = linked.library.get_abstraction_definition(abstraction_type.abstraction_ref.vlnv)
        for port in definition.ports if definition is not None else []:
            wire = port.wire
            if wire is None or wire.qualifier is None or not wire.qualifier.is_address:
                continue
            for mode in (wire.on_initiator, wire.on_target):
                if mode is not None and mode.width is not None:
                    try:
                        return int(evaluate(str(mode.width), parameter_resolver(definition.parameters)))
                    except ExpressionError:
                        return None
    return None


def _index_of(interface: ipxact.BusInterface, component: ipxact.Component) -> int:
    """Which of a decoder's interfaces this is, counted in the order the component declares them,
    among the ones that send addresses out. They need not all have the same mode."""
    modes = _OUTBOUND if interface.mode in _OUTBOUND else (interface.mode,)
    return [b.name for b in component.bus_interfaces if b.mode in modes].index(interface.name)


def _rule(linked: LinkedDesign, module: str, view_of: Callable, connection, endpoint) -> Rule:
    """The window recorded on one end of a connection, under the instance on the side that sends the address,
    or under the module when it is reached through one of the module's own interfaces."""
    where = f"interconnection {connection.name!r}"
    this_end = f"{endpoint.instance.instance_name}.{endpoint.bus_interface.name}"
    window = address_window(endpoint.source)
    if "baseAddr" not in window or "size" not in window:
        raise ValueError(f"{where}: the address window of {this_end} needs both baseAddr and size")
    try:
        base, size = int(window["baseAddr"], 0), int(window["size"], 0)
    except ValueError as exc:
        raise ValueError(f"{where}: the address window of {this_end} is not a number: {exc}") from exc
    if size < 1 or base < 0:
        raise ValueError(f"{where}: the address window of {this_end} has base {base} and size {size}")

    width = _abstraction_width(linked, endpoint.bus_interface, view_of(endpoint.instance.instance_name))
    others = [e for e in connection.endpoints if e is not endpoint]
    outbound = [e for e in others if e.bus_interface.mode in _OUTBOUND]
    hier = [h.bus_ref for h in connection.source.hier_interfaces]
    if outbound:  # at most one: link() checks that an interface of these modes only connects to targets
        decoder, target = outbound[0], this_end
    elif endpoint.bus_interface.mode in _OUTBOUND and (others or hier):
        # The window is on the decoder's own side, and the target is the other end.
        ends = [*(f"{e.instance.instance_name}.{e.bus_interface.name}" for e in others), *(f"{module}.{name}" for name in hier)]
        if len(ends) > 1:
            raise ValueError(f"{where}: cannot tell which of {', '.join(ends)} the address window on {this_end} belongs to")
        decoder, target = endpoint, ends[0]
    elif not others:
        # Reached from outside this module: what decodes it is in the design that instantiates the module.
        return Rule(module, 0, connection.name, this_end, base, size, width, from_outside=True)
    elif len(others) == 1:
        decoder, target = others[0], this_end
    else:
        names = ", ".join(f"{e.instance.instance_name}.{e.bus_interface.name}" for e in others)
        raise ValueError(f"{where}: cannot tell which of {names} decodes the address window of {this_end}")
    index = _index_of(decoder.bus_interface, decoder.instance.component)
    return Rule(decoder.instance.instance_name, index, decoder.bus_interface.name, target, base, size, width)


def _name_windows(decoder: str, rules: List[Rule]) -> None:
    """Say what each window's constants are called: after the decoder and its interface, and after the target
    too when one interface reaches several."""
    behind = Counter(rule.interface for rule in rules)
    for rule in rules:
        parts = (decoder, rule.interface) if behind[rule.interface] == 1 else (decoder, rule.interface, rule.target)
        rule.name = _constant_name(*parts)


_OF_A_TABLE = ("NUM_RULES", "ADDR_RULES", "ADDR_RULES_t", "ADDR_PAIRS")
_OF_A_WINDOW = ("BASE", "END", "SIZE")


def _constants_of(decoder: str, rules: List[Rule]) -> Set[str]:
    """Every name the package defines for one decoder's table, once its windows are named."""
    return {f"{_constant_name(decoder)}_{what}" for what in _OF_A_TABLE} | {f"{rule.name}_{what}" for rule in rules for what in _OF_A_WINDOW}


def _claim_constants(tables: Dict[str, List[Rule]]) -> None:
    """Refuse names that come out the same: they are upper case, and SystemVerilog names may differ only in case."""
    owners: Dict[str, str] = {}

    def claim(name: str, owner: str) -> None:
        if owners.setdefault(name, owner) != owner:
            raise ValueError(f"{owners[name]} and {owner} would both define the constant {name} in the address map package")

    for decoder, rules in tables.items():
        for what in ("NUM_RULES", "ADDR_RULES", "ADDR_PAIRS"):
            claim(f"{_constant_name(decoder)}_{what}", decoder)
        behind = Counter(rule.interface for rule in rules)
        for rule in rules:
            claim(rule.name, f"{decoder}.{rule.interface}" if behind[rule.interface] == 1 else f"{decoder}.{rule.interface} to {rule.target}")


def _width_of(rules: List[Rule]) -> int:
    """The address width a set of windows needs: the widest their buses say, and 32 bits for a bus that says
    none, or 64 when one of its windows needs it."""
    widths = {rule.width for rule in rules if rule.width is not None}
    unsaid = [rule for rule in rules if rule.width is None]
    if unsaid:
        widths.add(64 if max(rule.end for rule in unsaid) > 1 << _DEFAULT_WIDTH else _DEFAULT_WIDTH)
    return max(widths)


def _check_windows(rules: List[Rule], width: int, together: str) -> None:
    """Refuse a window that passes the end of its bus's address space, and two that overlap."""
    by_address = sorted(rules, key=lambda rule: rule.base)
    for rule in by_address:
        limit = rule.width if rule.width is not None else width
        if rule.end > 1 << limit:
            raise ValueError(f"the address window of {rule.target} ends at {rule.end:#x}, past the {limit}-bit address space")
    for lower, upper in zip(by_address, by_address[1:]):
        if lower.end > upper.base:
            raise ValueError(
                f"the address windows of {lower.target} and {upper.target} overlap: "
                f"{lower.base:#x}-{lower.end:#x} and {upper.base:#x}-{upper.end:#x}, {together}"
            )


def _declares_a_table(component: ipxact.Component, view: Optional[ipxact.View]) -> bool:
    """Whether the module of an instance has a parameter for its rule table or its count (D7)."""
    instantiations = component.model.component_instantiations if component.model is not None else []
    if view is not None and view.component_instantiation_ref:
        instantiations = [i for i in instantiations if i.name == view.component_instantiation_ref]
    return any(p.name in ("ADDR_RULES", "NUM_RULES") for i in instantiations for p in i.module_parameters)


def build_address_map(linked: LinkedDesign, module: str, view_of: Callable = lambda name: None) -> Optional[AddressMap]:
    """The design's address map as far as the design alone says, or None when it has no window behind an
    instance. `view_of` gives an instance's view. Every window the design records is read and checked, one in
    no table like any other (D52). The tables are those of the instances that are decoders by what is
    connected to them or by what their module declares (D51); the windows of the others wait in
    `candidates` for settle_address_map(), which is what says whether there is a package."""
    windows: Dict[str, List[Rule]] = {}  # by the instance on the side that sends the address
    outside: List[Rule] = []  # reached through the module's own interfaces
    for connection in linked.interconnections:
        for endpoint in connection.endpoints:
            if address_window(endpoint.source) is None:
                continue
            rule = _rule(linked, module, view_of, connection, endpoint)
            (outside if rule.from_outside else windows.setdefault(rule.decoder, [])).append(rule)
    if not windows and not outside:
        return None

    width_of_all = _width_of([*outside, *(rule for rules in windows.values() for rule in rules)])
    for name, rules in windows.items():
        _check_windows(rules, width_of_all, f"both decoded by {name}")
        _name_windows(name, rules)
    _check_windows(outside, width_of_all, f"both reached through the interfaces of {module}")
    if not windows:
        return None

    # How many of each instance's interfaces that take addresses in are connected.
    hosts = Counter(
        instance
        for instance, _ in {
            (endpoint.instance.instance_name, endpoint.bus_interface.name)
            for connection in linked.interconnections
            for endpoint in connection.endpoints
            if endpoint.bus_interface.mode in _INBOUND
        }
    )
    tables = {
        name: rules
        for name, rules in windows.items()
        if hosts[name] or _declares_a_table(linked.instances[name].component, view_of(name))
    }
    _claim_constants(tables)
    candidates = {name: rules for name, rules in windows.items() if name not in tables}
    return AddressMap(module, f"{module}_addr_map_pkg", 0, tables, {name: hosts[name] for name in tables}, candidates, list(windows))


def settle_address_map(address_map: Optional[AddressMap], values: Iterable[str]) -> Optional[AddressMap]:
    """The address map once the module's parameter values are known, or None when the design has no decoder
    (D51). `values` are those the module gives its instances and declares for itself. An instance that is a
    decoder for no other reason is one if a value names a constant of its table, such as
    `soc_addr_map_pkg::DEC_ADDR_PAIRS`: that is how a decoder with no ADDR_RULES is pointed at its table
    (D7), and the module would not compile without the package (D52)."""
    if address_map is None:
        return None
    if address_map.candidates:
        names = re.compile(rf"(?<![\w$]){re.escape(address_map.package)}::(\w+)")
        named = {name for value in values for name in names.findall(value)}
        for decoder in [name for name, rules in address_map.candidates.items() if named & _constants_of(name, rules)]:
            address_map.tables[decoder] = address_map.candidates.pop(decoder)
            address_map.hosts[decoder] = 0
    if not address_map.tables:
        return None
    address_map.tables = {name: address_map.tables[name] for name in address_map.order if name in address_map.tables}
    for rules in address_map.tables.values():
        rules.sort(key=lambda rule: (rule.index, rule.base))
    _claim_constants(address_map.tables)
    address_map.width = _width_of([rule for rules in address_map.tables.values() for rule in rules])
    return address_map


def render_address_map(address_map: AddressMap, header: str) -> str:
    width = address_map.width
    digits = (width + 3) // 4

    def literal(value: int) -> str:
        # A window that reaches the very top ends at 2**width, which wraps to 0 here. Decoders that
        # take this rule shape read an end of 0 as the end of the address space. On a narrower bus in
        # a wider table, the true end is kept: 0 would claim all the addresses above it as well.
        return f"{width}'h{value & ((1 << width) - 1):0{digits}x}"

    lines = [
        header,
        "",
        f"package {address_map.package};",
        "",
        "    typedef struct packed {",
        "        int unsigned idx;  // which initiator interface of the decoder the window is behind",
        f"        logic [{width - 1}:0] start_addr;",
        f"        logic [{width - 1}:0] end_addr;  // exclusive, and 0 means the end of the address space",
        "    } addr_rule_t;",
        "",
    ]
    for decoder, rules in address_map.tables.items():
        lines.append(f"    // Decoded by {decoder}")
        for rule in rules:
            name = rule.name
            lines.append(f"    // {decoder}.{rule.interface} reaches {rule.target}")
            lines.append(f"    localparam logic [{width - 1}:0] {name}_BASE = {literal(rule.base)};")
            lines.append(f"    localparam logic [{width - 1}:0] {name}_END  = {literal(rule.end)};")
            if rule.size >> width:  # the whole address space, one bit more than an address has
                lines.append(f"    localparam logic [{width}:0] {name}_SIZE = {width + 1}'h{rule.size:x};")
            else:
                lines.append(f"    localparam logic [{width - 1}:0] {name}_SIZE = {literal(rule.size)};")
        count = address_map.constant(decoder, "NUM_RULES")
        table = address_map.constant(decoder, "ADDR_RULES")
        lines.append(f"    localparam int unsigned {count} = {len(rules)};")
        # A concatenation of {idx, start_addr, end_addr}, the highest rule first: Icarus Verilog does
        # not read an assignment pattern on a packed array. The cast keeps slang from warning that
        # the bits are converted implicitly.
        table_type = f"{table}_t"
        lines.append(f"    typedef addr_rule_t [{count}-1:0] {table_type};")
        lines.append(f"    localparam {table_type} {table} = {table_type}'({{")
        entries = [f"        {{32'd{rule.index}, {literal(rule.base)}, {literal(rule.end)}}}" for rule in reversed(rules)]
        lines.append(",\n".join(entries))
        lines.append("    });")
        # The same windows as a flat vector, for a decoder that takes them that way.
        pairs = ", ".join(f"{literal(rule.base)}, {literal(rule.end)}" for rule in rules)
        lines.append("    // Start and end of each window, rule 0 in the most significant bits")
        lines.append(f"    localparam logic [{2 * width * len(rules) - 1}:0] {address_map.constant(decoder, 'ADDR_PAIRS')} = {{{pairs}}};")
        lines.append("")
    lines.append("endpackage")
    return "\n".join(lines) + "\n"


# PeakRDL-socgen's layout of an address map package, for files written against its package: what its
# output looks like, not its code (a GPL-3 template). An interconnect's constants start with its name, with
# "interconnect" shortened to "intc", and a window's with the name of what it reaches, then the interconnect's.
SOCGEN_WIDTH = 32


def socgen_names(value: Union[str, Mapping[str, str], Sequence[str], None]) -> Dict[str, str]:
    """The socgen_names option: what socgen calls an instance, as `instance:name` pairs, comma-separated
    from the CLI, or a dict or list of pairs from Python."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        pairs = list(value.items())
    else:
        items = value.split(",") if isinstance(value, str) else list(value)
        pairs = []
        for item in (item.strip() for item in items):
            if not item:
                continue
            if item.count(":") != 1:
                raise TypeError(f"xactflow-rtl-gen: socgen_names takes instance:name pairs, got {item!r}")
            instance, name = (part.strip() for part in item.split(":"))
            pairs.append((instance, name))
    names: Dict[str, str] = {}
    for instance, name in pairs:
        for what, text in (("instance", instance), ("name", name)):
            if not isinstance(text, str) or not _IDENTIFIER.fullmatch(text):
                raise TypeError(f"xactflow-rtl-gen: socgen_names: {what} {text!r} is not an identifier")
        if names.setdefault(instance, name) != name:
            raise TypeError(f"xactflow-rtl-gen: socgen_names gives {instance} two names, {names[instance]} and {name}")
    return names


def render_socgen_package(address_maps: Sequence[AddressMap], package: str, names: Mapping[str, str], header: str) -> str:
    """The windows of every module generated, in one package with PeakRDL-socgen's layout.

    Per interconnect: <INTC>_NMASTER, the interfaces that bring it addresses and are connected; <INTC>_NSLAVE,
    its windows; <INTC>_BASE_ADDRESS, where the lowest of them starts (socgen writes the base of the subsystem,
    the same whenever its first window is at offset 0); and <INTC>_ADDR_RULES, the rule table, its first
    window in the highest element. Per window: <NAME>_<INTC>_START_ADDRESS, _SIZE, _END_ADDRESS and _IDX.
    Its addresses are 32 bits, like socgen's, so a wider map is refused."""
    if not _IDENTIFIER.fullmatch(package) or package in SYSTEMVERILOG_KEYWORDS:
        raise TypeError(f"xactflow-rtl-gen: socgen_package {package!r} is not a package name")
    owners: Dict[str, str] = {}

    def claim(name: str, owner: str) -> str:
        if owners.setdefault(name, owner) != owner:
            raise ValueError(f"in the socgen package, {owners[name]} and {owner} would both define {name}: give one another name with socgen_names")
        return name

    def literal(value: int) -> str:
        return f"32'h{value & 0xFFFF_FFFF:08x}"

    known = set()
    declarations: List[str] = []
    tables: List[str] = []
    for address_map in address_maps:
        if address_map.width > SOCGEN_WIDTH:
            raise ValueError(f"module {address_map.module}: its address map is {address_map.width} bits wide, and the socgen package's addresses are {SOCGEN_WIDTH}")
        for decoder, rules in address_map.tables.items():
            known.add(decoder)
            where = f"{decoder} in {address_map.module}"
            intc = _constant_name(names.get(decoder, decoder).replace("interconnect", "intc"))
            base = min(rule.base for rule in rules)
            declarations += [
                f"    // Decoded by {where}",
                f"    localparam {claim(f'{intc}_NMASTER', where)} = {address_map.hosts.get(decoder, 0)};",
                f"    localparam {claim(f'{intc}_NSLAVE', where)}  = {len(rules)};",
                f"    localparam {claim(f'{intc}_BASE_ADDRESS', where)} = {literal(base)};",
                "",
            ]
            entries = []
            for rule in rules:
                if rule.size > 0xFFFF_FFFF:
                    raise ValueError(f"the address window of {rule.target}, decoded by {where}, is {rule.size:#x} bytes, which 32 bits cannot hold")
                target = rule.target.split(".")[0]
                known.add(target)
                prefix = _constant_name(names.get(target, target), intc)
                owner = f"the window of {rule.target}, decoded by {where}"
                start, size, end, idx = (claim(f"{prefix}_{what}", owner) for what in ("START_ADDRESS", "SIZE", "END_ADDRESS", "IDX"))
                declarations += [
                    f"    // {decoder}.{rule.interface} reaches {rule.target}",
                    f"    localparam logic [31:0] {start} = {intc}_BASE_ADDRESS + {literal(rule.base - base)};",
                    f"    localparam logic [31:0] {size} = {literal(rule.size)};",
                    f"    localparam logic [31:0] {end} = {start} + {size};",
                    f"    localparam logic [31:0] {idx} = 32'd{rule.index};",
                    "",
                ]
                entries.append(f"        {{{idx}, {start}, {end}}}")
            table = claim(f"{intc}_ADDR_RULES", where)
            table_type = claim(f"{table}_t", where)
            # A concatenation, the first window in the highest element as in socgen's, for the reason
            # render_address_map gives. The cast keeps slang from warning.
            tables += [
                f"    typedef addr_map_rule_t [{intc}_NSLAVE-1:0] {table_type};",
                f"    localparam {table_type} {table} = {table_type}'({{",
                ",\n".join(entries),
                "    });",
                "",
            ]
    if not declarations:
        raise ValueError(f"the socgen_package option asks for {package}, and no module generated here has a decoder with an address window")
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"socgen_names names {', '.join(unknown)}, which no address window here is decoded by or reaches")
    claim("addr_map_rule_t", "the rule type")
    lines = [
        header,
        "// The address windows of every module generated with this one, in PeakRDL-socgen's layout, for files",
        "// written against its package. The modules themselves take theirs from their own <module>_addr_map_pkg.",
        "",
        f"package {package};",
        "",
        "    typedef struct packed {",
        "        logic [31:0] idx;",
        "        logic [31:0] start_addr;",
        "        logic [31:0] end_addr;  // exclusive: start_addr + size",
        "    } addr_map_rule_t;",
        "",
        *declarations,
        *tables,
        "endpackage",
    ]
    return "\n".join(lines) + "\n"
