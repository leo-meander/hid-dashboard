# HiD Integrations Spec

## 1. Cloudbeds API

### Authentication
- API Key via header: `X-Api-Key: {CLOUDBEDS_API_KEY}`
- Base URL: `https://api.cloudbeds.com/api/v1.1`

### Key Endpoints Used

#### GET /reservations
Pull all reservations for a property.

**Params:**
```
propertyID={property_id}
pageNumber={n}
pageSize=100
checkIn[gte]={date}     # date range filter
checkIn[lte]={date}
modifiedAt[gte]={date}  # for incremental sync
```

**Response fields we use:**
```
reservationID         → cloudbeds_reservation_id
guestCountry          → guest_country (raw), guest_country_code (mapped)
roomTypeName          → room_type (raw), room_type_category (derived)
sourceID / sourceName → source (raw), source_category (derived)
startDate             → check_in_date
endDate               → check_out_date
nights                → nights
adults                → adults
total                 → grand_total_native
status                → status
dateCreated           → reservation_date
```

**Pagination:** Loop through pages until `total` is reached.

### Sync Strategy
- **Nightly full sync** (2am Vietnam): pull last 90 days of reservations by `modifiedAt`
- **On-demand sync**: POST /api/sync/cloudbeds (for manual trigger)
- **Deduplication**: upsert on `cloudbeds_reservation_id` — no duplicates

### Ingestion Mapping (services/cloudbeds.py)

```python
COUNTRY_MAP = {
    "United States of America": "USA",
    "United Kingdom": "UK",
    "Unknown": "Others",
    # all others pass through as-is
}

def map_room_type_category(room_type: str) -> str:
    if "dorm" in room_type.lower():
        return "Dorm"
    return "Room"

def map_source_category(source: str) -> str:
    direct_keywords = ["website", "booking engine", "blogger", "direct"]
    if any(kw in source.lower() for kw in direct_keywords):
        return "Direct"
    return "OTA"

OTA_CANONICAL = {
    "booking.com": "Booking.com",
    "hostelworld": "Hostelworld",
    "agoda": "Agoda",
    "ctrip": "Ctrip",
    "trip.com": "Ctrip",
    "expedia": "Expedia",
}
```

---

## 2. Exchange Rate API

### Provider
Free tier: https://www.exchangerate-api.com  
Endpoint: `GET https://v6.exchangerate-api.com/v6/{API_KEY}/latest/{base}`

### Usage
- Fetch rates daily (cached in memory + DB)
- Base currency: always fetch from branch native currency → VND
- Currencies needed: TWD→VND, JPY→VND, USD→VND, VND→VND (1:1)

### Fallback
If API call fails → use last cached rate from DB, log warning.
Never block data ingestion due to currency API failure.

---

## 3. SendGrid (Email)

### Config
```python
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

sg = SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
```

### Weekly Email Schedule
- Every Monday at 7:00am `Asia/Ho_Chi_Minh`
- Recipients: `EMAIL_RECIPIENTS` env var (comma-separated)
- From: `EMAIL_FROM` env var

### Email Content
See `services/email_service.py` for template.
Sections: KPI snapshot, Hot countries top 3, Winning ad angles, KOL opportunities, Pending approvals.

---

## 4. Google Analytics 4 Data API

Powers one KPI: **Purchase Conversion Rate** on the Team KPI page → `Mason · Paid Ads`.
Service: `services/ga4_service.py`. Yearly assembly: `team_kpi_service.get_purchase_cvr_actuals_yearly`.

### Authentication
- Google service account, JSON key in `GA4_SERVICE_ACCOUNT_JSON` (one line).
- The service account email needs the **Viewer** role on each property; without it
  `runReport` returns 403 and the KPI renders blank. No end-user OAuth.
- We sign a JWT with the key and exchange it at `oauth2.googleapis.com/token`
  (`jwt-bearer` grant, scope `analytics.readonly`), cached until a minute before expiry.

### Request
`POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport`

```json
{
  "dateRanges": [{ "startDate": "2026-08-01", "endDate": "2026-08-31" }],
  "metrics": [
    { "name": "userKeyEventRate:purchase" },
    { "name": "totalUsers" },
    { "name": "activeUsers" },
    { "name": "keyEvents:purchase" }
  ]
}
```

Only the first metric is displayed (`"0.0163"` → `1.63%`). The other three let the
pipeline self-verify — `round(rate × denominator)` should land on the purchasing-user
count, and whichever denominator does is the real one (logged per reading).

### Rules this integration cannot break
- **No `dimensions`.** The KPI is one property-level number. GA4 computes its own
  Total independently of the rows — on Oani the ten visible channel rows summed to
  10,152 users against a Total of 10,133.
- **No `dimensionFilter`, and never on `hostName`.** Purchases fire on
  `hotels.cloudbeds.com`, not the branch's own site; `hostName` is event-scoped, so
  filtering on it excludes the purchase events and drives the rate to zero. The
  metric is only readable at whole-property scope.
- **One request per month, over that month's own dates.** The metric counts unique
  users, which de-duplicate across time — a month assembled from daily rows
  double-counts returning visitors, and the error grows with the window.
- **Year-to-date is its own Jan-1 → today request**, never a sum of the monthly cells.
- `metadata.subjectToThresholding` is read per response and surfaces as a `*` marker;
  a thresholded month is not a confident number.

### Property mapping
`settings.ga4_property_map` — Saigon `284939713`, 1948 `285135676`, Taipei `295612616`,
Osaka `482876806`. Oani (`514380737`) is deliberately unmapped: its tag also fires on
the 1948 and Osaka websites, so the property measures three branches. Its tab shows
`—` with the reason. The 1948 and Osaka properties themselves are clean.

The **All** tab shows `—` too: five properties are five user namespaces, and a user
cannot be de-duplicated across them, so no correct group-wide rate exists.

### Caching
Read-through, 1 hour (`_GA4_CVR_TTL`). GA4 is 24–48h from final on the current day,
so closed months are simply re-queried rather than frozen — `runReport` is cheap and
a re-query cannot go stale.

### Debugging
`GET /api/team-kpi/debug/ga4-purchase-rate?year=&month=&branch=` returns the raw
reading per branch plus both candidate denominators.

---

## 5. Future Integrations (Phase 7+)

### Meta Ads API 🔮
- Graph API v19+
- Endpoint: `/act_{account_id}/insights`
- Fields: campaign_name, adset_name, ad_name, spend, impressions, clicks, actions
- Auth: User Access Token (long-lived)

### Google Analytics 4 — channel breakdown 🔮
- Add `"dimensions": [{ "name": "firstUserPrimaryChannelGroup" }]` to the §4 request.
  That is the first-user / acquisition-scoped dimension the User acquisition report
  uses — **not** `sessionDefaultChannelGroup`, which is session-scoped last-click and
  returns materially different numbers.
- A total still needs its own dimensionless request; GA4's Total is not the sum of rows.
- Channel-level attribution here is directional only: `hotels.cloudbeds.com` leaks
  through the referral exclusion list, so a booking hop can start a fresh Referral
  session. Settlement-grade paid attribution stays with the ads platform.

### TikTok Ads API 🔮
- TikTok Marketing API
- Similar structure to Meta Ads
