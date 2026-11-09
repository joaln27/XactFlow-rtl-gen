"""Designs written to make the exporter generate wrong RTL without an error.

Each is valid IP-XACT, and each once got through. Where the exporter can generate the design, the
simulation has to show it does what the design says. Where it can't yet, it has to say so.
"""

import re

import ipxact
import pytest
from xactflow import link

from builders import testbench as bench
from builders import (
    VENDOR,
    ad_hoc,
    bounds,
    bus,
    bus_documents,
    component,
    design,
    has_icarus,
    has_verilator,
    icarus_runs_elaboration_tasks_in_generate_blocks,
    hierarchical,
    instance,
    interconnection,
    library_of,
    logical,
    port_map,
    simulate,
    slang_errors,
    struct,
    verilator_lint,
    vlnv_ref,
    wire,
)
from xactflow_rtl_gen import RtlExporter, generate_files

_E = ipxact.Expression

SEND = "module sender #(parameter logic [7:0] VALUE = 8'ha5) (output logic [7:0] data_o); assign data_o = VALUE; endmodule\n"
SEE = "module seer (input logic [7:0] data_i, output logic [7:0] seen_o); assign seen_o = data_i; endmodule\n"
NIBBLE = "module nib (input logic [3:0] x_i, output logic [3:0] seen_o); assign seen_o = x_i; endmodule\n"
PADS = """
module driver_pad (inout wire [7:0] pad); assign pad = 8'h5a; endmodule
module seeing_pad (inout wire [7:0] pad, output logic [7:0] seen_o); assign seen_o = pad; endmodule
"""
STRUCT_PACKAGES = """
package req_pkg; typedef struct packed { logic [3:0] hi; logic [3:0] lo; } req_t; endpackage
package other_pkg; typedef struct packed { logic [3:0] hi; logic [3:0] lo; } req_t; endpackage
"""
STRUCTS = """
module src (output req_pkg::req_t req_o); assign req_o = 8'ha5; endmodule
module dst (input req_pkg::req_t req_i, output logic [7:0] seen_o); assign seen_o = req_i; endmodule
"""


def sender():
    return component("sender", [wire("data_o", "out", 8)], [bus("b", "initiator", port_map("WORD", "data_o"))])


def seer(*maps, default=None, drivers=None):
    data = wire("data_i", "in", 8, default=default, drivers=drivers)
    return component("seer", [data, wire("seen_o", "out", 8)], [bus("b", "target", *(maps or [port_map("WORD", "data_i")]))])


def nib(*maps):
    return component("nib", [wire("x_i", "in", 4), wire("seen_o", "out", 4)], [bus("b", "target", *maps)] if maps else [])


def driver_pad(*maps):
    return component("driver_pad", [wire("pad", "inout", 8)], [bus("b", "initiator", *(maps or [port_map("PAD", "pad")]))])


def seeing_pad(*maps):
    return component(
        "seeing_pad", [wire("pad", "inout", 8), wire("seen_o", "out", 8)], [bus("b", "target", *(maps or [port_map("PAD", "pad")]))]
    )


def far_side():
    """The other end of a bus, which maps none of the logical ports these tests are about."""
    return component("far_side", [wire("flag_o", "out")], [bus("b", "initiator", port_map("FLAG", "flag_o"))])


FAR_SIDE = "module far_side (output logic flag_o); assign flag_o = 1'b1; endmodule\n"


def files_of(the_design, documents, configuration=None, buses=None, **options):
    library = library_of([*(bus_documents() if buses is None else buses), *documents, the_design])
    return generate_files(link(the_design, library, configuration), **options)


def passes(tmp_path, the_design, documents, source, module="soc", drive=None, expect=None, configuration=None, buses=None, packages="", lint=True):
    """Compiles without complaint in all three tools, and passes a simulation. `packages` are compiled first."""
    files = files_of(the_design, documents, configuration, buses)
    tools_see = {**files, "ip_types_pkg.sv": packages} if packages else files
    ordered = [text for _, text in sorted(tools_see.items(), key=lambda item: not item[0].endswith("_pkg.sv"))]
    assert slang_errors(*ordered, source) == []
    if has_verilator and lint:
        result = verilator_lint(tmp_path, tools_see, [source], module)
        assert result.returncode == 0, result.stderr
    if has_icarus:
        assert "PASS" in simulate(tmp_path, tools_see, [source], bench(module, drive or {}, expect or {}))
    return files


def refused(match, the_design, documents, configuration=None, buses=None, **options):
    with pytest.raises(ValueError, match=match):
        files_of(the_design, documents, configuration, buses, **options)


def bus_of(name, instances, **options):
    return design(name, instances, **options)


def alternate_view(a_component, name="alt", instantiation="rtl_inst"):
    a_component.model.views.append(ipxact.View(name=name, component_instantiation_ref=instantiation))
    return a_component


def choose_view(view, instance_name, design_name=None, values=None):
    return ipxact.DesignConfiguration(
        vlnv=ipxact.VLNV(VENDOR, "design", "cfg", "1.0"),
        design_ref=vlnv_ref("design", design_name) if design_name else None,
        view_configurations=[ipxact.ViewConfiguration(instance_name=instance_name, view_ref=view, configurable_element_values=values or {})],
    )


# --- the checks themselves


def test_slang_check_sees_what_pyslang_does_not_call_an_error():
    assert slang_errors("module m; logic a; logic a; endmodule\n")
    assert slang_errors("module m; logic [3:0] b; assign b[0:3] = 0; endmodule\n")
    assert slang_errors("module n (input logic [3:0] i); endmodule\nmodule m; logic [7:0] w; n x (.i(w)); endmodule\n")


# --- views and configurations


def two_maps():
    wide = component("wide", [wire("a_o", "out", 8), wire("b_o", "out", 8)])
    alternate_view(wide)
    wide.bus_interfaces = [
        ipxact.BusInterface(
            name="b",
            bus_type=vlnv_ref("bus", "lbus"),
            mode=ipxact.InterfaceMode("initiator"),
            abstraction_types=[
                ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "lbus_rtl"), port_maps=[port_map("WORD", "a_o")], view_refs=[ipxact.TextValue("rtl")]),
                ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "lbus_rtl"), port_maps=[port_map("WORD", "b_o")], view_refs=[ipxact.TextValue("alt")]),
            ],
        )
    ]
    return wide


TWO_MAPS = "module wide (output logic [7:0] a_o, output logic [7:0] b_o); assign a_o = 8'h11; assign b_o = 8'h22; endmodule\n" + SEE


def test_an_instance_uses_the_port_maps_of_its_view(tmp_path):
    soc = design("soc", [instance("m", "wide"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [two_maps(), seer()], TWO_MAPS, expect={"s_seen_o": (8, 0x11)})
    passes(tmp_path, soc, [two_maps(), seer()], TWO_MAPS, expect={"s_seen_o": (8, 0x22)}, configuration=choose_view("alt", "m"))


def fast_or_slow():
    both = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    alternate_view(both, "fast", "fast_inst")
    both.model.component_instantiations.append(ipxact.ComponentInstantiation(name="fast_inst", module_name="seer_fast"))
    return both


FAST_OR_SLOW = SEE + "module seer_fast (input logic [7:0] data_i, output logic [7:0] seen_o); assign seen_o = data_i + 8'd1; endmodule\n"


def test_a_configuration_the_top_component_holds_applies_at_the_top(tmp_path):
    chip = ipxact.Component(
        vlnv=ipxact.VLNV(VENDOR, "ip", "chip", "1.0"),
        model=ipxact.Model(
            views=[ipxact.View(name="structural", design_configuration_instantiation_ref="dc")],
            design_configuration_instantiations=[
                ipxact.DesignConfigurationInstantiation(name="dc", design_configuration_ref=vlnv_ref("design", "cfg"))
            ],
            ports=[wire("i", "in", 8), wire("o", "out", 8)],
        ),
    )
    chip_d = design("chip_d", [instance("s", "seer")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["i"]), ad_hoc("b", ("s", "seen_o"), external=["o"])])
    documents = [chip, fast_or_slow(), choose_view("fast", "s", "chip_d")]
    passes(tmp_path, chip_d, documents, FAST_OR_SLOW, module="chip", drive={"i": (8, 1)}, expect={"o": (8, 2)})


def test_a_value_in_a_configuration_sees_its_own_parameters(tmp_path):
    configuration = choose_view("rtl", "s", values={"WIDTH": _E("CW")})
    configuration.parameters = [design_parameter("CW", "16")]
    the_component = component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})
    passes(tmp_path, design("soc", [instance("s", "sized")]), [the_component], SIZED, expect={"s_data_o": (16, 0xFFFF)}, configuration=configuration)


def test_a_configuration_for_another_design_is_refused():
    refused("is for design .*other_d", design("soc", [instance("s", "seer")]), [plain_seer()], configuration=choose_view("rtl", "s", "other_d"))


def test_a_configuration_naming_no_instance_is_refused():
    refused("configures 'typo', which is not an instance", design("soc", [instance("s", "seer")]), [plain_seer()], configuration=choose_view("rtl", "typo"))


def test_a_configured_view_that_instantiates_nothing_is_refused():
    the_component = plain_seer()
    the_component.model.views.append(ipxact.View(name="empty"))
    refused("selects view 'empty', which instantiates nothing", design("soc", [instance("s", "seer")]), [the_component], configuration=choose_view("empty", "s"))


def wrapped(**values):
    wrap = hierarchical("wrap", "wrap_d", [wire("o", "out", 8)])
    wrap.model.design_instantiations[0].design_ref = vlnv_ref("design", "wrap_d", **values)
    wrap_d = design("wrap_d", [instance("m", "plain_send")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    return wrap, wrap_d, component("plain_send", [wire("data_o", "out", 8)], module="sender")


def configured_too(holder, design_name):
    """The view of a hierarchical component names a design configuration next to its design, as Kactus2
    writes it. Returns the configuration, which chooses the view of instance m."""
    holder.model.views[0].design_configuration_instantiation_ref = "dc"
    holder.model.design_configuration_instantiations = [
        ipxact.DesignConfigurationInstantiation(name="dc", design_configuration_ref=vlnv_ref("design", "cfg"))
    ]
    return choose_view("rtl", "m", design_name)


def sized_holder(name, width_parameter, default, **design_values):
    """A hierarchical component with a parameter `width_parameter` and an output o of that width. Its design
    holds m, a `sized`, whose WIDTH is the design's parameter DW (8 by default). `design_values` are what the
    component's reference to its design sets, as {design parameter: expression in the component}."""
    holder = hierarchical(name, f"{name}_d", [wire("o", "out", f"{width_parameter}-1")])
    holder.parameters = [design_parameter(width_parameter, str(default))]
    holder.model.design_instantiations[0].design_ref = vlnv_ref("design", f"{name}_d", **design_values)
    inside = design(f"{name}_d", [instance("m", "sized", WIDTH="DW")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    inside.parameters = [design_parameter("DW", "8")]
    return holder, inside, component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})


# --- D25: the values a component's reference to its design gives the design's parameters


def test_the_top_components_reference_sets_the_designs_parameters(tmp_path):
    chip, chip_d, sized = sized_holder("chip", "CW", 16, DW="CW")
    passes(tmp_path, chip_d, [chip, sized], SIZED, module="chip", expect={"o": (16, 0xFFFF)})


def test_the_reference_sets_them_next_to_a_configuration_too(tmp_path):
    chip, chip_d, sized = sized_holder("chip", "CW", 16, DW="CW")
    passes(tmp_path, chip_d, [chip, sized, configured_too(chip, "chip_d")], SIZED, module="chip", expect={"o": (16, 0xFFFF)})


def test_a_sub_designs_reference_sets_its_parameters(tmp_path):
    wrap, wrap_d, sized = sized_holder("wrap", "W", 16, DW="W")
    files = passes(tmp_path, design("soc", [instance("w", "wrap")]), [wrap, wrap_d, sized], SIZED, expect={"w_o": (16, 0xFFFF)})
    assert ".WIDTH(16)" in files["wrap.sv"].replace(" ", "")


def test_without_the_reference_the_designs_own_value_stays(tmp_path):
    chip, chip_d, sized = sized_holder("chip", "CW", 8)
    passes(tmp_path, chip_d, [chip, sized], SIZED, module="chip", expect={"o": (8, 0xFF)})


def test_a_reference_setting_a_parameter_the_design_lacks_is_refused():
    refused("its reference to design .*wrap_d.* name no parameter of that design: SEL", design("soc", [instance("w", "wrap")]), wrapped(SEL=1))


def test_a_reference_value_naming_nothing_in_the_component_is_refused():
    chip, chip_d, sized = sized_holder("chip", "CW", 16, DW="NOPE")
    refused("design parameter DW", chip_d, [chip, sized])


def test_a_view_whose_design_and_configuration_disagree_is_refused():
    wrap, wrap_d, sending = wrapped()
    configuration = configured_too(wrap, "other_d")
    refused("names design .*wrap_d.* its design configuration is for .*other_d", design("soc", [instance("w", "wrap")]), [wrap, wrap_d, sending, configuration])


# --- D26: a hierarchical instance's values get a module of their own


def test_a_hierarchical_instances_values_reach_its_design(tmp_path):
    wrap, wrap_d, sized = sized_holder("wrap", "W", 8, DW="W")
    soc = design("soc", [instance("w", "wrap", W=16)])
    files = passes(tmp_path, soc, [wrap, wrap_d, sized], SIZED, expect={"w_o": (16, 0xFFFF)})
    assert list(files) == ["soc.sv", "wrap_1.sv"]
    assert re.search(r"^\s*wrap_1 w \(", files["soc.sv"], re.M)
    assert "// The version of test.org:ip:wrap:1.0 with W = 16." in files["wrap_1.sv"]


def test_one_module_for_each_set_of_values(tmp_path):
    wrap, wrap_d, sized = sized_holder("wrap", "W", 8, DW="W")
    soc = design("soc", [instance("a", "wrap", W=16), instance("b", "wrap", W=16), instance("c", "wrap"), instance("d", "wrap", W=8), instance("e", "wrap", W=4)])
    files = passes(tmp_path, soc, [wrap, wrap_d, sized], SIZED, expect={"a_o": (16, 0xFFFF), "b_o": (16, 0xFFFF), "c_o": (8, 0xFF), "d_o": (8, 0xFF), "e_o": (4, 0xF)})
    assert sorted(files) == ["soc.sv", "wrap.sv", "wrap_1.sv", "wrap_2.sv"]
    by_instance = {name: module for module, name in re.findall(r"^\s*(wrap\w*) (\w+) \(", files["soc.sv"], re.M)}
    assert by_instance == {"a": "wrap_1", "b": "wrap_1", "c": "wrap", "d": "wrap", "e": "wrap_2"}


def test_values_pass_down_more_than_one_level(tmp_path):
    wrap, wrap_d, sized = sized_holder("wrap", "W", 8, DW="W")
    outer = hierarchical("outer", "outer_d", [wire("o", "out", "OW-1")])
    outer.parameters = [design_parameter("OW", "8")]
    outer_d = design("outer_d", [instance("w", "wrap", W="OW")], ad_hocs=[ad_hoc("o", ("w", "o"), external=["o"])])
    soc = design("soc", [instance("x", "outer", OW=12)])
    files = passes(tmp_path, soc, [outer, outer_d, wrap, wrap_d, sized], SIZED, expect={"x_o": (12, 0xFFF)})
    assert sorted(files) == ["outer_1.sv", "soc.sv", "wrap_1.sv"]


def test_a_value_for_a_parameter_the_hierarchical_component_lacks_is_refused():
    wrap, wrap_d, sized = sized_holder("wrap", "W", 8, DW="W")
    refused("instance 'w': parameter overrides name no parameter of test.org:ip:wrap:1.0: NOPE", design("soc", [instance("w", "wrap", NOPE=1)]), [wrap, wrap_d, sized])


def test_a_version_named_like_another_module_is_refused():
    wrap, wrap_d, sized = sized_holder("wrap", "W", 8, DW="W")
    clash = component("wrap_1", [wire("data_o", "out", 8)])
    refused("wrap_1", design("soc", [instance("w", "wrap", W=16), instance("k", "wrap_1")]), [wrap, wrap_d, sized, clash])


def test_an_override_on_a_reference_to_an_abstraction_is_refused():
    receiving = seer()
    receiving.bus_interfaces[0].abstraction_types[0].abstraction_ref = vlnv_ref("bus", "lbus_rtl", D=9)
    soc = design("soc", [instance("m", "sender"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused("parameter overrides on its reference .* D", soc, [sender(), receiving])


def test_the_view_a_parent_selects_names_the_module_generated_for_it(tmp_path):
    wrap = hierarchical("wrap", "wrap_d", [wire("i", "in", 8), wire("o", "out", 8)])
    wrap.model.views = [
        ipxact.View(name="s1", design_instantiation_ref="d", component_instantiation_ref="a"),
        ipxact.View(name="s2", design_instantiation_ref="d", component_instantiation_ref="b"),
    ]
    wrap.model.component_instantiations = [
        ipxact.ComponentInstantiation(name="a", module_name="wrap_a"),
        ipxact.ComponentInstantiation(name="b", module_name="wrap_b"),
    ]
    wrap_d = design("wrap_d", [instance("s", "seer")], ad_hocs=[ad_hoc("x", ("s", "data_i"), external=["i"]), ad_hoc("y", ("s", "seen_o"), external=["o"])])
    chip = hierarchical("chip", "chip_d", [wire("i", "in", 8), wire("o", "out", 8)])
    chip_d = design("chip_d", [instance("w", "wrap")], ad_hocs=[ad_hoc("x", ("w", "i"), external=["i"]), ad_hoc("y", ("w", "o"), external=["o"])])
    documents = [chip, wrap, wrap_d, component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])]
    files = passes(tmp_path, chip_d, documents, SEE, module="chip", drive={"i": (8, 1)}, expect={"o": (8, 1)}, configuration=choose_view("s2", "w"))
    assert "wrap_b.sv" in files and "wrap_a.sv" not in files


# --- parameters


SIZED = "module sized #(parameter int WIDTH = 8) (output logic [WIDTH-1:0] data_o); assign data_o = '1; endmodule\n"


def test_a_module_parameter_is_written_even_when_no_override_changes_it(tmp_path):
    the_component = component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 16})
    files = passes(tmp_path, design("soc", [instance("s", "sized")]), [the_component], SIZED, expect={"s_data_o": (16, 0xFFFF)})
    assert ".WIDTH(16)" in files["soc.sv"]


def test_a_runtime_parameter_is_not_set_on_the_instance():
    the_component = component("runner", [wire("x_o", "out")])
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="SEED", value=_E("1"), usage_type="runtime")]
    assert "#(" not in files_of(design("soc", [instance("s", "runner")]), [the_component])["soc.sv"]


def moded(value):
    the_component = component("moded", [wire("fast_o", "out")], parameters={"DEPTH": 2}, module_parameters=[])
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="MODE", value=_E(value))]
    return the_component


MODED = 'module moded #(parameter string MODE = "SLOW") (output logic fast_o); assign fast_o = (MODE == "FAST"); endmodule\n'


def test_a_string_module_parameter_is_written_as_it_stands(tmp_path):
    soc = design("soc", [instance("s", "moded")])
    files = passes(tmp_path, soc, [moded('"FAST"')], MODED, expect={"s_fast_o": (1, 1)})
    assert '.MODE("FAST")' in files["soc.sv"]


def test_a_string_that_depends_on_a_parameter_is_left_to_the_compiler(tmp_path):
    soc = design("soc", [instance("s", "moded", DEPTH=8)])
    source = 'module moded #(parameter MODE = "SLOW") (output logic fast_o); assign fast_o = (MODE == "FAST"); endmodule\n'
    files = passes(tmp_path, soc, [moded('DEPTH > 4 ? "FAST" : "SLOW"')], source, expect={"s_fast_o": (1, 1)})
    assert '.MODE(8 > 4 ? "FAST" : "SLOW")' in files["soc.sv"]


UNTYPED = """
module plain_p #(parameter P = 8'h00) (output logic [15:0] twice_o, output logic [31:0] bits_o, output logic neg_o);
    assign twice_o = {P, P};
    assign bits_o = $bits(P);
    assign neg_o = P < 0;
endmodule
"""


def untyped_ip(value="8'h00"):
    return component("plain_p", [wire("twice_o", "out", 16), wire("bits_o", "out", 32), wire("neg_o", "out")], parameters={"P": value})


def test_a_sized_literal_keeps_its_width_in_an_untyped_parameter(tmp_path):
    expect = {"s_twice_o": (16, 0xA5A5), "s_bits_o": (32, 8), "s_neg_o": (1, 0)}
    passes(tmp_path, design("soc", [instance("s", "plain_p")]), [untyped_ip("8'hA5")], UNTYPED, expect=expect)
    passes(tmp_path, design("soc", [instance("s", "plain_p", P="8'hA5")]), [untyped_ip()], UNTYPED, expect=expect)
    soc = design("soc", [instance("s", "plain_p", P="design_p")], parameters=[design_parameter("design_p", "8'hA5")])
    passes(tmp_path, soc, [untyped_ip()], UNTYPED, expect=expect)


def test_a_negative_value_past_32_bits_stays_negative_in_an_untyped_parameter(tmp_path):
    source = "module big_neg #(parameter P = 0) (output logic neg_o); assign neg_o = P < 0; endmodule\n"
    the_component = component("big_neg", [wire("neg_o", "out")], parameters={"P": "-5000000000"})
    files = files_of(design("soc", [instance("s", "big_neg")]), [the_component])
    assert "-34'sd5000000000" in files["soc.sv"]
    if has_icarus:
        assert "PASS" in simulate(tmp_path, files, [source], bench("soc", {}, {"s_neg_o": (1, 1)}))


def test_an_expression_keeps_systemverilogs_own_meaning(tmp_path):
    source = "module signed_p #(parameter int P = 0) (output logic [31:0] q_o); assign q_o = P; endmodule\n"
    the_component = component("signed_p", [wire("q_o", "out", 32)], parameters={"P": "~8'hF0"})
    # Verilator only warns about the widening, which is what this is about.
    files = passes(tmp_path, design("soc", [instance("s", "signed_p")]), [the_component], source, expect={"s_q_o": (32, 0xFFFFFF0F)}, lint=False)
    assert ".P(~8'hF0)" in files["soc.sv"]


def test_a_value_using_parameters_is_worked_out_like_a_port_width():
    source = "module sum_p #(parameter int A = 1, parameter int B = 2) (output logic x_o); assign x_o = 0; endmodule\n"
    the_component = component("sum_p", [wire("x_o", "out")], parameters={"A": "3 + 4", "B": "A * 2"})
    files = files_of(design("soc", [instance("s", "sum_p")]), [the_component])
    assert ".A(3 + 4)" in files["soc.sv"]
    assert ".B(" not in files["soc.sv"]  # derived from A, which the HDL is given: the HDL works it out
    compiles(files, source)


def test_a_derived_parameter_is_worked_out_when_its_inputs_are_not_the_hdls():
    the_component = valued(design_parameter("A", "3 + 4"), value="A * 2")
    assert ".P(14)" in files_of(design("soc", [instance("s", "valued")]), [the_component])["soc.sv"]


def test_a_derived_parameter_is_written_when_it_is_overridden_itself(tmp_path):
    the_component = component("sum_p", [wire("x_o", "out")], parameters={"A": "3 + 4", "B": "A * 2"})
    assert ".B(5)" in files_of(design("soc", [instance("s", "sum_p", B=5)]), [the_component])["soc.sv"]


def test_a_parameter_keeps_its_type_when_another_value_uses_it(tmp_path):
    """Inline, 200000000*16 would overflow 32 bits. CLK_HZ is a longint where the HDL says so."""
    source = """
    module dividing #(parameter longint CLK_HZ = 200000000, parameter int BAUD = 115200, parameter int DIV = CLK_HZ * 16 / BAUD)
        (output logic [31:0] div_o);
        assign div_o = DIV;
    endmodule
    """
    the_component = component("dividing", [wire("div_o", "out", 32)], parameters={"CLK_HZ": 200000000, "BAUD": 115200, "DIV": "CLK_HZ * 16 / BAUD"})
    files = passes(tmp_path, design("soc", [instance("s", "dividing")]), [the_component], source, expect={"s_div_o": (32, 27777)}, lint=False)
    assert ".DIV(" not in files["soc.sv"]  # derived from the module's own parameters: left to the HDL


def typed_parameter(name, value, type_name, *vectors, sign=None):
    return ipxact.Parameter(
        name=name, value=_E(value), parameter_id=name, resolve="user", type=type_name, sign=sign,
        vectors=[ipxact.Vector(_E(str(left)), _E(str(right))) for left, right in vectors],
    )


def valued(*parameters, value="V"):
    the_component = component("valued", [wire("q_o", "out", 32)], module_parameters=[])
    the_component.parameters = list(parameters)
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="P", value=_E(value))]
    return the_component


VALUED = "module valued #(parameter int P = 0) (output logic [31:0] q_o); assign q_o = P; endmodule\n"


@pytest.mark.parametrize(
    "parameter, expected",
    [
        (typed_parameter("V", "20", "bit", (3, 0)), 4),  # cut down to its 4 bits
        (typed_parameter("V", "200", "byte"), 0xFFFFFFC8),  # -56
        (typed_parameter("V", "'1", "bit", (7, 0)), 0xFF),
    ],
)
def test_a_parameters_declared_type_changes_its_value(tmp_path, parameter, expected):
    passes(tmp_path, design("soc", [instance("s", "valued")]), [valued(parameter)], VALUED, expect={"s_q_o": (32, expected)}, lint=False)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("EN ? 8 : 16", 8),  # a based value next to a question mark would lex as one number
        ("8 - OFS", 9),  # 8--1
        ("-OFS", 1),  # --1
        ("CFG[3:0]", 5),  # 8'hA5[3:0], which is 4 bits
        ("small * 2", 6),  # a parameterId that is a keyword
    ],
)
def test_an_expression_using_parameters_comes_out_valid(tmp_path, value, expected):
    parameters = [design_parameter("EN", "1'b1"), design_parameter("OFS", "-1"), design_parameter("CFG", "8'hA5"), design_parameter("small", "3")]
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [valued(*parameters, value=value)], VALUED, expect={"s_q_o": (32, expected)}, lint=False)
    assert re.search(rf"\.P\((\d+'d)?{expected}\)", files["soc.sv"])


def test_another_parameters_expression_keeps_its_own_width(tmp_path):
    """MASK is ~8'h0F on its own, 8'hF0. Pasted into int P it would become 32 bits wide, and -16."""
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [valued(design_parameter("MASK", "~8'h0F"), value="MASK")], VALUED, expect={"s_q_o": (32, 0xF0)}, lint=False)
    assert ".P(8'd240)" in files["soc.sv"]


PLAIN_P = "module plain_p #(parameter W = 8, parameter P = {W{1'b1}}) (output logic [15:0] twice_o); assign twice_o = {P, P}; endmodule\n"


def test_a_worked_out_value_keeps_its_width_in_an_untyped_parameter(tmp_path):
    """Left to the HDL, P is 8 bits; written as 255 it would be 32, and {P, P} would change."""
    the_component = component("plain_p", [wire("twice_o", "out", 16)], parameters={"W": 8, "P": "{W{1'b1}}"})
    files = passes(tmp_path, design("soc", [instance("s", "plain_p")]), [the_component], PLAIN_P, expect={"s_twice_o": (16, 0xFFFF)})
    assert ".P(" not in files["soc.sv"]  # derived from W: the HDL works it out
    files = passes(tmp_path, design("soc", [instance("s", "plain_p", W=4)]), [the_component], PLAIN_P, expect={"s_twice_o": (16, 0xFF)}, lint=False)
    assert ".W(4)" in files["soc.sv"] and ".P(" not in files["soc.sv"]


def test_an_imported_derived_default_is_left_to_the_hdl():
    """B = A + 8'd100 is 44 in SystemVerilog, where the evaluator, not wrapping at 8 bits, says 300."""
    the_component = component("derived", [wire("x_o", "out")], parameters={"A": "8'd200", "B": "A + 8'd100"})
    text = files_of(design("soc", [instance("s", "derived")]), [the_component])["soc.sv"]
    assert ".A(8'd200)" in text and ".B(" not in text


def test_an_unsized_unsigned_value_stays_unsigned(tmp_path):
    source = "module offset #(parameter OFF = 0) (output logic neg_o); assign neg_o = OFF < 0; endmodule\n"
    the_component = component("offset", [wire("neg_o", "out")], parameters={"BASE": "'h80", "OFF": "BASE - 'h100"}, module_parameters=["OFF"])
    files = passes(tmp_path, design("soc", [instance("s", "offset")]), [the_component], source, expect={"s_neg_o": (1, 0)})
    assert ".OFF(32'd4294967168)" in files["soc.sv"]


def test_a_module_parameter_past_its_own_maximum_is_refused():
    the_component = valued(design_parameter("A", "200"), value="A")
    module_parameter = the_component.model.component_instantiations[0].module_parameters[0]
    module_parameter.type = "int"
    module_parameter.maximum = "100"
    refused("module parameter P", design("soc", [instance("s", "valued")]), [the_component])


def test_a_signed_minimum_stays_negative(tmp_path):
    """-8'sd128 would read as +128 once extended: the bits are what have to be written."""
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [valued(typed_parameter("V", "-128", "byte"))], VALUED, expect={"s_q_o": (32, 0xFFFFFF80)}, lint=False)
    assert ".P(8'sh80)" in files["soc.sv"]


def test_a_signed_minimum_survives_an_override_that_names_it(tmp_path):
    soc = design("soc", [instance("s", "valued", V="B")], parameters=[typed_parameter("B", "-128", "byte")])
    passes(tmp_path, soc, [valued(typed_parameter("V", "0", "int"))], VALUED, expect={"s_q_o": (32, 0xFFFFFF80)}, lint=False)


def test_a_signed_minimum_survives_the_top_components_scope(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("o", "out", 8)])
    chip.parameters = [typed_parameter("B", "-128", "byte"), design_parameter("M", "B")]
    wide = component("wide_seer", [wire("data_i", "in", 16), wire("seen_o", "out", 16)], module="wide_seer")
    chip_d = design("chip_d", [instance("s", "wide_seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="M"), ad_hoc("b", ("s", "seen_o", (7, 0)), external=["o"])])
    source = "module wide_seer (input logic [15:0] data_i, output logic [15:0] seen_o); assign seen_o = data_i; endmodule\n"
    passes(tmp_path, chip_d, [chip, wide], source, module="chip", expect={"o": (8, 0x80)}, lint=False)
    files = files_of(chip_d, [chip, wide])
    assert "16'hff80" in files["chip.sv"]


def test_an_unsigned_negation_is_written_as_its_bits(tmp_path):
    source = "module big_p #(parameter P = 0) (output logic q_o); assign q_o = P > 100; endmodule\n"
    the_component = component("big_p", [wire("q_o", "out")], parameters={"A": "8'd5", "P": "-A"}, module_parameters=["P"])
    files = passes(tmp_path, design("soc", [instance("s", "big_p")]), [the_component], source, expect={"s_q_o": (1, 1)})
    assert ".P(8'd251)" in files["soc.sv"]


def test_a_module_parameters_own_type_applies_to_its_value(tmp_path):
    source = "module ticks #(parameter longint P = 0) (output logic [63:0] p_o); assign p_o = P; endmodule\n"
    the_component = component("ticks", [wire("p_o", "out", 64)], module_parameters=[])
    the_component.parameters = [typed_parameter("A", "100000", "int"), typed_parameter("B", "100000", "int")]
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="P", value=_E("A*B"), type="longint")]
    files = passes(tmp_path, design("soc", [instance("s", "ticks")]), [the_component], source, expect={"s_p_o": (64, 10_000_000_000)}, lint=False)
    assert ".P(64'sd10000000000)" in files["soc.sv"]


def test_a_value_its_width_cannot_hold_is_left_unsized(tmp_path):
    the_component = valued(value="A + B")
    the_component.parameters = [design_parameter("A", "8'd200"), design_parameter("B", "8'd100")]
    the_component.model.component_instantiations[0].module_parameters[0].type = "int"
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [the_component], VALUED, expect={"s_q_o": (32, 300)}, lint=False)
    assert re.search(r"\.P\((32'sd)?300\)", files["soc.sv"])  # not 8'd300, which would be 44


def test_an_override_reaching_a_module_parameter_through_a_vector_counts(tmp_path):
    masked = typed_parameter("M", "255", "bit", ("W-1", 0))
    the_component = valued(masked, design_parameter("W", "8"), value="M")
    passes(tmp_path, design("soc", [instance("s", "valued", W=4)]), [the_component], VALUED, expect={"s_q_o": (32, 15)}, lint=False)


@pytest.mark.parametrize(
    "parameter, override, expected",
    [
        (typed_parameter("V", "'1", "bit", (7, 0)), "'1", 0xFF),
        (typed_parameter("V", "0", "bit", (3, 0)), "20", 4),
        (typed_parameter("V", "0", "byte"), "200", 0xFFFFFFC8),
    ],
)
def test_an_override_keeps_the_type_of_the_parameter_it_overrides(tmp_path, parameter, override, expected):
    soc = design("soc", [instance("s", "valued", V=override)])
    passes(tmp_path, soc, [valued(parameter)], VALUED, expect={"s_q_o": (32, expected)}, lint=False)


def test_an_overridden_typed_parameter_sizes_the_port_and_the_value_alike(tmp_path):
    source = "module sized #(parameter int W = 1) (output logic [W-1:0] q_o); assign q_o = '1; endmodule\n"
    the_component = component("sized", [wire("q_o", "out", "W-1")], module_parameters=[])
    the_component.parameters = [typed_parameter("W", "1", "bit", (3, 0))]
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="W", value=_E("W"))]
    files = passes(tmp_path, design("soc", [instance("s", "sized", W=20)]), [the_component], source, expect={"s_q_o": (4, 0xF)}, lint=False)
    assert "logic [3:0]" in files["soc.sv"] and ".W(4'd4)" in files["soc.sv"]


def test_a_parameters_own_expression_is_written_as_it_stands(tmp_path):
    """The module parameter's own value is what the HDL declares for it, so it keeps its meaning there."""
    the_component = component("valued", [wire("q_o", "out", 32)], parameters={"P": "~8'h0F"})
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [the_component], VALUED, expect={"s_q_o": (32, 0xFFFFFFF0)}, lint=False)
    assert ".P(~8'h0F)" in files["soc.sv"]


def test_an_ipxact_function_in_a_module_parameter_is_refused():
    refused("uses an IP-XACT function", design("soc", [instance("s", "valued")]), [valued(value='$ipxact_port_width("q_o")')])


def test_a_decimal_with_underscores_is_written_as_it_stands(tmp_path):
    files = passes(tmp_path, design("soc", [instance("s", "valued")]), [valued(value="100_000")], VALUED, expect={"s_q_o": (32, 100000)})
    assert ".P(100_000)" in files["soc.sv"]


def test_a_name_inside_a_string_with_an_escaped_quote_stays_a_string():
    the_component = valued(design_parameter("W", "8"), value='"say \\"W\\" here"')
    assert '"say \\"W\\" here"' in files_of(design("soc", [instance("s", "valued")]), [the_component])["soc.sv"]


def test_an_override_only_reaches_a_real_parameter_of_that_name():
    the_component = valued(design_parameter("W", "1"), value="pk::W")
    refused("reach no module parameter: W", design("soc", [instance("s", "valued", W=100)]), [the_component])


def test_an_override_can_reach_a_module_parameter_through_another_override(tmp_path):
    the_component = valued(design_parameter("A", "1"), design_parameter("B", "2"), value="A")
    soc = design("soc", [instance("s", "valued", A="B", B=7)])
    passes(tmp_path, soc, [the_component], VALUED, expect={"s_q_o": (32, 7)})


def test_a_fill_literal_in_an_expression_of_parameters_is_refused():
    the_component = valued(design_parameter("M", "'1"), value="M >> 4")
    refused("fill literal inside an expression", design("soc", [instance("s", "valued")]), [the_component])


def test_a_fill_literal_means_all_ones(tmp_path):
    source = "module filled #(parameter logic [7:0] P = 0) (output logic [7:0] q_o); assign q_o = P; endmodule\n"
    the_component = component("filled", [wire("q_o", "out", 8)], parameters={"P": "'1"})
    files = passes(tmp_path, design("soc", [instance("s", "filled")]), [the_component], source, expect={"s_q_o": (8, 0xFF)})
    assert ".P('1)" in files["soc.sv"]


def plain_seer():
    return component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])


@pytest.mark.parametrize("value", ["'1", "ALL"])
def test_tying_to_a_fill_literal_gives_all_ones(tmp_path, value):
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied=value)], parameters=[design_parameter("ALL", "'1")])
    passes(tmp_path, soc, [plain_seer()], SEE, expect={"s_seen_o": (8, 0xFF)})


def test_a_fill_literal_as_a_tie_off_gives_all_ones(tmp_path):
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [tier(port_map("WORD", tie="'1")), seer()], FAR_SIDE + SEE, expect={"s_seen_o": (8, 0xFF)})


def test_a_fill_literal_as_a_port_default_gives_all_ones(tmp_path):
    the_component = component("seer", [wire("data_i", "in", 8, default="'1"), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    passes(tmp_path, soc, [the_component], SEE, expect={"s_seen_o": (8, 0xFF)})


def test_a_fill_literal_as_an_abstraction_default_gives_all_ones(tmp_path):
    documents = bus_documents()
    next(p for p in documents[1].ports if p.logical_name == "WORD").wire.default_value = _E("'1")
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [far_side(), seer()], FAR_SIDE + SEE, expect={"s_seen_o": (8, 0xFF)}, buses=documents)


def test_a_fill_literal_through_a_typed_parameter_has_that_types_width(tmp_path):
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="MASK")], parameters=[typed_parameter("MASK", "'1", "bit", (3, 0))])
    passes(tmp_path, soc, [plain_seer()], SEE, expect={"s_seen_o": (8, 0x0F)})


@pytest.mark.parametrize("value", ["~'0", "-M"])
def test_a_fill_literal_inside_a_constant_is_refused(value):
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied=value)], parameters=[design_parameter("M", "'1")])
    refused("has no width of its own", soc, [plain_seer()])


def test_a_constant_past_32_bits_on_a_typedef_port_keeps_its_value():
    documents = [component("takes", [wire("boot_i", "in", type_name="pk::addr_t")])]
    soc = design("soc", [instance("u", "takes")], ad_hocs=[ad_hoc("t", ("u", "boot_i"), tied="64'h80000000")])
    assert "33'sd2147483648" in files_of(soc, documents)["soc.sv"].replace("  ", " ")


def test_a_parameter_from_further_out_keeps_the_meaning_of_its_names(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("o", "out", 8)])
    chip.parameters = [design_parameter("X", "1"), design_parameter("T", "X + 1")]
    chip_d = design(
        "chip_d",
        [instance("s", "seer")],
        ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="T"), ad_hoc("b", ("s", "seen_o"), external=["o"])],
        parameters=[design_parameter("X", "100")],
    )
    passes(tmp_path, chip_d, [chip, plain_seer()], SEE, module="chip", expect={"o": (8, 2)})


def test_a_fill_literal_where_a_width_is_needed_is_refused():
    port = ipxact.Port(name="x_o", wire=ipxact.WirePort(direction=ipxact.Direction.OUT, vectors=[ipxact.Vector(_E("'1"), _E("0"))]))
    refused("has no width of its own", design("soc", [instance("s", "p")]), [component("p", [port])])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="'1 & 3")])
    refused("has no width of its own", soc, [plain_seer()])


def test_a_name_no_parameter_explains_is_refused():
    the_component = component("p", [wire("x_o", "out")], parameters={"W": 1})
    refused("uses MISSING, which no parameter here", design("soc", [instance("s", "p", W="MISSING")]), [the_component])


def test_a_parameter_on_the_instantiation_is_in_scope(tmp_path):
    the_component = component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": "IP"})
    the_component.model.component_instantiations[0].parameters = [design_parameter("IP", "16")]
    passes(tmp_path, design("soc", [instance("s", "sized")]), [the_component], SIZED, expect={"s_data_o": (16, 0xFFFF)})


def test_an_override_on_a_runtime_parameter_is_refused():
    the_component = component("runner", [wire("x_o", "out")], parameters={"SEED": 1})
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="SEED", value=_E("SEED"), usage_type="runtime")]
    refused("runtime parameter, so an override cannot reach it", design("soc", [instance("s", "runner", SEED=2)]), [the_component])


def test_a_boolean_expression_follows_its_override(tmp_path):
    the_component = component("inverter", [wire("d_o", "out")], parameters={"EN": "true"}, module_parameters=[])
    the_component.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="DIS", value=_E("!EN"))]
    source = "module inverter #(parameter bit DIS = 0) (output logic d_o); assign d_o = DIS; endmodule\n"
    passes(tmp_path, design("soc", [instance("s", "inverter", EN="false")]), [the_component], source, expect={"s_d_o": (1, 1)})


def design_parameter(name, value):
    return ipxact.Parameter(name=name, value=_E(value), parameter_id=name, resolve="user")


def test_an_override_can_name_a_string_or_boolean_design_parameter(tmp_path):
    string_part = component("moded", [wire("fast_o", "out")], parameters={"MODE": '"SLOW"'})
    soc = design("soc", [instance("s", "moded", MODE="design_mode")], parameters=[design_parameter("design_mode", '"FAST"')])
    passes(tmp_path, soc, [string_part], MODED, expect={"s_fast_o": (1, 1)})

    source = "module enabled #(parameter bit EN = 0) (output logic en_o); assign en_o = EN; endmodule\n"
    boolean_part = component("enabled", [wire("en_o", "out")], parameters={"EN": "false"})
    soc = design("soc", [instance("s", "enabled", EN="design_en")], parameters=[design_parameter("design_en", "true")])
    passes(tmp_path, soc, [boolean_part], source, expect={"s_en_o": (1, 1)})


def test_a_design_and_its_top_component_can_use_the_same_parameter_id(tmp_path):
    source = "module valued #(parameter int W = 1) (output logic [31:0] w_o); assign w_o = W; endmodule\n"
    chip = hierarchical("chip", "chip_d", [wire("o", "out", 32)])
    chip.parameters = [design_parameter("W", "7")]
    chip_d = design(
        "chip_d",
        [instance("s", "valued", W="W")],
        ad_hocs=[ad_hoc("a", ("s", "w_o"), external=["o"])],
        parameters=[design_parameter("W", "5")],
    )
    the_component = component("valued", [wire("w_o", "out", 32)], parameters={"W": 1})
    passes(tmp_path, chip_d, [chip, the_component], source, module="chip", expect={"o": (32, 5)})  # the design's own


def test_an_override_wider_than_an_unsized_literal_is_written_with_its_size(tmp_path):
    source = "module big #(parameter logic [63:0] V = 0) (output logic [63:0] v_o); assign v_o = V; endmodule\n"
    the_component = component("big", [wire("v_o", "out", 64)], parameters={"V": 0})
    soc = design("soc", [instance("s", "big", V="64'hdeadbeefcafef00d")])
    files = passes(tmp_path, soc, [the_component], source, expect={"s_v_o": (64, 0xDEADBEEFCAFEF00D)})
    assert ".V(64'hdeadbeefcafef00d)" in files["soc.sv"]


@pytest.mark.parametrize("value", ["1e400", "$sqrt(-1.0)", "1/0"])
def test_an_unusable_width_is_refused(value):
    port = ipxact.Port(name="x_o", wire=ipxact.WirePort(direction=ipxact.Direction.OUT, vectors=[ipxact.Vector(_E(value), _E("0"))]))
    refused("x_o", design("soc", [instance("s", "p")]), [component("p", [port])])


@pytest.mark.parametrize("value", ["1e400", "1/0"])
def test_an_unusable_override_is_refused(value):
    refused("not a usable number|cannot evaluate", design("soc", [instance("s", "p", W=value)]), [component("p", [wire("x_o", "out")], parameters={"W": 1})])


def test_a_huge_port_is_refused_rather_than_exhausting_memory():
    refused("a port of 8589934592 bits", design("soc", [instance("s", "p")]), [component("p", [wire("x_o", "out", 1 << 33)])])


def test_a_top_option_that_is_not_text_is_a_type_error(tmp_path):
    soc = design("soc", [instance("s", "seer")])
    linked = link(soc, library_of([*bus_documents(), seer(), soc]))
    with pytest.raises(TypeError, match="top option"):
        RtlExporter().export(linked, tmp_path, top=123)


# --- design-level expressions see the design's parameters


WIDE = "module wide (output logic [15:0] data_o); assign data_o = 16'hbeef; endmodule\n"


def test_a_part_select_in_the_design_uses_the_designs_parameter(tmp_path):
    the_component = component("wide", [wire("data_o", "out", 16)], parameters={"W": 8}, module_parameters=[])
    soc = design(
        "soc",
        [instance("m", "wide"), instance("n", "nib")],
        ad_hocs=[ad_hoc("c", ("m", "data_o", ("W+3", "W")), ("n", "x_i"))],
        parameters=[design_parameter("W", "0")],  # the same id as the instance's W, which is 8
    )
    passes(tmp_path, soc, [the_component, nib()], WIDE + NIBBLE, expect={"n_seen_o": (4, 0xF)})


def test_a_tied_value_in_the_design_uses_the_designs_parameter(tmp_path):
    the_component = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)], parameters={"V": "'h11"}, module_parameters=[])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="V")], parameters=[design_parameter("V", "'h3c")])
    passes(tmp_path, soc, [the_component], SEE, expect={"s_seen_o": (8, 0x3C)})


def test_a_part_select_on_the_modules_own_port_uses_the_designs_parameter(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("bus_i", "in", 16), wire("o", "out", 4)])
    chip_d = design(
        "chip_d",
        [instance("n", "nib")],
        ad_hocs=[ad_hoc("c", ("n", "x_i"), external=[("bus_i", ("L+3", "L"))]), ad_hoc("b", ("n", "seen_o"), external=["o"])],
        parameters=[design_parameter("L", "8")],
    )
    passes(tmp_path, chip_d, [chip, nib()], NIBBLE, module="chip", drive={"bus_i": (16, 0xBEEF)}, expect={"o": (4, 0xE)})


# --- inouts


def test_two_inouts_of_the_module_on_one_net_are_refused():
    ports = [wire("pad1", "inout", 8), wire("pad2", "inout", 8), wire("o", "out", 8)]
    chip_d = design("chip_d", [instance("s", "seeing_pad")], ad_hocs=[ad_hoc("c", ("s", "pad"), external=["pad1", "pad2"]), ad_hoc("b", ("s", "seen_o"), external=["o"])])
    refused("two inouts of the module", chip_d, [hierarchical("chip", "chip_d", ports), seeing_pad()])

    top = hierarchical("chip", "chip_d", ports, [bus("u1", "target", port_map("PAD", "pad1")), bus("u2", "target", port_map("PAD", "pad2"))])
    chip_d = design("chip_d", [instance("s", "seeing_pad")], [interconnection("l", ("s", "b"), hier=["u1", "u2"])], [ad_hoc("b", ("s", "seen_o"), external=["o"])])
    refused("two inouts of the module", chip_d, [top, seeing_pad()])


def test_an_inverted_inout_is_refused():
    soc = design("soc", [instance("d", "driver_pad"), instance("s", "seeing_pad")], [interconnection("l", ("d", "b"), ("s", "b"))])
    refused("inverted port map on inout s.pad", soc, [driver_pad(), seeing_pad(port_map("PAD", "pad", invert=True))])


def test_an_inout_tied_to_a_constant_under_a_top_component(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("a_o", "out", 8)])
    chip_d = design("chip_d", [instance("s", "seeing_pad")], ad_hocs=[ad_hoc("t", ("s", "pad"), tied="'h5a"), ad_hoc("a", ("s", "seen_o"), external=["a_o"])])
    files = passes(tmp_path, chip_d, [chip, seeing_pad()], PADS, module="chip", expect={"a_o": (8, 0x5A)})
    assert "None" not in files["chip.sv"]


@pytest.mark.parametrize("vectors", [[(15, 8)], [(0, 7)]])
def test_logical_slices_on_a_top_inout_not_declared_from_zero_are_refused(vectors):
    top = hierarchical("chip", "chip_d", [wire("pad", "inout", vectors=vectors), wire("o", "out", 8)], [bus("u", "target", port_map("PAD", "pad"))])
    slices = component(
        "nibs",
        [wire("hi_i", "in", 4), wire("lo_i", "in", 4), wire("seen_o", "out", 8)],
        [bus("b", "target", port_map("PAD", "hi_i", logical_range=(7, 4)), port_map("PAD", "lo_i", logical_range=(3, 0)))],
    )
    chip_d = design("chip_d", [instance("n", "nibs")], [interconnection("l", ("n", "b"), hier=["u"])], [ad_hoc("b", ("n", "seen_o"), external=["o"])])
    refused("logical slices on inout pad", chip_d, [top, slices])


def test_width_mismatches_through_an_inout_are_refused():
    narrow = component("nib", [wire("x_i", "in", 4), wire("seen_o", "out", 4)])
    soc = design("soc", [instance("d", "driver_pad"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("d", "pad"), ("n", "x_i"))])
    refused("width mismatch, n.x_i is 4 bits and the inout is 8", soc, [component("driver_pad", [wire("pad", "inout", 8)]), narrow])

    driving = component("nibs", [wire("x_o", "out", 4)])
    soc = design("soc", [instance("d", "seeing_pad"), instance("n", "nibs")], ad_hocs=[ad_hoc("c", ("d", "pad"), ("n", "x_o"))])
    refused("width mismatch, n.x_o is 4 bits and the inout is 8", soc, [component("seeing_pad", [wire("pad", "inout", 8), wire("seen_o", "out", 8)]), driving])


def test_a_tie_off_reaches_an_inout(tmp_path):
    tier = component("far_side", [wire("flag_o", "out")], [bus("b", "initiator", port_map("PAD", tie="'h3c"))])
    soc = design("soc", [instance("m", "far_side"), instance("s", "seeing_pad")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [tier, seeing_pad()], FAR_SIDE + PADS, expect={"s_seen_o": (8, 0x3C)})


def defaulted_pad():
    documents = bus_documents()
    next(p for p in documents[1].ports if p.logical_name == "PAD").wire.default_value = _E("'h3c")
    return documents


def test_an_abstraction_default_reaches_an_inout_nothing_else_connects(tmp_path):
    soc = design("soc", [instance("m", "far_side"), instance("s", "seeing_pad")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [far_side(), seeing_pad()], FAR_SIDE + PADS, expect={"s_seen_o": (8, 0x3C)}, buses=defaulted_pad())


def test_an_abstraction_default_does_not_fight_an_inout_that_drives(tmp_path):
    soc = design("soc", [instance("d", "driver_pad"), instance("s", "seeing_pad")], [interconnection("l", ("d", "b"), ("s", "b"))])
    passes(tmp_path, soc, [driver_pad(), seeing_pad()], PADS, expect={"s_seen_o": (8, 0x5A)}, buses=defaulted_pad())


# --- structs


def test_a_part_select_on_a_struct_is_refused():
    source = component("src", [struct("req_o", "out", hi=4, lo=4)], [bus("b", "initiator", port_map("WORD", "req_o", select=(7, 4)))])
    soc = design("soc", [instance("m", "src"), instance("n", "nib")], [interconnection("l", ("m", "b"), ("n", "b"))])
    refused(r"part select on m\.req_o", soc, [source, nib(port_map("WORD", "x_i"))])


def test_a_struct_into_a_wire_of_another_size_is_refused():
    soc = design("soc", [instance("m", "src"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("n", "x_i"))])
    refused(r"width mismatch, m\.req_o is 8 bits and n\.x_i is 4 bits", soc, [component("src", [struct("req_o", "out", hi=4, lo=4)]), nib()])


def test_a_struct_into_a_wire_of_its_size_connects(tmp_path):
    soc = design("soc", [instance("m", "src"), instance("s", "seer")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("s", "data_i"))])
    documents = [component("src", [struct("req_o", "out", hi=4, lo=4)]), component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])]
    passes(tmp_path, soc, documents, STRUCTS + SEE, expect={"s_seen_o": (8, 0xA5)}, packages=STRUCT_PACKAGES)


def test_a_struct_with_no_declared_sub_ports_only_connects_to_its_own_type(tmp_path):
    documents = [component("src", [struct("req_o", "out")]), component("dst", [struct("req_i", "in"), wire("seen_o", "out", 8)])]
    soc = design("soc", [instance("m", "src"), instance("t", "dst")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("t", "req_i"))])
    passes(tmp_path, soc, documents, STRUCTS, expect={"t_seen_o": (8, 0xA5)}, packages=STRUCT_PACKAGES)

    documents = [component("src", [struct("req_o", "out", "other_pkg::req_t")]), documents[1]]
    refused("cannot check the width", soc, documents)

    soc = design("soc", [instance("m", "src"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("n", "x_i"))])
    refused("cannot check the width", soc, [component("src", [struct("req_o", "out")]), nib()])


def test_a_struct_and_an_array_of_one_of_it_connect(tmp_path):
    source = STRUCTS + "module one (input req_pkg::req_t [0:0] req_i, output logic [7:0] seen_o); assign seen_o = req_i; endmodule\n"
    documents = [component("src", [struct("req_o", "out")]), component("one", [struct("req_i", "in", "req_pkg::req_t [0:0]"), wire("seen_o", "out", 8)])]
    soc = design("soc", [instance("m", "src"), instance("t", "one")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("t", "req_i"))])
    passes(tmp_path, soc, documents, source, expect={"t_seen_o": (8, 0xA5)}, packages=STRUCT_PACKAGES)


def test_a_sub_port_of_a_typedef_with_declared_siblings_is_refused():
    typed_sub_port = ipxact.SubPort(name="e", wire=ipxact.WirePort(direction=ipxact.Direction.IN, wire_type_defs=[ipxact.WireTypeDef(type_name="p::u3_t")]))
    port = struct("req_i", "in", lo=4)
    port.structured.sub_ports.append(typed_sub_port)
    soc = design("soc", [instance("n", "bytes"), instance("t", "dst")], ad_hocs=[driving_a_sub_port("e")])
    refused("cannot check the width", soc, [component("bytes", [wire("x_o", "out", 8)]), component("dst", [port, wire("seen_o", "out", 8)])])


def test_a_sub_port_of_an_array_of_structs_is_refused():
    soc = design("soc", [instance("n", "nibs"), instance("t", "dst")], ad_hocs=[driving_a_sub_port("hi")])
    documents = [component("nibs", [wire("x_o", "out", 4)]), component("dst", [struct("req_i", "in", "req_pkg::req_t [1:0]", hi=4, lo=4)])]
    refused("a sub-port of t.req_i, an array", soc, documents)


def union(name, direction, **members):
    port = struct(name, direction, "req_pkg::req_u", **members)
    port.structured.struct_type = "union"
    return port


def test_a_union_driven_through_two_members_is_refused():
    connections = [driving_a_sub_port("a"), ipxact.AdHocConnection(name="d", internal_port_references=[ipxact.InternalPortReference(component_instance_ref="o", port_ref="x_o"), sub_port_reference("req_i", "b")])]
    soc = design("soc", [instance("n", "nibs"), instance("o", "nibs"), instance("t", "dst")], ad_hocs=connections)
    refused("already driven", soc, [component("nibs", [wire("x_o", "out", 4)]), component("dst", [union("req_i", "in", a=4, b=4)])])


def nested(kind, **members):
    inner = struct("u", "in", "req_pkg::inner_t", **members).structured
    inner.struct_type = kind
    outer = struct("req_i", "in", "req_pkg::outer_t", lo=4)
    outer.structured.sub_ports.append(ipxact.SubPort(name="u", structured=inner))
    return outer


def driving_nested(*tail):
    return [driving_a_sub_port("lo"), ipxact.AdHocConnection(name="d", internal_port_references=[ipxact.InternalPortReference(component_instance_ref="o", port_ref="x_o"), sub_port_reference("req_i", "u", *tail)])]


def nested_documents(port):
    return [component("nibs", [wire("x_o", "out", 4)]), component("dst", [port])]


def test_a_nested_union_is_driven_through_one_member():
    soc = design("soc", [instance("n", "nibs"), instance("o", "nibs"), instance("t", "dst")], ad_hocs=driving_nested("a"))
    assert "assign t_req_i.u.a = o_x_o;" in files_of(soc, nested_documents(nested("union", a=4, b=4)))["soc.sv"].replace("  ", " ")


def test_a_struct_inside_a_struct_can_leave_its_sub_ports_unlisted():
    soc = design("soc", [instance("n", "nibs"), instance("o", "nibs"), instance("t", "dst")], ad_hocs=driving_nested("x"))
    assert "t_req_i.u.x" in files_of(soc, nested_documents(nested("struct")))["soc.sv"]


def test_an_unpacked_struct_does_not_connect_to_a_vector():
    port = struct("req_o", "out", hi=4, lo=4)
    port.structured.packed = False
    soc = design("soc", [instance("m", "src"), instance("s", "seer")], ad_hocs=[ad_hoc("c", ("m", "req_o"), ("s", "data_i"))])
    refused("cannot check the width", soc, [component("src", [port]), plain_seer()])


def test_a_phantom_struct_port_is_left_out():
    port = struct("req_o", "out")
    port.structured.direction = None
    port.structured.phantom = True
    assert "req_o" not in files_of(design("soc", [instance("m", "src")]), [component("src", [port, wire("x_o", "out")])])["soc.sv"]


def test_a_typedef_with_no_package_in_its_name_is_refused():
    documents = [component("a", [wire("x_o", "out", type_name="byte_t")]), component("z", [wire("i_i", "in", type_name="byte_t")])]
    soc = design("soc", [instance("a", "a"), instance("z", "z")], ad_hocs=[ad_hoc("c", ("a", "x_o"), ("z", "i_i"))])
    refused("cannot see type byte_t", soc, documents)


ENUM_PACKAGE = "package mode_pkg; typedef enum logic [1:0] { A, B, C } mode_e; endpackage\n"
ENUMS = """
module mode_setter (output mode_pkg::mode_e mode_o); assign mode_o = mode_pkg::A; endmodule
module mode_getter (input mode_pkg::mode_e mode_i, output logic [1:0] seen_o); assign seen_o = mode_i; endmodule
"""


def mode_getter(*maps):
    return component("mode_getter", [wire("mode_i", "in", type_name="mode_pkg::mode_e"), wire("seen_o", "out", 2)], [bus("b", "target", *maps)] if maps else [])


def test_a_constant_on_an_enum_port_is_cast(tmp_path):
    soc = design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied=2)])
    files = passes(tmp_path, soc, [mode_getter()], ENUMS, expect={"g_seen_o": (2, 2)}, packages=ENUM_PACKAGE)
    assert "mode_pkg::mode_e'(2)" in files["soc.sv"]


def test_an_inverted_enum_connection_is_cast(tmp_path):
    setter = component("mode_setter", [wire("mode_o", "out", type_name="mode_pkg::mode_e")], [bus("b", "initiator", port_map("FLAG", "mode_o"))])
    soc = design("soc", [instance("s", "mode_setter"), instance("g", "mode_getter")], [interconnection("l", ("s", "b"), ("g", "b"))])
    files = passes(tmp_path, soc, [setter, mode_getter(port_map("FLAG", "mode_i", invert=True))], ENUMS, expect={"g_seen_o": (2, 3)}, packages=ENUM_PACKAGE)
    assert "mode_pkg::mode_e'(~" in files["soc.sv"]


def test_tying_a_typedef_port_to_its_default_casts_the_default(tmp_path):
    the_component = component("mode_getter", [wire("mode_i", "in", type_name="mode_pkg::mode_e", default="2"), wire("seen_o", "out", 2)])
    soc = design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied="default")])
    files = passes(tmp_path, soc, [the_component], ENUMS, expect={"g_seen_o": (2, 2)}, packages=ENUM_PACKAGE)
    assert "mode_pkg::mode_e'(2)" in files["soc.sv"]


def test_tying_a_typedef_port_to_a_default_given_in_parts_is_refused():
    in_parts = [ipxact.Driver(default_value=_E("1"), range=bounds(0, 0)), ipxact.Driver(default_value=_E("0"), range=bounds(1, 1))]
    the_component = component("mode_getter", [wire("mode_i", "in", type_name="mode_pkg::mode_e", drivers=in_parts), wire("seen_o", "out", 2)])
    soc = design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied="default")])
    refused("tying g.mode_i, a mode_pkg::mode_e, to a default given in parts", soc, [the_component])


def test_tying_a_typedef_port_with_no_default_to_its_default_is_refused():
    soc = design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied="default")])
    refused("tied to 'default', but g.mode_i has no default value", soc, [mode_getter()])


CFG_PACKAGE = """
package p_pkg;
    typedef enum logic [1:0] { OFF, SLOW, FAST } mode_e;
    typedef struct packed { mode_e mode; logic [5:0] data; } cfg_t;
endpackage
"""
CFG_SEER = "module cfg_seer (input p_pkg::cfg_t cfg_i, output logic [7:0] seen_o); assign seen_o = cfg_i; endmodule\n"


def cfg_seer(packed=None):
    port = struct("cfg_i", "in", type_name="p_pkg::cfg_t", data=6)
    mode = ipxact.SubPort(name="mode", wire=ipxact.WirePort(direction=ipxact.Direction.IN, wire_type_defs=[ipxact.WireTypeDef(type_name="p_pkg::mode_e")]))
    port.structured.sub_ports.insert(0, mode)
    port.structured.packed = packed
    return component("cfg_seer", [port, wire("seen_o", "out", 8)])


def tied_sub_port(name, sub_port, value):
    reference = ipxact.InternalPortReference(component_instance_ref="u", port_ref="cfg_i", sub_port_refs=[ipxact.SubPortReference(sub_port_ref=sub_port)])
    return ipxact.AdHocConnection(name=name, tied_value=_E(str(value)), internal_port_references=[reference])


def test_a_constant_on_an_enum_member_of_a_struct_is_cast(tmp_path):
    soc = design("soc", [instance("u", "cfg_seer")], ad_hocs=[tied_sub_port("tm", "mode", 2), tied_sub_port("td", "data", 5)])
    files = passes(tmp_path, soc, [cfg_seer()], CFG_SEER, expect={"u_seen_o": (8, 0x85)}, packages=CFG_PACKAGE)
    assert "assign u_cfg_i.mode = p_pkg::mode_e'(2);" in files["soc.sv"]


def test_a_constant_too_wide_for_an_enum_is_stopped_by_the_compiler(tmp_path):
    """Only the compiler knows how wide an enum is, so the generated module has it check."""
    soc = design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied=6)])
    files = files_of(soc, [mode_getter()])
    assert "if ($bits(g_mode_i) < 3) begin : g_mode_i_fits" in files["soc.sv"]
    assert "DiagCode(ErrorTask)" in slang_errors(ENUM_PACKAGE, files["soc.sv"], ENUMS)
    if has_icarus and icarus_runs_elaboration_tasks_in_generate_blocks(tmp_path):  # not before Icarus 13
        with pytest.raises(AssertionError, match="the constant 6 does not fit in g.mode_i, a mode_pkg::mode_e"):
            simulate(tmp_path, {"mode_pkg.sv": ENUM_PACKAGE, **files}, [ENUMS], bench("soc", {}, {"g_seen_o": (2, 2)}), top="tb")
    if has_verilator:
        assert verilator_lint(tmp_path, {"mode_pkg.sv": ENUM_PACKAGE, **files}, [ENUMS], "soc").returncode != 0
    # one that fits, or a single bit, which any type holds, is left alone
    assert "$bits" not in files_of(design("soc", [instance("g", "mode_getter")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied=1)]), [mode_getter()])["soc.sv"]


def test_the_compilers_check_has_a_name_of_its_own():
    soc = design("soc", [instance("g", "mode_getter"), instance("g_mode_i_fits", "nib")], ad_hocs=[ad_hoc("t", ("g", "mode_i"), tied=2)])
    text = files_of(soc, [mode_getter(), nib()])["soc.sv"]
    assert "begin : g_mode_i_fits1" in text and slang_errors(ENUM_PACKAGE, text, ENUMS + NIBBLE) == []


@pytest.mark.parametrize("sub_port", [None, "mode"])
def test_a_constant_into_an_unpacked_struct_is_refused(sub_port):
    the_component = cfg_seer(packed=False)
    ties = [tied_sub_port("t", sub_port, 0), tied_sub_port("d", "data", 0)] if sub_port else [ad_hoc("t", ("u", "cfg_i"), tied=0)]
    soc = design("soc", [instance("u", "cfg_seer")], ad_hocs=ties)
    if sub_port:  # a member of an unpacked struct is still a member of its own type
        assert "p_pkg::mode_e'(0)" in files_of(soc, [the_component])["soc.sv"]
    else:
        refused("u.cfg_i, an unpacked struct", soc, [the_component])


@pytest.mark.parametrize("on_the_module_parameter", [False, True])
def test_a_derived_parameter_left_to_the_hdl_still_keeps_to_its_maximum(on_the_module_parameter):
    the_component = component("doubling", [wire("x_o", "out")], parameters={"A": "4", "B": "A * 2"})
    bounded = the_component.parameters[1]
    if on_the_module_parameter:
        bounded = the_component.model.component_instantiations[0].module_parameters[1]
        bounded.value = _E("A * 2")
    bounded.type, bounded.maximum = "int", "10"
    assert ".B(" not in files_of(design("soc", [instance("s", "doubling", A=5)]), [the_component])["soc.sv"]
    refused("module parameter B: .*evaluates to 100, above its maximum 10", design("soc", [instance("s", "doubling", A=50)]), [the_component])


def test_a_further_parameters_vector_bounds_keep_their_meaning(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("o", "out", 32)])
    chip.parameters = [design_parameter("W", "32"), typed_parameter("MASK", "'1", "bit", ("W-1", 0))]
    chip_d = design("chip_d", [instance("s", "valued", V="MASK")], ad_hocs=[ad_hoc("b", ("s", "q_o"), external=["o"])], parameters=[design_parameter("W", "16")])
    passes(tmp_path, chip_d, [chip, valued(design_parameter("V", "0"))], VALUED, module="chip", expect={"o": (32, 0xFFFFFFFF)}, lint=False)


def test_a_struct_output_of_the_module_nothing_drives_is_refused():
    chip = hierarchical("chip", "chip_d", [struct("req_o", "out"), wire("o", "out", 8)])
    plain = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    chip_d = design("chip_d", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied=1), ad_hoc("b", ("s", "seen_o"), external=["o"])])
    refused(r"chip\.req_o: top-level output parts", chip_d, [chip, plain])


def sub_port_reference(name, *path):
    return ipxact.InternalPortReference(component_instance_ref="t", port_ref=name, sub_port_refs=[ipxact.SubPortReference(sub_port_ref=p) for p in path])


def driving_a_sub_port(*path):
    return ipxact.AdHocConnection(name="c", internal_port_references=[ipxact.InternalPortReference(component_instance_ref="n", port_ref="x_o"), sub_port_reference("req_i", *path)])


def test_a_struct_input_driven_in_part_is_refused():
    soc = design("soc", [instance("n", "nibs"), instance("t", "dst")], ad_hocs=[driving_a_sub_port("hi")])
    documents = [component("nibs", [wire("x_o", "out", 4)]), component("dst", [struct("req_i", "in", hi=4, lo=4), wire("seen_o", "out", 8)])]
    refused(r"t\.req_i: parts \['\.lo'\] are not driven", soc, documents)


def test_a_sub_port_the_struct_does_not_declare_is_refused():
    soc = design("soc", [instance("n", "nibs"), instance("t", "dst")], ad_hocs=[driving_a_sub_port("middle")])
    documents = [component("nibs", [wire("x_o", "out", 4)]), component("dst", [struct("req_i", "in", hi=4, lo=4), wire("seen_o", "out", 8)])]
    refused(r"t\.req_i has no sub-port '\.middle'", soc, documents)


def test_a_sub_port_of_the_wrong_size_is_refused():
    soc = design("soc", [instance("n", "bytes"), instance("t", "dst")], ad_hocs=[driving_a_sub_port("hi")])
    documents = [component("bytes", [wire("x_o", "out", 8)]), component("dst", [struct("req_i", "in", hi=4, lo=4), wire("seen_o", "out", 8)])]
    refused(r"width mismatch, n\.x_o is 8 bits and t\.req_i\.hi is 4 bits", soc, documents)


def test_a_struct_driven_whole_and_by_sub_port_is_refused():
    whole = ad_hoc("w", ("m", "req_o"), ("t", "req_i"))
    soc = design("soc", [instance("m", "src"), instance("n", "nibs"), instance("t", "dst")], ad_hocs=[whole, driving_a_sub_port("hi")])
    documents = [
        component("src", [struct("req_o", "out", hi=4, lo=4)]),
        component("nibs", [wire("x_o", "out", 4)]),
        component("dst", [struct("req_i", "in", hi=4, lo=4), wire("seen_o", "out", 8)]),
    ]
    refused(r"t\.req_i\.hi is already driven", soc, documents)


# --- defaults


def test_a_tied_default_composes_the_drivers_bit_ranges(tmp_path):
    drivers = [ipxact.Driver(default_value=_E("'ha"), range=bounds(7, 4)), ipxact.Driver(default_value=_E("'h5"), range=bounds(3, 0))]
    the_component = component("seer", [wire("data_i", "in", 8, drivers=drivers), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    passes(tmp_path, soc, [the_component], SEE, expect={"s_seen_o": (8, 0xA5)})


def test_a_default_too_wide_for_its_port_is_refused():
    the_component = component("seer", [wire("data_i", "in", 8, default="9'h1FF"), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    refused("does not fit in 8 bits", soc, [the_component])


def test_a_negative_constant_that_does_not_fit_is_refused(tmp_path):
    refused("does not fit in 8 bits", design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="-300")]), [plain_seer()])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="-1")])
    passes(tmp_path, soc, [plain_seer()], SEE, expect={"s_seen_o": (8, 0xFF)})


def test_tying_a_port_of_the_design_alone_to_its_default_is_refused():
    pad = component("seeing_pad", [wire("pad", "inout", 8, default="'h3c"), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("s", "seeing_pad")], ad_hocs=[ad_hoc("c", ("s", "pad"), external=["pad_io"]), ad_hoc("t", external=["pad_io"], tied="default")])
    with pytest.raises(ValueError):
        files_of(soc, [pad])


def test_a_tied_default_with_bits_no_driver_covers_is_refused():
    drivers = [ipxact.Driver(default_value=_E("'ha"), range=bounds(7, 4))]
    the_component = component("seer", [wire("data_i", "in", 8, drivers=drivers), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    refused(r"no default value for bits \[0, 1, 2, 3\]", soc, [the_component])


def test_a_tied_default_uses_the_driver_of_the_selected_view(tmp_path):
    drivers = [ipxact.Driver(default_value=_E("'h11"), view_refs=[ipxact.TextValue("rtl")]), ipxact.Driver(default_value=_E("'h22"), view_refs=[ipxact.TextValue("alt")])]
    the_component = alternate_view(component("seer", [wire("data_i", "in", 8, drivers=drivers), wire("seen_o", "out", 8)]))
    soc = design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    passes(tmp_path, soc, [the_component], SEE, expect={"s_seen_o": (8, 0x22)}, configuration=choose_view("alt", "s"))


def test_a_tied_default_on_a_part_select_takes_those_bits_of_the_default(tmp_path):
    soc = design(
        "soc",
        [instance("m", "sender"), instance("s", "seer")],
        ad_hocs=[ad_hoc("hi", ("s", "data_i", (7, 4)), tied="default"), ad_hoc("lo", ("m", "data_o", (3, 0)), ("s", "data_i", (3, 0)))],
    )
    documents = [component("sender", [wire("data_o", "out", 8)]), component("seer", [wire("data_i", "in", 8, default="'hc3"), wire("seen_o", "out", 8)])]
    passes(tmp_path, soc, documents, SEND + SEE, expect={"s_seen_o": (8, 0xC5)})


def test_a_tied_default_on_a_part_select_of_an_ascending_port(tmp_path):
    source = "module reversed (input logic [0:7] data_i, output logic [7:0] seen_o); assign seen_o = data_i; endmodule\n" + SEND
    receiver = component("reversed", [wire("data_i", "in", vectors=[(0, 7)], default="'hc3"), wire("seen_o", "out", 8)])
    soc = design(
        "soc",
        [instance("m", "sender"), instance("s", "reversed")],
        ad_hocs=[ad_hoc("hi", ("s", "data_i", (0, 3)), tied="default"), ad_hoc("lo", ("m", "data_o", (3, 0)), ("s", "data_i", (4, 7)))],
    )
    passes(tmp_path, soc, [component("sender", [wire("data_o", "out", 8)]), receiver], source, expect={"s_seen_o": (8, 0xC5)}, lint=False)


def test_a_constant_too_wide_for_its_port_is_refused():
    plain = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    refused("does not fit in 8 bits", design("soc", [instance("s", "seer")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="'h1ff")]), [plain])

    tier = component("far_side", [wire("flag_o", "out")], [bus("b", "initiator", port_map("WORD", tie="'h1ff"))])
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused("does not fit in 8 bits", soc, [tier, seer()])


def test_an_abstraction_default_can_use_the_abstractions_parameter(tmp_path):
    documents = bus_documents()
    documents[1].parameters = [ipxact.Parameter(name="D", value=_E("'h3c"), parameter_id="D")]
    next(p for p in documents[1].ports if p.logical_name == "WORD").wire.default_value = _E("D")
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [far_side(), seer()], FAR_SIDE + SEE, expect={"s_seen_o": (8, 0x3C)}, buses=documents)


def test_requires_driver_with_no_driver_is_not_left_floating():
    documents = bus_documents()
    word = next(p for p in documents[1].ports if p.logical_name == "WORD")
    word.wire.requires_driver = ipxact.RequiresDriver(value=True)
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    assert "s_data_i" in files_of(soc, [far_side(), seer()], buses=documents)["soc.sv"].split(");")[0]


# --- tie-offs


def tier(*maps):
    return component("far_side", [wire("flag_o", "out")], [bus("b", "initiator", *maps)])


def test_tie_offs_for_logical_slices_each_apply(tmp_path):
    documents = [tier(port_map("WORD", tie="'ha", logical_range=(7, 4)), port_map("WORD", tie="'h5", logical_range=(3, 0))), seer()]
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, documents, FAR_SIDE + SEE, expect={"s_seen_o": (8, 0xA5)})


def test_a_tie_off_can_supply_the_bits_a_driver_does_not(tmp_path):
    half = component("half", [wire("lo_o", "out", 4)], [bus("b", "initiator", port_map("WORD", "lo_o", logical_range=(3, 0)), port_map("WORD", tie="'ha", logical_range=(7, 4)))])
    source = "module half (output logic [3:0] lo_o); assign lo_o = 4'h5; endmodule\n" + SEE
    soc = design("soc", [instance("m", "half"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    passes(tmp_path, soc, [half, seer()], source, expect={"s_seen_o": (8, 0xA5)})


def test_tie_offs_that_disagree_are_refused():
    def tying(name, value):
        return component(name, [wire("flag_i", "in")], [bus("b", "target", port_map("WORD", tie=value))])

    soc = design(
        "soc",
        [instance("m", "far_side"), instance("t", "first"), instance("u", "second"), instance("s", "seer")],
        [interconnection("l", ("m", "b"), ("t", "b"), ("u", "b"), ("s", "b"))],
    )
    refused("tied off more than one way", soc, [far_side(), tying("first", "'h11"), tying("second", "'h22"), seer()])


def replying(name, *maps):
    return component(name, [wire("reply_o", "out", 8), wire("flag_i", "in")], [bus("b", "target", *maps)], module="replying")


REPLYING = "module replying (output logic [7:0] reply_o, input logic flag_i); assign reply_o = 8'h5a; endmodule\n"
READS_REPLY = "module reads_reply (input logic [7:0] reply_i, output logic [7:0] seen_o); assign seen_o = reply_i; endmodule\n"


def reads_reply():
    return component("reads_reply", [wire("reply_i", "in", 8), wire("seen_o", "out", 8)], [bus("b", "initiator", port_map("REPLY", "reply_i"))])


def test_a_tie_off_on_a_driving_end_conflicts_with_a_real_driver():
    soc = design(
        "soc",
        [instance("m", "reads_reply"), instance("t", "real"), instance("u", "tied")],
        [interconnection("l", ("m", "b"), ("t", "b"), ("u", "b"))],
    )
    documents = [reads_reply(), replying("real", port_map("REPLY", "reply_o")), replying("tied", port_map("REPLY", tie="'h11"))]
    refused(r"'REPLY' is driven by t\.reply_o and tied off by u", soc, documents)


def test_a_tie_off_on_a_receiving_end_leaves_the_real_driver_alone(tmp_path):
    soc = design(
        "soc",
        [instance("m", "sender"), instance("u", "unused"), instance("s", "seer")],
        [interconnection("l", ("m", "b"), ("u", "b"), ("s", "b"))],
    )
    unused = component("unused", [wire("flag_i", "in")], [bus("b", "target", port_map("WORD", tie="'h11"))])
    source = SEND + SEE + "module unused (input logic flag_i); endmodule\n"
    tied = ad_hoc("t", ("u", "flag_i"), tied=0)
    soc.ad_hoc_connections = [tied]
    passes(tmp_path, soc, [sender(), unused, seer()], source, expect={"s_seen_o": (8, 0xA5)})


def test_a_slice_tied_off_on_a_receiving_end_leaves_the_real_driver_alone(tmp_path):
    unused = component("unused", [wire("flag_i", "in")], [bus("b", "target", port_map("WORD", tie="'h1", logical_range=(3, 0)))])
    soc = design("soc", [instance("m", "sender"), instance("u", "unused"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("u", "b"), ("s", "b"))], [ad_hoc("t", ("u", "flag_i"), tied=0)])
    passes(tmp_path, soc, [sender(), unused, seer()], SEND + SEE + "module unused (input logic flag_i); endmodule\n", expect={"s_seen_o": (8, 0xA5)})


def test_a_tie_off_can_be_wider_than_what_a_receiver_takes(tmp_path):
    takes_low = component("nib", [wire("x_i", "in", 4), wire("seen_o", "out", 4)], [bus("b", "target", port_map("WORD", "x_i", logical_range=(3, 0)))])
    soc = design("soc", [instance("m", "far_side"), instance("n", "nib")], [interconnection("l", ("m", "b"), ("n", "b"))])
    passes(tmp_path, soc, [tier(port_map("WORD", tie="'hC3")), takes_low], FAR_SIDE + NIBBLE, expect={"n_seen_o": (4, 3)})


def test_a_tie_off_on_a_system_end_next_to_a_driver_is_refused():
    system_end = component("sys", [wire("flag_i", "in")], [bus("b", "system", port_map("WORD", tie="'h11"))])
    system_end.bus_interfaces[0].system = "g"
    soc = design("soc", [instance("m", "sender"), instance("u", "sys"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("u", "b"), ("s", "b"))])
    with pytest.raises(ValueError):
        files_of(soc, [sender(), system_end, seer()])


def test_a_tie_off_with_slices_that_overlap_is_refused():
    documents = [tier(port_map("WORD", tie="'ha", logical_range=(7, 3)), port_map("WORD", tie="'h5", logical_range=(3, 0))), seer()]
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused("already driven", soc, documents)


# --- port types and part selects


def test_a_port_typed_int_is_32_bits(tmp_path):
    source = "module big (output int data_o); assign data_o = 32'hdeadbeef; endmodule\nmodule bigseer (input int data_i, output logic [31:0] seen_o); assign seen_o = data_i; endmodule\n"
    documents = [component("big", [wire("data_o", "out", type_name="int")]), component("bigseer", [wire("data_i", "in", type_name="int"), wire("seen_o", "out", 32)])]
    soc = design("soc", [instance("a", "big"), instance("b", "bigseer")], ad_hocs=[ad_hoc("c", ("a", "data_o"), ("b", "data_i"))])
    passes(tmp_path, soc, documents, source, expect={"b_seen_o": (32, 0xDEADBEEF)})


def test_a_part_select_on_a_port_typed_int_is_kept(tmp_path):
    source = "module big (output int data_o); assign data_o = 32'hdeadbeef; endmodule\n" + NIBBLE
    documents = [component("big", [wire("data_o", "out", type_name="int")]), nib()]
    soc = design("soc", [instance("a", "big"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("a", "data_o", (31, 28)), ("n", "x_i"))])
    passes(tmp_path, soc, documents, source, expect={"n_seen_o": (4, 0xD)})


def test_vectors_on_a_sized_type_are_refused():
    the_component = component("big", [wire("data_o", "out", 8, type_name="int")])
    refused("vectors on a port of type int", design("soc", [instance("a", "big")]), [the_component])


def test_a_typedef_connects_to_its_own_type_only(tmp_path):
    package = "package mode_pkg; typedef enum logic [1:0] { A, B } mode_e; endpackage\n"
    source = """
module setter (output mode_pkg::mode_e mode_o); assign mode_o = mode_pkg::B; endmodule
module getter (input mode_pkg::mode_e mode_i, output logic [1:0] seen_o); assign seen_o = mode_i; endmodule
"""
    documents = [
        component("setter", [wire("mode_o", "out", type_name="mode_pkg::mode_e")]),
        component("getter", [wire("mode_i", "in", type_name="mode_pkg::mode_e"), wire("seen_o", "out", 2)]),
    ]
    soc = design("soc", [instance("s", "setter"), instance("g", "getter")], ad_hocs=[ad_hoc("c", ("s", "mode_o"), ("g", "mode_i"))])
    passes(tmp_path, soc, documents, source, expect={"g_seen_o": (2, 1)}, packages=package)

    soc = design("soc", [instance("s", "setter"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("s", "mode_o"), ("n", "x_i"))])
    refused("cannot check the width", soc, [documents[0], nib()])


def test_a_part_select_on_a_port_with_several_dimensions_is_refused():
    matrix = component("matrix", [wire("data_o", "out", vectors=[(3, 0), (7, 0)])], [bus("b", "initiator", port_map("WORD", "data_o", select=(2, 1)))])
    soc = design("soc", [instance("m", "matrix"), instance("n", "nib")], [interconnection("l", ("m", "b"), ("n", "b"))])
    refused("several dimensions", soc, [matrix, nib(port_map("WORD", "x_i"))])


@pytest.mark.parametrize("vectors, select", [([(15, 0)], (4, 11)), ([(0, 15)], (11, 4))])
def test_a_part_select_running_against_its_port_is_refused(vectors, select):
    wide = component("wide", [wire("data_o", "out", vectors=vectors)], [bus("b", "initiator", port_map("WORD", "data_o", select=select))])
    soc = design("soc", [instance("m", "wide"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused("runs the other way", soc, [wide, seer()])


# --- names


def compiles(files, source):
    ordered = [text for _, text in sorted(files.items(), key=lambda item: not item[0].endswith("_pkg.sv"))]
    assert slang_errors(*ordered, source) == []


def test_a_net_gives_way_to_an_instance_of_the_same_name():
    soc = design(
        "soc",
        [instance("m", "sender"), instance("s", "seer"), instance("m_data_o", "seer")],
        [interconnection("l", ("m", "b"), ("s", "b"))],
        [ad_hoc("t", ("m_data_o", "data_i"), tied=0)],
    )
    compiles(files_of(soc, [sender(), seer()]), SEND + SEE)


def test_a_shared_net_gives_way_to_an_instance_of_the_same_name():
    soc = design(
        "soc",
        [instance("d", "driver_pad"), instance("s", "seeing_pad"), instance("link_PAD", "seer")],
        [interconnection("link", ("d", "b"), ("s", "b"))],
        [ad_hoc("t", ("link_PAD", "data_i"), tied=0)],
    )
    compiles(files_of(soc, [driver_pad(), seeing_pad(), seer()]), PADS + SEE)


def test_a_net_that_would_be_a_keyword_changes_name():
    source = "module a (output logic match); assign match = 1; endmodule\nmodule z (input logic i); endmodule\n"
    soc = design("soc", [instance("first", "a"), instance("zz", "z")], ad_hocs=[ad_hoc("c", ("first", "match"), ("zz", "i"))])
    compiles(files_of(soc, [component("a", [wire("match", "out")]), component("z", [wire("i", "in")])]), source)


def test_a_module_port_that_would_be_a_keyword_is_refused():
    refused("first_match.* is a SystemVerilog keyword", design("soc", [instance("first", "a")]), [component("a", [wire("match", "out")])])


def test_a_net_gives_way_to_a_module_port_of_the_same_name(tmp_path):
    chip = hierarchical("chip", "chip_d", [wire("s_data_i", "in", 8), wire("o", "out", 8)])
    inverted = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)], [bus("b", "target", port_map("WORD", "data_i", invert=True))])
    other = component("other", [wire("data_i", "in", 8), wire("seen_o", "out", 8)], module="seer")
    chip_d = design(
        "chip_d",
        [instance("m", "sender"), instance("s", "seer"), instance("k", "other")],
        [interconnection("l", ("m", "b"), ("s", "b"))],
        [ad_hoc("a", ("k", "data_i"), external=["s_data_i"]), ad_hoc("b", ("s", "seen_o"), external=["o"])],
    )
    passes(tmp_path, chip_d, [chip, sender(), inverted, other], SEND + SEE, module="chip", drive={"s_data_i": (8, 0)}, expect={"o": (8, 0x5A)})


def test_a_net_gives_way_to_a_port_that_claims_its_name_later(tmp_path):
    source = """
    module a_b (output logic c_o); assign c_o = 1'b1; endmodule
    module z (input logic i_i); endmodule
    module y (output logic b_c_o); assign b_c_o = 1'b0; endmodule
    """
    documents = [component("a_b", [wire("c_o", "out")]), component("z", [wire("i_i", "in")]), component("y", [wire("b_c_o", "out")])]
    # a_b's output gets the net a_b_c_o. Then y's unconnected output a.b_c_o claims a_b_c_o as a port of the module.
    soc = design("soc", [instance("a_b", "a_b"), instance("zz", "z"), instance("a", "y")], ad_hocs=[ad_hoc("c", ("a_b", "c_o"), ("zz", "i_i"))])
    files = passes(tmp_path, soc, documents, source, expect={"a_b_c_o": (1, 0)})
    assert "a_b_c_o_net" in files["soc.sv"]


def test_a_shared_net_gives_way_to_a_port_declared_after_it():
    documents = [driver_pad(), seeing_pad(), component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])]
    soc = design(
        "soc",
        [instance("d", "driver_pad"), instance("s", "seeing_pad"), instance("k", "seer")],
        [interconnection("link", ("d", "b"), ("s", "b"))],
        [ad_hoc("t", ("k", "data_i"), external=["link_PAD"])],
    )
    files = files_of(soc, documents)
    assert "link_PAD_net" in files["soc.sv"]
    compiles(files, PADS + SEE)


def test_an_instance_named_like_a_port_of_the_module_is_refused():
    chip = hierarchical("chip", "chip_d", [wire("s", "in", 8), wire("o", "out", 8)])
    plain = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    chip_d = design("chip_d", [instance("s", "seer")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["s"]), ad_hoc("b", ("s", "seen_o"), external=["o"])])
    refused("instance 's' has the name of a port", chip_d, [chip, plain])


def test_a_generated_module_named_like_an_ip_is_refused():
    plain = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    refused("module 'seer' would be generated here", design("seer", [instance("s", "seer")]), [plain])

    wrapper = hierarchical("wrap", "wrap_d", [wire("i", "in", 8), wire("o", "out", 8)])
    wrapper.model.views[0].component_instantiation_ref = "n"
    wrapper.model.component_instantiations = [ipxact.ComponentInstantiation(name="n", module_name="seer")]
    wrap_d = design("wrap_d", [instance("s", "seer")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["i"]), ad_hoc("b", ("s", "seen_o"), external=["o"])])
    refused("module 'seer' would be generated here", design("soc", [instance("w", "wrap")]), [plain, wrapper, wrap_d])


# --- the address map


DECODER = """
module decoder #(parameter int unsigned NUM_RULES = 1, parameter logic [NUM_RULES*96-1:0] ADDR_RULES = '0)
    (input logic [31:0] addr_i, output logic [NUM_RULES-1:0] sel_o, output logic x_o, output logic y_o);
    assign x_o = 0;
    assign y_o = 0;
    always_comb begin
        sel_o = '0;
        for (int unsigned i = 0; i < NUM_RULES; i++)
            if (addr_i >= ADDR_RULES[i*96+32+:32] && addr_i < ADDR_RULES[i*96+:32]) sel_o[ADDR_RULES[i*96+64+:32]] = 1;
    end
endmodule
module chosen (input logic sel_i, output logic seen_o); assign seen_o = sel_i; endmodule
"""
DECODE_THE_SECOND = """
module tb;
    logic [31:0] addr = 32'h2800;
    wire [1:0] sel;
    wire a, b;
    soc dut (.dec_addr_i(addr), .dec_sel_o(sel), .a_seen_o(a), .b_seen_o(b));
    initial begin
        #1;
        if (sel === 2'b10) $display("PASS"); else $display("FAIL sel=%b", sel);
        $finish;
    end
endmodule
"""


def window(end, base, size):
    extension = f'<xactflow:addressMapping xmlns:xactflow="https://github.com/IP-XACT/XactFlow" baseAddr="{base:#x}" size="{size:#x}"/>'
    return ipxact.ActiveInterface(component_instance_ref=end[0], bus_ref=end[1], vendor_extensions=[extension])


def active(end):
    return ipxact.ActiveInterface(component_instance_ref=end[0], bus_ref=end[1])


def link_to(name, first, *others):
    return ipxact.Interconnection(name=name, active_interface=first, other_active_interfaces=list(others))


def decoder_ip(first_mode="initiator", second_mode="initiator"):
    return component(
        "decoder",
        [wire("addr_i", "in", 32), wire("sel_o", "out", "NUM_RULES-1"), wire("x_o", "out"), wire("y_o", "out")],
        [bus("m0", first_mode, port_map("FLAG", "x_o")), bus("m1", second_mode, port_map("FLAG", "y_o"))],
        parameters={"NUM_RULES": 1, "ADDR_RULES": 0},
    )


def target_ip():
    return component("chosen", [wire("sel_i", "in"), wire("seen_o", "out")], [bus("b", "target", port_map("FLAG", "sel_i"))])


def two_windows(first=(0x1000, 0x1000), second=(0x2000, 0x1000)):
    return design(
        "soc",
        [instance("dec", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [
            link_to("l0", active(("dec", "m0")), window(("a", "b"), *first)),
            link_to("l1", active(("dec", "m1")), window(("b", "b"), *second)),
        ],
    )


def test_a_decoder_whose_module_parameters_have_ids_of_their_own(tmp_path):
    """Kactus2 writes module parameters with parameterIds of their own. The count still has to size the ports."""
    decoder = decoder_ip()
    for parameter in decoder.model.component_instantiations[0].module_parameters:
        parameter.parameter_id = f"mp_{parameter.name}"
    files = files_of(two_windows(), [decoder, target_ip()])
    assert "logic [1:0] dec_sel_o" in re.sub(" +", " ", files["soc.sv"])
    if has_icarus:
        assert "PASS" in simulate(tmp_path, files, [DECODER], DECODE_THE_SECOND)


def test_a_decoders_parameters_set_by_the_design_are_kept():
    decoder = decoder_ip()
    for parameter in decoder.model.component_instantiations[0].module_parameters:
        parameter.parameter_id = f"mp_{parameter.name}"
    soc = two_windows()
    soc.component_instances[0] = instance("dec", "decoder", NUM_RULES=1, ADDR_RULES=0)
    text = files_of(soc, [decoder, target_ip()])["soc.sv"]
    assert ".NUM_RULES(1)" in text and "DEC_ADDR_RULES" not in text


def test_a_design_setting_num_rules_to_another_count_is_refused():
    soc = two_windows()
    soc.component_instances[0] = instance("dec", "decoder", NUM_RULES=3)
    refused("sets NUM_RULES to 3, but the address map has 2 rules", soc, [decoder_ip(), target_ip()])


def test_a_design_setting_num_rules_to_the_tables_count_gets_the_table(tmp_path):
    soc = two_windows()
    soc.component_instances[0] = instance("dec", "decoder", NUM_RULES=2)
    files = files_of(soc, [decoder_ip(), target_ip()])
    assert ".ADDR_RULES(soc_addr_map_pkg::DEC_ADDR_RULES)" in files["soc.sv"]
    if has_icarus:
        assert "PASS" in simulate(tmp_path, files, [DECODER], DECODE_THE_SECOND)


def test_a_design_setting_only_the_table_is_refused():
    soc = two_windows()
    soc.component_instances[0] = instance("dec", "decoder", ADDR_RULES=0)
    refused("sets ADDR_RULES itself", soc, [decoder_ip(), target_ip()])


def test_a_package_file_named_like_another_module_is_refused():
    inner = hierarchical("x", "x_d", [wire("o", "out")])
    inner_d = design("x_d", [instance("dec", "decoder"), instance("a", "chosen")], [link_to("l", active(("dec", "m0")), window(("a", "b"), 0x1000, 0x1000))], [ad_hoc("o", ("dec", "y_o"), external=["o"]), ad_hoc("t", ("dec", "addr_i"), tied=0)])
    refused("two different files would both be called x_addr_map_pkg.sv", design("x_addr_map_pkg", [instance("i", "x")]), [inner, inner_d, decoder_ip(), target_ip()])


def test_an_inverted_tie_off_is_refused():
    soc = design("soc", [instance("m", "far_side"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    tied = tier(port_map("WORD", tie="'h3c"))
    tied.bus_interfaces[0].abstraction_types[0].port_maps[0].invert = True
    refused("ties 'WORD' off with invert", soc, [tied, seer()])


def test_a_port_sized_by_num_rules_gets_the_tables_count(tmp_path):
    files = files_of(two_windows(), [decoder_ip(), target_ip()])
    assert "soc_addr_map_pkg.sv" in files
    if has_icarus:
        assert "PASS" in simulate(tmp_path, files, [DECODER], DECODE_THE_SECOND)


def test_rule_indexes_count_a_decoders_interfaces_of_either_outbound_mode(tmp_path):
    files = files_of(two_windows(), [decoder_ip(second_mode="mirroredTarget"), target_ip()])
    assert "{32'd1, 32'h00002000, 32'h00003000}" in files["soc_addr_map_pkg.sv"]
    if has_icarus:
        assert "PASS" in simulate(tmp_path, files, [DECODER], DECODE_THE_SECOND)


def test_the_decoder_of_a_broadcast_gets_the_table_wherever_it_is_listed():
    soc = design(
        "soc",
        [instance("dec", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [link_to("l", active(("a", "b")), active(("dec", "m0")), window(("b", "b"), 0x2000, 0x1000))],
    )
    package = files_of(soc, [decoder_ip(), target_ip()])["soc_addr_map_pkg.sv"]
    assert "// Decoded by dec" in package and "// dec.m0 reaches b.b" in package


def test_a_window_among_targets_with_no_decoder_is_refused():
    soc = design(
        "soc",
        [instance("a", "chosen"), instance("b", "chosen"), instance("c", "chosen")],
        [link_to("l", active(("a", "b")), active(("c", "b")), window(("b", "b"), 0x2000, 0x1000))],
    )
    refused("cannot tell which of a.b, c.b", soc, [target_ip()])


def test_two_windows_behind_one_interface_get_their_own_constants():
    soc = design(
        "soc",
        [instance("dec", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [link_to("l", active(("dec", "m0")), window(("a", "b"), 0x1000, 0x1000), window(("b", "b"), 0x2000, 0x1000))],
    )
    files = files_of(soc, [decoder_ip(), target_ip()])
    assert "DEC_M0_A_B_BASE" in files["soc_addr_map_pkg.sv"] and "DEC_M0_B_B_BASE" in files["soc_addr_map_pkg.sv"]
    compiles(files, DECODER)


def test_instances_whose_constants_would_collide_are_refused():
    soc = design(
        "soc",
        [instance("dec", "decoder"), instance("DEC", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [link_to("l0", active(("dec", "m0")), window(("a", "b"), 0x1000, 0x1000)), link_to("l1", active(("DEC", "m0")), window(("b", "b"), 0x2000, 0x1000))],
    )
    refused("both define the constant", soc, [decoder_ip(), target_ip()])


def test_interfaces_whose_constants_would_collide_are_refused():
    two_cases = component(
        "decoder",
        [wire("x_o", "out"), wire("y_o", "out")],
        [bus("m0", "initiator", port_map("FLAG", "x_o")), bus("M0", "initiator", port_map("FLAG", "y_o"))],
    )
    soc = design(
        "soc",
        [instance("dec", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [link_to("l0", active(("dec", "m0")), window(("a", "b"), 0x1000, 0x1000)), link_to("l1", active(("dec", "M0")), window(("b", "b"), 0x2000, 0x1000))],
    )
    refused("both define the constant", soc, [two_cases, target_ip()])


def test_a_negative_window_is_refused():
    refused("has base -16", one_window(-16, 0x1000), [decoder_ip(), target_ip()])


def test_the_address_width_comes_from_the_views_abstraction():
    documents = address_abstraction("32", "32")
    narrow = ipxact.AbstractionDefinition(
        vlnv=ipxact.VLNV(VENDOR, "bus", "lbus16_rtl", "1.0"), bus_type=vlnv_ref("bus", "lbus"), ports=[logical("FLAG"), logical("ADDR", width=16)]
    )
    narrow.ports[1].wire.qualifier = ipxact.Qualifier(is_address=True)
    target = target_ip()
    alternate_view(target, "narrow")
    first = target.bus_interfaces[0].abstraction_types[0]
    first.view_refs = [ipxact.TextValue("rtl")]
    target.bus_interfaces[0].abstraction_types.append(
        ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "lbus16_rtl"), port_maps=first.port_maps, view_refs=[ipxact.TextValue("narrow")])
    )
    decoder = decoder_ip()
    decoder.bus_interfaces[0].abstraction_types[0].abstraction_ref = vlnv_ref("bus", "lbus16_rtl")
    refused("past the 16-bit address space", one_window(0x20000, 0x1000), [decoder, target], choose_view("narrow", "a"), buses=[*documents, narrow])


def test_an_instance_named_like_its_module_cannot_share_a_rule_table():
    chip = hierarchical("soc", "soc_d", [wire("f", "in"), wire("x_o", "out")], [bus("up", "target", port_map("FLAG", "f"))])
    soc_d = design(
        "soc_d",
        [instance("soc", "decoder"), instance("a", "chosen"), instance("b", "chosen")],
        [link_to("l0", active(("soc", "m0")), window(("a", "b"), 0x1000, 0x1000)), ipxact.Interconnection(name="l1", active_interface=window(("b", "b"), 0x8000, 0x1000), hier_interfaces=[ipxact.HierInterface(bus_ref="up")])],
    )
    refused("cannot tell their windows apart", soc_d, [chip, decoder_ip(), component("chosen", [wire("sel_i", "in")], [bus("b", "target", port_map("FLAG", "sel_i"))])])


def test_the_size_of_the_whole_address_space_is_not_zero():
    soc = design("soc", [instance("dec", "decoder"), instance("a", "chosen")], [link_to("l", active(("dec", "m0")), window(("a", "b"), 0, 1 << 32))])
    package = files_of(soc, [decoder_ip(), target_ip()])["soc_addr_map_pkg.sv"]
    assert "33'h100000000" in package


def address_abstraction(on_initiator=None, on_target=None, parameters=()):
    documents = bus_documents()
    address = next(p for p in documents[1].ports if p.logical_name == "ADDR")
    address.wire.on_initiator = ipxact.WireModeConstraints(direction="out", width=_E(on_initiator) if on_initiator else None)
    address.wire.on_target = ipxact.WireModeConstraints(direction="in", width=_E(on_target) if on_target else None)
    documents[1].parameters = list(parameters)
    return documents


def one_window(base, size):
    return design("soc", [instance("dec", "decoder"), instance("a", "chosen")], [link_to("l", active(("dec", "m0")), window(("a", "b"), base, size))])


def test_the_address_width_can_be_an_abstraction_parameter():
    documents = address_abstraction("AW", "AW", [ipxact.Parameter(name="AW", value=_E("64"), parameter_id="AW")])
    package = files_of(one_window(0x1000, 0x1000), [decoder_ip(), target_ip()], buses=documents)["soc_addr_map_pkg.sv"]
    assert "logic [63:0] start_addr" in package


def test_the_address_width_can_be_given_for_targets_only():
    documents = address_abstraction(on_target="16")
    refused("past the 16-bit address space", one_window(0x20000, 0x1000), [decoder_ip(), target_ip()], buses=documents)


def narrow_and_wide_buses(narrow_window):
    """A decoder with a 16-bit bus and a 32-bit one, so the package is 32 bits wide."""
    documents = address_abstraction("16", "16")
    wide_definition = ipxact.BusDefinition(vlnv=ipxact.VLNV(VENDOR, "bus", "wbus", "1.0"), direct_connection=True, is_addressable=True)
    wide_address = logical("ADDR", width=32)
    wide_address.wire.qualifier = ipxact.Qualifier(is_address=True)
    wide_abstraction = ipxact.AbstractionDefinition(vlnv=ipxact.VLNV(VENDOR, "bus", "wbus_rtl", "1.0"), bus_type=vlnv_ref("bus", "wbus"), ports=[wide_address, logical("FLAG")])

    def wide_bus(name, mode, *maps):
        return ipxact.BusInterface(
            name=name,
            bus_type=vlnv_ref("bus", "wbus"),
            mode=ipxact.InterfaceMode(mode),
            abstraction_types=[ipxact.AbstractionType(abstraction_ref=vlnv_ref("bus", "wbus_rtl"), port_maps=list(maps))],
        )

    two_buses = component("decoder", [wire("x_o", "out"), wire("y_o", "out")], [bus("m0", "initiator", port_map("FLAG", "x_o")), wide_bus("m1", "initiator", port_map("FLAG", "y_o"))])
    wide_target = component("wide_target", [wire("sel_i", "in")], [wide_bus("b", "target", port_map("FLAG", "sel_i"))], module="chosen")
    soc = design(
        "soc",
        [instance("dec", "decoder"), instance("a", "chosen"), instance("c", "wide_target")],
        [link_to("l0", active(("dec", "m0")), window(("a", "b"), *narrow_window)), link_to("l1", active(("dec", "m1")), window(("c", "b"), 0x1000, 0x1000))],
    )
    return soc, [two_buses, target_ip(), wide_target], [*documents, wide_definition, wide_abstraction]


def test_a_window_past_its_own_bus_is_refused_when_another_bus_is_wider():
    soc, documents, buses = narrow_and_wide_buses((0xFFFF0000, 0x1000))
    refused("past the 16-bit address space", soc, documents, buses=buses)


def test_a_window_at_the_top_of_a_narrower_bus_keeps_its_true_end_in_a_wider_table():
    """An end of 0 in a 32-bit table would claim every address above the window as well."""
    soc, documents, buses = narrow_and_wide_buses((0xF000, 0x1000))
    package = files_of(soc, documents, buses=buses)["soc_addr_map_pkg.sv"]
    assert "{32'd0, 32'h0000f000, 32'h00010000}" in package
    assert "{32'd1, 32'h00001000, 32'h00002000}" in package


def test_a_bus_that_does_not_say_its_width_is_not_held_to_another_buses_width():
    """Only the APB-RT side of a real SoC marks no address: its windows stay 32 bits next to any other bus."""
    soc, documents, buses = narrow_and_wide_buses((0xF000, 0x1000))
    wide = next(d for d in buses if d.vlnv.name == "wbus_rtl")
    next(p for p in wide.ports if p.logical_name == "ADDR").wire.qualifier = None
    soc.interconnections[1] = link_to("l1", active(("dec", "m1")), window(("c", "b"), 0x20000, 0x1000))
    package = files_of(soc, documents, buses=buses)["soc_addr_map_pkg.sv"]
    assert "32'h00020000" in package


def test_a_window_on_the_decoders_own_side_of_a_hierarchical_interface():
    chip = hierarchical("chip", "chip_d", [wire("f1", "out")], [bus("u1", "mirroredTarget", port_map("FLAG", "f1"))])
    two_out = component("decoder", [wire("x_o", "out"), wire("y_o", "out")], [bus("m0", "initiator", port_map("FLAG", "x_o")), bus("m1", "initiator", port_map("FLAG", "y_o"))])
    chip_d = design("chip_d", [instance("dec", "decoder")])
    chip_d.interconnections = [
        ipxact.Interconnection(name="l", active_interface=window(("dec", "m1"), 0x1000, 0x1000), hier_interfaces=[ipxact.HierInterface(bus_ref="u1")])
    ]
    package = files_of(chip_d, [chip, two_out])["chip_addr_map_pkg.sv"]
    assert "// dec.m1 reaches chip.u1" in package
    assert "{32'd1, 32'h00001000, 32'h00002000}" in package


def test_windows_reached_from_outside_are_counted_by_the_modules_own_interfaces():
    chip = hierarchical(
        "chip", "chip_d", [wire("f1", "in"), wire("f2", "in")], [bus("u1", "target", port_map("FLAG", "f1")), bus("u2", "target", port_map("FLAG", "f2"))]
    )
    chip_d = design("chip_d", [instance("a", "chosen"), instance("b", "chosen")])
    chip_d.interconnections = [
        ipxact.Interconnection(name="l1", active_interface=window(("a", "b"), 0x1000, 0x1000), hier_interfaces=[ipxact.HierInterface(bus_ref="u1")]),
        ipxact.Interconnection(name="l2", active_interface=window(("b", "b"), 0x2000, 0x1000), hier_interfaces=[ipxact.HierInterface(bus_ref="u2")]),
    ]
    package = files_of(chip_d, [chip, component("chosen", [wire("sel_i", "in")], [bus("b", "target", port_map("FLAG", "sel_i"))])])["chip_addr_map_pkg.sv"]
    assert "// Reached from outside chip" in package
    assert "{32'd1, 32'h00002000, 32'h00003000}" in package


# --- an unpacked array port of an instance can be left open


ARRAYED = (
    "module arr (input logic [7:0] in_i, output logic [7:0] v_o, output logic [7:0] a_o [4]);\n"
    "  assign v_o = in_i; assign a_o = '{default: 8'h0};\nendmodule\n"
)


def arrayed(array_direction="out"):
    """`arr` has an output v_o and a port a_o that is an unpacked array of four bytes."""
    array_port = wire("a_o", array_direction, 8)
    array_port.arrays = [ipxact.ArrayBound(left=_E("3"), right=_E("0"))]
    return component("arr", [wire("in_i", "in", 8), wire("v_o", "out", 8), array_port])


def chip_around_arr(*extra):
    chip = hierarchical("chip", "chip_d", [wire("i", "in", 8), wire("o", "out", 8)])
    connections = [ad_hoc("in", ("m", "in_i"), external=["i"]), ad_hoc("out", ("m", "v_o"), external=["o"]), *extra]
    return chip, design("chip_d", [instance("m", "arr")], ad_hocs=connections)


def test_an_unpacked_array_output_left_open_is_generated(tmp_path):
    chip, chip_d = chip_around_arr()
    files = files_of(chip_d, [chip, arrayed()])
    assert re.search(r"^\s*\.a_o\s*\(\)", files["chip.sv"], re.M)
    assert slang_errors(*files.values(), ARRAYED) == []
    if has_verilator:
        result = verilator_lint(tmp_path, files, [ARRAYED], "chip")
        assert result.returncode == 0, result.stderr


def test_a_connection_to_an_unpacked_array_port_is_refused():
    chip, chip_d = chip_around_arr()
    chip.model.ports.append(wire("x", "out", 8))
    chip_d.ad_hoc_connections.append(ad_hoc("x", ("m", "a_o"), external=["x"]))
    refused("m.a_o: unpacked array ports are not supported yet", chip_d, [chip, arrayed()])


def test_an_unpacked_array_input_is_refused():
    chip, chip_d = chip_around_arr()
    refused("m.a_o: unpacked array ports are not supported yet", chip_d, [chip, arrayed("in")])


def test_an_unpacked_array_port_that_would_become_a_module_port_is_refused():
    refused("m.a_o: unpacked array ports are not supported yet", design("soc", [instance("m", "arr")]), [arrayed()])


# --- D29: a port map with no logical range takes its port's own bits, from logical bit 0 (B.1.7 b))


def word_end(name, mode, direction, width, logical_range=None):
    port = "data_o" if direction == "out" else "data_i"
    return component(name, [wire(port, direction, width)], [bus("b", mode, port_map("WORD", port, logical_range=logical_range))])


def test_a_narrow_driver_leaves_bits_a_wider_receiver_reads_and_is_refused():
    soc = design("soc", [instance("m", "half"), instance("s", "seer")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused(r"s\.data_i reads bits \[4, 5, 6, 7\] of 'WORD', which nothing drives", soc, [word_end("half", "initiator", "out", 4), seer()])


def test_a_port_wider_than_its_logical_port_with_no_range_is_refused():
    soc = design("soc", [instance("m", "sender"), instance("s", "big")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused(r"s\.data_i is 16 bits, mapped to 'WORD' with no logical range.*gives it 8 \(B\.1\.7 b\), SCR 6\.19\)", soc, [sender(), word_end("big", "target", "in", 16)])


def test_a_logical_range_past_the_abstractions_width_is_refused():
    soc = design("soc", [instance("m", "sender"), instance("s", "high")], [interconnection("l", ("m", "b"), ("s", "b"))])
    refused(r"logical range \[11:8\] of 'WORD' on s\.data_i goes past its width in the abstraction, 8 bits \(SCR 6\.19\)", soc, [sender(), word_end("high", "target", "in", 4, (11, 8))])
