"""
Country name ↔ ISO-3166 alpha-2 mapping.

Lifted out of `cloudbeds.py` so callers that only need the lookup do not pull
in the whole Cloudbeds client. `biweekly_render` is the reason: it renders
flag emoji from a country, and importing the ingestion module for a dict
would drag HTTP clients into a pure-formatting path.

Note what `reservations.guest_country_code` actually holds: `map_country_code`
normalises everything to a canonical **display name**, so that column stores
"Taiwan", not "TW". Anything reading it for a real ISO code has to come back
through `iso_code_for`.
"""
from typing import Optional

_ISO_TO_NAME: dict[str, str] = {
    "US": "United States", "GB": "United Kingdom",
    "AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
    "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "CH": "Switzerland",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PT": "Portugal", "IE": "Ireland", "PL": "Poland", "CZ": "Czech Republic",
    "RO": "Romania", "JP": "Japan", "KR": "South Korea", "CN": "China",
    "TW": "Taiwan", "HK": "Hong Kong", "MO": "Macau",
    "SG": "Singapore", "MY": "Malaysia", "TH": "Thailand", "VN": "Vietnam",
    "PH": "Philippines", "ID": "Indonesia", "IN": "India",
    "RU": "Russia", "BR": "Brazil", "MX": "Mexico",
    "ZA": "South Africa", "SA": "Saudi Arabia", "AE": "United Arab Emirates",
    "TR": "Turkey", "KH": "Cambodia", "MM": "Myanmar", "LK": "Sri Lanka",
    "CY": "Cyprus", "UY": "Uruguay", "PE": "Peru", "AR": "Argentina",
    "CO": "Colombia", "CL": "Chile", "EG": "Egypt", "IL": "Israel",
    "QA": "Qatar", "LA": "Laos", "NP": "Nepal", "BD": "Bangladesh",
    "GR": "Greece", "HR": "Croatia", "HU": "Hungary", "BG": "Bulgaria",
    "RS": "Serbia", "SK": "Slovakia", "SI": "Slovenia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "IS": "Iceland",
    "NG": "Nigeria", "KE": "Kenya", "GH": "Ghana",
    "PK": "Pakistan", "LB": "Lebanon", "JO": "Jordan", "KW": "Kuwait",
    "BH": "Bahrain", "OM": "Oman",
}

# Full name aliases → canonical name
_NAME_ALIASES: dict[str, str] = {
    "United States of America": "United States",
    "Republic of Korea": "South Korea",
    "Korea, Republic of": "South Korea",
    "Korea": "South Korea",
    "Viet Nam": "Vietnam",
    "Czechia": "Czech Republic",
    "Russian Federation": "Russia",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "Hong Kong SAR China": "Hong Kong",
    "Macao": "Macau",
    "Macau SAR China": "Macau",
    "Taiwan, Province of China": "Taiwan",
}


# Reverse direction, built once. Names are unique in _ISO_TO_NAME, so this is
# lossless; aliases fold in so "Viet Nam" and "Czechia" resolve too.
_NAME_TO_ISO: dict[str, str] = {name: code for code, name in _ISO_TO_NAME.items()}


def iso_code_for(value: Optional[str]) -> Optional[str]:
    """Best-effort ISO alpha-2 for a country name or code.

    Accepts what the database actually stores (a display name), a raw ISO
    code, or an alias. Returns None when nothing matches, so callers can
    show a neutral placeholder instead of a wrong flag.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 2 and s.isalpha() and s.upper() in _ISO_TO_NAME:
        return s.upper()
    canonical = _NAME_ALIASES.get(s, s)
    return _NAME_TO_ISO.get(canonical)
