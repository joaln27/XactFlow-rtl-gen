#!/usr/bin/env python
"""Describe the Croc SoC in IP-XACT, to test xactflow-rtl-gen on a hand-written PULP design (D24).

Croc's croc_soc.sv and croc_domain.sv are written by hand. This writes the same two as IP-XACT designs,
from what a person using XactFlow would start from:

- each IP's component, imported from its SystemVerilog with xactflow-sv;
- an OBI bus, written out here, for the IPs whose OBI signals are separate ports (the core and the debug
  module), mapped into the struct ports of the crossbar and the peripheral demultiplexer;
- the connections of croc_domain.sv and croc_soc.sv, as interconnections and ad hoc connections;
- one glue module, croc_domain_glue.sv next to this file, for the three struct constants croc_domain takes
  from packages.

What Croc brings that the earlier SoCs did not: type parameters (`parameter type obi_req_t`), ports that
are packed arrays of structs, members of those structs wired one by one, and a hand-written top to compare
with by running Croc's own firmware on both.

Changes made to what xactflow-sv imports, each one because the importer reads a module header without
elaborating it (printed when run):

- `cf_math_pkg::idx_width(N)`, a package function, is written out as its body, `(N > 1) ? $clog2(N) : 1`,
  wherever a width or a parameter uses it: an IP-XACT expression cannot call a SystemVerilog package.
- `ObiCfg.AddrWidth` and `ObiCfg.DataWidth`, members of a struct-valued parameter, are replaced by their
  value in croc_pkg, 32, where a port's width uses them.
- A port whose type the module gets through `import croc_pkg::*` has the package added to its name, as
  a generated module imports nothing.
- croc_soc's GpioCount is set to the testbench's, 32, in its component: a generated module declares its
  parameters, as xactflow-sv imported them, but is generated for one value of each (D36), and the testbench
  instantiates `croc_soc #(.GpioCount(32))`.

    python build_library.py CROC_DIR OUT_DIR [--list-sub-ports]

CROC_DIR is a clone of https://github.com/pulp-platform/croc (no submodules: its dependencies are vendored
under rtl/). Needs xactflow-sv 0.2.1 or later.

`--list-sub-ports` also lists the members of the OBI structs in each component, which xactflow-sv leaves out.
The exporter can then check every member's width, and that every member of a struct it drives is driven.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import ipxact
from xactflow import Library
from xactflow_component import ComponentExporter
from xactflow_design import DesignExporter
from xactflow_sv import SVImporter

HERE = Path(__file__).resolve().parent
GLUE = HERE / "croc_domain_glue.sv"

VENDOR = "pulp-platform.org"
_E = ipxact.Expression
TESTBENCH_GPIO_COUNT = 32  # rtl/test/tb_croc_soc.sv: croc_soc #(.GpioCount(32))
INCLUDE_DIRS = ["common_cells/include", "obi/include", "apb/include", "idma/include", "cve2/include"]  # as in Croc's file list

# module -> its file under rtl/
SOURCES = {
    "core_wrap": "core_wrap.sv",
    "obi_xbar": "obi/obi_xbar.sv",
    "obi_demux": "obi/obi_demux.sv",
    "obi_err_sbr": "obi/obi_err_sbr.sv",
    "obi_sram_shim": "obi/obi_sram_shim.sv",
    "tc_sram_impl": "tech_cells_generic/tc_sram_impl.sv",
    "addr_decode": "common_cells/addr_decode.sv",
    "dmi_jtag": "riscv-dbg/dmi_jtag.sv",
    "dm_obi_top": "riscv-dbg/dm_obi_top.sv",
    "soc_ctrl_regs": "soc_ctrl/soc_ctrl_regs.sv",
    "obi_uart": "obi_uart/obi_uart.sv",
    "gpio": "gpio/gpio.sv",
    "clint": "clint/clint.sv",
    "obi_timer": "obi_timer/obi_timer.sv",
    "bootrom": "bootrom/bootrom.sv",
    "rstgen": "common_cells/rstgen.sv",
    "user_domain": "user_domain.sv",
}

# croc_pkg.sv, as numbers: what the design needs to know to size ports.
NUM_XBAR_MANAGERS = 4  # NumXbarManagers, with iDMAEnable = 0
NUM_XBAR_SUBORDINATES = 5  # $size(CrocAddrMap) + 1
NUM_PERIPHS = 9  # $size(PeriphAddrMap) + 1
NUM_SRAM_BANKS = 2
SRAM_BANK_NUM_WORDS = 512
OBI_CFG = {"AddrWidth": 32, "DataWidth": 32}  # the same in MgrObiCfg and SbrObiCfg
SBR_ID_WIDTH = 3  # SbrObiCfg.IdWidth: 1 + idx_width(NumXbarManagers)
BOOTROM_ADDR = "32'h02000000"  # BootromAddr: get_periph_start_addr(PeriphBootrom)
XBAR = {"Error": 0, "Periph": 1, "User": 2, "Bank0": 3}  # croc_xbar_outputs_e
PERIPH = {"Error": 0, "Debug": 1, "Bootrom": 2, "Clint": 3, "SocCtrl": 4, "Uart": 5, "Gpio": 6, "Timer": 7, "iDMA": 8}  # periph_outputs_e
CROC_PKG_TYPES = ("sbr_obi_req_t", "sbr_obi_rsp_t", "mgr_obi_req_t", "mgr_obi_rsp_t")

SBR_TYPES = {"obi_req_t": "croc_pkg::sbr_obi_req_t", "obi_rsp_t": "croc_pkg::sbr_obi_rsp_t"}
SBR_CFG = {"ObiCfg": "croc_pkg::SbrObiCfg", **SBR_TYPES}

# The OBI signals of croc_pkg's request and response structs: (logical port, member, width, from the manager's side).
# An id is 1 bit before the crossbar and 3 after it, so the abstraction gives it no width.
OBI = [
    ("REQ", "req", 1, "out"),
    ("ADDR", "a.addr", 32, "out"),
    ("WE", "a.we", 1, "out"),
    ("BE", "a.be", 4, "out"),
    ("WDATA", "a.wdata", 32, "out"),
    ("AID", "a.aid", None, "out"),
    ("A_OPTIONAL", "a.a_optional", 1, "out"),
    ("GNT", "gnt", 1, "in"),
    ("RVALID", "rvalid", 1, "in"),
    ("RDATA", "r.rdata", 32, "in"),
    ("RID", "r.rid", None, "in"),
    ("ERR", "r.err", 1, "in"),
    ("R_OPTIONAL", "r.r_optional", 1, "in"),
]


def vlnv_ref(library: str, name: str, **values: object) -> ipxact.VLNVRef:
    return ipxact.VLNVRef(VENDOR, library, name, "1.0", config_element_values={k: _E(str(v)) for k, v in values.items()})


def part_select(left: int, right: int) -> ipxact.PartSelect:
    return ipxact.PartSelect(range=ipxact.Range(_E(str(left)), _E(str(right))))


def bus_definition_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ipxact:busDefinition xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">
  <ipxact:vendor>{VENDOR}</ipxact:vendor><ipxact:library>bus</ipxact:library><ipxact:name>obi</ipxact:name><ipxact:version>1.0</ipxact:version>
  <ipxact:directConnection>true</ipxact:directConnection>
  <ipxact:broadcast>false</ipxact:broadcast>
  <ipxact:isAddressable>true</ipxact:isAddressable>
</ipxact:busDefinition>
"""


def abstraction_xml() -> str:
    rows = []
    for logical, _, width, direction in OBI:
        opposite = "in" if direction == "out" else "out"
        qualifier = "<ipxact:qualifier><ipxact:isAddress>true</ipxact:isAddress></ipxact:qualifier>" if logical == "ADDR" else ""
        size = f"<ipxact:width>{width}</ipxact:width>" if width else ""
        mode = lambda tag, way: f"<ipxact:{tag}>{size}<ipxact:direction>{way}</ipxact:direction></ipxact:{tag}>"
        rows.append(
            f"    <ipxact:port><ipxact:logicalName>{logical}</ipxact:logicalName><ipxact:wire>{qualifier}"
            f"{mode('onInitiator', direction)}{mode('onTarget', opposite)}</ipxact:wire></ipxact:port>"
        )
    body = "\n".join(rows)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ipxact:abstractionDefinition xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">
  <ipxact:vendor>{VENDOR}</ipxact:vendor><ipxact:library>bus</ipxact:library><ipxact:name>obi_rtl</ipxact:name><ipxact:version>1.0</ipxact:version>
  <ipxact:busType vendor="{VENDOR}" library="bus" name="obi" version="1.0"/>
  <ipxact:ports>
{body}
  </ipxact:ports>
</ipxact:abstractionDefinition>
"""


def bus_interface(name: str, mode: str, maps: List[ipxact.PortMap]) -> ipxact.BusInterface:
    return ipxact.BusInterface(
        name=name,
        bus_type=vlnv_ref("bus", "obi"),
        mode=ipxact.InterfaceMode(mode),
        abstraction_types=[ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "obi_rtl"), port_maps=maps)],
    )


def port_maps(ports: Dict[str, str], ties: Optional[Dict[str, str]] = None) -> List[ipxact.PortMap]:
    """An OBI interface of separate ports: {logical: physical}, and the signals the IP ties off itself."""
    maps = [ipxact.PortMap(logical_port=logical, physical_port=physical) for logical, physical in ports.items()]
    maps.extend(ipxact.PortMap(logical_port=logical, logical_tie_off=_E(value)) for logical, value in (ties or {}).items())
    return maps


def element_maps(request: str, response: str, index: int) -> List[ipxact.PortMap]:
    """An OBI interface that is one element of two arrays of structs: `request[index].a.addr`, and so on."""
    maps = []
    for logical, member, _, direction in OBI:
        maps.append(
            ipxact.PortMap(
                logical_port=logical,
                physical_port=request if direction == "out" else response,
                part_select=part_select(index, index),
                sub_port_refs=[ipxact.SubPortReference(sub_port_ref=part) for part in member.split(".")],
            )
        )
    return maps


IDX_WIDTH = re.compile(r"cf_math_pkg::idx_width\(([^()]*)\)")
OBI_CFG_MEMBER = re.compile(r"\bObiCfg\.(\w+)")


def without_package_calls(text: str) -> str:
    return IDX_WIDTH.sub(lambda m: f"(({m.group(1)}) > 32'd1 ? $clog2({m.group(1)}) : 32'd1)", text)


def obi_struct_members(is_request: bool, direction: str, id_width: int) -> List[ipxact.SubPort]:
    """The members of croc_pkg's request or response struct, as sub-ports."""

    def leaf(name: str, width: int) -> ipxact.SubPort:
        vectors = [ipxact.Vector(_E(str(width - 1)), _E("0"))] if width != 1 else []
        return ipxact.SubPort(name=name, wire=ipxact.WirePort(direction=ipxact.Direction(direction), vectors=vectors))

    def inner(name: str, type_name: str, members: List[ipxact.SubPort]) -> ipxact.SubPort:
        return ipxact.SubPort(
            name=name,
            structured=ipxact.StructuredPort(
                struct_type="struct",
                direction=ipxact.Direction(direction),
                sub_ports=members,
                struct_port_type_defs=[ipxact.StructPortTypeDef(type_name=type_name)],
            ),
        )

    kind = "sbr" if id_width == SBR_ID_WIDTH else "mgr"
    if is_request:
        channel = [leaf("addr", 32), leaf("we", 1), leaf("be", 4), leaf("wdata", 32), leaf("aid", id_width), leaf("a_optional", 1)]
        return [inner("a", f"croc_pkg::{kind}_obi_a_chan_t", channel), leaf("req", 1)]
    channel = [leaf("rdata", 32), leaf("rid", id_width), leaf("err", 1), leaf("r_optional", 1)]
    return [inner("r", f"croc_pkg::{kind}_obi_r_chan_t", channel), leaf("gnt", 1), leaf("rvalid", 1)]


class Importer:
    def __init__(self, rtl: Path, metadata_folder: Path) -> None:
        self.rtl = rtl
        self.metadata = metadata_folder / "metadata.json"
        self.metadata.write_text(json.dumps({"vendor": VENDOR, "library": "ip", "version": "1.0"}))
        self.changes: Dict[str, List[str]] = {}

    def __call__(self, module: str, source: Optional[Path] = None) -> ipxact.Component:
        # xactflow-sv reads the whole file: it needs its `include`s, and the module's name
        options = {"include": [str(self.rtl / folder) for folder in INCLUDE_DIRS], "module": module, "define": ["VERILATOR", "COMMON_CELLS_ASSERTS_OFF"]}
        library = Library()
        SVImporter().import_(source or self.rtl / SOURCES[module], library, metadata=str(self.metadata), **options)
        (component,) = library.targets(ipxact.Component)
        changes = self.changes.setdefault(module, [])

        def expression(owner: str, value: ipxact.Expression) -> None:
            text = value.value
            fixed = OBI_CFG_MEMBER.sub(lambda m: str(OBI_CFG[m.group(1)]), without_package_calls(text))
            if fixed != text:
                changes.append(f"{owner}: {text} -> {fixed}")
                value.value = fixed

        for parameter in component.parameters:
            text = parameter.value.value
            fixed = without_package_calls(text)
            if fixed != text:
                changes.append(f"parameter {parameter.name}: {text} -> {fixed}")
                parameter.value.value = fixed
        for port in component.model.ports:
            kind = port.wire if port.wire is not None else port.structured
            for vector in kind.vectors:
                expression(f"port {port.name}", vector.left)
                expression(f"port {port.name}", vector.right)
            if port.structured is not None:
                definition = port.structured.struct_port_type_defs[0]
                if definition.type_name in CROC_PKG_TYPES:
                    changes.append(f"port {port.name}: type {definition.type_name} -> croc_pkg::{definition.type_name}")
                    definition.type_name = f"croc_pkg::{definition.type_name}"
        return component


def list_obi_sub_ports(component: ipxact.Component, types: Dict[str, str]) -> None:
    """Give each OBI struct port its members. `types` maps a port's type name to the croc_pkg type it stands for."""
    for port in component.model.ports:
        if port.structured is None:
            continue
        # xactflow-sv leaves a port's packed dimensions in its type's name: obi_req_t[NumMgrPorts-1:0]
        type_name = types.get(port.structured.struct_port_type_defs[0].type_name.split("[")[0].strip())
        found = re.fullmatch(r"croc_pkg::(sbr|mgr)_obi_(req|rsp)_t", type_name or "")
        if found is None:
            continue
        id_width = SBR_ID_WIDTH if found.group(1) == "sbr" else 1
        port.structured.sub_ports = obi_struct_members(found.group(2) == "req", port.structured.direction.value, id_width)


def components(croc: Path, list_sub_ports: bool, work: Path) -> Tuple[Dict[str, ipxact.Component], Importer]:
    imported = Importer(croc / "rtl", work)
    made = {name: imported(name) for name in SOURCES}

    core = made["core_wrap"]
    core.bus_interfaces = [
        bus_interface(
            "instr",
            "initiator",
            port_maps(
                {"REQ": "instr_req_o", "ADDR": "instr_addr_o", "GNT": "instr_gnt_i", "RVALID": "instr_rvalid_i", "RDATA": "instr_rdata_i", "ERR": "instr_err_i"},
                # croc_domain.sv: core_instr_obi_req.a.aid = '0, .we = '0, .be = '1, .wdata = '0, .a_optional = '0
                {"WE": "0", "BE": "'1", "WDATA": "0", "AID": "0", "A_OPTIONAL": "0"},
            ),
        ),
        bus_interface(
            "data",
            "initiator",
            port_maps(
                {
                    "REQ": "data_req_o", "ADDR": "data_addr_o", "WE": "data_we_o", "BE": "data_be_o", "WDATA": "data_wdata_o",
                    "GNT": "data_gnt_i", "RVALID": "data_rvalid_i", "RDATA": "data_rdata_i", "ERR": "data_err_i",
                },  # fmt: skip
                {"AID": "0", "A_OPTIONAL": "0"},  # core_data_obi_req.a.aid = '0, .a_optional = '0
            ),
        ),
    ]

    debug = made["dm_obi_top"]
    debug.bus_interfaces = [
        bus_interface(
            "master",
            "initiator",
            port_maps(
                {
                    "REQ": "master_req_o", "ADDR": "master_addr_o", "WE": "master_we_o", "WDATA": "master_wdata_o", "BE": "master_be_o",
                    "GNT": "master_gnt_i", "RVALID": "master_rvalid_i", "RDATA": "master_rdata_i", "ERR": "master_err_i",
                },  # fmt: skip
                {"AID": "0", "A_OPTIONAL": "0"},  # dbg_req_obi_req.a.aid = '0, .a_optional = '0
            ),
        ),
        bus_interface(
            "slave",
            "target",
            port_maps(
                {
                    "REQ": "slave_req_i", "WE": "slave_we_i", "ADDR": "slave_addr_i", "BE": "slave_be_i", "WDATA": "slave_wdata_i",
                    "AID": "slave_aid_i", "GNT": "slave_gnt_o", "RVALID": "slave_rvalid_o", "RDATA": "slave_rdata_o", "RID": "slave_rid_o",
                },  # fmt: skip
                {"ERR": "0", "R_OPTIONAL": "0"},  # dbg_mem_obi_rsp.r.r_optional = 1'b0, .r.err = 1'b0
            ),
        ),
    ]

    # The crossbar's manager side (its subordinate ports) and the demultiplexer's subordinate side, where an IP
    # with separate OBI ports connects: one bus interface for each such element of their arrays of structs.
    made["obi_xbar"].bus_interfaces = [
        bus_interface(f"sbr{index}", "target", element_maps("sbr_ports_req_i", "sbr_ports_rsp_o", index)) for index in (1, 2, 3)
    ]
    made["obi_demux"].bus_interfaces = [
        bus_interface(f"mgr{PERIPH['Debug']}", "initiator", element_maps("mgr_ports_req_o", "mgr_ports_rsp_i", PERIPH["Debug"]))
    ]

    made["croc_domain_glue"] = imported("croc_domain_glue", GLUE)

    if list_sub_ports:
        sbr = {**{v: v for v in SBR_TYPES.values()}, **SBR_TYPES}
        mgr_and_sbr = {f"croc_pkg::{name}": f"croc_pkg::{name}" for name in CROC_PKG_TYPES}
        xbar = {
            "sbr_port_obi_req_t": "croc_pkg::mgr_obi_req_t", "sbr_port_obi_rsp_t": "croc_pkg::mgr_obi_rsp_t",
            "mgr_port_obi_req_t": "croc_pkg::sbr_obi_req_t", "mgr_port_obi_rsp_t": "croc_pkg::sbr_obi_rsp_t",
        }  # fmt: skip
        for name, component in made.items():
            list_obi_sub_ports(component, xbar if name == "obi_xbar" else mgr_and_sbr if name == "user_domain" else sbr)

    # The two hierarchical components: their ports and parameters are those of the hand-written modules, as
    # imported. The view that describes the module is the one that holds the design.
    for name, design_name in (("croc_domain", "croc_domain_d"), ("croc_soc", "croc_soc_d")):
        component = imported(name, croc / "rtl" / f"{name}.sv")
        (described,) = component.model.component_instantiations
        component.model.views = [ipxact.View(name="structural", component_instantiation_ref=described.name, design_instantiation_ref="design")]
        component.model.design_instantiations = [ipxact.DesignInstantiation(name="design", design_ref=vlnv_ref("design", design_name))]
        made[name] = component
    for parameter in made["croc_soc"].parameters:
        if parameter.name == "GpioCount":
            imported.changes["croc_soc"].append(f"parameter GpioCount: {parameter.value.value} -> {TESTBENCH_GPIO_COUNT}, the testbench's")
            parameter.value.value = str(TESTBENCH_GPIO_COUNT)
    if list_sub_ports:
        list_obi_sub_ports(made["croc_domain"], {f"croc_pkg::{name}": f"croc_pkg::{name}" for name in CROC_PKG_TYPES})
    return made, imported


def instance(name: str, component: str, **parameters: object) -> ipxact.ComponentInstance:
    return ipxact.ComponentInstance(instance_name=name, component_ref=vlnv_ref("ip", component, **parameters))


def connect(name: str, first: Tuple[str, str], second: Tuple[str, str]) -> ipxact.Interconnection:
    def active(end: Tuple[str, str]) -> ipxact.ActiveInterface:
        return ipxact.ActiveInterface(component_instance_ref=end[0], bus_ref=end[1])

    return ipxact.Interconnection(name=name, active_interface=active(first), other_active_interfaces=[active(second)])


def reference(end: tuple) -> ipxact.InternalPortReference:
    """(instance, port), (instance, port, (left, right)) or (instance, port, (left, right), "a.addr")."""
    select = part_select(*end[2]) if len(end) > 2 and end[2] is not None else None
    members = [ipxact.SubPortReference(sub_port_ref=part) for part in end[3].split(".")] if len(end) > 3 else []
    return ipxact.InternalPortReference(component_instance_ref=end[0], port_ref=end[1], part_select=select, sub_port_refs=members)


def wire(name: str, *internal: tuple, external: Sequence = (), tied: Optional[str] = None) -> ipxact.AdHocConnection:
    outside = [
        ipxact.ExternalPortReference(port_ref=e) if isinstance(e, str) else ipxact.ExternalPortReference(port_ref=e[0], part_select=part_select(*e[1]))
        for e in external
    ]
    return ipxact.AdHocConnection(
        name=name,
        internal_port_references=[reference(end) for end in internal],
        external_port_references=outside,
        tied_value=_E(tied) if tied is not None else None,
    )


def element(instance_name: str, port: str, index: int) -> tuple:
    return instance_name, port, (index, index)


def design(name: str, instances, interconnections, ad_hocs) -> ipxact.Design:
    return ipxact.Design(
        vlnv=ipxact.VLNV(VENDOR, "design", name, "1.0"),
        component_instances=list(instances),
        interconnections=list(interconnections),
        ad_hoc_connections=list(ad_hocs),
    )


def croc_domain_design() -> ipxact.Design:
    error_sbr = dict(SBR_CFG, NumMaxTrans=1, RspData="32'hBADCAB1E")
    placed = [
        instance("i_core_wrap", "core_wrap"),
        instance("i_obi_err_sbr_idma_cfg", "obi_err_sbr", **error_sbr),  # gen_no_dma, as iDMAEnable is 0
        instance("i_dmi_jtag", "dmi_jtag", IdcodeValue="croc_pkg::PulpJtagIdCode"),
        instance("i_dm_top", "dm_obi_top", BusWidth=OBI_CFG["DataWidth"], IdWidth=SBR_ID_WIDTH),
        instance(
            "i_main_xbar",
            "obi_xbar",
            SbrPortObiCfg="croc_pkg::MgrObiCfg",
            MgrPortObiCfg="croc_pkg::SbrObiCfg",
            sbr_port_obi_req_t="croc_pkg::mgr_obi_req_t",
            sbr_port_a_chan_t="croc_pkg::mgr_obi_a_chan_t",
            sbr_port_obi_rsp_t="croc_pkg::mgr_obi_rsp_t",
            sbr_port_r_chan_t="croc_pkg::mgr_obi_r_chan_t",
            mgr_port_obi_req_t="croc_pkg::sbr_obi_req_t",
            mgr_port_obi_rsp_t="croc_pkg::sbr_obi_rsp_t",
            NumSbrPorts=NUM_XBAR_MANAGERS,
            NumMgrPorts=NUM_XBAR_SUBORDINATES,
            NumMaxTrans=2,
            NumAddrRules=4,
            addr_map_rule_t="croc_pkg::addr_map_rule_t",
            UseIdForRouting="1'b0",
            Connectivity="croc_pkg::XbarConnectivity",
        ),
        instance("i_xbar_err", "obi_err_sbr", **error_sbr),
        instance(
            "i_addr_decode_periphs",
            "addr_decode",
            NoIndices=NUM_PERIPHS,
            NoRules=NUM_PERIPHS - 1,
            addr_t=f"logic [{OBI_CFG['DataWidth'] - 1}:0]",
            rule_t="croc_pkg::addr_map_rule_t",
            Napot="1'b0",
        ),
        instance("i_obi_demux", "obi_demux", **SBR_CFG, NumMgrPorts=NUM_PERIPHS, NumMaxTrans=2),
        instance("i_soc_ctrl", "soc_ctrl_regs", **SBR_TYPES, BootAddrDefault="croc_pkg::BootAddr"),
        instance("i_uart", "obi_uart", **SBR_CFG),
        instance("i_gpio", "gpio", **SBR_CFG, GpioCount="GpioCount"),
        instance("i_clint", "clint", **SBR_TYPES),
        instance("i_obi_timer", "obi_timer", **SBR_TYPES),
        instance("i_bootrom", "bootrom", **SBR_CFG),
        instance("i_periph_err", "obi_err_sbr", **error_sbr),
        instance("i_glue", "croc_domain_glue"),
    ]
    xbar, demux = "i_main_xbar", "i_obi_demux"
    wires = [
        # interrupts: [0] timer, [1] uart, [2] gpio, [3] iDMA (not there), [4+:NumExternalIrqs] from outside, the rest 0
        wire("irq_timer", ("i_obi_timer", "expired_o"), ("i_core_wrap", "irqs_i", (0, 0))),
        wire("irq_uart", ("i_uart", "irq_o"), ("i_core_wrap", "irqs_i", (1, 1))),
        wire("irq_gpio", ("i_gpio", "interrupt_o"), ("i_core_wrap", "irqs_i", (2, 2))),
        wire("irq_idma", ("i_core_wrap", "irqs_i", (3, 3)), tied="0"),
        wire("irq_external", ("i_core_wrap", "irqs_i", ("NumExternalIrqs + 3", 4)), external=["interrupts_i"]),
        wire("irq_unused", ("i_core_wrap", "irqs_i", (15, "NumExternalIrqs + 4")), tied="0"),
        wire("timer_irq", ("i_clint", "timer_irq_o"), ("i_core_wrap", "timer_irq_i")),
        wire("software_irq", ("i_clint", "software_irq_o"), ("i_core_wrap", "software_irq_i")),
        # core
        wire("core_test_enable", ("i_core_wrap", "test_enable_i"), external=["testmode_i"]),
        wire("boot_addr", ("i_core_wrap", "boot_addr_i"), tied=BOOTROM_ADDR),
        wire("debug_req", ("i_dm_top", "debug_req_o"), ("i_core_wrap", "debug_req_i")),
        wire("fetch_enable", ("i_soc_ctrl", "fetch_en_o"), ("i_core_wrap", "fetch_enable_i")),
        wire("core_busy", ("i_core_wrap", "core_busy_o"), external=["core_busy_o"]),
        # the user domain as a manager of the crossbar, and as one of its subordinates
        wire("user_mgr_req", element(xbar, "sbr_ports_req_i", 0), external=["user_mgr_obi_req_i"]),
        wire("user_mgr_rsp", element(xbar, "sbr_ports_rsp_o", 0), external=["user_mgr_obi_rsp_o"]),
        wire("user_sbr_req", element(xbar, "mgr_ports_req_o", XBAR["User"]), external=["user_sbr_obi_req_o"]),
        wire("user_sbr_rsp", element(xbar, "mgr_ports_rsp_i", XBAR["User"]), external=["user_sbr_obi_rsp_i"]),
        # crossbar
        wire("xbar_addr_map", ("i_glue", "croc_addr_map_o"), (xbar, "addr_map_i")),
        wire("xbar_en_default_idx", (xbar, "en_default_idx_i"), tied="'1"),
        wire("xbar_default_idx", (xbar, "default_idx_i"), tied="0"),
        wire("xbar_err_req", element(xbar, "mgr_ports_req_o", XBAR["Error"]), ("i_xbar_err", "obi_req_i")),
        wire("xbar_err_rsp", ("i_xbar_err", "obi_rsp_o"), element(xbar, "mgr_ports_rsp_i", XBAR["Error"])),
        # peripherals behind the demultiplexer, picked by the address
        wire("periph_req", element(xbar, "mgr_ports_req_o", XBAR["Periph"]), (demux, "sbr_port_req_i")),
        wire("periph_rsp", (demux, "sbr_port_rsp_o"), element(xbar, "mgr_ports_rsp_i", XBAR["Periph"])),
        wire("periph_addr", (xbar, "mgr_ports_req_o", (XBAR["Periph"], XBAR["Periph"]), "a.addr"), ("i_addr_decode_periphs", "addr_i")),
        wire("periph_addr_map", ("i_glue", "periph_addr_map_o"), ("i_addr_decode_periphs", "addr_map_i")),
        wire("periph_idx", ("i_addr_decode_periphs", "idx_o"), (demux, "sbr_port_select_i")),
        wire("periph_en_default_idx", ("i_addr_decode_periphs", "en_default_idx_i"), tied="1"),
        wire("periph_default_idx", ("i_addr_decode_periphs", "default_idx_i"), tied=str(PERIPH["Error"])),
        # debug module
        wire("jtag_tck", ("i_dmi_jtag", "tck_i"), external=["jtag_tck_i"]),
        wire("jtag_tms", ("i_dmi_jtag", "tms_i"), external=["jtag_tms_i"]),
        wire("jtag_trst", ("i_dmi_jtag", "trst_ni"), external=["jtag_trst_ni"]),
        wire("jtag_tdi", ("i_dmi_jtag", "td_i"), external=["jtag_tdi_i"]),
        wire("jtag_tdo", ("i_dmi_jtag", "td_o"), external=["jtag_tdo_o"]),
        wire("dmi_rst", ("i_dmi_jtag", "dmi_rst_no"), ("i_dm_top", "dmi_rst_ni")),
        wire("dmi_req", ("i_dmi_jtag", "dmi_req_o"), ("i_dm_top", "dmi_req_i")),
        wire("dmi_req_valid", ("i_dmi_jtag", "dmi_req_valid_o"), ("i_dm_top", "dmi_req_valid_i")),
        wire("dmi_req_ready", ("i_dm_top", "dmi_req_ready_o"), ("i_dmi_jtag", "dmi_req_ready_i")),
        wire("dmi_resp", ("i_dm_top", "dmi_resp_o"), ("i_dmi_jtag", "dmi_resp_i")),
        wire("dmi_resp_ready", ("i_dmi_jtag", "dmi_resp_ready_o"), ("i_dm_top", "dmi_resp_ready_i")),
        wire("dmi_resp_valid", ("i_dm_top", "dmi_resp_valid_o"), ("i_dmi_jtag", "dmi_resp_valid_i")),
        wire("dm_unavailable", ("i_dm_top", "unavailable_i"), tied="0"),
        wire("dm_hartinfo", ("i_glue", "hartinfo_o"), ("i_dm_top", "hartinfo_i")),
        wire("dm_master_other_err", ("i_dm_top", "master_other_err_i"), tied="0"),
        # UART: the modem control pins are not used
        wire("uart_rx", ("i_uart", "rxd_i"), external=["uart_rx_i"]),
        wire("uart_tx", ("i_uart", "txd_o"), external=["uart_tx_o"]),
        *[wire(f"uart_{pin}", ("i_uart", pin), tied="1") for pin in ("cts_ni", "dsr_ni", "ri_ni", "cd_ni")],
        # GPIO
        wire("gpio_in", ("i_gpio", "gpio_i"), external=["gpio_i"]),
        wire("gpio_out", ("i_gpio", "gpio_o"), external=["gpio_o"]),
        wire("gpio_out_en", ("i_gpio", "gpio_out_en_o"), external=["gpio_out_en_o"]),
        wire("gpio_in_sync", ("i_gpio", "gpio_in_sync_o"), external=["gpio_in_sync_o"]),
        wire("clint_rtc", ("i_clint", "rtc_i"), external=["ref_clk_i"]),
    ]
    peripherals = {
        "Error": "i_periph_err", "Bootrom": "i_bootrom", "Clint": "i_clint", "SocCtrl": "i_soc_ctrl",
        "Uart": "i_uart", "Gpio": "i_gpio", "Timer": "i_obi_timer", "iDMA": "i_obi_err_sbr_idma_cfg",
    }  # fmt: skip
    for name, owner in peripherals.items():
        wires.append(wire(f"periph_{name.lower()}_req", element(demux, "mgr_ports_req_o", PERIPH[name]), (owner, "obi_req_i")))
        wires.append(wire(f"periph_{name.lower()}_rsp", (owner, "obi_rsp_o"), element(demux, "mgr_ports_rsp_i", PERIPH[name])))

    # gen_sram_bank: one shim and one SRAM for each bank
    for bank in range(NUM_SRAM_BANKS):
        shim, sram = f"i_sram_shim_{bank}", f"i_sram_{bank}"
        placed.append(instance(shim, "obi_sram_shim", **SBR_CFG))
        placed.append(instance(sram, "tc_sram_impl", NumWords=SRAM_BANK_NUM_WORDS, DataWidth=32, NumPorts=1, Latency=1))
        wires.extend(
            [
                wire(f"bank{bank}_obi_req", element(xbar, "mgr_ports_req_o", XBAR["Bank0"] + bank), (shim, "obi_req_i")),
                wire(f"bank{bank}_obi_rsp", (shim, "obi_rsp_o"), element(xbar, "mgr_ports_rsp_i", XBAR["Bank0"] + bank)),
                wire(f"bank{bank}_req", (shim, "req_o"), (sram, "req_i")),
                wire(f"bank{bank}_we", (shim, "we_o"), (sram, "we_i")),
                # bank_word_addr = bank_byte_addr[AddrWidth-1:2], cut to the SRAM's 9 address bits
                wire(f"bank{bank}_addr", (shim, "addr_o", (10, 2)), (sram, "addr_i")),
                wire(f"bank{bank}_wdata", (shim, "wdata_o"), (sram, "wdata_i")),
                wire(f"bank{bank}_be", (shim, "be_o"), (sram, "be_i")),
                wire(f"bank{bank}_rdata", (sram, "rdata_o"), (shim, "rdata_i")),
                wire(f"bank{bank}_gnt", (shim, "gnt_i"), tied="1"),  # always ready for a request
            ]
        )
    wires.append(wire("sram_impl", ("i_soc_ctrl", "sram_dly_o"), *[(f"i_sram_{bank}", "impl_i") for bank in range(NUM_SRAM_BANKS)]))

    buses = [
        connect("core_instr", ("i_core_wrap", "instr"), (xbar, "sbr3")),
        connect("core_data", ("i_core_wrap", "data"), (xbar, "sbr2")),
        connect("dbg_req", ("i_dm_top", "master"), (xbar, "sbr1")),
        connect("dbg_mem", (demux, f"mgr{PERIPH['Debug']}"), ("i_dm_top", "slave")),
    ]
    return design("croc_domain_d", placed, buses, wires)


def croc_soc_design() -> ipxact.Design:
    placed = [
        instance("i_rstgen", "rstgen"),
        instance("i_croc", "croc_domain", GpioCount="GpioCount", NumExternalIrqs=4),
        instance("i_user", "user_domain", GpioCount="GpioCount", NumExternalIrqs=4),
    ]
    through = ["jtag_tck_i", "jtag_tdi_i", "jtag_tdo_o", "jtag_tms_i", "jtag_trst_ni", "uart_rx_i", "uart_tx_o", "gpio_i", "gpio_o", "gpio_out_en_o"]
    wires = [
        wire("synced_rst_n", ("i_rstgen", "rst_no"), ("i_croc", "rst_ni"), ("i_user", "rst_ni")),
        wire("rstgen_test_mode", ("i_rstgen", "test_mode_i"), external=["testmode_i"]),
        *[wire(name, ("i_croc", name), external=[name]) for name in through],
        wire("status", ("i_croc", "core_busy_o"), external=["status_o"]),
        wire("gpio_in_sync", ("i_croc", "gpio_in_sync_o"), ("i_user", "gpio_in_sync_i")),
        wire("user_sbr_obi_req", ("i_croc", "user_sbr_obi_req_o"), ("i_user", "user_sbr_obi_req_i")),
        wire("user_sbr_obi_rsp", ("i_user", "user_sbr_obi_rsp_o"), ("i_croc", "user_sbr_obi_rsp_i")),
        wire("user_mgr_obi_req", ("i_user", "user_mgr_obi_req_o"), ("i_croc", "user_mgr_obi_req_i")),
        wire("user_mgr_obi_rsp", ("i_croc", "user_mgr_obi_rsp_o"), ("i_user", "user_mgr_obi_rsp_i")),
        wire("interrupts", ("i_user", "interrupts_o"), ("i_croc", "interrupts_i")),
    ]
    return design("croc_soc_d", placed, [], wires)


def build(croc: Path, out: Path, list_sub_ports: bool = False) -> List[str]:
    """Write the library into `out`, which is emptied first. Returns what was changed in the imported components."""
    croc = Path(croc).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "obi.xml").write_text(bus_definition_xml())
    (out / "obi_rtl.xml").write_text(abstraction_xml())
    with tempfile.TemporaryDirectory() as work:
        parts, imported = components(croc, list_sub_ports, Path(work))
    library = Library()
    for document in [*parts.values(), croc_domain_design(), croc_soc_design()]:
        library.add(document, target=True)
    ComponentExporter().export(library, out)
    DesignExporter().export(library, out)
    return [f"{module}: {change}" for module, changes in imported.changes.items() for change in changes]


def main() -> int:
    arguments = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(arguments) != 2:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    list_sub_ports = "--list-sub-ports" in sys.argv[1:]
    out = Path(arguments[1])
    for change in build(Path(arguments[0]), out, list_sub_ports):
        print(change)
    print(f"{len(SOURCES) + 3} components, 1 bus and 2 designs in {out}" + (", with the OBI structs' members listed" if list_sub_ports else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
