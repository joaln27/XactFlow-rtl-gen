"""Connection scenarios, each run in real tools.

The list of situations follows the cases Kactus2's Verilog generator is tested on, as a checklist of
what a generator like this has to get right. The designs and checks here are written for this project.

Every scenario compiles in slang and Verilator and passes a simulation. Then every assign it generates
is broken in turn, and the simulation has to notice, so no connection here goes unchecked.
"""

import re

import pytest

from builders import (
    Scenario,
    ad_hoc,
    bus,
    component,
    design,
    hierarchical,
    has_icarus,
    has_verilator,
    instance,
    interconnection,
    port_map,
    simulate,
    slang_errors,
    tlm,
    verilator_lint,
    wire,
)

SEND_WORD = """
module sender #(parameter logic [7:0] VALUE = 8'ha5) (output logic [7:0] data_o);
    assign data_o = VALUE;
endmodule
"""
SEE_WORD = """
module seer (input logic [7:0] data_i, output logic [7:0] seen_o);
    assign seen_o = data_i;
endmodule
"""


def sender():
    return component("sender", [wire("data_o", "out", 8)], [bus("b", "initiator", port_map("WORD", "data_o"))], module_parameters=[])


def seer(name="seer", module="seer"):
    return component(
        name, [wire("data_i", "in", 8), wire("seen_o", "out", 8)], [bus("b", "target", port_map("WORD", "data_i"))], module=module
    )


def plain_master_to_slave():
    return Scenario(
        "plain_master_to_slave",
        design("soc", [instance("m", "sender"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [sender(), seer()],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xA5)},
    )


def one_bit_slicing():
    source = """
    module wide (output logic [7:0] data_o);
        assign data_o = 8'h20;
    endmodule
    module narrow (input logic flag_i, output logic seen_o);
        assign seen_o = flag_i;
    endmodule
    """
    return Scenario(
        "one_bit_slicing",
        design("soc", [instance("m", "wide"), instance("s", "narrow")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            component("wide", [wire("data_o", "out", 8)], [bus("b", "initiator", port_map("FLAG", "data_o", select=(5, 5)))]),
            component("narrow", [wire("flag_i", "in"), wire("seen_o", "out")], [bus("b", "target", port_map("FLAG", "flag_i"))]),
        ],
        source,
        "soc",
        expect={"s_seen_o": (1, 1)},
    )


def physically_sliced_master_to_slave():
    source = """
    module wide (output logic [15:0] data_o);
        assign data_o = 16'hbeef;
    endmodule
    """
    return Scenario(
        "physically_sliced_master_to_slave",
        design("soc", [instance("m", "wide"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            component("wide", [wire("data_o", "out", 16)], [bus("b", "initiator", port_map("WORD", "data_o", select=(11, 4)))]),
            seer(),
        ],
        source + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xEE)},
    )


def port_sliced_across_logical_ports():
    source = """
    module wide (output logic [15:0] data_o);
        assign data_o = 16'hbeef;
    endmodule
    module wide_seer (input logic [15:0] data_i, output logic [15:0] seen_o);
        assign seen_o = data_i;
    endmodule
    """
    return Scenario(
        "port_sliced_across_logical_ports",
        design("soc", [instance("m", "wide"), instance("s", "wide_seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            component(
                "wide",
                [wire("data_o", "out", 16)],
                [bus("b", "initiator", port_map("WORD", "data_o", select=(7, 0)), port_map("HIGH", "data_o", select=(15, 8)))],
            ),
            component(
                "wide_seer",
                [wire("data_i", "in", 16), wire("seen_o", "out", 16)],
                [bus("b", "target", port_map("WORD", "data_i", select=(15, 8)), port_map("HIGH", "data_i", select=(7, 0)))],
            ),
        ],
        source,
        "soc",
        expect={"s_seen_o": (16, 0xEFBE)},
    )


def logical_slices_from_two_ports():
    source = """
    module nibbles (output logic [3:0] hi_o, output logic [3:0] lo_o);
        assign hi_o = 4'ha;
        assign lo_o = 4'h5;
    endmodule
    """
    return Scenario(
        "logical_slices_from_two_ports",
        design("soc", [instance("m", "nibbles"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            component(
                "nibbles",
                [wire("hi_o", "out", 4), wire("lo_o", "out", 4)],
                [
                    bus(
                        "b",
                        "initiator",
                        port_map("WORD", "hi_o", logical_range=(7, 4)),
                        port_map("WORD", "lo_o", logical_range=(3, 0)),
                    )
                ],
            ),
            seer(),
        ],
        source + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xA5)},
    )


def logical_slices_into_two_ports():
    source = """
    module nibble_seer (input logic [3:0] hi_i, input logic [3:0] lo_i, output logic [7:0] seen_o);
        assign seen_o = {lo_i, hi_i};
    endmodule
    """
    return Scenario(
        "logical_slices_into_two_ports",
        design("soc", [instance("m", "sender"), instance("s", "nibble_seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            sender(),
            component(
                "nibble_seer",
                [wire("hi_i", "in", 4), wire("lo_i", "in", 4), wire("seen_o", "out", 8)],
                [bus("b", "target", port_map("WORD", "hi_i", logical_range=(7, 4)), port_map("WORD", "lo_i", logical_range=(3, 0)))],
            ),
        ],
        SEND_WORD + source,
        "soc",
        expect={"s_seen_o": (8, 0x5A)},
    )


def logical_slices_inverted():
    source = """
    module nibble_seer (input logic [3:0] hi_i, input logic [3:0] lo_i, output logic [7:0] seen_o);
        assign seen_o = {hi_i, lo_i};
    endmodule
    """
    return Scenario(
        "logical_slices_inverted",
        design("soc", [instance("m", "sender"), instance("s", "nibble_seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            sender(),
            component(
                "nibble_seer",
                [wire("hi_i", "in", 4), wire("lo_i", "in", 4), wire("seen_o", "out", 8)],
                [
                    bus(
                        "b",
                        "target",
                        port_map("WORD", "hi_i", logical_range=(7, 4), invert=True),
                        port_map("WORD", "lo_i", logical_range=(3, 0)),
                    )
                ],
            ),
        ],
        SEND_WORD + source,
        "soc",
        expect={"s_seen_o": (8, 0x55)},
    )


def master_to_multiple_slaves():
    return Scenario(
        "master_to_multiple_slaves",
        design(
            "soc",
            [instance("m", "sender"), instance("s1", "seer"), instance("s2", "seer")],
            [interconnection("link", ("m", "b"), ("s1", "b"), ("s2", "b"))],
        ),
        [sender(), seer()],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s1_seen_o": (8, 0xA5), "s2_seen_o": (8, 0xA5)},
    )


def inverted_between_instances():
    return Scenario(
        "inverted_between_instances",
        design("soc", [instance("m", "sender"), instance("s", "low_seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            sender(),
            component(
                "low_seer",
                [wire("data_i", "in", 8), wire("seen_o", "out", 8)],
                [bus("b", "target", port_map("WORD", "data_i", invert=True))],
                module="seer",
            ),
        ],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0x5A)},
    )


TOP_REPLY = """
module replier (input logic [7:0] data_i, output logic [7:0] reply_o);
    assign reply_o = data_i + 8'd1;
endmodule
"""


def replier(*maps):
    return component(
        "replier",
        [wire("data_i", "in", 8), wire("reply_o", "out", 8)],
        [bus("b", "target", *(maps or (port_map("WORD", "data_i"), port_map("REPLY", "reply_o"))))],
    )


def bus_exposed_at_the_top():
    the_top = hierarchical(
        "chip",
        "chip_d",
        [wire("in_i", "in", 8), wire("out_o", "out", 8)],
        [bus("up", "target", port_map("WORD", "in_i"), port_map("REPLY", "out_o"))],
    )
    return Scenario(
        "bus_exposed_at_the_top",
        design("chip_d", [instance("s", "replier")], [interconnection("link", ("s", "b"), hier=["up"])]),
        [the_top, replier()],
        TOP_REPLY,
        "chip",
        drive={"in_i": (8, 0x41)},
        expect={"out_o": (8, 0x42)},
    )


def bus_at_the_top_sliced_on_the_instance():
    source = """
    module replier (input logic [7:0] data_i, output logic [15:0] reply_o);
        assign reply_o = {data_i + 8'd1, 8'h00};
    endmodule
    """
    the_top = hierarchical(
        "chip",
        "chip_d",
        [wire("in_i", "in", 8), wire("out_o", "out", 8)],
        [bus("up", "target", port_map("WORD", "in_i"), port_map("REPLY", "out_o"))],
    )
    return Scenario(
        "bus_at_the_top_sliced_on_the_instance",
        design("chip_d", [instance("s", "replier")], [interconnection("link", ("s", "b"), hier=["up"])]),
        [
            the_top,
            component(
                "replier",
                [wire("data_i", "in", 8), wire("reply_o", "out", 16)],
                [bus("b", "target", port_map("WORD", "data_i"), port_map("REPLY", "reply_o", select=(15, 8)))],
            ),
        ],
        source,
        "chip",
        drive={"in_i": (8, 0x41)},
        expect={"out_o": (8, 0x42)},
    )


def bus_at_the_top_sliced_on_the_top():
    the_top = hierarchical(
        "chip",
        "chip_d",
        [wire("bus_i", "in", 16), wire("bus_o", "out", 16)],
        [
            bus(
                "up",
                "target",
                port_map("WORD", "bus_i", select=(11, 4)),
                port_map("REPLY", "bus_o", select=(15, 8)),
                port_map("HIGH", "bus_i", select=(7, 0)),
            )
        ],
    )
    tied = ad_hoc("low_half", external=[("bus_o", (7, 0))], tied=0x3C)
    return Scenario(
        "bus_at_the_top_sliced_on_the_top",
        design("chip_d", [instance("s", "replier")], [interconnection("link", ("s", "b"), hier=["up"])], [tied]),
        [the_top, replier()],
        TOP_REPLY,
        "chip",
        drive={"bus_i": (16, 0x4100)},
        expect={"bus_o": (16, 0x113C)},
    )


def inverted_at_the_top():
    the_top = hierarchical(
        "chip",
        "chip_d",
        [wire("in_i", "in", 8), wire("out_o", "out", 8)],
        [bus("up", "target", port_map("WORD", "in_i", invert=True), port_map("REPLY", "out_o", invert=True))],
    )
    return Scenario(
        "inverted_at_the_top",
        design("chip_d", [instance("s", "replier")], [interconnection("link", ("s", "b"), hier=["up"])]),
        [the_top, replier()],
        TOP_REPLY,
        "chip",
        drive={"in_i": (8, 0x41)},
        expect={"out_o": (8, 0x40)},  # ~0x41 is 0xbe, the IP replies 0xbf, and ~0xbf is 0x40
    )


def ad_hoc_to_top_level_ports():
    the_top = hierarchical("chip", "chip_d", [wire("in_i", "in", 8), wire("out_o", "out", 8)])
    return Scenario(
        "ad_hoc_to_top_level_ports",
        design(
            "chip_d",
            [instance("s", "replier")],
            ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["in_i"]), ad_hoc("b", ("s", "reply_o"), external=["out_o"])],
        ),
        [the_top, replier()],
        TOP_REPLY,
        "chip",
        drive={"in_i": (8, 0x41)},
        expect={"out_o": (8, 0x42)},
    )


def ad_hoc_part_selects_at_the_top():
    the_top = hierarchical("chip", "chip_d", [wire("bus_i", "in", 16), wire("out_o", "out", 8)])
    source = """
    module adder (input logic [7:0] a_i, input logic [7:0] b_i, output logic [7:0] sum_o);
        assign sum_o = a_i + b_i;
    endmodule
    """
    return Scenario(
        "ad_hoc_part_selects_at_the_top",
        design(
            "chip_d",
            [instance("s", "adder")],
            ad_hocs=[
                ad_hoc("a", ("s", "a_i"), external=[("bus_i", (7, 0))]),
                ad_hoc("b", ("s", "b_i"), external=[("bus_i", (15, 8))]),
                ad_hoc("c", ("s", "sum_o"), external=["out_o"]),
            ],
        ),
        [the_top, component("adder", [wire("a_i", "in", 8), wire("b_i", "in", 8), wire("sum_o", "out", 8)])],
        source,
        "chip",
        drive={"bus_i": (16, 0x0102)},
        expect={"out_o": (8, 0x03)},
    )


def tied_values_at_the_top():
    the_top = hierarchical("chip", "chip_d", [wire("low_o", "out", 8), wire("in_i", "in", 8), wire("out_o", "out", 8)])
    return Scenario(
        "tied_values_at_the_top",
        design(
            "chip_d",
            [instance("s", "replier")],
            ad_hocs=[
                ad_hoc("tie", external=["low_o"], tied=0x5A),
                ad_hoc("a", ("s", "data_i"), external=["in_i"]),
                ad_hoc("b", ("s", "reply_o"), external=["out_o"]),
            ],
        ),
        [the_top, replier()],
        TOP_REPLY,
        "chip",
        drive={"in_i": (8, 0x10)},
        expect={"low_o": (8, 0x5A), "out_o": (8, 0x11)},
    )


def ad_hoc_part_select_between_instances():
    source = """
    module wide (output logic [15:0] data_o);
        assign data_o = 16'hbeef;
    endmodule
    """
    return Scenario(
        "ad_hoc_part_select_between_instances",
        design(
            "soc",
            [instance("m", "wide"), instance("s", "seer")],
            ad_hocs=[ad_hoc("a", ("m", "data_o", (15, 8)), ("s", "data_i"))],
        ),
        [component("wide", [wire("data_o", "out", 16)]), seer()],
        source + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xBE)},
    )


def ad_hoc_on_a_port_of_a_bus():
    return Scenario(
        "ad_hoc_on_a_port_of_a_bus",
        design(
            "soc",
            [instance("m", "sender"), instance("s1", "seer"), instance("s2", "seer")],
            [interconnection("link", ("m", "b"), ("s1", "b"))],
            [ad_hoc("also", ("m", "data_o"), ("s2", "data_i"))],
        ),
        [sender(), seer()],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s1_seen_o": (8, 0xA5), "s2_seen_o": (8, 0xA5)},
    )


def ad_hoc_tie_off_on_an_instance():
    return Scenario(
        "ad_hoc_tie_off_on_an_instance",
        design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("tie", ("s", "data_i"), tied="'h3c")]),
        [seer()],
        SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0x3C)},
    )


PADS = """
module driver_pad (inout wire [7:0] pad);
    assign pad = 8'h5a;
endmodule
module seeing_pad (inout wire [7:0] pad, output logic [7:0] seen_o);
    assign seen_o = pad;
endmodule
"""


def pads():
    return [
        component("driver_pad", [wire("pad", "inout", 8)], [bus("b", "initiator", port_map("PAD", "pad"))]),
        component("seeing_pad", [wire("pad", "inout", 8), wire("seen_o", "out", 8)], [bus("b", "target", port_map("PAD", "pad"))]),
    ]


def ad_hoc_inout_between_instances():
    return Scenario(
        "ad_hoc_inout_between_instances",
        design(
            "soc",
            [instance("d", "driver_pad"), instance("s", "seeing_pad")],
            ad_hocs=[ad_hoc("pads", ("d", "pad"), ("s", "pad"))],
        ),
        pads(),
        PADS,
        "soc",
        expect={"s_seen_o": (8, 0x5A)},
    )


def bus_inout_between_instances():
    return Scenario(
        "bus_inout_between_instances",
        design("soc", [instance("d", "driver_pad"), instance("s", "seeing_pad")], [interconnection("link", ("d", "b"), ("s", "b"))]),
        pads(),
        PADS,
        "soc",
        expect={"s_seen_o": (8, 0x5A)},
    )


def top_inout_shared_by_several_ports():
    the_top = hierarchical("chip", "chip_d", [wire("pad", "inout", 8), wire("a_o", "out", 8), wire("b_o", "out", 8)])
    return Scenario(
        "top_inout_shared_by_several_ports",
        design(
            "chip_d",
            [instance("s1", "seeing_pad"), instance("s2", "seeing_pad")],
            ad_hocs=[
                ad_hoc("pads", ("s1", "pad"), ("s2", "pad"), external=["pad"]),
                ad_hoc("a", ("s1", "seen_o"), external=["a_o"]),
                ad_hoc("b", ("s2", "seen_o"), external=["b_o"]),
            ],
        ),
        [the_top, *pads()],
        PADS,
        "chip",
        drive={"pad": (8, 0x77)},
        expect={"a_o": (8, 0x77), "b_o": (8, 0x77)},
    )


def transactional_ports_are_left_out():
    return Scenario(
        "transactional_ports_are_left_out",
        design("soc", [instance("m", "sender"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))]),
        [
            component("sender", [wire("data_o", "out", 8), tlm("tlm_port")], [bus("b", "initiator", port_map("WORD", "data_o"))], module_parameters=[]),
            seer(),
        ],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xA5)},
    )


def tied_default_on_a_part_select():
    return Scenario(
        "tied_default_on_a_part_select",
        design(
            "soc",
            [instance("m", "plain_sender"), instance("s", "seer")],
            ad_hocs=[ad_hoc("hi", ("s", "data_i", (7, 4)), tied="default"), ad_hoc("lo", ("m", "data_o", (3, 0)), ("s", "data_i", (3, 0)))],
        ),
        [
            component("plain_sender", [wire("data_o", "out", 8)], module="sender"),
            component("seer", [wire("data_i", "in", 8, default="'hc3"), wire("seen_o", "out", 8)]),
        ],
        SEND_WORD + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xC5)},
    )


def flagging_tier(*maps):
    return component("tier", [wire("flag_o", "out")], [bus("b", "initiator", *maps)])


FLAG_TIER = "module tier (output logic flag_o); assign flag_o = 1'b1; endmodule\n"


def tie_offs_for_logical_slices():
    return Scenario(
        "tie_offs_for_logical_slices",
        design("soc", [instance("m", "tier"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))]),
        [flagging_tier(port_map("WORD", tie="'ha", logical_range=(7, 4)), port_map("WORD", tie="'h5", logical_range=(3, 0))), seer()],
        FLAG_TIER + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xA5)},
    )


def tie_off_next_to_a_driver():
    half = component(
        "half",
        [wire("lo_o", "out", 4)],
        [bus("b", "initiator", port_map("WORD", "lo_o", logical_range=(3, 0)), port_map("WORD", tie="'ha", logical_range=(7, 4)))],
    )
    return Scenario(
        "tie_off_next_to_a_driver",
        design("soc", [instance("m", "half"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))]),
        [half, seer()],
        "module half (output logic [3:0] lo_o); assign lo_o = 4'h5; endmodule\n" + SEE_WORD,
        "soc",
        expect={"s_seen_o": (8, 0xA5)},
    )


def inout_tied_to_a_constant():
    return Scenario(
        "inout_tied_to_a_constant",
        design("soc", [instance("s", "seeing_pad")], ad_hocs=[ad_hoc("t", ("s", "pad"), tied="'h5a")]),
        [component("seeing_pad", [wire("pad", "inout", 8), wire("seen_o", "out", 8)])],
        "module seeing_pad (inout wire [7:0] pad, output logic [7:0] seen_o); assign seen_o = pad; endmodule\n",
        "soc",
        expect={"s_seen_o": (8, 0x5A)},
    )


def module_parameter_not_the_hdl_default():
    return Scenario(
        "module_parameter_not_the_hdl_default",
        design("soc", [instance("s", "sized")]),
        [component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 16})],
        "module sized #(parameter int WIDTH = 8) (output logic [WIDTH-1:0] data_o); assign data_o = '1; endmodule\n",
        "soc",
        expect={"s_data_o": (16, 0xFFFF)},
    )


def ports_typed_int():
    return Scenario(
        "ports_typed_int",
        design("soc", [instance("a", "big"), instance("b", "bigseer")], ad_hocs=[ad_hoc("c", ("a", "data_o"), ("b", "data_i"))]),
        [
            component("big", [wire("data_o", "out", type_name="int")]),
            component("bigseer", [wire("data_i", "in", type_name="int"), wire("seen_o", "out", 32)]),
        ],
        """
        module big (output int data_o); assign data_o = 32'hdeadbeef; endmodule
        module bigseer (input int data_i, output logic [31:0] seen_o); assign seen_o = data_i; endmodule
        """,
        "soc",
        expect={"b_seen_o": (32, 0xDEADBEEF)},
    )


def part_select_with_a_design_parameter():
    import ipxact

    low = ipxact.Parameter(name="LOW", value=ipxact.Expression("4"), parameter_id="design_low", resolve="user")
    return Scenario(
        "part_select_with_a_design_parameter",
        design(
            "soc",
            [instance("m", "plain_sender"), instance("n", "nib")],
            ad_hocs=[ad_hoc("c", ("m", "data_o", ("design_low+3", "design_low")), ("n", "x_i"))],
            parameters=[low],
        ),
        [component("plain_sender", [wire("data_o", "out", 8)], module="sender"), component("nib", [wire("x_i", "in", 4), wire("seen_o", "out", 4)])],
        SEND_WORD + "module nib (input logic [3:0] x_i, output logic [3:0] seen_o); assign seen_o = x_i; endmodule\n",
        "soc",
        expect={"n_seen_o": (4, 0xA)},
    )


PORT_DEFAULT = 0x3C
ABSTRACTION_DEFAULT = 0x77  # of the logical port TAGGED; WORD has none
NO_DEFAULT_APPLIES = None


def default_value_rule(name, own_default, logical_port, connected, tied, applies):
    """A row of IEEE 1685-2022 Table C.2, or the text of 7.5.5, for a receiver's input `data_i`.

    Its bus interface, if any, maps it to `logical_port`. The far side of the bus doesn't map that
    logical port, so nothing drives it. When no default applies, the input stays unconnected, so it
    becomes a port of the module, and the simulation drives that to prove no constant is in the way.
    """
    receiver = component(
        "seer",
        [wire("data_i", "in", 8, default=f"'h{PORT_DEFAULT:x}" if own_default else None), wire("seen_o", "out", 8)],
        [bus("b", "target", port_map(logical_port, "data_i"))] if logical_port else [],
    )
    far_side = component("flagger", [wire("flag_o", "out")], [bus("b", "initiator", port_map("FLAG", "flag_o"))])
    driven = 0x5A
    return Scenario(
        f"default_{name}",
        design(
            "soc",
            [instance("s", "seer"), *([instance("f", "flagger")] if connected else [])],
            [interconnection("c", ("f", "b"), ("s", "b"))] if connected else [],
            [ad_hoc("tie", ("s", "data_i"), tied="default")] if tied else [],
        ),
        [receiver, far_side],
        SEE_WORD + "module flagger (output logic flag_o); assign flag_o = 1'b1; endmodule\n",
        "soc",
        drive={"s_data_i": (8, driven)} if applies is NO_DEFAULT_APPLIES else {},
        expect={"s_seen_o": (8, driven if applies is NO_DEFAULT_APPLIES else applies)},
    )


DEFAULT_VALUE_RULES = [
    default_value_rule("row2_tied", True, None, False, True, PORT_DEFAULT),
    default_value_rule("row2_tied_on_a_connected_bus", True, "WORD", True, True, PORT_DEFAULT),
    default_value_rule("row3_abstraction", False, "TAGGED", True, False, ABSTRACTION_DEFAULT),
    default_value_rule("row3_abstraction_also_tied", False, "TAGGED", True, True, ABSTRACTION_DEFAULT),
    default_value_rule("row4_no_connection", True, "TAGGED", False, False, NO_DEFAULT_APPLIES),
    default_value_rule("row5_tied_with_no_connection", True, "TAGGED", False, True, PORT_DEFAULT),
    default_value_rule("row6_abstraction_over_port", True, "TAGGED", True, False, ABSTRACTION_DEFAULT),
    default_value_rule("row7_tied_port_over_abstraction", True, "TAGGED", True, True, PORT_DEFAULT),
    default_value_rule("not_in_a_bus_and_not_tied", True, None, False, False, NO_DEFAULT_APPLIES),
    default_value_rule("on_a_bus_with_no_default_and_not_tied", True, "WORD", True, False, NO_DEFAULT_APPLIES),
]


def parameter_override_from_a_design_parameter():
    import ipxact

    width = ipxact.Parameter(name="W", value=ipxact.Expression("6 + 10"), parameter_id="design_w", resolve="user")
    source = """
    module sized #(parameter int WIDTH = 8) (output logic [WIDTH-1:0] data_o);
        assign data_o = '1;
    endmodule
    """
    return Scenario(
        "parameter_override_from_a_design_parameter",
        design("soc", [instance("s", "sized", WIDTH="design_w")], parameters=[width]),
        [component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})],
        source,
        "soc",
        expect={"s_data_o": (16, 0xFFFF)},
    )


def sub_design_in_its_own_module():
    inner = hierarchical("inner", "inner_d", [wire("in_i", "in", 8), wire("out_o", "out", 8)])
    inner_design = design(
        "inner_d",
        [instance("s", "replier")],
        ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["in_i"]), ad_hoc("b", ("s", "reply_o"), external=["out_o"])],
    )
    the_top = hierarchical("chip", "chip_d", [wire("in_i", "in", 8), wire("out_o", "out", 8)])
    return Scenario(
        "sub_design_in_its_own_module",
        design(
            "chip_d",
            [instance("first", "inner"), instance("second", "inner")],
            ad_hocs=[
                ad_hoc("a", ("first", "in_i"), external=["in_i"]),
                ad_hoc("b", ("first", "out_o"), ("second", "in_i")),
                ad_hoc("c", ("second", "out_o"), external=["out_o"]),
            ],
        ),
        [the_top, inner, inner_design, replier()],
        TOP_REPLY,
        "chip",
        drive={"in_i": (8, 0x10)},
        expect={"out_o": (8, 0x12)},
    )


def common_port_on_a_top_component():
    source = """
    module clocked (input logic clk_i, output logic seen_o);
        assign seen_o = clk_i;
    endmodule
    """
    the_top = hierarchical("chip", "chip_d", [wire("clk_i", "in"), wire("a_o", "out"), wire("b_o", "out")])
    scenario = Scenario(
        "common_port_on_a_top_component",
        design(
            "chip_d",
            [instance("s1", "clocked"), instance("s2", "clocked")],
            ad_hocs=[ad_hoc("a", ("s1", "seen_o"), external=["a_o"]), ad_hoc("b", ("s2", "seen_o"), external=["b_o"])],
        ),
        [the_top, component("clocked", [wire("clk_i", "in"), wire("seen_o", "out")])],
        source,
        "chip",
        drive={"clk_i": (1, 1)},
        expect={"a_o": (1, 1), "b_o": (1, 1)},
        common=["clk_i"],
    )
    return scenario


SCENARIOS = [
    plain_master_to_slave(),
    one_bit_slicing(),
    physically_sliced_master_to_slave(),
    port_sliced_across_logical_ports(),
    logical_slices_from_two_ports(),
    logical_slices_into_two_ports(),
    logical_slices_inverted(),
    master_to_multiple_slaves(),
    inverted_between_instances(),
    bus_exposed_at_the_top(),
    bus_at_the_top_sliced_on_the_instance(),
    bus_at_the_top_sliced_on_the_top(),
    inverted_at_the_top(),
    ad_hoc_to_top_level_ports(),
    ad_hoc_part_selects_at_the_top(),
    tied_values_at_the_top(),
    ad_hoc_part_select_between_instances(),
    ad_hoc_on_a_port_of_a_bus(),
    ad_hoc_tie_off_on_an_instance(),
    ad_hoc_inout_between_instances(),
    bus_inout_between_instances(),
    top_inout_shared_by_several_ports(),
    transactional_ports_are_left_out(),
    *DEFAULT_VALUE_RULES,
    tied_default_on_a_part_select(),
    tie_offs_for_logical_slices(),
    tie_off_next_to_a_driver(),
    inout_tied_to_a_constant(),
    module_parameter_not_the_hdl_default(),
    ports_typed_int(),
    part_select_with_a_design_parameter(),
    parameter_override_from_a_design_parameter(),
    sub_design_in_its_own_module(),
    common_port_on_a_top_component(),
]

by_name = pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])


@by_name
def test_compiles_with_slang(scenario):
    assert slang_errors(*[text for _, text in sorted(scenario.files().items())], scenario.source) == []


@by_name
@pytest.mark.skipif(not has_verilator, reason="verilator not installed")
def test_verilator_finds_no_errors(scenario, tmp_path):
    result = verilator_lint(tmp_path, scenario.files(), [scenario.source], scenario.module)
    assert result.returncode == 0, result.stderr


@by_name
@pytest.mark.skipif(not has_icarus, reason="iverilog not installed")
def test_does_what_the_design_says_in_simulation(scenario, tmp_path):
    output = simulate(tmp_path, scenario.files(), [scenario.source], scenario.tb())
    assert "PASS" in output, output


@by_name
@pytest.mark.skipif(not has_icarus, reason="iverilog not installed")
def test_simulation_notices_every_assign_being_broken(scenario, tmp_path):
    files = scenario.files()
    for name, text in files.items():
        for match in re.finditer(r"assign (\S+)\s*= [^;]+;", text):
            broken = dict(files)
            # all ones or zeros, whichever the scenario's expected value is not
            for value in ("'0", "'1"):
                broken[name] = text[: match.start()] + f"assign {match.group(1)} = {value};" + text[match.end() :]
                if "PASS" not in simulate(tmp_path, broken, [scenario.source], scenario.tb()):
                    break
            else:
                pytest.fail(f"nothing noticed when this was broken: {match.group(0)}")
