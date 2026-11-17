#!/usr/bin/env python
"""Copy Didactic SoC's IP-XACT library, with the changes XactFlow and xactflow-rtl-gen need to read it.

Kactus2 writes a bus interface connected to several others (a clock going to every subsystem) as one
interconnection per pair. SCR 2.3 says a component/bus interface combination appears in only one
interconnection, so XactFlow refuses those designs. This merges each such group into a single
interconnection with all the endpoints, the standard's way of writing a fan-out. Since XactFlow 0.4.0 the
rule also counts a hierInterface, an interface of the component that holds the design (its clock going to
every instance), so interconnections that share one are merged as well.

An interconnection with more than two interfaces needs a bus definition that sets broadcast to true
(SCR 2.17, checked since XactFlow 0.4.0). Kactus2's pairs never needed it, and Didactic's clock, reset and
other fanned-out bus definitions do not set it. Each bus definition such an interconnection uses gets
broadcast set to true, which is what the designs already do with it.

Kactus2 also leaves expressions empty where nothing was entered: a scalar port gets a vector with an empty
left and right, and a port with no default gets a driver with an empty defaultValue. An empty expression has
no value, so xactflow-rtl-gen refuses it rather than guess a width. This drops a vector whose two bounds are both
empty, and a driver whose only content is an empty defaultValue. A vector with only one bound empty is kept,
for the exporter to refuse.

Kactus2's generator also drives an input that nothing connects with the port's default value. The standard
gives a port its default only through an ad hoc connection with tiedValue "default" (7.5.5), so xactflow-rtl-gen
refuses such an input. To keep what the designs mean when Kactus2 generates them, each such input that has a
default gets that ad hoc connection, named `<instance>_<port>_default`. An input with no default is left alone.

A module parameter whose value is its own parameterId refers to itself, which has no value; Kactus2 writes
the component parameter of the same name for it. Each such one is pointed at that component parameter.

Last, a few ports whose width in the IP-XACT disagrees with Didactic's own RTL get the RTL's width, listed in
WIDTHS_FROM_THE_RTL with where the RTL says so. Kactus2 does not check widths, so its Verilog connects them anyway
and loses the extra bits; xactflow-rtl-gen refuses them.

The same goes for module parameters a component lists that its RTL module does not have, in
NOT_IN_THE_RTL: xactflow-rtl-gen writes every module parameter on the instance (D19), so the generated module
would not compile against Didactic's RTL. Kactus2 only writes the ones a design changes, which hides them.

Every other byte of every file stays as it was, and every change is printed or counted in the output.

    python prepare_library.py DIDACTIC_DIR OUT_DIR

DIDACTIC_DIR is a clone of https://github.com/Edu4Chip/Didactic-SoC (no submodules); its ipxact/ folder is read.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from lxml import etree

NS = "http://www.accellera.org/XMLSchema/IPXACT/1685-2022"
Q = "{%s}" % NS


# (component file, port, left, right, where the RTL says so)
WIDTHS_FROM_THE_RTL = [
    ("tuni.fi/subsystem/SysCtrl_SS/1.1/SysCtrl_SS.1.1.xml", "pmod_sel", "7", "0",
     "src/rtl/SS_Ctrl_reg_array.sv and src/rtl/pmod_mux.sv: logic [7:0] pmod_sel"),
]


# (component file, module parameters its RTL does not have, where the RTL says so)
NOT_IN_THE_RTL = [
    ("tuni.fi/lowRISC/ibex/1.1/ibex.1.1.xml", ["BranchPredictor", "BranchTargetALU", "DbgHwBreakNum", "DbgTriggerEn"],
     "src/rtl/ibex_wrapper.sv: the parameter list of module ibex_wrapper"),
]


def endpoints(interconnection: etree._Element) -> List[etree._Element]:
    return [e for e in interconnection if e.tag in (Q + "activeInterface", Q + "hierInterface")]


def key(endpoint: etree._Element) -> Tuple[str, str, str]:
    kind = "active" if endpoint.tag == Q + "activeInterface" else "hier"
    return kind, endpoint.get("componentInstanceRef", ""), endpoint.get("busRef", "")


def merge_split_fanouts(root: etree._Element) -> List[str]:
    """Merge the interconnections that share an active interface or a hierInterface. Returns one line per merge."""
    connections = root.find(Q + "interconnections")
    if connections is None:
        return []
    items = [c for c in connections if c.tag == Q + "interconnection"]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: Dict[Tuple[str, str, str], int] = {}
    for index, item in enumerate(items):
        for endpoint in endpoints(item):
            k = key(endpoint)
            if k in owner:
                parent[find(index)] = find(owner[k])
            else:
                owner[k] = index

    groups: Dict[int, List[int]] = {}
    for index in range(len(items)):
        groups.setdefault(find(index), []).append(index)

    report = []
    for members in groups.values():
        if len(members) < 2:
            continue
        kept = items[members[0]]
        present = {key(e) for e in endpoints(kept)}
        anchor = endpoints(kept)[-1]
        for index in members[1:]:
            for endpoint in endpoints(items[index]):
                if key(endpoint) not in present:
                    present.add(key(endpoint))
                    copy = etree.fromstring(etree.tostring(endpoint))
                    previous = anchor.getprevious()
                    copy.tail = anchor.tail
                    anchor.tail = previous.tail if previous is not None else anchor.tail
                    anchor.addnext(copy)
                    anchor = copy
            connections.remove(items[index])
        names = [items[i].findtext(Q + "name") for i in members]
        report.append(f"merged {len(members)} interconnections into {names[0]!r}: {', '.join(names)}")
    return report


def _vlnv(element: etree._Element) -> Tuple[str, str, str, str]:
    return tuple(element.get(k) or element.findtext(Q + k) for k in ("vendor", "library", "name", "version"))


def fanned_out_bus_definitions(roots: List[etree._Element]) -> Dict[Tuple[str, ...], List[str]]:
    """The bus definitions used by interconnections with more than two interfaces, each with those
    interconnections as `design/interconnection`."""
    components = {_vlnv(root): root for root in roots if root.tag == Q + "component"}
    configurations = {_vlnv(root): root for root in roots if root.tag == Q + "designConfiguration"}
    holders: Dict[Tuple[str, ...], List[etree._Element]] = {}  # the components whose view holds each design
    for component in components.values():
        for reference in component.iter(Q + "designRef"):
            holders.setdefault(_vlnv(reference), []).append(component)
        for reference in component.iter(Q + "designConfigurationRef"):
            configuration = configurations.get(_vlnv(reference))
            held = configuration.find(Q + "designRef") if configuration is not None else None
            if held is not None:
                holders.setdefault(_vlnv(held), []).append(component)

    def bus_types(component: etree._Element, bus_ref: str) -> List[Tuple[str, ...]]:
        return [
            _vlnv(interface.find(Q + "busType"))
            for interface in component.iter(Q + "busInterface")
            if interface.findtext(Q + "name") == bus_ref and interface.find(Q + "busType") is not None
        ]

    used: Dict[Tuple[str, ...], List[str]] = {}
    for design in (root for root in roots if root.tag == Q + "design"):
        instances = {}
        for instance in design.iter(Q + "componentInstance"):
            component = components.get(_vlnv(instance.find(Q + "componentRef")))
            if component is not None:
                instances[instance.findtext(Q + "instanceName")] = component
        for interconnection in design.iter(Q + "interconnection"):
            ends = endpoints(interconnection)
            if len(ends) <= 2:
                continue
            where = f"{design.findtext(Q + 'name')}/{interconnection.findtext(Q + 'name')}"
            for end in ends:
                if end.tag == Q + "activeInterface":
                    owners = [instances[end.get("componentInstanceRef")]] if end.get("componentInstanceRef") in instances else []
                else:
                    owners = holders.get(_vlnv(design), [])
                for owner in owners:
                    for bus_type in bus_types(owner, end.get("busRef")):
                        if where not in used.setdefault(bus_type, []):
                            used[bus_type].append(where)
    return used


def set_broadcast(definition: etree._Element) -> bool:
    """Set broadcast to true on a bus definition. Returns whether that changed it."""
    broadcast = definition.find(Q + "broadcast")
    if broadcast is not None:
        if (broadcast.text or "").strip() == "true":
            return False
        broadcast.text = "true"
        return True
    direct = definition.find(Q + "directConnection")  # broadcast comes right after it
    broadcast = etree.Element(Q + "broadcast")
    broadcast.text = "true"
    broadcast.tail = direct.tail
    direct.addnext(broadcast)
    return True


def _empty(element) -> bool:
    return element is not None and not (element.text or "").strip() and len(element) == 0


def drop_empty_expressions(root: etree._Element) -> Dict[str, int]:
    """Drop Kactus2's placeholders for 'nothing entered'. Returns how many of each were dropped."""
    dropped = {"scalar vector": 0, "empty default driver": 0}
    for vector in list(root.iter(Q + "vector")):
        left, right = vector.find(Q + "left"), vector.find(Q + "right")
        if _empty(left) and _empty(right) and len(vector) == 2:
            _remove(vector)
            dropped["scalar vector"] += 1
    for driver in list(root.iter(Q + "driver")):
        if len(driver) == 1 and _empty(driver.find(Q + "defaultValue")):
            _remove(driver)
            dropped["empty default driver"] += 1
    for container in list(root.iter(Q + "vectors", Q + "drivers")):
        if len(container) == 0:
            _remove(container)
    return dropped


def _remove(element: etree._Element) -> None:
    """Remove an element and keep the indentation of what follows it."""
    parent, previous = element.getparent(), element.getprevious()
    if previous is not None:
        previous.tail = element.tail
    else:
        parent.text = element.tail
    parent.remove(element)


def fix_self_references(component: etree._Element) -> List[str]:
    """Point a module parameter whose value is its own parameterId at the component parameter of that name."""
    box = component.find(Q + "parameters")
    by_name = {p.findtext(Q + "name"): p.get("parameterId") for p in (box if box is not None else [])}
    fixed = []
    for parameter in component.iter(Q + "moduleParameter"):
        value = parameter.find(Q + "value")
        if value is not None and (value.text or "").strip() == parameter.get("parameterId"):
            name = parameter.findtext(Q + "name")
            if by_name.get(name):
                value.text = by_name[name]
                fixed.append(name)
    return fixed


def tie_unconnected_inputs(design: etree._Element, components: Dict[Tuple[str, ...], etree._Element]) -> Tuple[List[str], List[str]]:
    """Add a tie to its default for each instance input nothing connects. Returns the ports tied, and the
    unconnected inputs left alone because they have no default."""
    instances = {}
    for instance in design.iter(Q + "componentInstance"):
        component = components.get(_vlnv(instance.find(Q + "componentRef")))
        if component is not None:
            instances[instance.findtext(Q + "instanceName")] = component
    connected = set()
    for reference in design.iter(Q + "internalPortReference"):
        connected.add((reference.get("componentInstanceRef"), reference.get("portRef")))
    for active in design.iter(Q + "activeInterface"):
        name, component = active.get("componentInstanceRef"), instances.get(active.get("componentInstanceRef"))
        for interface in component.iter(Q + "busInterface") if component is not None else []:
            if interface.findtext(Q + "name") == active.get("busRef"):
                for physical in interface.iter(Q + "physicalPort"):
                    connected.add((name, physical.findtext(Q + "name")))
    tied, left = [], []
    adhocs = design.find(Q + "adHocConnections")
    for name, component in instances.items():
        for port in component.iter(Q + "port"):
            port_name, wire = port.findtext(Q + "name"), port.find(Q + "wire")
            if wire is None or wire.findtext(Q + "direction") != "in" or (name, port_name) in connected:
                continue
            if wire.find(f"{Q}drivers/{Q}driver/{Q}defaultValue") is None:
                left.append(f"{name}.{port_name}")
                continue
            if adhocs is None:
                adhocs = etree.SubElement(design, Q + "adHocConnections")
                # adHocConnections comes right after interconnections (or componentInstances)
                anchor = design.find(Q + "interconnections")
                anchor = anchor if anchor is not None else design.find(Q + "componentInstances")
                anchor.addnext(adhocs)
            connection = etree.SubElement(adhocs, Q + "adHocConnection")
            etree.SubElement(connection, Q + "name").text = f"{name}_{port_name}_default"
            etree.SubElement(connection, Q + "tiedValue").text = "default"
            references = etree.SubElement(connection, Q + "portReferences")
            etree.SubElement(references, Q + "internalPortReference", componentInstanceRef=name, portRef=port_name)
            tied.append(f"{name}.{port_name}")
    return tied, left


def prepare(didactic: Path, out: Path) -> None:
    """Write the changed library into `out`, which is emptied first, printing each change."""
    source = Path(didactic) / "ipxact"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out)
    changed = 0
    totals: Dict[str, int] = {}
    for path in sorted(out.rglob("*.xml")):
        tree = etree.parse(str(path))
        report = merge_split_fanouts(tree.getroot()) if tree.getroot().tag == Q + "design" else []
        dropped = drop_empty_expressions(tree.getroot())
        if tree.getroot().tag == Q + "component":
            for name in fix_self_references(tree.getroot()):
                report.append(f"module parameter {name} referred to itself: now the component parameter {name}")
        for what, count in dropped.items():
            totals[what] = totals.get(what, 0) + count
        if report or any(dropped.values()):
            changed += 1
            text = etree.tostring(tree.getroot(), encoding="unicode")
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", encoding="utf-8")
        if report:
            print(f"{path.relative_to(out)}:")
            for line in report:
                print(f"  {line}")
    documents = {path: etree.parse(str(path)) for path in sorted(out.rglob("*.xml"))}
    fanned_out = fanned_out_bus_definitions([tree.getroot() for tree in documents.values()])
    for path, tree in documents.items():
        root = tree.getroot()
        if root.tag == Q + "busDefinition" and _vlnv(root) in fanned_out and set_broadcast(root):
            changed += 1
            text = etree.tostring(root, encoding="unicode")
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", encoding="utf-8")
            users = fanned_out[_vlnv(root)]
            print(f"{path.relative_to(out)}: broadcast set to true, for {len(users)} interconnections with more than two interfaces, such as {users[0]}")
    for file, port, left, right, why in WIDTHS_FROM_THE_RTL:
        path = out / file
        tree = etree.parse(str(path))
        found = [p for p in tree.getroot().iter(Q + "port") if p.findtext(Q + "name") == port]
        vectors = found[0].findall(f"{Q}wire/{Q}vectors/{Q}vector") if len(found) == 1 else []
        if len(vectors) != 1:
            raise SystemExit(f"{file}: expected one port {port} with one vector")
        was = f"[{vectors[0].findtext(Q + 'left')}:{vectors[0].findtext(Q + 'right')}]"
        vectors[0].find(Q + "left").text, vectors[0].find(Q + "right").text = left, right
        text = etree.tostring(tree.getroot(), encoding="unicode")
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", encoding="utf-8")
        print(f"{file}: port {port} {was} -> [{left}:{right}], as in {why}")
    for file, names, why in NOT_IN_THE_RTL:
        path = out / file
        tree = etree.parse(str(path))
        found = [p for p in tree.getroot().iter(Q + "moduleParameter") if p.findtext(Q + "name") in names]
        if sorted(p.findtext(Q + "name") for p in found) != sorted(names):
            raise SystemExit(f"{file}: expected the module parameters {', '.join(names)}")
        for parameter in found:
            _remove(parameter)
        text = etree.tostring(tree.getroot(), encoding="unicode")
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", encoding="utf-8")
        print(f"{file}: module parameters {', '.join(names)} dropped, not in {why}")
    components = {}
    for path in sorted(out.rglob("*.xml")):
        root = etree.parse(str(path)).getroot()
        if root.tag == Q + "component":
            components[_vlnv(root)] = root
    for path in sorted(out.rglob("*.xml")):
        tree = etree.parse(str(path))
        if tree.getroot().tag != Q + "design":
            continue
        tied, left = tie_unconnected_inputs(tree.getroot(), components)
        if tied:
            etree.indent(tree.getroot(), space="\t")
            text = etree.tostring(tree.getroot(), encoding="unicode")
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", encoding="utf-8")
        if tied or left:
            print(f"{path.relative_to(out)}: tied to their default: {', '.join(tied) or '-'}; no default, left alone: {', '.join(left) or '-'}")
    print("dropped: " + ", ".join(f"{count} {what}s" for what, count in totals.items()))
    print(f"{changed} files changed, library in {out}")


def main() -> int:
    if len(sys.argv) != 3:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
