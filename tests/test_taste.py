"""Swipe hits, keep the verdicts, refine the agent's instruction from them (services/taste.py)."""
from fastapi.testclient import TestClient

from app.db import Hit, HitVote, SearchAgent, session
from app.llm.base import LLMResult
from app.services import taste


def _agent_with_hits(n=6):
    with session() as db:
        a = SearchAgent(name="Mini PC", source_url="https://www.willhaben.at/iad/x", webapi_url="/webapi/x",
                        screen=True, screen_brief="Mini-PC mit mindestens 16 GB",
                        queries=[{"keyword": "mini pc", "platform": "willhaben"}])
        db.add(a)
        db.commit()
        db.refresh(a)
        for i in range(n):
            db.add(Hit(agent_id=a.id, ad_id=str(100 + i), heading=f"Mini PC {i}", price=100 + i * 10, url=f"https://x/{i}",
                       item_condition_label="Gebraucht", location="Wien", body="i5, 16 GB" if i % 2 else "Celeron"))
        db.commit()
        return a.id


def test_a_swipe_saves_or_discards_and_is_remembered():
    aid = _agent_with_hits()
    from app.main import app
    with TestClient(app) as c:
        page = c.get(f"/hits/swipe?agent_id={aid}").text
        assert page.count("data-swipe-card data-id") == 6 and "Mini PC 5" in page
        with session() as db:
            ids = [h.id for h in db.query(Hit).order_by(Hit.id)]
        assert c.post(f"/hits/{ids[0]}/vote", data={"liked": "1"}).json()["state"] == "saved"
        assert c.post(f"/hits/{ids[1]}/vote", data={"liked": "0"}).json()["state"] == "ignored"
        c.post(f"/hits/{ids[2]}/state", data={"state": "saved"})             # the plain Save button counts too
        assert c.get(f"/hits/swipe?agent_id={aid}").text.count("data-swipe-card data-id") == 3
    with session() as db:
        votes = {v.ad_id: v.liked for v in db.query(HitVote)}
        db.delete(db.get(Hit, ids[0]))                                       # a cleared hit: its verdict stays
        db.commit()
    assert votes == {"100": True, "101": False, "102": True}
    assert taste.counts(aid) == {"liked": 2, "disliked": 1}


def test_the_latest_verdicts_balanced():
    aid = _agent_with_hits(0)
    with session() as db:
        for i in range(30):
            db.add(HitVote(agent_id=aid, ad_id=str(i), heading=f"x{i}", liked=i < 25))
        db.commit()
    vs = taste.votes(aid, limit=10)
    assert len(vs) == 10 and sum(not v.liked for v in vs) == 5                 # half each when both have enough


async def test_claude_proposes_a_sharper_instruction(monkeypatch):
    aid = _agent_with_hits()
    with session() as db:
        for h in db.query(Hit):
            taste.record(h, liked="16 GB" in (h.body or ""))
    seen = []

    class Fake:
        async def complete(self, *, system, prompt, schema, images=None):
            seen.append(prompt)
            return LLMResult(data={"brief": "Mini-PC mit i5 und 16 GB, kein Celeron", "changes": ["Celeron raus"]},
                             model="f")
    monkeypatch.setattr(taste, "get_backend", lambda *a: Fake())
    p = await taste.suggest(aid)
    assert p["brief"].startswith("Mini-PC mit i5") and p["liked"] == 3 and p["disliked"] == 3
    assert "Bisherige Anweisung: Mini-PC mit mindestens 16 GB" in seen[0] and "Celeron" in seen[0]
    from app.main import app
    with TestClient(app) as c:
        frag = c.post(f"/agents/{aid}/refine").text
        assert "data-refine-proposal" in frag and "kein Celeron" in frag and "Use this" in frag
        page = c.get("/agents").text
        assert f'hx-post="/agents/{aid}/refine"' in page and "3 saved, 3 discarded" in page


async def test_too_few_verdicts_say_so():
    aid = _agent_with_hits(0)
    try:
        await taste.suggest(aid)
        raise AssertionError("no error")
    except ValueError as e:
        assert "at least 5" in str(e)


async def test_check_availability_of_the_hits_shown(monkeypatch):
    """10-29: a button to read every hit shown again (still online, sold, gone) – not one card at a time."""
    from types import SimpleNamespace

    from app.services import search_agents as sa
    aid = _agent_with_hits(3)
    with session() as db:
        hits = list(db.query(Hit).order_by(Hit.id))
        db.add(Hit(agent_id=aid, platform="facebook", ad_id="fb1", heading="FB", url="https://x/fb"))
        db.commit()
        ids = [h.id for h in db.query(Hit)]
    asked, recorded = [], []

    class Site:
        name = "willhaben"

        def can(self, what):
            return True

        async def detail(self, ad_id, url=None):
            asked.append(ad_id)
            if ad_id == "102":
                raise RuntimeError("slow")
            return SimpleNamespace(status="sold" if ad_id == "101" else "active")
    monkeypatch.setattr(sa, "_site", lambda p: Site())
    monkeypatch.setattr(sa, "site_status", lambda d: d.status)
    monkeypatch.setattr(sa, "record_status", lambda p, ad, st, d: recorded.append((ad, st)))
    r = await sa.check_hits(ids)
    assert sorted(asked) == ["100", "101", "102"]                     # Facebook left out, each ad once
    assert r == {"checked": 2, "online": 1, "gone": 1, "failed": 1} and len(hits) == 3
    from app.main import app
    with TestClient(app) as c:
        page = c.get(f"/hits?agent_id={aid}&state=all").text
        assert 'hx-post="/hits/check-status"' in page and 'data-refresh-on-lane="agents"' in page
