"""Parameters of a generated module (D36), and values on a reference to a design configuration (D37).

A hierarchical component can describe a module with parameters, as `xactflow-sv` imports one: each a parameter
of the component, and a module parameter that names it. The generated module declares them, so that what
instantiates it by hand still compiles, with the one value it is generated for, and has the compiler refuse
any other. They are declared with no type (D41).
"""

import subprocess

import ipxact
import pytest

from builders import (
    VENDOR,
    ad_hoc,
    component,
    design,
    files_of,
    has_icarus,
    has_verilator,
    hierarchical,
    icarus_runs_elaboration_tasks_in_generate_blocks,
    in_compile_order,
    instance,
    passes,
    refused,
    simulation_says,
    slang_errors,
    vlnv_ref,
    wire,
    without_elaboration_checks,
)

_E = ipxact.Expression

SIZED = "module sized #(parameter int WIDTH = 8) (output logic [WIDTH-1:0] data_o); assign data_o = '1; endmodule\n"


def sized():
    return component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})


def parameter(name, value):
    return ipxact.Parameter(name=name, value=_E(str(value)), parameter_id=name, resolve="user")


def with_module(holder, module_parameters, name="rtl"):
    """The view that holds the design also describes the module, with these module parameters."""
    holder.model.views[0].component_instantiation_ref = name
    holder.model.component_instantiations = [
        ipxact.ComponentInstantiation(name=name, module_name=holder.vlnv.name, module_parameters=list(module_parameters))
    ]
    return holder


def holder(name, parameters, data_type="int unsigned"):
    """A hierarchical component as the importer writes one: an output `o` as wide as its parameter W, which its
    design gives to m, a `sized`."""
    made = hierarchical(name, f"{name}_d", [wire("o", "out", "W-1")])
    made.parameters = [parameter(key, value) for key, value in parameters.items()]
    with_module(made, [ipxact.ModuleParameter(name=key, value=_E(key), data_type=data_type) for key in parameters])
    inside = design(f"{name}_d", [instance("m", "sized", WIDTH="W")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    return made, inside


def by_hand(module, parameters, width=16):
    """What a person writes around a generated module: an instance that sets its parameters."""
    return f"module by_hand (output logic [{width - 1}:0] o); {module} #({parameters}) u (.o(o)); endmodule\n"


def stops_every_tool(tmp_path, files, source, top="by_hand"):
    """An elaboration the generated module's own check has to stop, in each tool that runs such checks."""
    ordered = [text for _, text in in_compile_order(files)]
    assert any("ElabTask" in code or "StaticAssert" in code or "Error" in code for code in slang_errors(*ordered, source)), slang_errors(*ordered, source)
    paths = []
    for name, text in [*in_compile_order(files), ("by_hand.sv", source)]:
        (tmp_path / name).write_text(text)
        paths.append(str(tmp_path / name))
    if has_verilator:
        result = subprocess.run(["verilator", "--lint-only", "--top-module", top, *paths], capture_output=True, text=True)
        assert result.returncode != 0 and "is generated for" in result.stderr, result.stderr
    if has_icarus and icarus_runs_elaboration_tasks_in_generate_blocks(tmp_path):
        result = subprocess.run(["iverilog", "-g2012", "-s", top, "-o", str(tmp_path / "x.vvp"), *paths], capture_output=True, text=True)
        assert result.returncode != 0 and "is generated for" in result.stderr + result.stdout, result.stderr


def compiles_everywhere(tmp_path, files, source, top="by_hand"):
    ordered = [text for _, text in in_compile_order(files)]
    assert slang_errors(*ordered, source) == []
    paths = []
    for name, text in [*in_compile_order(files), ("by_hand.sv", source)]:
        (tmp_path / name).write_text(text)
        paths.append(str(tmp_path / name))
    if has_verilator:
        result = subprocess.run(["verilator", "--lint-only", "--top-module", top, *paths], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    if has_icarus:
        result = subprocess.run(["iverilog", "-g2012", "-s", top, "-o", str(tmp_path / "x.vvp"), *paths], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


# --- D36: the module declares its parameters


def test_the_module_declares_its_parameters_with_the_values_it_is_generated_for(tmp_path):
    chip, chip_d = holder("chip", {"W": 16})
    files = passes(tmp_path, chip_d, [chip, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)})
    text = files["chip.sv"]
    assert "module chip #(\n    parameter W = 16\n) (\n    output logic [15:0] o\n);" in text
    assert "    if (W !== 16) begin : W_as_generated\n" in text
    assert '$error("chip is generated for W = 16, and was given another value");' in text
    assert ".WIDTH(16)" in text.replace(" ", "")


def test_an_instance_written_by_hand_with_that_value_compiles(tmp_path):
    chip, chip_d = holder("chip", {"W": 16})
    files = files_of(chip_d, [chip, sized()])
    compiles_everywhere(tmp_path, {**files, "sized.sv": SIZED}, by_hand("chip", ".W(16)"))


@pytest.mark.parametrize("given", [".W(8)", ".W(17)", ".W(16'hx)"])
def test_an_instance_written_by_hand_with_another_value_is_stopped_by_the_compiler(tmp_path, given):
    chip, chip_d = holder("chip", {"W": 16})
    files = files_of(chip_d, [chip, sized()])
    stops_every_tool(tmp_path, {**files, "sized.sv": SIZED}, by_hand("chip", given))


@pytest.mark.skipif(not has_icarus, reason="needs Icarus Verilog")
def test_a_tool_that_goes_on_past_the_check_stops_when_its_simulation_starts(tmp_path):
    """D40. Taking the $error line out is how the Icarus at hand sees what Icarus 12 and `verilator -Wno-fatal`
    do with it. The line under it then stops the simulation, with the same message."""
    chip, chip_d = holder("chip", {"W": 16})
    files = without_elaboration_checks(files_of(chip_d, [chip, sized()]))
    assert "$error(" not in files["chip.sv"] and "initial $fatal(1, " in files["chip.sv"]
    bench = 'module tb; wire [15:0] o; by_hand dut (.o(o)); initial begin #1; $display("ran on, o=%h", o); $finish; end endmodule\n'
    stopped = simulation_says(tmp_path, files, [SIZED, by_hand("chip", ".W(8)")], bench)
    assert "chip is generated for W = 16, and was given another value" in stopped and "ran on" not in stopped
    assert "ran on, o=ffff" in simulation_says(tmp_path, files, [SIZED, by_hand("chip", ".W(16)")], bench)


def test_a_module_with_parameters_and_no_ports(tmp_path):
    chip = with_module(hierarchical("chip", "chip_d", []), [ipxact.ModuleParameter(name="DEPTH", value=_E("4"))])
    files = files_of(design("chip_d", [instance("m", "sized")]), [chip, sized()], unconnected="open")
    assert "module chip #(\n    parameter DEPTH = 4\n) ();" in files["chip.sv"]
    assert slang_errors(files["chip.sv"], SIZED) == []


def test_the_users_guides_parameter_passing_example(tmp_path):
    """Accellera's IP-XACT User Guide, Example 67: A's parameters reach B's module parameter through the
    design's own, and A's module parameter pA is the first of them."""
    b = component("B", [], parameters={"id_B": 1}, module_parameters=[])
    b.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="pB", value=_E("id_B"))]
    a = hierarchical("A", "A_design", [])
    a.parameters = [parameter("id_A1", 3), parameter("id_A2", 4)]
    with_module(a, [ipxact.ModuleParameter(name="pA", value=_E("id_A1"))])
    a.model.design_instantiations[0].design_ref = vlnv_ref("design", "A_design", id_A3="id_A1*id_A2")
    a_design = design("A_design", [instance("u_B", "B", id_B="id_A3+7")], parameters=[parameter("id_A3", 1)])
    files = files_of(a_design, [a, b])
    text = files["A.sv"]
    assert "module A #(\n    parameter pA = 3\n) ();" in text
    assert ".pB(19)" in text
    assert slang_errors(text, "module B #(parameter pB = 1) (); endmodule\n") == []


def test_a_hierarchical_instance_is_given_the_values_its_module_is_generated_for(tmp_path):
    wrap, wrap_d = holder("wrap", {"W": 8})
    soc = design("soc", [instance("a", "wrap", W=16), instance("b", "wrap")])
    files = passes(tmp_path, soc, [wrap, wrap_d, sized()], SIZED, expect={"a_o": (16, 0xFFFF), "b_o": (8, 0xFF)})
    assert sorted(files) == ["soc.sv", "wrap.sv", "wrap_1.sv"]
    top = files["soc.sv"].replace(" ", "")
    assert "wrap_1#(\n.W(16)\n)a(" in top and "wrap#(\n.W(8)\n)b(" in top
    assert "    parameter W = 16\n" in files["wrap_1.sv"] and "    parameter W = 8\n" in files["wrap.sv"]
    assert "if (W !== 16) begin : W_as_generated" in files["wrap_1.sv"]


def test_an_override_can_name_a_module_parameter_of_a_hierarchical_component(tmp_path):
    """A module parameter with an id of its own, set on the instance, as one of a leaf can be."""
    wrap = hierarchical("wrap", "wrap_d", [wire("o", "out", "id_w-1")])
    with_module(wrap, [ipxact.ModuleParameter(name="pW", value=_E("8"), parameter_id="id_w", resolve="user")])
    wrap_d = design("wrap_d", [], ad_hocs=[ad_hoc("o", external=["o"], tied="'1")])
    soc = design("soc", [instance("w", "wrap", id_w=12)])
    files = passes(tmp_path, soc, [wrap, wrap_d], "", expect={"w_o": (12, 0xFFF)})
    assert "module wrap_1 #(\n    parameter pW = 12\n) (\n    output logic [11:0] o\n);" in files["wrap_1.sv"]
    assert "wrap_1#(\n.pW(12)\n)w(" in files["soc.sv"].replace(" ", "")


def test_a_parameter_the_hdl_would_derive_is_declared_with_its_value(tmp_path):
    """On an instance of an IP such a parameter is left to the HDL (D19). The generated module is the HDL."""
    chip, chip_d = holder("chip", {"W": 16, "HALF": "W / 2"})
    files = passes(tmp_path, chip_d, [chip, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)})
    assert "parameter W = 16,\n    parameter HALF = 8\n" in files["chip.sv"]
    outer = design("soc", [instance("c", "chip")])
    files = passes(tmp_path, outer, [chip, chip_d, sized()], SIZED, expect={"c_o": (16, 0xFFFF)})
    assert "chip#(\n.W(16)\n)c(" in files["soc.sv"].replace(" ", "")


@pytest.mark.parametrize(
    "data_type, value, declared, check",
    [
        (None, "8'hA5", "parameter P = 8'hA5", "P !== 8'hA5"),
        ("int unsigned", "W * 2", "parameter P = 32", "P !== 32"),
        ("logic [W-1:0]", "'1", "parameter P = '1", "P !== '1"),
        ("bit   signed [3:0]", "-4'sd2", "parameter P = -4'sd2", "P !== (-4'sd2)"),
        ("string", '"fast"', 'parameter P = "fast"', 'P != "fast"'),
        ("real", "1.5", "parameter P = 1.5", "P != 1.5"),
        ("real", "2", "parameter P = 2", "P != 2"),
        (None, "2.5e3", "parameter P = 2.5e3", "P != 2.5e3"),
        # Only a typedef named with its package is kept: the check casts the value to it, as the declaration does.
        ("ip_types_pkg::mode_t", "ip_types_pkg::FAST", "parameter ip_types_pkg::mode_t P = ip_types_pkg::FAST", "P !== ip_types_pkg::mode_t'(ip_types_pkg::FAST)"),
        ("ip_types_pkg::word_t", "16'hFFFE", "parameter ip_types_pkg::word_t P = 16'hFFFE", "P !== ip_types_pkg::word_t'(16'hFFFE)"),
        ("ip_types_pkg::pair_t", "ip_types_pkg::PAIR", "parameter ip_types_pkg::pair_t P = ip_types_pkg::PAIR", "P !== ip_types_pkg::pair_t'(ip_types_pkg::PAIR)"),
        # A type the module could not see, or that is no type at all, is not written, so it does no harm.
        ("mode_t", "1", "parameter P = 1", "P !== 1"),
        ("int = 2, parameter Q", "1", "parameter P = 1", "P !== 1"),
        # Round 9: with its type written, each of these was converted by the declaration, and the module
        # refused its own value. `int` is what the importer writes for a parameter with no type.
        ("int", '"XILINX"', 'parameter P = "XILINX"', 'P != "XILINX"'),
        ("int", "2.5", "parameter P = 2.5", "P != 2.5"),
        ("int", "64'hFFFF_FFFF_0000_0001", "parameter P = 64'hFFFF_FFFF_0000_0001", "P !== 64'hFFFF_FFFF_0000_0001"),
        ("int", "5000000000", "parameter P = 34'sd5000000000", "P !== 34'sd5000000000"),
        ("int", "'x", "parameter P = 'x", "P !== 'x"),
        ("logic [15:0]", "-2", "parameter P = -2", "P !== (-2)"),
        ("bit [7:0]", "-1", "parameter P = -1", "P !== (-1)"),
        ("logic [15:0]", "8'shFF", "parameter P = 8'shFF", "P !== 8'shFF"),
        # Verilog-2001's parameters with a range and no type, as the importer writes them.
        ("[7:0]", "8'hF0", "parameter P = 8'hF0", "P !== 8'hF0"),
        ("signed[7:0]", "-8'sd3", "parameter P = -8'sd3", "P !== (-8'sd3)"),
    ],
)
def test_each_kind_of_parameter_is_declared_and_checked(tmp_path, data_type, value, declared, check):
    """The module compiles in the three tools and simulates with its own values, and so does a design the
    exporter generates around it, which gives it those values on the instance."""
    chip, chip_d = holder("chip", {"W": 16})
    chip.model.component_instantiations[0].module_parameters.append(ipxact.ModuleParameter(name="P", value=_E(value), data_type=data_type))
    packages = (
        "package ip_types_pkg;\n    typedef enum logic [1:0] {SLOW, FAST} mode_t;\n    typedef logic [15:0] word_t;\n"
        "    typedef struct packed { logic [3:0] a; logic [3:0] b; } pair_t;\n    localparam pair_t PAIR = 8'hA5;\nendpackage\n"
    )
    files = passes(tmp_path, chip_d, [chip, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)}, packages=packages)
    assert f"    {declared}\n" in files["chip.sv"] and "parameter Q" not in files["chip.sv"]
    assert f"    if ({check}) begin : P_as_generated\n" in files["chip.sv"]
    around = design("soc", [instance("c", "chip")])
    passes(tmp_path, around, [chip, chip_d, sized()], SIZED, expect={"c_o": (16, 0xFFFF)}, packages=packages)


def test_a_runtime_parameter_is_not_declared():
    chip, chip_d = holder("chip", {"W": 16})
    chip.model.component_instantiations[0].module_parameters.append(ipxact.ModuleParameter(name="SEED", value=_E("1"), usage_type="runtime"))
    assert "SEED" not in files_of(chip_d, [chip, sized()])["chip.sv"]


def test_the_checks_label_gives_way_to_a_name_in_the_module(tmp_path):
    chip, chip_d = holder("chip", {"W": 16})
    chip_d.component_instances[0].instance_name = "W_as_generated"
    chip_d.ad_hoc_connections = [ad_hoc("o", ("W_as_generated", "data_o"), external=["o"])]
    files = passes(tmp_path, chip_d, [chip, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)})
    assert "begin : W_as_generated1\n" in files["chip.sv"]


@pytest.mark.parametrize(
    "module_parameter, message",
    [
        (ipxact.ModuleParameter(name="T", value=_E("logic"), data_type="type"), "module parameter T: a type parameter on a generated module is not supported yet"),
        (ipxact.ModuleParameter(name="P", value=_E("'{1, 2}"), data_type="ip_types_pkg::pair_t"), "cannot be compared with another"),
        (ipxact.ModuleParameter(name="o", value=_E("1")), "a port and a module parameter both called 'o'"),
        (ipxact.ModuleParameter(name="m", value=_E("1")), "instance 'm' has the name of a parameter of module chip"),
        (ipxact.ModuleParameter(name="W", value=_E("2")), "two module parameters of that name"),
        (ipxact.ModuleParameter(name="P", value=_E("NOPE + 1")), "uses NOPE, which no parameter here is called"),
    ],
)
def test_what_a_generated_module_cannot_declare_is_refused(module_parameter, message):
    chip, chip_d = holder("chip", {"W": 16})
    chip.model.component_instantiations[0].module_parameters.append(module_parameter)
    refused(message, chip_d, [chip, sized()])


def test_a_net_gives_way_to_a_parameter_of_the_module(tmp_path):
    """Instance m's port data_o would have the net m_data_o: the parameter of that name keeps it."""
    chip, chip_d = holder("chip", {"W": 16})
    chip.model.component_instantiations[0].module_parameters.append(ipxact.ModuleParameter(name="m_data_o", value=_E("1")))
    chip_d.component_instances.append(instance("s", "seer16"))
    chip_d.ad_hoc_connections = [ad_hoc("a", ("m", "data_o"), ("s", "data_i")), ad_hoc("b", ("s", "seen_o"), external=["o"])]
    seer16 = component("seer16", [wire("data_i", "in", 16), wire("seen_o", "out", 16)])
    source = SIZED + "module seer16 (input logic [15:0] data_i, output logic [15:0] seen_o); assign seen_o = data_i; endmodule\n"
    files = passes(tmp_path, chip_d, [chip, sized(), seer16], source, module="chip", expect={"o": (16, 0xFFFF)})
    assert "m_data_o_net;" in files["chip.sv"]


def test_a_value_written_over_two_lines_stays_in_its_comment_and_its_message(tmp_path):
    """Round 9. A comment and a string end where their line does, and an expression in XML need not."""
    chip, chip_d = holder("chip", {"W": "8 +\n 8"})
    files = passes(tmp_path, chip_d, [chip, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)})
    assert '$error("chip is generated for W = 8 + 8, and was given another value");' in files["chip.sv"]
    wrap, wrap_d = holder("wrap", {"W": 8})
    soc = design("soc", [instance("a", "wrap", W="8 +\n 8")])
    files = passes(tmp_path, soc, [wrap, wrap_d, sized()], SIZED, expect={"a_o": (16, 0xFFFF)})
    assert "// The version of test.org:ip:wrap:1.0 with W = 8 + 8.\n" in files["wrap_1.sv"]


@pytest.mark.parametrize(
    "first, second",
    [
        ({"W": 16}, {"W": 16, "D": 2}),  # one also states a default
        ({"W": 16, "D": 3}, {"D": 3, "W": 16}),  # in another order
        ({"W": 16}, {"W": "8+8"}),  # written another way
        ({"W": 16}, {"W": "DP"}),  # through a parameter of the design
    ],
)
def test_instances_with_the_same_values_share_a_module_however_they_wrote_them(tmp_path, first, second):
    """Round 9: the module's second line named the values as each instance wrote them, so two instances that
    gave the same ones another way were refused as two different files of one name."""
    wrap, wrap_d = holder("wrap", {"W": 8, "D": 2})
    soc = design("soc", [instance("a", "wrap", **first), instance("b", "wrap", **second)], parameters=[parameter("DP", "8 + 8")])
    files = passes(tmp_path, soc, [wrap, wrap_d, sized()], SIZED, expect={"a_o": (16, 0xFFFF), "b_o": (16, 0xFFFF)})
    assert sorted(files) == ["soc.sv", "wrap_1.sv"]


# --- D37: the values a component's reference to its design configuration gives that one's parameters


def configured_holder(name, default, **configuration_values):
    """A hierarchical component with a parameter CW and an output of that width, whose view names a design and
    a design configuration. The configuration has a parameter CP, 8 by default, and gives it to m as its WIDTH.
    `configuration_values` are what the component's reference to the configuration sets."""
    made = hierarchical(name, f"{name}_d", [wire("o", "out", "CW-1")])
    made.parameters = [parameter("CW", default)]
    made.model.views[0].design_configuration_instantiation_ref = "dc"
    made.model.design_configuration_instantiations = [
        ipxact.DesignConfigurationInstantiation(name="dc", design_configuration_ref=vlnv_ref("design", f"{name}_cfg", **configuration_values))
    ]
    inside = design(f"{name}_d", [instance("m", "sized")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    configuration = ipxact.DesignConfiguration(
        vlnv=ipxact.VLNV(VENDOR, "design", f"{name}_cfg", "1.0"),
        design_ref=vlnv_ref("design", f"{name}_d"),
        view_configurations=[ipxact.ViewConfiguration(instance_name="m", view_ref="rtl", configurable_element_values={"WIDTH": _E("CP")})],
        parameters=[parameter("CP", 8)],
    )
    return made, inside, configuration


def test_the_reference_sets_the_design_configurations_parameters(tmp_path):
    """The user's guide's Example 68: a component's value reaches a module parameter through its design
    configuration's parameter."""
    chip, chip_d, configuration = configured_holder("chip", 16, CP="CW")
    files = passes(tmp_path, chip_d, [chip, configuration, sized()], SIZED, module="chip", expect={"o": (16, 0xFFFF)})
    assert ".WIDTH(16)" in files["chip.sv"].replace(" ", "")


def test_without_the_reference_the_configurations_own_value_stays(tmp_path):
    chip, chip_d, configuration = configured_holder("chip", 8)
    passes(tmp_path, chip_d, [chip, configuration, sized()], SIZED, module="chip", expect={"o": (8, 0xFF)})


def test_a_sub_designs_configuration_takes_the_values_of_its_instance(tmp_path):
    wrap, wrap_d, configuration = configured_holder("wrap", 8, CP="CW + 1")
    wrap.model.ports = [wire("o", "out", "CW")]
    soc = design("soc", [instance("a", "wrap", CW=11), instance("b", "wrap")])
    files = passes(tmp_path, soc, [wrap, wrap_d, configuration, sized()], SIZED, expect={"a_o": (12, 0xFFF), "b_o": (9, 0x1FF)})
    assert ".WIDTH(12)" in files["wrap_1.sv"].replace(" ", "") and ".WIDTH(9)" in files["wrap.sv"].replace(" ", "")


def test_a_reference_setting_a_parameter_the_configuration_lacks_is_refused():
    chip, chip_d, configuration = configured_holder("chip", 16, NOPE="CW")
    refused("its reference to the design configuration: parameter overrides name no parameter of that design configuration: NOPE", chip_d, [chip, configuration, sized()])


def test_a_configuration_value_naming_nothing_in_the_component_is_refused():
    chip, chip_d, configuration = configured_holder("chip", 16, CP="NOPE")
    refused("the value for design configuration parameter CP: 'NOPE' uses NOPE", chip_d, [chip, configuration, sized()])


def test_values_for_one_configuration_do_not_go_to_another():
    """The design is linked with a configuration of the caller's, and the component's values are for its own."""
    chip, chip_d, configuration = configured_holder("chip", 16, CP="CW")
    other = ipxact.DesignConfiguration(vlnv=ipxact.VLNV(VENDOR, "design", "other_cfg", "1.0"), design_ref=vlnv_ref("design", "chip_d"))
    refused("sets CP, and the design is configured by another one", chip_d, [chip, configuration, other, sized()], configuration=other)
