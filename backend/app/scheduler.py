import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Sync jobs run on a threadpool so blocking HTTP / DB calls NEVER freeze the
# FastAPI event loop. Without this, a slow Cloudbeds / Meta / Google call at
# 2am can hang the whole server until manual restart.
scheduler = AsyncIOScheduler(
    timezone="Asia/Ho_Chi_Minh",
    executors={
        "default": ThreadPoolExecutor(max_workers=8),
        "asyncio": AsyncIOExecutor(),
    },
    job_defaults={
        "coalesce": True,          # if many runs were missed, collapse to one
        "max_instances": 1,         # never let a job overlap itself
        "misfire_grace_time": 600,  # 10 min grace when resuming after a restart
    },
)


def setup_scheduler(app):
    """Register scheduled jobs and attach lifecycle to FastAPI app."""

    @app.on_event("startup")
    async def start_scheduler():
        from app.services.cloudbeds import sync_all_branches
        from app.services.metrics_engine import nightly_metrics_job, cloudbeds_insights_sync_job
        from app.services.verdict_sync import sync_combo_performance, compute_derived_verdicts
        from app.database import SessionLocal

        # Sync wrapper so sync_all_branches (async def, but no real awaits inside)
        # can run on the threadpool executor without touching the main event loop.
        def _cloudbeds_sync_job(incremental: bool = True):
            asyncio.run(sync_all_branches(incremental=incremental))

        # Nightly FULL Cloudbeds sync at 2:00am Vietnam time
        # Full sync pulls all reservations in lookback window (365d back + 180d forward)
        scheduler.add_job(
            _cloudbeds_sync_job,
            kwargs={"incremental": False},
            trigger=CronTrigger(hour=2, minute=0),
            id="nightly_cloudbeds_sync",
            replace_existing=True,
            executor="default",
        )

        # Daytime incremental Cloudbeds sync at 10:00am Vietnam time
        # Catches new reservations + modifications from the morning (fast — last 2 days only)
        scheduler.add_job(
            _cloudbeds_sync_job,
            trigger=CronTrigger(hour=10, minute=0),
            id="daytime_cloudbeds_sync_morning",
            replace_existing=True,
            executor="default",
        )

        # Nightly metrics recompute at 3:00am Vietnam time (after sync)
        # Includes full-month Cloudbeds Insights overlay
        scheduler.add_job(
            nightly_metrics_job,
            args=[SessionLocal],
            trigger=CronTrigger(hour=3, minute=0),
            id="nightly_metrics_compute",
            replace_existing=True,
            executor="default",
        )

        # Cloudbeds Insights sync at 9:00am and 2:00pm Vietnam time
        # Keeps OCC/ADR/RevPAR/Revenue fresh throughout the day
        # Revenue KPI table pulls actual revenue entirely from this Insights data
        scheduler.add_job(
            cloudbeds_insights_sync_job,
            args=[SessionLocal],
            trigger=CronTrigger(hour=9, minute=0),
            id="insights_sync_morning",
            replace_existing=True,
            executor="default",
        )
        scheduler.add_job(
            cloudbeds_insights_sync_job,
            args=[SessionLocal],
            trigger=CronTrigger(hour=14, minute=0),
            id="insights_sync_afternoon",
            replace_existing=True,
            executor="default",
        )

        # Daily unified Ads Platform sync at 6:00am Vietnam time.
        # One call replaces the legacy Meta Graph + Google Sheets pair; pulls
        # daily spend + ad metadata + budget + booking matches from
        # settings.ADS_PLATFORM_BASE_URL using X-API-Key auth.
        def _ads_sync_job():
            from app.services.ads_platform_sync import run_ads_platform_sync
            db = SessionLocal()
            try:
                result = run_ads_platform_sync(db)
                db.commit()
                logger.info(
                    "Ads Platform sync OK — daily=%s ads=%s budgets=%s matches=%s "
                    "(%.1fs, window=%s..%s)",
                    result["synced_daily_rows"], result["synced_ads"],
                    result["synced_budgets"], result["synced_booking_matches"],
                    result["duration_s"], result["date_from"], result["date_to"],
                )
            except Exception:
                db.rollback()
                logger.exception("Ads Platform sync job failed")
            finally:
                db.close()

        scheduler.add_job(
            _ads_sync_job,
            trigger=CronTrigger(hour=6, minute=0),
            id="daily_ads_sync",
            replace_existing=True,
            executor="default",
        )

        # Daily alert evaluation at 3:15am (after metrics, before verdict sync)
        def _alert_evaluation_job():
            from app.services.alert_engine import run_daily_alerts
            run_daily_alerts(SessionLocal)

        scheduler.add_job(
            _alert_evaluation_job,
            trigger=CronTrigger(hour=3, minute=15),
            id="daily_alert_evaluation",
            replace_existing=True,
            executor="default",
        )

        # Nightly combo performance sync at 3:30am (after metrics)
        def _verdict_sync_job():
            db = SessionLocal()
            try:
                synced = sync_combo_performance(db)
                derived = compute_derived_verdicts(db)
                logger.info("Verdict sync complete — %d combos synced, %d components updated", synced, derived)
            except Exception:
                logger.exception("Verdict sync job failed")
            finally:
                db.close()

        scheduler.add_job(
            _verdict_sync_job,
            trigger=CronTrigger(hour=3, minute=30),
            id="nightly_verdict_sync",
            replace_existing=True,
            executor="default",
        )

        # Nightly email stats aggregation at 4:00am (after verdict sync)
        def _email_stats_job():
            from app.services.email_stats import aggregate_email_stats
            from datetime import date, timedelta
            db = SessionLocal()
            try:
                count = aggregate_email_stats(
                    db,
                    date.today() - timedelta(days=7),
                    date.today(),
                )
                logger.info("Email stats aggregation complete — %d rows", count)
            except Exception:
                logger.exception("Email stats aggregation failed")
            finally:
                db.close()

        scheduler.add_job(
            _email_stats_job,
            trigger=CronTrigger(hour=4, minute=0),
            id="nightly_email_stats",
            replace_existing=True,
            executor="default",
        )

        # Daily marketing-budget actuals sync at 6:30am (after Ads Platform
        # daily sync at 06:00 — needs ads_performance up-to-date upstream).
        # Pulls per-month paid_ads + kol actuals into
        # marketing_budgets.cached_actual_vnd so Budget Planner reads serve
        # from local DB instead of round-tripping to upstream every request.
        def _marketing_budget_actuals_job():
            from app.services.budget_actuals_sync import (
                run_daily_marketing_actuals_job,
            )
            run_daily_marketing_actuals_job()

        scheduler.add_job(
            _marketing_budget_actuals_job,
            trigger=CronTrigger(hour=6, minute=30),
            id="daily_marketing_budget_actuals",
            replace_existing=True,
            executor="default",
        )

        # Daily GHL email stats sync at 5:00am (after aggregation)
        def _ghl_email_sync_job():
            from app.services.ghl_email_sync import sync_ghl_email_stats
            db = SessionLocal()
            try:
                count = sync_ghl_email_stats(db)
                logger.info("GHL email sync complete — %d workflows", count)
            except Exception:
                logger.exception("GHL email sync failed")
            finally:
                db.close()

        scheduler.add_job(
            _ghl_email_sync_job,
            trigger=CronTrigger(hour=5, minute=0),
            id="daily_ghl_email_sync",
            replace_existing=True,
            executor="default",
        )

        # Nightly Holiday Intelligence index refresh at 1:00am (before Cloudbeds sync)
        def _holiday_index_refresh_job():
            from app.services.holiday_intel import recompute_season_index
            db = SessionLocal()
            try:
                count = recompute_season_index(db)
                logger.info("Holiday index refresh complete — %d cells", count)
            except Exception:
                logger.exception("Holiday index refresh failed")
            finally:
                db.close()

        scheduler.add_job(
            _holiday_index_refresh_job,
            trigger=CronTrigger(hour=1, minute=0),
            id="nightly_holiday_index_refresh",
            replace_existing=True,
            executor="default",
        )

        # Heartbeat every 10 minutes — lightweight "I'm alive" log so that if
        # the server hangs again we can tell from the log exactly when it froze.
        def _heartbeat():
            logger.info("Scheduler heartbeat — server alive")

        scheduler.add_job(
            _heartbeat,
            trigger=IntervalTrigger(minutes=10),
            id="scheduler_heartbeat",
            replace_existing=True,
            executor="default",
        )

        scheduler.start()
        logger.info(
            "Scheduler started — "
            "Cloudbeds reservation sync at 02:00, 10:00 ICT, "
            "metrics compute (14-day lookback + next month) at 03:00 ICT, "
            "alert evaluation at 03:15 ICT, "
            "Ads Platform sync at 06:00 ICT, "
            "marketing-budget actuals at 06:30 ICT, "
            "Insights refresh (14-day lookback) at 09:00 & 14:00 ICT, "
            "verdict sync at 03:30 ICT, "
            "email stats at 04:00 ICT, "
            "GHL email sync at 05:00 ICT, "
            "holiday index refresh at 01:00 ICT"
        )

    @app.on_event("shutdown")
    async def stop_scheduler():
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
