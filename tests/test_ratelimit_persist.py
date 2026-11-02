"""A site's limiter keeps its cool-down after a block across a restart: an update right after a 403 must not hit the
site again at once."""
import pytest

from app.platforms.base import Blocked
from app.platforms.ratelimit import RateLimiter


async def test_a_block_pause_survives_a_restart():
    before = RateLimiter(0, name="Vinted", persist="vinted")
    before.penalize()                                       # 60 s pause, longer than the 45 s a caller waits
    after = RateLimiter(0, name="Vinted", persist="vinted")  # the app started again
    with pytest.raises(Blocked):
        async with after:
            pass
    assert after.cooling_down_s > 30


async def test_limiters_without_persist_start_fresh():
    RateLimiter(0, name="Vinted", persist="vinted").penalize()
    async with RateLimiter(0, name="Vinted"):               # a test's own or a lane's limiter: nothing restored
        pass


async def test_a_success_clears_the_kept_pause():
    lim = RateLimiter(0, name="Vinted", persist="vinted")
    lim.penalize()
    lim._cooldown_until = 0                                  # the pause ran out
    lim.reward()                                             # a request got through again
    async with RateLimiter(0, name="Vinted", persist="vinted"):
        pass
