#!/usr/bin/env python
"""Compare what drives every port in the generated croc_soc and croc_domain with the hand-written ones, bit by bit.

Both versions of Croc are elaborated by slang, from the file lists check_slang.py writes. In each, for croc_soc and
for croc_domain, every continuous assign, the one always_comb block and every port connection is followed, so
that each input bit of each instance, and each output bit of the module, is traced to what drives it: a bit of
an instance's output, a bit of one of the module's inputs, or a constant. The hand-written modules go through
struct variables and generate blocks, the generated ones through one net for each port, and both come down
to the same table if they are the same hardware. Each instance's parameters are compared as the values and types
slang worked out for them.

Only reads the files: nothing of Croc's is run. A table for each module is written to
OUT_DIR/connections/{ref,gen}/<module>.txt, one line for each run of bits, to diff or to check by hand:

    i_core_wrap.irqs_i[7:4]          <- top:interrupts_i[3:0]
    i_main_xbar.sbr_ports_req_i[287:256] <- i_core_wrap.instr_addr_o[31:0]

    python compare_connections.py CROC_DIR RTL_DIR OUT_DIR

Names: an instance in a generate loop, `gen_sram_bank[0].i_sram`, is `i_sram_0` in the IP-XACT design, and one in
an if-generate keeps its own name. The glue module of the generated croc_domain only holds constants, so it is
looked through, not counted as an instance.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pyslang
from pyslang import ast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # -I leaves the script's folder off the path
TOP = "tb_croc_soc"
LOOKED_THROUGH = {"croc_domain_glue"}
EK, SK = ast.ExpressionKind, ast.SymbolKind

Source = tuple  # ("net", path, bit) | ("const", "0") | ("out", instance, port, bit) | ("top", port, bit) | ("not", Source) | ...


def load(flist: Path):
    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.parseCommandLine(f"slang -f {flist} --top {TOP}") or not driver.processOptions() or not driver.parseAllSources():
        raise SystemExit(f"slang could not read {flist}")
    return driver, driver.createCompilation()


def child(scope, name: str):
    for member in scope:
        if member.kind == SK.Instance and member.name == name:
            return member
    raise SystemExit(f"no instance {name}")


def label_of(path: str, below: str) -> str:
    """An instance's name as the IP-XACT design has it: `gen_x[0].i_y` is `i_y_0`, `gen_x.i_y` is `i_y`."""
    name = path[len(below) + 1 :]
    name = re.sub(r"\w+\[(\d+)\]\.(\w+)", r"\2_\1", name)
    return re.sub(r"^(\w+\.)+", "", name)


class Tracer:
    def __init__(self, module) -> None:
        self.module = module
        self.path = module.hierarchicalPath
        self.drivers: Dict[Tuple[str, int], Source] = {}
        self.inputs: Dict[str, str] = {}  # the variable of each of the module's inputs -> the port's name
        self.sinks: List[Tuple[str, List[Source]]] = []  # what to report: "instance.port" or "top:port", and its bits
        self.parameters: Dict[str, Dict[str, str]] = {}
        self.modules: Dict[str, str] = {}
        self.notes: List[str] = []
        for member in module.body:
            if member.kind == SK.Port:
                width = member.type.bitWidth
                internal = member.internalSymbol.hierarchicalPath
                if member.direction == ast.ArgumentDirection.In:
                    self.inputs[internal] = member.name
                elif member.direction == ast.ArgumentDirection.Out:
                    self.sinks.append((f"top:{member.name}", [("net", internal, bit) for bit in range(width)]))
                else:
                    self.notes.append(f"port {member.name} is neither an input nor an output: not traced")
        self.scope(module.body, module.body)

    # --- expressions

    def constant(self, expression, scope) -> Optional[List[Source]]:
        try:
            value = expression.eval(ast.EvalContext(scope))
        except Exception:  # noqa: BLE001 - whatever cannot be evaluated is not a constant here
            return None
        number = getattr(value, "value", None)
        if not isinstance(number, pyslang.SVInt):
            return None
        width = expression.type.bitWidth
        if number.hasUnknown:
            return [("const", "x")] * width
        as_int = int(number) & ((1 << width) - 1)
        return [("const", str((as_int >> bit) & 1)) for bit in range(width)]

    def bits(self, expression, scope) -> List[Source]:
        """What an expression is, bit by bit, least significant first."""
        constant = self.constant(expression, scope)
        if constant is not None:
            return constant
        kind, width = expression.kind, expression.type.bitWidth
        if kind in (EK.NamedValue, EK.HierarchicalValue):
            return [("net", expression.symbol.hierarchicalPath, bit) for bit in range(width)]
        if kind == EK.MemberAccess:
            offset = expression.member.bitOffset
            return self.bits(expression.value, scope)[offset : offset + width]
        if kind in (EK.ElementSelect, EK.RangeSelect):
            base = self.bits(expression.value, scope)
            array = expression.value.type.canonicalType
            if array.kind == SK.PackedArrayType:
                bounds, each = array.range, array.elementType.bitWidth
            else:  # int and the like: a row of bits, [width - 1:0]
                bounds, each = None, 1
            left, right = (bounds.left, bounds.right) if bounds is not None else (len(base) - 1, 0)

            def number(part) -> int:
                found = self.constant(part, scope)
                if found is None:
                    raise ValueError("a select that is not a constant")
                return sum(int(bit[1]) << position for position, bit in enumerate(found))

            if kind == EK.ElementSelect:
                low = high = number(expression.selector)
            elif expression.selectionKind == ast.RangeSelectionKind.Simple:
                low, high = sorted((number(expression.left), number(expression.right)))
            elif expression.selectionKind == ast.RangeSelectionKind.IndexedUp:
                low = number(expression.left)
                high = low + number(expression.right) - 1
            else:
                high = number(expression.left)
                low = high - number(expression.right) + 1
            if left >= right:
                first, last = low - right, high - right
            else:
                first, last = right - high, right - low
            return base[first * each : (last + 1) * each]
        if kind == EK.Concatenation:
            return [bit for operand in reversed(list(expression.operands)) for bit in self.bits(operand, scope)]
        if kind == EK.Conversion:
            inner = self.bits(expression.operand, scope)
            fill = inner[-1] if inner and expression.operand.type.isSigned else ("const", "0")
            return (inner + [fill] * width)[:width]
        if kind == EK.UnaryOp and expression.op == ast.UnaryOperator.BitwiseNot:
            return [("not", bit) for bit in self.bits(expression.operand, scope)]
        text = " ".join(str(expression.syntax).split())
        self.notes.append(f"not followed: {text}")
        return [("expression", text, bit) for bit in range(width)]

    def drive(self, left, right_bits: List[Source], scope) -> None:
        for target, source in zip(self.bits(left, scope), right_bits):
            if target[0] != "net":
                self.notes.append(f"an assignment to something that is no net: {target}")
                continue
            self.drivers[(target[1], target[2])] = source

    # --- what a module holds

    def scope(self, members, scope) -> None:
        for member in members:
            kind = member.kind
            if kind == SK.ContinuousAssign:
                assignment = member.assignment
                self.drive(assignment.left, self.bits(assignment.right, scope), scope)
            elif kind == SK.ProceduralBlock:
                self.procedure(member, scope)
            elif kind == SK.GenerateBlock:
                if not member.isUninstantiated:
                    self.scope(member, member)
            elif kind == SK.GenerateBlockArray:
                for entry in member.entries:
                    self.scope(entry, entry)
            elif kind == SK.Instance:
                self.instance(member, scope)

    def procedure(self, block, scope) -> None:
        """An always_comb of plain assignments, each one over the last: later ones win, bit by bit."""
        body = block.body
        statements = list(body.body.list) if hasattr(body.body, "list") else [body.body]
        plain = block.procedureKind == ast.ProceduralBlockKind.AlwaysComb and all(
            s.kind == ast.StatementKind.ExpressionStatement and s.expr.kind == EK.Assignment and not s.expr.isNonBlocking for s in statements
        )
        if not plain:
            self.notes.append(f"a procedural block that is not followed: {' '.join(str(block.syntax).split())[:80]}")
            return
        for statement in statements:
            self.drive(statement.expr.left, self.bits(statement.expr.right, scope), scope)

    def instance(self, instance, scope) -> None:
        through = instance.definition.name in LOOKED_THROUGH
        label = label_of(instance.hierarchicalPath, self.path)
        if not through:
            self.modules[label] = instance.definition.name
            values = self.parameters.setdefault(label, {})
            for member in instance.body:
                if member.kind == SK.Parameter and member.isPortParam:
                    values[member.name] = str(member.value)
                elif member.kind == SK.TypeParameter and member.isPortParam:
                    values[member.name] = f"type {member.targetType.type.canonicalType}"
        for connection in instance.portConnections:
            port, expression = connection.port, connection.expression
            if port.kind != SK.Port:
                self.notes.append(f"{label}.{port.name}: not a plain port, not traced")
                continue
            width = port.type.bitWidth
            inside = [("net", port.internalSymbol.hierarchicalPath, bit) for bit in range(width)]
            if port.direction == ast.ArgumentDirection.In:
                outside = self.bits(expression, scope) if expression is not None else [("open",)] * width
                if through:
                    for target, source in zip(inside, outside):
                        self.drivers[(target[1], target[2])] = source
                else:
                    self.sinks.append((f"{label}.{port.name}", outside))
            elif port.direction == ast.ArgumentDirection.Out:
                if expression is None:
                    continue
                if expression.kind != EK.Assignment:
                    self.notes.append(f"{label}.{port.name}: an output connected in a way this does not read")
                    continue
                sources = inside if through else [("out", label, port.name, bit) for bit in range(width)]
                self.drive(expression.left, sources, scope)
            else:
                self.notes.append(f"{label}.{port.name}: an inout, not traced")
        if through:
            self.scope(instance.body, instance.body)

    # --- the table

    def resolve(self, source: Source, depth: int = 0) -> Source:
        if depth > 200:
            return ("loop",)
        if source[0] == "net":
            key = (source[1], source[2])
            if key in self.drivers:
                return self.resolve(self.drivers[key], depth + 1)
            if source[1] in self.inputs:
                return ("top", self.inputs[source[1]], source[2])
            return ("undriven", label_of(source[1], self.path), source[2])
        if source[0] == "not":
            return ("not", self.resolve(source[1], depth + 1))
        return source

    def table(self) -> Dict[str, str]:
        """{"instance.port[bit]": what drives it}, with everything followed to its end."""
        rows = {}
        for name, bits in self.sinks:
            for bit, source in enumerate(bits):
                rows[f"{name}[{bit}]"] = describe(self.resolve(source))
        return rows


def describe(source: Source) -> str:
    kind = source[0]
    if kind == "const":
        return f"1'b{source[1]}"
    if kind == "out":
        return f"{source[1]}.{source[2]}[{source[3]}]"
    if kind == "top":
        return f"top:{source[1]}[{source[2]}]"
    if kind == "not":
        return f"~{describe(source[1])}"
    if kind == "undriven":
        return f"UNDRIVEN:{source[1]}[{source[2]}]"
    if kind == "open":
        return "(open)"
    return f"{kind}:{' '.join(str(part) for part in source[1:])}"


def runs(rows: Dict[str, str]) -> List[str]:
    """The table with consecutive bits joined into one line: port[7:4] <- other[3:0], or a constant as bits."""
    bit = re.compile(r"^(.*)\[(\d+)\]$")
    lines: List[str] = []
    run: Optional[list] = None  # [sink, low, high, source base or None, source low, constant bits]

    def flush() -> None:
        if run is None:
            return
        sink, low, high, base, source_low, constant = run
        select = f"[{high}:{low}]" if high != low else f"[{low}]"
        if base is None:
            what = f"{high - low + 1}'b{constant[::-1]}" if set(constant) <= {"0", "1"} else constant
        else:
            what = f"{base}[{source_low + high - low}:{source_low}]" if high != low else f"{base}[{source_low}]"
        lines.append(f"{sink + select:<52} <- {what}")

    for key, source in rows.items():
        sink, index = bit.match(key).group(1), int(bit.match(key).group(2))
        found = bit.match(source)
        constant = re.fullmatch(r"1'b([01xz])", source)
        if run is not None and run[0] == sink and index == run[2] + 1:
            if constant and run[3] is None and isinstance(run[5], str) and set(run[5]) <= set("01xz"):
                run[2], run[5] = index, run[5] + constant.group(1)
                continue
            if found and run[3] == found.group(1) and int(found.group(2)) == run[4] + index - run[1]:
                run[2] = index
                continue
        flush()
        if constant:
            run = [sink, index, index, None, 0, constant.group(1)]
        elif found:
            run = [sink, index, index, found.group(1), int(found.group(2)), ""]
        else:
            run = [sink, index, index, None, 0, source]
    flush()
    return lines


def as_number(text: str) -> Optional[int]:
    found = re.fullmatch(r"(?:\d+'[sS]?[dD])?(-?\d+)|\d+'[sS]?[bB]([01]+)|\d+'[sS]?[hH]([0-9a-fA-F]+)", text)
    if not found:
        return None
    return int(found.group(1)) if found.group(1) else int(found.group(2), 2) if found.group(2) else int(found.group(3), 16)


def compare(module: str, reference: Tracer, generated: Tracer, generated_modules: Dict[str, str], out: Path) -> Dict[str, int]:
    """Print what differs between the two versions of one module. Returns how many bits, instances and parameters
    were compared, and how many things differ."""
    differences = 0
    out = out / "connections"
    tables = {}
    for name, tracer in (("ref", reference), ("gen", generated)):
        tables[name] = tracer.table()
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"// {module}, {'hand-written' if name == 'ref' else 'generated by xactflow-rtl-gen'}", ""]
        # Instances by name, so that the two files line up: a generate block puts them elsewhere in the module.
        for instance, values in sorted(tracer.parameters.items()):
            lines.extend(f"{instance} #{parameter:<24} = {value}" for parameter, value in values.items())
        lines.append("")
        by_owner: Dict[str, Dict[str, str]] = {}
        for key, value in tables[name].items():
            by_owner.setdefault(key.split(".")[0] if not key.startswith("top:") else "", {})[key] = value
        for owner in sorted(by_owner):
            lines.extend(runs(by_owner[owner]))
        lines.extend(["", *[f"note: {note}" for note in dict.fromkeys(tracer.notes)]])
        (folder / f"{module}.txt").write_text("\n".join(lines) + "\n")

    ours, theirs = tables["gen"], tables["ref"]
    print(f"{module}: {len(theirs)} bits traced in the hand-written module, {len(ours)} in the generated one")
    for key in sorted(set(ours) | set(theirs)):
        if ours.get(key) != theirs.get(key):
            differences += 1
            if differences <= 25:
                print(f"  {key}: hand-written {theirs.get(key, '(absent)')}, generated {ours.get(key, '(absent)')}")
    unfollowed = [value for table in tables.values() for value in table.values() if value.startswith(("UNDRIVEN", "expression", "loop"))]
    if unfollowed:
        differences += len(unfollowed)
        print(f"  {len(unfollowed)} bits end in something not followed, such as {unfollowed[0]}")

    # the same instances, of the same modules, with the same parameter values
    for instance in sorted(set(reference.modules) | set(generated.modules)):
        theirs_module, ours_module = reference.modules.get(instance), generated.modules.get(instance)
        if generated_modules.get(ours_module, ours_module) != theirs_module:
            differences += 1
            print(f"  instance {instance}: hand-written {theirs_module}, generated {ours_module}")
            continue
        if ours_module in generated_modules:
            continue  # a generated module takes no parameters: its values are inside it (D26)
        theirs_values, ours_values = reference.parameters[instance], generated.parameters[instance]
        for parameter in sorted(set(theirs_values) | set(ours_values)):
            a, b = theirs_values.get(parameter), ours_values.get(parameter)
            same = a == b or (a is not None and b is not None and as_number(a) is not None and as_number(a) == as_number(b))
            if not same:
                differences += 1
                print(f"  {instance} #{parameter}: hand-written {a}, generated {b}")
    for name, tracer in (("hand-written", reference), ("generated", generated)):
        for note in dict.fromkeys(tracer.notes):
            print(f"  note ({name}): {note}")
    parameters = sum(len(values) for values in reference.parameters.values())
    print(f"  {len(reference.modules)} instances, {parameters} parameters, {differences} differences")
    return {"bits": len(theirs), "instances": len(reference.modules), "parameters": parameters, "differences": differences}


def compare_all(croc: Path, rtl: Path, out: Path) -> Dict[str, Dict[str, int]]:
    """Print what differs in croc_soc and croc_domain, and return what compare() counted for each."""
    import check_slang

    reference_list, generated_list = check_slang.file_lists(croc, rtl, out)
    _, reference = load(reference_list)
    _, generated = load(generated_list)
    soc_ref = child(reference.getRoot().topInstances[0].body, "i_croc_soc")
    soc_gen = child(generated.getRoot().topInstances[0].body, "i_croc_soc")
    domain_gen = child(soc_gen.body, "i_croc")
    renamed = {domain_gen.definition.name: "croc_domain"}  # croc_domain_1: the version for these values (D26)
    counted = {
        "croc_soc": compare("croc_soc", Tracer(soc_ref), Tracer(soc_gen), renamed, out),
        "croc_domain": compare("croc_domain", Tracer(child(soc_ref.body, "i_croc")), Tracer(domain_gen), renamed, out),
    }
    print(f"tables in {out / 'connections'}")
    return counted


def main() -> int:
    if len(sys.argv) != 4:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    counted = compare_all(Path(sys.argv[1]), Path(sys.argv[2]).resolve(), Path(sys.argv[3]))
    return 1 if any(c["differences"] for c in counted.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
