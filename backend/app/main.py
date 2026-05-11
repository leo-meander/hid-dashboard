from datetime import datetime, timezone
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.routers import kpi, sync
from app.routers import metrics, events, website_metrics, countries, branches
from app.routers import marketing, ads, kol, angles, insights, report
from app.routers import auth
from app.routers import creative_angles, creative_copies, creative_materials, combos
from app.routers import ad_analyzer
from app.routers import crm
from app.routers import email_marketing
from app.routers import marketing_activity, marketing_budget
from app.routers import gov_visitor
from app.routers import holiday_intel
from app.routers import api_keys, public_api
from app.routers import alerts
from app.routers import rate_plan_quota
from app.routers import chat
from app.scheduler import setup_scheduler
from app.database import SessionLocal
from app.models.branch import Branch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="HiD — Hotel Intelligence Dashboard", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])

# Phase 1
app.include_router(kpi.router, prefix="/api/kpi", tags=["KPI"])
app.include_router(sync.router, prefix="/api/sync", tags=["Sync"])

# Phase 2
app.include_router(branches.router)
app.include_router(metrics.router, prefix="/api/metrics", tags=["Metrics"])
app.include_router(events.router, prefix="/api/events", tags=["Events"])
app.include_router(website_metrics.router, prefix="/api/website-metrics", tags=["Website Metrics"])
app.include_router(countries.router, prefix="/api/countries", tags=["Countries"])

# Phase 3
app.include_router(marketing.router, prefix="/api/marketing", tags=["Marketing"])
app.include_router(ads.router, prefix="/api/ads", tags=["Ads"])
app.include_router(kol.router, prefix="/api/kol", tags=["KOL"])
app.include_router(angles.router, prefix="/api/angles", tags=["Angles"])
app.include_router(insights.router, prefix="/api/insights", tags=["Insights"])
app.include_router(report.router, prefix="/api/report", tags=["Report"])

# Phase 4 — Creative Intelligence Library
app.include_router(creative_angles.router, prefix="/api/creative-angles", tags=["Creative Angles"])
app.include_router(creative_copies.router, prefix="/api/copies", tags=["Copies"])
app.include_router(creative_materials.router, prefix="/api/materials", tags=["Materials"])
app.include_router(combos.router, prefix="/api/combos", tags=["Ad Combos"])
app.include_router(ad_analyzer.router, prefix="/api/ad-analyzer", tags=["Ad Analyzer"])

# CRM Dashboard
app.include_router(crm.router, prefix="/api/crm", tags=["CRM"])

# Email Marketing (GHL)
app.include_router(email_marketing.router, prefix="/api/email-marketing", tags=["Email Marketing"])

# Marketing Activity (consolidated view)
app.include_router(marketing_activity.router, prefix="/api/marketing-activity", tags=["Marketing Activity"])

# Marketing Budget Planner — monthly allocation per branch & channel
app.include_router(marketing_budget.router, prefix="/api/marketing-budget", tags=["Marketing Budget"])

# Government Visitor Data
app.include_router(gov_visitor.router, prefix="/api/gov-visitor", tags=["Government Visitor Data"])

# Phase 5 — Holiday Intelligence
app.include_router(holiday_intel.router)

# API Keys & Public API
app.include_router(api_keys.router, prefix="/api/api-keys", tags=["API Keys"])
app.include_router(public_api.router, prefix="/api/public", tags=["Public API"])

# Alert System
app.include_router(alerts.router, prefix="/api/alerts", tags=["Alerts"])

# Rate Plan Quota tracking — manual cap on bookings per CRM/event rate plan
app.include_router(rate_plan_quota.router, prefix="/api/rate-plan-quotas", tags=["Rate Plan Quotas"])

# HiD Assistant chatbox — Claude-powered Q&A over all dashboard data (Phase 1: read-only)
app.include_router(chat.router, prefix="/api/chat", tags=["Chat Assistant"])

setup_scheduler(app)


# One-time currency patch — runs on every startup, safe to leave in
_BRANCH_CURRENCIES = {
    "11111111-1111-1111-1111-111111111101": "TWD",  # Taipei
    "11111111-1111-1111-1111-111111111102": "VND",  # Saigon
    "11111111-1111-1111-1111-111111111103": "TWD",  # 1948
    "11111111-1111-1111-1111-111111111104": "TWD",  # Oani
    "11111111-1111-1111-1111-111111111105": "JPY",  # Osaka
}

def _patch_branch_currencies():
    # Tolerant of connection errors so the container can finish booting
    # even when Supabase pool is temporarily saturated (e.g., rolling
    # deploy overlap). The next /health check passes once the pool frees,
    # and the patch is idempotent — it'll get applied on next restart.
    try:
        db = SessionLocal()
    except Exception as e:
        logger.warning("Currency patch skipped — DB session unavailable: %s", e)
        return
    try:
        for bid, cur in _BRANCH_CURRENCIES.items():
            b = db.query(Branch).filter_by(id=bid).first()
            if b and b.currency != cur:
                b.currency = cur
                logger.info("Patched branch %s currency → %s", b.name, cur)
        db.commit()
    except Exception as e:
        logger.warning("Currency patch failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _ensure_kpi_override_column():
    """Add actual_revenue_override column to kpi_targets if missing."""
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(text(
            "ALTER TABLE kpi_targets ADD COLUMN IF NOT EXISTS "
            "actual_revenue_override NUMERIC(15,2)"
        ))
        db.commit()
        logger.info("Ensured actual_revenue_override column exists")
    except Exception:
        db.rollback()
    finally:
        db.close()

@app.on_event("startup")
async def _startup():
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _patch_branch_currencies)
    loop.run_in_executor(None, _ensure_kpi_override_column)


@app.get("/health")
def health():
    return {
        "success": True,
        "data": {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()},
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Serve React SPA (production only) ─────────────────────────────────────────
_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend_dist")
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """Catch-all: serve index.html for all non-API routes (SPA routing)."""
        file_path = os.path.join(_DIST, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(_DIST, "index.html"))
