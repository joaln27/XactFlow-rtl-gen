#!/usr/bin/env python
"""Compare xactflow-rtl-gen's Didactic SoC modules with Kactus2's, bit by bit.

The reference is what Kactus2's Verilog generator wrote: the tops Didactic commits in src/generated, made by
Kactus2 3.13.5 in October 2025. The IP-XACT of some subsystems changed after that, so a difference can come from
a stale file.

For each module, every instance input bit and module output bit is traced to what drives it, by reading the
two generators' text (verilog_netlist.py). A difference is a lead, not a verdict: decide from the standard and
the RTL which side is right.

    python compare_kactus2.py DIDACTIC_DIR LIBRARY_DIR RTL_DIR [REPORT.json]

LIBRARY_DIR is what prepare_library.py wrote, RTL_DIR what xactflow-rtl-gen generated from it.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

import ipxact
from xactflow import Library

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's folder off the path
import verilog_netlist  # noqa: E402

TOP = "tuni.fi:soc:Didactic:1.2"

# our module -> (its hierarchical component, Kactus2's file name)
MODULES: Dict[str, Tuple[str, str]] = {
    "Didactic": (TOP, "Didactic"),
    "SysCtrl_SS_wrapper_1": ("tuni.fi:subsystem.wrapper:SysCtrl_SS_wrapper:1.1", "SysCtrl_SS_wrapper_0"),
    "SysCtrl_SS_1": ("tuni.fi:subsystem:SysCtrl_SS:1.1", "SysCtrl_SS_0"),
    "analog_wrapper": ("tuni.fi:subsystem.wrapper:analog_wrapper:1.0", "analog_wrapper_0"),
    "dtu_wrapper": ("tuni.fi:subsystem.wrapper:dtu_wrapper:1.0", "dtu_wrapper_0"),
    "imt_wrapper": ("tuni.fi:subsystem.wrapper:imt_wrapper:1.0", "imt_wrapper_0"),
    "kth_wrapper": ("tuni.fi:subsystem.wrapper:kth_wrapper:1.0", "kth_wrapper_0"),
    "tum_wrapper": ("tuni.fi:subsystem.wrapper:tum_wrapper:1.0", "tum_wrapper_0"),
}


def kactus2_file(folder: Path, name: str) -> Optional[Path]:
    found = sorted(folder.rglob(f"{name}.v")) + sorted(folder.rglob(f"{name}.sv"))
    return found[0] if found else None


CAST = re.compile(r"\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*'\(([^()]*)\)")


def without_casts(path: Path, work: Path) -> Path:
    """The parser doesn't read type'(value), which xactflow-rtl-gen writes on typed ports (D20); a cast changes
    no bit, so it is taken out of a copy."""
    copy = work / path.name
    copy.write_text(CAST.sub(lambda m: m.group(1), path.read_text()))
    return copy


def holding_view(component: ipxact.Component) -> ipxact.View:
    for view in component.model.views:
        if view.design_instantiation_ref or view.design_configuration_instantiation_ref:
            return view
    raise ValueError(f"{component.vlnv} has no view that holds a design")


def design_of(library: Library, component: ipxact.Component, view: ipxact.View) -> ipxact.Design:
    model = component.model
    if view.design_instantiation_ref:
        reference = next(i for i in model.design_instantiations if i.name == view.design_instantiation_ref).design_ref
    else:
        instantiation = next(i for i in model.design_configuration_instantiations if i.name == view.design_configuration_instantiation_ref)
        reference = library.get_design_configuration(instantiation.design_configuration_ref.vlnv).design_ref
    design = library.get_design(reference.vlnv)
    if design is None:
        raise ValueError(f"design {reference} is not in the library")
    return design


def instance_ports(library: Library, design: ipxact.Design) -> Dict[str, Dict[str, str]]:
    """Each instance's ports and their directions, which the parser needs to tell a driver from a load."""
    ports: Dict[str, Dict[str, str]] = {}
    for instance in design.component_instances:
        component = library.get_component(instance.component_ref.vlnv)
        ports[instance.instance_name] = {
            port.name: port.wire.direction.value if port.wire is not None else "other" for port in component.model.ports
        }
    return ports


def reconcile(ours: Dict[str, str], theirs: Dict[str, str]) -> None:
    """Replace one side's port[all] = const:V by the bits the other side has for that port, when the other
    side gives every one of them a constant 0 or 1. Then a value written on the pin and one assigned bit by bit
    compare per bit."""
    for this, other in ((ours, theirs), (theirs, ours)):
        for key in [k for k, v in this.items() if k.endswith("[all]") and v.startswith("const:")]:
            port = key[: -len("[all]")]
            bits = {k: v for k, v in other.items() if k.startswith(port + "[") and not k.endswith("[all]")}
            if not bits or any(v not in ("0", "1") for v in bits.values()):
                continue
            try:
                value = int(this[key][len("const:"):])
            except ValueError:
                continue
            del this[key]
            for k in bits:
                index = int(k[len(port) + 1 : -1])
                this[k] = str((value >> index) & 1)
            if value >> len(bits):
                this[f"{port}[beyond]"] = f"const:{value >> len(bits)}"


def differences(ours: Path, theirs: Path, ports, work: Path) -> dict:
    ours_map, ours_problems = verilog_netlist.connectivity(without_casts(ours, work), ports)
    theirs_map, theirs_problems = verilog_netlist.connectivity(theirs, ports)
    reconcile(ours_map, theirs_map)
    keys = sorted(set(ours_map) | set(theirs_map))
    return {
        "endpoints": len(ours_map),
        "differences": {k: (ours_map.get(k, "(absent)"), theirs_map.get(k, "(absent)")) for k in keys if ours_map.get(k) != theirs_map.get(k)},
        "problems": {"ours": ours_problems, "kactus2": theirs_problems},
    }


def compare(didactic: Path, library_folder: Path, rtl: Path) -> dict:
    """For each module: how many endpoints were traced, which differ, and what the parser could not read."""
    library = Library.scan(library_folder)
    report = {}
    with tempfile.TemporaryDirectory() as work:
        for module, (vlnv, k2_name) in MODULES.items():
            component = library.get_component(ipxact.VLNV.parse(vlnv))
            ports = instance_ports(library, design_of(library, component, holding_view(component)))
            folder = Path(didactic) / "src" / "generated"
            theirs = kactus2_file(folder, k2_name) or kactus2_file(folder, module)
            report[module] = differences(Path(rtl) / f"{module}.sv", theirs, ports, Path(work)) if theirs else "no Kactus2 file"
    return report


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    report = compare(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    for module, result in report.items():
        if isinstance(result, str):
            print(f"{module:22} {result}")
            continue
        problems = sum(len(v) for v in result["problems"].values())
        print(f"{module:22} {result['endpoints']:5} endpoints, {len(result['differences']):4} differences, {problems} parse problems")
    if len(sys.argv) == 5:
        Path(sys.argv[4]).write_text(json.dumps(report, indent=1))
        print(f"details in {sys.argv[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
