"""The netlist by itself: what a module is made of, before it is text.

The generator's tests check the SystemVerilog written from it, in real tools. These hold what the netlist
promises to anything else that reads it: a printer for another language, or a comparison of two designs.
"""

import ipxact
import pytest
from xactflow import link

from builders import ad_hoc, bus_documents, component, design, hierarchical, instance, library_of, slang_errors, wire
from test_scenarios import (
    by_name,
    inverted_between_instances,
    plain_master_to_slave,
    replier,
    tie_offs_for_logical_slices,
)
from xactflow_rtl_gen import generate_files, generate_netlists, systemverilog
from xactflow_rtl_gen.netlist import IN, OUT, Assign, Block, Constant, Module, Net, Parameter, Port, Reference, Select, SignalType

_E = ipxact.Expression
BYTE = SignalType("logic", ((7, 0),))


def linked_of(the_design, documents):
    return link(the_design, library_of([*bus_documents(), *documents, the_design]))


def modules_of(scenario):
    return generate_netlists(linked_of(scenario.design, scenario.documents), scenario.common)


@by_name
def test_the_systemverilog_is_the_netlist_written_out(scenario):
    linked = linked_of(scenario.design, scenario.documents)
    files = generate_files(linked, scenario.common)
    modules = generate_netlists(linked, scenario.common)
    assert [f"{name}.sv" for name in modules] == [name for name in files if not name.endswith("_pkg.sv")]
    for name, module in modules.items():
        assert module.name == name and systemverilog.render(module) == files[f"{name}.sv"]


def test_a_bus_connection_is_an_assign_from_the_driving_net_to_the_receiving_one():
    soc = modules_of(plain_master_to_slave())["soc"]
    assert [(port.name, port.direction, port.type) for port in soc.ports] == [("s_seen_o", OUT, BYTE)]
    assert [(net.name, net.type) for net in soc.nets] == [("m_data_o", BYTE), ("s_data_i", BYTE)]
    [block] = soc.blocks
    assert block.comment == ["link: m.b <-> s.b (test.org:bus:lbus_rtl:1.0)"]
    [assign] = block.assigns
    assert (assign.sink, assign.source, assign.note) == (Reference("s_data_i"), Reference("m_data_o"), "WORD")

    sender, seer = soc.instances
    assert (sender.name, sender.module, sender.parameters) == ("m", "sender", [])
    assert sender.comments == ["IP-XACT VLNV: test.org:ip:sender:1.0"]
    by_interface = [(group.heading, [(c.port, c.net) for c in group.connections]) for group in seer.groups]
    assert by_interface == [("Interface: b", [("data_i", "s_data_i")]), ("Not in any interface", [("seen_o", "s_seen_o")])]
    assert [c.port for c in seer.connections] == ["data_i", "seen_o"]


def test_an_inverted_port_map_is_said_on_the_driving_end():
    [block] = modules_of(inverted_between_instances())["soc"].blocks
    assert block.assigns[0].source == Reference("m_data_o", invert=True)


def test_a_tie_off_is_a_number_and_a_slice_is_its_bounds():
    [block] = modules_of(tie_offs_for_logical_slices())["soc"].blocks
    high, low, whole = block.assigns
    assert (high.sink, high.source) == (Reference("l_WORD", Select((7, 4))), Constant(10, 4))
    assert (low.sink, low.source) == (Reference("l_WORD", Select((3, 0))), Constant(5, 4))
    assert (whole.sink, whole.source) == (Reference("s_data_i"), Reference("l_WORD"))
    assert not Select() and str(Select((7, 4))) == "[7:4]" and str(Select(element=3, path=".addr")) == "[3].addr"


def test_a_constant_into_a_type_only_the_compiler_knows_asks_for_a_cast_and_a_check():
    moded = component("moded", [wire("mode_i", "in", type_name="mode_pkg::mode_e")])
    soc = design("soc", [instance("s", "moded")], ad_hocs=[ad_hoc("t", ("s", "mode_i"), tied=2)])
    [block] = generate_netlists(linked_of(soc, [moded]))["soc"].blocks
    [assign] = block.assigns
    assert assign.source == Constant(2, None) and assign.cast == "mode_pkg::mode_e"
    assert assign.check.bits == 2 and "the constant 2 does not fit in s.mode_i, a mode_pkg::mode_e" in assign.check.message


def test_a_port_left_open_has_no_net_and_what_is_left_undriven_is_noted():
    chip = hierarchical("chip", "chip_d", [wire("in_i", "in", 8), wire("spare_o", "out", 8)])
    chip_d = design("chip_d", [instance("s", "replier")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["in_i"])])
    module = generate_netlists(linked_of(chip_d, [chip, replier()]), unconnected="open")["chip"]
    assert [(port.name, port.direction) for port in module.ports] == [("in_i", IN), ("spare_o", OUT)]
    [s] = module.instances
    assert [(c.port, c.net) for c in s.connections] == [("data_i", "in_i"), ("reply_o", None)]
    assert module.notes == ["spare_o: nothing in the design drives this output: left undriven (unconnected=open)"]


def sized_in_a_wrap():
    """A hierarchical component `wrap` with a parameter W, which its module declares, around a `sized` of W bits."""
    wrap = hierarchical("wrap", "wrap_d", [wire("o", "out", "W-1")], module="wrap")
    wrap.parameters = [ipxact.Parameter(name="W", value=_E("8"), parameter_id="W", resolve="user")]
    wrap.model.component_instantiations[0].module_parameters = [ipxact.ModuleParameter(name="W", value=_E("W"))]
    wrap_d = design("wrap_d", [instance("m", "sized", WIDTH="W")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    return [wrap, wrap_d, component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})]


def test_each_module_of_a_hierarchy_has_a_netlist_and_says_which_values_it_is_for():
    soc = design("soc", [instance("a", "wrap", W=16), instance("b", "wrap")])
    modules = generate_netlists(linked_of(soc, sized_in_a_wrap()))
    assert list(modules) == ["soc", "wrap_1", "wrap"]
    assert [(i.name, i.module, i.parameters) for i in modules["soc"].instances] == [("a", "wrap_1", [("W", "16")]), ("b", "wrap", [("W", "8")])]
    assert modules["wrap_1"].remarks == ["The version of test.org:ip:wrap:1.0 with W = 16."] and modules["wrap"].remarks == []
    assert modules["wrap_1"].parameters == [Parameter("W", "16")] and modules["wrap"].parameters == [Parameter("W", "8")]
    assert modules["wrap_1"].ports == [Port("o", OUT, SignalType("logic", ((15, 0),)))]
    assert modules["wrap_1"].source == "test.org:design:wrap_d:1.0"


def test_what_cannot_be_wired_is_refused_before_there_is_a_netlist():
    soc = design("soc", [instance("a", "replier"), instance("b", "replier")], ad_hocs=[ad_hoc("x", ("a", "reply_o"), ("b", "reply_o"))])
    with pytest.raises(ValueError, match="needs exactly one driver"):
        generate_netlists(linked_of(soc, [replier()]))
    with pytest.raises(TypeError, match="the unconnected option is one of refuse, open"):
        generate_netlists(linked_of(design("soc", []), []), unconnected="maybe")


def test_the_printer_writes_a_netlist_made_by_hand():
    module = Module(
        name="tiny",
        source="a test",
        parameters=[],
        ports=[Port("a_i", IN, SignalType("logic", ((3, 0),))), Port("b_o", OUT, BYTE), Port("ones_o", OUT, BYTE)],
        nets=[Net("n", BYTE)],
        blocks=[
            Block(
                ["made by hand"],
                [
                    Assign(Reference("n", Select((3, 0))), Reference("a_i", invert=True), note="the low half"),
                    Assign(Reference("n", Select((7, 4))), Constant(0xA, 4)),
                    Assign(Reference("b_o"), Reference("n")),
                    Assign(Reference("ones_o"), Constant(-1)),
                ],
            )
        ],
        instances=[],
    )
    text = systemverilog.render(module)
    assert text.splitlines()[0].startswith("// Generated by xactflow-rtl-gen ") and text.splitlines()[0].endswith(" from a test. Do not edit.")
    assert "    assign n[3:0] = ~a_i;  // the low half\n    assign n[7:4] = 4'ha;\n    assign b_o    = n;\n    assign ones_o = '1;\n" in text
    assert slang_errors(text) == []


def test_another_printer_needs_nothing_but_the_netlist():
    """What a printer for another language would do, as far as the assigns go: nothing of SystemVerilog is read."""

    def part(reference):
        select = reference.select
        if select.bounds is not None:
            return f"{reference.net}({select.bounds[0]} downto {select.bounds[1]}){select.path}"
        return reference.net + (f"({select.element})" if select.element is not None else "") + select.path

    def value(source):
        if isinstance(source, Constant):
            all_ones = source.width is None and source.value == -1
            return "(others => '1')" if all_ones else f"std_logic_vector(to_unsigned({source.value}, {source.width}))"
        return ("not " if source.invert else "") + part(source)

    def assigns(module):
        return [f"{part(assign.sink)} <= {value(assign.source)};" for block in module.blocks for assign in block.assigns]

    assert assigns(modules_of(tie_offs_for_logical_slices())["soc"]) == [
        "l_WORD(7 downto 4) <= std_logic_vector(to_unsigned(10, 4));",
        "l_WORD(3 downto 0) <= std_logic_vector(to_unsigned(5, 4));",
        "s_data_i <= l_WORD;",
    ]
    assert assigns(modules_of(inverted_between_instances())["soc"]) == ["s_data_i <= not m_data_o;"]
