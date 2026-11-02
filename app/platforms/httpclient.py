"""The HTTP client every platform adapter builds on: one anonymous visitor per site, paced, cached, recorded.

`HttpPlatformClient._get()` does what every site needs the same way: the platform's rate limiter, a short response
cache (404s only briefly), the offline replay (WH_REPLAY=1), the access log and the request recorder (with the
platform), a fresh cookie jar every few hours and after a block, and the 403/429 handling (new visitor, back-off,
`Blocked`). A site adds its headers, its request paths and, if its answers need it, its own `_check_status`.

The wire is behind `Transport`: `HttpxTransport` (willhaben) or `CurlTransport` (sites whose bot protection checks
the TLS fingerprint; curl_cffi impersonates a browser). Tests hand in a fake transport or an httpx MockTransport.
Adapters never import the logged-in willhaben session (tests/test_anonymous_public.py).

With the site's VPN switch on (app/platforms/vpn.py) every request goes through the local proxy of the site's
server; a refused or unreachable server is reported (the site pauses, a search starts in the background) and the
request asks again once there is a route for it (up to VPN_ATTEMPTS); with none it fails instead of going out directly.
An injected transport means no VPN (tests, unless they pass `tunnel`). The VPN search (app/services/vpn_check.py) sends
its test searches under a `Trial`: through the server under test only, never another, never directly, and as a
visitor of its own (its own transport and cookies), so the live requests of the same client keep theirs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, Protocol

import httpx

from app.i18n import t
from app.platforms.base import Blocked, PlatformError
from app.platforms.ratelimit import RateLimiter
from app.willhaben import access_log, recorder, replay

if TYPE_CHECKING:
    from app.platforms.vpn import Route, Tunnel

log = logging.getLogger(__name__)

NOT_FOUND_CACHE_S = 30
ANON_SESSION_S = 6 * 3600   # fresh anonymous visitor every 6 h
TIMEOUT_S = 30
VPN_ATTEMPTS = 3            # exits one request may use before it gives up (each one through the limiter)
VPN_TRIES = 8               # ... and sends at most (a refusal is asked again until it counts: vpn.CONFIRM)
TRIAL_TIMEOUT_S = 10        # a test request of the VPN search: a server that does not answer by then is out


@dataclass
class FetchResponse:
    """What a transport returns; the same attributes as an httpx.Response the adapters read."""
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str
    extensions: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def header(self, name: str) -> str:
        """A response header, whatever case the transport kept (curl_cffi keeps the server's)."""
        name = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == name), "")


class TransportError(Exception):
    """The request could not be sent or answered (network, timeout, TLS)."""


def is_challenge(r: FetchResponse) -> bool:
    """Cloudflare's bot check in place of the page asked for (cf-mitigated: challenge), whatever its status."""
    return r.header("cf-mitigated").lower() == "challenge"


class ExitMoved(PlatformError):
    """The site's server changed (a switch-over, wireproxy stopped) while the request waited for the limiter;
    nothing was sent."""


@dataclass(eq=False)
class _Visitor:
    """One anonymous visitor of a site: its transport (connections, cookies), the route it was built for and when its
    cookies started. The live requests share the client's; each Trial has its own (a test never resets the live
    visitor, nor eBay's warm-up of it)."""
    transport: Transport | None = None
    proxy: str | None = None
    gen: int = 0                  # the VPN process start the transport belongs to (Route.gen)
    since: float = field(default_factory=time.monotonic)
    extra: dict[str, Any] = field(default_factory=dict)    # a site's own state per visitor (eBay: its warm-up)


@dataclass
class Trial:
    """One site's try in the VPN search (app/services/vpn_check.py): requests of the task that set it go out through
    `route` alone, never through another server and never directly, as a visitor of their own; what they met is
    noted here (the search marks a refused server, not the client). close() when done."""
    route: Route
    limit: int = 1                # requests it may send (eBay's warm-up: 3); one more refuses the server
    sent: int = 0                 # requests that went out through it and came back (or failed)
    wire: int = 0                 # requests put on the wire (one under way: wire > sent)
    status: int | None = None     # the last answer's status
    ctype: str = ""               # the last answer's content type: a JSON 200 the parser rejects is the site's error
    challenge: bool = False       # the last answer was Cloudflare's challenge
    refused: str | None = None    # blocked | rate | unreachable
    detail: str = ""              # "HTTP 403", the network error
    visitors: dict[int, tuple[Any, _Visitor]] = field(default_factory=dict, repr=False)   # per client

    def visitor(self, client: Any) -> _Visitor:
        return self.visitors.setdefault(id(client), (client, _Visitor()))[1]

    def close(self) -> None:
        """Close the test's transports in the background (a live request never used them)."""
        for client, v in self.visitors.values():
            if v.transport is not None and v.transport is not client._live.transport:
                _close_later(v.transport)
            v.transport = None
        self.visitors.clear()


_trial: ContextVar[Trial | None] = ContextVar("vpn_trial", default=None)


class LaneLost(Blocked):
    """The lane's server is gone or the site refused it: this lane stops, its work goes to the other lanes."""


@dataclass
class Lane:
    """One of a site's parallel connections (vpn.lanes(); the product search runs one worker per lane): requests of
    the task that set it go out through `route` alone, paced by `limiter` – that server's own, sites pace per IP –
    as a visitor of their own. A refusal or an unreachable server ends the lane (`lost`, LaneLost): the server is
    marked for the site (vpn.lane_refused) and no request of this lane goes anywhere else."""
    pid: str
    route: Route
    limiter: RateLimiter
    lost: str | None = None
    visitors: dict[int, tuple[Any, _Visitor]] = field(default_factory=dict, repr=False)

    def visitor(self, client: Any) -> _Visitor:
        return self.visitors.setdefault(id(client), (client, _Visitor()))[1]

    def close(self) -> None:
        for _client, v in self.visitors.values():
            if v.transport is not None:
                _close_later(v.transport)
            v.transport = None
        self.visitors.clear()


_lane: ContextVar[Lane | None] = ContextVar("vpn_lane", default=None)
_lane_limiters: dict[tuple[str, str], RateLimiter] = {}
# the logged-in session's own direct request (Vinted messaging): paced on its own limiter, so an anonymous search
# block (403 on the shared limiter) never pauses chat and a chat bot-protection 403 never pauses the search
_authed: ContextVar[bool] = ContextVar("authed_request", default=False)


def lane_limiter(pid: str, server: str, like: RateLimiter) -> RateLimiter:
    """The limiter of one server for one site: the site's pacing and hourly budget, counted for that server's IP
    alone (kept while the app runs, so the budget holds across searches)."""
    lim = _lane_limiters.get((pid, server))
    if lim is None:
        lim = _lane_limiters[(pid, server)] = RateLimiter(like.min_gap_s, like.per_hour, like.max_wait_s,
                                                          name=f"{like.name} · {server}")
    return lim


@contextmanager
def lane(ln: Lane) -> Iterator[Lane]:
    """Within the block (this task only) the site's requests go through the lane."""
    token = _lane.set(ln)
    try:
        yield ln
    finally:
        _lane.reset(token)
_closing: set[asyncio.Task] = set()     # transports of tests and of dropped visitors, closing in the background


def _close_later(transport: Transport) -> None:
    async def close() -> None:
        try:
            await transport.aclose()
        except Exception as e:  # noqa: BLE001 - closing a transport nobody uses any more
            log.debug("closing a transport: %s", e)

    task = asyncio.get_running_loop().create_task(close())
    _closing.add(task)
    task.add_done_callback(_closing.discard)


@contextmanager
def trial(tr: Trial) -> Iterator[Trial]:
    """Within the block (this task only; live requests never see it) requests go through tr.route alone."""
    token = _trial.set(tr)
    try:
        yield tr
    finally:
        _trial.reset(token)


def current_trial() -> Trial | None:
    """The Trial of this task, if any (for a client that does not build on HttpPlatformClient: Geizhals)."""
    return _trial.get()


class Transport(Protocol):
    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse: ...

    def clear_cookies(self) -> None: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    """httpx with its own cookie jar; `transport` lets tests serve canned answers (httpx.MockTransport); `proxy` is
    the VPN tunnel's local proxy."""

    def __init__(self, headers: dict[str, str], transport: httpx.AsyncBaseTransport | None = None,
                 proxy: str | None = None) -> None:
        if transport is not None and proxy:      # httpx would take the proxy and quietly ignore the mock
            raise ValueError("a mock transport and a proxy at the same time")
        self._headers, self._transport, self.proxy = headers, transport, proxy
        self.client = self._new()

    def _new(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=TIMEOUT_S, follow_redirects=True, transport=self._transport,
                                 proxy=self.proxy, cookies=httpx.Cookies())

    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse:
        if self.client.is_closed:            # closed at shutdown (releases connections); a later request reopens
            self.client = self._new()
        try:
            r = await self.client.request(method, url, params=params or None, headers=headers)
        except httpx.HTTPError as e:
            raise TransportError(str(e)) from e
        return FetchResponse(r.status_code, dict(r.headers), r.content, str(r.url))

    def clear_cookies(self) -> None:
        self.client.cookies.clear()

    async def aclose(self) -> None:
        await self.client.aclose()


class CurlTransport:
    """curl_cffi impersonating a current Chrome (TLS fingerprint and HTTP/2 settings), for sites behind Akamai and
    the like. Imported lazily: willhaben-only installs never load it."""

    def __init__(self, headers: dict[str, str], impersonate: str = "chrome", proxy: str | None = None) -> None:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession

        # NOPROXY "": libcurl would honour a NO_PROXY environment variable and skip the tunnel for those hosts
        self.proxy = proxy
        self.session = AsyncSession(impersonate=impersonate, headers=headers, timeout=TIMEOUT_S, allow_redirects=True,
                                    proxy=proxy, curl_options={CurlOpt.NOPROXY: ""} if proxy else None)

    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse:
        from curl_cffi import CurlError

        try:
            r = await self.session.request(method, url, params=params or None, headers=headers)
        except CurlError as e:
            raise TransportError(str(e)) from e
        return FetchResponse(r.status_code, {k: v for k, v in r.headers.items()}, r.content, str(r.url))

    def clear_cookies(self) -> None:
        self.session.cookies.clear()

    async def aclose(self) -> None:
        await self.session.close()


class HttpPlatformClient:
    def __init__(self, platform_id: str, limiter: RateLimiter, *, name: str | None = None,
                 headers: dict[str, str] | None = None, transport: Transport | None = None,
                 httpx_transport: httpx.AsyncBaseTransport | None = None, backend: str = "httpx",
                 channel: str = "anonymous", session_s: float = ANON_SESSION_S,
                 transport_factory: Callable[[str | None], Transport] | None = None,
                 tunnel: Callable[[], Tunnel | None] | None = None) -> None:
        """`transport_factory(proxy)` builds the transport for a route (None: direct); `tunnel()` gives the site's VPN
        tunnel or None (default: the site's switch in Settings → VPN; never when a transport is injected)."""
        self.platform = platform_id
        self.name = name or platform_id
        self.channel = channel
        self._limiter = limiter
        self._authed_lim: RateLimiter | None = None   # the logged-in session's limiter, built on first direct request
        self.session_s = session_s
        self._cache: dict[Any, tuple[float, FetchResponse]] = {}
        headers = headers or {}
        injected = transport is not None or httpx_transport is not None
        if transport_factory is None:
            if transport is not None:
                transport_factory = lambda proxy: transport                                  # noqa: E731
            elif httpx_transport is not None:
                transport_factory = lambda proxy: HttpxTransport(headers, httpx_transport)   # noqa: E731
            elif backend == "curl":
                transport_factory = lambda proxy: CurlTransport(headers, proxy=proxy)        # noqa: E731
            else:
                transport_factory = lambda proxy: HttpxTransport(headers, proxy=proxy)       # noqa: E731
        if tunnel is None and not injected:
            from app.platforms import vpn

            tunnel = lambda: vpn.tunnel_for(platform_id)                                    # noqa: E731
        self._factory = transport_factory
        self._tunnel: Callable[[], Tunnel | None] = tunnel or (lambda: None)
        self._live = _Visitor(transport_factory(None) if injected else None)   # else built on first use
        self._authed_v = _Visitor()   # the logged-in session's own connections and cookie jar (direct requests)

    @property
    def limiter(self) -> RateLimiter:
        """The site's limiter – within a lane of this site (parallel work) the lane's server's own; for the logged-in
        session's own request (direct=True) its own, kept apart from the anonymous search's cool-down and strikes."""
        if _authed.get():
            return self._authed_limiter()
        ln = self._lane()
        return ln.limiter if ln is not None else self._limiter

    @limiter.setter
    def limiter(self, value: RateLimiter) -> None:
        self._limiter = value

    def _authed_limiter(self) -> RateLimiter:
        """The logged-in session's limiter: the site's pacing, but its OWN cool-down and strikes, so a search 403
        (the shared limiter) does not pause chat and a chat bot-protection 403 does not pause the search."""
        if self._authed_lim is None:
            base = self._limiter
            self._authed_lim = RateLimiter(base.min_gap_s, 0, base.max_wait_s, name=self.name or self.platform)
        return self._authed_lim

    def _lane(self) -> Lane | None:
        ln = _lane.get()
        return ln if ln is not None and ln.pid == self.platform and _trial.get() is None else None

    def _visitor(self) -> _Visitor:
        """The visitor this request belongs to: the logged-in session's own for a direct request, the Trial's own under
        the VPN search, a lane's own, else the live one."""
        if _authed.get():
            return self._authed_v       # never the anonymous search's transport or jar
        tr = _trial.get()
        if tr is not None:
            return tr.visitor(self)
        ln = self._lane()
        return ln.visitor(self) if ln is not None else self._live

    @property
    def _transport(self) -> Transport | None:
        return self._visitor().transport

    @_transport.setter
    def _transport(self, value: Transport | None) -> None:
        self._visitor().transport = value

    @property
    def _proxy(self) -> str | None:
        return self._visitor().proxy

    @_proxy.setter
    def _proxy(self, value: str | None) -> None:
        self._visitor().proxy = value

    @property
    def _gen(self) -> int:
        return self._visitor().gen

    @_gen.setter
    def _gen(self, value: int) -> None:
        self._visitor().gen = value

    @property
    def _jar_since(self) -> float:
        return self._visitor().since

    @_jar_since.setter
    def _jar_since(self, value: float) -> None:
        self._visitor().since = value

    @property
    def _client(self) -> httpx.AsyncClient | None:
        """The httpx client of the default transport (tests look at its cookie jar)."""
        return getattr(self._transport, "client", None)

    @property
    def proxy(self) -> str | None:
        """The VPN route the last request went through (None: direct)."""
        return self._proxy

    def classify(self, url: str) -> str:
        """Access-log kind of a request; willhaben's path rules by default, a site overrides."""
        return access_log.classify(url)

    def new_anonymous_session(self) -> None:
        """Forget the site's cookies; the next request starts as a new visitor. Not for the logged-in session: its
        jar is the account's, a bot-protection 403 there is healed by re-seeding, not by forgetting."""
        if _authed.get():
            return
        if self._transport is not None:
            self._transport.clear_cookies()
        self._jar_since = time.monotonic()

    async def aclose(self) -> None:
        done: set[int] = set()
        for v in (self._live, self._authed_v):
            if v.transport is not None and id(v.transport) not in done:   # an injected transport is shared
                done.add(id(v.transport))
                await v.transport.aclose()

    def _check_status(self, r: FetchResponse) -> None:
        """403/429 = the site throttles or blocks this IP: new visitor, back-off, Blocked. A site may treat some
        codes differently (e.g. 401 = rotated app credentials)."""
        if r.status_code in (403, 429):
            self.new_anonymous_session()        # don't keep presenting the visitor that just got blocked
            delay = self.limiter.penalize()
            blocked = Blocked(t("{name} responded {status}; pausing {s}s", name=self.name, status=r.status_code,
                                s=f"{delay:.0f}"))
            blocked.sent = True
            raise blocked

    def _exit_unreachable(self, url: str) -> bool:
        """Through the VPN: does a network error on this URL say the server does not reach the site? A site
        overrides this for an optional host (eBay's description)."""
        return True

    def _exit_blocked(self, r: FetchResponse) -> bool:
        """Through the VPN: does this answer refuse the server (then a search finds another)? 403 = the site blocks
        the server's IP (willhaben: Cloudflare), 429 = others on the shared server used up its rate. A site overrides
        this when one of these codes means something else there."""
        return r.status_code in (403, 429)

    async def _use(self, proxy: str | None) -> Transport:
        """The transport for this route; a new route gets a new one (new connections, new visitor). Called with the
        limiter held, so no request of this client is in flight on the old one."""
        v = self._visitor()
        if v.transport is None or proxy != v.proxy:
            old, v.transport, v.proxy = v.transport, self._factory(proxy), proxy
            v.since = time.monotonic()
            if old is not None and old is not v.transport:
                try:
                    await old.aclose()
                except Exception as e:  # noqa: BLE001 - closing the transport of a dead route
                    log.debug("closing transport of %s: %s", self.platform, e)
        return v.transport

    async def _get(self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
                   cache_s: float = 0, kind: str | None = None,
                   valid: Callable[[FetchResponse], bool] | None = None, refresh: bool = False,
                   direct: bool = False) -> FetchResponse:
        """GET through the platform's limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same request
        (`refresh`: fetched anew all the same, the answer replaces the kept one).
        `valid(r)` judges every fetched 200, never a cached or replayed one: False is a page in place of the one asked
        for (a bot check, an error page), which is not cached.

        Every call is counted in the access log and, when switched on, recorded; `kind` defaults to classify(url).
        """
        kind = kind or access_log.image_kind(url) or self.classify(url)     # photos: "image" on every site
        full = str(httpx.URL(url).copy_merge_params(params)) if params else url   # the request as one URL
        key = (full, (headers or {}).get("accept"))
        hit = self._cache.get(key)
        # a 404 can turn into a live ad within minutes (freshly published or reactivated): keep it only briefly
        if cache_s and hit and not refresh and time.monotonic() - hit[0] < (min(cache_s, NOT_FOUND_CACHE_S) if hit[1].status_code == 404 else cache_s):
            access_log.record(self.channel, kind, "cached", platform=self.platform)
            recorder.record_http(self.channel, kind, "GET", full, status=hit[1].status_code, resp_headers=hit[1].headers,
                                 resp_body=hit[1].content, duration_ms=0, outcome="cached", cached=True,
                                 final_url=hit[1].url, store_response=False, platform=self.platform)   # body stored once already
            return hit[1]
        if replay.enabled() and (rec := replay.find("GET", full)):     # WH_REPLAY=1: no request, no waiting
            replay.note(rec, kind)
            return replay.as_fetch_response(rec, full)
        # `direct`: a logged-in request (the Vinted messaging API with the session's own cookies) goes from the home
        # IP like the browser login, never through the anonymous VPN exit — a rotating datacenter IP on an
        # authenticated session is what the bot protection blocks (403), which took the chat offline. It is also
        # paced on its own limiter (_authed), so an anonymous search block does not pause chat, nor chat the search.
        tok = _authed.set(True) if direct else None
        try:
            tunnel, r = (None if direct else self._tunnel()), None
            if tunnel is None:
                if not direct and _trial.get() is not None:   # the VPN test: never from the home IP, even after the switch went off
                    raise PlatformError(t("{name}: the VPN is off, the test sends nothing directly", name=self.name))
                try:
                    r = await self._send_once(full, kind, headers, None, force_direct=direct)
                except ExitMoved:                   # the VPN was switched on while it waited: through the VPN after all
                    if direct or (tunnel := self._tunnel()) is None:   # a direct request stays direct
                        raise
                except TransportError as e:
                    raise PlatformError(t("Network error: {error}", error=e)) from e
            if r is None:
                r = await self._send_vpn(tunnel, full, kind, headers, valid)
            self._check_status(r)
            if r.status_code == 200 and valid is not None and not valid(r):
                return r                            # no answer to keep, and no sign that the site lets us in again
            self.limiter.reward()
        finally:
            if tok is not None:
                _authed.reset(tok)
        if cache_s and r.status_code in (200, 404):
            now = time.monotonic()
            if len(self._cache) > 200:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 600}
            self._cache[key] = (now, r)
        return r

    async def _send_once(self, full: str, kind: str, headers: dict[str, str] | None,
                         route: Route | None, *, force_direct: bool = False) -> FetchResponse:
        """One request through the limiter on `route` (None: direct), counted and recorded. ExitMoved when the
        connection left `route`, or a direct request's site switched the VPN on, while it waited for the limiter
        (nothing sent). `force_direct` keeps a logged-in request on the home IP even when the site's VPN is on."""
        async with self.limiter:
            moved = not self._pinned(route) if route is not None else (not force_direct and self._tunnel() is not None)
            if moved:
                self.limiter.refund()               # waited for the limiter, sent nothing
                raise ExitMoved(t("{name}: the VPN server changed before the request went out", name=self.name))
            if route is not None and route.gen != self._gen:   # another connection: never the old one's transport,
                self._proxy, self._gen = "", route.gen          # even when its port comes back
            transport = await self._use(route.proxy if route is not None else None)
            if time.monotonic() - self._jar_since > self.session_s:
                self.new_anonymous_session()
            with access_log.track(self.channel, kind, platform=self.platform) as tr, self._hold(route):
                started = time.monotonic()
                try:
                    r = await self._send(transport, full, headers)
                except TransportError as e:
                    recorder.record_http(self.channel, kind, "GET", full, req_headers=headers, outcome="error", error=str(e),
                                         duration_ms=int((time.monotonic() - started) * 1000), platform=self.platform)
                    raise
                except asyncio.CancelledError:
                    if _trial.get() is not None:      # a VPN test cut off mid-request: no other request reuses this
                        self._drop_transport()
                    raise
                tr.status = r.status_code
        recorder.record_http(self.channel, kind, "GET", full, req_headers=headers, status=r.status_code,
                             resp_headers=r.headers, resp_body=r.content, final_url=r.url,
                             duration_ms=int((time.monotonic() - started) * 1000), platform=self.platform)
        return r

    async def _send(self, transport: Transport, full: str, headers: dict[str, str] | None) -> FetchResponse:
        """The request on the wire; under a Trial at most TRIAL_TIMEOUT_S (the search moves on instead of waiting for
        a server that does not answer)."""
        if (tr := _trial.get()) is None:
            return await transport.send("GET", full, headers=headers)
        tr.wire += 1
        try:
            return await asyncio.wait_for(transport.send("GET", full, headers=headers), TRIAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._drop_transport()                # cut off mid-request: no other request reuses it
            raise TransportError(f"timed out after {TRIAL_TIMEOUT_S} s") from None

    def _pinned(self, route: Route) -> bool:
        """`route` may still be used (checked once the limiter let the request through): a live request's route is
        still its site's (no switch-over meanwhile, wireproxy did not stop); a Trial's route still runs."""
        tunnel = self._tunnel()
        if tunnel is None:
            return False
        ln = self._lane()
        if _trial.get() is not None or ln is not None and ln.route == route:
            return tunnel.alive(route)
        return tunnel.current() == route

    def _hold(self, route: Route | None) -> Any:
        """A request in flight on `route`: a switch-over lets it finish before that server stops."""
        tunnel = self._tunnel() if route is not None else None
        return tunnel.hold(route) if tunnel is not None else nullcontext()

    def _drop_transport(self) -> None:
        """Forget the transport at once (a cancelled request may have left it half-used); it closes in the background
        and the next request builds a new one (a new visitor)."""
        old, self._transport, self._proxy = self._transport, None, None
        self.new_anonymous_session()          # eBay: a new warm-up too
        if old is not None:
            _close_later(old)

    async def _send_vpn(self, tunnel: Tunnel, full: str, kind: str, headers: dict[str, str] | None,
                        valid: Callable[[FetchResponse], bool] | None = None) -> FetchResponse:
        """Through the site's VPN server: a refused server (403/429, a bot check: Cloudflare's challenge or a 200
        `valid` rejects) or one that does not answer is reported (Tunnel.refused()). Until the refusals in a row
        count (vpn.CONFIRM) the request asks again through the same server with the same visitor (eBay's bot
        manager refuses at random and lets the next one in); once they count, the site moves to its other server
        or pauses and a search starts, and the request asks again once ensure() has a route for it: VPN_ATTEMPTS
        exits and VPN_TRIES sends at most; a paused site's ensure() raises VpnWait. An accepted answer resets the
        count (Tunnel.answered()). No limiter.penalize() for a refused server: willhaben's limiter is shared with the
        logged-in session on the home IP, which the refusal says nothing about. No route: PlatformError, never a
        direct request. Under a Trial (the VPN search) one try through its server only (_send_trial)."""
        if (tr := _trial.get()) is not None:
            return await self._send_trial(tunnel, tr, full, kind, headers, valid)
        if (ln := self._lane()) is not None:
            return await self._send_lane(tunnel, ln, full, kind, headers, valid)
        last: PlatformError | None = None
        exits: set[Route] = set()
        for _ in range(VPN_TRIES):
            route = await tunnel.ensure()                 # PlatformError when there is no route (fail closed)
            exits.add(route)
            if len(exits) > VPN_ATTEMPTS:
                break
            try:
                r = await self._send_once(full, kind, headers, route)
            except ExitMoved as e:                        # switched over while it waited: the new route
                last = e
                continue
            except TransportError as e:
                if not self._exit_unreachable(full):
                    raise PlatformError(t("Network error through the VPN: {error}", error=e)) from e
                await tunnel.refused(route, "unreachable", detail=str(e)[:160])
                last = PlatformError(t("Network error through the VPN: {error}", error=e))
                continue
            if self._exit_blocked(r):
                reason = "rate" if r.status_code == 429 else "blocked"
            elif (is_challenge(r) or (r.status_code == 200 and valid is not None and not valid(r))) \
                    and not tunnel.odd():       # a search found the site changed, not the servers
                reason = "botcheck"
            else:
                if r.status_code not in (403, 429):      # not eBay's warm-up 403 either: an answer the site accepted
                    tunnel.answered(route)
                return r
            if await tunnel.refused(route, reason, detail=f"HTTP {r.status_code}"):
                self.new_anonymous_session()             # it counted: the next server gets a new visitor
            last = Blocked(t("{name} refused the VPN server (HTTP {status})", name=self.name, status=r.status_code)
                           if reason != "botcheck" else
                           t("{name} answered through the VPN server with a bot check", name=self.name))
            last.sent = True
        assert last is not None
        raise last

    async def _send_lane(self, tunnel: Tunnel, ln: Lane, full: str, kind: str, headers: dict[str, str] | None,
                         valid: Callable[[FetchResponse], bool] | None = None) -> FetchResponse:
        """A lane's request: through its server only. Gone, unreachable or refused (403/429, a bot check): the lane
        is lost (LaneLost; a refusal marks the server for the site), never another route and never directly."""
        from app.platforms import vpn

        def lost(reason: str, msg: str, sent: bool = False) -> LaneLost:
            ln.lost = reason
            if reason in ("blocked", "rate", "botcheck", "unreachable"):
                vpn.lane_refused(self.platform, ln.route, reason)
            e = LaneLost(msg)
            e.sent = sent
            return e

        if ln.lost:
            raise LaneLost(t("{name}: this VPN server is out of the search ({reason})", name=self.name, reason=ln.lost))
        if not tunnel.alive(ln.route):
            raise lost("gone", t("{name}: the VPN server stopped", name=self.name))
        try:
            r = await self._send_once(full, kind, headers, ln.route)
        except ExitMoved as e:
            raise lost("gone", str(e)) from e
        except TransportError as e:
            if not self._exit_unreachable(full):
                raise PlatformError(t("Network error through the VPN: {error}", error=e)) from e
            raise lost("unreachable", t("Network error through the VPN: {error}", error=e), sent=True) from e
        if self._exit_blocked(r):
            self.new_anonymous_session()
            raise lost("rate" if r.status_code == 429 else "blocked",
                       t("{name} refused the VPN server (HTTP {status})", name=self.name, status=r.status_code), sent=True)
        if (is_challenge(r) or (r.status_code == 200 and valid is not None and not valid(r))) and not tunnel.odd():
            self.new_anonymous_session()
            raise lost("botcheck", t("{name} answered through the VPN server with a bot check", name=self.name), sent=True)
        return r

    async def _send_trial(self, tunnel: Tunnel, tr: Trial, full: str, kind: str, headers: dict[str, str] | None,
                          valid: Callable[[FetchResponse], bool] | None = None) -> FetchResponse:
        """The test's request: through tr.route once. ExitMoved when that server stopped (nothing sent); a refused or
        unreachable server ends the try, noted in `tr` (the search marks it). A try that already sent tr.limit
        requests without an answer the site accepted (eBay: warm-up 403s, however often its client starts a new
        visitor) refuses the server instead of asking again."""
        if not tunnel.alive(tr.route):
            raise ExitMoved(t("{name}: the VPN server changed before the request went out", name=self.name))
        if tr.sent >= tr.limit:
            tr.refused = "rate" if tr.status == 429 else "blocked"
            tr.detail = f"HTTP {tr.status}" if tr.status else ""
            self.new_anonymous_session()
            blocked = Blocked(t("{name} refused the VPN server (HTTP {status})", name=self.name, status=tr.status))
            blocked.sent = True
            raise blocked
        try:
            r = await self._send_once(full, kind, headers, tr.route)
        except TransportError as e:
            tr.sent, tr.refused, tr.detail = tr.sent + 1, "unreachable", str(e)[:160]
            raise PlatformError(t("Network error through the VPN: {error}", error=e)) from e
        tr.sent, tr.status = tr.sent + 1, r.status_code
        tr.ctype = r.header("content-type").lower()
        tr.challenge = is_challenge(r) or (r.status_code == 200 and valid is not None and not valid(r))
        if self._exit_blocked(r):
            tr.refused, tr.detail = ("rate" if r.status_code == 429 else "blocked"), f"HTTP {r.status_code}"
            self.new_anonymous_session()
            blocked = Blocked(t("{name} refused the VPN server (HTTP {status})", name=self.name, status=r.status_code))
            blocked.sent = True
            raise blocked
        return r
