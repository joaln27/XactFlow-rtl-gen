#!/usr/bin/env python
"""Elaborate Croc's testbench in slang twice, as Croc wrote it and with the generated croc_soc and croc_domain in
place of the hand-written ones, and compare what slang says.

Only reads the files: slang parses and elaborates, nothing of Croc's is run. The generated run has to raise no
error, and no warning on a line of a generated file that the hand-written run does not have its like of.

    python check_slang.py CROC_DIR RTL_DIR OUT_DIR

RTL_DIR is where xactflow-rtl-gen wrote the modules. Writes OUT_DIR/ref/croc_rtl.f and OUT_DIR/gen/croc_rtl.f,
Croc's file lists for both, which can also build Croc's Verilator simulation. Croc's testbench is used as it is
in both: it instantiates `croc_soc #(.GpioCount(32))`, and the generated module declares that parameter, for that
value (D36). Needs PyYAML for flist.py.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pyslang

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # -I leaves the script's folder off the path
from flist import file_list  # noqa: E402

GLUE = HERE / "croc_domain_glue.sv"
TOP = "tb_croc_soc"
Diagnostic = Tuple[str, str, str, int, str]


def file_lists(croc: Path, rtl: Path, out: Path) -> Tuple[Path, Path]:
    """Croc's file list as it is, in OUT_DIR/ref, and with the generated modules in place of croc_soc.sv and
    croc_domain.sv, in OUT_DIR/gen."""
    reference, generated = out / "ref" / "croc_rtl.f", out / "gen" / "croc_rtl.f"
    for path in (reference, generated):
        path.parent.mkdir(parents=True, exist_ok=True)
    rtl = Path(rtl).resolve()
    modules = sorted(p for p in rtl.glob("*.sv") if p.name != "croc_soc.sv")
    reference.write_text(file_list(croc))
    generated.write_text(
        file_list(croc, {"rtl/croc_domain.sv": [str(p) for p in [GLUE, *modules]], "rtl/croc_soc.sv": [str(rtl / "croc_soc.sv")]})
    )
    return reference, generated


def elaborate(flist: Path) -> List[Diagnostic]:
    """(severity, code, file, line, message) of everything slang reports."""
    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.parseCommandLine(f"slang -f {flist} --top {TOP}"):
        raise SystemExit(f"slang could not read {flist}")
    if not driver.processOptions() or not driver.parseAllSources():
        raise SystemExit(f"slang could not parse the sources of {flist}")
    compilation = driver.createCompilation()
    manager, engine = driver.sourceManager, driver.diagEngine
    found = []
    for diagnostic in compilation.getAllDiagnostics():
        location = diagnostic.location
        severity = engine.getSeverity(diagnostic.code, location)
        if severity == pyslang.DiagnosticSeverity.Ignored:
            continue
        # slang names a file relative to the working folder
        file = str(Path(manager.getFileName(location)).resolve()) if location else "?"
        line = manager.getLineNumber(location) if location else 0
        found.append((severity.name, str(diagnostic.code), str(file), line, engine.formatMessage(diagnostic)))
    return found


def summary(found: List[Diagnostic]) -> str:
    counts = collections.Counter(severity for severity, *_ in found)
    return ", ".join(f"{count} {severity.lower()}s" for severity, count in sorted(counts.items())) or "nothing"


def check(croc: Path, rtl: Path, out: Path) -> Dict[str, List[Diagnostic]]:
    """What slang says in each run, and what only the generated run says: on lines of the generated files, and
    on lines of Croc's own files that the hand-written run has nothing like."""
    rtl = Path(rtl).resolve()
    reference_list, generated_list = file_lists(croc, rtl, out)
    reference, generated = elaborate(reference_list), elaborate(generated_list)
    ours = {str(p) for p in rtl.glob("*.sv")} | {str(GLUE)}
    in_ours = [d for d in generated if d[2] in ours]
    # What both runs say about the same line of the same file is Croc's own.
    theirs = collections.Counter((d[0], d[1], d[2], d[3]) for d in reference)
    new_elsewhere = []
    for d in generated:
        key = (d[0], d[1], d[2], d[3])
        if d[2] in ours:
            continue
        if theirs[key]:
            theirs[key] -= 1
        else:
            new_elsewhere.append(d)
    return {"reference": reference, "generated": generated, "in_generated_files": in_ours, "new_in_croc_files": new_elsewhere}


def main() -> int:
    if len(sys.argv) != 4:
        print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("python ")), file=sys.stderr)
        return 2
    found = check(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"as Croc wrote it:           {summary(found['reference'])}")
    print(f"with the generated modules: {summary(found['generated'])}")
    for title, key in (("on lines of the generated files", "in_generated_files"), ("new on lines of Croc's own files", "new_in_croc_files")):
        items = found[key]
        print(f"{len(items)} {title}")
        for severity, code, file, line, message in items[:40]:
            print(f"  {severity} {code} {Path(file).name}:{line}: {message}")
    # what both runs say about Croc's own lines is Croc's
    return 1 if found["in_generated_files"] or found["new_in_croc_files"] else 0


if __name__ == "__main__":
    sys.exit(main())
