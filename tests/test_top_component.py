"""Hierarchical components, logical slices and inouts: what comes out, and what must be refused.

Behaviour in simulation is checked in test_scenarios.py. These are the rules around it.
"""

import dataclasses
import re

import ipxact
import pytest

from builders import (
    ad_hoc,
    bus,
    bus_documents,
    component,
    design,
    files_of,
    generate,
    hierarchical,
    instance,
    interconnection,
    library_of,
    logical,
    port_map,
    slang_errors,
    wire,
)
from test_scenarios import replier, sender, seer
from xactflow import link
from xactflow_rtl_gen import RtlExporter, generate_top

IO = [wire("in_i", "in", 8), wire("out_o", "out", 8)]


def wired():
    return [ad_hoc("a", ("s", "data_i"), external=["in_i"]), ad_hoc("b", ("s", "reply_o"), external=["out_o"])]


def test_module_name_and_ports_come_from_the_component():
    text = generate(
        design("chip_d", [instance("s", "replier")], ad_hocs=wired()),
        [hierarchical("chip", "chip_d", [wire("spare_i", "in"), *IO]), replier()],
    )["chip.sv"]
    assert "module chip (" in text
    assert "chip_d" not in text.split("\n", 2)[2]  # only the header names the design
    assert "input  logic       spare_i" in text  # a port nothing uses is still the module's port


def test_module_name_can_come_from_the_views_instantiation():
    top = hierarchical("chip", "chip_d", IO)
    top.model.views[0].component_instantiation_ref = "named"
    top.model.component_instantiations = [ipxact.ComponentInstantiation(name="named", module_name="chip_top")]
    files = generate(design("chip_d", [instance("s", "replier")], ad_hocs=wired()), [top, replier()])
    assert "module chip_top (" in files["chip_top.sv"]


def test_unconnected_input_is_refused_when_the_component_defines_the_ports():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=[ad_hoc("b", ("s", "reply_o"), external=["out_o"])])
    with pytest.raises(ValueError, match=r"s\.data_i is not connected.*list it in common"):
        generate(the_design, [hierarchical("chip", "chip_d", IO), replier()])


def default_replier():
    return component(
        "default_replier", [wire("data_i", "in", 8, default="'h3c"), wire("reply_o", "out", 8)], module="replier"
    )


def test_input_with_a_default_is_still_refused_when_the_component_defines_the_ports():
    """IEEE 1685-2022 7.5.5: a default isn't connected unless an ad hoc connection ties it."""
    the_design = design("chip_d", [instance("s", "default_replier")], ad_hocs=[ad_hoc("b", ("s", "reply_o"), external=["out_o"])])
    with pytest.raises(ValueError, match=r"s\.data_i is not connected.*'default' for its default value"):
        generate(the_design, [hierarchical("chip", "chip_d", IO), default_replier()])


def test_input_tied_to_its_default_is_allowed_when_the_component_defines_the_ports():
    connections = [ad_hoc("b", ("s", "reply_o"), external=["out_o"]), ad_hoc("t", ("s", "data_i"), tied="default")]
    the_design = design("chip_d", [instance("s", "default_replier")], ad_hocs=connections)
    assert "8'h3c" in generate(the_design, [hierarchical("chip", "chip_d", IO), default_replier()])["chip.sv"]


def receiver(port_default=None, map_as=port_map("TAGGED", "data_i")):
    return component("receiver", [wire("data_i", "in", 8, default=port_default)], [bus("b", "target", map_as)], module="seer")


def far_side():
    return component("far_side", [wire("flag_o", "out")], [bus("b", "initiator", port_map("FLAG", "flag_o"))])


def on_a_bus(*ties):
    return design(
        "soc", [instance("f", "far_side"), instance("s", "receiver")], [interconnection("c", ("f", "b"), ("s", "b"))], ties
    )


def test_tied_to_default_with_no_default_anywhere_is_refused():
    documents = [receiver(map_as=port_map("WORD", "data_i")), far_side()]
    with pytest.raises(ValueError, match=r"tied to 'default', but s\.data_i has no default value"):
        generate(on_a_bus(ad_hoc("t", ("s", "data_i"), tied="default")), documents)


def test_tied_to_default_when_the_bus_does_not_reach_it_ignores_the_abstraction_default():
    the_design = design("soc", [instance("s", "receiver")], ad_hocs=[ad_hoc("t", ("s", "data_i"), tied="default")])
    with pytest.raises(ValueError, match=r"tied to 'default', but s\.data_i has no default value"):
        generate(the_design, [receiver()])


def test_tying_a_port_to_its_default_twice_is_refused():
    ties = [ad_hoc("t", ("s", "data_i"), tied="default"), ad_hoc("u", ("s", "data_i"), tied="default")]
    with pytest.raises(ValueError, match=r"s\.data_i is already driven"):
        generate(on_a_bus(*ties), [receiver("'h3c"), far_side()])


def test_tying_part_of_a_port_over_its_abstraction_default_is_refused():
    with pytest.raises(ValueError, match=r"s\.data_i\[3:0\] is already driven"):
        generate(on_a_bus(ad_hoc("t", ("s", "data_i", (3, 0)), tied="default")), [receiver("'h3c"), far_side()])


def test_tying_over_an_abstraction_default_through_a_net_takes_the_ports_default():
    documents = [receiver("'h3c", port_map("TAGGED", "data_i", logical_range=(7, 0))), far_side()]
    text = generate(on_a_bus(ad_hoc("t", ("s", "data_i"), tied="default")), documents)["soc.sv"]
    assert "assign s_data_i = 8'h3c;" in re.sub(" +", " ", text)


def test_requires_driver_with_no_driver_is_not_left_floating():
    required = ipxact.AbstractionPort(
        logical_name="TAGGED", wire=ipxact.WireAbstractionPort(requires_driver=ipxact.RequiresDriver(value=True))
    )
    documents = bus_documents()
    documents[1].ports = [required if p.logical_name == "TAGGED" else p for p in documents[1].ports]
    the_design = on_a_bus()
    the_library = library_of([*documents, receiver(), far_side(), the_design])
    assert "input  logic [7:0] s_data_i" in generate_top(link(the_design, the_library))


def test_tied_open_input_is_allowed_when_the_component_defines_the_ports():
    connections = [ad_hoc("b", ("s", "reply_o"), external=["out_o"]), ad_hoc("o", ("s", "data_i"), tied="open")]
    text = generate(design("chip_d", [instance("s", "replier")], ad_hocs=connections), [hierarchical("chip", "chip_d", IO), replier()])
    assert ".data_i  ()" in text["chip.sv"]


def test_unconnected_output_is_left_open_when_the_component_defines_the_ports():
    connections = [ad_hoc("a", ("s", "data_i"), external=["in_i"])]
    top = hierarchical("chip", "chip_d", [wire("in_i", "in", 8)])
    text = generate(design("chip_d", [instance("s", "replier")], ad_hocs=connections), [top, replier()])["chip.sv"]
    assert ".reply_o ()" in text


def test_top_level_output_nothing_drives_is_refused():
    connections = [ad_hoc("a", ("s", "data_i"), external=["in_i"])]
    with pytest.raises(ValueError, match=r"chip\.out_o: top-level output bits .* have no driver"):
        generate(design("chip_d", [instance("s", "replier")], ad_hocs=connections), [hierarchical("chip", "chip_d", IO), replier()])


def test_external_port_the_component_lacks_is_refused():
    connections = [ad_hoc("a", ("s", "data_i"), external=["typo_i"])]
    with pytest.raises(ValueError, match=r"SCR 13\.5.*externalPortReference portRef 'typo_i' does not match a port on component .*chip"):
        generate(design("chip_d", [instance("s", "replier")], ad_hocs=connections), [hierarchical("chip", "chip_d", IO), replier()])


def test_hier_interface_the_component_lacks_is_refused():
    connection = interconnection("link", ("s", "b"), hier=["nope"])
    with pytest.raises(ValueError, match="hierInterface busRef 'nope' does not match a busInterface on component .*chip"):
        generate(design("chip_d", [instance("s", "replier")], [connection]), [hierarchical("chip", "chip_d", IO), replier()])


def test_module_parameters_of_the_top_component_become_the_modules_own():
    """More of it in test_module_parameters.py (D36)."""
    top = hierarchical("chip", "chip_d", IO)
    top.model.views[0].component_instantiation_ref = "named"
    top.model.component_instantiations = [
        ipxact.ComponentInstantiation(name="named", module_parameters=[ipxact.ModuleParameter(name="W", value=ipxact.Expression("8"))])
    ]
    text = generate(design("chip_d", [instance("s", "replier")], ad_hocs=wired()), [top, replier()])["chip.sv"]
    assert "module chip #(\n    parameter W = 8\n) (" in text and "if (W !== 8) begin : W_as_generated" in text


def test_an_unconnected_input_is_left_open_when_asked(tmp_path):
    """D38. The instance says so where the port is, and nothing else changes."""
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=[ad_hoc("b", ("s", "reply_o"), external=["out_o"])])
    documents = [hierarchical("chip", "chip_d", IO), replier()]
    text = files_of(the_design, documents, unconnected="open")["chip.sv"]
    assert re.search(r"\.data_i +\(\),?  // nothing in the design connects this input: left open \(unconnected=open\)", text)
    assert ".reply_o (out_o)" in text
    # It compiles, and a linter still points at the input.
    source = "module replier (input logic [7:0] data_i, output logic [7:0] reply_o); assign reply_o = ~data_i; endmodule\n"
    assert slang_errors(text, source) == ["DiagCode(EmptyInputPortConn)"]
    with pytest.raises(TypeError, match="the unconnected option is one of refuse, open, got 'float'"):
        files_of(the_design, documents, unconnected="float")


def test_an_output_of_the_module_nothing_drives_is_left_undriven_when_asked():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["in_i"])])
    documents = [hierarchical("chip", "chip_d", IO), replier()]
    with pytest.raises(ValueError, match=r"chip\.out_o: top-level output bits \[0, 1, 2, 3, 4, 5, 6, 7\] have no driver"):
        files_of(the_design, documents)
    text = files_of(the_design, documents, unconnected="open")["chip.sv"]
    assert "    // out_o: nothing in the design drives this output: left undriven (unconnected=open)\n" in text
    source = "module replier (input logic [7:0] data_i, output logic [7:0] reply_o); assign reply_o = ~data_i; endmodule\n"
    assert slang_errors(text, source) == []
    # Driven in part is a mistake in the design, whatever the option says.
    reply = ad_hoc("b", ("s", "reply_o", (3, 0)), external=[("out_o", (3, 0))])
    in_part = design("chip_d", [instance("s", "replier")], ad_hocs=[ad_hoc("a", ("s", "data_i"), external=["in_i"]), reply])
    with pytest.raises(ValueError, match=r"top-level output bits \[4, 5, 6, 7\] have no driver"):
        files_of(in_part, documents, unconnected="open")


def test_leaving_inputs_open_reaches_the_modules_inside_and_no_further():
    inner = hierarchical("inner", "inner_d", [wire("out_o", "out", 8)])
    inner_d = design("inner_d", [instance("s", "replier")], ad_hocs=[ad_hoc("b", ("s", "reply_o"), external=["out_o"])])
    outer_d = design("chip_d", [instance("i", "inner")], ad_hocs=[ad_hoc("b", ("i", "out_o"), external=["out_o"])])
    documents = [hierarchical("chip", "chip_d", IO), inner, inner_d, replier()]
    with pytest.raises(ValueError, match=r"s\.data_i is not connected.*or leave every such input open with the option unconnected=open"):
        files_of(outer_d, documents)
    files = files_of(outer_d, documents, unconnected="open")
    assert "left open (unconnected=open)" in files["inner.sv"] and "left open" not in files["chip.sv"]
    # Without a component to define the ports, an input nothing connects still becomes a port of the module.
    alone = files_of(design("soc", [instance("s", "replier")]), [replier()], unconnected="open")["soc.sv"]
    assert "input  logic [7:0] s_data_i" in alone and "left open" not in alone


def test_an_input_connected_in_part_is_still_refused_when_inputs_may_be_left_open():
    the_design = design(
        "chip_d",
        [instance("s", "replier")],
        ad_hocs=[ad_hoc("a", ("s", "data_i", (3, 0)), external=[("in_i", (3, 0))]), ad_hoc("b", ("s", "reply_o"), external=["out_o"])],
    )
    with pytest.raises(ValueError, match=r"s\.data_i: bits \[4, 5, 6, 7\] are not driven by any connection"):
        files_of(the_design, [hierarchical("chip", "chip_d", IO), replier()], unconnected="open")


def test_two_components_holding_the_design_need_the_top_option():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=wired())
    documents = [hierarchical("chip_a", "chip_d", IO), hierarchical("chip_b", "chip_d", IO), replier()]
    with pytest.raises(ValueError, match="more than one component holds design.*top option"):
        generate(the_design, documents)
    assert "module chip_b (" in generate(the_design, documents, top="test.org:ip:chip_b:1.0")["chip_b.sv"]


def test_top_option_must_hold_the_design():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=wired())
    documents = [hierarchical("chip", "other_d", IO), replier()]
    with pytest.raises(ValueError, match="has no view that holds design"):
        generate(the_design, documents, top="test.org:ip:chip:1.0")
    with pytest.raises(ValueError, match="is not in the library"):
        generate(the_design, documents, top="test.org:ip:nope:1.0")


def test_a_broken_component_elsewhere_in_the_library_does_not_matter():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=wired())
    documents = [hierarchical("chip", "chip_d", IO), hierarchical("broken", "missing_d", IO), replier()]
    assert "module chip (" in generate(the_design, documents)["chip.sv"]


def test_sub_design_gets_a_file_of_its_own():
    inner = hierarchical("inner", "inner_d", IO)
    inner_design = design("inner_d", [instance("s", "replier")], ad_hocs=wired())
    top = hierarchical("chip", "chip_d", IO)
    connections = [ad_hoc("a", ("i", "in_i"), external=["in_i"]), ad_hoc("b", ("i", "out_o"), external=["out_o"])]
    files = generate(design("chip_d", [instance("i", "inner")], ad_hocs=connections), [top, inner, inner_design, replier()])
    assert list(files) == ["chip.sv", "inner.sv"]
    assert "inner i (" in files["chip.sv"]
    assert "replier s (" in files["inner.sv"]


def test_design_that_contains_itself_is_refused():
    loop = hierarchical("loop", "loop_d", IO)
    connections = [ad_hoc("a", ("i", "in_i"), external=["in_i"]), ad_hoc("b", ("i", "out_o"), external=["out_o"])]
    with pytest.raises(ValueError, match="contains itself"):
        generate(design("loop_d", [instance("i", "loop")], ad_hocs=connections), [loop])


def test_parameter_override_on_a_hierarchical_instance_must_name_one_of_its_parameters():
    inner = hierarchical("inner", "inner_d", IO)
    inner_design = design("inner_d", [instance("s", "replier")], ad_hocs=wired())
    with pytest.raises(ValueError, match="parameter overrides name no parameter of test.org:ip:inner:1.0: W"):
        generate(design("chip_d", [instance("i", "inner", W=8)]), [inner, inner_design, replier()])


def test_design_configuration_selects_the_view_of_an_instance():
    both = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)], [bus("b", "target", port_map("WORD", "data_i"))])
    both.model.views.append(ipxact.View(name="fast", component_instantiation_ref="fast_inst"))
    both.model.component_instantiations.append(ipxact.ComponentInstantiation(name="fast_inst", module_name="seer_fast"))
    the_design = design("soc", [instance("s", "seer")])
    configuration = ipxact.DesignConfiguration(
        vlnv=ipxact.VLNV("test.org", "design", "cfg", "1.0"),
        view_configurations=[ipxact.ViewConfiguration(instance_name="s", view_ref="fast")],
    )
    library = library_of([*bus_documents(), both, the_design])
    assert "seer s (" in generate_top(link(the_design, library))
    assert "seer_fast s (" in generate_top(link(the_design, library, configuration))


def nibble_seer(*maps):
    return component("nibble_seer", [wire("hi_i", "in", 4), wire("lo_i", "in", 4), wire("seen_o", "out", 8)], [bus("b", "target", *maps)])


def nibbles(*maps):
    return component("nibbles", [wire("hi_o", "out", 4), wire("lo_o", "out", 4)], [bus("b", "initiator", *maps)])


CONNECTED = design("soc", [instance("m", "nibbles"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))])


def test_logical_bits_nothing_drives_are_refused():
    only_low = nibbles(port_map("WORD", "lo_o", logical_range=(3, 0)))
    with pytest.raises(ValueError, match=r"s\.data_i reads bits \[4, 5, 6, 7\] of 'WORD', which nothing drives"):
        generate(CONNECTED, [only_low, seer()])


def test_overlapping_logical_drivers_are_refused():
    both_low = nibbles(port_map("WORD", "hi_o", logical_range=(3, 0)), port_map("WORD", "lo_o", logical_range=(3, 0)))
    with pytest.raises(ValueError, match="link_WORD.* is already driven"):
        generate(CONNECTED, [both_low, seer()])


def test_logical_slice_of_the_wrong_width_is_refused():
    wrong = nibbles(port_map("WORD", "hi_o", logical_range=(7, 2)))
    with pytest.raises(ValueError, match="width mismatch"):
        generate(CONNECTED, [wrong, seer()])


def test_part_select_outside_the_port_is_refused():
    outside = component("sender", [wire("data_o", "out", 8)], [bus("b", "initiator", port_map("WORD", "data_o", select=(11, 4)))], module_parameters=[])
    the_design = design("soc", [instance("m", "sender"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))])
    with pytest.raises(ValueError, match=r"part select \[11:4\] is outside port m\.data_o"):
        generate(the_design, [outside, seer()])


def pad(name="pad_ip", width=8):
    return component(name, [wire("pad", "inout", width)], module=name)


def test_unconnected_inout_becomes_an_inout_top_level_port():
    text = generate(design("soc", [instance("p", "pad_ip")]), [pad()])["soc.sv"]
    assert "inout  wire [7:0] p_pad" in text
    assert ".pad (p_pad)" in text


def test_inout_part_select_is_refused():
    connection = ad_hoc("a", ("p", "pad", (3, 0)), ("q", "pad", (3, 0)))
    with pytest.raises(ValueError, match="a part select on inout p.pad is not supported yet"):
        generate(design("soc", [instance("p", "pad_ip"), instance("q", "pad_ip")], ad_hocs=[connection]), [pad()])


def test_inout_of_another_width_is_refused():
    connection = ad_hoc("a", ("p", "pad"), ("q", "pad"))
    with pytest.raises(ValueError, match="width mismatch, inout"):
        generate(design("soc", [instance("p", "pad_ip"), instance("q", "tiny_pad")], ad_hocs=[connection]), [pad(), pad("tiny_pad", 4)])


def test_inout_on_two_connections_is_refused():
    connections = [ad_hoc("a", ("p", "pad"), ("q", "pad")), ad_hoc("b", ("p", "pad"), ("r", "pad"))]
    the_design = design("soc", [instance("p", "pad_ip"), instance("q", "pad_ip"), instance("r", "pad_ip")], ad_hocs=connections)
    with pytest.raises(ValueError, match="inout p.pad is already connected to a"):
        generate(the_design, [pad()])


def test_instance_shows_its_vlnv_description_and_bus_interfaces():
    the_instance = dataclasses.replace(instance("s", "seer"), description="Sees the word\nSecond line")
    the_design = design("soc", [instance("m", "sender"), the_instance], [interconnection("link", ("m", "b"), ("s", "b"))])
    text = generate(the_design, [sender(), seer()])["soc.sv"]
    assert "    // IP-XACT VLNV: test.org:ip:seer:1.0\n    // Sees the word\n    // Second line\n    seer s (" in text
    assert "        // Interface: b\n        .data_i (s_data_i),\n        // Not in any interface\n        .seen_o (s_seen_o)\n" in text


def test_exporter_writes_every_file_or_none(tmp_path):
    inner = hierarchical("inner", "inner_d", IO)
    inner_design = design("inner_d", [instance("s", "replier")], ad_hocs=wired())
    top = hierarchical("chip", "chip_d", IO)
    connections = [ad_hoc("a", ("i", "in_i"), external=["in_i"]), ad_hoc("b", ("i", "out_o"), external=["out_o"])]
    the_design = design("chip_d", [instance("i", "inner")], ad_hocs=connections)
    linked = link(the_design, library_of([*bus_documents(), top, inner, inner_design, replier(), the_design]))

    (tmp_path / "inner.sv").write_text("module inner; endmodule // written by a person\n")
    with pytest.raises(FileExistsError):
        RtlExporter().export(linked, tmp_path)
    assert not (tmp_path / "chip.sv").exists()

    (tmp_path / "inner.sv").unlink()
    RtlExporter().export(linked, tmp_path, top="test.org:ip:chip:1.0")
    assert (tmp_path / "chip.sv").exists() and (tmp_path / "inner.sv").exists()


# --- the component that holds the design, as link() takes it since XactFlow 0.4.0 (D44)


def test_the_top_option_also_chooses_the_component_link_checks_against():
    """With two components holding the design link() resolves no hierInterface by itself: it is given the one chosen."""
    up = bus("up", "target", port_map("WORD", "in_i"), port_map("REPLY", "out_o"))
    the_design = design("chip_d", [instance("s", "replier")], [interconnection("link", ("s", "b"), hier=["up"])])
    documents = [hierarchical("chip_a", "chip_d", IO, [up]), hierarchical("chip_b", "chip_d", IO), replier()]
    text = generate(the_design, documents, top="test.org:ip:chip_a:1.0")["chip_a.sv"]
    assert "module chip_a (" in text and ".data_i (in_i)" in re.sub(r" +", " ", text)
    with pytest.raises(ValueError, match="hierInterface busRef 'up' does not match a busInterface on component .*chip_b"):
        generate(the_design, documents, top="test.org:ip:chip_b:1.0")


def test_the_component_given_to_link_is_the_top_component_unless_the_top_option_names_another():
    the_design = design("chip_d", [instance("s", "replier")], ad_hocs=wired())
    chip_a, chip_b = hierarchical("chip_a", "chip_d", IO), hierarchical("chip_b", "chip_d", IO)
    library = library_of([*bus_documents(), chip_a, chip_b, replier(), the_design])
    assert "module chip_b (" in generate_top(link(the_design, library, component=chip_b))
    assert "module chip_a (" in generate_top(link(the_design, library, component=chip_b), top="test.org:ip:chip_a:1.0")


# --- names as xactflow-archidesc writes them: <name>.component holds <name>.design (D45)


def test_a_component_named_with_the_suffix_gives_a_module_without_it():
    top = hierarchical("chip.component", "chip.design", IO)
    files = generate(design("chip.design", [instance("s", "replier")], ad_hocs=wired()), [top, replier()])
    assert list(files) == ["chip.sv"]
    assert "module chip (" in files["chip.sv"]
    assert slang_errors(files["chip.sv"], "module replier (input logic [7:0] data_i, output logic [7:0] reply_o); endmodule") == []


def test_a_design_named_with_the_suffix_and_no_component_gives_a_module_without_it():
    the_design = design("soc.design", [instance("m", "sender"), instance("s", "seer")], [interconnection("link", ("m", "b"), ("s", "b"))])
    assert "module soc (" in generate(the_design, [sender(), seer()])["soc.sv"]


def test_a_hierarchical_instance_named_with_the_suffix_is_the_module_without_it():
    inner = hierarchical("inner.component", "inner.design", IO)
    inner_design = design("inner.design", [instance("s", "replier")], ad_hocs=wired())
    connections = [ad_hoc("a", ("i", "in_i"), external=["in_i"]), ad_hoc("b", ("i", "out_o"), external=["out_o"])]
    documents = [hierarchical("chip.component", "chip.design", IO), inner, inner_design, replier()]
    files = generate(design("chip.design", [instance("i", "inner.component")], ad_hocs=connections), documents)
    assert list(files) == ["chip.sv", "inner.sv"]
    assert "inner i (" in files["chip.sv"]
    assert "module inner (" in files["inner.sv"]


def test_the_module_name_of_the_view_comes_before_the_components_name():
    top = hierarchical("chip.component", "chip.design", IO)
    top.model.views[0].component_instantiation_ref = "named"
    top.model.component_instantiations = [ipxact.ComponentInstantiation(name="named", module_name="chip_top")]
    files = generate(design("chip.design", [instance("s", "replier")], ad_hocs=wired()), [top, replier()])
    assert list(files) == ["chip_top.sv"]


@pytest.mark.parametrize(
    "component_name, design_name, message",
    [
        ("chip.design", "chip_d", "module name 'chip.design' is not a valid SystemVerilog identifier"),  # a design's suffix on a component
        ("a.b.component", "chip_d", "module name 'a.b' is not a valid SystemVerilog identifier"),
        (".component", "chip_d", "module name '.component' is not a valid SystemVerilog identifier"),
        (None, "chip.component", "design name 'chip.component' is not a valid SystemVerilog identifier"),
        (None, ".design", "design name '.design' is not a valid SystemVerilog identifier"),
    ],
)
def test_only_its_own_suffix_is_taken_off_a_name(component_name, design_name, message):
    documents = [replier()] if component_name is None else [hierarchical(component_name, design_name, IO), replier()]
    connections = [ad_hoc("b", ("s", "reply_o"), external=["out_o"])] if component_name else []
    with pytest.raises(ValueError, match=re.escape(message)):
        files_of(design(design_name, [instance("s", "replier")], ad_hocs=connections), documents, unconnected="open")
