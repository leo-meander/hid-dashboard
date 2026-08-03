"""
Guest email usability shared by the conversion-upload services.

OTA channels hand the property an alias address instead of the guest's real one:
Ctrip books arrive as `xxx@guest.ctrip.com`, Trip.com as `xxx@guest.trip.com`,
Booking.com as `xxx@guest.booking.com`. The address exists only inside that OTA's
message relay, so no ad platform has ever seen it attached to an account and the
hash of it can never match a real person.

Sending them anyway is not free:
  - Google Ads routes the reservation to the "both identifiers" conversion action
    when the phone is the only usable identifier, so the split between the
    single- and both-identifier actions stops meaning anything.
  - Meta and TikTok count the unmatchable hash against reported match quality.

So the phone number is the identifier to trust for OTA bookings. It may itself be
a relay number on some channels — a known and accepted risk — but unlike the
email it is at least sometimes the guest's own.

The domain list is deliberately allowed to contain entries we have not observed
in production yet. A domain that never shows up costs nothing; a rule broad
enough to catch real guest addresses would silently drop good conversions, which
is why this is an exact-domain list and not a `guest.*` pattern.
"""
from typing import Optional

# Confirmed from the Saigon webhook log; the rest are the documented relay
# domains of the other OTAs we sell through. Add to this list whenever a new
# alias domain shows up in the EMAIL column of the Webhook Monitor.
OTA_ALIAS_DOMAINS = frozenset({
    "guest.ctrip.com",              # confirmed — Saigon, 2026-07-30
    "guest.trip.com",               # confirmed — Saigon, 2026-07-30
    "guest.booking.com",
    "guest.airbnb.com",
    "stay.airbnb.com",
    "m.expediapartnercentral.com",
    "guest.expedia.com",
    "guest.agoda.com",
    "agoda-messaging.com",
    "message.agoda.com",
    "guest.hotels.com",
    "guest.klook.com",
    "guest.traveloka.com",
})

# Cloudbeds writes a literal "N/A" when a channel sends no address at all. Kept
# as a substring test rather than a word-boundary regex, which would also reject
# real addresses like na@company.com.
_PLACEHOLDER = "n/a"


def usable_email(raw: Optional[str]) -> Optional[str]:
    """
    Return the guest email in the form it should be hashed in, or None.

    None means "do not send an email identifier for this reservation" — the
    address is missing, a Cloudbeds placeholder, malformed, or an OTA alias that
    cannot match. Callers should fall back to the phone number.
    """
    if not raw:
        return None

    email = str(raw).strip().lower()
    if not email or _PLACEHOLDER in email:
        return None
    if email.count("@") != 1:
        return None

    _, domain = email.split("@", 1)
    if not domain or "." not in domain or domain in OTA_ALIAS_DOMAINS:
        return None

    return email
