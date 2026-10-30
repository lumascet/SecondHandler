"""Account-level willhaben endpoints (need the logged-in BrowserSession).

Verification status (see docs/willhaben-api.md):
  VERIFIED   userprofile/me, chat-api/v1/counter, chat-api/v1/conversations (list),
             chat-api/v1/conversations/{id}/messages (first MESSAGE_CANDIDATES entry),
             my adverts (server-rendered in /iad/myprofile/myadverts __NEXT_DATA__)
  FROM BUNDLE send message (chat WebSocket send_message; confirmed per send via the REST history)
  FROM BUNDLE create conversation (ad contact form, POST iad-messaging/sendrequest/chat; see create_conversation)
  FROM BUNDLE mark sold / reserved (app/willhaben/sales.py, links from the my-adverts list; needs live verification)
  MISSING    reactivate, ad insertion -> need a request capture
Unverified write endpoints are never guessed: they raise EndpointUnknown until captured.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.i18n import t
from app.willhaben.parse import BASE, _iso, _ms, attrs, extract_next_data
from app.willhaben.session import BrowserSession, NotLoggedIn

log = logging.getLogger(__name__)

CHAT = "/webapi/chat-api/v1"
MYADVERTS_PAGE = "/iad/myprofile/myadverts"
CHAT_WS = "wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/"
SEND_REQUEST_CHAT = "/webapi/iad-messaging/sendrequest/chat"   # the ad page's contact form, opens the chat
MESSAGE_CANDIDATES = [
    CHAT + "/conversations/{id}/messages?limit=100&offset=0",
    CHAT + "/conversations/{id}/messages",
    CHAT + "/conversations/{id}",
]


class EndpointUnknown(RuntimeError):
    """The willhaben endpoint for this action has not been captured/verified yet."""


class SendFailed(RuntimeError):
    """willhaben did not take the message."""


class OpenerUnconfirmed(RuntimeError):     # not a SendFailed: the message DID go out
    """willhaben accepted a new conversation (2xx) but the chat could not be found yet: the message HAS gone out, so
    starting again would write to the seller a second time."""


@dataclass
class ConversationSummary:
    id: str
    ad_id: str | None
    ad_title: str | None
    partner_name: str | None
    unread_count: int
    last_text: str | None
    last_at: datetime | None
    raw: dict[str, Any]
    role: str | None = None  # seller | buyer, when the response says who posted the ad
    last_is_mine: bool | None = None  # the newest message was written by me (not news for a push)
    last_read: bool | None = None     # the newest message's read flag: with last_is_mine, the other person has seen it
    partner_uuid: str | None = None   # participant_info.uuid: asked for their online state (track_presence)
    ad_uuid: str | None = None        # ad_info.uuid: the profile page of the other person hangs off it


@dataclass
class ChatMessage:
    ext_id: str
    text: str
    created_at: datetime | None
    direction: str  # in | out | unknown
    read: bool | None = None  # willhaben's read flag: incoming, did I read it; my own, has the other person seen it
    kind: str | None = None   # system | integration: willhaben's own notice (PayLivery, status); None = a normal message
    raw_links: list[str] = field(default_factory=list)  # every http(s) string inside the message object
    params: dict[str, Any] | None = None  # willhaben's structured `parameters` of the message (notices carry them)


def _pick(d: dict[str, Any] | None, *keys: str) -> Any:
    """First non-empty value among dotted keys, e.g. _pick(x, 'lastMessage.text', 'lastMessageText')."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", []):
            return cur
    return None


def _when(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return _ms(str(n if n > 1e11 else n * 1000))
    return _iso(str(v))


def _list(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    found = _pick(data, *keys)
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []


def _role(c: dict[str, Any]) -> str | None:
    me, seller = c.get("user_uuid"), _pick(c, "ad_info.user_uuid")
    if not me or not seller:
        return None
    return "seller" if me == seller else "buyer"


def parse_conversations(data: Any) -> list[ConversationSummary]:
    out = []
    for c in _list(data, "conversations", "data", "items", "content"):
        cid = _pick(c, "conversation_id", "id", "conversationId", "uuid", "conversationUuid")
        if cid is None:
            continue
        out.append(ConversationSummary(
            id=str(cid),
            ad_id=str(_pick(c, "ad_info.id", "adId", "advertId", "ad.id", "advert.id") or "") or None,
            ad_title=_pick(c, "ad_info.subject", "adTitle", "advertTitle", "ad.title", "advert.title", "subject"),
            partner_name=_pick(c, "participant_info.name", "partnerName", "partner.name", "partner.nickname", "otherParticipant.name", "contactName"),
            unread_count=int(_pick(c, "unseen", "unreadCount", "unreadMessages", "unread") or 0),
            last_text=_pick(c, "last_message.preview", "lastMessage.text", "lastMessage.body", "lastMessage.message", "lastMessageText", "preview"),
            last_at=_when(_pick(c, "last_message.timestamp", "updated_at", "lastMessage.createdAt", "lastMessage.timestamp", "lastMessageDate", "updatedAt", "lastActivity")),
            raw=c,
            role=_role(c),
            last_is_mine=_pick(c, "last_message.is_mine") if isinstance(_pick(c, "last_message.is_mine"), bool) else None,
            last_read=_pick(c, "last_message.read") if isinstance(_pick(c, "last_message.read"), bool) else None,
            partner_uuid=_pick(c, "participant_info.uuid"),
            ad_uuid=_pick(c, "ad_info.uuid"),
        ))
    return out


def _notice_kind(m: dict[str, Any]) -> str | None:
    """`system_message` / `integration_message` types are willhaben notices, not written by either side."""
    kind = m.get("type")
    if isinstance(kind, str) and kind.endswith("system_message"):
        return "system"
    if isinstance(kind, str) and kind.endswith("integration_message"):
        return "integration"
    return None


def _links(obj: Any, depth: int = 0) -> list[str]:
    """Every string starting with http(s) in a message object, structured fields included (deduplicated)."""
    if depth > 6:
        return []
    if isinstance(obj, str):
        found = [obj] if obj.startswith(("http://", "https://")) else []
    elif isinstance(obj, dict):
        found = [x for v in obj.values() for x in _links(v, depth + 1)]
    elif isinstance(obj, list):
        found = [x for v in obj for x in _links(v, depth + 1)]
    else:
        found = []
    return list(dict.fromkeys(found))


def parse_messages(data: Any, my_ids: set[str]) -> list[ChatMessage]:
    out = []
    for m in _list(data, "messages", "data", "items", "content", "conversation.messages"):
        kind = _notice_kind(m)
        text = _pick(m, "text", "body", "message", "content", "messageText")
        if not isinstance(text, str):
            if kind is None:
                continue
            text = ""   # a notice without text still carries its links (see raw_links)
        created = _when(_pick(m, "created_at", "createdAt", "timestamp", "sentAt", "date", "created"))
        ext = _pick(m, "message_id", "id", "messageId", "uuid")
        if ext is None:
            ext = hashlib.sha1(f"{created}|{text}".encode()).hexdigest()[:16]
        own = _pick(m, "is_mine", "isOwnMessage", "ownMessage", "own", "fromMe", "isMine")
        sender = _pick(m, "sender_id", "sender_uuid", "user_uuid", "senderId", "sender.id", "sender.uuid", "senderUuid", "userId", "from")
        direction = "unknown"
        if kind is not None:
            pass  # willhaben notices (PayLivery, safety tips, status) - neither side wrote them
        elif isinstance(own, bool):
            direction = "out" if own else "in"
        elif sender is not None and my_ids:
            direction = "out" if str(sender) in my_ids else "in"
        elif isinstance(_pick(m, "direction", "type"), str):
            d = _pick(m, "direction", "type").lower()
            direction = "out" if d in ("out", "outgoing", "sent") else "in" if d in ("in", "incoming", "received") else "unknown"
        read = m.get("read")
        params = m.get("parameters") if isinstance(m.get("parameters"), (dict, list)) else None
        out.append(ChatMessage(ext_id=str(ext), text=text, created_at=created, direction=direction,
                               read=read if isinstance(read, bool) else None, kind=kind, raw_links=_links(m),
                               params=params))
    out.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return out


@dataclass
class OwnAdvert:
    ad_id: str
    heading: str
    status: str            # willhaben advertStatus.id: active, expired, deactivated, sold, ...
    status_label: str | None
    price_display: str | None
    start: datetime | None
    end: datetime | None
    url: str
    image_url: str | None
    actions: dict[str, dict[str, Any]] | None = None   # advertEditStatusActionsList link id -> link
    reasons_query: bool = False   # non-empty deleteReasonQueryParameter: willhaben asks for a reason before "sold"


class AdvertList(list):
    """The account's ads as the my-adverts page lists them; `complete` = every ad of the account is in it."""
    total: int | None = None      # initialUserAdsList.totalNumberOfAdvertDetails

    @property
    def complete(self) -> bool:
        return self.total is not None and len(self) >= self.total


# Link ids in an own ad's advertEditStatusActionsList (from the /bbx-adin myadverts bundle, 2026-10-09)
ACTION_ACTIVATE = "4"     # deactivated ad back online: PUT, no body
ACTION_FINALIZE = "5"     # unfinished insertion ("Fertigstellen", e.g. a renewal that was never completed): no call of
#                           its own, willhaben's button opens the insertion form for this same ad id; finished like a new ad
ACTION_REPUBLISH = "6"    # expired / passive ad: POST -> new insertion draft (new adId), finished like a new ad
ACTION_DEACTIVATE = "2"   # take a live ad offline (reversible via ACTIVATE): PUT, no body
ACTION_DELETE = "3"       # delete the ad for good: DELETE, no body (the delete-reason survey is optional)
# statuses whose ad may come back as a copy when there is no ACTIVATE; "passive" ("Passiv") covers both an ad
# deactivated by hand (offers ACTIVATE) and one that ended (seen live 2026-10-09: only REPUBLISH and DELETE)
REPUBLISHABLE = ("expired", "passive")


def has_ended(ad: OwnAdvert) -> bool:
    """The ad's run is over (willhaben lists it as "Passiv", not "expired"): no ACTIVATE, only REPUBLISH.
    An ad deactivated by hand is passive too but keeps ACTIVATE, so it does not count."""
    links = ad.actions or {}
    return ad.status == "expired" or (ad.status == "passive" and ACTION_ACTIVATE not in links
                                      and ACTION_REPUBLISH in links)


def parse_my_adverts(next_data: dict[str, Any]) -> AdvertList:
    """Own ads from the `__NEXT_DATA__` of /iad/myprofile/myadverts (all verticals, first 50)."""
    try:
        lst = next_data["props"]["pageProps"]["initialUserAdsList"]
    except (KeyError, TypeError) as e:
        raise ValueError(t("Page contains no ad list")) from e
    return parse_ads_list(lst)


def parse_ads_list(lst: dict[str, Any]) -> AdvertList:
    """Own ads from an ads list: the page's initialUserAdsList or the app API's showpagedadsfromuser answer."""
    out = AdvertList()
    total = lst.get("totalNumberOfAdvertDetails")
    out.total = total if isinstance(total, int) else None
    for vertical in lst.get("verticalList") or []:
        for ad in vertical.get("advertDetails") or []:
            ad_id = str(ad.get("id") or "")
            if not ad_id:
                continue
            a = attrs(ad)
            status = ad.get("advertStatus") or {}
            images = (ad.get("advertImageList") or {}).get("advertImage") or []
            out.append(OwnAdvert(
                ad_id=ad_id,
                heading=ad.get("description") or "",
                status=status.get("id") or "unknown",
                status_label=status.get("description"),
                price_display=(a.get("PRICE_FOR_DISPLAY") or [None])[0],
                start=_iso(ad.get("startDate")),
                end=_iso(ad.get("endDate")),
                url=f"{BASE}/iad/object?adId={ad_id}",
                image_url=(images[0].get("mainImageUrl") or images[0].get("thumbnailImageUrl")) if images else None,
                actions={str(link.get("id")): link for link in
                         ((ad.get("advertEditStatusActionsList") or {}).get("contextLinkList") or {}).get("contextLink") or []
                         if link.get("relativePath") or str(link.get("id")) == ACTION_FINALIZE},   # 5 needs no path
                reasons_query=bool(ad.get("deleteReasonQueryParameter")),
            ))
    return out


def _preview_matches(preview: str | None, text: str) -> bool:
    """The list shows the newest message, possibly cut short with an ellipsis: compare the shared start."""
    p = (preview or "").strip().rstrip("…").rstrip()
    x = text.strip()
    return bool(p) and (x == p or x.startswith(p) or p.startswith(x))


def _link_path(link: dict[str, Any]) -> str:
    return f"/webapi/{link.get('serviceName') or 'iad'}{link['relativePath']}"


class AccountClient:
    def __init__(self, session: BrowserSession) -> None:
        self.s = session
        self._messages_path: str | None = None

    async def me(self) -> dict[str, Any]:
        return await self.s.api_json("GET", "/webapi/userprofile-service/userprofile/me")

    async def unread_count(self) -> int:
        data = await self.s.api_json("GET", CHAT + "/counter")
        if isinstance(data, (int, float)):
            return int(data)
        v = _pick(data, "count", "unreadCount", "unread", "total", "unreadConversations")
        return int(v or 0)

    async def conversations(self, limit: int = 50, offset: int = 0) -> list[ConversationSummary]:
        data = await self.s.api_json("GET", f"{CHAT}/conversations?limit={limit}&offset={offset}")
        return parse_conversations(data)

    def _my_ids(self) -> set[str]:
        u = self.s.user or {}
        return {str(v) for v in (u.get("id"), u.get("uuid")) if v}

    async def messages(self, conversation_id: str) -> list[ChatMessage]:
        """Messages of one conversation. Endpoint not yet verified: tries read-only candidates."""
        paths = [self._messages_path] if self._messages_path else MESSAGE_CANDIDATES
        for tmpl in paths:
            r = await self.s.api("GET", tmpl.format(id=conversation_id))
            if not r.ok:
                continue
            try:
                msgs = parse_messages(r.json(), self._my_ids())
            except ValueError:
                continue
            if msgs:
                if self._messages_path != tmpl:
                    log.info("chat messages endpoint works: %s", tmpl)
                self._messages_path = tmpl
                return msgs
        raise EndpointUnknown(t(
            "Messages endpoint not verified yet. Start a recording named 'chat' under 'Session' "
            "and open a conversation in the browser window."
        ))

    async def mark_read(self, conversation_id: str, msgs: list[ChatMessage] | None = None) -> int:
        """Send read receipts for unread incoming messages, like the web app does when they scroll into view.

        Through the app API (logged in there): one PUT that marks the conversation read, as willhaben's app does
        (ChatApiMarkConversationRead, from the APK 8.67.0). Else the web app's frames (bundle 2026-10-09):
        {req_type: "mark_message_as_read", payload: {conversation_id, message_id}}, no request_id, fire-and-forget.
        Returns how many were marked.
        """
        from app.willhaben import native

        if msgs is None:
            msgs = await self.messages(conversation_id)
        unread = [m for m in msgs if m.direction == "in" and m.read is False]
        if not unread:
            return 0
        if (c := native.active()) and (link := c.link("ChatApiMarkConversationRead")):
            try:
                r = await c.request("PUT", link.replace("$conversation_id", conversation_id), kind="chat")
            except native.NativeError as e:          # marking read twice does no harm: the browser tries
                log.info("read receipt through the app API failed: %s", e)
            else:
                if r.ok:
                    return len(unread)
                log.info("read receipt through the app API: HTTP %s, the browser sends it", r.status)
        frames = [{"payload": {"conversation_id": conversation_id, "message_id": m.ext_id},
                   "req_type": "mark_message_as_read"} for m in unread]
        await self.s.ws_send(CHAT_WS, frames)
        return len(unread)

    async def delete_conversations(self, ids: list[str]) -> None:
        """Delete conversations on willhaben for good, as the chat web app's "delete" does (recorded 2026-10-25):
        DELETE chat-api/v1/conversations with {"ids": [...]}, answered 204. Several at once."""
        r = await self.s.api("DELETE", f"{CHAT}/conversations", {"ids": list(ids)})
        if r.status not in (200, 204):
            raise SendFailed(t("willhaben did not delete the chat (HTTP {status})", status=r.status))

    async def ping_chat(self) -> dict[str, Any]:
        """Health check of the chat WebSocket (the web app's own ping); sends no message."""
        return await self.s.ws_request(CHAT_WS, {"req_type": "ping"}, None, req_type="ping", timeout_ms=10000)

    async def send_message(self, conversation_id: str, text: str) -> str:
        """Send a text message; a send only counts once willhaben shows it in the history. Returns its message_id.

        Through the app API when it is logged in there: POST <ChatApiSendMessage><conversation id> with
        {client_message_id, text, createdAt}, as willhaben's app sends it (APK 8.67.0). When the app API surely did not
        take it (refused, no such endpoint), the browser sends it; after a server error or a broken connection it may
        have, so the history decides and nothing is sent a second time.

        Else the way the chat web app does (WebSocket `send_message`, from its bundle 2026-10-09).
        """
        from app.willhaben import native

        text = text.strip()
        if not text:
            raise ValueError(t("Empty message"))
        client_id = str(uuid.uuid4())
        if (c := native.active()) and (base := c.link("ChatApiSendMessage")):
            body = {"client_message_id": client_id, "text": text,
                    "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            try:
                r = await c.request("POST", base.rstrip("/") + "/" + conversation_id, body, kind="send")
            except native.NativeError as e:
                return await self._confirm(conversation_id, client_id, text, t("connection broke ({error})", error=e))
            if not native.falls_back(r.status, "POST"):
                if r.ok:
                    return await self._confirm(conversation_id, client_id, text, f"HTTP {r.status}")
                if r.status < 500:                     # a 4xx it explained: nothing went out
                    raise SendFailed(t("willhaben rejected the message: {detail}", detail=f"HTTP {r.status} {r.text[:200]}"))
                return await self._confirm(conversation_id, client_id, text, f"HTTP {r.status}")
            log.info("message through the app API: HTTP %s, the browser sends it", r.status)
        request_id = str(uuid.uuid4())
        frame = {"payload": {"conversation_id": conversation_id, "client_message_id": client_id,
                             "text": text, "type": "text"},
                 "req_type": "send_message", "request_id": request_id}
        res = await self.s.ws_request(CHAT_WS, frame, request_id, req_type="send_message")
        if res.get("status") == "error":
            raise SendFailed(t("willhaben rejected the message: {detail}", detail=res.get('response') or res.get('error') or res))
        if res.get("status") in ("ws_error", "closed") and not res.get("acked"):
            raise SendFailed(t("Chat connection failed ({status})", status=f"{res.get('status')} {res.get('code') or ''}".strip()))
        # success, or only an ack before the timeout: the history decides
        return await self._confirm(conversation_id, client_id, text, str(res.get("status")))

    async def _confirm(self, conversation_id: str, client_id: str, text: str, status: str) -> str:
        """The message's id once the history shows it (by our client_message_id, or as our newest text), else
        SendFailed – the message may still have gone out, so it is never sent again from here."""
        for attempt in range(3):
            await asyncio.sleep(1.5 * (attempt + 1))
            data = await self.s.api_json("GET", f"{CHAT}/conversations/{conversation_id}/messages?limit=10&offset=0")
            for m in (data or {}).get("messages") or []:
                if m.get("client_message_id") == client_id or (m.get("is_mine") and (m.get("text") or "").strip() == text):
                    return str(m.get("message_id") or client_id)
        raise SendFailed(t("Message did not show up in the history (response: {status}) – check on willhaben",
                           status=status))

    async def create_conversation(self, ad_id: str, text: str) -> str:
        """Open a new chat about an ad with a first message (the buyer's opener). Returns the conversation id.

        A chat with a seller can only be started from the ad page: its contact form ("Nachricht absenden") posts
        `POST /webapi/iad-messaging/sendrequest/chat` and answers `{sent, conversationId}` (ad-detail bundle of
        /bbx-search, 2026-10-10, see docs/willhaben-api.md "Purchase orders"). The chat WebSocket cannot open one:
        a `create_conversation` frame went unanswered live (timeout). The sender's name and e-mail come from the
        profile, like the form fills them in. Without an id in the answer the conversation list is polled for the
        chat of this ad that holds our text (find_conversation). Once the POST went through, any failure is
        OpenerUnconfirmed: the opener reached the seller, so it must never be sent a second time.
        """
        text = text.strip()
        if not text:
            raise ValueError(t("Empty message"))
        me = await self._profile()
        u = (me.get("userData") if isinstance(me, dict) and isinstance(me.get("userData"), dict) else me) or {}
        first, last = (u.get("firstName") or "").strip(), (u.get("lastName") or "").strip()
        body = {"fromFullName": " ".join(x for x in (first, last) if x), "shareTenantProfile": False,
                "from": u.get("emailAddress") or u.get("email") or "", "adId": int(ad_id), "copyToSender": False,
                "showTelephoneNumber": False, "telephone": "", "selectedContactSuggestions": [], "mailContent": text}
        if first:
            body["firstName"] = first
        if last:
            body["lastName"] = last
        r = await self.s.api("POST", SEND_REQUEST_CHAT, body)
        if r.status in (401, 403):             # nothing was sent; logged out only if the profile fails as well
            await self._profile()              # raises NotLoggedIn (and marks the session) when the login is gone
            raise SendFailed(t("willhaben refused the contact form (HTTP {status})", status=r.status))
        try:
            res = r.json()
        except ValueError:
            res = {}
        if not r.ok or (isinstance(res, dict) and res.get("sent") is False):
            raise SendFailed(t("willhaben rejected the new conversation: {detail}",
                               detail=f"HTTP {r.status} {r.text[:200]}".strip()))
        cid = _pick(res, "conversationId", "conversation_id") if isinstance(res, dict) else None
        if cid:
            return str(cid)
        # sent, but no id in the answer: the conversation list decides
        try:
            cid = await self.find_conversation(ad_id, text)
        except Exception as e:  # noqa: BLE001 - the opener went out: whatever fails now, it must never be sent again
            raise OpenerUnconfirmed(t("Conversation could not be looked up ({error}) – check on willhaben",
                                      error=e)) from e
        if cid:
            return cid
        raise OpenerUnconfirmed(t("Conversation did not show up in the list (response: {status}) – check on willhaben",
                                  status=r.status))

    async def _profile(self) -> dict[str, Any]:
        """me() around the contact form: NotLoggedIn as is, any other error as SendFailed (nothing was sent)."""
        try:
            return await self.me()
        except NotLoggedIn:
            raise
        except Exception as e:  # noqa: BLE001 - the caller may simply fail the order
            raise SendFailed(t("Profile could not be read: {error}", error=e)) from e

    async def find_conversation(self, ad_id: str, text: str, tries: int = 3, pages: int = 3) -> str | None:
        """The chat of an ad that holds our message with this text (after a contact-form opener), or None. The seller
        may have answered already, so for the ad's chats whose newest message is not ours the history decides."""
        for attempt in range(tries):
            await asyncio.sleep(1.5 * (attempt + 1))
            for page in range(pages):
                convs = await self.conversations(limit=50, offset=50 * page)
                for c in convs:
                    if c.ad_id != str(ad_id):
                        continue
                    if c.last_is_mine is not False and _preview_matches(c.last_text, text):
                        return c.id
                    if await self._holds_mine(c.id, text):
                        return c.id
                if len(convs) < 50:
                    break
        return None

    async def _holds_mine(self, conversation_id: str, text: str) -> bool:
        try:
            msgs = await self.messages(conversation_id)
        except (EndpointUnknown, ValueError):
            return False
        return any(m.direction == "out" and _preview_matches(m.text, text) for m in msgs)

    async def my_adverts(self) -> AdvertList:
        """The account's ads: from the app API when it is logged in (every page), else from the my-adverts page that
        willhaben renders server-side (first 50). Same list either way."""
        from app.willhaben import native

        try:
            lst = await native.my_ads_list()
        except native.NativeError as e:
            log.info("own ads through the app API failed, the browser lists them: %s", e)
            lst = None
        if lst is not None:
            return parse_ads_list(lst)
        r = await self.s.api("GET", MYADVERTS_PAGE, accept="text/html")
        nd = extract_next_data(r.text) if r.ok else None
        if not nd or nd.get("page") != MYADVERTS_PAGE:   # anonymous users land on the login page
            raise NotLoggedIn(t("{page}: no ad list (HTTP {status}, logged in?)", page=MYADVERTS_PAGE, status=r.status))
        return parse_my_adverts(nd)

    async def reactivate(self, ad_id: str, ads: list[OwnAdvert] | None = None) -> str:
        """Put an own ad online again, like the buttons in "Meine Anzeigen". Returns the ad id afterwards.

        Deactivated ads offer ACTIVATE (same id). An unfinished insertion offers FINALIZE ("Fertigstellen"): the
        draft is finished under its own id. Expired and passive ads offer REPUBLISH, which creates a new insertion
        draft with a NEW id. Drafts are finished like a new ad (save -> empty cart -> checkout, never paid).
        `ads`: an account list fetched before (several ads in a row need only one list request).
        """
        from app.willhaben import insert

        ad = next((a for a in (ads if ads is not None else await self.my_adverts()) if a.ad_id == str(ad_id)), None)
        if ad is None:
            raise EndpointUnknown(t("Ad {id} is not in the account list", id=ad_id))
        links = ad.actions or {}
        if link := links.get(ACTION_ACTIVATE):
            await self.s.api_json("PUT", _link_path(link))
            return ad.ad_id
        if ACTION_FINALIZE in links:     # before REPUBLISH: finishing this draft never leaves a second copy behind
            base = await insert.get_own_ad(self.s, ad.ad_id)
            res = await insert.publish(self.s, base, [], existing_ad_id=ad.ad_id)
            return res["ad_id"]
        if (link := links.get(ACTION_REPUBLISH)) and ad.status in REPUBLISHABLE:   # never duplicate a live ad
            new = await self.s.api_json("POST", _link_path(link))
            new_id = str((new or {}).get("adId") or "")
            if not new_id:
                raise EndpointUnknown(t("willhaben returned no ad id for the republished ad"))
            base = await insert.get_own_ad(self.s, new_id)
            res = await insert.publish(self.s, base, [], existing_ad_id=new_id)
            return res["ad_id"]
        raise EndpointUnknown(t("willhaben offers no reactivation for this ad (status: {status})",
                                status=ad.status_label or ad.status))

    async def _ad_action(self, ad_id: str, action: str, method: str, what: str,
                         ads: list[OwnAdvert] | None = None) -> None:
        """Run one of willhaben's own action links of an ad, like its button in "Meine Anzeigen".

        `ads`: an account list fetched before (several ads in a row need only one list request).
        """
        ad = next((a for a in (ads if ads is not None else await self.my_adverts()) if a.ad_id == str(ad_id)), None)
        if ad is None:
            raise EndpointUnknown(t("Ad {id} is not in the account list", id=ad_id))
        link = (ad.actions or {}).get(action)
        if link is None:
            raise EndpointUnknown(t("willhaben offers no “{what}” for this ad (status: {status})",
                                    what=what, status=ad.status_label or ad.status))
        r = await self.s.api(method, _link_path(link))
        if r.status in (401, 403):
            raise NotLoggedIn(f"{_link_path(link)}: HTTP {r.status}")
        if not r.ok:
            raise RuntimeError(f"{method} {_link_path(link)}: HTTP {r.status} {r.text[:200]}")

    async def deactivate_ad(self, ad_id: str, ads: list[OwnAdvert] | None = None) -> None:
        await self._ad_action(ad_id, ACTION_DEACTIVATE, "PUT", t("Deactivate"), ads)

    async def delete_ad(self, ad_id: str, ads: list[OwnAdvert] | None = None) -> None:
        await self._ad_action(ad_id, ACTION_DELETE, "DELETE", t("Delete"), ads)

    async def publish_ad(self, draft: dict[str, Any]) -> str:
        raise EndpointUnknown(t("Ad posting not captured yet (recording 'anzeige-aufgeben')."))
