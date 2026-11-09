"""Short ways to write the IP-XACT objects of a test, and to run generated RTL in real tools."""

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import ipxact
from pyslang import ast, syntax
from xactflow import Library, link

from xactflow_rtl import generate_files

VENDOR = "test.org"
_E = ipxact.Expression


def vlnv_ref(library, name, **values):
    return ipxact.VLNVRef(VENDOR, library, name, "1.0", config_element_values={k: _E(str(v)) for k, v in values.items()})


def bounds(left, right):
    return ipxact.Range(_E(str(left)), _E(str(right)))


def wire(name, direction, width=1, default=None, vectors=None, drivers=None, type_name=None):
    """`width` is a number of bits, or the left bound as text. `vectors` gives (left, right) pairs instead."""
    if vectors is None:
        vectors = [(width if isinstance(width, str) else width - 1, 0)] if width != 1 else []
    if drivers is None:
        drivers = [ipxact.Driver(default_value=_E(str(default)))] if default is not None else []
    return ipxact.Port(
        name=name,
        wire=ipxact.WirePort(
            direction=ipxact.Direction(direction),
            vectors=[ipxact.Vector(_E(str(left)), _E(str(right))) for left, right in vectors],
            drivers=drivers,
            wire_type_defs=[ipxact.WireTypeDef(type_name=type_name)] if type_name else [],
        ),
    )


def struct(name, direction, type_name="req_pkg::req_t", **sub_ports):
    """A struct port. `sub_ports` gives the width of each; with none, its shape is left unsaid, as
    xactflow-sv leaves it."""
    return ipxact.Port(
        name=name,
        structured=ipxact.StructuredPort(
            struct_type="struct",
            direction=ipxact.Direction(direction),
            struct_port_type_defs=[ipxact.StructPortTypeDef(type_name=type_name)],
            sub_ports=[
                ipxact.SubPort(
                    name=sub_port,
                    wire=ipxact.WirePort(
                        direction=ipxact.Direction(direction),
                        vectors=[ipxact.Vector(_E(str(width - 1)), _E("0"))] if width != 1 else [],
                    ),
                )
                for sub_port, width in sub_ports.items()
            ],
        ),
    )


def tlm(name):
    return ipxact.Port(name=name, transactional=ipxact.TransactionalPort(initiative=ipxact.Initiative.REQUIRES, kind="tlm_port"))


def logical(name, on_initiator="out", on_target="in", width=None, default=None, requires_driver=False):
    def mode(direction):
        return ipxact.WireModeConstraints(direction=direction, width=_E(str(width)) if width else None)

    return ipxact.AbstractionPort(
        logical_name=name,
        wire=ipxact.WireAbstractionPort(
            on_initiator=mode(on_initiator),
            on_target=mode(on_target),
            default_value=_E(str(default)) if default is not None else None,
            requires_driver=ipxact.RequiresDriver(value=True) if requires_driver else None,
        ),
    )


def bus_documents():
    """A small bus with the logical ports the scenarios need."""
    definition = ipxact.BusDefinition(
        vlnv=ipxact.VLNV(VENDOR, "bus", "lbus", "1.0"), direct_connection=True, broadcast=True, is_addressable=True
    )
    address = logical("ADDR", width=32)
    address.wire.qualifier = ipxact.Qualifier(is_address=True)
    abstraction = ipxact.AbstractionDefinition(
        vlnv=ipxact.VLNV(VENDOR, "bus", "lbus_rtl", "1.0"),
        bus_type=vlnv_ref("bus", "lbus"),
        ports=[
            address,
            logical("WORD", width=8),
            logical("TAGGED", width=8, default="'h77"),
            logical("HIGH", width=8),
            logical("REPLY", "in", "out", width=8),
            logical("FLAG"),
            logical("PAD", "inout", "inout", width=8),
        ],
    )
    return [definition, abstraction]


def port_map(name, physical=None, select=None, logical_range=None, invert=False, tie=None):
    return ipxact.PortMap(
        logical_port=name,
        physical_port=physical,
        part_select=ipxact.PartSelect(range=bounds(*select)) if select else None,
        logical_range=bounds(*logical_range) if logical_range else None,
        invert=invert,
        logical_tie_off=_E(str(tie)) if tie is not None else None,
    )


def bus(name, mode, *maps):
    return ipxact.BusInterface(
        name=name,
        bus_type=vlnv_ref("bus", "lbus"),
        mode=ipxact.InterfaceMode(mode),
        abstraction_types=[ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "lbus_rtl"), port_maps=list(maps))],
    )


def component(name, ports, buses=(), parameters=None, module=None, module_parameters=None):
    """An IP. `parameters` gives overridable defaults; each is also a module parameter unless
    `module_parameters` names others."""
    parameters = parameters or {}
    return ipxact.Component(
        vlnv=ipxact.VLNV(VENDOR, "ip", name, "1.0"),
        bus_interfaces=list(buses),
        parameters=[
            ipxact.Parameter(name=key, value=_E(str(value)), parameter_id=key, resolve="user") for key, value in parameters.items()
        ],
        model=ipxact.Model(
            views=[ipxact.View(name="rtl", component_instantiation_ref="rtl_inst")],
            component_instantiations=[
                ipxact.ComponentInstantiation(
                    name="rtl_inst",
                    module_name=module or name,
                    module_parameters=[
                        ipxact.ModuleParameter(name=key, value=_E(key))
                        for key in (parameters if module_parameters is None else module_parameters)
                    ],
                )
            ],
            ports=list(ports),
        ),
    )


def hierarchical(name, design_name, ports, buses=(), library="ip"):
    """A component that holds a design: where the module's name and ports come from."""
    return ipxact.Component(
        vlnv=ipxact.VLNV(VENDOR, library, name, "1.0"),
        bus_interfaces=list(buses),
        model=ipxact.Model(
            views=[ipxact.View(name="structural", design_instantiation_ref="d")],
            design_instantiations=[ipxact.DesignInstantiation(name="d", design_ref=vlnv_ref("design", design_name))],
            ports=list(ports),
        ),
    )


def instance(name, component_name=None, **overrides):
    return ipxact.ComponentInstance(instance_name=name, component_ref=vlnv_ref("ip", component_name or name, **overrides))


def interconnection(name, first, *others, hier=()):
    def active(end):
        return ipxact.ActiveInterface(component_instance_ref=end[0], bus_ref=end[1])

    return ipxact.Interconnection(
        name=name,
        active_interface=active(first),
        other_active_interfaces=[active(end) for end in others],
        hier_interfaces=[ipxact.HierInterface(bus_ref=h) for h in hier],
    )


def ad_hoc(name, *internal, external=(), tied=None):
    """`internal` holds (instance, port) or (instance, port, (left, right)). `external` holds port
    names, or (port, (left, right))."""

    def select(pair):
        return ipxact.PartSelect(range=bounds(*pair))

    internals = [
        ipxact.InternalPortReference(component_instance_ref=r[0], port_ref=r[1], part_select=select(r[2]) if len(r) > 2 else None)
        for r in internal
    ]
    externals = [
        ipxact.ExternalPortReference(port_ref=r)
        if isinstance(r, str)
        else ipxact.ExternalPortReference(port_ref=r[0], part_select=select(r[1]))
        for r in external
    ]
    return ipxact.AdHocConnection(
        name=name,
        internal_port_references=internals,
        external_port_references=externals,
        tied_value=_E(str(tied)) if tied is not None else None,
    )


def design(name, instances, connections=(), ad_hocs=(), parameters=()):
    return ipxact.Design(
        vlnv=ipxact.VLNV(VENDOR, "design", name, "1.0"),
        component_instances=list(instances),
        interconnections=list(connections),
        ad_hoc_connections=list(ad_hocs),
        parameters=list(parameters),
    )


def library_of(documents) -> Library:
    library = Library()
    for document in documents:
        library.add(document)
    return library


def generate(the_design, documents, common=(), top=None) -> Dict[str, str]:
    return generate_files(link(the_design, library_of([*bus_documents(), *documents, the_design])), common, top)


# --- real tools


def slang_errors(*texts) -> List[str]:
    """What slang has to say, as anything but a clean result. Not only what it calls errors: pyslang's
    isError() is False even for a name declared twice, and it warns about truncated connections."""
    compilation = ast.Compilation()
    for text in texts:
        compilation.addSyntaxTree(syntax.SyntaxTree.fromText(text))
    # .port() left open on purpose, a rule table given to a decoder that takes it as a flat vector,
    # a port typed int read as bits, and the cast of an inverted enum, which slang alone thinks useless
    fine = ("EmptyOutputPortConn", "PackedArrayConv", "SignConversion", "UselessCast")
    return [str(d.code) for d in compilation.getAllDiagnostics() if not any(name in str(d.code) for name in fine)]


def in_compile_order(files: Dict[str, str]) -> List[Tuple[str, str]]:
    """Packages first: a module that uses one must come after it."""
    return sorted(files.items(), key=lambda item: not item[0].endswith("_pkg.sv"))


def testbench(module: str, drive: Dict[str, Tuple[int, int]], expect: Dict[str, Tuple[int, int]]) -> str:
    """Drives the module's inputs with fixed values and checks its outputs after they settle."""
    declarations, connections, checks = [], [], []
    for name, (width, value) in drive.items():
        declarations.append(f"    wire [{width - 1}:0] {name};\n    assign {name} = {width}'h{value:x};")
        connections.append(f".{name}({name})")
    for name, (width, value) in expect.items():
        declarations.append(f"    wire [{width - 1}:0] {name};")
        connections.append(f".{name}({name})")
        checks.append(
            f'        if ({name} !== {width}\'h{value:x}) begin $display("FAIL {name}=%h, expected {value:x}", {name}); ok = 0; end'
        )
    return "\n".join(
        [
            "module tb;",
            *declarations,
            f"    {module} dut ({', '.join(connections)});",
            "    integer ok = 1;",
            "    initial begin",
            "        #1;",
            *checks,
            '        if (ok) $display("PASS");',
            "        $finish;",
            "    end",
            "endmodule",
            "",
        ]
    )


def simulate(tmp_path: Path, files: Dict[str, str], sources: Sequence[str], tb: str, top: str = "tb") -> str:
    """Compile with Icarus and run. Returns what the simulation printed."""
    paths = []
    for name, text in [*in_compile_order(files), *[(f"ip{i}.sv", s) for i, s in enumerate(sources)], ("tb.sv", tb)]:
        path = tmp_path / name
        path.write_text(text)
        paths.append(str(path))
    binary = tmp_path / "sim.vvp"
    compiled = subprocess.run(["iverilog", "-g2012", "-s", top, "-o", str(binary), *paths], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    return subprocess.run(["vvp", str(binary)], check=True, capture_output=True, text=True).stdout


def verilator_lint(tmp_path: Path, files: Dict[str, str], sources: Sequence[str], top: str) -> subprocess.CompletedProcess:
    paths = []
    for name, text in [*in_compile_order(files), *[(f"lint_ip{i}.sv", s) for i, s in enumerate(sources)]]:
        path = tmp_path / name
        path.write_text(text)
        paths.append(str(path))
    return subprocess.run(["verilator", "--lint-only", "--top-module", top, *paths], capture_output=True, text=True)


has_icarus = shutil.which("iverilog") is not None
has_verilator = shutil.which("verilator") is not None


@dataclass
class Scenario:
    """A design, the RTL of its IPs, and what its module's ports must do in simulation."""

    name: str
    design: ipxact.Design
    documents: list
    source: str
    module: str
    drive: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    expect: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    common: Sequence[str] = ()

    def files(self) -> Dict[str, str]:
        return generate(self.design, self.documents, self.common)

    def tb(self) -> str:
        return testbench(self.module, self.drive, self.expect)
