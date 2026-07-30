"""
Phone normalization shared by the conversion-upload services.

Google Ads (Data Manager), Meta CAPI and TikTok Events all hash the guest phone
number before sending it, so the number has to be in a canonical form first or
the hash matches nothing on the other side. They differ only in whether the
canonical form keeps the leading "+".

Cloudbeds does not enforce a phone format, so numbers arrive as "+886 912 345
678", "0912-345-678", "00886912345678" or bare local digits. Only the first form
is unambiguous; for the rest we assume the number belongs to the branch's
country, which is the best signal available at ingestion time. A guest who typed
a foreign local number without a country code normalizes to the wrong country and
simply fails to match — the same outcome as sending it unnormalized, so this is
never worse than not normalizing.
"""
import re
from typing import Optional


def normalize_e164(raw: Optional[str], country_calling_code: str) -> Optional[str]:
    """
    Normalize a raw phone string to E.164 ("+886912345678").

    Returns None when the input has too few digits to be a real number, or when
    it has no country code and none can be assumed.
    """
    if not raw:
        return None

    raw = str(raw).strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 6:
        return None

    cc = re.sub(r"\D", "", country_calling_code or "")

    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if not cc:
        return None
    if digits.startswith("0"):
        # National trunk prefix — drop it before prepending the country code.
        return f"+{cc}{digits.lstrip('0')}"
    if digits.startswith(cc):
        return f"+{digits}"
    return f"+{cc}{digits}"


def normalize_e164_digits(raw: Optional[str], country_calling_code: str) -> Optional[str]:
    """
    Same as normalize_e164 but without the leading "+" ("886912345678").

    Meta and TikTok both document digits-with-country-code, and the previous
    implementations stripped "+" too, so this keeps their wire format unchanged
    while fixing numbers that arrived without a country code.
    """
    e164 = normalize_e164(raw, country_calling_code)
    return e164[1:] if e164 else None
