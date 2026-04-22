# HiD — Hotel Intelligence Dashboard

## What This Project Is
Internal marketing BI dashboard for a 5-branch hotel group (Saigon, Taipei, 1948, Osaka, Oani).
Consolidates Cloudbeds reservations, Meta Ads, and KOL data into one source of truth.
Replaces manual Excel workflows for 6 marketing team users.

## Tech Stack
- Backend:    Python FastAPI + APScheduler
- Frontend:   React + Vite + Recharts + TailwindCSS
- Database:   PostgreSQL via Supabase
- Email:      SendGrid
- Deployment: Zeabur (backend + frontend)

## Architecture Overview
- API spec:         docs/specs/api-spec.md
- Data model:       docs/specs/data-model.md
- Frontend spec:    docs/specs/frontend-spec.md
- Integrations:     docs/specs/integrations.md
- Design rationale: docs/architecture.md

## Critical Rules
- All API responses: `{ success: bool, data: any, error: str|null, timestamp: ISO8601 }`
- Never hardcode credentials — always use environment variables via `config.py`
- All monetary values stored in BOTH native currency AND vnd equivalent at write-time
- All DB queries use SQLAlchemy ORM — never raw SQL strings
- Alembic for all schema changes — never ALTER TABLE directly in Supabase UI
- All external API calls (Cloudbeds, SendGrid, exchange rate) wrapped in try/except with logging
- OCC, ADR, RevPAR are COMPUTED from reservations — never manually entered or overridden
- room_type_category (Room/Dorm) and source_category (OTA/Direct) derived on ingestion, never later
- Run tests before committing: `pytest backend/tests/ -v`

## Project Structure
```
hid/
├── CLAUDE.md
├── .claude/rules/          # Scoped rules loaded per file type
├── .env.example
├── docs/
│   ├── current-phase.md    # ALWAYS read this first when starting work
│   ├── architecture.md
│   ├── changelog.md
│   ├── lessons.md
│   └── specs/
│       ├── api-spec.md
│       ├── data-model.md
│       ├── frontend-spec.md
│       └── integrations.md
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── scheduler.py
│   │   ├── models/
│   │   ├── routers/
│   │   └── services/
│   ├── alembic/
│   ├── tests/
│   └── requirements.txt
└── frontend/
    └── src/
        ├── pages/
        ├── components/
        └── api/
```

## Key Commands
- Backend dev:  `cd backend && uvicorn app.main:app --reload`
- Frontend dev: `cd frontend && npm run dev`
- Tests:        `cd backend && pytest tests/ -v`
- Migration:    `cd backend && alembic upgrade head`
- Lint:         `cd backend && ruff check .`

## Current Phase
Phase 4: Creative Intelligence Library (with Ad Combo layer)
See `docs/current-phase.md` for detailed tasks and checklist.

## Creative Library Rules
- Verdict lives on ad_combos ONLY — never accept verdict as input on copies or materials
- derived_verdict on copies and materials is READ-ONLY — computed nightly by verdict_sync.py
- verdict_source = "manual" blocks nightly auto-overwrite — enforce in sync job always
- (copy_id, material_id) in ad_combos is UNIQUE — one pair, one row, ever
- Files are never uploaded to HiD — only Drive/URL links stored
- id_generator.py must use SELECT FOR UPDATE to prevent race conditions
