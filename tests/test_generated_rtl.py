"""Check generated RTL with real tools: the text proves nothing until something compiles and runs it.

pyslang always runs. Verilator and Icarus Verilog are skipped when not installed.
"""

import shutil
import subprocess

import pytest
from pyslang import ast, syntax
from xactflow import link

from conftest import RTL
from xactflow_rtl_gen import generate_top
from xactflow_rtl_gen.keywords import SYSTEMVERILOG_KEYWORDS

SOURCES = [RTL / "master_core.sv", RTL / "slave.sv"]


def slang_errors(*texts):
    compilation = ast.Compilation()
    for text in texts:
        compilation.addSyntaxTree(syntax.SyntaxTree.fromText(text))
    return [str(d.code) for d in compilation.getAllDiagnostics() if d.isError()]


def generated(soc, library):
    return generate_top(link(soc, library), common=["clk_i", "rst_ni"])


def simulate(top_text, tmp_path):
    top = tmp_path / "soc.sv"
    top.write_text(top_text)
    binary = tmp_path / "sim.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-o", str(binary), "-s", "tb_soc", str(top), *map(str, SOURCES), str(RTL / "tb_soc.sv")],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(["vvp", str(binary)], check=True, capture_output=True, text=True).stdout


needs_icarus = pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")
needs_verilator = pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")


def test_compiles_with_slang(soc, library):
    sources = [path.read_text() for path in SOURCES]
    assert slang_errors(generated(soc, library), *sources) == []


@needs_verilator
def test_verilator_finds_no_errors(soc, library, tmp_path):
    top = tmp_path / "soc.sv"
    top.write_text(generated(soc, library))
    result = subprocess.run(
        ["verilator", "--lint-only", "--top-module", "soc", str(top), *map(str, SOURCES)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@needs_icarus
def test_transaction_crosses_the_top_in_simulation(soc, library, tmp_path):
    assert "PASS" in simulate(generated(soc, library), tmp_path)


@needs_icarus
@pytest.mark.parametrize(
    "good, broken",
    [
        ("= ~m_valid_o;", "= m_valid_o;"),  # the inverted port map
        ("= m_wdata_o;", "= 8'h00;"),  # a plain bus signal, initiator to target
        ("= s_rdata_o[7:0];", "= s_rdata_o[15:8];"),  # the part select, target to initiator
        ("m_ready_i  = 1'h1;", "m_ready_i  = 1'h0;"),  # the abstraction default
        ("m_err_i    = 1'h0;", "m_err_i    = 1'h1;"),  # the tie-off
        ("m_irq_i = s_irq_o;", "m_irq_i = 1'b0;"),  # the ad hoc connection
        (".GAIN(5)", ".GAIN(6)"),  # a parameter override
    ],
)
def test_the_testbench_notices_broken_wiring(soc, library, tmp_path, good, broken):
    """Without this, the passing simulation above could just mean the testbench checks nothing."""
    text = generated(soc, library)
    assert good in text
    assert "PASS" not in simulate(text.replace(good, broken), tmp_path)


def test_keyword_list_matches_what_a_compiler_rejects():
    assert slang_errors("module not_a_keyword; endmodule") == []
    accepted = [word for word in SYSTEMVERILOG_KEYWORDS if not slang_errors(f"module {word}; endmodule")]
    assert accepted == []
