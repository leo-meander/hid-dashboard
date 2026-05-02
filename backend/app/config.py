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

    SENDGRID_API_KEY: str = ""
    EMAIL_FROM: str = ""
    EMAIL_RECIPIENTS: str = ""
    GMAIL_USER: str = ""
    GMAIL_APP_PASSWORD: str = ""
    APP_ENV: str = "development"
    SECRET_KEY: str = "changeme"
    FRONTEND_URL: str = ""

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

    # KOL Media Engine
    KOL_ENGINE_URL: str = "https://kol-media-engine.zeabur.app"
    KOL_ENGINE_ORG_ID: str = "7c7b450e-ffa2-42fb-8742-f28916e811d8"
    KOL_SYNC_API_KEY: str = ""
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

    def get_api_key_for_property(self, property_id: str) -> Optional[str]:
        return self.property_api_key_map.get(str(property_id)) or (
            self.CLOUDBEDS_API_KEY if self.CLOUDBEDS_API_KEY != "placeholder_key" else None
        )

    @property
    def email_recipients_list(self) -> List[str]:
        return [e.strip() for e in self.EMAIL_RECIPIENTS.split(",") if e.strip()]


settings = Settings()
