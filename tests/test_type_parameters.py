"""Type parameters, and ports that are arrays of structs: what a hand-written SoC of PULP IPs brought (D31, D32).

A PULP IP takes the types of its bus as `parameter type`, and a crossbar's ports are packed arrays of those
structs, wired one element, or one member of an element, at a time. The components here are written the way
xactflow-sv imports such modules, and the last tests go through the importer itself.
"""

import ipxact
import pytest
from xactflow import Library

from builders import (
    ad_hoc,
    bounds,
    bus,
    component,
    design,
    files_of,
    hierarchical,
    icarus_reads,
    instance,
    interconnection,
    passes,
    port_map,
    refused,
    struct,
    wire,
)

_E = ipxact.Expression

PACKAGE = """
package t_pkg;
    typedef struct packed { logic [3:0] hi; logic [3:0] lo; } pair_t;
    function automatic int unsigned twice(input int unsigned n);
        return n * 2;
    endfunction
endpackage
"""
MODULES = {
    "typed_src": """
module typed_src #(parameter type data_t = logic, parameter data_t VALUE = '0) (output data_t data_o);
    assign data_o = VALUE;
endmodule
""",
    "typed_dst": """
module typed_dst #(parameter type data_t = logic) (input data_t data_i, output logic [7:0] seen_o);
    assign seen_o = 8'(data_i);
endmodule
""",
    "sized_dst": """
module sized_dst #(parameter int unsigned W = 4, parameter type data_t = logic [W-1:0]) (input data_t data_i, output logic [7:0] seen_o);
    assign seen_o = 8'(data_i);
endmodule
""",
    "follow": """
module follow #(parameter type a_t = logic, parameter type b_t = a_t) (input a_t a_i, output b_t b_o);
    assign b_o = a_i;
endmodule
""",
    "scaled": """
module scaled #(parameter int unsigned W = 4) (output logic [7:0] data_o);
    assign data_o = 8'(W);
endmodule
""",
    "pairs_src": """
module pairs_src (output t_pkg::pair_t [1:0] pairs_o);
    assign pairs_o = 16'h3ca5;
endmodule
""",
    "quads_src": """
module quads_src (output t_pkg::pair_t [3:0] quads_o);
    assign quads_o = 32'h11223344;
endmodule
""",
    "pairs_dst": """
module pairs_dst (input t_pkg::pair_t [1:0] pairs_i, output logic [15:0] seen_o);
    assign seen_o = pairs_i;
endmodule
""",
    "pair_dst": """
module pair_dst (input t_pkg::pair_t pair_i, output logic [7:0] seen_o);
    assign seen_o = pair_i;
endmodule
""",
    "fan": """
module fan #(parameter int unsigned N = 2, parameter type req_t = logic) (input req_t req_i, output req_t [N-1:0] reqs_o);
    for (genvar i = 0; i < N; i++) begin : gen_out
        assign reqs_o[i] = (i == 0) ? req_i : ~req_i;
    end
endmodule
""",
    "sender": "module sender (output logic [7:0] data_o); assign data_o = 8'ha5; endmodule\n",
    "nibs": "module nibs (output logic [3:0] x_o); assign x_o = 4'h9; endmodule\n",
    "nib": "module nib (input logic [3:0] x_i, output logic [3:0] seen_o); assign seen_o = x_i; endmodule\n",
    "seer": "module seer (input logic [7:0] data_i, output logic [7:0] seen_o); assign seen_o = data_i; endmodule\n",
    "two_bytes": "module two_bytes (output logic [7:0] a_o, output logic [7:0] b_o); assign a_o = 8'h3c; assign b_o = 8'ha5; endmodule\n",
}
SOURCE = "".join(MODULES.values())
PAIR = "t_pkg::pair_t"
PROBE = PACKAGE + MODULES["typed_src"] + MODULES["pairs_dst"] + "module probe; t_pkg::pair_t [1:0] p; assign p[0].hi = 4'h1; endmodule\n"


@pytest.fixture
def run(tmp_path):
    """`passes`, with the package compiled first, and no simulation where the Icarus at hand cannot read type
    parameters or arrays of structs."""
    simulation = icarus_reads(tmp_path, PROBE)

    def check(the_design, documents, source=SOURCE, **options):
        return passes(tmp_path, the_design, documents, source, packages=PACKAGE, simulation=simulation, **options)

    return check


def sv_component(name, ports, parameters=None, buses=()):
    """A component as xactflow-sv imports a module: every parameter of the module is a module parameter that
    names the component parameter holding its default. `parameters` gives each one's (data type, default)."""
    parameters = parameters or {}
    built = component(name, ports, buses, parameters={key: default for key, (_, default) in parameters.items()})
    for module_parameter in built.model.component_instantiations[0].module_parameters:
        module_parameter.data_type = parameters[module_parameter.name][0]
    return built


def typed(name, direction, type_name, vectors=(), **members):
    """A port of a named type. xactflow-sv leaves its packed dimensions in the type's name; `vectors` gives them
    as IP-XACT has them."""
    port = struct(name, direction, type_name, **members)
    port.structured.vectors = [ipxact.Vector(_E(str(left)), _E(str(right))) for left, right in vectors]
    return port


def typed_src():
    return sv_component("typed_src", [typed("data_o", "out", "data_t")], {"data_t": ("type", "logic"), "VALUE": ("data_t", "'0")})


def typed_dst():
    return sv_component("typed_dst", [typed("data_i", "in", "data_t"), wire("seen_o", "out", 8)], {"data_t": ("type", "logic")})


def sized_dst():
    parameters = {"W": ("int unsigned", "4"), "data_t": ("type", "logic[W-1:0]")}
    return sv_component("sized_dst", [typed("data_i", "in", "data_t"), wire("seen_o", "out", 8)], parameters)


def sender():
    return component("sender", [wire("data_o", "out", 8)])


def nib():
    return component("nib", [wire("x_i", "in", 4), wire("seen_o", "out", 4)])


def nibs():
    return component("nibs", [wire("x_o", "out", 4)])


def pairs_src(dimensions_in_the_name=True, **members):
    if dimensions_in_the_name:
        return component("pairs_src", [typed("pairs_o", "out", f"{PAIR}[1:0]", **members)])
    return component("pairs_src", [typed("pairs_o", "out", PAIR, [(1, 0)], **members)])


def pairs_dst(**members):
    return component("pairs_dst", [typed("pairs_i", "in", f"{PAIR}[1:0]", **members), wire("seen_o", "out", 16)])


def pair_dst():
    return component("pair_dst", [typed("pair_i", "in", PAIR), wire("seen_o", "out", 8)])


def reference(instance_name, port, select=None, *members):
    return ipxact.InternalPortReference(
        component_instance_ref=instance_name,
        port_ref=port,
        part_select=ipxact.PartSelect(range=bounds(*select)) if select else None,
        sub_port_refs=[ipxact.SubPortReference(sub_port_ref=member) for member in members],
    )


def connection(name, *references, tied=None, external=()):
    return ipxact.AdHocConnection(
        name=name,
        internal_port_references=list(references),
        external_port_references=list(external),
        tied_value=_E(str(tied)) if tied is not None else None,
    )


# --- type parameters (D31)


def test_a_port_takes_the_type_its_instance_gives_a_type_parameter(run):
    soc = design(
        "soc",
        [instance("m", "typed_src", data_t=PAIR, VALUE="8'ha5"), instance("t", "typed_dst", data_t=PAIR)],
        ad_hocs=[ad_hoc("c", ("m", "data_o"), ("t", "data_i"))],
    )
    text = run(soc, [typed_src(), typed_dst()], expect={"t_seen_o": (8, 0xA5)})["soc.sv"]
    assert "t_pkg::pair_t m_data_o;" in text
    assert ".data_t(t_pkg::pair_t)" in text


def test_a_type_parameter_left_at_logic_makes_the_port_one_bit(run):
    soc = design("soc", [instance("m", "typed_src", VALUE="1'b1")])
    text = run(soc, [typed_src()], expect={"m_data_o": (1, 1)})["soc.sv"]
    assert "output logic m_data_o" in text

    soc = design("soc", [instance("m", "typed_src"), instance("n", "nib")], ad_hocs=[ad_hoc("c", ("m", "data_o"), ("n", "x_i"))])
    refused(r"width mismatch, m\.data_o is 1 bits and n\.x_i is 4 bits", soc, [typed_src(), nib()])


def test_a_type_parameter_worked_out_from_another_parameter_sizes_the_port(run):
    soc = design("soc", [instance("m", "sender"), instance("s", "sized_dst", W=8)], ad_hocs=[ad_hoc("c", ("m", "data_o"), ("s", "data_i"))])
    text = run(soc, [sender(), sized_dst()], expect={"s_seen_o": (8, 0xA5)})["soc.sv"]
    assert "logic [7:0] s_data_i;" in text
    assert ".data_t(" not in text  # the HDL works it out from W, as it declares it (D19)

    soc = design("soc", [instance("m", "sender"), instance("s", "sized_dst")], ad_hocs=[ad_hoc("c", ("m", "data_o"), ("s", "data_i"))])
    refused(r"width mismatch, m\.data_o is 8 bits and s\.data_i is 4 bits", soc, [sender(), sized_dst()])


@pytest.mark.parametrize("given", ["logic [7:0]", "logic [design_w-1:0]", "bit [design_w - 1 : 0]"])
def test_a_type_parameter_given_a_vector_by_the_design(run, given):
    width = ipxact.Parameter(name="W", value=_E("8"), parameter_id="design_w", resolve="user")
    soc = design(
        "soc", [instance("m", "sender"), instance("t", "typed_dst", data_t=given)], ad_hocs=[ad_hoc("c", ("m", "data_o"), ("t", "data_i"))], parameters=[width]
    )
    text = run(soc, [sender(), typed_dst()], expect={"t_seen_o": (8, 0xA5)})["soc.sv"]
    assert f".data_t({given.split()[0]} [7:0])" in text


def test_a_type_parameter_that_follows_another_gets_its_type(run):
    follow = sv_component("follow", [typed("a_i", "in", "a_t"), typed("b_o", "out", "b_t")], {"a_t": ("type", "logic"), "b_t": ("type", "a_t")})
    soc = design(
        "soc",
        [instance("m", "typed_src", data_t=PAIR, VALUE="8'ha5"), instance("f", "follow", a_t=PAIR), instance("t", "typed_dst", data_t=PAIR)],
        ad_hocs=[ad_hoc("c", ("m", "data_o"), ("f", "a_i")), ad_hoc("d", ("f", "b_o"), ("t", "data_i"))],
    )
    text = run(soc, [typed_src(), follow, typed_dst()], expect={"t_seen_o": (8, 0xA5)})["soc.sv"]
    assert "t_pkg::pair_t f_b_o;" in text
    assert ".b_t(" not in text


def test_a_type_the_generated_module_cannot_see_is_refused():
    soc = design("soc", [instance("m", "typed_src", data_t="pair_t"), instance("t", "typed_dst", data_t="pair_t")], ad_hocs=[ad_hoc("c", ("m", "data_o"), ("t", "data_i"))])
    refused("module parameter data_t: 'pair_t' uses pair_t, which no parameter here is called", soc, [typed_src(), typed_dst()])


def test_a_type_parameter_that_stands_for_no_type_this_reads_is_refused():
    soc = design("soc", [instance("m", "typed_src", data_t="struct packed { logic a; }")])
    refused("cannot work out 'struct packed { logic a; }', the type that type parameter data_t stands for", soc, [typed_src()])


def test_type_parameters_that_name_each_other_are_refused():
    loop = sv_component("follow", [typed("a_i", "in", "a_t"), typed("b_o", "out", "b_t")], {"a_t": ("type", "b_t"), "b_t": ("type", "a_t")})
    refused("refers to itself", design("soc", [instance("f", "follow")]), [loop])


def test_a_dimension_with_a_condition_in_it_is_read(run):
    """`(N > 1) ? $clog2(N) : 1` has a colon of its own, which is not the one between the two bounds."""
    parameters = {"W": ("int unsigned", "4"), "data_t": ("type", "logic[((W) > 32'd1 ? $clog2(W) : 32'd1)-1:0]")}
    index = sv_component("sized_dst", [typed("data_i", "in", "data_t"), wire("seen_o", "out", 8)], parameters)
    soc = design("soc", [instance("s", "sized_dst", W=256)], ad_hocs=[ad_hoc("c", ("s", "data_i"), tied="8'ha5")])
    assert "logic [7:0] s_data_i;" in files_of(soc, [index])["soc.sv"]


def test_a_parameter_whose_value_calls_a_package_stands_in_for_a_parameter_that_names_it(run):
    """A design parameter that only the compiler can work out, used by name: its value is written out. This used
    to recurse until Python stopped it."""
    width = ipxact.Parameter(name="DW", value=_E("t_pkg::twice(4)"), parameter_id="design_dw", resolve="user")
    scaled = sv_component("scaled", [wire("data_o", "out", 8)], {"W": ("int unsigned", "4")})
    soc = design("soc", [instance("s", "scaled", W="design_dw")], parameters=[width])
    text = run(soc, [scaled], expect={"s_data_o": (8, 8)})["soc.sv"]
    assert ".W(t_pkg::twice(4))" in text


# --- arrays of structs (D32)


@pytest.mark.parametrize("dimensions_in_the_name", [True, False])
def test_an_element_of_an_array_of_structs_connects_to_a_struct(run, dimensions_in_the_name):
    soc = design(
        "soc",
        [instance("m", "pairs_src"), instance("a", "pair_dst"), instance("b", "pair_dst")],
        ad_hocs=[ad_hoc("high", ("m", "pairs_o", (1, 1)), ("a", "pair_i")), ad_hoc("low", ("m", "pairs_o", (0, 0)), ("b", "pair_i"))],
    )
    text = run(soc, [pairs_src(dimensions_in_the_name), pair_dst()], expect={"a_seen_o": (8, 0x3C), "b_seen_o": (8, 0xA5)})["soc.sv"]
    assert "t_pkg::pair_t [1:0] m_pairs_o;" in text
    assert "assign a_pair_i = m_pairs_o[1];" in text


@pytest.mark.parametrize("members", [{}, {"hi": 4, "lo": 4}], ids=["members not listed", "members listed"])
def test_a_member_of_an_element_connects(run, members):
    soc = design("soc", [instance("m", "pairs_src"), instance("n", "nib")], ad_hocs=[connection("c", reference("m", "pairs_o", (1, 1), "lo"), reference("n", "x_i"))])
    text = run(soc, [pairs_src(**members), nib()], expect={"n_seen_o": (4, 0xC)})["soc.sv"]
    assert "assign n_x_i = m_pairs_o[1].lo;" in text


def test_a_member_of_an_element_of_another_width_is_refused_when_the_members_are_listed():
    seer = component("seer", [wire("data_i", "in", 8), wire("seen_o", "out", 8)])
    soc = design("soc", [instance("m", "pairs_src"), instance("s", "seer")], ad_hocs=[connection("c", reference("m", "pairs_o", (1, 1), "lo"), reference("s", "data_i"))])
    refused(r"width mismatch, m\.pairs_o\[1\]\.lo is 4 bits and s\.data_i is 8 bits", soc, [pairs_src(hi=4, lo=4), seer])


@pytest.mark.parametrize("members", [{}, {"hi": 4, "lo": 4}], ids=["members not listed", "members listed"])
def test_an_array_of_structs_is_driven_an_element_and_a_member_at_a_time(run, members):
    soc = design(
        "soc",
        [instance("m", "typed_src", data_t=PAIR, VALUE="8'h3c"), instance("n", "nibs"), instance("t", "pairs_dst")],
        ad_hocs=[
            connection("whole", reference("m", "data_o"), reference("t", "pairs_i", (1, 1))),
            connection("high", reference("n", "x_o"), reference("t", "pairs_i", (0, 0), "hi")),
            connection("low", reference("t", "pairs_i", (0, 0), "lo"), tied="4'h6"),
        ],
    )
    text = run(soc, [typed_src(), nibs(), pairs_dst(**members)], expect={"t_seen_o": (16, 0x3C96)})["soc.sv"]
    assert "assign t_pairs_i[1] = m_data_o;" in text
    assert "assign t_pairs_i[0].hi = n_x_o;" in text.replace("  ", " ")
    if members:
        assert "assign t_pairs_i[0].lo = 4'h6;" in text.replace("  ", " ")
    else:  # only the compiler knows the member's width, so it checks the constant fits (D21)
        assert "if ($bits(t_pairs_i[0].lo) < 3) begin : t_pairs_i_0_lo_fits" in text


def test_an_element_nothing_drives_is_refused():
    soc = design("soc", [instance("m", "typed_src", data_t=PAIR), instance("t", "pairs_dst")], ad_hocs=[connection("c", reference("m", "data_o"), reference("t", "pairs_i", (1, 1)))])
    refused(r"t\.pairs_i: parts \['element \[0\]'\] are not driven", soc, [typed_src(), pairs_dst()])


def test_a_member_of_an_element_nothing_drives_is_refused_when_the_members_are_listed():
    connections = [
        connection("whole", reference("m", "data_o"), reference("t", "pairs_i", (1, 1))),
        connection("high", reference("n", "x_o"), reference("t", "pairs_i", (0, 0), "hi")),
    ]
    soc = design("soc", [instance("m", "typed_src", data_t=PAIR), instance("n", "nibs"), instance("t", "pairs_dst")], ad_hocs=connections)
    refused(r"t\.pairs_i: parts \['\[0\]\.lo'\] are not driven", soc, [typed_src(), nibs(), pairs_dst(hi=4, lo=4)])
    files_of(soc, [typed_src(), nibs(), pairs_dst()])  # with none listed, nothing here can tell


def test_an_element_driven_twice_is_refused():
    connections = [
        connection("whole", reference("m", "data_o"), reference("t", "pairs_i", (0, 0))),
        connection("high", reference("n", "x_o"), reference("t", "pairs_i", (0, 0), "hi")),
    ]
    soc = design("soc", [instance("m", "typed_src", data_t=PAIR), instance("n", "nibs"), instance("t", "pairs_dst")], ad_hocs=connections)
    refused(r"t\.pairs_i\[0\]\.hi is already driven", soc, [typed_src(), nibs(), pairs_dst()])


@pytest.mark.parametrize(
    "select, members, message",
    [
        ((2, 2), (), r"part select \[2:2\] is outside port t\.pairs_i"),
        ((0, 1), (), r"part select \[0:1\] runs the other way"),
        ((1, 0), ("hi",), r"a sub-port of t\.pairs_i\[1:0\], which is 2 elements, has no meaning"),
        (None, ("hi",), r"a sub-port of t\.pairs_i, an array"),
    ],
)
def test_selects_on_an_array_of_structs_that_mean_nothing_are_refused(select, members, message):
    soc = design("soc", [instance("n", "nibs"), instance("t", "pairs_dst")], ad_hocs=[connection("c", reference("n", "x_o"), reference("t", "pairs_i", select, *members))])
    refused(message, soc, [nibs(), pairs_dst()])


def test_an_index_on_an_array_of_structs_is_refused():
    indexed = reference("t", "pairs_i")
    indexed.part_select = ipxact.PartSelect(indices=[_E("1")])
    soc = design("soc", [instance("m", "typed_src", data_t=PAIR), instance("t", "pairs_dst")], ad_hocs=[connection("c", reference("m", "data_o"), indexed)])
    refused("an indexed part select is not supported yet", soc, [typed_src(), pairs_dst()])


def test_a_run_of_elements_connects_to_an_array_of_as_many(run):
    quads = component("quads_src", [typed("quads_o", "out", f"{PAIR}[3:0]")])
    soc = design("soc", [instance("m", "quads_src"), instance("t", "pairs_dst")], ad_hocs=[ad_hoc("c", ("m", "quads_o", (2, 1)), ("t", "pairs_i"))])
    text = run(soc, [quads, pairs_dst()], expect={"t_seen_o": (16, 0x2233)})["soc.sv"]
    assert "assign t_pairs_i = m_quads_o[2:1];" in text

    soc = design("soc", [instance("m", "quads_src"), instance("t", "pairs_dst")], ad_hocs=[ad_hoc("c", ("m", "quads_o", (3, 1)), ("t", "pairs_i"))])
    refused("cannot check the width between m.quads_o", soc, [quads, pairs_dst()])


@pytest.mark.parametrize("members", [{}, {"hi": 4, "lo": 4}], ids=["members not listed", "members listed"])
def test_a_constant_into_an_element_is_cast_to_its_type(run, members):
    soc = design(
        "soc",
        [instance("m", "typed_src", data_t=PAIR, VALUE="8'h3c"), instance("t", "pairs_dst")],
        ad_hocs=[connection("whole", reference("m", "data_o"), reference("t", "pairs_i", (1, 1))), connection("tie", reference("t", "pairs_i", (0, 0)), tied="8'h5a")],
    )
    text = run(soc, [typed_src(), pairs_dst(**members)], expect={"t_seen_o": (16, 0x3C5A)})["soc.sv"]
    assert "assign t_pairs_i[0] = t_pkg::pair_t'(" in text

    tied = design("soc", [instance("t", "pairs_dst")], ad_hocs=[connection("tie", reference("t", "pairs_i", (1, 0)), tied="0")])
    refused(r"a constant or an inversion into t\.pairs_i\[1:0\]", tied, [pairs_dst()])


def test_port_maps_reach_elements_and_their_members(run):
    """A bus interface on an array of structs, as a crossbar has one for each element: a whole element for one
    logical port, the members of another for the halves of a second, and the abstraction's default for a third."""
    two_bytes = component(
        "two_bytes", [wire("a_o", "out", 8), wire("b_o", "out", 8)], [bus("b", "initiator", port_map("WORD", "a_o"), port_map("HIGH", "b_o"))]
    )
    quads = typed("quads_i", "in", f"{PAIR}[2:0]", hi=4, lo=4)

    def member(name, index, half, logical_range):
        mapped = port_map(name, "quads_i", select=(index, index), logical_range=logical_range)
        mapped.sub_port_refs = [ipxact.SubPortReference(sub_port_ref=half)]
        return mapped

    maps = [
        port_map("WORD", "quads_i", select=(2, 2)),
        member("HIGH", 1, "hi", (7, 4)),
        member("HIGH", 1, "lo", (3, 0)),
        port_map("TAGGED", "quads_i", select=(0, 0)),  # nothing drives it, so its default, 'h77, does
    ]
    sink = component("quads_dst", [quads, wire("seen_o", "out", 24)], [bus("b", "target", *maps)])
    source = SOURCE + "module quads_dst (input t_pkg::pair_t [2:0] quads_i, output logic [23:0] seen_o); assign seen_o = quads_i; endmodule\n"
    soc = design("soc", [instance("m", "two_bytes"), instance("t", "quads_dst")], [interconnection("link", ("m", "b"), ("t", "b"))])
    text = run(soc, [two_bytes, sink], source=source, expect={"t_seen_o": (24, 0x3CA577)})["soc.sv"]
    assert "t_pkg::pair_t'(8'h77)" in text


def test_a_module_port_that_is_an_array_of_structs_is_driven_an_element_at_a_time(run):
    chip = hierarchical("chip", "chip_d", [typed("pairs_o", "out", f"{PAIR}[1:0]")])
    external = [ipxact.ExternalPortReference(port_ref="pairs_o", part_select=ipxact.PartSelect(range=bounds(index, index))) for index in (1, 0)]
    instances = [instance("a", "typed_src", data_t=PAIR, VALUE="8'h3c"), instance("b", "typed_src", data_t=PAIR, VALUE="8'ha5")]
    chip_d = design(
        "chip_d", instances, ad_hocs=[connection("high", reference("a", "data_o"), external=[external[0]]), connection("low", reference("b", "data_o"), external=[external[1]])]
    )
    text = run(chip_d, [chip, typed_src()], module="chip", expect={"pairs_o": (16, 0x3CA5)})["chip.sv"]
    assert "output t_pkg::pair_t [1:0] pairs_o" in text

    half = design("chip_d", instances, ad_hocs=[connection("high", reference("a", "data_o"), external=[external[0]])])
    refused(r"chip\.pairs_o: top-level output parts \['element \[0\]'\] have no driver", half, [chip, typed_src()])


# --- through the importer itself


def test_modules_imported_by_xactflow_sv_generate_and_run(tmp_path, run):
    """The same shapes as xactflow-sv writes them today: `type` as a type parameter's data type, a named type as a
    struct with no members listed, and a port's packed dimensions left in its type's name."""
    xactflow_sv = pytest.importorskip("xactflow_sv")
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"vendor": "test.org", "library": "ip", "version": "1.0"}')

    def imported(name):
        path = tmp_path / f"{name}.sv"
        path.write_text(MODULES[name])
        library = Library()
        xactflow_sv.SVImporter().import_(path, library, metadata=str(metadata))
        return next(library.targets(ipxact.Component))

    documents = [imported(name) for name in ("typed_src", "fan", "typed_dst", "pair_dst", "sized_dst", "sender")]
    soc = design(
        "soc",
        [
            instance("m", "typed_src", data_t=PAIR, VALUE="8'ha5"),
            instance("f", "fan", N=2, req_t=PAIR),
            instance("t", "typed_dst", data_t=PAIR),
            instance("p", "pair_dst"),
            instance("w", "sender"),
            instance("s", "sized_dst", W=8),
        ],
        ad_hocs=[
            ad_hoc("in", ("m", "data_o"), ("f", "req_i")),
            ad_hoc("one", ("f", "reqs_o", (1, 1)), ("t", "data_i")),
            ad_hoc("zero", ("f", "reqs_o", (0, 0)), ("p", "pair_i")),
            ad_hoc("wide", ("w", "data_o"), ("s", "data_i")),
        ],
    )
    text = run(soc, documents, expect={"t_seen_o": (8, 0x5A), "p_seen_o": (8, 0xA5), "s_seen_o": (8, 0xA5)})["soc.sv"]
    assert "t_pkg::pair_t [1:0] f_reqs_o;" in text
