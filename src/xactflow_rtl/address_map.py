"""The address map of a design, as a SystemVerilog package with one rule table per decoder.

The rule shape, an index with a start address and an exclusive end address, is the one PULP's
crossbars and common_cells' addr_decode take, so a table can be handed to them directly.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

import ipxact
from xactflow import ExpressionError, LinkedDesign, evaluate, parameter_resolver

_ATTRIBUTE = re.compile(r"""(baseAddr|size)=["']([^"']+)["']""")
_DEFAULT_WIDTH = 32
_OUTBOUND = (ipxact.InterfaceMode.INITIATOR, ipxact.InterfaceMode.MIRRORED_TARGET)  # what a decoder sends through


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
    decoder: str  # the instance that decodes the address
    index: int  # which of its initiator interfaces the window is reached through
    interface: str  # that interface's name
    target: str  # "<instance>.<bus interface>" of the target
    base: int
    size: int
    width: Optional[int] = None  # of this window's own bus, when its abstraction says
    name: str = ""  # what its constants are called
    from_outside: bool = False  # reached through the module's own interface, not decoded by an instance

    @property
    def end(self) -> int:
        return self.base + self.size


@dataclass
class AddressMap:
    module: str
    package: str
    width: int
    tables: Dict[str, List[Rule]]

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


def _rule(linked: LinkedDesign, module: str, top_component: Optional[ipxact.Component], view_of: Callable, connection, endpoint) -> Rule:
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
    hier = [h.bus_ref for h in connection.hier_interfaces]
    if outbound:  # at most one: link() checks that an interface of these modes only connects to targets
        decoder, target = outbound[0], this_end
    elif endpoint.bus_interface.mode in _OUTBOUND and (others or hier):
        # The window is on the decoder's own side, and the target is the other end.
        ends = [*(f"{e.instance.instance_name}.{e.bus_interface.name}" for e in others), *(f"{module}.{name}" for name in hier)]
        if len(ends) > 1:
            raise ValueError(f"{where}: cannot tell which of {', '.join(ends)} the address window on {this_end} belongs to")
        decoder, target = endpoint, ends[0]
    elif not others:
        # Reached from outside this module, through one of its own interfaces.
        declared = [b.name for b in top_component.bus_interfaces] if top_component is not None else []
        index = declared.index(hier[0]) if hier and hier[0] in declared else 0
        return Rule(module, index, connection.name, this_end, base, size, width, from_outside=True)
    elif len(others) == 1:
        decoder, target = others[0], this_end
    else:
        names = ", ".join(f"{e.instance.instance_name}.{e.bus_interface.name}" for e in others)
        raise ValueError(f"{where}: cannot tell which of {names} decodes the address window of {this_end}")
    index = _index_of(decoder.bus_interface, decoder.instance.component)
    return Rule(decoder.instance.instance_name, index, decoder.bus_interface.name, target, base, size, width)


def _name_constants(tables: Dict[str, List[Rule]]) -> None:
    """Name each window's constants, refusing names that come out the same: they are upper case, and
    SystemVerilog names may differ only in case."""
    owners: Dict[str, str] = {}

    def claim(name: str, owner: str) -> None:
        if owners.setdefault(name, owner) != owner:
            raise ValueError(f"{owners[name]} and {owner} would both define the constant {name} in the address map package")

    for decoder, rules in tables.items():
        for what in ("NUM_RULES", "ADDR_RULES", "ADDR_PAIRS"):
            claim(f"{_constant_name(decoder)}_{what}", decoder)
        behind = Counter(rule.interface for rule in rules)
        for rule in rules:
            parts = (decoder, rule.interface) if behind[rule.interface] == 1 else (decoder, rule.interface, rule.target)
            rule.name = _constant_name(*parts)
            claim(rule.name, f"{decoder}.{rule.interface}" if behind[rule.interface] == 1 else f"{decoder}.{rule.interface} to {rule.target}")


def build_address_map(
    linked: LinkedDesign, module: str, top_component: Optional[ipxact.Component] = None, view_of: Callable = lambda name: None
) -> Optional[AddressMap]:
    """The design's address map, or None when it records no address window. `view_of` gives an instance's view."""
    tables: Dict[str, List[Rule]] = {}
    for connection in linked.interconnections:
        for endpoint in connection.endpoints:
            if address_window(endpoint.source) is None:
                continue
            rule = _rule(linked, module, top_component, view_of, connection, endpoint)
            table = tables.setdefault(rule.decoder, [])
            if table and table[0].from_outside != rule.from_outside:
                raise ValueError(f"instance {module!r} has the name of its module, and the address map cannot tell their windows apart")
            table.append(rule)
    if not tables:
        return None

    rules_of_all = [rule for rules in tables.values() for rule in rules]
    widths = {rule.width for rule in rules_of_all if rule.width is not None}
    unsaid = [rule for rule in rules_of_all if rule.width is None]  # a window whose bus doesn't say how wide it is
    if unsaid:
        widths.add(64 if max(rule.end for rule in unsaid) > 1 << _DEFAULT_WIDTH else _DEFAULT_WIDTH)
    width = max(widths)

    for decoder, rules in tables.items():
        rules.sort(key=lambda rule: (rule.index, rule.base))
        by_address = sorted(rules, key=lambda rule: rule.base)
        for rule in by_address:
            limit = rule.width if rule.width is not None else width
            if rule.end > 1 << limit:
                raise ValueError(
                    f"the address window of {rule.target} ends at {rule.end:#x}, past the {limit}-bit address space"
                )
        for lower, upper in zip(by_address, by_address[1:]):
            if lower.end > upper.base:
                raise ValueError(
                    f"the address windows of {lower.target} and {upper.target} overlap: "
                    f"{lower.base:#x}-{lower.end:#x} and {upper.base:#x}-{upper.end:#x}, both decoded by {decoder}"
                )
    _name_constants(tables)
    return AddressMap(module, f"{module}_addr_map_pkg", width, tables)


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
        if decoder == address_map.module:
            lines.append(f"    // Reached from outside {decoder}, through its own interfaces")
        else:
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
