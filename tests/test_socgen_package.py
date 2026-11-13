"""The address windows in PeakRDL-socgen's layout (D43): its names, the same windows as the design's own
package, elaborated in slang and read by a testbench in Icarus and Verilator."""

import ipxact
import pyslang
import pytest
from pyslang import ast, syntax
from xactflow import link
from xactflow_archidesc import Design

from builders import (
    ad_hoc,
    bus,
    bus_documents,
    component,
    design,
    design_alone,
    has_icarus,
    has_verilator,
    hierarchical,
    instance,
    library_of,
    port_map,
    simulate,
    slang_errors,
    verilator_lint,
    vlnv_ref,
    wire,
)
from xactflow_rtl_gen import RtlExporter, generate_files
from xactflow_rtl_gen.address_map import socgen_names

PACKAGE = "soc_addr_map_pkg"
NAMES = {"xbar_i": "main_interconnect", "ram_i": "ram"}  # rom_i and io_i keep their own names
WINDOW = '<xactflow:addressMapping xmlns:xactflow="https://github.com/IP-XACT/XactFlow" baseAddr="{:#x}" size="{:#x}"/>'


def hub():
    """A decoder with two interfaces that take addresses in, only one of them connected below."""
    return component(
        "hub",
        [wire("addr_i", "in", 32), wire("req0_i", "in"), wire("req1_i", "in"), wire("sel_o", "out", 3)],
        [
            bus("s0", "target", port_map("FLAG", "req0_i")),
            bus("s1", "target", port_map("FLAG", "req1_i")),
            *(bus(f"m{i}", "initiator", port_map("FLAG", "sel_o", select=(i, i))) for i in range(3)),
        ],
    )


def host():
    return component("host", [wire("req_o", "out")], [bus("h", "initiator", port_map("FLAG", "req_o"))])


def leaf():
    return component("leaf", [wire("sel_i", "in"), wire("seen_o", "out")], [bus("b", "target", port_map("FLAG", "sel_i"))])


def decoder():
    return component(
        "decoder",
        [wire("addr_i", "in", 32), wire("sel_o", "out", 2)],
        [bus(f"m{i}", "initiator", port_map("FLAG", "sel_o", select=(i, i))) for i in range(2)],
    )


def sub_documents(window=(0x6000_0000, 0x10)):
    """A module of its own, with a decoder and one window inside: test.org:ip:sub holds design sub_d."""
    inside = design(
        "sub_d",
        [instance("dec2", "decoder"), instance("t", "leaf")],
        [
            ipxact.Interconnection(
                name="dec2_to_t",
                active_interface=ipxact.ActiveInterface(component_instance_ref="dec2", bus_ref="m0"),
                other_active_interfaces=[
                    ipxact.ActiveInterface(component_instance_ref="t", bus_ref="b", vendor_extensions=[WINDOW.format(*window)])
                ],
            )
        ],
        [ad_hoc("addr", ("dec2", "addr_i"), external=["addr_i"]), ad_hoc("seen", ("t", "seen_o"), external=["seen_o"])],
    )
    holder = hierarchical("sub", "sub_d", [wire("addr_i", "in", 32), wire("seen_o", "out")])
    return [decoder(), inside, holder]


def chip(name="chip", windows=None, with_sub=False, buses=None):
    windows = windows or (("m2", "io_i", 0x5000_0000, 0x100), ("m0", "ram_i", 0x1000_0000, 0x2000), ("m1", "rom_i", 0x1001_0000, 0x400))
    documents = [*(buses or bus_documents()), hub(), host(), leaf(), *(sub_documents() if with_sub else [])]
    library = library_of(documents)
    the_design = Design(f"test.org:design:{name}:1.0", library)
    xbar = the_design.add_ip("xbar_i", "test.org:ip:hub:1.0")
    the_design.connect_bus(the_design.add_ip("cpu_i", "test.org:ip:host:1.0").bus("h"), xbar.bus("s0"))
    for interface, target, base, size in windows:  # out of order on purpose: idx follows the interface
        the_design.connect_bus(xbar.bus(interface), the_design.add_ip(target, "test.org:ip:leaf:1.0"), base_addr=base, size=size)
    if with_sub:
        the_design.add_ip("sub_i", "test.org:ip:sub:1.0")
    return link(design_alone(the_design), library)


def package_of(linked=None, names=NAMES):
    return generate_files(linked or chip(), socgen_package=PACKAGE, socgen_names=names)[f"{PACKAGE}.sv"]


def values(text):
    """Every parameter of the package as slang elaborates it, as a number."""
    compilation = ast.Compilation()
    compilation.addSyntaxTree(syntax.SyntaxTree.fromText(text))
    found = {}
    for member in compilation.getPackage(PACKAGE):
        if member.kind == ast.SymbolKind.Parameter:
            found[member.name] = int(member.value.value.toString(pyslang.LiteralBase.Hex, False), 16)
    return found


def test_constants_have_socgens_names_and_the_windows_values():
    found = values(package_of())
    assert found["MAIN_INTC_NMASTER"] == 1  # s0 is connected, s1 is not
    assert found["MAIN_INTC_NSLAVE"] == 3
    assert found["MAIN_INTC_BASE_ADDRESS"] == 0x1000_0000  # where the lowest window starts
    for name, base, size, idx in (("RAM", 0x1000_0000, 0x2000, 0), ("ROM_I", 0x1001_0000, 0x400, 1), ("IO_I", 0x5000_0000, 0x100, 2)):
        assert found[f"{name}_MAIN_INTC_START_ADDRESS"] == base
        assert found[f"{name}_MAIN_INTC_SIZE"] == size
        assert found[f"{name}_MAIN_INTC_END_ADDRESS"] == base + size
        assert found[f"{name}_MAIN_INTC_IDX"] == idx


def test_start_is_written_from_the_base_as_socgen_writes_it():
    package = package_of()
    assert "localparam logic [31:0] ROM_I_MAIN_INTC_START_ADDRESS = MAIN_INTC_BASE_ADDRESS + 32'h00010000;" in package
    assert "localparam logic [31:0] ROM_I_MAIN_INTC_END_ADDRESS = ROM_I_MAIN_INTC_START_ADDRESS + ROM_I_MAIN_INTC_SIZE;" in package


def test_rule_table_has_its_first_window_in_the_highest_element():
    table = values(package_of())["MAIN_INTC_ADDR_RULES"]
    elements = [(table >> (96 * i)) & ((1 << 96) - 1) for i in range(3)]
    rule = lambda idx, start, end: (idx << 64) | (start << 32) | end  # noqa: E731
    assert elements == [rule(2, 0x5000_0000, 0x5000_0100), rule(1, 0x1001_0000, 0x1001_0400), rule(0, 0x1000_0000, 0x1000_2000)]


def test_the_same_windows_as_the_designs_own_package():
    files = generate_files(chip(), socgen_package=PACKAGE, socgen_names=NAMES)
    own = files["chip_addr_map_pkg.sv"]
    for base, end in ((0x1000_0000, 0x1000_2000), (0x1001_0000, 0x1001_0400), (0x5000_0000, 0x5000_0100)):
        assert f"32'h{base:08x}, 32'h{end:08x}}}" in own
    assert files["chip.sv"] == generate_files(chip())["chip.sv"]  # the module itself is not touched


def test_a_module_generated_below_adds_its_windows_after_the_top_ones():
    package = package_of(chip(with_sub=True))
    found = values(package)
    assert package.index("MAIN_INTC_NSLAVE") < package.index("DEC2_NSLAVE")
    assert found["DEC2_NMASTER"] == 0 and found["DEC2_NSLAVE"] == 1
    assert found["T_DEC2_START_ADDRESS"] == 0x6000_0000 and found["T_DEC2_SIZE"] == 0x10
    assert "// Decoded by dec2 in sub" in package


def test_a_window_at_the_very_top_ends_at_zero_like_socgens():
    found = values(package_of(chip(windows=(("m0", "ram_i", 0xFFFF_F000, 0x1000),))))
    assert found["RAM_MAIN_INTC_END_ADDRESS"] == 0


def test_compiles_with_slang():
    assert slang_errors(package_of()) == []


TESTBENCH = f"""
module tb;
    import {PACKAGE}::*;
    logic [7:0] memory [RAM_MAIN_INTC_SIZE];  // sized by it, as a testbench written for socgen's package does
    addr_map_rule_t [MAIN_INTC_NSLAVE-1:0] rules;  // Icarus 13 indexes a parameter's packed array as bits, a copy right
    initial begin
        rules = MAIN_INTC_ADDR_RULES;
        if ($size(memory) == 32'h2000
            && rules[2] == {{32'd0, 32'h10000000, 32'h10002000}}
            && rules[0] == {{32'd2, 32'h50000000, 32'h50000100}}
            && IO_I_MAIN_INTC_END_ADDRESS == 32'h50000100)
            $display("PASS");
        else
            $display("FAIL");
    end
endmodule
"""


@pytest.mark.skipif(not has_icarus, reason="iverilog not installed")
def test_a_testbench_reads_it_in_icarus(tmp_path):
    assert "PASS" in simulate(tmp_path, {f"{PACKAGE}.sv": package_of()}, [], TESTBENCH)


@pytest.mark.skipif(not has_verilator, reason="verilator not installed")
def test_verilator_finds_no_errors(tmp_path):
    result = verilator_lint(tmp_path, {f"{PACKAGE}.sv": package_of()}, [TESTBENCH], "tb")
    assert result.returncode == 0, result.stderr


def test_through_the_exporter_with_the_clis_strings(tmp_path):
    RtlExporter().export(chip(), tmp_path, socgen_package=PACKAGE, socgen_names="xbar_i:main_interconnect, ram_i:ram")
    assert "RAM_MAIN_INTC_SIZE" in (tmp_path / f"{PACKAGE}.sv").read_text()


def test_nothing_more_without_the_option():
    assert sorted(generate_files(chip())) == ["chip.sv", "chip_addr_map_pkg.sv"]


# --- refused


def test_a_name_for_an_instance_no_window_has_is_refused():
    with pytest.raises(ValueError, match="socgen_names names cpu_i, which no address window here is decoded by or reaches"):
        package_of(names={**NAMES, "cpu_i": "core"})


def test_two_windows_with_one_name_are_refused():
    with pytest.raises(ValueError, match="would both define MEM_MAIN_INTC_START_ADDRESS"):
        package_of(names={"xbar_i": "main_interconnect", "ram_i": "mem", "rom_i": "mem"})


def test_a_design_with_no_window_is_refused():
    with pytest.raises(ValueError, match="no module generated here has an address window"):
        package_of(chip(windows=(("m0", "ram_i", None, None),)), names={})


def test_an_address_map_wider_than_32_bits_is_refused():
    no_address_port = bus_documents()
    no_address_port[1].ports = [p for p in no_address_port[1].ports if p.logical_name != "ADDR"]
    with pytest.raises(ValueError, match="its address map is 64 bits wide, and the socgen package's addresses are 32"):
        package_of(chip(windows=(("m0", "ram_i", 0x1_0000_0000, 0x1000),), buses=no_address_port))


def test_windows_reached_from_outside_the_module_are_refused():
    inside = design(
        "outside_d",
        [instance("t", "leaf")],
        [
            ipxact.Interconnection(
                name="in",
                active_interface=ipxact.ActiveInterface(component_instance_ref="t", bus_ref="b", vendor_extensions=[WINDOW.format(0, 0x100)]),
                hier_interfaces=[ipxact.HierInterface(bus_ref="b")],
            )
        ],
        [ad_hoc("seen", ("t", "seen_o"), external=["seen_o"])],
    )
    holder = hierarchical("outside", "outside_d", [wire("sel_i", "in"), wire("seen_o", "out")], [bus("b", "target", port_map("FLAG", "sel_i"))])
    linked = link(inside, library_of([*bus_documents(), leaf(), inside, holder]))
    assert "outside_addr_map_pkg.sv" in generate_files(linked)
    with pytest.raises(ValueError, match="windows reached from outside it, through its own interfaces, have no place in the socgen package"):
        generate_files(linked, socgen_package=PACKAGE)


def test_a_package_with_the_name_of_the_designs_own_is_refused():
    with pytest.raises(ValueError, match="two different files would both be called soc_addr_map_pkg.sv"):
        package_of(chip(name="soc"))


@pytest.mark.parametrize("name", ["2pkg", "module", "soc-pkg", ""])
def test_a_package_name_that_is_no_identifier_is_refused(name):
    with pytest.raises(TypeError, match="is not a package name"):
        generate_files(chip(), socgen_package=name)


def test_names_without_the_package_are_refused():
    with pytest.raises(TypeError, match="socgen_names only applies with socgen_package"):
        generate_files(chip(), socgen_names=NAMES)


@pytest.mark.parametrize(
    "value, match",
    [
        ("xbar_i", "takes instance:name pairs"),
        ("xbar_i:a:b", "takes instance:name pairs"),
        ("xbar_i:9lives", "name '9lives' is not an identifier"),
        ("xbar_i:a,xbar_i:b", "gives xbar_i two names, a and b"),
    ],
)
def test_malformed_names_are_refused(value, match):
    with pytest.raises(TypeError, match=match):
        socgen_names(value)


def test_names_from_python_as_a_dict_or_pairs():
    assert socgen_names({"a_i": "a"}) == socgen_names(["a_i:a"]) == socgen_names(" a_i : a ,") == {"a_i": "a"}
