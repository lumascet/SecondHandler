"""The README screenshots (docs/images) from made-up demo data: a fresh database with search agents, hits, chats, own ads
and a product search, simple drawn product pictures, and the app started offline with the sites, Geizhals and the
willhaben login answered from that data. Nothing reaches the network; nothing of the real data directory is read.

Run in the test container (Playwright and Chromium are installed there):
    docker exec -w /src wh-tests sh -c 'PATH=/tmp/venv/bin:$PATH python scripts/demo_screenshots.py'
Options: --out DIR (default docs/images), --only NAME[,NAME] (e.g. hits,ad-viewer), --serve (keep the demo running
on port 8093 to look around).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update({
    "DATA_DIR": tempfile.mkdtemp(prefix="wh-demo-"), "BROWSER_ENABLED": "0", "HA_LISTENER": "0",
    "TELEGRAM_LISTENER": "0", "CHAT_LIVE": "0", "TELEGRAM_BOT_TOKEN": "", "HA_URL": "", "HA_TOKEN": "",
    "HA_NOTIFY_SERVICE": "", "CLAUDE_CODE_OAUTH_TOKEN": "", "DASHBOARD_PASSWORD": "", "JOB_GRACE_S": "0",
})

# the demo never reaches the network: a call the patches below miss fails at once
_LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
_gai, _conn = socket.getaddrinfo, socket.socket.connect


def _local(host) -> bool:
    return not isinstance(host, str) or host in _LOCAL or host.startswith("127.") or "/" in host


socket.getaddrinfo = lambda host, *a, **k: _gai(host, *a, **k) if _local(host) else (_ for _ in ()).throw(
    socket.gaierror(f"demo: no network ({host})"))
socket.socket.connect = lambda self, addr: _conn(self, addr) if _local(addr[0] if isinstance(addr, tuple) else addr) \
    else (_ for _ in ()).throw(ConnectionRefusedError(f"demo: no network ({addr})"))



def _guard_http() -> None:
    """The site clients send through httpx and curl_cffi (its own C sockets, past the socket guard above): both refuse
    every host but this machine."""
    from urllib.parse import urlsplit

    import httpx

    def check(url) -> None:
        host = urlsplit(str(url)).hostname or ""
        if not _local(host):
            raise httpx.ConnectError(f"demo: no network ({host})")

    for cls in (httpx.AsyncClient, httpx.Client):
        orig = cls.send
        if cls is httpx.AsyncClient:
            async def send(self, request, *a, _o=orig, **k):
                check(request.url)
                return await _o(self, request, *a, **k)
        else:
            def send(self, request, *a, _o=orig, **k):
                check(request.url)
                return _o(self, request, *a, **k)
        cls.send = send
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return
    orig_req = AsyncSession.request

    async def request(self, method, url, *a, **k):
        check(url)
        return await orig_req(self, method, url, *a, **k)
    AsyncSession.request = request


_guard_http()

from app.db import (Conversation, Draft, EventLog, Hit, Message, MyAd, ProductSearch, SearchAgent,  # noqa: E402
                    init_db, session, set_setting, utcnow)

NOW = utcnow()


def ago(**kw):
    return NOW - timedelta(**kw)


# ------------------------------------------------------------------ pictures
def _scene(bg1: str, bg2: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="800" height="600">
<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/></linearGradient>
<radialGradient id="sh"><stop offset="0" stop-color="#000" stop-opacity=".22"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient></defs>
<rect width="400" height="300" fill="url(#bg)"/><rect y="225" width="400" height="75" fill="#fff" opacity=".35"/>
<ellipse cx="200" cy="250" rx="150" ry="14" fill="url(#sh)"/>{body}</svg>"""


def bike(c: str) -> str:
    wheel = lambda x: (f'<circle cx="{x}" cy="190" r="52" fill="none" stroke="#2b2f36" stroke-width="7"/>'   # noqa: E731
                       f'<circle cx="{x}" cy="190" r="44" fill="none" stroke="#c9ced6" stroke-width="1.2"/>'
                       + "".join(f'<line x1="{x}" y1="190" x2="{x + 44 * __import__("math").cos(i * 0.5236):.1f}" '
                                 f'y2="{190 + 44 * __import__("math").sin(i * 0.5236):.1f}" stroke="#aab1bb" stroke-width="1"/>'
                                 for i in range(12)) + f'<circle cx="{x}" cy="190" r="5" fill="#555"/>')
    return _scene("#dceaf7", "#eef4fa", wheel(110) + wheel(290) + f"""
<path d="M110 190 L175 120 L270 120 L290 190 M175 120 L205 190 L110 190 M205 190 L270 120 M205 190 L290 190" fill="none" stroke="{c}" stroke-width="9" stroke-linejoin="round" stroke-linecap="round"/>
<path d="M175 120 L168 100 M150 98 h36" stroke="#2b2f36" stroke-width="7" stroke-linecap="round"/>
<path d="M270 120 L276 95 q24 -6 26 14 q0 12 -12 14" fill="none" stroke="#2b2f36" stroke-width="6" stroke-linecap="round"/>
<circle cx="205" cy="190" r="12" fill="none" stroke="#555" stroke-width="5"/>""")


def headphones(c: str, pad: str = "#2b2f36") -> str:
    return _scene("#f6e3e8", "#fbf1f4", f"""
<path d="M120 170 q0 -120 80 -120 q80 0 80 120" fill="none" stroke="{c}" stroke-width="16" stroke-linecap="round"/>
<rect x="96" y="150" width="56" height="88" rx="26" fill="{c}"/><rect x="248" y="150" width="56" height="88" rx="26" fill="{c}"/>
<rect x="108" y="160" width="20" height="68" rx="10" fill="{pad}" opacity=".35"/><rect x="272" y="160" width="20" height="68" rx="10" fill="{pad}" opacity=".35"/>""")


def espresso(c: str) -> str:
    return _scene("#f5e6d6", "#fbf3ea", f"""
<rect x="125" y="60" width="150" height="170" rx="14" fill="{c}"/><rect x="125" y="60" width="150" height="30" rx="12" fill="#000" opacity=".12"/>
<circle cx="242" cy="115" r="17" fill="#fff" stroke="#888" stroke-width="3"/><line x1="242" y1="115" x2="250" y2="105" stroke="#d33" stroke-width="3"/>
<rect x="140" y="104" width="40" height="8" rx="4" fill="#333"/><rect x="140" y="118" width="28" height="8" rx="4" fill="#333"/>
<rect x="150" y="140" width="70" height="16" rx="6" fill="#444"/><rect x="100" y="144" width="60" height="9" rx="4" fill="#333"/>
<rect x="178" y="178" width="28" height="30" rx="4" fill="#fff"/><path d="M206 186 q12 2 0 14" fill="none" stroke="#fff" stroke-width="4"/>
<rect x="135" y="212" width="130" height="10" rx="3" fill="#bbb"/>""")


def camera(c: str) -> str:
    return _scene("#efe8da", "#f8f4ec", f"""
<rect x="105" y="105" width="190" height="115" rx="16" fill="#26292e"/><rect x="105" y="135" width="190" height="55" fill="{c}"/>
<rect x="150" y="85" width="70" height="28" rx="6" fill="#26292e"/><circle cx="125" cy="98" r="9" fill="#444"/><circle cx="268" cy="98" r="8" fill="#444"/>
<circle cx="200" cy="162" r="44" fill="#1b1d21" stroke="#555" stroke-width="5"/><circle cx="200" cy="162" r="28" fill="#2c3e57"/>
<circle cx="190" cy="152" r="8" fill="#fff" opacity=".7"/>""")


def raspberry() -> str:
    return _scene("#e1efe4", "#f1f8f2", """
<rect x="95" y="80" width="210" height="140" rx="10" fill="#1f7a3e"/><rect x="95" y="80" width="210" height="140" rx="10" fill="none" stroke="#155c2d" stroke-width="3"/>
<rect x="175" y="125" width="52" height="52" rx="4" fill="#9aa3ab"/><rect x="183" y="133" width="36" height="36" rx="3" fill="#c9ced6"/>
<g fill="#d4af37">""" + "".join(f'<rect x="{110 + i * 9}" y="88" width="5" height="10"/><rect x="{110 + i * 9}" y="101" width="5" height="10"/>'
                                for i in range(20)) + """</g>
<rect x="250" y="160" width="55" height="40" rx="3" fill="#c0c4c9"/><rect x="250" y="120" width="55" height="34" rx="3" fill="#c0c4c9"/>
<rect x="120" y="200" width="30" height="16" rx="3" fill="#c0c4c9"/><circle cx="110" cy="96" r="5" fill="#155c2d"/>""")


def minipc(c: str) -> str:
    return _scene("#e4e7f0", "#f2f4f9", f"""
<rect x="110" y="120" width="180" height="100" rx="14" fill="{c}"/><rect x="110" y="120" width="180" height="22" rx="12" fill="#fff" opacity=".18"/>
<circle cx="140" cy="190" r="6" fill="#4fc3f7"/><rect x="160" y="186" width="24" height="8" rx="2" fill="#111" opacity=".6"/>
<rect x="192" y="186" width="24" height="8" rx="2" fill="#111" opacity=".6"/><rect x="224" y="184" width="14" height="12" rx="2" fill="#111" opacity=".6"/>""")


def turntable() -> str:
    return _scene("#f4e4d6", "#fbf2ea", """
<rect x="90" y="110" width="220" height="115" rx="10" fill="#2a2d33"/><circle cx="185" cy="167" r="48" fill="#111" stroke="#666" stroke-width="3"/>
<circle cx="185" cy="167" r="34" fill="none" stroke="#333" stroke-width="2"/><circle cx="185" cy="167" r="14" fill="#e3364c"/>
<circle cx="275" cy="128" r="9" fill="#bbb"/><path d="M275 128 L270 200 L252 214" fill="none" stroke="#ccc" stroke-width="4"/>
<circle cx="110" cy="210" r="5" fill="#ddd"/><circle cx="126" cy="210" r="5" fill="#ddd"/>""")


def console() -> str:
    return _scene("#e3e6f3", "#f2f3fa", """
<rect x="120" y="120" width="160" height="92" rx="10" fill="#2a2d33"/><rect x="134" y="132" width="132" height="68" rx="4" fill="#3d6fb6"/>
<rect x="86" y="112" width="36" height="108" rx="16" fill="#00a6d6"/><rect x="278" y="112" width="36" height="108" rx="16" fill="#e8413c"/>
<circle cx="104" cy="140" r="7" fill="#1d1f24"/><circle cx="296" cy="190" r="7" fill="#1d1f24"/>""")


def shelf() -> str:
    return _scene("#ece8e1", "#f7f4ef", """
<rect x="140" y="40" width="120" height="190" rx="3" fill="#f4f4f2" stroke="#cfcac2" stroke-width="3"/>
<g stroke="#cfcac2" stroke-width="3"><line x1="140" y1="85" x2="260" y2="85"/><line x1="140" y1="130" x2="260" y2="130"/>
<line x1="140" y1="175" x2="260" y2="175"/></g>
<g><rect x="152" y="58" width="12" height="27" fill="#c0392b"/><rect x="166" y="62" width="10" height="23" fill="#2c5fa8"/>
<rect x="178" y="55" width="14" height="30" fill="#16857a"/><rect x="200" y="104" width="44" height="26" rx="2" fill="#d4a373"/>
<rect x="156" y="150" width="30" height="25" rx="2" fill="#9aa3ab"/></g>""")


PICTURES = {
    "bike-teal": bike("#16857a"), "bike-red": bike("#c23a2b"), "bike-blue": bike("#2c5fa8"),
    "hp-black": headphones("#2e3238"), "hp-silver": headphones("#d9d6cf", "#777"), "hp-blue": headphones("#3c5a85"),
    "espresso-silver": espresso("#c4c8cd"), "espresso-black": espresso("#3a3d42"), "espresso-red": espresso("#b8332a"),
    "camera-silver": camera("#d8dadd"), "camera-black": camera("#3b3f45"), "pi": raspberry(),
    "minipc-black": minipc("#30343b"), "minipc-silver": minipc("#b9bec6"), "turntable": turntable(),
    "console": console(), "shelf": shelf(),
}


def img(name: str) -> str:
    return f"/demo-img/{name}.svg"


OWN_PRICE = {"8a000001": 180, "8a000002": 60, "8a000003": 260, "8a000004": 95, "8a000005": 230}
OWN_TEXT = {"8a000001": "Woom 4 in Blau, 20 Zoll, für Kinder von 1,15 bis 1,35 m. Gut gepflegt, Bremsen und Schaltung frisch "
                        "eingestellt, neue Klingel. Abholung in 1070 Wien oder Versand.",
            "8a000002": "Drei Billy Regale, weiß, 80 x 202 cm, je 60 €. Zerlegt, Abholung in 1070 Wien."}

# ------------------------------------------------------------------ data
AGENTS = [
    ("Road bike, frame 56", "Rennrad Rahmen 56", 600, "Rennrad mit Rahmenhöhe 56, Shimano 105 oder besser, bis 700 €"),
    ("Sony WH-1000XM5", "Sony WH-1000XM5", 260, "Sony WH-1000XM5, funktionstüchtig, mit Case"),
    ("Espresso machine up to € 300", "Siebträger", 1500, "Siebträgermaschine mit Dampflanze, bis 300 €"),
    ("Fujifilm X-T30", "Fujifilm X-T30", 900, "Fujifilm X-T30 Body, geringe Auslösungen"),
]

# (agent index, title, price, old price, place, postcode, picture, state, deal, reason, minutes ago, text, condition)
HITS = [
    (0, "Cube Attain SL – road bike, frame 56, Shimano 105", 590, None, "Wien, 07. Bezirk", "1070", "bike-teal", "new", "good",
     "Good price: frame 56 and Shimano 105, about 11 % below comparable bikes.", 3,
     "Cube Attain SL road bike, frame size 56, aluminium frame with carbon fork. Shimano 105 groupset (2 x 11), hydraulic "
     "disc brakes, new Continental GP5000 tyres this spring. Ridden about 3,500 km, always serviced, no crashes.\n\n"
     "Test ride welcome, pickup in 1070 Wien.", "used"),
    (1, "Sony WH-1000XM5 black, with case, 1 year old", 189, 220, "Graz", "8020", "hp-black", "new", "good",
     "Good price: about 45 % below new, case and cable included, battery as new.", 12, "Kaum benutzt, mit Case und Kabel.", "used"),
    (1, "Sony WH-1000XM5 silver – BRAND NEW, sealed", 89, None, "Wien, 10. Bezirk", "1100", "hp-silver", "new", "great",
     "Far too cheap for a sealed XM5 and prepayment only – very likely a scam.", 18, "Neu und versiegelt. Nur Vorauskasse.", "new"),
    (2, "Gaggia Classic Pro, recently descaled", 280, None, "Linz", "4020", "espresso-silver", "new", "fair",
     "Fair price for a Classic Pro with fresh descaling; usual € 250–320.", 25, "Funktioniert einwandfrei, frisch entkalkt.", "used"),
    (0, "Specialized Allez, 56 cm, Tiagra", 430, 480, "Mödling", "2340", "bike-red", "seen", "fair",
     "Right size, but Tiagra instead of 105; the price is in line with similar bikes.", 34, "Gepflegt, neue Kette.", "used"),
    (3, "Fujifilm X-T30 silver body, 4,800 shutter count", 520, None, "Wien, 03. Bezirk", "1030", "camera-silver", "new", "good",
     "Good price for a low shutter count; used bodies usually sell for € 580–650.", 40, "Wenig benutzt, mit 2 Akkus.", "used"),
    (2, "Rancilio Silvia V6 with portafilter set", 299, None, "Bregenz", "6900", "espresso-black", "new", "great",
     "Great price for a Silvia V6 with a second portafilter.", 50, "Mit zweitem Siebträger.", "used"),
    (0, "Canyon Endurace AL 7, size M (56)", 780, None, "Salzburg", "5020", "bike-blue", "filtered", "overpriced",
     "Over the 700 € budget of the search.", 70, "Top Zustand.", "used"),
    (3, "Fujifilm X-T30 black + XC 15-45", 610, 650, "St. Pölten", "3100", "camera-black", "seen", "fair",
     "Fair for body and kit lens together.", 95, "Mit Kitobjektiv.", "used"),
    (1, "Sony WH-1000XM5 midnight blue, like new", 239, None, "Innsbruck", "6020", "hp-blue", "saved", "good",
     "Good price for the blue edition, receipt included.", 130, "Wie neu, Rechnung vorhanden.", "like_new"),
    (2, "La Pavoni style lever machine, red", 260, None, "Klagenfurt", "9020", "espresso-red", "seen", "fair",
     "Fair price; lever machines need practice.", 180, "Hebelmaschine.", "used"),
    (0, "Cube Attain Race 56 – 2023", 650, None, "Wien, 21. Bezirk", "1210", "bike-teal", "ignored", "fair",
     "Fair, but at the top of the budget.", 240, "Wie neu.", "used"),
]

SITE_OF = {2: "vinted", 4: "kleinanzeigen", 9: "vinted", 11: "shpock"}   # the rest: willhaben

GEO = {"1070": (48.202, 16.349), "8020": (47.071, 15.42), "1100": (48.165, 16.38), "4020": (48.306, 14.286),
       "2340": (48.086, 16.283), "1030": (48.198, 16.394), "6900": (47.503, 9.747), "5020": (47.809, 13.055),
       "3100": (48.204, 15.626), "6020": (47.269, 11.404), "9020": (46.624, 14.307), "1210": (48.27, 16.4)}

COMPARABLE = [("Cube Attain SL 56 – 2022", 665, "Wien"), ("Cube Attain SL Rennrad RH 56", 585, "Linz"),
              ("Cube Attain SL Disc 105, 56 cm", 640, "Graz"), ("Cube Attain SL 2021, Größe 56", 610, "Wels"),
              ("Cube Attain SL – Shimano 105, 58", 720, "Salzburg"), ("Cube Attain SL schwarz 56", 560, "Baden"),
              ("Cube Attain SL Rennrad, kaum gefahren", 699, "Innsbruck"), ("Cube Attain SL 105 Disc", 625, "Krems"),
              ("Cube Attain SL 56cm Carbon-Gabel", 650, "Wien"), ("Cube Attain SL teal 2023", 740, "St. Pölten"),
              ("Scott Speedster 30, M", 530, "Wiener Neustadt"), ("Bianchi Via Nirone 7, 55", 580, "Graz")]


def seed() -> None:
    from app import platforms
    from app.services import deals

    wh = platforms.get("willhaben")
    init_db()
    set_setting("ship_sender", '{"name": "Max Muster", "street": "Musterstraße", "house_no": "1", "zip": "1070", "city": "Wien", "country": "AT"}')
    set_setting("purchase_enabled", "1")
    set_setting("platform_kleinanzeigen_enabled", "1")              # its chats in the list, with their online mark
    set_setting("account_kleinanzeigen", '{"logged_in": true, "checked_at": null, "error": null}')
    set_setting("ship_sender_geo", '{"addr": "Musterstraße 1, 1070 Wien, AT", "lat": 48.2017, "lon": 16.3497}')
    with session() as db:
        agents = []
        for i, (name, kw, interval, brief) in enumerate(AGENTS):
            spec = wh.keyword_search(kw, sort="newest")
            a = SearchAgent(name=name, source_url=spec.url, webapi_url=spec.api or spec.url, spec=spec.to_json(),
                            interval_s=interval, baseline_done=True,
                            last_run_at=ago(minutes=2 + i), last_ok_at=ago(minutes=2 + i), last_rows_found=40 + 13 * i,
                            screen=True, screen_brief=brief, screen_min="match", created_at=ago(days=20 - i))
            if i == 0:
                a.geizhals_level = "cheap"
            if i == 1:
                a.auto_buy, a.auto_buy_max = True, 240.0
            db.add(a)
            agents.append(a)
        db.commit()
        for a in agents:
            db.refresh(a)
        for n, (ai, title, price, old, place, pc, pic, state, deal, reason, mins, text, cond) in enumerate(HITS, 1):
            lat, lon = GEO[pc]
            gh = {"name": "Sony WH-1000XM5 Kopfhörer", "url": "https://geizhals.at/", "price_from": 349.0} if ai == 1 else \
                {"name": "Cube Attain SL", "url": "https://geizhals.at/", "price_from": 1199.0} if "Attain SL" in title else None
            h = Hit(agent_id=agents[ai].id, ad_id=str(1_900_000_000 + n), heading=title, body=text, price=price,
                    price_display=f"€ {price}", old_price=old, location=f"{pc} {place}", postcode=pc,
                    published_at=ago(minutes=mins), url=f"https://www.willhaben.at/iad/object?adId={1_900_000_000 + n}",
                    image_url=img(pic), is_private=True, first_seen_at=ago(minutes=mins), last_seen_at=ago(minutes=1),
                    state=state, price_dropped_at=ago(minutes=mins - 1) if old else None, screen_relevant=deal != "overpriced",
                    screen_deal=deal, screen_reason=reason, screen_at=ago(minutes=mins), lat=lat, lon=lon,
                    screen_issues=["scam_suspect"] if "sealed" in title else (["too_expensive"] if deal == "overpriced" else []),
                    scam_flags=[{"code": "prepayment", "label": "Prepayment only", "severity": "high"}] if "sealed" in title else None,
                    geizhals=gh, item_condition=cond, shipping="ship" if ai in (1, 3) else "pickup",
                    shipping_price=4.49 if ai in (1, 3) else None, site_status="active", status_checked_at=ago(minutes=5),
                    bumped_count=2 if "Rancilio" in title else 0,
                    platform=SITE_OF.get(n, "willhaben"))
            db.add(h)
        db.commit()

        # chats
        chats = [("c1", "Hanna K.", "Kinderfahrrad Woom 4, blau", "Wäre am Samstag um 10 Uhr eine Abholung möglich?", 6, 1),
                 ("c2", "Lukas P.", "IKEA Billy Regal, weiß", "Ist das Regal noch zu haben?", 45, 0),
                 ("c3", "Sabine M.", "Nintendo Switch OLED + 2 Spiele", "Würdest du 220 € nehmen?", 120, 0),
                 ("c4", "Thomas W.", "Bosch Akkuschrauber GSR 18V-55", "Danke, ist gut angekommen!", 1500, 0)]
        for cid, who, ad, last, mins, unread in chats:
            db.add(Conversation(id=cid, ad_id=f"8{cid[-1]}000001", ad_title=ad, partner_name=who, unread_count=unread,
                                last_message_text=last, last_message_at=ago(minutes=mins), role="seller",
                                ad_context={"ad_id": f"8{cid[-1]}000001", "title": ad, "price": "€ 180" if cid == "c1" else "€ 60",
                                            "status": "aktiv", "source": "own_ad"},
                                ad_context_at=ago(minutes=1), synced_at=ago(minutes=1), messages_synced_at=ago(minutes=1)))
        db.commit()
        msgs = [("c1", "in", "Hallo! Ist das Woom 4 noch verfügbar? Mein Sohn ist 1,20 m groß.", 40),
                ("c1", "out", "Hallo Hanna, ja, das Rad ist noch da. Bei 1,20 m passt das Woom 4 gut.", 30),
                ("c1", "in", "Super! Wäre am Samstag um 10 Uhr eine Abholung möglich?", 6),
                ("c2", "in", "Ist das Regal noch zu haben?", 45),
                ("c3", "in", "Hallo, würdest du 220 € nehmen?", 120),
                ("c4", "out", "Das Paket ist unterwegs, die Sendungsnummer schicke ich gleich.", 2900),
                ("c4", "in", "Danke, ist gut angekommen!", 1500)]
        for k, (cid, d, text, mins) in enumerate(msgs):
            db.add(Message(conversation_id=cid, ext_id=f"m{k}", direction=d, text=text, created_at=ago(minutes=mins),
                           read=True if d == "out" else None))
        db.add(Draft(conversation_id="c1", text="Hallo Hanna, Samstag um 10 Uhr passt gut. Die Adresse ist Musterstraße 1, "
                     "1070 Wien – läute bitte bei Muster. Das Rad ist frisch geputzt und die Bremsen sind eingestellt.",
                     intent="logistics", model="claude-sonnet-5-5", created_at=ago(minutes=5),
                     alternatives=[{"label": "Sonntag vorschlagen", "instruction": "Schlag Sonntag 11 Uhr vor"},
                                   {"label": "Nur Versand", "instruction": "Biete Versand an"}]))
        # a Kleinanzeigen chat (0.5.5): I am the buyer there
        db.add(Conversation(id="ka:demo:c5", platform="kleinanzeigen", ad_id="3100000005", role="buyer",
                            ad_title="Rennrad Cube Attain, 56 cm", partner_name="Jonas B.", unread_count=1,
                            last_message_text="Ja, gerne – Sonntag passt mir auch.", last_message_at=ago(minutes=12),
                            ad_context={"title": "Rennrad Cube Attain, 56 cm", "price": 420.0, "platform": "kleinanzeigen",
                                        "url": "https://www.kleinanzeigen.de/s-anzeige/3100000005"},
                            ad_context_at=ago(minutes=1), synced_at=ago(minutes=1), messages_synced_at=ago(minutes=1)))
        for k, (d, text, mins) in enumerate([("out", "Hallo, ist das Rennrad noch zu haben? Ich könnte Sonntag vorbeikommen.", 20),
                                             ("in", "Ja, gerne – Sonntag passt mir auch.", 12)]):
            db.add(Message(conversation_id="ka:demo:c5", ext_id=f"ka-m{k}", direction=d, text=text,
                           created_at=ago(minutes=mins)))
        db.add(Draft(conversation_id="c3", text="Hallo Sabine, 220 € ist mir zu wenig – 245 € inklusive der beiden Spiele "
                     "wäre für mich in Ordnung.", intent="price_negotiation", model="claude-sonnet-5-5", created_at=ago(minutes=119),
                     suggested_price=245))

        # own ads
        ads = [("8a000001", "Kinderfahrrad Woom 4, blau", "active", 180, True, True, 160, None, "bike-blue", 1),
               ("8a000002", "IKEA Billy Regal, weiß, 80 cm", "active", 60, False, True, 50, 3, "shelf", 12),
               ("8a000003", "Nintendo Switch OLED + 2 Spiele", "active", 260, True, True, 240, None, "console", 8),
               ("8a000004", "Bosch Akkuschrauber GSR 18V-55", "sold", 95, False, False, None, None, None, 40),
               ("8a000005", "Fujifilm XF 35mm f/2", "active", 230, True, False, None, None, "camera-black", 3)]
        for ad_id, title, status, _price, renew, auto, minp, qty, pic, days in ads:
            db.add(MyAd(ad_id=ad_id, heading=title, url=f"https://www.willhaben.at/iad/object?adId={ad_id}",
                        image_url=img(pic) if pic else None, status=status, published_at=ago(days=days),
                        end_date=NOW + timedelta(days=60 - days), auto_renew=renew, auto_reply=auto,
                        auto_reply_instructions="Abholung in 1070 Wien oder Versand mit Post." if auto else None,
                        min_price=minp, quantity=qty, last_checked_at=ago(minutes=10),
                        sale_status="sold" if status == "sold" else None))
        for kind, title, msg, mins in ([
                ("hit", "New hit: Cube Attain SL – road bike", "€ 590 · good price", 3),
                ("chat", "Hanna K. wrote", "Wäre am Samstag um 10 Uhr eine Abholung möglich?", 6),
                ("price_drop", "Price drop: Sony WH-1000XM5 black", "€ 220 → € 189", 11),
                ("hit", "New hit: Fujifilm X-T30 silver body", "€ 520 · good price", 40),
                ("myad", "Renewed: Nintendo Switch OLED", "auto-renew", 300)]):
            db.add(EventLog(kind=kind, message=f"{title} – {msg}", ts=ago(minutes=mins)))
        db.commit()

    # a finished product search: the stats as the cards keep them
    prices = [p for _, p, _ in COMPARABLE]
    st = deals.stats(prices)

    def card(n, title, price, place, pc, pic, fit, reason, product, gh, flags=()):
        lat, lon = GEO.get(pc, (48.2, 16.37))
        return {"ad_id": str(1_950_000_000 + n), "platform": "willhaben", "ref": str(1_950_000_000 + n), "title": title,
                "price": price, "price_display": f"€ {price}", "location": f"{pc} {place}", "postcode": pc, "lat": lat, "lon": lon,
                "url": f"https://www.willhaben.at/iad/object?adId={1_950_000_000 + n}", "image_url": img(pic), "is_private": True,
                "published_at": ago(hours=n).isoformat(), "text": "", "query": "Rennrad 56", "position": None,
                "median": st["median"], "min": st["min"], "p25": st["p25"], "comparables": st["count"], "stats": st,
                "item_condition": "used", "item_condition_label": "Gebraucht", "shipping_price": None, "shipping": "pickup",
                "scam_flags": [], "fit": fit, "reason": reason, "red_flags": list(flags), "product": product, "geizhals": gh}
    gh_cube = {"name": "Cube Attain SL", "url": "https://geizhals.at/", "price_from": 1199.0}
    results = [card(1, "Cube Attain SL – road bike, frame 56, Shimano 105", 590, "Wien", "1070", "bike-teal", 94,
                    "Exactly the size and groupset, serviced, 11 % below comparable bikes.", "Cube Attain SL", gh_cube),
               card(2, "Rose Pro SL Disc 105, RH 57", 640, "Wien", "1210", "bike-blue", 86,
                    "57 instead of 56 fits too; disc brakes, fair price.", "Rose Pro SL Disc 105",
                    {"name": "Rose Pro SL Disc 105", "url": "https://geizhals.at/", "price_from": 1399.0}),
               card(3, "Specialized Allez, 56 cm, Tiagra", 430, "Mödling", "2340", "bike-red", 71,
                    "Right size and cheap, but Tiagra instead of 105.", "Specialized Allez Tiagra",
                    {"name": "Specialized Allez", "url": "https://geizhals.at/", "price_from": 999.0}, ["Tiagra statt 105"]),
               card(4, "Canyon Endurace AL 7, size M (56)", 780, "Salzburg", "5020", "bike-blue", 64,
                    "Very good bike, but 80 € over the budget.", "Canyon Endurace AL 7",
                    {"name": "Canyon Endurace AL 7", "url": "https://geizhals.at/", "price_from": 1299.0}, ["über Budget"])]
    with session() as db:
        db.add(ProductSearch(description="Rennrad mit Rahmenhöhe 56, Shimano 105 oder besser, bis 700 €, Abholung rund um Wien",
                             status="done", started_at=ago(minutes=9), finished_at=ago(minutes=7),
                             settings={"depth": "normal", "unusual": False, "categories": False, "sort": "relevance",
                                       "photos": True, "ask": True, "platforms": ["willhaben"], "condition": "used",
                                       "per_site": 100, "over_budget": 1.5},
                             plan={"summary": "Ein gebrauchtes Rennrad in Rahmenhöhe 56 mit Shimano 105 oder besser, bis 700 €.",
                                   "max_price": 700, "must_have": ["Rahmenhöhe 55–57", "Shimano 105 oder besser"],
                                   "nice_to_have": ["Scheibenbremsen", "Carbon-Gabel"], "exclude": ["Damenrad", "defekt"],
                                   "queries": [{"keyword": "Rennrad 56", "why": "direkt"},
                                               {"keyword": "Rennrad Shimano 105", "why": "Schaltgruppe"},
                                               {"keyword": "Roadbike RH 56", "why": "andere Schreibweise"}]},
                             queries=[{"keyword": k, "platform": "willhaben", "found": f, "kept": kp, "why": w}
                                      for k, f, kp, w in (("Rennrad 56", 214, 61, "direkt"), ("Rennrad Shimano 105", 188, 40, "Schaltgruppe"),
                                                          ("Roadbike RH 56", 37, 12, "andere Schreibweise"))],
                             requests=3, candidates=113, results=results, note="Unter 700 € gibt es mehrere gute 105er-Räder in Wien.",
                             progress={"done": 3, "total": 3, "rank": {"done": 3, "total": 3}, "calls": 3}))
        db.commit()


# ------------------------------------------------------------------ offline answers
def patch() -> None:
    from app import platforms, scheduler
    from app.platforms.types import AdDetail, Listing
    from app.services import deals
    from app.willhaben import session as wsess

    scheduler.start = lambda: None                      # no agent runs, no chat sync

    # the home page's connection tiles and the setup hint: as on a set-up installation
    from app import main as app_main
    from app.services import health
    app_main.claude_token = lambda: "demo"
    from app.services import quiet_hours
    quiet_hours.state = lambda *a, **k: None            # the demo may run at night: no quiet-hours notice

    class Public:                                       # My ads → "As buyers see it"
        async def get_detail(self, ad_id, refresh=False):
            with session() as db:
                row = db.get(MyAd, str(ad_id))
            if row is None:
                return None
            return AdDetail(ad_id=row.ad_id, heading=row.heading, description=OWN_TEXT.get(row.ad_id, ""), price=OWN_PRICE[row.ad_id],
                            price_display=f"€ {OWN_PRICE[row.ad_id]}", status="active", url=row.url, published_at=row.published_at,
                            first_published_at=row.published_at, end_date=row.end_date, images=[row.image_url] if row.image_url else [],
                            location="1070 Wien", is_private=True, shipping="ship", shipping_price=5.9, paylivery_enabled=True)
    app_main.get_public = lambda: Public()
    ok = {"willhaben": ("willhaben", "logged in as Max Muster"), "claude": ("Claude", "subscription, signed in"),
          "ha": ("Home Assistant", "connected"), "telegram": ("Telegram", "bot connected"),
          "live_chat": ("Live chat", "connected")}
    health.CHECKS[:] = [(lambda k=f.__name__.strip("_"): health.Status(k, ok[k][0], "ok", ok[k][1]))
                        if f.__name__.strip("_") in ok else f for f in health.CHECKS]

    s = wsess.get_session()
    s.context = object()                                # "started": the sidebar shows the willhaben state
    s.logged_in, s.user = True, {"nickname": "Max Muster", "email": "max@example.invalid"}
    s.chat_live.update(state="connected", since=ago(hours=3))
    from app.platforms import kleinanzeigen_inbox
    kleinanzeigen_inbox.live = lambda: {"state": "connected", "since": ago(hours=3), "last_ok": utcnow()}   # checked just now
    s.check_login = lambda *a, **k: asyncio.sleep(0, True)

    async def detail(ad_id, url=None):
        with session() as db:
            from sqlmodel import select
            h = db.exec(select(Hit).where(Hit.ad_id == str(ad_id))).first()
        if h is None:
            return None
        d = AdDetail.from_hit(h)
        d.description = h.body
        pic = h.image_url.rsplit("/", 1)[1].split(".")[0]
        family = pic.split("-")[0]
        d.images = [h.image_url] + [img(k) for k in PICTURES if k.startswith(family) and k != pic][:3]
        d.category_path = ["Sport / Sportgeräte", "Fahrräder", "Rennräder"] if family == "bike" else ["Elektronik"]
        d.seller_name, d.first_published_at = "Paul S.", ago(days=20)
        return d
    platforms.get("willhaben").detail = detail

    async def search(site, query):
        ads = [Listing(ad_id=str(1_800_000_000 + i), heading=t, body=None, price=p, price_display=f"€ {p}", old_price=None,
                       location=loc, postcode=None, state=None, published_at=ago(days=i), changed_at=None, end_date=None,
                       url="https://www.willhaben.at/", image_url=None, is_private=True, item_condition="used")
               for i, (t, p, loc) in enumerate(COMPARABLE)]
        return {"rows_found": len(ads), "ads": [a.as_dict() for a in ads]}
    deals._search = search

    async def geizhals_match(query, raise_errors=False):
        q = (query or "").lower()
        if "attain" in q or "cube" in q:
            return {"name": "Cube Attain SL", "url": "https://geizhals.at/", "price_from": 1199.0}
        if "sony" in q:
            return {"name": "Sony WH-1000XM5 Kopfhörer", "url": "https://geizhals.at/", "price_from": 349.0}
        return None
    deals.geizhals_match = geizhals_match


def build_app():
    from fastapi.responses import Response

    from app.main import app

    async def picture(name: str):
        return Response(PICTURES.get(name, PICTURES["pi"]), media_type="image/svg+xml",
                        headers={"cache-control": "max-age=3600"})
    app.add_api_route("/demo-img/{name}.svg", picture)
    app.router.routes.insert(0, app.router.routes.pop())       # before any catch-all
    return app


def serve(port: int) -> None:
    import uvicorn
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.2)
    raise SystemExit("demo server did not start")


# ------------------------------------------------------------------ screenshots
EXTRA = {"hits-phone"}     # only when asked for (--only): pictures for release notes, not the README
SHOTS = {
    # name: (path, viewport, action)
    "dashboard": ("/", (1440, 900), None),
    "search-agents": ("/agents", (1440, 900), None),
    "hits": ("/hits", (1440, 900), None),
    "ad-viewer": ("/hits", (1440, 900), "viewer"),
    "chat": ("/chats/c1", (1440, 900), None),
    "product-search": ("/deals?search=1#ps-result", (1440, 900), "scroll-result"),
    "my-ads": ("/myads", (1440, 900), None),
    "telegram": ("/tg/hits", (390, 844), "tg"),
    "hits-phone": ("/hits", (390, 844), "cards"),          # release notes; not in the README
}


async def shoot(base: str, out: Path, only: set[str] | None) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        for name, (path, (w, h), action) in SHOTS.items():
            if only and name not in only or not only and name in EXTRA:
                continue
            ctx = await b.new_context(viewport={"width": w, "height": h}, device_scale_factor=1.5 if w > 500 else 2,
                                      color_scheme="light", locale="en-US", timezone_id="Europe/Vienna")
            p = await ctx.new_page()
            errors = []
            p.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            if action == "tg":
                await p.goto(base + "/tg")
                await p.evaluate("sessionStorage.setItem('tg', '1')")
            await p.goto(base + path)
            await p.wait_for_timeout(900)
            if action == "viewer":
                await p.locator("a[data-ad]").first.click()
                await p.wait_for_timeout(1800)
            elif action == "cards":
                await p.evaluate("document.querySelector('article[id^=hit-]').scrollIntoView(); window.scrollBy(0, -12)")
                await p.wait_for_timeout(300)
            elif action == "scroll-result":
                await p.locator("#ps-box").scroll_into_view_if_needed()
                await p.evaluate("window.scrollBy(0, -16)")
                await p.wait_for_timeout(400)
            await p.screenshot(path=str(out / f"{name}.png"))
            print(f"{name}: {'errors ' + '; '.join(errors) if errors else 'ok'}")
            await ctx.close()
        await b.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--only", default="")
    ap.add_argument("--serve", action="store_true")
    args = ap.parse_args()
    seed()
    patch()
    port = 8093
    serve(port)
    if args.serve:
        print(f"demo on http://127.0.0.1:{port} – Ctrl-C ends it")
        while True:
            time.sleep(3600)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    asyncio.run(shoot(f"http://127.0.0.1:{port}", out, set(filter(None, args.only.split(","))) or None))


if __name__ == "__main__":
    main()
