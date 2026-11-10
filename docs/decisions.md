# Decision log

The decisions that shape this exporter, why they were made, and what would reopen them. Newest last.
A decision that changes gets a new entry, and the old one is marked superseded rather than edited away,
so the reasoning behind each turn stays readable.

Each entry: what was decided, why, what else was considered, its status, and what would make it worth
revisiting. **Status** is one of *active*, *superseded by D-n*, or *to reconsider*. "The author" is the
person whose project this is. An AI assistant, Claude, wrote most of the code with them, and the `rtl-breaker`
rounds are an adversarial AI agent attacking it.

---

## D1. An exporter plugin in its own repository
*2026-11-06 · active*

- **Decision:** the RTL exporter is a XactFlow exporter plugin (`xactflow.exporters` entry point, `rtl`
  subcommand, `rtl-gen` since D30) in its own repository, packaged like the IP-XACT organisation's other exporters, under LGPL-3.0.
- **Why:** it is the "RTL exporter" block of XactFlow's architecture slide, and no public implementation
  existed. Matching the organisation's layout and licence keeps a later move into it simple.
- **Considered:** a module inside XactFlow itself; a separate tool outside XactFlow's plugin model.
- **Revisit if:** the maintainers want it in their organisation under another name or layout. Where it goes,
  and when, is the author's call.

## D2. Generate from a linked design
*2026-11-06 · active*

- **Decision:** the input is a `LinkedDesign` from `xactflow.link()`. A bare `ipxact.Design` is only
  accepted with the `lib` option, which links it first.
- **Why:** the wiring needs the components' ports and port maps, which only linking brings in. The slide
  still shows `elaborate()`, which XactFlow 0.2.0 removed.
- **Revisit if:** XactFlow's API changes how linking works. `link()` silently drops bad ad hoc references and
  ignores parameter overrides, so the exporter checks and applies those itself (see D19).

## D3. Refuse rather than write wrong RTL
*2026-11-06 · active*

- **Decision:** anything this version cannot generate correctly raises `ValueError`, naming the instance and
  port. Silent wrong RTL is the one outcome to avoid.
- **Why:** wrong hardware that compiles is far costlier than an error. It also matches upstream's "raise
  instead of dropping data".
- **Considered:** warnings, or best-effort output.
- **Revisit if:** refusals block real designs often enough that a supported path is worth building. The
  README lists what is refused.

## D4. One net per connected port, and one assign per connection
*2026-11-06 · active*

- **Decision:** each connected instance port gets a net, `<instance>_<port>`, and each connection is an
  `assign`. Logical slices and inouts get a shared net. A whole port wired to a module port connects
  directly, since D35 not where a part of it is taken and the bounds differ. A net's name gives way, adding `_net`, when a keyword,
  instance or module port needs it.
- **Why:** one shape covers part selects, inverts, tie-offs and sub-ports, and every line says who drives
  whom. It also makes bit-level comparison with other generators straightforward.
- **Considered:** wiring instance ports to each other directly, which reads more compactly but makes
  slices, inverts and checks harder.
- **Revisit if:** the generated size or readability becomes a real problem for users.

## D5. Module ports from the hierarchical component, and a `common` option
*2026-11-06 · active · D38 adds an option that leaves an unconnected input open*

- **Decision:** with a hierarchical component in the library, its ports define the module, and an
  unconnected input is an error. Without one, an unconnected port becomes a module port,
  `<instance>_<port>`. `common` lists inputs shared by every instance, like clocks.
- **Why:** the hierarchical component is the standard's way to define a module's ports. `archidesc` cannot
  declare module ports or fan one input out, so `common` fills that gap.
- **Revisit if:** `archidesc` gains module ports. `common` is this plugin's own, not IP-XACT; worth raising
  with the maintainer.

## D6. Kactus2's ideas, none of its code
*2026-11-06 · active*

- **Decision:** behaviour and checklists may come from Kactus2 (GPL-2) and PeakRDL-socgen (GPL-3); code may
  not. The author's example repository has no licence, so it is only read and run against.
- **Why:** this project is LGPL-3.0.
- **Lesson:** agreeing with Kactus2 is not evidence about the standard. A default-value behaviour copied from
  it turned out to contradict the standard (D8).

## D7. An address map package for existing decoders, not a generated crossbar
*2026-11-06 · active*

- **Decision:** windows recorded by `archidesc` become a package. It holds constants per window and one rule
  table per decoder, `{idx, start, end}` with an exclusive end and 0 meaning the end of the address space,
  as in PULP's `common_cells`. It also holds `ADDR_PAIRS`, the same windows as a flat vector. A decoder
  declaring `ADDR_RULES` and `NUM_RULES` gets its table automatically, unless the design sets them, and the
  count also sizes the ports it sizes, whether the module parameter has an id of its own or not. A design that
  sets only one of the two is refused, unless it sets the count the table has. Any other decoder is pointed at
  the table by a parameter override.
- **Why:** Kactus2's interconnect generator feeds PULP crossbars a table of this shape. The SoC it is tested on
  (D13) uses the same shape for its crossbar, and a flat start/end vector for another interconnect.
- **Considered:** generating the interconnect itself. Dropped once that SoC was chosen, since it has its own.
- **Revisit if:** a target needs a crossbar generated, or a decoder takes another format.

## D8. Default values follow IEEE 1685-2022 7.5.5 and Table C.2
*2026-11-06 · active, supersedes the first version*

- **Decision:** a port's own `defaultValue` applies only through an ad hoc `tiedValue="default"`, where it wins
  over the abstraction's default. An unconnected input with a default is otherwise treated like any other.
- **Why:** 7.5.5 of the standard says so explicitly: a port counts as connected to its own default value only
  through an ad hoc connection whose `tiedValue` is `default`. The first version tied such inputs to their
  default, copied from Kactus2, and was silently wrong.
- **Revisit if:** a later revision of the standard changes Table C.2.

## D9. Two readings the standard does not settle
*2026-11-06 · to reconsider when better information exists*

- **Decision:**
  - A `logicalTieOff` drives the receivers on the other side of a bus, unless a real driver exists. A tie-off
    on an end that would drive the port then conflicts with it and is refused.
  - `invert` applies on a hierarchical component's own port map as well as an instance's.
  - `invert` on a port map with a `logicalTieOff` is refused, as nothing says whether the constant is inverted.
- **Why:** the 2022 text was read for both (6.7.3.2, 6.5.5.2, 7.5.5, Annex B), and it settles neither. These
  readings follow its wording most directly. Kactus2 ignores `logicalTieOff` and that `invert`.
- **Revisit if:** the IEEE working group, Accellera's reference or a maintainer gives an answer. The README says
  both rest on a reading.

## D10. Every module parameter is written
*2026-11-06 · superseded by D19 on 2026-11-09*

- **Decision:** an instance gets every module parameter of its view, with its value in this design, not only
  the ones an override changed. A `runtime` parameter is left off, and an override on one is refused.
- **Why:** the HDL's own default can differ from the component's value, and nothing in IP-XACT says what the
  HDL's default is.

## D11. How a parameter's value is written
*2026-11-06 · active, after two superseded versions*

1. **Evaluate everything to a number.** Superseded: it lost what a literal says. `8'hA5` became 165, so
   `{P,P}` in an untyped HDL parameter came out wrong, and `'1` became 1.
2. **Write the expression with parameters substituted as text.** Superseded the same day: a substituted
   parameter loses its type and self-determined width. An imported IP with
   `DIV = CLK_HZ * 16 / BAUD` overflowed 32 bits inline. Port widths, from the evaluator, could also disagree
   with values, from the compiler. Several substitutions also produced invalid SystemVerilog.
3. **Current:** a value with no parameters is written as it stands. So is the module parameter's own value,
   which is what the HDL declares for it, and a parameter whose value is a single literal. Anything else using
   parameters is worked out by XactFlow's evaluator, the same one that sizes ports, and it honours declared
   types. That gives another parameter's expression its self-determined value, as SystemVerilog would: a
   `MASK` of `~8'h0F` is 240, not -16 once pasted into an `int`. Only what the evaluator cannot do, strings or
   package names, goes to the compiler with numbers filled in. Refined on 2026-11-09 after the third round.
   A worked-out value is written at the width and sign the evaluator gives it, `8'd255` rather than `255`, as an
   untyped HDL parameter takes its width from what it is given. An override goes through the parameter it
   overrides, so it keeps that parameter's type. Both from the fourth round, 2026-11-09.
   From the fifth round, the same day: a negative is written as its bits, `8'sh80` for -128, since `-8'sd128`
   is +128 once extended; an unsigned negative is its unsigned bits; a number its width can't hold, which the
   evaluator doesn't wrap, is left unsized; and a module parameter with a type of its own is cast to it.
   From the sixth round, the same day: an unsigned value with no width of its own, such as `BASE - 'h100` with
   unsized based literals, is written as 32 unsigned bits, `32'd4294967168`, not `-128`. A value outside a typed
   module parameter's own `minimum` or `maximum` is refused; that check used to be lost in the fallback for
   strings.

- **Why:** one source of truth for anything that depends on parameters, and exactness for literals.
- **Known gap:** the evaluator simplifies some sign rules: `-8'd1` used in an expression is -1 to it, 255 in
  SystemVerilog. And a parameter-free expression that also sizes a port is worked out by the compiler for the
  value and by the evaluator for the width. Where the evaluator simplifies SystemVerilog's sign and width rules, the
  two can differ. Tools warn about the port width.
- **Revisit if:** XactFlow's evaluator gets full SystemVerilog semantics, or this gap shows up in a real design.
  The width comes from the evaluator's private `_evaluate`; if a later XactFlow drops it, values fall back to
  plain numbers, and a test will say so.

## D12. Parameter scopes are lexical
*2026-11-06 · active*

- **Decision:** expressions written in a design see the design's parameters, then its hierarchical
  component's. A design configuration's values see its own parameters first. A parameter from further out
  is worked out in its own scope first, so a nearer parameter of the same name cannot change what it means.
  That covers its vector bounds too, since the sixth round: a `bit [W-1:0]` from the top component keeps the
  top component's `W`.
- **Why:** a parameter's value means what it meant where it was written. The standard's text on this was not
  checked.
- **Revisit if:** the standard says otherwise. The standard's text on it has not been read yet.

## D13. A real SoC is the test target
*2026-11-06 · active · decided by the author*

- **Decision:** the exporter is proven on a real SoC from the author's work, which is not public, using the SoC's
  own interconnects. Its IPs are imported with `xactflow-sv`, and its connections come from its SystemRDL
  description. Logic its current generator, PeakRDL-socgen, adds from extra files goes in two small glue modules.
  The result is compared with PeakRDL-socgen's output bit by bit, and the SoC's firmware tests run on both builds.
- **Why:** a real SoC with firmware tests is far stronger evidence than tests written alongside the code.
- **Rule:** the SoC is not public, so its files, results and findings are kept outside this repository.
- **Revisit if:** window sizes and module port lists stop needing PeakRDL-socgen's output, which is still the case.

## D14. Width checks on structs, as far as declarations go
*2026-11-06 · active*

- **Decision:** a struct's size is checked when its sub-ports are listed. A sub-port of a struct with none
  listed is not checked, as `xactflow-sv` lists none. Two whole ports of an unknown type connect only when
  the type is the same. Driving one member of a union drives all of it.
- **Revisit if:** `xactflow-sv` starts listing sub-ports.

## D15. Testing: real tools, adversarial rounds, and when to stop
*2026-11-06 · active*

- **Decision:**
  - Each connection scenario compiles in slang, Verilator and Icarus, and simulates. Every generated `assign` is
    then broken in turn, and the simulation has to notice.
  - The slang check counts warnings too, as pyslang's `isError()` misses real errors.
  - The Kactus2 comparison is a lead, not a verdict.
  - The `rtl-breaker` agent attacks the code in rounds, until one finds no silent wrong RTL a real flow could hit,
    which is the author's rule (D22, which replaced D18).
- **Why:** the first round found 34 problems in code that passed all of its tests, because those tests shared
  its author's assumptions.

## D16. A window's true end, in a wider table
*2026-11-06 · active, reverses a same-day fix*

- **Decision:** a window at the top of a narrow bus, in a table made wider by another bus, keeps its true end
  rather than 0.
- **Why:** an end of 0 in the wider table would claim every address above the window. A fix that wrapped it to 0
  was made and then reverted the same day, once a reproducer showed the overlap.

## D17. Upstream issues and the standard
*2026-11-06 · active*

- **Decision:** problems found in other projects are drafted as issues outside this repository, for example
  `xactflow-sv`'s port widths and XactFlow's evaluator (D11), and filing them is the author's call. The standard's
  text is used under a licence, so it is read for specific questions and not quoted at length.
- **Revisit if:** the issues are filed, or the maintainers fix them. The test SoC's build script (D13) works around
  the importer bugs until then.

## D18. Rounds of `rtl-breaker` until one leads to no fixes
*2026-11-09 · superseded by D22 the same day · the author's rule*

- **Decision:** run `rtl-breaker` again only when the last round led to fixes. Rounds so far found 34, 28, 15, 4,
  5, 4 and 3 problems, and each set was fixed or refused by name. From the third to the fifth, all were in how
  parameter values are written, not in the wiring; the sixth and seventh found constants on typed ports (D20, D21). Its reproducers and fuzzers are kept outside
  this repository, with a script that reruns them all after a change.
- **Why:** each round attacks what the last one's fixes changed; a round with nothing to fix means the
  rewritten code held.
- **Revisit if:** rounds stop paying for themselves, for example by finding only cases no real design writes.

## D19. Every module parameter is written, except one the HDL derives
*2026-11-09 · active · supersedes D10 · a trade-off worth reviewing*

- **Decision:** an instance gets every module parameter of its view, with its value in this design, as in D10,
  with one exception. A parameter whose value is worked out only from the instance's other module parameters,
  such as an imported `B = A + 8'd100`, is left to the HDL, unless an override reaches it directly. The HDL then
  works it out from the values given to those, exactly as it declares it. A `runtime` parameter is still left
  off, and an override on one refused.
- **Why:** `xactflow-sv` imports every HDL parameter untyped, with its default expression as its value, so
  writing every parameter meant re-evaluating each default with XactFlow's evaluator. Where its sign and width
  rules are simpler than SystemVerilog's (D11), the instance got a different value from the HDL's own default,
  with no override in sight: `B` above is 44 in SystemVerilog and 300 to the evaluator. Found in the sixth round.
  In the test SoC (D13) this leaves 11 derived values, such as address and data widths, to the HDL.
- **Bounds:** a parameter left to the HDL is still worked out once, only to check its `minimum` and `maximum` and
  those of the parameters its value goes through, as the seventh round found that skipping it skipped them. Where
  the evaluator simplifies, this can refuse a value the HDL would accept, which D3 prefers to the opposite.
- **The cost:** this trusts that the HDL derives the parameter the way the component says. That holds for an
  imported IP, since the importer copied the expression from the HDL. A hand-written component whose derivation
  disagrees with its HDL now gets the HDL's, where D10 would have written the component's.
- **Considered:** writing the evaluator's value, which is D10, and wrong where the evaluator simplifies; writing
  the expression with each input cast to its type, which would be exact but brings back the text substitution
  D11 moved away from; refusing any value whose evaluation might be simplified, which can't be told reliably.
- **Revisit if:** XactFlow's evaluator gets full SystemVerilog semantics (`interop/issues/xactflow-evaluator-sign-width.md`),
  which would make D10 safe again, or a real component turns up whose derived value differs from its HDL.

## D20. A constant or an inverted value on a typed port is cast to the type
*2026-11-09 · active*

- **Decision:** a tie-off, an abstraction default or an inverted connection that drives a port of a named
  type, such as an enum or a packed struct, is written as `type'(value)`. So is one that drives a listed
  sub-port of a named type, from the seventh round. A `tiedValue="default"` on such a port is refused, since its
  default can't be read as bits, and so is a constant or an inversion into an unpacked struct, which no cast
  turns a number into. *Narrowed by D27: one default for the whole port is now taken as a constant.*
- **Why:** SystemVerilog has no implicit conversion from a number to an enum. All three tools rejected the
  uncast assign, which the sixth round found. Imported IPs with enum inputs, such as OpenTitan's `mubi4_t`, are
  often tied to constants. slang calls the cast of an inverted enum useless, but Icarus and Verilator need it, so
  the tests accept that warning.
- **Revisit if:** a typed port's default value needs supporting, or the importer starts giving enum values as
  names.

## D21. Where only the compiler knows a width, the compiler checks the constant
*2026-11-09 · active*

- **Decision:** a constant tied to a port or sub-port whose width the exporter can't know, such as an enum or a
  struct member nothing lists, comes with an elaboration-time check in the generated module:
  `if ($bits(net) < N) begin : net_fits $error("..."); end`, where N is the fewest bits that hold the constant.
  A constant of one bit needs no check. Its label gives way to any other name in the module, as a net's does.
- **Why:** D20's cast turned a compile error into silent truncation: `mode_e'(6)` into a 2-bit enum compiled
  cleanly in all three tools, where the uncast assign had been rejected. D3 promises that a constant too wide for
  its port is refused, and here only the compiler can tell. Found in the seventh round.
- **How strong:** slang and Icarus 13 stop with the message. Verilator stops too, but reports it as a warning
  (`USERERROR`), so a build with `-Wno-fatal` only prints it. Icarus 12, the version Ubuntu 24.04 ships, ignores
  an elaboration task inside a generate block (its `PGenerate::elaborate_` never runs them), so there the constant
  still compiles and is cut. Found by the first CI run. Lint with slang or Verilator to rely on the check.
- **Considered:** refusing every constant into a port of unknown width, which would refuse the enum tie-offs real
  imported IPs need; reading the width from the package source, which IP-XACT doesn't always point to.
- **Revisit if:** importers start giving the width of a typedef, so the exporter can check it itself.

## D22. Rounds of `rtl-breaker` until one finds no silent wrong RTL a real flow could hit
*2026-11-09 · active · supersedes D18 · the author's rule*

- **Decision:** run another round only when the last one found silent wrong RTL that a real flow could hit. Every
  other finding is recorded in `docs/findings.md` with its reproducer, and doesn't start a round.
  - **Silent wrong RTL:** RTL that compiles cleanly and does something other than the design says. RTL the tools
    reject, a crash, and a missing refusal next to correct RTL don't count.
  - **A real flow:** input as XactFlow's own tools write it, with components from `xactflow-sv` and designs from
    `xactflow-archidesc`, or as the test SoC's flow (D13) writes it: imported IPs, buses written out, and ordinary
    instances and connections. Hand-written XML that only exercises a corner of the standard doesn't count.
  - Each round's report says, for each finding, whether it meets both.
- **Why:** under D18 the rounds were not ending. Rounds 4 to 7 found 4, 5, 4 and 3 problems, and the later ones were
  mostly in code the previous round's fixes had added. They needed hand-written components or out-of-range
  constants; none came through the import flow, and the test SoC's RTL stopped changing after round 6. An adversarial
  agent pointed at new code nearly always finds something, so "nothing to fix" may never come.
- **Considered:** a fixed cap on rounds; keeping D18.
- **Note:** fixing a recorded finding is a separate choice, made case by case. A fix is still covered by the tests and
  `rerun_all.sh`, but no new round attacks it unless a qualifying finding starts one anyway.
- **Revisit if:** a recorded finding turns up in a real design, or a new importer or flow widens what "real" covers.
- **So far:** round 8, the first under this rule, found no silent wrong RTL at all, from a real flow or a hand-written
  one, and two problems the tools reject (F1 and F2 in `findings.md`). So the rounds ended there, on 2026-11-09,
  after 34, 28, 15, 4, 5, 4, 3 and 2 findings.

## D23. Pinned below XactFlow 0.4 while it uses a private function
*2026-11-09 · active*

- **Decision:** the package requires `xactflow>=0.3.0,<0.4`.
- **Why:** the width and sign of a worked-out value come from `xactflow.expressions._evaluate`, which is private
  (D11). A rename falls back to plain numbers, but a changed signature would crash, or worse, change what a value
  means. The organisation's other plugins only set a lower bound, as they use the public API alone.
- **The cost:** with this installed, pip won't move to XactFlow 0.4 until a release of this plugin allows it.
- **Revisit if:** XactFlow makes width and sign public, which is worth asking its maintainer for; then use that, and
  drop the upper bound.

## D24. Another real SoC only when it brings input the earlier ones did not
*2026-11-09 · active · the author's rule*

- **Decision:** test the exporter on another real SoC only if it brings a kind of input none of the earlier ones
  did, and stop once one finds no new class of exporter problem. A problem in an importer, or in a test's own import
  script, is recorded but doesn't count.
- **The plan this gives:** three in all.
  - The first (D13), whose IP-XACT this project's own script writes.
  - [Didactic SoC](https://github.com/Edu4Chip/Didactic-SoC) (Edu4Chip): IP-XACT written by another team in
    Kactus2, with several levels of hierarchy and design configurations, compared with the tops Kactus2 generated.
  - [Croc](https://github.com/pulp-platform/croc) (PULP), only if Didactic finds a new class of problem: struct
    ports, a hand-written top to compare with, and firmware run with open-source tools.
  - Accellera's Leon2 example, as a check that the standard's own 1685-2022 example generates and compiles, if its
    terms allow.
- **Why:** what a SoC covers matters more than how many there are. The first one's IP-XACT comes from this project,
  so it cannot test the reading of the standard against someone else's. Past that, more SoCs of the same kind mostly
  retest `xactflow-sv` and the import scripts, and one whose IP-XACT has to be built first takes days.
- **Considered:** a fixed number of SoCs; larger ones such as X-HEEP or OpenTitan, which would mostly exercise the
  importer.
- **Revisit if:** a new importer or flow widens what the exporter is fed (as in D22), or a problem turns up on a design
  unlike these.
- **So far:** Didactic SoC (version 1.2 of its design) found a new class of problem on 2026-11-09, so the plan goes on
  to Croc after it.
  - **Fixed:** a view that names both a design and a design configuration, as Kactus2 writes it, had the parameter
    overrides on its design reference dropped without a word, so the design's own defaults were used. This was
    silent wrong RTL: a value the overrides change, that no width check sees, came out wrong.
  - **Added to generate it:** values on a design reference (D25), overrides on a hierarchical instance (D26), a
    typed port tied whole to its default (D27), an unpacked array output left open (D28), and the standard's
    reading of a port map with no logical range (D29).
  - **In the input, not the exporter,** and changed by the test before linking: Kactus2 splits a fan-out into one
    interconnection per pair, which SCR 2.3 rules out; it writes empty `left`, `right` and `defaultValue`
    expressions where nothing was entered; it ties an input nothing connects to its default, which the standard
    only does with `tiedValue="default"` (D8), so those ties are written out; three module parameters refer to
    themselves; and one port is wider in the IP-XACT than in the RTL.
  - **Read as the standard says, after asking it:** two narrow address ports mapped with no logical range, which
    the exporter refused and Kactus2 connects to the low bits (D29).
  - **Result:** all eight modules generate. Every input and output bit of every instance connects as in Kactus2's
    Verilog, both the committed one and a fresh run, except the eight bits of the port the RTL makes narrower.
  - **Against its RTL, on 2026-11-10:** the generated modules were elaborated in slang with the RTL Didactic's
    repository holds, which has the module of 26 of their 36 instances. One more problem in the input showed:
    the Ibex component lists four module parameters its RTL wrapper does not have. Kactus2 writes only the
    parameters a design changes, which hides them; this exporter writes them all (D19), so the module would not
    compile. The test now drops them. What is left is one inout of the pad frame's RTL that its component lacks.
- **Croc, on 2026-11-10,** found new classes again, none of them silent wrong RTL. Its two hand-written modules,
  `croc_soc` and `croc_domain`, were described in IP-XACT from components `xactflow-sv` imported.
  - **Added to generate it:** type parameters (D31), and elements and members of ports that are arrays of structs
    (D32). Before, each instance of a PULP IP was refused.
  - **Fixed:** a parameter whose value calls a package's function, named by another parameter, recursed until
    Python stopped it, and the error said so. Its value is now written out.
  - **In the input, not the exporter,** and changed by the test after importing, as `xactflow-sv` reads a module's
    header without elaborating it: a call of a package's function in a width, written out as its body; a member
    of a struct-valued parameter in a width, replaced by its value; a type the module gets through
    `import pkg::*`, given its package; and the modules' own parameters, as a generated module takes none (D26).
    One glue module holds the three struct constants the hand-written module takes straight from packages,
    which an IP-XACT `tiedValue` cannot name.
  - **Result:** both modules generate, `croc_domain` as `croc_domain_1`, for the testbench's `GpioCount`. Croc's
    whole testbench elaborates in slang and lints in Verilator with them in place, with the same messages as with
    the hand-written ones. A trace of both elaborations finds the same driver for each of the 3769 input bits of
    the 22 instances and output bits of the two modules, and the same value for each of their 110 parameters.
    Croc's firmware, nine programs run in Verilator, passes on both and prints the same lines at the same
    simulation times. Verilator 5.022 builds neither with the two `--unroll` limits of Croc's own script, so
    both were built without them.
  - **With D36,** later that day: the two components keep the module parameters `xactflow-sv` imported, and
    Croc's testbench, which instantiates `croc_soc #(.GpioCount(32))`, is used as it is, where the test had
    needed a copy without the parameter. The modules differ from those above only in the parameters they
    declare and check, and in the values `croc_soc` gives its `croc_domain_1`. Given a `GpioCount` of 16, the
    testbench is refused by the module's own check.
- **Topwrap's examples, the same day:** five small designs whose 1685-2022 files Antmicro's Topwrap writes, tried as
  a third tool's IP-XACT. All five are refused, four of them for what is in the files: instance names that are
  no names (`2mux`, `4-bit counter`), a bus definition the library lacks, and a port whose width ignores its
  parameter. They brought one change, D33. The fifth leaves an input unconnected, which D5 refuses, and an
  output of a module undriven; with `unconnected=open` (D38) its five modules generate.

## D25. A design reference's values set the design's parameters
*2026-11-09 · active*

- **Decision:** the `configurableElementValues` on a component's reference to the design it holds give that
  design's parameters their values. Each value is an expression in the component, worked out with the
  component's own parameter values, and those of the instance it is generated for (D26). This applies at the
  top and at every hierarchical instance, also when the view names a design configuration as well, which is how
  Kactus2 writes every hierarchical component. A value for a parameter the design lacks, or one naming nothing in
  the component, is refused.
- **Why:** it is what the reference means: the design's parameters are configurable elements of the design, and
  the reference configures them. Didactic SoC sets every design parameter this way, from the top component's.
  Before this, such values were refused, except next to a design configuration, where they were dropped and the
  design's defaults used without a word.
- **Considered:** keep refusing them; that left every Kactus2 hierarchy out.
- **Revisit if:** XactFlow's `link()` starts applying these values itself; then take its result.

## D26. One module for each set of values a hierarchical instance is given
*2026-11-09 · active · since D36 a generated module declares parameters, each for the one value it is generated for*

- **Decision:** a hierarchical instance whose overrides change a parameter of its component gets a module of its
  own, generated with those values throughout its design: `<module>_1`, `<module>_2`, ... in the order they turn
  up, one for each distinct set of worked-out values. Instances with the same values share a module, and values
  that change nothing keep the plain module. The module's first lines name the values it is for. An override for a
  parameter the component lacks is refused, and so is a version whose name another module already has.
- **Why:** a generated module's widths and values are worked out by the evaluator (D11), so the module takes no
  parameters, and its port
  widths and inner instances depend on the values. Kactus2 writes one module per instance for the same reason,
  `<module>_0`. Didactic SoC passes its top's values to its system-control subsystem, which changes four of them.
- **Considered:** generated modules with SystemVerilog parameters, set on each instance. Every width check and
  value in the module would then have to carry expressions instead of numbers. One module per instance, as
  Kactus2 does, which repeats identical modules.
- **The cost:** the name of a version depends on the order instances turn up, so adding one can renumber another.
- **Revisit if:** generated modules get parameters for another reason, or someone needs stable version names.

## D27. A typed port tied whole to its default takes that default as a constant
*2026-11-09 · active · narrows D20*

- **Decision:** `tiedValue="default"` on a whole port of a type only the compiler knows, such as an enum or a
  struct, takes the port's default as one constant, cast to the type (D20) and checked by the compiler (D21), like
  any other tie. A default given in parts, a tie on part of the port, and a default that is not a number are
  still refused.
- **Why:** D20 refused it because such a default cannot be read as bits. A whole default for a whole port needs
  no bits: it is one value. Didactic SoC ties Ibex's `ram_cfg_i`, a struct, to its default of `'h0`.
- **Revisit if:** a default given in parts is needed; that needs the type's layout, which only the compiler has.

## D28. An unpacked array output of an instance can be left open
*2026-11-09 · active*

- **Decision:** an instance's port that is an unpacked array is accepted when nothing connects to it and it is an
  output or an inout of a module whose ports a component defines: it is written as `.port()`. A connection to
  it, an input, and one that would have to become a port of the module are refused, as all unpacked arrays were.
- **Why:** leaving an output open needs nothing of its shape. Didactic SoC has one array port, the PULP GPIO's
  `gpio_padcfg`, and nothing uses it.
- **Revisit if:** a design connects an unpacked array port; then the array needs supporting in full.

## D29. A port map with no logical range takes its port's bits from logical bit 0
*2026-11-09 · active · the standard, read for the author*

- **Decision:** a port map whose logical port has no range, onto a wire port, takes logical bits [the port's
  width - 1:0], or of its part select. Ends of a bus that map different numbers of bits then connect bit by
  bit through the logical port's net, by logical bit number: a narrow receiver gets the low bits. A bit an end
  reads that no end drives is refused. Where bits are picked by number like this, or by an explicit logical
  range, each end's bits have to lie within the width its abstraction gives the logical port in that mode (SCR
  6.19); ends that all map whole ports of one width pick none, so that isn't checked there.
- **Why:** Annex B.1.7 b) of IEEE 1685-2022 says so, normatively: with no logical range on a wire port, the range
  is the physical port's width counted from 0. SCR 6.18, that both sides of a port map are the same size, then
  holds by construction. Clause 6.5.5.2 says connections are made by logical bit number. The exporter had read
  a missing range as the whole logical port and refused the different widths; it wrote no wrong RTL, but it
  refused valid designs, such as Didactic SoC's 12-bit memory addresses on a 32-bit bus. Kactus2 reads it the
  standard's way.
- **Not settled by the standard:** what a logical bit mapped by only one end gets. Bits a driver has and no
  receiver reads are left unread; bits a receiver reads and no end drives are refused.
- **Considered:** checking SCR 6.19 everywhere. Kactus2 libraries leave an abstraction's width at its default
  where every end maps the same whole port, as Didactic does for a 250-bit configuration bus with a width of 5;
  refusing those would prevent no wrong RTL, and checking the SCRs is XactFlow's part (6.19 has no check there
  yet).
- **Revisit if:** the standard or its working group says what a bit only one end maps gets; or a non-wire port
  needs a default range, for which the standard gives none.


## D30. Renamed to XactFlow-rtl-gen
*2026-11-09 · active · the author's choice*

- **Decision:** the plugin is XactFlow-rtl-gen: the package `xactflow-rtl-gen`, the module `xactflow_rtl_gen` and
  the subcommand `rtl-gen`, as the organisation names each plugin, its package and its subcommand after its
  repository. Generated files start with `// Generated by xactflow-rtl-gen`. A file that starts with the former
  header, `// Generated by xactflow-rtl `, still counts as generated, so it can be regenerated over.
- **Why:** the author's choice of name, made before anything was published under the old one.
- **The cost:** `xactflow rtl` is now `xactflow rtl-gen`, and a script that imports `xactflow_rtl` needs the new
  name.
- **Considered:** renaming only the package and keeping the shorter subcommand `rtl`, which would break the
  organisation's pattern.

## D31. A type parameter stands for the type its instance gives it
*2026-11-10 · active*

- **Decision:** a module parameter whose `dataType` is `type`, as `xactflow-sv` imports SystemVerilog's
  `parameter type`, stands for a type. A port whose type's name is such a parameter is declared with the type
  the instance gives it: an override, else its default, followed through a parameter that names another.
  - `logic`, `bit` or `reg` with dimensions, such as `logic [W-1:0]`, makes the port a plain vector, whose
    width is worked out and checked like any other.
  - A named type has to carry its package, `pkg::req_t`, as a generated module imports nothing.
  - Packed dimensions written in a type's name, `pkg::req_t [N-1:0]`, are read as the port's dimensions and
    worked out. `xactflow-sv` leaves them there, where they used to go into the module with `N` unexplained.
  - A type parameter worked out from the module's other parameters is left to the HDL, like any other (D19).
    A keyword is no parameter's name there, so `logic [W-1:0]` counts as worked out from `W`.
  - A design can give a type parameter a vector with its own parameters in the bounds.
- **Why:** PULP's IPs take the types of their bus as type parameters, and the nets between them need those types.
  Croc (D24) has them on nearly every instance. Before this each one was refused: the generated module "cannot
  see type obi_req_t", and a default such as `logic [AddrWidth-1:0]` could not be evaluated.
- **Considered:** leaving the type parameter's name on the net, which no module outside the IP can declare;
  waiting for the importer to elaborate types, which it does not do by design.
- **The limits:** a type that is neither a vector nor a named type, such as a signed vector or a struct written
  out, is refused. So is a bound the evaluator cannot work out, such as a call of a package's function: Croc's
  test writes `cf_math_pkg::idx_width(N)` out as its body.
- **Revisit if:** `xactflow-sv` marks type parameters another way or starts to elaborate them, or components
  begin to use IP-XACT's own `typeParameters`.

## D32. A part select picks elements of an array of structs, and an element has members
*2026-11-10 · active · rests on the schema*

- **Decision:** on a port that is a one-dimensional packed array of a type only the compiler knows, a part select
  with a range picks elements. `[3:3]` is one element, written `[3]`: it connects to a port of the element's type,
  takes a constant cast to that type, and can have sub-ports, `[3].a.addr`. `[5:2]` is a run of elements, and
  connects whole to an array of as many. What is driven is tracked for each element, so an input that is an
  array needs every element driven, whole or member by member, and every member where the component lists them.
- **Where the part select goes:** in a port map it is the `partSelect` of the `physicalPort`, before its
  `subPort`s. In a design's port reference it is the reference's own `partSelect`; each `subPortReference` has
  one of its own for the sub-port it names, which is still refused.
- **Why:** a crossbar's ports are arrays of structs, one element for each manager or subordinate, and Croc wires
  the members of some elements to IPs whose bus signals are separate ports. Before, any part select on a struct,
  and any sub-port of an array, was refused.
- **Not settled here:** whether an element of a packed dimension could also be picked with `indices`. The schema
  allows both forms; only `range` is read, and `indices` are refused as before. The standard's text was not read
  for this.
- **The limits:** an array with several dimensions, a constant into a run of elements, and a part select on a
  sub-port are refused. Members of a struct that lists none stay unchecked (D14): a member nothing drives then
  goes unnoticed, where it is refused by name when the members are listed.
- **Revisit if:** the standard's text says how a packed dimension of a structured port is indexed.

## D33. A component with no view takes its only component instantiation
*2026-11-10 · active · a reading to review*

- **Decision:** when no view of a component instantiates anything and it has exactly one component
  instantiation, that one gives the module's name and parameters. With several and no view, the instance is
  refused. A view that names a component instantiation the component lacks is refused too; it used to count as
  having none.
- **Why:** Topwrap writes components this way: module parameters in a component instantiation, and no views.
  Ignoring the instantiation lost its module name and parameters without a word; an override on such a
  component was refused as reaching no module parameter, which pointed the wrong way.
- **Considered:** keeping to the letter, where only a view brings an instantiation in. The component then says
  nothing a generator can use, and what it does say is dropped.
- **Revisit if:** the standard gives a view-less component a meaning.

## D34. What goes into the module as it stands is checked first
*2026-11-10 · active*

- **Decision:** three things are written as the IP-XACT has them, and each now has to be what it claims.
  - A parameter's value has to be one value: its own brackets closed, and no comma or semicolon outside them.
  - A module parameter's name has to be an identifier, and no keyword.
  - A port's type has to be a type's name: `pkg::t`, with `signed` or `unsigned` after it at most.
  A percent sign in a name is doubled in the message of the compiler's check (D21), which `$error` reads as a
  format.
- **Why:** found by reading the code. A value `2), .OTHER(40`, a parameter called `WIDTH(3), .OTHER`, or a type
  `pkg::t; assign x = 1; logic` came out as SystemVerilog that set or drove something else. The evaluator stopped
  most of them, and the compilers most of the rest, but nothing here did. None can come from `xactflow-sv` or
  `archidesc`, and none is valid IP-XACT; XactFlow does not check names against the schema.
- **Revisit if:** XactFlow validates names and expressions itself.

## D35. A port goes straight to a module port only when both number their bits alike
*2026-11-10 · active · narrows D4*

- **Decision:** a whole port wired to one of the module's own ports connects directly, with no net in between,
  when the two have the same bounds, or when no connection takes a part of either. Otherwise the instance's port
  keeps a net of its own and an `assign`.
- **Why:** found by reading the code, and it was silent wrong RTL. A port connected directly goes by the module
  port's name everywhere. A part select on it in another connection, `[3:0]`, was written with the instance's
  numbers onto the module port's name. With a module port declared `[0:7]` or `[8:1]` that read other bits of the
  port, and nothing complained; with `[15:8]` it read past the port, which Icarus simulates as x. It takes a
  hierarchical component whose port has other bounds than the instance's, and a second connection on a part of
  the same instance port. The test SoC (D13), Didactic and Croc have none, and their output did not change.
- **Considered:** renumbering the part select for the module port, which is right too, but makes the line say
  something the design does not.

## D36. A generated module declares its component's module parameters, each for one value
*2026-11-10 · active · changes D26's "the module takes no parameters"*

- **Decision:** when the view that holds a design also names a component instantiation, its module parameters
  become parameters of the generated module. Each is declared with its `dataType`, if it has one, and with the
  value the module is generated for: the component's, with the overrides of the instance it is generated for
  (D26). Under the header the module has the compiler check each one,
  `if (W !== 16) begin : W_as_generated $error("..."); end`, so an instance that gives another value is refused
  at elaboration. A real number or a string is compared with `!=`, which `!==` does not take.
  - A hierarchical instance is given those values in its `#(...)`, like any instance. An override on it may
    name a module parameter of that view, or a parameter of its component instantiation, as on any other
    instance; before, only the component's own.
  - A parameter the HDL would work out from the others (D19) is declared with its worked-out value: here the
    generated module is the HDL.
  - The module's ports and parameters see the component instantiation's parameters. Expressions in the design
    still see only the component's (D12).
- **Why:** code written by hand instantiates these modules with parameters. Croc's testbench has
  `croc_soc #(.GpioCount(32))`, so the generated module could not take the hand-written one's place without a
  changed testbench. `xactflow-sv` imports every module with its parameters, so a hierarchical component made
  from an import was refused until they were taken out. Accellera's IP-XACT User Guide has such a component in
  its Example 67.
- **What it is not:** the parameters do not configure the module. Everything in it is still worked out for one
  set of values (D11), and another set still needs another module (D26). The guide's netlist passes the
  expression on, `B #(.pB((pA*4)+7))`; this one writes `B #(.pB(19))` and refuses a `pA` other than 3.
- **How strong:** as D21. slang and Icarus 13 stop. Verilator reports it as a warning, `USERERROR`, which stops
  a build unless it runs with `-Wno-fatal`. Icarus 12 skips it.
- **Refused:** a type parameter on a generated module; a `dataType` that is not a type's name, or a typedef
  without its package; a value that cannot be compared, such as an assignment pattern; a parameter named like
  a port or an instance of the module.
- **Considered:** carrying expressions instead of numbers through the module, D26's alternative, which is the
  real thing and changes every width check; declaring the parameters with no check, which would take another
  value without a word; `$fatal`, which Verilator also only warns about.
- **Revisit if:** a module has to be configurable after it is generated. Then the expressions have to be
  carried.

## D37. A design configuration reference's values set the configuration's parameters
*2026-11-10 · active*

- **Decision:** the `configurableElementValues` on a component's reference to its design configuration give
  that configuration's parameters their values, worked out in the component, as a design reference's are (D25).
  They belong to the configuration the reference names: where the design was linked with another one, they are
  refused, not given to it. A value for a parameter the configuration lacks is refused.
- **Why:** it is D25 again, for the other document a view can name. The user guide's Example 68 passes a
  component's parameters to a module parameter this way, through the configuration's own parameter and a
  `viewConfiguration`. Such values used to be refused.
- **Revisit if:** XactFlow's `link()` starts applying these values itself.

## D38. An option leaves what the design connects to nothing as it is
*2026-11-10 · active · narrows D5*

- **Decision:** with `unconnected=open`, a port the design connects to nothing stays that way, in a module
  whose ports a component defines. An instance's input is left open, `.port ()`, with a comment next to it,
  and an output of the module that nothing drives is left undriven, with a comment under the module's header.
  The default, `refuse`, is D5 as it was. A port connected or driven only in part is still refused. With no
  hierarchical component such an input still becomes a port of the module.
- **Why:** D5 calls a floating input a bug, which it is in a design one is writing. A design from elsewhere
  may leave ports unconnected on purpose: one of Topwrap's examples has an input and an output it calls
  empty, and adding `tiedValue="open"` to each in someone else's file is not always possible. It is also what
  the design says: 7.5.5 connects such a port to nothing, not even its default (D8). Linters still report
  each input, as slang's `EmptyInputPortConn` and Verilator's `PINCONNECTEMPTY`. That example now generates.
- **Considered:** tying them to their default values, as Kactus2 does, which the standard rules out (D8); a
  warning in place of an option, which the plugin interface has no way to give.
- **Revisit if:** XactFlow gets a way to report warnings; then the default could warn and go on.

## D39. A file list, when asked for
*2026-11-10 · active*

- **Decision:** `filelist=<name>` writes a list of the files a run generated into the output folder: packages
  first, then the modules, the design's own last, one name on a line, relative to the list, for a compiler's
  `-F`. It starts with the same header as the modules, so it is regenerated over and never replaces a file
  written by hand.
- **Why:** a build system has to know what was generated, and in which order it compiles: an address map
  package before its module. A version's name can change between runs (D26), and the old file stays in the
  folder; the list says which files belong to this run.
- **Considered:** always writing `<module>.f`, which adds a file to every output folder and may find a list
  written by hand in its way; IP-XACT file sets on a generated component, the standard's way and a larger step.
- **Revisit if:** a build system integration, such as a SoCMake function, needs another form.
