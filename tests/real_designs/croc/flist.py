#!/usr/bin/env python
"""Write Croc's Verilator file list from its Bender manifests, without Bender.

Croc's run_verilator.sh gets croc_rtl.f from
`bender script flist-plus -t rtl -t verilator -t synthesis -D VERILATOR=1 -D COMMON_CELLS_ASSERTS_OFF=1`.
Every dependency is vendored under rtl/ (Bender.local points at them), so the same list can be read off the
manifests without Bender: packages in dependency order, each one's source groups whose
target expression holds, the include folders they export, and the defines.

    python flist.py CROC_DIR [--replace FILE=OTHER ...] > croc_rtl.f

`--replace rtl/croc_domain.sv=/path/to/generated.sv` swaps one source file for other files (comma-separated),
which is how the generated modules take the place of the hand-written ones.

Needs PyYAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

TARGETS = {"rtl", "verilator", "synthesis"}
DEFINES = ["VERILATOR=1", "COMMON_CELLS_ASSERTS_OFF=1"]


def holds(expression: str, targets: Set[str]) -> bool:
    """A Bender target expression: a name, or all(...), any(...), not(...) of expressions."""
    tokens = re.findall(r"[\w-]+|[(),]", expression)
    position = 0

    def parse() -> bool:
        nonlocal position
        word = tokens[position]
        position += 1
        if position < len(tokens) and tokens[position] == "(":
            position += 1
            values = []
            while tokens[position] != ")":
                values.append(parse())
                if tokens[position] == ",":
                    position += 1
            position += 1
            if word == "all":
                return all(values)
            if word == "any":
                return any(values)
            if word == "not":
                return not values[0]
            raise ValueError(f"unknown operator {word!r} in {expression!r}")
        return word in targets

    return parse()


class Package:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        manifest = yaml.safe_load((folder / "Bender.yml").read_text())
        self.name = manifest["package"]["name"]
        self.dependencies = list(manifest.get("dependencies") or {})
        self.export_include_dirs = [folder / d for d in manifest.get("export_include_dirs") or []]
        self.sources = manifest.get("sources") or []

    def collect(self, targets: Set[str]):
        """(files, include folders, defines) of the source groups that apply."""
        files: List[Path] = []
        include_dirs: List[Path] = []
        defines: List[str] = []

        def group(node) -> None:
            if isinstance(node, str):
                files.append(self.folder / node)
                return
            if isinstance(node, list):
                for item in node:
                    group(item)
                return
            if "target" in node and not holds(str(node["target"]), targets):
                return
            include_dirs.extend(self.folder / d for d in node.get("include_dirs") or [])
            for name, value in (node.get("defines") or {}).items():
                defines.append(name if value in (None, True) else f"{name}={value}")
            group(node.get("files") or [])

        group(self.sources)
        return files, include_dirs, defines


def file_list(croc: Path, replace: Optional[Dict[str, List[str]]] = None) -> str:
    """Croc's file list, with each file named in `replace` (relative to CROC_DIR) swapped for the files given."""
    croc = Path(croc).resolve()
    replace = replace or {}
    overrides = yaml.safe_load((croc / "Bender.local").read_text())["overrides"]
    root = Package(croc)
    packages = {root.name: root}
    paths = {name: croc / spec["path"] for name, spec in overrides.items()}
    for name, spec in (yaml.safe_load((croc / "Bender.yml").read_text()).get("dependencies") or {}).items():
        if "path" in spec:
            paths[name] = croc / spec["path"]

    order: List[Package] = []
    seen: Set[str] = set()

    def visit(name: str, folder: Path) -> None:
        if name in seen:
            return
        seen.add(name)
        package = Package(folder)
        for dependency in package.dependencies:
            if dependency not in paths:
                raise SystemExit(f"{package.name} depends on {dependency}, which has no local path")
            visit(dependency, paths[dependency])
        order.append(package)

    visit(root.name, croc)

    lines: List[str] = []
    for package in order:
        for folder in package.export_include_dirs:
            lines.append(f"+incdir+{folder}")
    for target in sorted(TARGETS):
        lines.append(f"+define+TARGET_{target.upper()}")
    lines.extend(f"+define+{define}" for define in DEFINES)
    used = set()
    for package in order:
        files, include_dirs, defines = package.collect(TARGETS)
        lines.extend(f"+incdir+{folder}" for folder in include_dirs)
        lines.extend(f"+define+{define}" for define in defines)
        for file in files:
            relative = str(file.relative_to(croc))
            if relative in replace:
                used.add(relative)
                lines.extend(replace[relative])
            else:
                lines.append(str(file))
    missing = set(replace) - used
    if missing:
        raise ValueError(f"files to replace that are not in the list: {', '.join(sorted(missing))}")
    return "\n".join(dict.fromkeys(lines)) + "\n"


def main() -> int:
    arguments = sys.argv[1:]
    replace: Dict[str, List[str]] = {}
    while "--replace" in arguments:
        index = arguments.index("--replace")
        old, new = arguments[index + 1].split("=", 1)
        replace[old] = [n for n in new.split(",") if n]
        del arguments[index : index + 2]
    if len(arguments) != 1:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    sys.stdout.write(file_list(Path(arguments[0]), replace))
    return 0


if __name__ == "__main__":
    sys.exit(main())
