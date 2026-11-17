# Real designs

Two SoCs the exporter was not written for, each turned into an IP-XACT library, generated, and checked against
what its own project holds (D24, D50). `tests/test_real_designs.py` runs all of it, and CI runs that on every push
at the commits named in `.github/workflows/build.yml`. Each script below also runs on its own and prints the
details. They only read the two projects' files: nothing of theirs is built or run.

```sh
git clone https://github.com/pulp-platform/croc ../croc
git clone https://github.com/Edu4Chip/Didactic-SoC ../Didactic-SoC
CROC_DIR=../croc DIDACTIC_DIR=../Didactic-SoC pytest tests/test_real_designs.py
```

## Croc

[Croc](https://github.com/pulp-platform/croc) (PULP, Solderpad) has a hand-written `croc_soc` and `croc_domain`
built from IPs with type parameters and ports that are arrays of structs.

- `build_library.py CROC_DIR OUT_DIR` writes the two modules as IP-XACT designs, from components `xactflow-sv`
  imports from Croc's RTL, an OBI bus written out here, and `croc_domain_glue.sv` for the three struct constants
  the hand-written module takes from packages. It prints each change it makes to what the importer wrote, and why
  is in its docstring.
- Then `xactflow rtl-gen OUT_DIR/pulp-platform.org_design_croc_soc_d_1.0.xml --lib OUT_DIR --output RTL_DIR
  --option common=clk_i,rst_ni,testmode_i,ref_clk_i`.
- `check_slang.py CROC_DIR RTL_DIR WORK_DIR` elaborates Croc's testbench in slang twice, as Croc wrote it and with the
  generated modules in their place. The second run has to say nothing the first does not.
- `compare_connections.py CROC_DIR RTL_DIR WORK_DIR` traces what drives every input bit of every instance and
  every output bit of both modules, in both elaborations, and compares them with every instance parameter's value.
  Its tables, in `WORK_DIR/connections/`, diff line for line.
- `flist.py CROC_DIR` writes Croc's file list from its Bender manifests, without Bender. The lists the checks
  write into `WORK_DIR/{ref,gen}/croc_rtl.f` also build Croc's Verilator simulation. Its firmware has been run on
  both, with the same output (D24), but a build takes too long for CI.

## Didactic SoC

[Didactic SoC](https://github.com/Edu4Chip/Didactic-SoC) (Edu4Chip) is IP-XACT written by another team in Kactus2,
with several levels of hierarchy and design configurations.

- `prepare_library.py DIDACTIC_DIR OUT_DIR` copies its `ipxact/` folder with the changes the standard or its RTL
  calls for: fan-outs Kactus2 splits into pairs merged, empty expressions dropped, the default ties Kactus2
  assumes written out, one port given the RTL's width, and four module parameters the RTL lacks dropped. Each
  change is printed, and why is in its docstring.
- Then `xactflow rtl-gen OUT_DIR/tuni.fi/soc/Didactic/1.2/Didactic.design.1.2.xml --config
  OUT_DIR/tuni.fi/soc/Didactic/1.2/Didactic.designcfg.1.2.xml --lib OUT_DIR --output RTL_DIR`.
- `compare_kactus2.py DIDACTIC_DIR OUT_DIR RTL_DIR` traces every bit of the eight generated modules and of the
  tops Kactus2 generated, which Didactic commits, with `verilog_netlist.py`, a reader of what the two generators
  write. A difference is a lead, not a verdict: agreeing with Kactus2 proves nothing about the standard.
- `check_with_rtl.py DIDACTIC_DIR RTL_DIR` elaborates the generated modules in slang with the RTL Didactic's
  repository holds, which has the module of most instances, and prints what slang says about generated lines.

## When a number changes

The tests compare with the numbers taken at the pinned commits. A change to the exporter that changes one has to
say why, in the commit and, if it is a decision, in `docs/decisions.md`. Moving a pin to a newer commit of Croc or
Didactic may change them too: run the scripts by hand, read what differs, and move the numbers with the pin.
