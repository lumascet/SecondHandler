"""Runtime configuration from environment / .env (secrets and infrastructure only).

User-editable preferences (reply rules, toggles) live in the database `setting` table.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("./data")

    # --- Claude (Option B: `claude -p` with the Max subscription token) ---
    llm_backend: str = "claude_cli"
    claude_bin: str = "claude"
    llm_model: str = "opus"
    llm_timeout_s: int = 240
    # Read by the claude CLI itself from the environment; listed here so it is documented and checked.
    claude_code_oauth_token: str | None = None

    # --- willhaben access ---
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    request_min_gap_s: float = 3.0       # between any two willhaben requests (search, pages, account)
    requests_per_hour: int = 240         # hard budget over all willhaben requests; 0 = off
    default_agent_interval_s: int = 300
    min_agent_interval_s: int = 120
    search_rows: int = 30
    # WH_REPLAY=1: answer willhaben requests from the stored recordings (app/willhaben/replay.py); a request never
    # seen before still goes to willhaben (and is recorded). With BROWSER_ENABLED=0 the account works offline too.
    wh_replay: bool = False
    # --- platforms (app/platforms): which search-agent adapters exist besides willhaben; switching them on for
    # the user happens in Settings → Platforms
    platforms_enabled: str = "willhaben,kleinanzeigen,shpock,vinted,preisjaeger,ebay,facebook"
    # hourly budgets: None = willhaben's (requests_per_hour, as set in Settings → willhaben), so all sites get the same
    platform_kleinanzeigen_min_gap_s: float = 3.0
    platform_kleinanzeigen_per_hour: int | None = None
    platform_shpock_min_gap_s: float = 4.0
    platform_shpock_per_hour: int | None = None
    platform_vinted_min_gap_s: float = 8.0          # DataDome watches the website; every search is a whole page
    platform_vinted_per_hour: int | None = None
    platform_preisjaeger_min_gap_s: float = 4.0
    platform_preisjaeger_per_hour: int | None = None
    platform_ebay_min_gap_s: float = 8.0            # Akamai watches the website; every search is a whole page
    platform_ebay_per_hour: int | None = None
    platform_facebook_min_gap_s: float = 8.0        # logged-out web pages; Facebook's terms forbid automated access
    platform_facebook_per_hour: int | None = 60     # own budget, below willhaben's
    kleinanzeigen_api_auth: str = ""            # "user:pw" of the kleinanzeigen app API (the app's own public creds);
    #                                             set here it wins over Settings → Platforms; none set: the agents are off
    willhaben_app_key: str = ""                 # signs the willhaben app API's token request (from willhaben's Android app);
    #                                             set here it wins over Settings → willhaben; none: the browser does it all
    # --- VPN for the anonymous requests (app/platforms/vpn.py): WireGuard configs are uploaded in Settings → VPN
    wireproxy_bin: str = "wireproxy"             # userspace WireGuard with a local proxy; part of the image

    # --- logged-in browser session (Playwright persistent profile) ---
    browser_enabled: bool = True
    browser_headless: bool = False
    browser_chrome: bool = True        # Google Chrome via patchright when installed (BROWSER_CHROME=0: bundled Chromium)
    novnc_url: str | None = None  # e.g. http://homeserver:6080/vnc.html?autoconnect=1&resize=scale
    chat_poll_interval_s: int = 300
    humanize_chat: bool = True   # human-like delays + read receipts for auto replies (settings can switch off)
    session_check_interval_s: int = 900

    # --- my ads ---
    myads_check_hour: int = 8
    # --- background jobs (app/services/jobs.py) ---
    # Irreversible work (delete an ad, mark it sold, send a message) waits this long in the queue before it runs:
    # the window to take the click back (cancel button on the page, jobs popover) instead of a confirm dialog.
    job_grace_s: float = 5.0

    # --- Home Assistant notifications ---
    ha_url: str | None = None
    ha_token: str | None = None
    ha_notify_service: str | None = None  # e.g. "mobile_app_pixel_8" -> notify.mobile_app_pixel_8
    ha_listener: bool = True  # keep a websocket to HA for push-button taps (off in tests)
    telegram_bot_token: str = ""  # from @BotFather; Settings stores one in DATA_DIR/telegram-token (wins)
    telegram_listener: bool = True  # long-poll Telegram for button taps (off in tests)
    chat_live: bool = True    # keep the willhaben chat WebSocket open instead of relying on polling

    # --- web app ---
    public_url: str = "http://localhost:8000"
    # Optional HTTP Basic auth for dashboard + API (recommended: the app can send messages for you).
    dashboard_user: str = "admin"
    dashboard_password: str | None = None
    # A login for scripts on the home network while debugging (header X-Debug-Token, app/auth.debug_login): only
    # straight from a private address, never through the reverse proxy. Unset = off.
    debug_token: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "ad-drafts"

    @property
    def hit_images_dir(self) -> Path:
        return self.data_dir / "hit-images"

    @property
    def wireguard_dir(self) -> Path:
        return self.data_dir / "wireguard"


def address_check(url: str | None) -> dict[str, bool]:
    """Can a phone open links to this dashboard address? `reachable`: not a loopback/placeholder host;
    `https`: Telegram buttons need it."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    reachable = parts.scheme in ("http", "https") and host not in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1")
    return {"reachable": reachable, "https": reachable and parts.scheme == "https"}


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    for d in (s.captures_dir, s.uploads_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
