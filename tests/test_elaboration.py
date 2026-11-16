"""The elaboration by itself: the views, values and hierarchy of a design, with no module generated.

The generator's tests run all of this as well, through the modules it writes. These hold what the interface
promises to anything else that uses it.
"""

import ipxact
import pytest
from xactflow import evaluate, link

from builders import VENDOR, ad_hoc, component, design, hierarchical, instance, library_of, vlnv_ref, wire
from xactflow_rtl_gen import generate_top
from xactflow_rtl_gen.elaboration import elaborate

_E = ipxact.Expression


def parameter(name, value):
    return ipxact.Parameter(name=name, value=_E(str(value)), parameter_id=name, resolve="user")


def sized():
    """An IP whose output is WIDTH bits, 8 unless a design says otherwise."""
    return component("sized", [wire("data_o", "out", "WIDTH-1")], parameters={"WIDTH": 8})


def wrap():
    """A hierarchical component with a parameter W, whose design gives W to the `sized` inside it."""
    holder = hierarchical("wrap", "wrap_d", [wire("o", "out", "W-1")])
    holder.parameters = [parameter("W", 8)]
    inside = design("wrap_d", [instance("m", "sized", WIDTH="W")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    return holder, inside


def elaborated(the_design, documents, configuration=None, **options):
    return elaborate(link(the_design, library_of([*documents, the_design]), configuration), **options)


def by_name(elaborated_design):
    return {an_instance.name: an_instance for an_instance in elaborated_design.instances()}


def test_an_instance_has_the_values_the_design_gives_it():
    soc = design("soc", [instance("a", "sized", WIDTH=16), instance("b", "sized")])
    instances = by_name(elaborated(soc, [sized()]))
    assert instances["a"].number("WIDTH") == 16 and instances["b"].number("WIDTH") == 8
    assert instances["a"].overrides == {"WIDTH": "16"} and instances["b"].overrides == {}
    left_bound = instances["a"].component.model.ports[0].wire.vectors[0].left
    assert instances["a"].number(left_bound) == 15 and instances["b"].number(left_bound) == 7  # WIDTH-1, in each


def test_a_value_is_read_where_it_is_written():
    """A value on an instance is written in the design, so it sees the design's parameters."""
    soc = design("soc", [instance("a", "sized", WIDTH="WIDE + 4")], parameters=[parameter("WIDE", 12)])
    [a] = elaborated(soc, [sized()]).instances()
    assert a.number("WIDTH") == 16


def test_the_design_configuration_selects_the_view_and_its_values_come_last():
    both = sized()
    both.model.views.append(ipxact.View(name="fast", component_instantiation_ref="fast_inst"))
    both.model.component_instantiations.append(ipxact.ComponentInstantiation(name="fast_inst", module_name="sized_fast"))
    soc = design("soc", [instance("a", "sized", WIDTH=16)])
    configuration = ipxact.DesignConfiguration(
        vlnv=ipxact.VLNV(VENDOR, "design", "cfg", "1.0"),
        view_configurations=[ipxact.ViewConfiguration(instance_name="a", view_ref="fast", configurable_element_values={"WIDTH": _E("4")})],
    )
    [plain] = elaborated(soc, [both]).instances()
    assert plain.view.name == "rtl" and plain.instantiation.module_name == "sized" and plain.view_configuration is None
    assert plain.number("WIDTH") == 16
    [configured] = elaborated(soc, [both], configuration).instances()
    assert configured.view.name == "fast" and configured.instantiation.module_name == "sized_fast"
    assert configured.view_configuration.view_ref == "fast" and configured.number("WIDTH") == 4


def test_the_component_that_holds_the_design_is_found_and_gives_the_design_its_values():
    """A component's reference to its design sets the design's parameters, read in the component (D25)."""
    chip = hierarchical("chip", "chip_d", [wire("o", "out", "CW-1")])
    chip.parameters = [parameter("CW", 16)]
    chip.model.design_instantiations[0].design_ref = vlnv_ref("design", "chip_d", DW="CW")
    connections = [ad_hoc("o", ("m", "data_o"), external=["o"])]
    chip_d = design("chip_d", [instance("m", "sized", WIDTH="DW")], ad_hocs=connections, parameters=[parameter("DW", 8)])
    top = elaborated(chip_d, [chip, sized()])
    assert top.component.vlnv == chip.vlnv and top.view.name == "structural" and top.path == ()
    assert evaluate("DW", top.design_resolve) == 16 and evaluate("CW", top.resolve) == 16
    [m] = top.instances()
    assert m.number("WIDTH") == 16


def test_two_components_holding_the_design_need_one_named():
    chip_d = design("chip_d", [instance("m", "sized")], ad_hocs=[ad_hoc("o", ("m", "data_o"), external=["o"])])
    documents = [hierarchical("chip_a", "chip_d", [wire("o", "out", 8)]), hierarchical("chip_b", "chip_d", [wire("o", "out", 8)]), sized()]
    with pytest.raises(ValueError, match="more than one component holds design.*top option"):
        elaborated(chip_d, documents)
    assert elaborated(chip_d, documents, top="test.org:ip:chip_b:1.0").component.vlnv.name == "chip_b"


def test_descend_elaborates_the_design_inside_an_instance_for_its_values():
    holder, inside = wrap()
    soc = design("soc", [instance("w", "wrap", W=16), instance("plain", "sized")])
    top = elaborated(soc, [holder, inside, sized()])
    instances = by_name(top)
    assert top.component is None  # no component holds soc: it is elaborated on its own
    assert top.held_by(instances["plain"]) is None and top.descend(instances["plain"]) is None
    assert top.held_by(instances["w"]).design.vlnv == inside.vlnv

    below = top.descend(instances["w"])
    assert below.linked.vlnv == inside.vlnv and below.component.vlnv == holder.vlnv and below.view.name == "structural"
    assert below.path == (str(soc.vlnv),) and below.overrides == {"W": "16"}
    [m] = below.instances()
    assert m.number("WIDTH") == 16  # W, as the instance above gave it

    [unchanged] = top.descend(instances["w"], overrides={}).instances()
    assert unchanged.number("WIDTH") == 8  # the component's own value, for an instance that gives none


def test_instances_whose_values_come_to_the_same_have_one_variant():
    holder, inside = wrap()
    the_instances = [instance("a", "wrap", W=16), instance("b", "wrap", W="8+8"), instance("c", "wrap", W=8), instance("d", "wrap"), instance("e", "wrap", W=4)]
    instances = by_name(elaborated(design("soc", the_instances), [holder, inside, sized()]))
    assert instances["a"].variant() is not None
    assert instances["a"].variant() == instances["b"].variant() != instances["e"].variant()
    assert instances["c"].variant() is None and instances["d"].variant() is None  # values that change nothing


def test_a_generator_can_give_an_instance_one_more_value():
    [a] = elaborated(design("soc", [instance("a", "sized")]), [sized()]).instances()
    more = a.with_values({"WIDTH": "32"})
    assert more.number("WIDTH") == 32 and a.number("WIDTH") == 8
    assert a.with_values({}) is a


def test_a_design_that_contains_itself_is_refused_on_the_way_down():
    loop = hierarchical("loop", "loop_d", [])
    top = elaborated(design("loop_d", [instance("i", "loop")]), [loop])
    [inner] = top.instances()
    with pytest.raises(ValueError, match="design test.org:design:loop_d:1.0 contains itself"):
        top.descend(inner)


def test_what_cannot_be_elaborated_is_refused():
    with pytest.raises(ValueError, match="has link errors"):
        elaborated(design("soc", [instance("a", "missing")]), [])
    with pytest.raises(ValueError, match="parameter overrides name no parameter of test.org:ip:sized:1.0: NOPE"):
        by_name(elaborated(design("soc", [instance("a", "sized", NOPE=1)]), [sized()]))["a"].variant()
    with pytest.raises(ValueError, match="instance 'a': cannot evaluate"):
        by_name(elaborated(design("soc", [instance("a", "sized")]), [sized()]))["a"].number("NOPE + 1")


def test_the_generator_writes_what_the_elaboration_works_out():
    soc = design("soc", [instance("a", "sized", WIDTH="WIDE + 4")], parameters=[parameter("WIDE", 12)])
    linked = link(soc, library_of([sized(), soc]))
    [a] = elaborate(linked).instances()
    assert f"logic [{a.number('WIDTH') - 1}:0] a_data_o" in generate_top(linked)
