import json
from typing import List, Dict, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    CLOUDBEDS_API_KEY: str = "placeholder_key"
    CLOUDBEDS_PROPERTY_IDS: str = "[]"
    EXCHANGE_RATE_API_KEY: str = "placeholder_key"
    ANTHROPIC_API_KEY: str = ""

    # Unified ads source — replaces Meta Graph API + Google Sheets exports (migration 028).
    ADS_PLATFORM_BASE_URL: str = "https://ads-performance-fuls.zeabur.app"
    ADS_PLATFORM_API_KEY: str = ""

    # Google OAuth — retained for KOL sheet sync + GHL email sync (NOT ads).
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""

    # Email transport — Resend is preferred (HTTP, works on Zeabur).
    # SendGrid is supported as a fallback. Gmail SMTP is dev-only:
    # most PaaS providers (Zeabur, Render, Heroku) block outbound SMTP.
    RESEND_API_KEY: str = ""
    SENDGRID_API_KEY: str = ""
    EMAIL_FROM: str = ""
    EMAIL_RECIPIENTS: str = ""
    GMAIL_USER: str = ""
    GMAIL_APP_PASSWORD: str = ""
    APP_ENV: str = "development"
    SECRET_KEY: str = "changeme"
    FRONTEND_URL: str = ""

    # Public origin of THIS service, used to build links that are opened from
    # outside the app — currently the Bi-Weekly report's no-login share link,
    # which is served by the backend (it is a whole rendered HTML page, not a
    # dashboard route). FRONTEND_URL cannot stand in for it: on Zeabur the
    # frontend is a separate deployment on a different domain and would 404.
    #
    # Defaulted to production rather than left empty, the same way
    # ADS_PLATFORM_BASE_URL and KOL_ENGINE_URL are. An empty value put a
    # relative path into an outgoing email, which is a dead link in an inbox
    # and only discoverable after sending one — a bad failure to leave one
    # unset env var away. Override per environment when it is not this host.
    PUBLIC_API_URL: str = "https://meander-hid-dashboard.zeabur.app"

    # Shared secret used by GitHub Actions cron workflows to call /api/sync/*
    # endpoints. Empty string disables the check (dev convenience).
    SYNC_TRIGGER_TOKEN: str = ""

    # GoHighLevel (GHL) — Email Marketing (per-branch)
    GHL_LOCATION_ID_SAIGON: str = ""
    GHL_API_KEY_SAIGON: str = ""
    GHL_LOCATION_ID_1948: str = ""
    GHL_API_KEY_1948: str = ""
    GHL_LOCATION_ID_TAIPEI: str = ""
    GHL_API_KEY_TAIPEI: str = ""
    GHL_LOCATION_ID_OANI: str = ""
    GHL_API_KEY_OANI: str = ""
    GHL_LOCATION_ID_OSAKA: str = ""
    GHL_API_KEY_OSAKA: str = ""
    GHL_WEBHOOK_SECRET: str = ""

    # Cloudbeds inbound webhook secret (set in Cloudbeds → Webhooks → Secret)
    CLOUDBEDS_WEBHOOK_SECRET: str = ""

    # Meta Conversions API — per-branch pixel + system user access token
    META_PIXEL_ID_1948: str = ""
    META_ACCESS_TOKEN_1948: str = ""
    META_PIXEL_ID_SAIGON: str = ""
    META_ACCESS_TOKEN_SAIGON: str = ""
    META_PIXEL_ID_TAIPEI: str = ""
    META_ACCESS_TOKEN_TAIPEI: str = ""
    META_PIXEL_ID_OANI: str = ""
    META_ACCESS_TOKEN_OANI: str = ""
    META_PIXEL_ID_OSAKA: str = ""
    META_ACCESS_TOKEN_OSAKA: str = ""

    # Google Ads offline conversion — Data Manager API (Ads API path blocked 2026-06-15).
    # Reuses GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (same OAuth app), but NOT
    # GOOGLE_REFRESH_TOKEN: that token is scoped for Sheets/Gmail and Data Manager
    # needs its own scope (auth/datamanager). Mint a second refresh token for it.
    # Until GOOGLE_DATAMANAGER_REFRESH_TOKEN is set, the fan-out skips Google Ads.
    GOOGLE_DATAMANAGER_REFRESH_TOKEN: str = ""
    # Optional dedicated OAuth client. Set these when Data Manager uses its own
    # client (recommended — revoking consent on one client then can't take down
    # the Sheets/Gmail token). Empty falls back to the shared GOOGLE_CLIENT_*.
    GOOGLE_DATAMANAGER_CLIENT_ID: str = ""
    GOOGLE_DATAMANAGER_CLIENT_SECRET: str = ""
    # Unused by Data Manager (no developer-token header); kept so existing envs
    # don't break and in case the Ads API reporting path needs it later.
    GOOGLE_DEVELOPER_TOKEN: str = ""
    GOOGLE_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_CUSTOMER_ID_1948: str = ""
    GOOGLE_ADS_CONVERSION_SINGLE_1948: str = ""   # email-only OR phone-only
    GOOGLE_ADS_CONVERSION_BOTH_1948: str = ""     # both email + phone
    GOOGLE_ADS_CUSTOMER_ID_SAIGON: str = ""
    GOOGLE_ADS_CONVERSION_SINGLE_SAIGON: str = ""
    GOOGLE_ADS_CONVERSION_BOTH_SAIGON: str = ""
    GOOGLE_ADS_CUSTOMER_ID_TAIPEI: str = ""
    GOOGLE_ADS_CONVERSION_SINGLE_TAIPEI: str = ""
    GOOGLE_ADS_CONVERSION_BOTH_TAIPEI: str = ""
    GOOGLE_ADS_CUSTOMER_ID_OANI: str = ""
    GOOGLE_ADS_CONVERSION_SINGLE_OANI: str = ""   # email-only
    GOOGLE_ADS_CONVERSION_PHONE_OANI: str = ""    # phone-only (different action for Oani)
    GOOGLE_ADS_CONVERSION_BOTH_OANI: str = ""
    GOOGLE_ADS_CUSTOMER_ID_OSAKA: str = ""
    GOOGLE_ADS_CONVERSION_SINGLE_OSAKA: str = ""
    GOOGLE_ADS_CONVERSION_BOTH_OSAKA: str = ""

    # GA4 Data API v1beta — Purchase Conversion Rate KPI (Paid Ads).
    # Service account JSON key, pasted whole. The service account email needs
    # the Viewer role on every property below; without it runReport 403s and
    # the KPI renders blank.
    #
    # Set GA4_SERVICE_ACCOUNT_JSON_B64, not the raw variant: the key file's
    # private_key contains literal \n, and Zeabur's env-var text box does not
    # preserve them — the value arrives corrupted and fails to parse at
    # character zero. Produce it with:
    #     base64 -w 0 your-service-account.json
    # GA4_SERVICE_ACCOUNT_JSON (raw JSON) stays supported as a fallback for
    # environments that handle newlines properly, e.g. a local .env file. The
    # B64 variant wins when both are set; either one also accepts the other's
    # format, so a mistake here degrades to working rather than to blank.
    GA4_SERVICE_ACCOUNT_JSON_B64: str = ""
    GA4_SERVICE_ACCOUNT_JSON: str = ""
    # Property IDs are not secrets, so they ship as defaults and stay
    # overridable per environment. The query layer never sees them directly —
    # it reads ga4_property_map.
    GA4_PROPERTY_ID_SAIGON: str = "284939713"
    GA4_PROPERTY_ID_1948: str = "285135676"
    GA4_PROPERTY_ID_TAIPEI: str = "295612616"
    GA4_PROPERTY_ID_OSAKA: str = "482876806"
    # Oani's tag used to fire on the 1948 and Osaka websites too, so this
    # property measured three branches. Removed from both GTM containers on
    # 2026-08-09 and verified at the container source. GA4 does not clean
    # history, so months up to and including August 2026 are still three
    # branches — purchase_cvr gates Oani to 2026-09 via starts_by_branch.
    GA4_PROPERTY_ID_OANI: str = "514380737"

    # PageSpeed Insights API — Avg Website Load Speed KPI (Paid Ads).
    # Free without a key at low volume (5 branches/month is well under the
    # keyless rate limit), but an API key lifts the quota if that ever
    # changes. Get one at https://developers.google.com/speed/docs/insights/v5/get-started
    PAGESPEED_API_KEY: str = ""
    # Branch URLs are not secrets, so they ship as defaults and stay
    # overridable per environment. The query layer never sees them directly —
    # it reads pagespeed_url_map. Source: docs/specs/integrations.md branch
    # site map (staymeander.com/<branch>/en).
    PAGESPEED_URL_SAIGON: str = "https://staymeander.com/meandersaigon/en"
    PAGESPEED_URL_1948: str = "https://staymeander.com/meander1948/en"
    PAGESPEED_URL_TAIPEI: str = "https://staymeander.com/meandertaipei/en"
    PAGESPEED_URL_OSAKA: str = "https://staymeander.com/meanderosaka/en"
    PAGESPEED_URL_OANI: str = "https://staymeander.com/oani/en"

    # TikTok Events API — Saigon only
    TIKTOK_ACCESS_TOKEN_SAIGON: str = ""
    TIKTOK_EVENT_SOURCE_ID_SAIGON: str = ""

    # KOL Media Engine
    KOL_ENGINE_URL: str = "https://kol-media-engine.zeabur.app"
    KOL_ENGINE_ORG_ID: str = "7c7b450e-ffa2-42fb-8742-f28916e811d8"
    KOL_SYNC_API_KEY: str = ""               # X-Sync-API-Key header for /api/sync/kol-data
    # Public targets API — Bearer-auth endpoint exposing monthly targets
    # vs actuals (Invited Proactive / Collaborated / Posted). Different
    # auth scheme + different endpoint than KOL_SYNC_API_KEY above.
    KOL_TARGETS_ORG_SLUG: str = "meander"
    KOL_PUBLIC_API_KEY: str = ""
    # Public revenue API — Bearer-auth endpoint exposing monthly KOL
    # bookings/revenue, de-duped against Ads Platform attribution from
    # 2026-05-01 onwards. Distinct secret from KOL_PUBLIC_API_KEY because
    # the two endpoints rotate independently.
    KOL_REVENUE_API_SECRET: str = ""
    HID_API_SECRET: str = ""

    # Lark Base — Designer task tracking
    LARK_APP_ID: str = ""
    LARK_APP_SECRET: str = ""
    LARK_BASE_APP_TOKEN: str = ""
    LARK_TASKS_TABLE_ID: str = ""
    # Tenant domain used to build deep links to individual Lark records.
    # Copy it from the address bar when a Base record is open.
    LARK_WORKSPACE_DOMAIN: str = "www.larksuite.com"
    GHL_BASE_URL: str = "https://services.leadconnectorhq.com"
    # Legacy single-location (kept for backward compat)
    GHL_LOCATION_ID: str = ""
    GHL_API_KEY: str = ""

    @property
    def ghl_locations(self) -> list:
        """Return list of configured GHL locations [{name, location_id, api_key}]."""
        locations = []
        pairs = [
            ("Saigon", self.GHL_LOCATION_ID_SAIGON, self.GHL_API_KEY_SAIGON),
            ("1948", self.GHL_LOCATION_ID_1948, self.GHL_API_KEY_1948),
            ("Taipei", self.GHL_LOCATION_ID_TAIPEI, self.GHL_API_KEY_TAIPEI),
            ("Oani", self.GHL_LOCATION_ID_OANI, self.GHL_API_KEY_OANI),
            ("Osaka", self.GHL_LOCATION_ID_OSAKA, self.GHL_API_KEY_OSAKA),
        ]
        for name, loc_id, api_key in pairs:
            if loc_id and api_key:
                locations.append({"name": name, "location_id": loc_id, "api_key": api_key})
        # Fallback to legacy single-location config
        if not locations and self.GHL_LOCATION_ID and self.GHL_API_KEY:
            locations.append({"name": "Saigon", "location_id": self.GHL_LOCATION_ID, "api_key": self.GHL_API_KEY})
        return locations

    # Per-property Cloudbeds keys (loaded from .env CB_API_KEY_* and CB_PROPERTY_ID_*)
    CB_API_KEY_TAIPEI: str = ""
    CB_PROPERTY_ID_TAIPEI: str = ""
    CB_API_KEY_SAIGON: str = ""
    CB_PROPERTY_ID_SAIGON: str = ""
    CB_API_KEY_1948: str = ""
    CB_PROPERTY_ID_1948: str = ""
    CB_API_KEY_OANI: str = ""
    CB_PROPERTY_ID_OANI: str = ""
    CB_API_KEY_OSAKA: str = ""
    CB_PROPERTY_ID_OSAKA: str = ""

    @property
    def cloudbeds_properties(self) -> List[dict]:
        try:
            return json.loads(self.CLOUDBEDS_PROPERTY_IDS)
        except (json.JSONDecodeError, ValueError):
            return []

    @property
    def property_api_key_map(self) -> Dict[str, str]:
        """Map property_id (str) → api_key for per-property auth."""
        result: Dict[str, str] = {}
        pairs = [
            (self.CB_PROPERTY_ID_TAIPEI, self.CB_API_KEY_TAIPEI),
            (self.CB_PROPERTY_ID_SAIGON, self.CB_API_KEY_SAIGON),
            (self.CB_PROPERTY_ID_1948, self.CB_API_KEY_1948),
            (self.CB_PROPERTY_ID_OANI, self.CB_API_KEY_OANI),
            (self.CB_PROPERTY_ID_OSAKA, self.CB_API_KEY_OSAKA),
        ]
        for pid, key in pairs:
            if pid and key:
                result[str(pid)] = key
        return result

    @property
    def cloudbeds_property_to_branch(self) -> Dict[str, str]:
        """Map Cloudbeds property_id → branch slug (1948/saigon/taipei/oani/osaka)."""
        return {
            str(self.CB_PROPERTY_ID_1948): "1948",
            str(self.CB_PROPERTY_ID_SAIGON): "saigon",
            str(self.CB_PROPERTY_ID_TAIPEI): "taipei",
            str(self.CB_PROPERTY_ID_OANI): "oani",
            str(self.CB_PROPERTY_ID_OSAKA): "osaka",
        }

    def get_webhook_config_for_branch(self, branch: str) -> dict:
        """Return all webhook-related config for a branch slug."""
        b = branch.lower()
        ghl_loc = {
            "1948":   (self.GHL_LOCATION_ID_1948,   self.GHL_API_KEY_1948),
            "saigon": (self.GHL_LOCATION_ID_SAIGON, self.GHL_API_KEY_SAIGON),
            "taipei": (self.GHL_LOCATION_ID_TAIPEI, self.GHL_API_KEY_TAIPEI),
            "oani":   (self.GHL_LOCATION_ID_OANI,   self.GHL_API_KEY_OANI),
            "osaka":  (self.GHL_LOCATION_ID_OSAKA,  self.GHL_API_KEY_OSAKA),
        }.get(b, ("", ""))

        meta = {
            "1948":   (self.META_PIXEL_ID_1948,   self.META_ACCESS_TOKEN_1948),
            "saigon": (self.META_PIXEL_ID_SAIGON, self.META_ACCESS_TOKEN_SAIGON),
            "taipei": (self.META_PIXEL_ID_TAIPEI, self.META_ACCESS_TOKEN_TAIPEI),
            "oani":   (self.META_PIXEL_ID_OANI,   self.META_ACCESS_TOKEN_OANI),
            "osaka":  (self.META_PIXEL_ID_OSAKA,  self.META_ACCESS_TOKEN_OSAKA),
        }.get(b, ("", ""))

        gads_customer = {
            "1948":   self.GOOGLE_ADS_CUSTOMER_ID_1948,
            "saigon": self.GOOGLE_ADS_CUSTOMER_ID_SAIGON,
            "taipei": self.GOOGLE_ADS_CUSTOMER_ID_TAIPEI,
            "oani":   self.GOOGLE_ADS_CUSTOMER_ID_OANI,
            "osaka":  self.GOOGLE_ADS_CUSTOMER_ID_OSAKA,
        }.get(b, "")

        gads_single = {
            "1948":   self.GOOGLE_ADS_CONVERSION_SINGLE_1948,
            "saigon": self.GOOGLE_ADS_CONVERSION_SINGLE_SAIGON,
            "taipei": self.GOOGLE_ADS_CONVERSION_SINGLE_TAIPEI,
            "oani":   self.GOOGLE_ADS_CONVERSION_SINGLE_OANI,
            "osaka":  self.GOOGLE_ADS_CONVERSION_SINGLE_OSAKA,
        }.get(b, "")

        # Oani has a separate phone-only conversion action; all others use the same as single
        gads_phone = {
            "oani": self.GOOGLE_ADS_CONVERSION_PHONE_OANI,
        }.get(b, "")

        gads_both = {
            "1948":   self.GOOGLE_ADS_CONVERSION_BOTH_1948,
            "saigon": self.GOOGLE_ADS_CONVERSION_BOTH_SAIGON,
            "taipei": self.GOOGLE_ADS_CONVERSION_BOTH_TAIPEI,
            "oani":   self.GOOGLE_ADS_CONVERSION_BOTH_OANI,
            "osaka":  self.GOOGLE_ADS_CONVERSION_BOTH_OSAKA,
        }.get(b, "")

        # Per-branch timezone and currency (mirrors Make.com blueprint formulas)
        # tz_offset_hours: UTC offset of the branch local time
        # event_time_extra_offset: hours subtracted on top (Make's addHours value)
        # phone_country_code: assumed country when guestPhone has no "+" prefix,
        #   used to build E.164 before hashing for Data Manager
        branch_meta = {
            "1948":   {"currency": "TWD", "tz_offset_hours": 8, "event_time_extra_offset": 1, "phone_country_code": "886"},
            "taipei": {"currency": "TWD", "tz_offset_hours": 8, "event_time_extra_offset": 1, "phone_country_code": "886"},
            "oani":   {"currency": "TWD", "tz_offset_hours": 8, "event_time_extra_offset": 1, "phone_country_code": "886"},
            "osaka":  {"currency": "JPY", "tz_offset_hours": 9, "event_time_extra_offset": 2, "phone_country_code": "81"},
            "saigon": {"currency": "VND", "tz_offset_hours": 7, "event_time_extra_offset": 0, "phone_country_code": "84"},
        }.get(b, {"currency": "TWD", "tz_offset_hours": 8, "event_time_extra_offset": 1, "phone_country_code": "886"})

        return {
            "ghl_location_id": ghl_loc[0],
            "ghl_api_key": ghl_loc[1],
            "meta_pixel_id": meta[0],
            "meta_access_token": meta[1],
            "google_ads_customer_id": gads_customer,
            "google_ads_conversion_single": gads_single,
            "google_ads_conversion_phone": gads_phone,
            "google_ads_conversion_both": gads_both,
            "google_ads_refresh_token": self.GOOGLE_DATAMANAGER_REFRESH_TOKEN,
            "currency": branch_meta["currency"],
            "tz_offset_hours": branch_meta["tz_offset_hours"],
            "event_time_extra_offset": branch_meta["event_time_extra_offset"],
            "phone_country_code": branch_meta["phone_country_code"],
            # TikTok — Saigon only
            "tiktok_access_token": self.TIKTOK_ACCESS_TOKEN_SAIGON if b == "saigon" else "",
            "tiktok_event_source_id": self.TIKTOK_EVENT_SOURCE_ID_SAIGON if b == "saigon" else "",
        }

    def get_api_key_for_property(self, property_id: str) -> Optional[str]:
        return self.property_api_key_map.get(str(property_id)) or (
            self.CLOUDBEDS_API_KEY if self.CLOUDBEDS_API_KEY != "placeholder_key" else None
        )

    @property
    def ga4_property_map(self) -> Dict[str, str]:
        """branch_key → GA4 property ID, for branches that have a usable one.

        A branch with no ID configured is simply absent — that is how Oani stays
        blank while its tagging is being fixed.
        """
        return {
            key: value
            for key, value in {
                "saigon": self.GA4_PROPERTY_ID_SAIGON,
                "1948":   self.GA4_PROPERTY_ID_1948,
                "taipei": self.GA4_PROPERTY_ID_TAIPEI,
                "osaka":  self.GA4_PROPERTY_ID_OSAKA,
                "oani":   self.GA4_PROPERTY_ID_OANI,
            }.items()
            if str(value or "").strip()
        }

    @property
    def pagespeed_url_map(self) -> Dict[str, str]:
        """branch_key → website URL to test, for branches that have one configured."""
        return {
            key: value
            for key, value in {
                "saigon": self.PAGESPEED_URL_SAIGON,
                "1948":   self.PAGESPEED_URL_1948,
                "taipei": self.PAGESPEED_URL_TAIPEI,
                "osaka":  self.PAGESPEED_URL_OSAKA,
                "oani":   self.PAGESPEED_URL_OANI,
            }.items()
            if str(value or "").strip()
        }

    @property
    def datamanager_client_id(self) -> str:
        return self.GOOGLE_DATAMANAGER_CLIENT_ID or self.GOOGLE_CLIENT_ID

    @property
    def datamanager_client_secret(self) -> str:
        return self.GOOGLE_DATAMANAGER_CLIENT_SECRET or self.GOOGLE_CLIENT_SECRET

    @property
    def email_recipients_list(self) -> List[str]:
        return [e.strip() for e in self.EMAIL_RECIPIENTS.split(",") if e.strip()]


settings = Settings()
