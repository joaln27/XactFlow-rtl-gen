from pathlib import Path

import pytest
from xactflow import Library
from xactflow_archidesc import Design

from builders import design_alone

FIXTURES = Path(__file__).parent / "fixtures"
LIB = FIXTURES / "lib"
RTL = FIXTURES / "rtl"


@pytest.fixture
def library():
    return Library.scan(LIB)


@pytest.fixture
def soc(library):
    """The design every wiring case goes through: one bus connection and one ad hoc connection."""
    design = Design("test.org:design:soc:1.0", library)
    master = design.add_ip("m", "test.org:ip:master:1.0")
    slave = design.add_ip("s", "test.org:ip:slave:1.0", parameters={"GAIN": 5, "OUT_W": 16})
    design.connect_bus(master, slave, base_addr=0x4000_0000, size=0x1000)
    design.connect(slave.irq_o, master.irq_i)
    return design_alone(design)
