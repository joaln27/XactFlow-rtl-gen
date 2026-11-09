# Findings recorded, not fixed

`rtl-breaker` findings that don't start another round under D22 in `decisions.md`: anything but silent wrong
RTL that a real flow could hit. Each entry says what happens, how likely a real design is to hit it, and where its
reproducer is. When one is fixed, move it to the bottom section with the date, rather than deleting it.

Reproducers are kept outside this repository, by round (`run<N>/`), with a script that reruns them all.

## Open

### F1. A listed sub-port's type without its package is cast as written (round 8, 2026-11-09)
- **What happens:** a hand-written component lists a struct's sub-ports, and gives a member the type `mode_e`, as
  the package names it inside itself. A constant into that member is written `mode_e'(2)`, which the generated
  module can't see. Icarus, Verilator and slang reject it; the exporter gives no error of its own.
- **Expected:** the refusal a whole port already gets: "the generated module cannot see type ..., which needs its
  package in its name". `_check_type_is_visible` isn't applied to `_Port.sub_types`.
- **Silent wrong RTL:** no, the tools reject it. **Real flow:** no, as `xactflow-sv` and the test SoC's flow (D13)
  list no sub-ports.
- **Reproducer:** `run8/e1_unqualified_member_type.py`.

### F2. An enum member of a struct with no sub-ports listed gets no cast (round 8, 2026-11-09)
- **What happens:** `xactflow-sv` metadata can map a logical port into a struct member, such as
  `"PPROT": "req_i.pprot"`. If that member is an enum, the generated `assign t_req_i.pprot = m_pprot_o;` drives it
  from a `logic [2:0]`, and so does a tie-off or an abstraction default. slang rejects it, and Verilator stops with
  `ENUMVALUE` even with `-Wno-fatal`. Icarus accepts it and simulates the right value.
- **Why:** the importer lists no sub-ports (D14), so nothing tells the exporter the member's type. Refusing every
  connection to a member of such a struct would block real designs.
- **Silent wrong RTL:** no, slang and Verilator reject it. **Real flow:** yes, with real structs that have enum
  members, such as OpenTitan's `tl_h2d_t.a_opcode`.
- **Ways out:** list the struct's sub-ports in the component, which then gets the cast (D20); or have `xactflow-sv`
  list them, worth raising with its maintainer alongside the port-width draft.
- **Reproducers:** `run8/rf3/run.py` (through the importer and `archidesc`), and the `e4_unlisted_*` cases of
  `run8/e4_member_checks.py` for constants.

## Fixed later
