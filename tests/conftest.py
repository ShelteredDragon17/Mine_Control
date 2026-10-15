import pytest_asyncio

from curtail.sim.antminer import SimAntminer, SimServer


@pytest_asyncio.fixture
async def sim():
    """A fast simulated S21 XP. Yields (SimAntminer, SimServer)."""
    m = SimAntminer(ramp_s=0.3, sleep_delay_s=0.1)
    async with SimServer(m) as srv:
        yield m, srv
