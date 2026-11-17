#!/usr/bin/env python
"""Elaborate the generated Didactic modules in slang together with the RTL Didactic's repository holds, and
print what slang says about lines of the generated files.

Didactic's own simulation needs Questa, and some of its IPs come through Bender, so the whole SoC cannot be
built here. Most of the modules the generated tops instantiate are in src/rtl, though, and slang checks each
instance against the real module: its parameters, its ports, and their widths. Only reads files.

Left out, as they only say that something is not in the repository: a module slang cannot find (the IPs
that come through Bender), a package it cannot find (theirs), and an output left open on purpose.

    python check_with_rtl.py DIDACTIC_DIR RTL_DIR
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import List, Tuple

from pyslang import ast, syntax

NOT_IN_THE_REPOSITORY = ("UnknownModule", "UnknownClassOrPackage", "EmptyOutputPortConn")


def check(didactic: Path, generated_folder: Path) -> Tuple[List[str], List[str]]:
    """What slang says about lines of the generated files, and a summary of what was read and left out."""
    sources = Path(didactic) / "src"
    generated = sorted(Path(generated_folder).glob("*.sv"))
    # src/reuse has a second sp_sram; the SoC's is the one in src/rtl.
    theirs = [p for p in sorted(sources.rglob("*.sv")) + sorted(sources.rglob("*.v")) if not {"generated", "tb", "reuse"} & set(p.relative_to(sources).parts)]
    compilation = ast.Compilation()
    for path in [*generated, *theirs]:
        compilation.addSyntaxTree(syntax.SyntaxTree.fromFile(str(path)))
    manager = compilation.sourceManager
    names = {p.name for p in generated}
    skipped: collections.Counter = collections.Counter()
    instances = sum(text.count("    // IP-XACT VLNV:") for text in (p.read_text() for p in generated))
    found: List[str] = []
    for diagnostic in compilation.getAllDiagnostics():
        file = Path(manager.getFileName(diagnostic.location)).name if diagnostic.location else "?"
        if file not in names:
            continue
        code = str(diagnostic.code).split("(")[-1].rstrip(")")
        if code in NOT_IN_THE_REPOSITORY:
            skipped[code] += 1
            continue
        line = manager.getLineNumber(diagnostic.location)
        text = (Path(generated_folder) / file).read_text().splitlines()[line - 1].strip()
        found.append(f"{code}: {file}:{line}: {text}   {[str(a) for a in diagnostic.args]}")
    summary = [
        f"{len(generated)} generated modules with {instances} instances, {len(theirs)} files of Didactic's RTL",
        f"left out: {', '.join(f'{count} {code}' for code, count in sorted(skipped.items()))}",
    ]
    return found, summary


def main() -> int:
    if len(sys.argv) != 3:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    found, summary = check(Path(sys.argv[1]), Path(sys.argv[2]))
    print("\n".join([*found, *summary, f"{len(found)} things slang says about the generated modules"]))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
