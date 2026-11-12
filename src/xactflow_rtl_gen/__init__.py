__version__ = "0.3.0"

from pathlib import Path
from typing import List, Sequence, Union

import ipxact
from xactflow import Exporter, Library, LinkedDesign, link

from .top_writer import GENERATED_MARKER, file_list, generate_files, generate_top

__all__ = ["RtlExporter", "__version__", "file_list", "generate_files", "generate_top"]


def _names(value: Union[str, Sequence[str]]) -> List[str]:
    """A list from Python, or the comma-separated form the CLI's --option passes."""
    items = value.split(",") if isinstance(value, str) else value
    return [item.strip() for item in items if item.strip()]


class RtlExporter(Exporter):
    """Write the SystemVerilog of a design into output_dir: <module>.sv, plus an address map package
    if the design records address windows, and the modules of any hierarchical instances.

    Exports the ipxact.Design that is a target of the library: the file given to `xactflow rtl-gen`,
    or the design an importer added. It is linked here against that library, which has to hold the
    components and bus definitions the design refers to (the CLI's --lib). A design configuration
    that is a target as well configures it (the CLI's --config). From Python, a LinkedDesign is
    accepted in place of the library, for a design linked another way.

    Options:
      common: input port names to share across instances, as one top-level port each (clocks,
              resets). A comma-separated string or a list.
      top:    the VLNV of the hierarchical component to take the module's name and ports from.
              Needed only when more than one component holds the design.
      unconnected: what to do with a port the design connects to nothing, in a module whose ports a
              component defines: an instance's input, or an output of the module. `refuse`, the
              default, or `open`, which leaves it as the design has it.
      filelist: the name of a file to write next to the modules, listing what was generated in an
              order that compiles, for a compiler's -F.
      socgen_package: the name of one more package to write, such as `soc_addr_map_pkg`: the address
              windows of every module generated, in PeakRDL-socgen's layout, for files written
              against that tool's package.
      socgen_names: what socgen calls the decoders and targets in that package, as `instance:name`
              pairs, comma-separated, or a dict. An instance not given goes by its own name.
    """

    name = "rtl-gen"

    def export(self, library: Union[Library, LinkedDesign], output_dir: Path, **options: object) -> None:
        common = _names(options.pop("common", ()))
        if "lib" in options:
            raise TypeError(
                "xactflow-rtl-gen: the lib option is gone. Since XactFlow 0.4 the library comes with the "
                "export: give its folders to the command with --lib, or add the documents to the Library"
            )
        top = options.pop("top", None)
        unconnected = options.pop("unconnected", "refuse")
        filelist = options.pop("filelist", None)
        socgen_package = options.pop("socgen_package", None)
        socgen_names = options.pop("socgen_names", None)
        if options:
            raise TypeError(f"xactflow-rtl-gen got unknown options: {', '.join(sorted(options))}")
        if filelist is not None and (
            not isinstance(filelist, str) or not filelist.strip() or Path(filelist).name != filelist or filelist in (".", "..")
        ):
            raise TypeError(f"xactflow-rtl-gen: the filelist option is a file name in the output folder, got {filelist!r}")

        files = generate_files(self._linked(library), common, top, unconnected, socgen_package, socgen_names)
        if filelist is not None:
            if filelist in files:
                raise ValueError(f"the file list cannot be called {filelist}: a generated module has that file")
            files[filelist] = file_list(files)

        output_dir = Path(output_dir)
        for name in files:  # before writing anything, so a refusal leaves no partial output
            _check_overwrite(output_dir / name)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (output_dir / name).write_text(text, encoding="utf-8")

    @staticmethod
    def _linked(library: Union[Library, LinkedDesign]) -> LinkedDesign:
        """The one design to export, linked against the library it came in (D44)."""
        if isinstance(library, LinkedDesign):
            return library
        if not isinstance(library, Library):
            raise TypeError(
                f"xactflow-rtl-gen exports the design that is a target of a Library, or a LinkedDesign, "
                f"got {type(library).__name__}"
            )
        designs = list(library.targets(ipxact.Design))
        if not designs:
            raise TypeError("xactflow-rtl-gen needs an ipxact.Design target in the library")
        if len(designs) > 1:
            names = ", ".join(str(design.vlnv) for design in designs)
            raise ValueError(f"xactflow-rtl-gen exports one design at a time, and the library has {len(designs)} as targets: {names}")
        configurations = list(library.targets(ipxact.DesignConfiguration))
        if len(configurations) > 1:
            names = ", ".join(str(configuration.vlnv) for configuration in configurations)
            raise ValueError(f"xactflow-rtl-gen takes one design configuration, and the library has {len(configurations)} as targets: {names}")
        return link(designs[0], library, configurations[0] if configurations else None)


_FORMER_MARKER = "// Generated by xactflow-rtl "  # its header under its former name, so that output can be regenerated (D30)


def _check_overwrite(path: Path) -> None:
    """Regenerating our own output is fine. Never replace a file a person may have written."""
    if not path.exists():
        return
    if path.is_dir():
        raise FileExistsError(f"{path} already exists and is a folder")
    with path.open(encoding="utf-8", errors="replace") as file:
        first_line = file.readline()
    if not first_line.startswith((GENERATED_MARKER, _FORMER_MARKER)):
        raise FileExistsError(f"{path} already exists and was not generated by xactflow-rtl-gen")
