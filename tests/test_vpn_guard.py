"""The handshake guard: the provider counts new tunnels. A burst of them was followed by no handshake anywhere for an
hour, and failovers kept asking through it; the guard caps new tunnels and pauses them during an outage."""
import pytest

from app.platforms import vpn


@pytest.fixture
def g(monkeypatch):
    monkeypatch.setattr(vpn, "HANDSHAKES_PER_WINDOW", 3)
    guard = vpn.HandshakeGuard()
    return guard


async def test_a_budget_of_new_tunnels(g):
    for _ in range(3):
        await g.admit(0)
    with pytest.raises(vpn.HandshakePause):           # the fourth within the window: not now (a request waits 0 s)
        await g.admit(0)


async def test_an_outage_pauses_new_tunnels_longer_each_time_until_one_works(g):
    assert g.outage() == vpn.OUTAGE_PAUSES_S[0]
    with pytest.raises(vpn.HandshakePause, match="no handshake"):
        await g.admit(60)
    await g.admit(0, in_pause=True)                     # the live server may start again (within the budget)
    g.pause_until = 0                                   # the pause ran out, the next try fails as well
    assert g.outage() == vpn.OUTAGE_PAUSES_S[1]
    g.pause_until = 0
    g.result(True)                                      # a handshake works: the series ends
    assert g.outage() == vpn.OUTAGE_PAUSES_S[0]


async def test_no_failover_during_a_pause(monkeypatch):
    """During a pause the standby failover tries nothing and marks nothing (no server is at fault)."""
    tried = []

    async def switch(*a, **k):
        tried.append(a)
    monkeypatch.setattr(vpn, "switch", switch)
    vpn.guard.outage()
    assert await vpn.failover("fake", "fake.example") is None and tried == []
