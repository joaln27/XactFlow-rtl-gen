"""The exporter's interface, and every case it must refuse rather than write wrong RTL."""

import copy
import dataclasses
import inspect
import shutil
import subprocess

import ipxact
import pytest
from xactflow import Library, link
from xactflow.cli import main
from xactflow_archidesc import Design
from xactflow_design import DesignExporter

from builders import design_alone
from conftest import LIB, RTL
from test_generated_rtl import slang_errors
from xactflow_rtl_gen import RtlExporter, file_list, generate_top

MASTER = ipxact.VLNV("test.org", "ip", "master", "1.0")
SLAVE = ipxact.VLNV("test.org", "ip", "slave", "1.0")


def ad_hoc(name, *ports, **fields):
    references = [ipxact.InternalPortReference(component_instance_ref=i, port_ref=p) for i, p in ports]
    return ipxact.AdHocConnection(name=name, internal_port_references=references, **fields)


def with_ad_hoc(soc, *connections):
    return dataclasses.replace(soc, ad_hoc_connections=[*soc.ad_hoc_connections, *connections])


def changed_library(vlnv, change):
    """The test library with one component changed. Library.scan hands out shared objects."""
    library = Library.scan(LIB)
    component = copy.deepcopy(library.get_component(vlnv))
    change(component)
    library.add(component, replace=True)
    return library


def port_of(component, name):
    return next(p for p in component.model.ports if p.name == name)


def port_map_of(component, logical):
    port_maps = component.bus_interfaces[0].abstraction_types[0].port_maps
    return next(m for m in port_maps if m.logical_port == logical)


# --- what comes out


def test_module_name_comes_from_the_view_not_the_vlnv(soc, library):
    assert "master_core m (" in generate_top(link(soc, library))


def test_ports_no_connection_reaches_become_top_level_ports(soc, library):
    text = generate_top(link(soc, library))
    for line in ("input  logic        m_clk_i", "input  logic        s_clk_i", "output logic [7:0]  m_result_o"):
        assert line in text


def test_common_ports_become_one_top_level_port(soc, library):
    text = generate_top(link(soc, library), common=["clk_i"])
    assert "input  logic        clk_i" in text
    assert "m_clk_i" not in text and "s_clk_i" not in text
    assert ".clk_i    (clk_i)" in text


def test_a_bus_signal_with_no_receiver_is_left_open_not_exposed(soc, library):
    text = generate_top(link(soc, library))
    assert ".debug_o  ()" in text
    assert "m_debug_o" not in text


def test_parameter_override_sets_the_width_of_the_top_level_port(soc, library):
    assert "output logic [15:0] s_data_o" in generate_top(link(soc, library))


def test_unused_ports_of_a_design_without_overrides_keep_their_default_width(library):
    design = Design("test.org:design:plain:1.0", library)
    design.add_ip("s", "test.org:ip:slave:1.0")
    text = generate_top(link(design_alone(design), library))
    assert "output logic [7:0]  s_data_o" in text
    assert ".OUT_W(8)" in text  # the component's value, written rather than left to the HDL's default


def test_address_window_and_transforms_are_recorded_in_comments(library):
    design = Design("test.org:design:noted:1.0", library)
    master = design.add_ip("m", "test.org:ip:master:1.0", transforms=["tmrg"])
    slave = design.add_ip("s", "test.org:ip:slave:1.0")
    design.connect_bus(master, slave, base_addr=0x1000, size=0x100)
    text = generate_top(link(design_alone(design), library))
    assert "baseAddr 0x1000, size 0x100" in text
    assert "not applied here: tmrg" in text


def test_external_port_reference_names_a_shared_top_level_port(soc, library):
    clock = ad_hoc(
        "clocks", ("m", "clk_i"), ("s", "clk_i"), external_port_references=[ipxact.ExternalPortReference(port_ref="sys_clk")]
    )
    text = generate_top(link(with_ad_hoc(soc, clock), library))
    assert "input  logic        sys_clk" in text
    assert ".clk_i    (sys_clk)" in text
    assert "m_clk_i" not in text


def test_tied_ad_hoc_values(soc, library):
    design = with_ad_hoc(
        soc,
        ad_hoc("tie", ("m", "rst_ni"), tied_value=ipxact.Expression("1")),
        ad_hoc("open", ("s", "rst_ni"), tied_value=ipxact.Expression("open")),
    )
    text = generate_top(link(design, library))
    assert "assign m_rst_ni = 1'h1;" in text
    assert ".rst_ni   ()" in text


def test_struct_ports_connect_whole_and_by_sub_port(tmp_path):
    def struct_port(name, direction):
        structured = ipxact.StructuredPort(
            struct_type="struct",
            direction=direction,
            struct_port_type_defs=[ipxact.StructPortTypeDef(type_name="req_pkg::req_t")],
        )
        return ipxact.Port(name=name, structured=structured)

    def component(name, ports):
        return ipxact.Component(vlnv=ipxact.VLNV("test.org", "ip", name, "1.0"), model=ipxact.Model(ports=ports))

    bit = lambda name, direction: ipxact.Port(name=name, wire=ipxact.WirePort(direction=direction))
    library = Library()
    library.add(component("src", [struct_port("req_o", ipxact.Direction.OUT), bit("ok_o", ipxact.Direction.OUT)]))
    library.add(component("dst", [struct_port("req_i", ipxact.Direction.IN), bit("flag_o", ipxact.Direction.OUT)]))
    design = ipxact.Design(
        vlnv=ipxact.VLNV("test.org", "design", "structs", "1.0"),
        component_instances=[
            ipxact.ComponentInstance(instance_name=n, component_ref=ipxact.VLNVRef("test.org", "ip", n, "1.0"))
            for n in ("src", "dst")
        ],
        ad_hoc_connections=[ad_hoc("req", ("src", "req_o"), ("dst", "req_i"))],
    )
    text = generate_top(link(design, library))
    assert "req_pkg::req_t dst_req_i;" in text.replace("  ", " ")
    assert "assign dst_req_i = src_req_o;" in text
    assert slang_errors(text, (RTL / "struct_ips.sv").read_text()) == []

    sub_ports = lambda *names: [ipxact.SubPortReference(sub_port_ref=name) for name in names]
    valid = ipxact.InternalPortReference(component_instance_ref="dst", port_ref="req_i", sub_port_refs=sub_ports("valid"))
    driver = ipxact.InternalPortReference(component_instance_ref="src", port_ref="ok_o")
    by_sub_port = ipxact.AdHocConnection(name="valid", internal_port_references=[driver, valid])
    text = generate_top(link(dataclasses.replace(design, ad_hoc_connections=[by_sub_port]), library))
    assert "assign dst_req_i.valid = src_ok_o;" in text
    assert slang_errors(text, (RTL / "struct_ips.sv").read_text()) == []

    both = dataclasses.replace(design, ad_hoc_connections=[*design.ad_hoc_connections, by_sub_port])
    with pytest.raises(ValueError, match=r"dst.req_i.valid is already driven"):
        generate_top(link(both, library))


# --- refused instead of wrong


def test_width_mismatch_is_refused_including_after_an_override(library):
    design = Design("test.org:design:bad:1.0", library)
    master = design.add_ip("m", "test.org:ip:master:1.0")
    slave = design.add_ip("s", "test.org:ip:slave:1.0", parameters={"OUT_W": 8})
    design.connect(slave.data_o, master.rdata_i)
    assert "assign m_rdata_i = s_data_o;" in generate_top(link(design_alone(design), library))

    design = Design("test.org:design:bad:1.0", library)
    master = design.add_ip("m", "test.org:ip:master:1.0")
    slave = design.add_ip("s", "test.org:ip:slave:1.0", parameters={"OUT_W": 16})
    design.connect(slave.data_o, master.rdata_i)
    with pytest.raises(ValueError, match="width mismatch.*16 bits.*8 bits"):
        generate_top(link(design_alone(design), library))


def test_an_input_driven_twice_is_refused(soc, library):
    design = with_ad_hoc(soc, ad_hoc("again", ("s", "irq_o"), ("m", "ready_i")))
    with pytest.raises(ValueError, match="m.ready_i is already driven"):
        generate_top(link(design, library))


def test_external_port_on_an_already_driven_input_is_refused(soc, library):
    design = with_ad_hoc(soc, ad_hoc("ext", ("m", "ready_i"), external_port_references=[ipxact.ExternalPortReference("r")]))
    with pytest.raises(ValueError, match="m.ready_i is already driven"):
        generate_top(link(design, library))


def test_two_outputs_on_one_logical_port_are_refused(soc):
    def make_valid_an_output(slave):
        port_of(slave, "valid_ni").wire.direction = ipxact.Direction.OUT

    with pytest.raises(ValueError, match="'VALID' has more than one driver: m.valid_o, s.valid_ni"):
        generate_top(link(soc, changed_library(SLAVE, make_valid_an_output)))


def test_partly_driven_input_is_refused(soc):
    def narrow(master):
        rdata = port_map_of(master, "RDATA")
        rdata.part_select = ipxact.PartSelect(range=ipxact.Range(ipxact.Expression("3"), ipxact.Expression("0")))

    def split(slave):
        rdata = port_map_of(slave, "RDATA")
        rdata.part_select = ipxact.PartSelect(range=ipxact.Range(ipxact.Expression("3"), ipxact.Expression("0")))

    library = changed_library(MASTER, narrow)
    slave = copy.deepcopy(library.get_component(SLAVE))
    split(slave)
    library.add(slave, replace=True)
    with pytest.raises(ValueError, match=r"m.rdata_i: bits \[4, 5, 6, 7\] are not driven"):
        generate_top(link(soc, library))


def test_ad_hoc_reference_to_no_port_is_refused(soc, library):
    design = with_ad_hoc(soc, ad_hoc("typo", ("s", "irq_o"), ("m", "no_such_port")))
    linked = link(design, library)
    assert len(linked.ad_hoc_connections["typo"]) == 1  # link() leaves the reference out
    with pytest.raises(ValueError, match=r"SCR 13\.5.*'typo' internalPortReference portRef 'no_such_port'"):
        generate_top(linked)
    linked.diagnostics.clear()  # as XactFlow 0.3 linked it, with no diagnostic: the exporter's own check
    with pytest.raises(ValueError, match=r"'typo' refers to unknown ports: m.no_such_port"):
        generate_top(linked)


def test_override_that_reaches_no_module_parameter_is_refused(soc):
    def forget_module_parameter(slave):
        slave.model.component_instantiations[0].module_parameters = [
            p for p in slave.model.component_instantiations[0].module_parameters if p.name != "GAIN"
        ]

    with pytest.raises(ValueError, match="reach no module parameter: GAIN"):
        generate_top(link(soc, changed_library(SLAVE, forget_module_parameter)))


def test_link_errors_are_refused(soc, library):
    design = dataclasses.replace(
        soc,
        component_instances=[
            *soc.component_instances,
            ipxact.ComponentInstance(instance_name="x", component_ref=ipxact.VLNVRef("test.org", "ip", "missing", "1.0")),
        ],
    )
    with pytest.raises(ValueError, match="has link errors"):
        generate_top(link(design, library))


def test_unknown_common_port_is_refused(soc, library):
    with pytest.raises(ValueError, match="common ports match no instance input: clk"):
        generate_top(link(soc, library), common=["clk"])


@pytest.mark.parametrize("name", ["interconnect", "logic"])
def test_keyword_names_are_refused(library, name):
    design = Design(f"test.org:design:{name}:1.0", library)
    with pytest.raises(ValueError, match="SystemVerilog keyword"):
        generate_top(link(design_alone(design), library))

    design = Design("test.org:design:fine:1.0", library)
    design.add_ip(name, "test.org:ip:slave:1.0")
    with pytest.raises(ValueError, match="SystemVerilog keyword"):
        generate_top(link(design_alone(design), library))


def test_name_with_characters_systemverilog_lacks_is_refused(library):
    design = Design("test.org:design:fine:1.0", library)
    design.add_ip("a.b", "test.org:ip:slave:1.0")
    with pytest.raises(ValueError, match="not a valid SystemVerilog identifier"):
        generate_top(link(design_alone(design), library))


def set_direction(direction):
    def change(slave):
        port_of(slave, "irq_o").wire.direction = direction

    return change


def make_interface(slave):
    port = port_of(slave, "irq_o")
    port.wire = None
    port.structured = ipxact.StructuredPort(struct_type="interface")


def make_array(slave):
    port_of(slave, "irq_o").arrays = [ipxact.ArrayBound(ipxact.Expression("1"), ipxact.Expression("0"))]


def map_ascending_logical_range(slave):
    port_map_of(slave, "WDATA").logical_range = ipxact.Range(ipxact.Expression("0"), ipxact.Expression("7"))


def map_indexed(slave):
    port_map_of(slave, "WDATA").part_select = ipxact.PartSelect(indices=[ipxact.Expression("0")])


@pytest.mark.parametrize(
    "change, message",
    [
        (make_interface, "interface ports are not supported yet"),
        (make_array, "unpacked array ports are not supported yet"),
        (map_ascending_logical_range, "ascending logical range"),
        (map_indexed, "indexed part select is not supported yet"),
    ],
)
def test_features_not_supported_yet_say_so(soc, change, message):
    with pytest.raises(ValueError, match=message):
        generate_top(link(soc, changed_library(SLAVE, change)))


def test_phantom_port_gets_no_connection(library):
    library = changed_library(SLAVE, set_direction(ipxact.Direction.PHANTOM))
    design = Design("test.org:design:ph:1.0", library)
    design.add_ip("s", "test.org:ip:slave:1.0")
    assert "irq_o" not in generate_top(link(design_alone(design), library))


def test_monitors_are_refused(soc, library):
    interface = ipxact.MonitorInterfaceRef(component_instance_ref="m", bus_ref="bus")
    monitor = ipxact.MonitorInterconnection(name="mon", monitored_active_interface=interface, monitor_interfaces=[interface])
    with pytest.raises(ValueError, match="monitor interconnections are not supported yet"):
        generate_top(link(dataclasses.replace(soc, monitor_interconnections=[monitor]), library))


def test_hier_interface_needs_a_hierarchical_component(soc, library):
    # In place of the target: three interfaces on one connection need a bus definition that broadcasts (SCR 2.17).
    connection = dataclasses.replace(soc.interconnections[0], other_active_interfaces=[], hier_interfaces=[ipxact.HierInterface(bus_ref="top_bus")])
    with pytest.raises(ValueError, match="hierInterface needs a hierarchical component"):
        generate_top(link(dataclasses.replace(soc, interconnections=[connection]), library))


def test_design_configuration_selecting_a_missing_view_is_refused(soc, library):
    configuration = ipxact.DesignConfiguration(
        vlnv=ipxact.VLNV("test.org", "design", "cfg", "1.0"),
        view_configurations=[ipxact.ViewConfiguration(instance_name="m", view_ref="gate_level")],
    )
    with pytest.raises(ValueError, match="selects view 'gate_level', which does not exist"):
        generate_top(link(soc, library, configuration))


# --- the plugin interface


def test_exporter_writes_design_name_dot_sv(soc, library, tmp_path):
    RtlExporter().export(link(soc, library), tmp_path / "out")
    assert (tmp_path / "out" / "soc.sv").read_text().startswith("// Generated by xactflow-rtl-gen")


def test_the_design_that_is_a_target_of_the_library_is_linked_and_exported(soc, library, tmp_path):
    """What the CLI and an importer hand over since XactFlow 0.4.0 (D44)."""
    library.add(soc, target=True)
    RtlExporter().export(library, tmp_path, common="clk_i,rst_ni")
    assert "input  logic        clk_i" in (tmp_path / "soc.sv").read_text()
    assert ".GAIN(5)" in (tmp_path / "soc.sv").read_text()


def test_a_library_with_no_design_to_export_raises_type_error(library):
    with pytest.raises(TypeError, match="needs an ipxact.Design target in the library"):
        RtlExporter().export(library, "unused")
    library.entries[MASTER].target = True  # a component alone is not a design
    with pytest.raises(TypeError, match="needs an ipxact.Design target in the library"):
        RtlExporter().export(library, "unused")


def test_a_design_in_the_library_that_is_no_target_is_not_exported(soc, library):
    library.add(soc)
    with pytest.raises(TypeError, match="needs an ipxact.Design target in the library"):
        RtlExporter().export(library, "unused")


def test_two_designs_to_export_are_refused(soc, library):
    library.add(soc, target=True)
    library.add(dataclasses.replace(soc, vlnv=dataclasses.replace(soc.vlnv, name="other")), target=True)
    with pytest.raises(ValueError, match="one design at a time, and the library has 2 as targets: .*design:soc:.*design:other:"):
        RtlExporter().export(library, "unused")


def test_a_design_configuration_that_is_a_target_configures_the_design(soc, library, tmp_path):
    """As the CLI's --config adds it."""
    library.add(soc, target=True)
    library.add(
        ipxact.DesignConfiguration(
            vlnv=ipxact.VLNV("test.org", "design", "cfg", "1.0"),
            view_configurations=[ipxact.ViewConfiguration(instance_name="m", view_ref="gate_level")],
        ),
        target=True,
    )
    with pytest.raises(ValueError, match="selects view 'gate_level', which does not exist"):
        RtlExporter().export(library, tmp_path)


def test_a_library_error_stops_the_export(soc, library, tmp_path):
    library.add(soc, target=True)
    library.add(library.get_component(MASTER))  # a second document with one VLNV (SCR 1.1)
    with pytest.raises(ValueError, match="has link errors: .*duplicate VLNV"):
        RtlExporter().export(library, tmp_path)


def test_a_bare_design_is_not_enough(soc):
    with pytest.raises(TypeError, match="target of a Library, or a LinkedDesign, got Design"):
        RtlExporter().export(soc, "unused")


def test_the_lib_option_is_gone(soc, library):
    library.add(soc, target=True)
    with pytest.raises(TypeError, match="the lib option is gone.*--lib"):
        RtlExporter().export(library, "unused", lib=str(LIB))


def test_other_subjects_and_unknown_options_raise_type_error(soc, library):
    with pytest.raises(TypeError, match="got Component"):
        RtlExporter().export(library.get_component(MASTER), "unused")
    with pytest.raises(TypeError, match="unknown options: nope"):
        RtlExporter().export(link(soc, library), "unused", nope="1")


def test_regenerating_is_fine_but_a_hand_written_file_is_kept(soc, library, tmp_path):
    linked = link(soc, library)
    RtlExporter().export(linked, tmp_path)
    RtlExporter().export(linked, tmp_path)

    (tmp_path / "soc.sv").write_text("module soc; endmodule // written by a person\n")
    with pytest.raises(FileExistsError, match="not generated by xactflow-rtl-gen"):
        RtlExporter().export(linked, tmp_path)
    assert "written by a person" in (tmp_path / "soc.sv").read_text()


def test_a_file_generated_under_the_former_name_can_be_regenerated(soc, library, tmp_path):
    (tmp_path / "soc.sv").write_text("// Generated by xactflow-rtl 0.2.0 from test.org:design:soc:1.0. Do not edit.\n")
    RtlExporter().export(link(soc, library), tmp_path)
    assert (tmp_path / "soc.sv").read_text().startswith("// Generated by xactflow-rtl-gen ")


def test_filelist_option_lists_what_was_generated_for_a_compilers_dash_F(soc, library, tmp_path):
    out = tmp_path / "out"
    for _ in range(2):  # the second time over its own output
        RtlExporter().export(link(soc, library), out, common="clk_i,rst_ni", filelist="soc.f")
    lines = (out / "soc.f").read_text().splitlines()
    assert lines[0].startswith("// Generated by xactflow-rtl-gen ")
    names = [line for line in lines if not line.startswith("//")]
    assert names == ["soc_addr_map_pkg.sv", "soc.sv"]  # the package before the module that may use it
    assert sorted(names) == sorted(path.name for path in out.glob("*.sv"))
    sources = [str(RTL / "master_core.sv"), str(RTL / "slave.sv")]
    if shutil.which("verilator"):  # from another folder: -F reads the names relative to the list
        result = subprocess.run(["verilator", "--lint-only", "--top-module", "soc", "-F", str(out / "soc.f"), *sources], capture_output=True, text=True, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
    if shutil.which("iverilog"):  # Icarus has only -f, which reads them relative to where it runs
        result = subprocess.run(["iverilog", "-g2012", "-s", "soc", "-o", str(tmp_path / "soc.vvp"), "-f", "soc.f", *sources], capture_output=True, text=True, cwd=out)
        assert result.returncode == 0, result.stderr


def test_the_file_list_ends_with_the_module_the_others_are_instances_in():
    marker = "// Generated by xactflow-rtl-gen 0.0.0 from a:b:c:1.0. Do not edit.\n"
    files = {
        "soc.sv": marker + "module soc; endmodule\n",
        "soc_addr_map_pkg.sv": marker + "\npackage soc_addr_map_pkg;\nendpackage\n",
        "wrap.sv": marker + "module wrap; endmodule\n",
        "wrap_addr_map_pkg.sv": marker + "\npackage wrap_addr_map_pkg;\nendpackage\n",
        "leaf_1.sv": marker + "// package of nothing\nmodule leaf_1; endmodule\n",
    }
    lines = file_list(files).splitlines()
    assert lines[0] == marker.strip() and lines[1].startswith("// ")
    assert lines[2:] == ["soc_addr_map_pkg.sv", "wrap_addr_map_pkg.sv", "leaf_1.sv", "wrap.sv", "soc.sv"]


def test_a_file_list_that_is_no_plain_name_or_takes_a_modules_file_is_refused(soc, library, tmp_path):
    for name in ("../soc.f", "lists/soc.f", "", 3):
        with pytest.raises(TypeError, match="the filelist option is a file name in the output folder"):
            RtlExporter().export(link(soc, library), tmp_path, filelist=name)
    with pytest.raises(ValueError, match="the file list cannot be called soc.sv"):
        RtlExporter().export(link(soc, library), tmp_path, filelist="soc.sv")
    (tmp_path / "soc.f").write_text("my_own.sv\n")
    with pytest.raises(FileExistsError, match="not generated by xactflow-rtl-gen"):
        RtlExporter().export(link(soc, library), tmp_path, filelist="soc.f")
    assert not (tmp_path / "soc.sv").exists()  # refused before anything was written


def test_a_folder_in_the_way_of_a_file_is_refused(soc, library, tmp_path):
    for name in (".", ".."):
        with pytest.raises(TypeError, match="the filelist option is a file name in the output folder"):
            RtlExporter().export(link(soc, library), tmp_path, filelist=name)
    (tmp_path / "lists").mkdir()
    with pytest.raises(FileExistsError, match="lists already exists and is a folder"):
        RtlExporter().export(link(soc, library), tmp_path, filelist="lists")
    (tmp_path / "out" / "soc.sv").mkdir(parents=True)
    with pytest.raises(FileExistsError, match="soc.sv already exists and is a folder"):
        RtlExporter().export(link(soc, library), tmp_path / "out")


def test_unconnected_option_reaches_the_generation(soc, library, tmp_path):
    RtlExporter().export(link(soc, library), tmp_path, unconnected="open")
    with pytest.raises(TypeError, match="the unconnected option is one of refuse, open, got 'maybe'"):
        RtlExporter().export(link(soc, library), tmp_path, unconnected="maybe")


def test_cli_exporter_subcommand_on_a_design_file(soc, library, tmp_path, capsys):
    library.add(soc, target=True)
    DesignExporter().export(library, tmp_path)
    design_file = tmp_path / "test.org_design_soc_1.0.xml"
    arguments = ["rtl-gen", str(design_file), "--lib", str(LIB), "--output", str(tmp_path / "rtl"), "--option", "common=clk_i,rst_ni"]
    assert main(arguments) == 0
    assert ".GAIN(5)" in (tmp_path / "rtl" / "soc.sv").read_text()


TOP_PORTS = (
    "clk = design.add_port('in', 'clk_i')\n"
    "rst = design.add_port('in', 'rst_ni')\n"
    "for ip in (m, s):\n"
    "    design.connect(clk, ip.clk_i)\n"
    "    design.connect(rst, ip.rst_ni)\n"
)


# The component xactflow-archidesc builds names the module since the release after 0.3.0 (its issue 11). The
# one 0.3.0 builds does not, and is refused for its own name, soc.component (D47).
archidesc_names_the_module = pytest.mark.skipif(
    "module_name" not in inspect.signature(Design).parameters,
    reason="this xactflow-archidesc gives the component it builds no moduleName",
)


def write_script(tmp_path, top_ports=TOP_PORTS, design_arguments=""):
    """A design script for xactflow-archidesc: the component it builds, soc.component, names the module and
    defines its ports, so the script declares them."""
    script = tmp_path / "soc.py"
    script.write_text(
        "from xactflow import Library\n"
        "from xactflow_archidesc import Design\n"
        f"library = Library.scan({str(LIB)!r})\n"
        f"design = Design('test.org:design:soc:1.0', library{design_arguments})\n"
        "m = design.add_ip('m', 'test.org:ip:master:1.0')\n"
        "s = design.add_ip('s', 'test.org:ip:slave:1.0')\n"
        "design.connect_bus(m, s)\n"
        "design.connect(s.irq_o, m.irq_i)\n" + top_ports
    )
    return script


@archidesc_names_the_module
def test_cli_chained_from_archidesc_as_on_the_slide(tmp_path):
    """The importer's design and component reach the exporter in the library that --lib scanned (D44)."""
    arguments = ["archidesc", str(write_script(tmp_path)), "--lib", str(LIB), "--then", "rtl-gen", "--output", str(tmp_path / "rtl")]
    assert main(arguments) == 0
    text = (tmp_path / "rtl" / "soc.sv").read_text()  # soc, the moduleName archidesc gives by default
    assert "module soc (" in text
    assert "input  logic clk_i" in text


@archidesc_names_the_module
def test_cli_chained_from_a_script_that_names_its_module(tmp_path):
    script = write_script(tmp_path, design_arguments=", module_name='soc_top'")
    assert main(["archidesc", str(script), "--lib", str(LIB), "--then", "rtl-gen", "--output", str(tmp_path / "rtl")]) == 0
    assert [file.name for file in (tmp_path / "rtl").iterdir()] == ["soc_top.sv"]
    assert "module soc_top (" in (tmp_path / "rtl" / "soc_top.sv").read_text()


def test_cli_chained_without_the_library_gives_a_one_line_error(tmp_path, capsys):
    arguments = ["archidesc", str(write_script(tmp_path)), "--then", "rtl-gen", "--output", str(tmp_path / "rtl")]
    assert main(arguments) == 1
    error = capsys.readouterr().err
    assert "'rtl-gen' failed while exporting the output of 'archidesc'" in error
    assert "[SCR 1.9] componentInstance 'm' componentRef does not resolve to a component" in error
    assert error.count("\n") == 1


def test_cli_chained_with_the_lib_option_of_before_says_where_it_went(tmp_path, capsys):
    arguments = ["archidesc", str(write_script(tmp_path)), "--then", "rtl-gen", "--then-option", f"lib={LIB}", "--output", str(tmp_path / "rtl")]
    assert main(arguments) == 1
    assert "'rtl-gen' cannot export the output of 'archidesc'" in capsys.readouterr().err.split("the lib option is gone")[0]


@archidesc_names_the_module
def test_cli_chained_from_a_script_that_declares_no_top_ports(tmp_path, capsys):
    """The component archidesc builds has no port then, and it defines the module's: an input left over is
    refused by name, or left open when asked (D38)."""
    script = str(write_script(tmp_path, top_ports=""))
    arguments = ["archidesc", script, "--lib", str(LIB), "--then", "rtl-gen", "--output", str(tmp_path / "rtl")]
    assert main(arguments) == 1
    assert "m.clk_i is not connected, and top component test.org:design:soc.component:1.0 defines the module's ports" in capsys.readouterr().err
    assert main([*arguments, "--then-option", "unconnected=open"]) == 0
    assert "module soc (" in (tmp_path / "rtl" / "soc.sv").read_text()
