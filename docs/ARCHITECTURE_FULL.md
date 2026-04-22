# HiD — Hotel Intelligence Dashboard
# Complete Architecture Document

**Version:** 4.1.0
**Last Updated:** 2026-03-27
**Status:** Phase 4 (Creative Intelligence Library) — Active Development

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Project Structure](#2-project-structure)
3. [Tech Stack](#3-tech-stack)
4. [System Architecture Diagram](#4-system-architecture-diagram)
5. [Backend Architecture](#5-backend-architecture)
6. [Frontend Architecture](#6-frontend-architecture)
7. [Database Schema](#7-database-schema)
8. [API Layer](#8-api-layer)
9. [Scheduled Jobs](#9-scheduled-jobs)
10. [External Integrations](#10-external-integrations)
11. [Authentication & Authorization](#11-authentication--authorization)
12. [Metrics Engine Rules](#12-metrics-engine-rules)
13. [Creative Library System](#13-creative-library-system)
14. [Deployment & Infrastructure](#14-deployment--infrastructure)
15. [Data Flow Pipelines](#15-data-flow-pipelines)
16. [Coding Rules & Conventions](#16-coding-rules--conventions)
17. [Testing Strategy](#17-testing-strategy)
18. [Phase Roadmap](#18-phase-roadmap)

---

## 1. System Overview

HiD (Hotel Intelligence Dashboard) is an internal marketing BI dashboard for **MEANDER Group** — a 5-branch hotel/hostel group operating across Southeast Asia and East Asia. It consolidates reservation data from Cloudbeds PMS, advertising performance from Meta Ads and Google Ads, KOL (Key Opinion Leader) collaboration tracking, and CRM email marketing from GoHighLevel into a single source of truth.

### Branches

| Branch | City | Country | Currency | Timezone | UUID |
|--------|------|---------|----------|----------|------|
| MEANDER Taipei | Taipei | Taiwan | TWD | Asia/Taipei | `11111111-...-111101` |
| MEANDER Saigon | Ho Chi Minh City | Vietnam | VND | Asia/Ho_Chi_Minh | `11111111-...-111102` |
| MEANDER 1948 | Taipei | Taiwan | TWD | Asia/Taipei | `11111111-...-111103` |
| Oani | Taipei | Taiwan | TWD | Asia/Taipei | `11111111-...-111104` |
| MEANDER Osaka | Osaka | Japan | JPY | Asia/Tokyo | `11111111-...-111105` |

### Problem Solved
Replaces manual Excel workflows for 6 marketing team users. Previously, the team:
- Manually pulled Cloudbeds data into spreadsheets
- Calculated OCC/ADR/RevPAR by hand
- Tracked KOL performance in disconnected Google Sheets
- Had no centralized view of ad performance across branches

---

## 2. Project Structure

```
hid/
├── CLAUDE.md                          # Project rules for AI assistants
├── .claude/
│   └── rules/                         # Scoped rules loaded per context
│       ├── api-conventions.md         # API response format, error handling
│       ├── creative-library-rules.md  # Verdict model, combo constraints
│       ├── database-rules.md          # Schema conventions, monetary values
│       ├── deployment-rules.md        # Env vars, Docker, Railway, CORS
│       ├── metrics-rules.md           # OCC/ADR/RevPAR calculation logic
│       └── testing-rules.md           # Test coverage, mocking, fixtures
├── .env.example                       # Environment variable template
├── docs/
│   ├── ARCHITECTURE_FULL.md           # This file
│   ├── architecture.md                # Design decisions (condensed)
│   ├── lessons.md                     # Documented mistakes & fixes
│   └── specs/
│       ├── api-spec.md                # Full API endpoint reference
│       ├── data-model.md              # All 17 table DDL definitions
│       ├── frontend-spec.md           # Route map, components, charts
│       └── integrations.md            # Cloudbeds, SendGrid, exchange rate
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI app, router registration, CORS, SPA serving
│   │   ├── config.py                  # Pydantic BaseSettings (all env vars)
│   │   ├── database.py                # SQLAlchemy engine, session, Base
│   │   ├── scheduler.py               # APScheduler setup (8 scheduled jobs)
│   │   ├── models/                    # SQLAlchemy ORM models (22 files)
│   │   │   ├── __init__.py
│   │   │   ├── branch.py
│   │   │   ├── reservation.py
│   │   │   ├── reservation_daily.py
│   │   │   ├── daily_metrics.py
│   │   │   ├── kpi.py
│   │   │   ├── event.py
│   │   │   ├── website_metrics.py
│   │   │   ├── ads.py
│   │   │   ├── kol.py
│   │   │   ├── angle.py
│   │   │   ├── activity.py
│   │   │   ├── user.py
│   │   │   ├── creative.py
│   │   │   ├── creative_angle.py
│   │   │   ├── creative_copy.py
│   │   │   ├── creative_material.py
│   │   │   ├── ad_combo.py
│   │   │   ├── ad_analysis.py
│   │   │   ├── email_campaign_stats.py
│   │   │   ├── email_event.py
│   │   │   └── gov_visitor.py
│   │   ├── routers/                   # FastAPI route handlers (23 files)
│   │   │   ├── auth.py
│   │   │   ├── kpi.py
│   │   │   ├── sync.py               # Cloudbeds + CSV + Sheets sync (largest router)
│   │   │   ├── metrics.py
│   │   │   ├── events.py
│   │   │   ├── website_metrics.py
│   │   │   ├── countries.py
│   │   │   ├── branches.py
│   │   │   ├── marketing.py
│   │   │   ├── ads.py
│   │   │   ├── kol.py
│   │   │   ├── angles.py
│   │   │   ├── insights.py            # AI-powered insights (Anthropic Claude)
│   │   │   ├── report.py              # Weekly email report generation
│   │   │   ├── creative_angles.py
│   │   │   ├── creative_copies.py
│   │   │   ├── creative_materials.py
│   │   │   ├── combos.py             # Ad combo CRUD + verdict
│   │   │   ├── ad_analyzer.py        # AI ad analysis
│   │   │   ├── crm.py                # CRM dashboard (GHL integration)
│   │   │   ├── email_marketing.py    # Email marketing analytics
│   │   │   └── gov_visitor.py        # Government visitor data
│   │   └── services/                  # Business logic layer (19 files)
│   │       ├── cloudbeds.py           # Cloudbeds API client + sync logic
│   │       ├── metrics_engine.py      # OCC/ADR/RevPAR computation + cache
│   │       ├── kpi_engine.py          # KPI target vs actual calculation
│   │       ├── country_scorer.py      # Hot/Warm/Cold country scoring
│   │       ├── currency.py            # Exchange rate API + conversion
│   │       ├── email_service.py       # SendGrid weekly email
│   │       ├── email_stats.py         # Email campaign statistics
│   │       ├── meta_ads.py            # Meta (Facebook) Ads API client
│   │       ├── google_sheets_ads.py   # Google Ads via Sheets API
│   │       ├── ghl_email_sync.py      # GoHighLevel email sync
│   │       ├── angle_classifier.py    # WIN/TEST/LOSE angle classification
│   │       ├── verdict_sync.py        # Nightly combo verdict computation
│   │       ├── creative_sync.py       # Creative library sync
│   │       ├── id_generator.py        # Sequential ID generator (CPY/MAT/CMB/ANG)
│   │       ├── ingest_csv.py          # CSV import service
│   │       ├── csv_kol_sync.py        # KOL CSV sync
│   │       ├── sheets_kol.py          # KOL Google Sheets sync
│   │       ├── sheets_revenue.py      # Revenue data from Sheets
│   │       └── ad_analyzer_service.py # AI-powered ad analysis (Claude API)
│   ├── alembic/                       # Database migrations
│   │   ├── env.py
│   │   └── versions/                  # 21 migration files (001–021)
│   ├── tests/
│   └── requirements.txt
└── frontend/
    ├── package.json
    ├── index.html
    ├── tailwind.config.js
    ├── postcss.config.js
    └── src/
        ├── main.jsx                   # React entry point
        ├── App.jsx                    # Router + auth guard + layout
        ├── index.css                  # Tailwind imports
        ├── context/
        │   ├── AuthContext.jsx         # JWT auth state
        │   └── BranchContext.jsx       # Global branch selector state
        ├── constants/
        │   └── audiences.js           # Target audience constants
        ├── components/                # Shared UI components (8 files)
        │   ├── Sidebar.jsx
        │   ├── BranchSelector.jsx
        │   ├── KPICard.jsx
        │   ├── TrendChart.jsx
        │   ├── CountryBadge.jsx
        │   ├── OCCHeatmap.jsx
        │   ├── ComboCard.jsx
        │   └── VerdictBadge.jsx
        ├── pages/                     # Route pages (29 files)
        │   ├── Login.jsx
        │   ├── Home.jsx
        │   ├── Dashboard.jsx
        │   ├── KPI.jsx
        │   ├── KPITargets.jsx
        │   ├── Performance.jsx
        │   ├── PerformanceDaily.jsx
        │   ├── PerformanceWeekly.jsx
        │   ├── PerformanceMonthly.jsx
        │   ├── PerformanceOTA.jsx
        │   ├── Countries.jsx
        │   ├── CountryDetail.jsx
        │   ├── CountryIntel.jsx
        │   ├── Marketing.jsx
        │   ├── Ads.jsx
        │   ├── KOL.jsx
        │   ├── Angles.jsx
        │   ├── Insights.jsx
        │   ├── Report.jsx
        │   ├── AdCombos.jsx
        │   ├── AdAnalyzer.jsx
        │   ├── CreativeCopies.jsx
        │   ├── CreativeMaterials.jsx
        │   ├── CRMDashboard.jsx
        │   ├── EmailMarketing.jsx
        │   ├── GovVisitorData.jsx
        │   ├── Reservations.jsx
        │   ├── Settings.jsx
        │   └── Users.jsx
        └── api/                       # Axios API layer (8 files)
            ├── analyzer.js
            ├── angles.js
            ├── combos.js
            ├── copies.js
            ├── crm.js
            ├── emailMarketing.js
            ├── kol.js
            └── materials.js
```

---

## 3. Tech Stack

### Backend
| Component | Technology | Version |
|-----------|-----------|---------|
| Framework | FastAPI | >= 0.115.0 |
| ORM | SQLAlchemy | >= 2.0.36 |
| Migrations | Alembic | 1.13.1 |
| Scheduler | APScheduler | 3.10.4 |
| HTTP Client | httpx | >= 0.27.0 |
| Auth | PyJWT + bcrypt | >= 2.8.0 / >= 4.0.0 |
| Config | pydantic-settings | >= 2.6.0 |
| Email | SendGrid SDK | 6.11.0 |
| AI | Anthropic SDK | >= 0.25.0 |
| Excel | openpyxl | >= 3.1.0 |
| Linting | ruff | >= 0.4.4 |
| Testing | pytest + pytest-asyncio | >= 8.2.0 |
| Server | uvicorn | >= 0.30.0 |

### Frontend
| Component | Technology | Version |
|-----------|-----------|---------|
| Framework | React | 18.3.x |
| Bundler | Vite | 5.3.x |
| Routing | React Router | 6.23.x |
| Charts | Recharts | 2.12.x |
| HTTP | Axios | 1.7.x |
| CSS | TailwindCSS | 3.4.x |

### Infrastructure
| Component | Technology |
|-----------|-----------|
| Database | PostgreSQL via Supabase (free tier) |
| Hosting | Railway (backend + frontend as single service) |
| Email | SendGrid |
| PMS | Cloudbeds API v1.1 |
| CRM | GoHighLevel (GHL) |
| Ads | Meta Graph API + Google Sheets |
| Currency | exchangerate-api.com |

---

## 4. System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL DATA SOURCES                            │
├──────────────┬──────────────┬─────────────┬────────────┬────────────────┤
│  Cloudbeds    │  Meta Ads    │ Google Ads  │    GHL     │  Exchange Rate │
│  API v1.1     │  Graph API   │ via Sheets  │  CRM API   │  API           │
└──────┬───────┴──────┬───────┴─────┬───────┴─────┬──────┴───────┬────────┘
       │              │             │             │              │
       ▼              ▼             ▼             ▼              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     FASTAPI BACKEND (Railway)                            │
│                                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  │
│  │  Scheduler   │  │  Routers    │  │  Services   │  │  Models      │  │
│  │ (APScheduler)│  │ (23 files)  │  │ (19 files)  │  │ (22 files)   │  │
│  │              │  │             │  │             │  │              │  │
│  │ 02:00 Sync   │  │ /api/kpi    │  │ cloudbeds   │  │ Branch       │  │
│  │ 03:00 Metrics│  │ /api/metrics│  │ metrics_eng │  │ Reservation  │  │
│  │ 03:30 Verdict│  │ /api/ads    │  │ kpi_engine  │  │ DailyMetrics │  │
│  │ 04:00 Email  │  │ /api/kol    │  │ country_scr │  │ AdsPerf      │  │
│  │ 05:00 GHL    │  │ /api/combos │  │ currency    │  │ KolRecord    │  │
│  │ 06:00 Ads    │  │ /api/crm    │  │ meta_ads    │  │ AdCombo      │  │
│  │ 08:00 Insight│  │ /api/sync   │  │ verdict_syn │  │ ...          │  │
│  │ 10:00 Sync   │  │ ...         │  │ ...         │  │              │  │
│  │ 14:00 Insight│  │             │  │             │  │              │  │
│  └─────────────┘  └──────┬──────┘  └─────────────┘  └──────────────┘  │
│                          │                                               │
│                          ▼                                               │
│                   ┌─────────────┐                                        │
│                   │  PostgreSQL  │                                        │
│                   │  (Supabase)  │                                        │
│                   │  17+ tables  │                                        │
│                   └─────────────┘                                        │
│                                                                          │
│  Production: serves React SPA from /frontend_dist/                       │
└──────────────────────────────────────────────────────────────────────────┘
       │
       │  REST API (JSON)
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   REACT FRONTEND (SPA)                                    │
│                                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Auth Guard  │  │  Sidebar    │  │ Branch       │  │  29 Pages    │  │
│  │  (JWT)       │  │  Navigation │  │ Selector     │  │              │  │
│  └─────────────┘  └─────────────┘  └──────────────┘  └──────────────┘  │
│                                                                          │
│  Charts: Recharts (Line, Bar, Stacked Bar, Heatmap)                     │
│  Styling: TailwindCSS (utility classes only)                             │
└──────────────────────────────────────────────────────────────────────────┘
       │
       │  Browser
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   END USERS (6 marketing team members)                   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Backend Architecture

### 5.1 Application Entry (`main.py`)

- Creates FastAPI app (v4.1.0)
- Registers CORS middleware (currently `allow_origins=["*"]` — should restrict in production)
- Mounts 23 routers organized by phase
- On startup: patches branch currencies and starts scheduler
- In production: serves React SPA from `frontend_dist/` with catch-all route

### 5.2 Configuration (`config.py`)

All environment variables loaded via Pydantic `BaseSettings`:
- `DATABASE_URL` — PostgreSQL connection string
- `CLOUDBEDS_API_KEY` + per-property keys (`CB_API_KEY_TAIPEI`, etc.)
- `CLOUDBEDS_PROPERTY_IDS` — JSON array mapping branch_id to property_id
- `META_ACCESS_TOKEN_*` / `META_AD_ACCOUNT_*` — per-branch Meta Ads credentials
- `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN` + per-branch Sheet IDs
- `GOOGLE_RES_SHEET_*` — per-branch reservation raw data sheets
- `GHL_LOCATION_ID_*` / `GHL_API_KEY_*` — per-branch GoHighLevel credentials
- `SENDGRID_API_KEY`, `EMAIL_FROM`, `EMAIL_RECIPIENTS`
- `ANTHROPIC_API_KEY` — for AI-powered insights
- `SECRET_KEY` — JWT signing
- `APP_ENV` — development/production

**Rule:** Never use `os.getenv()` inline — always import from `config.py`.

### 5.3 Database Layer (`database.py`)

```python
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,     # test connections before use
    pool_size=3,            # small pool (free tier)
    max_overflow=2,
    pool_timeout=30,
    pool_recycle=1800,      # recycle connections every 30 min
)
```

- Uses SQLAlchemy ORM exclusively — **never raw SQL strings**
- `get_db()` dependency yields session and auto-closes
- DeclarativeBase for all models

### 5.4 Router Organization

Routers are organized by functional domain and registered by phase:

| Phase | Prefix | Router | Description |
|-------|--------|--------|-------------|
| Auth | `/api/auth` | auth.py | Login/register/JWT |
| 1 | `/api/kpi` | kpi.py | KPI targets & summary |
| 1 | `/api/sync` | sync.py | Cloudbeds/CSV/Sheets sync |
| 2 | `/api/metrics` | metrics.py | Daily/Weekly/Monthly metrics |
| 2 | `/api/events` | events.py | Event calendar |
| 2 | `/api/website-metrics` | website_metrics.py | Manual website metrics |
| 2 | `/api/countries` | countries.py | Country ranking |
| 2 | `/api/branches` | branches.py | Branch CRUD |
| 3 | `/api/marketing` | marketing.py | Activity log |
| 3 | `/api/ads` | ads.py | Ads performance |
| 3 | `/api/kol` | kol.py | KOL records |
| 3 | `/api/angles` | angles.py | Ad angles (WIN/TEST/LOSE) |
| 3 | `/api/insights` | insights.py | AI-powered insights |
| 3 | `/api/report` | report.py | Weekly email report |
| 4 | `/api/creative-angles` | creative_angles.py | Creative angle management |
| 4 | `/api/copies` | creative_copies.py | Ad copy management |
| 4 | `/api/materials` | creative_materials.py | Ad material management |
| 4 | `/api/combos` | combos.py | Ad combo + verdict |
| 4 | `/api/ad-analyzer` | ad_analyzer.py | AI ad analysis |
| — | `/api/crm` | crm.py | CRM dashboard |
| — | `/api/email-marketing` | email_marketing.py | GHL email analytics |
| — | `/api/gov-visitor` | gov_visitor.py | Government visitor data |

### 5.5 Service Layer

Services contain all business logic. Routers are thin — they validate input, call services, and format responses.

**Key services:**
- **`cloudbeds.py`** — Cloudbeds API client. Full sync (365d back + 180d forward) and incremental sync (last 2 days). Handles pagination, deduplication via `cloudbeds_reservation_id` upsert, country/room-type/source mapping.
- **`metrics_engine.py`** — Computes OCC, ADR, RevPAR, cancellation % from reservations. Writes to `daily_metrics` cache. Also handles Cloudbeds Insights API overlay.
- **`kpi_engine.py`** — Computes KPI achievement: actual vs target revenue, run-rate forecast, OCC-based forecast.
- **`country_scorer.py`** — Scores countries as Hot/Warm/Cold using weighted formula: WoW growth (40%) + MoM growth (30%) + ADR trend (20%) + recency (10%).
- **`currency.py`** — Fetches exchange rates from exchangerate-api.com. Caches in memory + DB. Falls back to last cached rate on failure.
- **`verdict_sync.py`** — Nightly job: computes ad combo performance from ads data, classifies WIN/TEST/LOSE based on ROAS benchmark, propagates derived verdicts to copies/materials.
- **`id_generator.py`** — Generates sequential IDs (ANG-001, CPY-001, MAT-001, CMB-001) using `SELECT FOR UPDATE` to prevent race conditions.
- **`ad_analyzer_service.py`** — AI-powered ad analysis using Anthropic Claude API.

---

## 6. Frontend Architecture

### 6.1 App Shell

```
AuthProvider → BranchProvider → AppRoutes
                                   │
                                   ├── Not authenticated → Login page
                                   │
                                   └── Authenticated → Sidebar + BranchSelector + <Routes>
```

- **AuthContext** — JWT-based authentication state. Wraps entire app.
- **BranchContext** — Global branch selector. Persistent across all pages.
- **Sidebar** — Left navigation panel with section grouping.
- **BranchSelector** — Top bar dropdown, stored in React Context.

### 6.2 Route Map (29 pages)

```
/                           → Redirect to /home
/login                      → Login.jsx
/home                       → Home.jsx (KPI summary + hot countries + OCC heatmap)
/kpi                        → KPI.jsx (KPI detail + forecast)
/performance                → Performance.jsx (hub: pick Daily/Weekly/Monthly/OTA)
/performance/daily          → PerformanceDaily.jsx
/performance/weekly         → PerformanceWeekly.jsx
/performance/monthly        → PerformanceMonthly.jsx
/performance/ota            → PerformanceOTA.jsx
/countries                  → Countries.jsx (ranking table, Hot/Warm/Cold)
/countries/:code            → CountryDetail.jsx (booking trend + YoY)
/country-intel              → CountryIntel.jsx
/marketing                  → Marketing.jsx (activity log)
/ads                        → Ads.jsx (ads performance + ROAS)
/kol                        → KOL.jsx (KOL table + reservation linker)
/angles                     → Angles.jsx (WIN/TEST/LOSE cards)
/insights                   → Insights.jsx (AI insights + KOL→Paid Ads)
/report                     → Report.jsx (weekly email preview + send)
/combos                     → AdCombos.jsx (PRIMARY creative page)
/ad-analyzer                → AdAnalyzer.jsx (AI ad analysis)
/copies                     → CreativeCopies.jsx
/materials                  → CreativeMaterials.jsx
/crm                        → CRMDashboard.jsx
/email-marketing            → EmailMarketing.jsx (GHL analytics)
/gov-data                   → GovVisitorData.jsx
/dashboard                  → Dashboard.jsx (Phase 1 legacy)
/reservations               → Reservations.jsx
/kpi-targets                → KPITargets.jsx
/settings                   → Settings.jsx
/users                      → Users.jsx
```

### 6.3 State Management

- **React Context only** — no Redux.
- `AuthContext` — user session, JWT token
- `BranchContext` — selected branch ID, branch list
- All server state: fetch on mount, no client-side caching beyond React state.

### 6.4 Color System (OCC Bands)

```
 0% – 50%  → Red    (bg-red-100 text-red-800)
50% – 70%  → Yellow (bg-yellow-100 text-yellow-800)
70% – 90%  → Blue   (bg-blue-100 text-blue-800)
90% – 100% → Green  (bg-green-100 text-green-800)
```

### 6.5 Verdict Badge Colors (Creative Library)

```
WIN  → green-600
TEST → yellow-500
LOSE → red-600
```
Derived verdict badges always show "(derived)" label.

---

## 7. Database Schema

### 7.1 Tables Overview (17+ tables)

**Core tables:**
1. `branches` — 5 hotel/hostel properties
2. `kpi_targets` — Monthly revenue targets per branch
3. `reservations` — Cloudbeds reservation data (main data source)
4. `daily_metrics` — Computed cache (OCC/ADR/RevPAR per day)
5. `events` — City events calendar
6. `website_metrics` — Manual website traffic data
7. `users` — Dashboard users (admin/editor/viewer)

**Marketing tables:**
8. `ads_performance` — Meta Ads + Google Ads data
9. `kol_records` — KOL collaborations
10. `kol_bookings` — KOL-attributed reservations
11. `ad_angles` — Ad messaging angles
12. `marketing_activities` — Marketing activity log

**Creative Library tables (Phase 4):**
13. `branch_keypoints` — Branch selling points
14. `ad_copies` — Ad copy drafts (CPY-001 format)
15. `ad_materials` — Ad creative materials (MAT-001 format)
16. `ad_approvals` — Copy/material approval workflow
17. `ad_names` — Auto-generated ad names on approval
18. `ad_combos` — Copy+Material pairs with verdict (CMB-001 format)

**Additional tables:**
19. `email_campaign_stats` — Email campaign analytics
20. `email_events` — GHL email events
21. `gov_visitors` — Government visitor data

### 7.2 Standard Column Conventions

Every table includes:
- `id` — UUID primary key, `DEFAULT gen_random_uuid()`
- `created_at` — TIMESTAMPTZ, `DEFAULT NOW()`
- `updated_at` — TIMESTAMPTZ, `DEFAULT NOW()`, auto-update on change
- Exception: `daily_metrics` uses `computed_at` instead of `updated_at`

### 7.3 Monetary Value Convention

**Critical rule:** ALL monetary values stored in BOTH native currency AND VND equivalent:

```
grand_total_native  DECIMAL(12,2)   -- branch's local currency
grand_total_vnd     DECIMAL(15,2)   -- converted to VND at ingestion time
```

- Convert at **write time** using exchange rate API — never at read time
- This prevents historical data distortion from exchange rate fluctuations
- Cross-branch comparison always uses `_vnd` columns

### 7.4 Key Indexes

```sql
-- Reservations (most queried table)
idx_reservations_branch_checkin    ON reservations(branch_id, check_in_date)
idx_reservations_status            ON reservations(status)
idx_reservations_source_category   ON reservations(source_category)
idx_reservations_country_code      ON reservations(guest_country_code)

-- Daily metrics cache
idx_daily_metrics_branch_date      ON daily_metrics(branch_id, date)
```

### 7.5 Derived Fields (Set on Ingestion Only)

These fields are derived when data is first ingested and **never re-derived on query**:

| Field | Source | Logic |
|-------|--------|-------|
| `room_type_category` | `room_type` | Contains "Dorm" (case-insensitive) → "Dorm", else → "Room" |
| `source_category` | `source` | Contains "Website"/"Booking Engine"/"Blogger"/"Direct" → "Direct", else → "OTA" |
| `guest_country_code` | `guest_country` | "United States of America" → "USA", "United Kingdom" → "UK", "Unknown" → "Others", else as-is |

### 7.6 OTA Canonical Mapping

```python
OTA_CANONICAL = {
    "booking.com":  "Booking.com",
    "hostelworld":  "Hostelworld",
    "agoda":        "Agoda",
    "ctrip":        "Ctrip",
    "trip.com":     "Ctrip",
    "expedia":      "Expedia",
}
```

---

## 8. API Layer

### 8.1 Standard Response Format

Every endpoint returns this exact structure:

```json
{
  "success": true,
  "data": <any>,
  "error": null,
  "timestamp": "2026-01-01T00:00:00Z"
}
```

On error:
```json
{
  "success": false,
  "data": null,
  "error": "Human-readable message",
  "timestamp": "2026-01-01T00:00:00Z"
}
```

### 8.2 API Conventions

| Convention | Rule |
|-----------|------|
| **Dependency Injection** | `db: Session = Depends(get_db)` — never instantiate services in handlers |
| **Error handling** | Every endpoint body wrapped in `try/except`. 404 for not found, 422 for validation (Pydantic), 500 for unexpected |
| **Date filters** | `date_from` / `date_to` (ISO8601: YYYY-MM-DD) |
| **Branch filter** | `branch_id` (UUID) on all list endpoints |
| **Pagination** | `limit` (default 50, max 200) + `offset` (default 0) |
| **Soft delete** | DELETE sets `is_active = False` — never hard delete |
| **Naming** | Endpoints: lowercase, hyphen-separated. Params/fields: snake_case |

### 8.3 Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health check (no auth) |
| `POST` | `/api/auth/login` | JWT authentication |
| `POST` | `/api/sync/cloudbeds` | Manual Cloudbeds sync |
| `POST` | `/api/metrics/recompute` | Rebuild daily_metrics cache |
| `GET` | `/api/kpi/summary` | Cross-branch KPI achievement |
| `GET` | `/api/metrics/daily` | OCC/Revenue/ADR/RevPAR per day |
| `GET` | `/api/metrics/weekly` | Weekly rollup with OTA mix |
| `GET` | `/api/metrics/monthly` | Monthly rollup with country breakdown |
| `GET` | `/api/metrics/ota-mix` | OTA channel % share |
| `GET` | `/api/metrics/country-breakdown` | Room nights per country per month |
| `GET` | `/api/countries/ranking` | Hot/Warm/Cold country scores |
| `GET` | `/api/ads` | Ads performance data |
| `GET` | `/api/kol` | KOL records |
| `GET` | `/api/angles` | Ad angles (WIN/TEST/LOSE) |
| `GET` | `/api/combos` | Ad combos with verdict |
| `GET` | `/api/insights` | AI-generated insights |
| `GET` | `/api/report/weekly/preview` | Weekly email preview |
| `POST` | `/api/report/weekly/send` | Trigger weekly email |
| `GET` | `/api/crm/dashboard` | CRM analytics |
| `GET` | `/api/email-marketing/stats` | Email campaign stats |

---

## 9. Scheduled Jobs

All scheduled jobs run inside the FastAPI process via APScheduler. Timezone: `Asia/Ho_Chi_Minh`.

| Time (ICT) | Job ID | Description |
|------------|--------|-------------|
| **02:00** | `nightly_cloudbeds_sync` | Full Cloudbeds sync (365d back + 180d forward, all branches) |
| **03:00** | `nightly_metrics_compute` | Recompute daily_metrics cache (14-day lookback + next month) |
| **03:30** | `nightly_verdict_sync` | Sync ad combo performance metrics + compute derived verdicts |
| **04:00** | `nightly_email_stats` | Aggregate email campaign statistics (last 7 days) |
| **05:00** | `daily_ghl_email_sync` | Sync GoHighLevel email workflow stats |
| **06:00** | `daily_ads_sync` | Sync Meta Ads + Google Ads (last 3 days) |
| **08:00** | `insights_sync_morning` | Cloudbeds Insights API refresh (14-day lookback) |
| **10:00** | `daytime_cloudbeds_sync_morning` | Incremental Cloudbeds sync (last 2 days) |
| **14:00** | `insights_sync_afternoon` | Cloudbeds Insights API refresh (14-day lookback) |

**Logging rule:** Every scheduler run logs start, end, and row count.

---

## 10. External Integrations

### 10.1 Cloudbeds API (Primary Data Source)

- **Auth:** Per-property API keys via `CB_API_KEY_*` env vars
- **Base URL:** `https://api.cloudbeds.com/api/v1.1`
- **Endpoints used:**
  - `GET /reservations` — Pull reservations with date range + pagination
  - Cloudbeds Insights API — OCC/ADR/RevPAR (overlay on computed metrics)
- **Sync strategy:**
  - Full sync at 2am: 365 days back + 180 days forward, by `modifiedAt`
  - Incremental sync at 10am: last 2 days only
  - Deduplication: upsert on `cloudbeds_reservation_id`
- **Mapping:** Country names standardized, room types categorized, sources categorized

### 10.2 Meta Ads API

- **Auth:** Per-branch access tokens (`META_ACCESS_TOKEN_*`)
- **Endpoint:** `/act_{account_id}/insights`
- **Fields pulled:** campaign_name, adset_name, ad_name, spend, impressions, clicks, leads, bookings, revenue
- **Sync:** Daily at 6am, last 3 days

### 10.3 Google Ads (via Google Sheets)

- **Auth:** OAuth2 (client_id + client_secret + refresh_token)
- **Per-branch Sheet IDs** configured in env vars
- **Sync:** Daily at 6am alongside Meta Ads
- **Data flow:** Google Ads → Google Sheets (manual/automated) → HiD reads Sheets API

### 10.4 GoHighLevel (GHL) — Email CRM

- **Auth:** Per-branch location IDs + API keys (`GHL_LOCATION_ID_*`, `GHL_API_KEY_*`)
- **Base URL:** `https://services.leadconnectorhq.com`
- **Sync:** Daily at 5am
- **Purpose:** Email marketing campaign stats, workflow analytics

### 10.5 Exchange Rate API

- **Provider:** exchangerate-api.com (free tier)
- **Currencies:** TWD→VND, JPY→VND, USD→VND
- **Caching:** In-memory + DB. Falls back to last cached rate on failure.
- **Critical rule:** Never block data ingestion due to currency API failure.

### 10.6 SendGrid (Email)

- **Purpose:** Weekly marketing report email
- **Schedule:** Monday 7am Vietnam time (not currently in scheduler — may be manual trigger)
- **Recipients:** Comma-separated in `EMAIL_RECIPIENTS` env var
- **Content:** KPI snapshot, hot countries, winning angles, KOL opportunities, pending approvals

### 10.7 Anthropic Claude API

- **Purpose:** AI-powered ad analysis and marketing insights
- **Used by:** `ad_analyzer_service.py`, `insights.py` router
- **Auth:** `ANTHROPIC_API_KEY` env var

---

## 11. Authentication & Authorization

### 11.1 Auth Flow

1. User submits email + password to `POST /api/auth/login`
2. Backend verifies bcrypt password hash
3. Returns JWT token
4. Frontend stores token in AuthContext
5. All API requests include `Authorization: Bearer <token>` header

### 11.2 User Roles

| Role | Permissions |
|------|-------------|
| `admin` | Full access: CRUD all data, manage users, trigger syncs |
| `editor` | Read + write: create/edit reservations, KPIs, ads, KOLs |
| `viewer` | Read-only: dashboard viewing only |

### 11.3 Frontend Auth Guard

- `AuthProvider` wraps entire app
- If not authenticated → redirect to `/login`
- If authenticated → render main app shell (Sidebar + BranchSelector + Routes)

---

## 12. Metrics Engine Rules

### 12.1 OCC (Occupancy Rate)

```
OCC % = total_sold / branches.total_rooms
```

- `total_sold` = COUNT of reservation nights where `check_in_date <= target_date < check_out_date` AND `status != 'Cancelled'`
- If `branches.total_room_count` IS NOT NULL → also compute `room_occ_pct = rooms_sold / total_room_count`
- If `branches.total_dorm_count` IS NOT NULL → also compute `dorm_occ_pct = dorms_sold / total_dorm_count`
- **Never fail** if split counts are null — fall back to total OCC only
- OCC is **always computed, never stored in reservations**

### 12.2 ADR (Average Daily Rate)

```
ADR = SUM(grand_total_native) / COUNT(room_nights_sold)
```

- Use `grand_total_native` for per-branch display
- Use `grand_total_vnd` for cross-branch comparison
- **Exclude cancelled reservations**

### 12.3 RevPAR (Revenue Per Available Room)

```
RevPAR = ADR × OCC%
```

Alternative (should produce same result): `RevPAR = Total Revenue / Total Available Rooms`

### 12.4 Cancellation %

```
cancellation_pct = COUNT(cancelled reservations) / COUNT(all reservations)
```

- Use `cancellation_date` for WHEN cancelled
- Use `reservation_date` for WHEN booked

### 12.5 OTA Channel Mix

```
OTA mix % = room_nights_per_source / total_room_nights × 100
```

Canonical OTA names: Booking.com, Hostelworld, Agoda, Ctrip, Expedia, Direct, Other

### 12.6 daily_metrics Cache Strategy

- Nightly job at 3am Vietnam time
- Computes for: yesterday + any date where `computed_at < last reservation update`
- Upsert on `(branch_id, date)`
- Dashboard reads from cache first — falls back to live calculation only if cache is empty

### 12.7 Country Scoring (Hot/Warm/Cold)

Weighted formula:
- WoW booking growth: **40%**
- MoM booking growth: **30%**
- ADR trend: **20%**
- Recency: **10%**

Transparent, explainable, no ML required.

### 12.8 KPI Forecasting

Two forecast methods:
1. **Run-rate forecast:** Extrapolate current month's pace to month-end
2. **OCC-based forecast:** Use `predicted_occ_pct` (manual input) to estimate revenue

### 12.9 Number Formatting Rules

- **Revenue/ADR/RevPAR:** Display full numbers (no K/M/B abbreviation)
- **Percentages:** Display with 2 decimal places

---

## 13. Creative Library System (Phase 4)

### 13.1 Verdict Model

The verdict system classifies ad performance:

```
                    ┌─────────────┐
                    │  ad_combos  │ ← verdict lives HERE only
                    │  (CMB-001)  │
                    ├─────────────┤
                    │ copy_id     │──→ ad_copies (CPY-001)
                    │ material_id │──→ ad_materials (MAT-001)
                    │ verdict     │    WIN / TEST / LOSE
                    │ verdict_src │    "manual" / "auto"
                    └─────────────┘
                           │
                    derived_verdict propagates DOWN
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
        ad_copies               ad_materials
      derived_verdict          derived_verdict
       (READ-ONLY)              (READ-ONLY)
```

### 13.2 Verdict Classification Logic

Only **TOF Sales** campaigns count (`funnel_stage=TOF` AND `campaign_name LIKE %Sales%`):

```
Benchmark_TOF = AVG ROAS of all TOF Sales ads per branch (dynamic)

Qualification: impressions >= 20,000 AND bookings >= 5

WIN:  ROAS >= Benchmark_TOF
LOSE: ROAS <= 0.6 × Benchmark_TOF
TEST: Everything else (or insufficient data)
```

- `verdict_notes` stores reason (e.g., "ROAS 5.11x vs BM 2.65x", "insufficient data")

### 13.3 Verdict Rules

1. Verdict column exists on `ad_combos` ONLY
2. `derived_verdict` on copies/materials is computed — **never user-input**
3. When human PATCHes verdict: set `verdict_source = "manual"` always
4. Nightly sync **MUST check** `verdict_source` before overwriting: skip if "manual"
5. `(copy_id, material_id)` in ad_combos is UNIQUE — one pair, one row, ever

### 13.4 ID Generation

- Sequential IDs: ANG-001, CPY-001, MAT-001, CMB-001
- Generated via `id_generator.py` using `SELECT FOR UPDATE`
- Globally sequential (not per-branch)

### 13.5 Combo Constraints

- Copy and material in a combo **must belong to the same branch** — validated at API layer
- `combo_code` auto-generated on creation
- Files are never uploaded to HiD — only Drive/URL links stored
- File links always open in new tab

---

## 14. Deployment & Infrastructure

### 14.1 Railway Configuration

- **Single service** — backend serves both API and React SPA
- Backend START: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Frontend build output copied to `backend/frontend_dist/` for production
- All env vars set in Railway dashboard — never in `railway.json`

### 14.2 Docker (Multi-Stage Build)

```
builder stage → install dependencies
final stage   → slim Python image, only production deps
```

- Expose only `$PORT` (Railway sets automatically)
- No dev dependencies in production image

### 14.3 Health Check

- `GET /health` — MUST always exist and return 200
- Response: `{ "status": "ok", "timestamp": "...", "version": "4.1.0" }`
- **Never add auth** to this endpoint

### 14.4 CORS

- **Development:** Allow `http://localhost:5173`
- **Production:** Should restrict to Railway frontend domain only (currently `*` — needs fixing)

### 14.5 Database (Supabase)

- Free tier: 500MB storage, unlimited API calls
- `DATABASE_URL` stays the same when upgrading to Pro ($25/mo)
- All schema changes via Alembic — **never ALTER TABLE in Supabase UI**

---

## 15. Data Flow Pipelines

### 15.1 Reservation Data Pipeline

```
Cloudbeds API
    │
    │ GET /reservations (paginated, last 365d + 180d forward)
    ▼
cloudbeds.py (sync_all_branches)
    │
    │ For each reservation:
    │   1. Map guest_country → guest_country_code
    │   2. Derive room_type_category (Room/Dorm)
    │   3. Derive source_category (OTA/Direct)
    │   4. Convert grand_total → VND using exchange rate
    │   5. Upsert on cloudbeds_reservation_id
    ▼
reservations table (PostgreSQL)
    │
    │ Nightly at 3am
    ▼
metrics_engine.py (nightly_metrics_job)
    │
    │ For each (branch, date):
    │   1. Count rooms_sold, dorms_sold, total_sold
    │   2. Compute OCC% = total_sold / total_rooms
    │   3. Compute ADR = revenue / rooms_sold
    │   4. Compute RevPAR = ADR × OCC%
    │   5. Count cancellations, compute cancellation_%
    │   6. Upsert into daily_metrics
    ▼
daily_metrics table (cache)
    │
    │ API reads
    ▼
Dashboard (React frontend)
```

### 15.2 Ads Data Pipeline

```
Meta Ads API                    Google Sheets (Ads data)
    │                                   │
    │ GET /insights                      │ Sheets API v4
    ▼                                   ▼
meta_ads.py                     google_sheets_ads.py
    │                                   │
    │ Normalize: campaign, adset,       │ Parse rows: campaign,
    │ ad, spend, impressions,           │ cost, clicks, etc.
    │ clicks, leads, bookings           │
    ▼                                   ▼
scheduler.py (_ads_sync_job) ──────────────┐
    │                                       │
    │ Upsert by meta_ad_id (Meta)          │
    │ Upsert by campaign+date (Google)     │
    ▼                                       ▼
ads_performance table
    │
    ▼
verdict_sync.py (nightly 3:30am)
    │
    │ Compute ROAS, compare to benchmark
    │ Classify: WIN/TEST/LOSE
    ▼
ad_combos.verdict (+ derived_verdict on copies/materials)
```

### 15.3 Email Marketing Pipeline

```
GoHighLevel API
    │
    │ GET /workflows, /emails
    ▼
ghl_email_sync.py (daily 5am)
    │
    │ Sync workflow stats per branch
    ▼
email_campaign_stats / email_events tables
    │
    ▼
CRM Dashboard + Email Marketing pages
```

---

## 16. Coding Rules & Conventions

### 16.1 Backend Rules

| Rule | Details |
|------|---------|
| **Response format** | Always `{ success, data, error, timestamp }` |
| **Env vars** | Only via `config.py` Pydantic Settings — never `os.getenv()` |
| **Credentials** | Never hardcode — always env vars |
| **ORM** | SQLAlchemy only — never raw SQL strings |
| **Migrations** | Alembic only — never ALTER TABLE in Supabase UI |
| **External APIs** | All calls wrapped in `try/except` with logging |
| **Computed metrics** | OCC/ADR/RevPAR computed from reservations — never manual |
| **Derived fields** | room_type_category, source_category set on ingestion only |
| **Soft delete** | DELETE sets `is_active=False` — never hard delete |
| **Dependency injection** | Use `Depends()` — never instantiate services in handlers |
| **Monetary values** | Always store BOTH native AND VND |

### 16.2 Frontend Rules

| Rule | Details |
|------|---------|
| **CSS** | TailwindCSS utility classes only — no custom CSS files |
| **State** | React Context only — no Redux |
| **Charts** | Recharts library only |
| **API calls** | Axios via `/src/api/` layer |
| **Branch selector** | Must persist across all pages via BranchContext |
| **Verdict badges** | WIN=green, TEST=yellow, LOSE=red. Derived shows "(derived)" |
| **File links** | Always open in new tab — never inline preview |
| **URL params** | Filters persist as URL search params on creative pages |

### 16.3 Git / Development Workflow

```bash
# Backend development
cd backend && uvicorn app.main:app --reload

# Frontend development
cd frontend && npm run dev

# Run tests
cd backend && pytest tests/ -v

# Apply migrations
cd backend && alembic upgrade head

# Lint
cd backend && ruff check .
```

---

## 17. Testing Strategy

### 17.1 Coverage Requirements

- Every router endpoint: minimum 1 happy-path + 1 error-path test
- Every service function: unit test with known inputs/outputs
- Scheduler jobs: test underlying service function, not scheduler itself

### 17.2 Database in Tests

- Use pytest fixtures for setup/teardown
- In-memory SQLite or separate test schema — **NEVER hit production DB**
- Each test gets fresh DB state — no inter-test dependencies

### 17.3 External API Mocking

- **ALWAYS mock:** Cloudbeds, SendGrid, exchange rate, Meta Ads, Google Sheets, GHL
- Use `unittest.mock.patch` or `pytest-mock`
- **Never make real HTTP calls in tests**

### 17.4 Critical Metrics Tests

Must test with known fixture data:
- OCC% = rooms_sold / total_rooms (e.g., 45/69 = 65.22%)
- ADR = revenue / rooms_sold
- RevPAR = ADR × OCC%
- Cancellation% = cancellations / total_reservations
- OTA mix% = ota_nights / total_nights per source

### 17.5 File Naming

`test_{module_name}.py` — e.g., `test_metrics_engine.py`, `test_cloudbeds.py`

---

## 18. Phase Roadmap

| Phase | Name | Status | Key Deliverables |
|-------|------|--------|-----------------|
| **1** | Foundation | Complete | DB schema, Cloudbeds sync, KPI targets, basic dashboard |
| **2** | Performance Intelligence | Complete | Daily/Weekly/Monthly metrics, OTA mix, country analysis, events |
| **3** | Marketing Intelligence | Complete | Ads tracking, KOL management, ad angles, AI insights, weekly report |
| **4** | Creative Intelligence Library | **Active** | Ad combos, verdict system, copies, materials, AI ad analyzer |
| **5** | (Planned) | — | Advanced analytics, predictive models |
| **6** | Creative Ops | Partial | branch_keypoints, ad_copies, ad_materials, ad_approvals, ad_names |
| **7** | (Planned) | — | Meta Ads API direct, GA4 API, TikTok Ads API |
| **8** | (Planned) | — | ML country scoring, AI angle suggestions, GA4+Meta OAuth |

### Key Design Decisions

1. **OCC is always computed** — never stored in reservations (avoids dual source of truth)
2. **daily_metrics as cache** — pre-computed nightly, dashboard reads cache (performance)
3. **VND stored at write-time** — never computed at read-time (historical accuracy)
4. **Derived fields on ingestion** — room_type_category, source_category locked at import
5. **Manual ad tagging** — no auto-classification until sufficient training data (Phase 8)
6. **Formula-based country scoring** — transparent, no ML, adjustable weights
7. **APScheduler inside FastAPI** — single process, simpler deployment
8. **Website metrics: manual input** — GA4/Meta OAuth deferred to Phase 8
9. **Auto-calculated Daily Brief** — no manual override, Cloudbeds is source of truth
10. **Verdict on combos only** — copies/materials get derived verdict (read-only)

---

*This document is the single source of truth for the HiD system architecture. Update it when making architectural changes.*
