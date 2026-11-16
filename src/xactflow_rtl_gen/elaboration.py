"""Elaborate a linked IP-XACT design: choose its views, work out its values, go down its hierarchy.

XactFlow's link() resolves the references of one design. What it leaves to whoever uses the result is
here, with nothing in it about nets or SystemVerilog modules, so that anything else that needs a design
elaborated can use it:

- the component whose view holds the design, and the design configuration that view names;
- for each instance, the view the design configuration selects, and that view's component instantiation;
- the values of an instance's parameters in this design: the overrides of the design and of the design
  configuration applied, each expression read where it is written, a design's own parameters before those
  of the component that holds it;
- the design a hierarchical instance's view holds, elaborated in turn for the values of that instance.

    from xactflow import Library, link
    from xactflow_rtl_gen.elaboration import elaborate

    design = elaborate(link(document, library))
    for instance in design.instances():
        instance.view, instance.instantiation     # as the design configuration selects them
        instance.number("DATA_WIDTH")             # a parameter by its id, or any expression of the component
        inside = design.descend(instance)         # the design in it, elaborated for its values, or None

It refuses what it cannot elaborate correctly with a ValueError, as the generator does.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import ipxact
from xactflow import ExpressionError, LinkedDesign, LinkedInstance, Library, Severity, link, parameter_resolver

from .expressions import BOOLEANS, Resolver, number_of, resolved_text, text_of, worked_out

__all__ = [
    "ElaboratedDesign",
    "ElaboratedInstance",
    "HeldDesign",
    "elaborate",
    "refuse_link_errors",
    "refuse_override_on",
    "resolver_for",
]


def elaborate(
    linked: LinkedDesign,
    top: Optional[str] = None,
    *,
    view: Optional[ipxact.View] = None,
    overrides: Optional[Dict[str, str]] = None,
    path: Sequence[str] = (),
) -> "ElaboratedDesign":
    """Elaborate a linked design.

    `top` is the VLNV of the hierarchical component that holds the design, when more than one does. Without
    it, that is the component link() was given or found, else the only one in the library that holds the
    design. A design no component holds is elaborated on its own.

    The rest is for the design of a hierarchical instance, as descend() passes them: `view` is the view of
    the instance that holds the design, `overrides` the values the instance gives its component's parameters,
    by parameterId, and `path` the designs above this one.
    """
    _check_linked(linked)
    component = _find_top_component(linked, top)
    if component is not None and linked.component is not component:
        # link() checks hierInterfaces and external ports against the component holding the design, and finds
        # that component only when the library has exactly one: give it the one chosen here (D44).
        linked = link(linked.design, linked.library, linked.design_configuration, component)
        _check_linked(linked)
    design_values: Dict[str, object] = {}
    configuration_values: Dict[str, object] = {}
    if component is not None:
        where = f"top component {component.vlnv}"
        held = _design_of_view(linked.library, component, view or _view_holding_design(linked.library, component, linked.vlnv), where)
        if held is not None:
            design_values = held.design_values
        # A configuration the top component holds applies at the top, as it does further down.
        if linked.design_configuration is None and held is not None and held.configuration is not None:
            linked = link(linked.design, linked.library, held.configuration, component)
            _check_linked(linked)
        if held is not None and held.configuration_values:
            # Values for the configuration the component names, which may not be the one this was linked with.
            if linked.design_configuration is None or linked.design_configuration.vlnv != held.configuration.vlnv:
                raise ValueError(
                    f"{where}: its reference to design configuration {held.configuration.vlnv} sets "
                    f"{', '.join(sorted(held.configuration_values))}, and the design is configured by another one"
                )
            configuration_values = held.configuration_values
    return ElaboratedDesign(linked, component, view, overrides, design_values, configuration_values, path)


class ElaboratedDesign:
    """One design, with the component that holds it and the scopes its expressions are read in.

    `linked` is the design linked against that component and the design configuration its view names, so it
    may be another LinkedDesign than the one elaborate() was given. `component` is the hierarchical component
    that holds the design, None when the library has none, with `view` its view that holds it and
    `instantiation` that view's component instantiation.

    `parameters` are the component's and its component instantiation's, and `overrides` the values the
    instance this is elaborated for gives them (D26). Three scopes read expressions: `resolve` one written in
    the component, `design_resolve` one written in the design, and `configuration_resolve` one written in the
    design configuration. `path` names the designs above this one.
    """

    def __init__(
        self,
        linked: LinkedDesign,
        component: Optional[ipxact.Component],
        view: Optional[ipxact.View] = None,
        overrides: Optional[Dict[str, str]] = None,
        design_values: Optional[Dict[str, object]] = None,
        configuration_values: Optional[Dict[str, object]] = None,
        path: Sequence[str] = (),
    ) -> None:
        self.linked = linked
        self.component = component
        self.path = tuple(path)
        where = f"top component {component.vlnv}" if component else f"design {linked.vlnv}"
        # The view that holds the design, and the module it describes, if it does.
        self.view = self.instantiation = None
        if component is not None:
            self.view = view or _view_holding_design(linked.library, component, linked.vlnv)
            if self.view is not None:
                self.instantiation = _component_instantiation(component, self.view, where)
        # The component's parameters, with the values of the instance this is elaborated for (D26). Its
        # ports and module parameters see those of its component instantiation as well, as an instance's do.
        component_parameters = list(component.parameters) if component else []
        of_the_module = [*self.instantiation.parameters, *self.instantiation.module_parameters] if self.instantiation else []
        overrides = overrides or {}
        _check_overrides_name_parameters(overrides, [*component_parameters, *of_the_module], where, "the component")
        self.parameters = [*component_parameters, *of_the_module]
        self.resolve = resolver_for(self.parameters, overrides)
        component_parameters = [_overridden(p, overrides) for p in component_parameters]
        self.overrides = overrides
        self.component_resolve = resolver_for(component_parameters, {})
        # The values the component's reference to the design gives the design's parameters, worked out where
        # the reference is written: in the component (D25).
        design_parameters = list(linked.design.parameters)
        _check_overrides_name_parameters(design_values or {}, design_parameters, f"{where}: its reference to design {linked.vlnv}", "that design")
        names = {p.parameter_id: p.name for p in design_parameters}
        values = {
            key: resolved_text(text_of(value), self.component_resolve, f"{where}: the value for design parameter {names[key]}")
            for key, value in (design_values or {}).items()
        }
        # What an expression written in the design can see: the design's parameters first.
        scope = _nearest_first([_overridden(p, values) for p in design_parameters], component_parameters)
        self.design_resolve = resolver_for(scope, {})
        # A value in a design configuration sees that configuration's parameters before those. The component's
        # reference to the configuration gives them values the same way as the design's (D37).
        configuration = linked.design_configuration
        configuration_parameters = list(configuration.parameters) if configuration else []
        _check_overrides_name_parameters(
            configuration_values or {}, configuration_parameters, f"{where}: its reference to the design configuration", "that design configuration"
        )
        names = {p.parameter_id: p.name for p in configuration_parameters}
        values = {
            key: resolved_text(text_of(value), self.component_resolve, f"{where}: the value for design configuration parameter {names[key]}")
            for key, value in (configuration_values or {}).items()
        }
        self.configuration_resolve = resolver_for(_nearest_first([_overridden(p, values) for p in configuration_parameters], scope), {})

    def configured_view(self, linked_instance: LinkedInstance) -> Tuple[Optional[ipxact.View], Optional[ipxact.ViewConfiguration]]:
        """The view an instance uses, and what the design configuration says for the instance, if anything."""
        configuration_document = self.linked.design_configuration
        configuration = next(
            (c for c in (configuration_document.view_configurations if configuration_document else []) if c.instance_name == linked_instance.instance_name),
            None,
        )
        where = f"instance {linked_instance.instance_name!r}"
        return _selected_view(linked_instance.component, configuration.view_ref if configuration else None, where), configuration

    def _design_level(self, text: str, scope: Optional[Resolver] = None) -> str:
        """An override may refer to parameters an instance cannot see, so they are replaced here. A name
        they don't explain stays, for the instance's own scope to explain or refuse."""
        return resolved_text(text, scope or self.design_resolve, f"parameter override {text!r}", keep_unknown=True)

    def instance(self, linked_instance: LinkedInstance) -> "ElaboratedInstance":
        """One instance of the design: the view it uses, and its parameters with the values the design and the
        design configuration give them. link() does not apply those values, so they are applied here."""
        name = linked_instance.instance_name
        where = f"instance {name!r}"
        component = linked_instance.component
        view, configuration = self.configured_view(linked_instance)
        if configuration is not None and configuration.view_ref and view is not None and not (
            view.component_instantiation_ref or view.design_instantiation_ref or view.design_configuration_instantiation_ref
        ):
            raise ValueError(f"{where}: the design configuration selects view {view.name!r}, which instantiates nothing")
        instantiation = _component_instantiation(component, view, where)
        overrides = {key: self._design_level(text_of(value)) for key, value in linked_instance.source.component_ref.config_element_values.items()}
        if configuration is not None:
            overrides.update({key: self._design_level(text_of(value), self.configuration_resolve) for key, value in configuration.configurable_element_values.items()})
        parameters = [
            *component.parameters,
            *(instantiation.parameters if instantiation is not None else []),
            *(instantiation.module_parameters if instantiation is not None else []),
        ]
        return ElaboratedInstance(name, linked_instance, component, view, configuration, instantiation, parameters, overrides)

    def instances(self) -> List["ElaboratedInstance"]:
        """Every instance of the design, in the design's order."""
        return [self.instance(linked_instance) for linked_instance in self.linked.instances.values()]

    def held_by(self, instance: "ElaboratedInstance") -> Optional["HeldDesign"]:
        """The design the instance's view holds, if it holds one: the instance is then hierarchical."""
        return _design_of_view(self.linked.library, instance.component, instance.view, f"instance {instance.name!r}")

    def link_held(self, held: "HeldDesign", component: ipxact.Component) -> LinkedDesign:
        """The design a hierarchical instance holds, linked against the instance's component and the design
        configuration its view names. A design that contains itself is refused."""
        name = str(held.design.vlnv)
        if name in self.path or name == str(self.linked.vlnv):
            raise ValueError(f"design {name} contains itself: {' -> '.join((*self.path, str(self.linked.vlnv), name))}")
        return link(held.design, self.linked.library, held.configuration, component)

    def descend(self, instance: "ElaboratedInstance", overrides: Optional[Dict[str, str]] = None) -> Optional["ElaboratedDesign"]:
        """The design inside a hierarchical instance, elaborated for the values the instance gives its
        component's parameters, or None for an instance whose view holds no design. `overrides` gives other
        values than the instance's own, as for an instance that shares the design of one with the same values."""
        held = self.held_by(instance)
        if held is None:
            return None
        if overrides is None:
            overrides = dict(instance.overrides) if instance.variant() is not None else {}
        return elaborate(
            self.link_held(held, instance.component),
            str(instance.component.vlnv),
            view=instance.view,
            overrides=overrides,
            path=(*self.path, str(self.linked.vlnv)),
        )


@dataclass
class ElaboratedInstance:
    """An instance of a design, with the view it uses and its parameters' values in that design."""

    name: str
    linked: LinkedInstance
    component: ipxact.Component
    view: Optional[ipxact.View]  # the one the design configuration selects, else the first that instantiates anything
    view_configuration: Optional[ipxact.ViewConfiguration]  # what the design configuration says for the instance
    instantiation: Optional[ipxact.ComponentInstantiation]  # the component instantiation of that view
    parameters: List[ipxact.Parameter]  # the component's, then its instantiation's, then that one's module parameters
    overrides: Dict[str, str]  # the values this design gives them, by parameterId, the design's own names worked out
    resolve: Resolver = field(init=False, repr=False, compare=False)  # a parameter by its id, with its value in this instance

    def __post_init__(self) -> None:
        self.resolve = resolver_for(self.parameters, self.overrides)

    def with_values(self, values: Dict[str, str]) -> "ElaboratedInstance":
        """The instance with more values given to its parameters, by parameterId, as a generator gives one a
        value it works out itself."""
        if not values:
            return self
        return dataclasses.replace(self, overrides={**self.overrides, **values})

    def number(self, expression: object, where: Optional[str] = None) -> int:
        """What an expression of the component is worth in this instance: the bound of a port, or a parameter
        by its id. `where` starts the message when it is refused."""
        return number_of(expression, self.resolve, where or f"instance {self.name!r}")

    def variant(self) -> Optional[Tuple[object, ...]]:
        """What tells this instance's values from another's: its component, its view and the value of each
        parameter, or None when its overrides change no parameter. Instances whose values come to the same
        have the same one, however each wrote them: `16`, `8+8`, or with a default spelled out (D26)."""
        if not self.overrides:
            return None
        where = f"instance {self.name!r}"
        _check_overrides_name_parameters(self.overrides, self.parameters, where, f"{self.component.vlnv}")
        before, after = resolver_for(self.parameters, {}), resolver_for(self.parameters, self.overrides)

        def value(parameter: ipxact.Parameter, resolve: Resolver) -> str:
            final = resolve(parameter.parameter_id) if parameter.parameter_id else parameter
            try:
                return worked_out(final, resolve)
            except (ExpressionError, ValueError, OverflowError, MemoryError, RecursionError):
                return resolved_text(text_of(final.value), resolve, f"{where}: parameter {parameter.name}", keep_unknown=True)

        values = tuple((p.name, value(p, after)) for p in self.parameters)
        if values == tuple((p.name, value(p, before)) for p in self.parameters):
            return None
        return (str(self.component.vlnv), self.view.name if self.view is not None else None, values)


@dataclass
class HeldDesign:
    """What a view holds: a design, the design configuration it names if any, and the values its references
    give the parameters of each."""

    design: ipxact.Design
    configuration: Optional[ipxact.DesignConfiguration]
    design_values: Dict[str, object]
    configuration_values: Dict[str, object] = field(default_factory=dict)


def refuse_link_errors(linked: LinkedDesign) -> None:
    """Refuse a design whose library or link reports errors."""
    errors = [d for d in [*linked.library.diagnostics, *linked.diagnostics] if d.severity is Severity.ERROR]
    if errors:
        details = "; ".join(dict.fromkeys(str(d) for d in errors))
        raise ValueError(f"design {linked.vlnv} has link errors: {details}")


def _check_linked(linked: LinkedDesign) -> None:
    """Refuse a design with link errors, and what link() does not report or this elaboration lacks."""
    refuse_link_errors(linked)
    design = linked.design
    configuration = linked.design_configuration
    if configuration is not None and configuration.interconnection_configurations:
        raise ValueError("interconnection configurations (abstractors) are not supported yet")
    if configuration is not None:
        refuse_override_on(configuration.design_ref, f"design configuration {configuration.vlnv}")
        if configuration.design_ref is not None and configuration.design_ref.vlnv != linked.vlnv:
            raise ValueError(f"design configuration {configuration.vlnv} is for design {configuration.design_ref.vlnv}, not {linked.vlnv}")
        for view in configuration.view_configurations:
            if view.instance_name not in linked.instances:
                raise ValueError(f"design configuration {configuration.vlnv} configures {view.instance_name!r}, which is not an instance of the design")

    # link() leaves out an ad hoc reference it cannot resolve. Since XactFlow 0.4.0 it reports each one (SCR 13.5),
    # which is refused above; this stays for a LinkedDesign put together another way.
    for connection in design.ad_hoc_connections:
        resolved = linked.ad_hoc_connections.get(connection.name, [])
        if len(resolved) != len(connection.internal_port_references):
            found = {(r.source.component_instance_ref, r.source.port_ref) for r in resolved}
            missing = [
                f"{r.component_instance_ref}.{r.port_ref}"
                for r in connection.internal_port_references
                if (r.component_instance_ref, r.port_ref) not in found
            ]
            raise ValueError(f"ad hoc connection {connection.name!r} refers to unknown ports: {', '.join(missing)}")


def refuse_override_on(reference: Optional[ipxact.VLNVRef], where: str) -> None:
    """Parameter overrides on a reference to a bus, or from a design configuration to its design, would be
    lost: nothing here applies them."""
    if reference is not None and reference.config_element_values:
        names = ", ".join(sorted(reference.config_element_values))
        raise ValueError(f"{where}: parameter overrides on its reference to {reference.vlnv} are not supported yet: {names}")


def _selected_view(component: ipxact.Component, view_name: Optional[str], where: str) -> Optional[ipxact.View]:
    """The view the design configuration names, else the first one that instantiates anything."""
    if component.model is None:
        return None
    if view_name is not None:
        for view in component.model.views:
            if view.name == view_name:
                return view
        raise ValueError(f"{where}: the design configuration selects view {view_name!r}, which does not exist")
    for view in component.model.views:
        if view.component_instantiation_ref or view.design_instantiation_ref or view.design_configuration_instantiation_ref:
            return view
    return None


def _component_instantiation(
    component: ipxact.Component, view: Optional[ipxact.View], where: str
) -> Optional[ipxact.ComponentInstantiation]:
    """The component instantiation a view refers to. A component with no view that instantiates anything takes
    its only component instantiation, as some tools write a module's name and parameters with no view (D33)."""
    if view is None:
        instantiations = component.model.component_instantiations if component.model is not None else []
        if len(instantiations) > 1:
            names = ", ".join(i.name for i in instantiations)
            raise ValueError(f"{where}: {component.vlnv} has no view to choose between its component instantiations: {names}")
        return instantiations[0] if instantiations else None
    if not view.component_instantiation_ref:
        return None
    for instantiation in component.model.component_instantiations:
        if instantiation.name == view.component_instantiation_ref:
            return instantiation
    raise ValueError(f"{where}: view {view.name!r} of {component.vlnv} refers to a missing component instantiation, {view.component_instantiation_ref!r}")


def _design_of_view(library: Library, component: ipxact.Component, view: Optional[ipxact.View], where: str) -> Optional[HeldDesign]:
    """The design a view holds, directly or through a design configuration, if it holds one, with the values
    its reference to the design gives the design's parameters (D25), and those its reference to the design
    configuration gives that one's (D37). A view may name both a design and a design configuration; the
    design's values are then on the design's reference, which is why it is read either way."""
    if view is None:
        return None
    model = component.model
    direct = None
    if view.design_instantiation_ref:
        direct = next((i for i in model.design_instantiations if i.name == view.design_instantiation_ref), None)
        if direct is None:
            raise ValueError(f"{where}: view {view.name!r} refers to a missing design instantiation")
    values: Dict[str, object] = dict(direct.design_ref.config_element_values) if direct is not None else {}
    if view.design_configuration_instantiation_ref:
        instantiation = next(
            (i for i in model.design_configuration_instantiations if i.name == view.design_configuration_instantiation_ref),
            None,
        )
        if instantiation is None:
            raise ValueError(f"{where}: view {view.name!r} refers to a missing design configuration instantiation")
        configuration = library.get_design_configuration(instantiation.design_configuration_ref.vlnv)
        if configuration is None or configuration.design_ref is None:
            raise ValueError(f"{where}: design configuration {instantiation.design_configuration_ref} is not usable")
        if direct is not None and direct.design_ref.vlnv != configuration.design_ref.vlnv:
            raise ValueError(
                f"{where}: view {view.name!r} names design {direct.design_ref.vlnv}, "
                f"and its design configuration is for {configuration.design_ref.vlnv}"
            )
        design = library.get_design(configuration.design_ref.vlnv)
        if design is None:
            raise ValueError(f"{where}: design {configuration.design_ref} is not in the library")
        return HeldDesign(design, configuration, values, dict(instantiation.design_configuration_ref.config_element_values))
    if direct is not None:
        design = library.get_design(direct.design_ref.vlnv)
        if design is None:
            raise ValueError(f"{where}: design {direct.design_ref} is not in the library")
        return HeldDesign(design, None, values)
    return None


def _view_holding_design(library: Library, component: ipxact.Component, vlnv: ipxact.VLNV) -> Optional[ipxact.View]:
    """The view that holds this design. Only compares references, so that a component elsewhere in
    the library with a broken view of its own does not stop this design from being generated."""
    model = component.model
    if model is None:
        return None
    for view in model.views:
        if view.design_instantiation_ref:
            for instantiation in model.design_instantiations:
                if instantiation.name == view.design_instantiation_ref and instantiation.design_ref.vlnv == vlnv:
                    return view
        elif view.design_configuration_instantiation_ref:
            for instantiation in model.design_configuration_instantiations:
                if instantiation.name == view.design_configuration_instantiation_ref:
                    configuration = library.get_design_configuration(instantiation.design_configuration_ref.vlnv)
                    if configuration is not None and configuration.design_ref and configuration.design_ref.vlnv == vlnv:
                        return view
    return None


def _find_top_component(linked: LinkedDesign, top: Optional[str]) -> Optional[ipxact.Component]:
    """The hierarchical component whose view holds this design, if the library has one."""
    library, vlnv = linked.library, linked.vlnv
    if top is None and linked.component is not None and _view_holding_design(library, linked.component, vlnv) is not None:
        return linked.component  # the one link() was given, or the only one it found
    if top is not None:
        if not isinstance(top, str):
            raise TypeError(f"xactflow-rtl-gen: the top option is a VLNV as text, got {type(top).__name__}")
        component = library.get_component(ipxact.VLNV.parse(top))
        if component is None:
            raise ValueError(f"top component {top} is not in the library")
        if _view_holding_design(library, component, vlnv) is None:
            raise ValueError(f"top component {top} has no view that holds design {vlnv}")
        return component
    found = [
        document
        for document in library.documents()
        if isinstance(document, ipxact.Component) and _view_holding_design(library, document, vlnv) is not None
    ]
    if len(found) > 1:
        names = ", ".join(str(component.vlnv) for component in found)
        raise ValueError(f"more than one component holds design {vlnv}: {names}. Choose one with the top option")
    return found[0] if found else None


def _overridden(parameter: ipxact.Parameter, overrides: Dict[str, str]) -> ipxact.Parameter:
    """The parameter with its value in this place: an override replaces it, keeping the parameter's type."""
    text = overrides.get(parameter.parameter_id, text_of(parameter.value))
    if parameter.parameter_id not in overrides and text not in BOOLEANS:
        return parameter
    return dataclasses.replace(parameter, value=ipxact.Expression(BOOLEANS.get(text, text)))


def resolver_for(parameters: Iterable[ipxact.Parameter], overrides: Dict[str, str]) -> Resolver:
    return parameter_resolver(_overridden(parameter, overrides) for parameter in parameters)


def _check_overrides_name_parameters(overrides: Dict[str, object], parameters: Iterable[ipxact.Parameter], where: str, what: str) -> None:
    unknown = sorted(set(overrides) - {p.parameter_id for p in parameters if p.parameter_id})
    if unknown:
        raise ValueError(f"{where}: parameter overrides name no parameter of {what}: {', '.join(unknown)}")


def _nearest_first(nearer: Iterable[ipxact.Parameter], further: Iterable[ipxact.Parameter]) -> List[ipxact.Parameter]:
    """The parameters of a scope and the one around it, where a nearer one hides a further one of the same
    parameterId. A further one keeps its meaning: it is worked out first, as a nearer one must not
    change what the names in it refer to."""
    nearer, further = list(nearer), list(further)
    resolve = resolver_for(further, {})
    hidden = {parameter.parameter_id for parameter in nearer}
    kept = []
    for parameter in further:
        if parameter.parameter_id is None or parameter.parameter_id not in hidden:
            value = resolved_text(text_of(parameter.value), resolve, f"parameter {parameter.name}", keep_unknown=True)
            vectors = []
            for vector in parameter.vectors:  # the bounds of its type are named in its own scope as well
                try:
                    bounds = [number_of(side, resolve, f"parameter {parameter.name}") for side in (vector.left, vector.right)]
                except ValueError:
                    vectors.append(vector)
                else:
                    vectors.append(dataclasses.replace(vector, left=ipxact.Expression(str(bounds[0])), right=ipxact.Expression(str(bounds[1]))))
            kept.append(dataclasses.replace(parameter, value=ipxact.Expression(value), vectors=vectors))
    return [*nearer, *kept]
