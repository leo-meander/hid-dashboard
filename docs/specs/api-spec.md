# HiD API Specification

## Base URL
- Dev: `http://localhost:8000`
- Prod: `https://<zeabur-domain>` (update after Zeabur deploy)

## Standard Response Format
```json
{
  "success": true,
  "data": <any>,
  "error": null,
  "timestamp": "2026-01-13T07:00:00Z"
}
```

---

## System

### GET /health
Returns service status.
```json
{ "success": true, "data": { "status": "ok", "version": "1.0.0" }, "error": null, "timestamp": "..." }
```

### POST /api/sync/cloudbeds
Manually trigger Cloudbeds data pull for all branches.
```json
// Response
{ "success": true, "data": { "reservations_synced": 142, "branches_processed": 5 }, ... }
```

### POST /api/metrics/recompute
Recompute daily_metrics cache for a date range.
```json
// Body
{ "date_from": "2026-01-01", "date_to": "2026-01-31", "branch_id": "uuid|null" }
// Response
{ "success": true, "data": { "days_recomputed": 31 }, ... }
```

---

## KPI Targets

### GET /api/kpi/targets
Params: `branch_id` (optional), `year` (optional)

### POST /api/kpi/targets
```json
// Body
{
  "branch_id": "uuid",
  "year": 2026,
  "month": 3,
  "target_revenue_native": 3200000,
  "predicted_occ_pct": 0.78
}
```

### PATCH /api/kpi/targets/{id}
Update predicted_occ_pct or target_revenue.

### GET /api/kpi/summary
Returns all branches KPI achievement: actual vs target, both forecast methods.
```json
// Response data
[
  {
    "branch_id": "uuid",
    "branch_name": "MEANDER Saigon",
    "year": 2026, "month": 3,
    "target_revenue_vnd": 3200000000,
    "actual_revenue_vnd": 1800000000,
    "achievement_pct": 0.5625,
    "run_rate_forecast_vnd": 2700000000,
    "occ_based_forecast_vnd": 2900000000
  }
]
```

---

## Performance Metrics

### GET /api/metrics/daily
OCC, Revenue, ADR, RevPAR per branch per day (reads from daily_metrics cache).
Params: `branch_id` (required), `date_from`, `date_to` (default: last 30 days)
```json
// Response data
[
  {
    "date": "2026-03-01",
    "rooms_sold": 45, "dorms_sold": 12, "total_sold": 57,
    "occ_pct": 0.826, "room_occ_pct": 0.731, "dorm_occ_pct": null,
    "revenue_native": 195521, "revenue_vnd": 152000000,
    "adr_native": 3430, "revpar_native": 2833,
    "cancellations": 2, "cancellation_pct": 0.034,
    "events": [{ "event_name": "...", "is_key_event": true }]
  }
]
```

### GET /api/metrics/weekly
Weekly rollup with OTA mix and cancellation trend.
Params: `branch_id` (required), `weeks_back` (default: 12)
```json
// Response data
[
  {
    "week_start": "2026-03-09",
    "revenue_native": 5522595,
    "revenue_vnd": 4300000000,
    "cancellation_pct": 0.051,
    "ota_mix": {
      "Booking.com": 0.164, "Agoda": 0.118, "Hostelworld": 0.094,
      "Ctrip": 0.136, "Expedia": 0.058, "Direct": 0.430
    },
    "conversion_pct": 0.0072,
    "impressions": 22551
  }
]
```

### GET /api/metrics/monthly
Monthly rollup with country breakdown.
Params: `branch_id` (required), `year` (default: current year)
```json
// Response data
[
  {
    "month": 1, "year": 2026,
    "total_sold": 1712, "occ_pct": 0.801,
    "revenue_native": 3603947, "adr_native": 2105, "revpar_native": 1741,
    "country_breakdown": [
      { "country": "Australia", "room_nights": 68 },
      { "country": "Taiwan", "room_nights": 49 }
    ]
  }
]
```

### GET /api/metrics/ota-mix
OTA % share of room nights per branch per month.
Params: `branch_id` (optional = all branches), `year`
```json
// Response data
{
  "1948": [
    { "month": "2026-01", "Agoda": 0.118, "Booking.com": 0.164, "Ctrip": 0.136, "Expedia": 0.058, "Hostelworld": 0.094, "Direct": 0.430 }
  ]
}
```

### GET /api/metrics/country-breakdown
Reservation room nights per country per month, multi-year.
Params: `branch_id` (required), `country` (optional filter)
```json
// Response data
{
  "Australia": {
    "2024": { "Jan": 0, "Feb": 0, ..., "Total": 448 },
    "2025": { "Jan": 54, "Feb": 33, ..., "Total": 522 },
    "2026": { "Jan": 68, "Feb": 45, "Total": 113 }
  }
}
```

### GET /api/metrics/country-yoy
Year-over-year room nights per country, all branches or filtered.
Params: `branch_id` (optional), `year_a`, `year_b`

### GET /api/metrics/summary
Cross-branch snapshot — current week vs prior week vs prior year average.

---

## Events

### GET /api/events
Params: `city` (optional), `date_from`, `date_to`, `is_key_event`

### POST /api/events
```json
{
  "branch_id": null,
  "city": "Taipei",
  "event_name": "Taiwan Innotech Expo",
  "event_date_from": "2026-10-16",
  "event_date_to": "2026-10-18",
  "estimated_attendance": 53000,
  "is_key_event": true
}
```

### PATCH /api/events/{id}
### DELETE /api/events/{id} (soft: is_active = false)

---

## Website Metrics

### GET /api/website-metrics
Params: `branch_id` (optional), `platform` (optional), `date_from`, `date_to`

### POST /api/website-metrics
```json
{
  "branch_id": null,
  "week_start_date": "2026-03-09",
  "platform": "Meta",
  "impressions": 22551,
  "clicks": 1631,
  "website_traffic": 131886,
  "add_to_cart": 11496,
  "conversions": 954,
  "conversion_pct": 0.00724
}
```

### PATCH /api/website-metrics/{id}

---

## Countries

### GET /api/countries/ranking
Returns country scores (Hot/Warm/Cold) for a branch.
Params: `branch_id` (required)

### GET /api/countries/{country_code}
Country detail: booking trend, revenue trend, activities, ads.
Params: `branch_id`

---

## Ads Performance

### GET /api/ads
Params: `branch_id`, `channel`, `date_from`, `date_to`, `angle_id`

### POST /api/ads (single record)
### POST /api/ads/bulk (CSV import)
### PATCH /api/ads/{id}

---

## KOL Records

### GET /api/kols
Params: `branch_id`, `paid_ads_eligible`, `expiring_soon` (30-day window)

### POST /api/kols
### PATCH /api/kols/{id}

### POST /api/kols/{id}/link-reservation
```json
{ "cloudbeds_reservation_id": "1034185313569" }
```

### GET /api/kols/insights
Returns KOL-to-Paid Ads opportunities (ROAS >= 2.0, >= 2 bookings attributed).

---

## Ad Angles

### GET /api/angles
Params: `branch_id`, `status` (WIN|TEST|LOSE)

### POST /api/angles
### PATCH /api/angles/{id}

---

## Reports

### GET /api/reports/weekly/preview
Returns full weekly email content as JSON (for frontend preview).

### POST /api/reports/weekly/send
Manually triggers email send to all recipients.

---

## Creative Ops (Phase 6)

### GET/POST/PATCH /api/keypoints
### GET/POST/PATCH /api/copies
### GET/POST/PATCH /api/materials
### GET/POST/PATCH /api/approvals
### GET /api/ad-names
