"""The address map package, and an address actually decoded through a generated module."""

import pytest
from xactflow import link
from xactflow_archidesc import Design

from builders import bus, bus_documents, component, has_icarus, has_verilator, library_of, port_map, simulate, slang_errors, verilator_lint, wire
from xactflow_rtl import generate_files

# A decoder written the way the rule table is meant to be used: it takes the table as a parameter.
DECODER = """
module decoder #(
    parameter int unsigned NUM_RULES = 1,
    parameter logic [NUM_RULES*96-1:0] ADDR_RULES = '0
) (
    input  logic [31:0] addr_i,
    output logic [1:0]  sel_o
);
    always_comb begin
        sel_o = '0;
        for (int unsigned i = 0; i < NUM_RULES; i++) begin
            if (addr_i >= ADDR_RULES[i*96+32 +: 32] && addr_i < ADDR_RULES[i*96 +: 32])
                sel_o[ADDR_RULES[i*96+64 +: 32]] = 1'b1;
        end
    end
endmodule
module chosen (input logic sel_i, output logic seen_o);
    assign seen_o = sel_i;
endmodule
"""

TESTBENCH = """
module tb;
    logic [31:0] addr;
    wire a, b;
    soc dut (.dec_addr_i(addr), .first_seen_o(a), .second_seen_o(b));
    integer ok = 1;
    task check(input logic [31:0] value, input logic want_a, input logic want_b);
        addr = value;
        #1;
        if (a !== want_a || b !== want_b) begin
            $display("FAIL at %h: a=%b b=%b, expected %b %b", value, a, b, want_a, want_b);
            ok = 0;
        end
    endtask
    initial begin
        check(32'h4000_0000, 1, 0);  // first address of the first window
        check(32'h4000_0fff, 1, 0);  // last address of the first window
        check(32'h4000_1000, 0, 1);  // the end is exclusive, so this is the second window
        check(32'h4000_2fff, 0, 1);  // last address of the second window
        check(32'h4000_3000, 0, 0);  // past both
        check(32'h3fff_ffff, 0, 0);  // before both
        if (ok) $display("PASS");
        $finish;
    end
endmodule
"""


def decoder():
    return component(
        "decoder",
        [wire("addr_i", "in", 32), wire("sel_o", "out", 2)],
        [
            bus("m0", "initiator", port_map("FLAG", "sel_o", select=(0, 0))),
            bus("m1", "initiator", port_map("FLAG", "sel_o", select=(1, 1))),
        ],
        parameters={"NUM_RULES": 1, "ADDR_RULES": 0},
    )


def chosen():
    return component("chosen", [wire("sel_i", "in"), wire("seen_o", "out")], [bus("b", "target", port_map("FLAG", "sel_i"))])


def soc(windows=(("m1", "second", 0x4000_1000, 0x2000), ("m0", "first", 0x4000_0000, 0x1000)), documents=None):
    """Connected out of order on purpose: a window's index follows the interface, not the script."""
    library = library_of([*bus_documents(), *(documents or [decoder(), chosen()])])
    design = Design("test.org:design:soc:1.0", library)
    dec = design.add_ip("dec", "test.org:ip:decoder:1.0")
    targets = {}
    for interface, target, base, size in windows:
        targets.setdefault(target, design.add_ip(target, "test.org:ip:chosen:1.0"))
        design.connect_bus(dec.bus(interface), targets[target], base_addr=base, size=size)
    return link(design.build(), library)


def test_package_holds_a_table_for_each_decoder():
    package = generate_files(soc())["soc_addr_map_pkg.sv"]
    assert "package soc_addr_map_pkg;" in package
    assert "localparam logic [31:0] DEC_M0_BASE = 32'h40000000;" in package
    assert "localparam logic [31:0] DEC_M1_END  = 32'h40003000;" in package
    assert "localparam int unsigned DEC_NUM_RULES = 2;" in package
    # by interface, not by the order the script connected them, and the highest element first
    assert package.index("{32'd1, 32'h40001000, 32'h40003000}") < package.index("{32'd0, 32'h40000000, 32'h40001000}")


def test_the_windows_are_also_a_flat_vector_with_rule_0_on_top():
    package = generate_files(soc())["soc_addr_map_pkg.sv"]
    assert "localparam logic [127:0] DEC_ADDR_PAIRS = {32'h40000000, 32'h40001000, 32'h40001000, 32'h40003000};" in package


def test_decoder_that_declares_the_parameters_is_given_the_table():
    text = generate_files(soc())["soc.sv"]
    assert ".NUM_RULES(soc_addr_map_pkg::DEC_NUM_RULES)" in text
    assert ".ADDR_RULES(soc_addr_map_pkg::DEC_ADDR_RULES)" in text


def test_decoder_without_the_parameters_is_left_alone():
    plain = component(
        "decoder",
        [wire("addr_i", "in", 32), wire("sel_o", "out", 2)],
        [
            bus("m0", "initiator", port_map("FLAG", "sel_o", select=(0, 0))),
            bus("m1", "initiator", port_map("FLAG", "sel_o", select=(1, 1))),
        ],
    )
    files = generate_files(soc(documents=[plain, chosen()]))
    assert "soc_addr_map_pkg.sv" in files
    assert "#(" not in files["soc.sv"]


def test_design_without_windows_has_no_package():
    files = generate_files(soc(windows=()))
    assert list(files) == ["soc.sv"]


def test_compiles_with_slang():
    files = generate_files(soc())
    assert slang_errors(files["soc_addr_map_pkg.sv"], files["soc.sv"], DECODER) == []


@pytest.mark.skipif(not has_verilator, reason="verilator not installed")
def test_verilator_finds_no_errors(tmp_path):
    result = verilator_lint(tmp_path, generate_files(soc()), [DECODER], "soc")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not has_icarus, reason="iverilog not installed")
def test_addresses_decode_through_the_generated_module(tmp_path):
    output = simulate(tmp_path, generate_files(soc()), [DECODER], TESTBENCH)
    assert "PASS" in output, output


@pytest.mark.skipif(not has_icarus, reason="iverilog not installed")
@pytest.mark.parametrize(
    "good, broken",
    [
        ("32'h40001000, 32'h40003000}", "32'h40001001, 32'h40003000}"),  # second window's start
        ("32'h40001000, 32'h40003000}", "32'h40001000, 32'h40002fff}"),  # second window's end
        ("32'h40000000, 32'h40001000}", "32'h40000000, 32'h40001001}"),  # first window's end
        ("{32'd1,", "{32'd0,"),  # which interface a window is behind
    ],
)
def test_the_testbench_notices_a_wrong_table(tmp_path, good, broken):
    files = generate_files(soc())
    assert good in files["soc_addr_map_pkg.sv"]
    files["soc_addr_map_pkg.sv"] = files["soc_addr_map_pkg.sv"].replace(good, broken)
    assert "PASS" not in simulate(tmp_path, files, [DECODER], TESTBENCH)


def test_overlapping_windows_are_refused():
    windows = (("m0", "first", 0x1000, 0x1000), ("m1", "second", 0x1800, 0x1000))
    with pytest.raises(ValueError, match="first.b and second.b overlap: 0x1000-0x2000 and 0x1800-0x2800, both decoded by dec"):
        generate_files(soc(windows))


def test_touching_windows_are_fine():
    windows = (("m0", "first", 0x1000, 0x1000), ("m1", "second", 0x2000, 0x1000))
    assert "soc_addr_map_pkg.sv" in generate_files(soc(windows))


def test_window_past_the_address_space_is_refused():
    # the abstraction declares the bus's address port as 32 bits wide
    windows = (("m0", "first", 0xFFFF_F000, 0x2000),)
    with pytest.raises(ValueError, match="past the 32-bit address space"):
        generate_files(soc(windows))


def test_window_at_the_very_top_of_the_address_space_ends_at_zero():
    """Its exclusive end does not fit in the address width. The rule shape's meaning for this is an
    end of 0, as in PULP's common_cells: 'if end_addr == 0 end of address space is assumed'."""
    windows = (("m0", "first", 0xFFFF_F000, 0x1000),)
    package = generate_files(soc(windows))["soc_addr_map_pkg.sv"]
    assert "DEC_M0_END  = 32'h00000000;" in package
    assert "{32'd0, 32'hfffff000, 32'h00000000}" in package


def test_address_width_defaults_to_32_bits_and_grows_when_it_has_to():
    no_address_port = [d for d in bus_documents()]
    no_address_port[1].ports = [p for p in no_address_port[1].ports if p.logical_name != "ADDR"]
    library = library_of([*no_address_port, decoder(), chosen()])

    def package_for(base, size):
        design = Design("test.org:design:soc:1.0", library)
        dec = design.add_ip("dec", "test.org:ip:decoder:1.0")
        design.connect_bus(dec.bus("m0"), design.add_ip("first", "test.org:ip:chosen:1.0"), base_addr=base, size=size)
        return generate_files(link(design.build(), library))["soc_addr_map_pkg.sv"]

    assert "logic [31:0] start_addr" in package_for(0x1000, 0x1000)
    assert "logic [63:0] start_addr" in package_for(0x1_0000_0000, 0x1000)
