"""Generate the files of a linked IP-XACT design.

Three steps, each in a module of its own. The design is elaborated (elaboration.py): its views chosen, its
values worked out, its hierarchy followed. Each elaborated design is wired into the netlist of a module
(wiring.py). Each netlist is written as SystemVerilog (systemverilog.py), next to the address map package of
its design (address_map.py).
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Sequence, Tuple, Union

import ipxact
from xactflow import LinkedDesign

from . import systemverilog
from .address_map import AddressMap, render_address_map, render_socgen_package, socgen_names as parse_socgen_names
from .elaboration import elaborate, refuse_link_errors
from .netlist import Module
from .wiring import ModuleBuilder


def _generate(
    linked: LinkedDesign,
    common: Sequence[str],
    top: Optional[str],
    files: Dict[str, str],
    parents: Tuple[str, ...],
    written_elsewhere: Dict[str, str],
    top_view: Optional[ipxact.View] = None,
    top_overrides: Optional[Dict[str, str]] = None,
    module_name: Optional[str] = None,
    variants: Optional[Dict[Tuple[object, ...], Tuple[str, Dict[str, str]]]] = None,
    leave_open: bool = False,
    address_maps: Optional[Dict[str, AddressMap]] = None,
    modules: Optional[Dict[str, Module]] = None,
) -> None:
    refuse_link_errors(linked)
    if linked.design.monitor_interconnections:
        raise ValueError("monitor interconnections are not supported yet")
    # Its views chosen and its values worked out, for the instance it is the design of when it has parents.
    elaborated = elaborate(linked, top, view=top_view, overrides=top_overrides, path=parents)
    builder = ModuleBuilder(
        elaborated, common, must_use_common=not parents, module_name=module_name, variants=variants, leave_open=leave_open
    )
    module = builder.build()
    text = systemverilog.render(module)

    file_name = f"{module.name}.sv"
    if file_name in files and files[file_name] != text:
        raise ValueError(f"two different files would both be called {file_name}")
    files[file_name] = text
    if modules is not None:
        modules.setdefault(module.name, module)
    if builder.address_map is not None:
        package_file = f"{builder.address_map.package}.sv"
        package_text = render_address_map(builder.address_map, systemverilog.header(module.source))
        if package_file in files and files[package_file] != package_text:
            raise ValueError(f"two different files would both be called {package_file}")
        files[package_file] = package_text
        if address_maps is not None:  # a module made again, from the same values, has the same windows
            address_maps.setdefault(builder.address_map.module, builder.address_map)
    for instance in builder.instances.values():
        if not instance.hierarchical:
            written_elsewhere[instance.module] = str(instance.vlnv)

    for child in builder.children:
        _generate(
            elaborated.link_held(child.held, child.component),
            common,
            str(child.component.vlnv),
            files,
            (*parents, str(elaborated.linked.vlnv)),
            written_elsewhere,
            child.view,
            child.overrides,
            child.module or None,
            builder.variants,
            leave_open,
            address_maps,
            modules,
        )


_UNCONNECTED = ("refuse", "open")


def _leaves_open(unconnected: str) -> bool:
    if unconnected not in _UNCONNECTED:
        raise TypeError(f"xactflow-rtl-gen: the unconnected option is one of {', '.join(_UNCONNECTED)}, got {unconnected!r}")
    return unconnected == "open"


def _generate_all(
    linked: LinkedDesign, common: Sequence[str], top: Optional[str], leave_open: bool
) -> Tuple[Dict[str, str], Dict[str, Module], Dict[str, AddressMap]]:
    """The modules of a design and of its hierarchical instances, as files and as netlists, and their address maps."""
    files: Dict[str, str] = {}
    modules: Dict[str, Module] = {}
    written_elsewhere: Dict[str, str] = {}  # the modules of IPs, by name: the VLNV of one using each
    address_maps: Dict[str, AddressMap] = {}
    _generate(linked, common, top, files, (), written_elsewhere, leave_open=leave_open, address_maps=address_maps, modules=modules)
    for file_name in files:
        module = file_name[: -len(".sv")]
        if module in written_elsewhere:
            raise ValueError(f"module {module!r} would be generated here, and is already the module of IP {written_elsewhere[module]}")
    return files, modules, address_maps


def generate_files(
    linked: LinkedDesign,
    common: Sequence[str] = (),
    top: Optional[str] = None,
    unconnected: str = "refuse",
    socgen_package: Optional[str] = None,
    socgen_names: Union[str, Dict[str, str], Sequence[str], None] = None,
) -> Dict[str, str]:
    """Return every file a design needs, by name: its module first, then any address map package
    and the modules of hierarchical instances.

    `common` lists input port names shared across instances: each one that no connection drives
    connects to one port of the module with that name, like a clock or a reset, instead of one
    <instance>_<port> per instance.

    `top` is the VLNV of the hierarchical component to take the module's name and ports from, when
    more than one component holds the design. By default the library is searched for it.

    `unconnected` says what happens to a port the design connects to nothing, in a module whose ports
    a component defines: an instance's input, which cannot become a port of the module there, and an
    output of the module that nothing drives. "refuse", the default, or "open", which leaves each as
    the design has it and says so in the module.

    `socgen_package` names one more package to write, holding the address windows of every module
    generated here in PeakRDL-socgen's layout, for files written against that tool's package (D43).
    `socgen_names` gives what socgen calls an instance there, as `instance:name` pairs or a dict; one
    not given goes by its own name.
    """
    leave_open = _leaves_open(unconnected)
    names = parse_socgen_names(socgen_names)
    if socgen_package is not None and not isinstance(socgen_package, str):
        raise TypeError(f"xactflow-rtl-gen: socgen_package is a package name, got {socgen_package!r}")
    if names and socgen_package is None:
        raise TypeError("xactflow-rtl-gen: socgen_names only applies with socgen_package")
    files, _, address_maps = _generate_all(linked, common, top, leave_open)
    if socgen_package is not None:
        text = render_socgen_package(list(address_maps.values()), socgen_package, names, systemverilog.header(linked.vlnv))
        if f"{socgen_package}.sv" in files:
            raise ValueError(f"two different files would both be called {socgen_package}.sv")
        files[f"{socgen_package}.sv"] = text
    return files


def generate_top(linked: LinkedDesign, common: Sequence[str] = (), top: Optional[str] = None, unconnected: str = "refuse") -> str:
    """Return the module of a design, without the files it may depend on. See generate_files()."""
    return next(iter(generate_files(linked, common, top, unconnected).values()))


def generate_netlists(
    linked: LinkedDesign, common: Sequence[str] = (), top: Optional[str] = None, unconnected: str = "refuse"
) -> Dict[str, Module]:
    """Return the netlist of every module a design needs, by the module's name: its own first, then those of
    its hierarchical instances. They are what generate_files() writes as SystemVerilog, before it is text:
    for a printer of another language, or for reading what drives what. The options are generate_files()'s.
    Address maps are not part of a netlist."""
    return _generate_all(linked, common, top, _leaves_open(unconnected))[1]


def file_list(files: Dict[str, str]) -> str:
    """The files of generate_files() as a list a compiler reads with -F, one name on each line, relative to
    where the list is: packages first, as a module that uses one has to come after it, then the modules,
    the one the others are instances in last."""
    names = list(files)
    packages = [name for name in names if re.search(r"^package\s", files[name], re.MULTILINE)]
    modules = [name for name in reversed(names) if name not in packages]
    header = files[names[0]].splitlines()[0]
    lines = [header, "// What this run generated, in an order that compiles. Read it with -F: the names are relative to this file."]
    return "\n".join([*lines, *packages, *modules]) + "\n"
