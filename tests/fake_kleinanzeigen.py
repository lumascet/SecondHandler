"""Kleinanzeigen with comparables of the test's choosing (the Kleinanzeigen price analysis): the app API answers a search
with the fixture's ad copied once per price given (None: no amount, 0: free), the platform is switched on with app
credentials, and the price backend answers what the test gives it (or fails like Claude does when the CLI is missing)."""
from __future__ import annotations

import copy
import json
from typing import Any

from app import platforms
from app.config import get_settings
from app.db import set_setting
from app.llm.base import LLMError, LLMResult
from app.platforms import kleinanzeigen as ka
from app.platforms.httpclient import FetchResponse
from app.platforms.ratelimit import RateLimiter
from tests.conftest import load

AUTH = "me:secret"
# the comparables of most tests: median 120, middle half 90–180, seven ads with a price
AMOUNTS = [60, 90, 100, 120, 150, 180, 240]


def answer(amounts: list[float | None]) -> dict[str, Any]:
    body = copy.deepcopy(load("kleinanzeigen_ads.json"))
    value = body[ka.ADS_NS]["value"]
    template = copy.deepcopy(value["ad"][1])
    ads = []
    for i, amount in enumerate(amounts):
        ad = copy.deepcopy(template)
        ad["id"] = str(2712350000 + i)
        if amount is None:
            ad["price"] = {"price-type": {"value": "PLEASE_CONTACT"}}
        elif amount == 0:
            ad["price"] = {"price-type": {"value": "FREE"}}
        else:
            ad["price"] = {"price-type": {"value": "SPECIFIED_AMOUNT"}, "amount": {"value": amount}}
        ads.append(ad)
    value["ad"] = ads
    value["paging"]["numFound"] = "40"
    return body


class PricedTransport:
    def __init__(self, amounts: list[float | None]) -> None:
        self.amounts, self.calls = amounts, []

    async def send(self, method, url, *, params=None, headers=None):
        self.calls.append(url)
        body = answer(self.amounts) if "/ads.json" in url else load("kleinanzeigen_locations.json")
        return FetchResponse(200, {"content-type": "application/json"}, json.dumps(body).encode(), url)

    def clear_cookies(self):
        pass

    async def aclose(self):
        pass


def enable(monkeypatch, amounts: list[float | None] = AMOUNTS) -> PricedTransport:
    """Kleinanzeigen switched on with app credentials; its search answers `amounts`. Undo with disable()."""
    monkeypatch.setattr(get_settings(), "kleinanzeigen_api_auth", "")
    set_setting("kleinanzeigen_api_auth", AUTH)
    set_setting("platform_kleinanzeigen_enabled", "1")
    platforms.reset()
    transport = PricedTransport(amounts)
    client = ka.KleinanzeigenClient(transport=transport, auth=AUTH)
    client.limiter = RateLimiter(0, name="Kleinanzeigen")
    ka.set_client(client)
    return transport


def disable() -> None:
    ka.set_client(None)
    platforms.reset()


class FakeClaude:
    """The "ads" backend for the Kleinanzeigen price: answers `data`, or raises LLMError when `error` is given."""

    def __init__(self, data: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.data, self.error, self.prompts = data or {}, error, []

    async def complete(self, *, system, prompt, schema, images=None):
        self.prompts.append(prompt)
        if self.error:
            raise LLMError(self.error)
        return LLMResult(data=self.data, model="fake")
