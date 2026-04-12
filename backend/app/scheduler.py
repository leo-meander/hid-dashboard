import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Ho_Chi_Minh")


def setup_scheduler(app):
    """Register scheduled jobs and attach lifecycle to FastAPI app."""

    @app.on_event("startup")
    async def start_scheduler():
        from app.services.cloudbeds import sync_all_branches
        from app.services.metrics_engine import nightly_metrics_job, cloudbeds_insights_sync_job
        from app.services.verdict_sync import sync_combo_performance, compute_derived_verdicts
        from app.database import SessionLocal

        # Nightly FULL Cloudbeds sync at 2:00am Vietnam time
        # Full sync pulls all reservations in lookback window (365d back + 180d forward)
        scheduler.add_job(
            sync_all_branches,
            kwargs={"incremental": False},
            trigger=CronTrigger(hour=2, minute=0),
            id="nightly_cloudbeds_sync",
            replace_existing=True,
        )

        # Daytime incremental Cloudbeds sync at 10:00am Vietnam time
        # Catches new reservations + modifications from the morning (fast — last 2 days only)
        scheduler.add_job(
            sync_all_branches,
            trigger=CronTrigger(hour=10, minute=0),
            id="daytime_cloudbeds_sync_morning",
            replace_existing=True,
        )

        # Nightly metrics recompute at 3:00am Vietnam time (after sync)
        # Includes full-month Cloudbeds Insights overlay
        scheduler.add_job(
            nightly_metrics_job,
            args=[SessionLocal],
            trigger=CronTrigger(hour=3, minute=0),
            id="nightly_metrics_compute",
            replace_existing=True,
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
        )
        scheduler.add_job(
            cloudbeds_insights_sync_job,
            args=[SessionLocal],
            trigger=CronTrigger(hour=14, minute=0),
            id="insights_sync_afternoon",
            replace_existing=True,
        )

        # Daily Meta + Google Ads sync at 6:00am Vietnam time
        # Pulls latest spend/performance data so Running/Stopped status stays accurate
        def _ads_sync_job():
            from app.config import settings
            from app.models.branch import Branch
            from app.models.ads import AdsPerformance
            from app.services import meta_ads as meta_service
            from app.services.google_sheets_ads import sync_google_ads_sheet
            from datetime import date, timedelta

            db = SessionLocal()
            try:
                branches = db.query(Branch).filter_by(is_active=True).all()
                date_from = (date.today() - timedelta(days=3)).isoformat()
                date_to = date.today().isoformat()

                # ── Meta Ads ──────────────────────────────────────────────
                META_CONFIG = {
                    "saigon": ("META_ACCESS_TOKEN_SAIGON", "META_AD_ACCOUNT_SAIGON"),
                    "1948":   ("META_ACCESS_TOKEN_1948",   "META_AD_ACCOUNT_1948"),
                    "taipei": ("META_ACCESS_TOKEN_TAIPEI", "META_AD_ACCOUNT_TAIPEI"),
                    "osaka":  ("META_ACCESS_TOKEN_OSAKA",  "META_AD_ACCOUNT_OSAKA"),
                    "oani":   ("META_ACCESS_TOKEN_OANI",   "META_AD_ACCOUNT_OANI"),
                }

                for branch in branches:
                    key = branch.name.lower().replace("meander ", "").strip()
                    token = account_id = ""
                    for suffix, (tok_field, acc_field) in META_CONFIG.items():
                        if suffix in key:
                            token = getattr(settings, tok_field, "") or ""
                            account_id = getattr(settings, acc_field, "") or ""
                            break

                    if not token or not account_id:
                        continue

                    try:
                        ads = meta_service.sync_ads(
                            token, account_id,
                            date_from=date_from, date_to=date_to,
                        )
                        created = updated = 0
                        for ad in ads:
                            existing = (
                                db.query(AdsPerformance)
                                .filter_by(meta_ad_id=ad["meta_ad_id"])
                                .first()
                            )
                            if existing:
                                existing.cost_native = ad["spend_vnd"]
                                existing.cost_vnd = ad["spend_vnd"]
                                existing.impressions = ad["impressions"]
                                existing.clicks = ad["clicks"]
                                existing.leads = ad["leads"]
                                existing.bookings = ad.get("bookings", 0) or 0
                                existing.revenue_native = ad.get("revenue", 0.0) or 0.0
                                if ad["date_start"]:
                                    existing.date_from = date.fromisoformat(ad["date_start"])
                                if ad["date_stop"]:
                                    existing.date_to = date.fromisoformat(ad["date_stop"])
                                existing.campaign_name = ad["campaign_name"]
                                existing.adset_name = ad["adset_name"]
                                existing.ad_name = ad["ad_name"]
                                existing.target_country = ad["target_country"]
                                existing.target_audience = ad["target_audience"]
                                existing.funnel_stage = ad["funnel_stage"]
                                existing.channel = "Meta"
                                updated += 1
                            else:
                                row = AdsPerformance(
                                    branch_id=branch.id,
                                    meta_ad_id=ad["meta_ad_id"],
                                    meta_campaign_id=ad["meta_campaign_id"],
                                    campaign_name=ad["campaign_name"],
                                    adset_name=ad["adset_name"],
                                    ad_name=ad["ad_name"],
                                    channel="Meta",
                                    target_country=ad["target_country"],
                                    target_audience=ad["target_audience"],
                                    funnel_stage=ad["funnel_stage"],
                                    pic=ad.get("pic", ""),
                                    ad_body=ad.get("ad_body", ""),
                                    cost_native=ad["spend_vnd"],
                                    cost_vnd=ad["spend_vnd"],
                                    impressions=ad["impressions"],
                                    clicks=ad["clicks"],
                                    leads=ad["leads"],
                                    bookings=ad.get("bookings", 0) or 0,
                                    revenue_native=ad.get("revenue", 0.0) or 0.0,
                                    date_from=date.fromisoformat(ad["date_start"]) if ad.get("date_start") else None,
                                    date_to=date.fromisoformat(ad["date_stop"]) if ad.get("date_stop") else None,
                                )
                                db.add(row)
                                created += 1
                        db.commit()
                        logger.info("Meta sync OK branch=%s — %d created, %d updated", branch.name, created, updated)
                    except Exception as e:
                        db.rollback()
                        logger.warning("Meta sync FAIL branch=%s: %s", branch.name, e)

                # ── Google Ads (via Sheets) ───────────────────────────────
                GOOGLE_SHEET_MAP = {
                    "11111111-1111-1111-1111-111111111101": "GOOGLE_SHEET_TAIPEI",
                    "11111111-1111-1111-1111-111111111102": "GOOGLE_SHEET_SAIGON",
                    "11111111-1111-1111-1111-111111111103": "GOOGLE_SHEET_1948",
                    "11111111-1111-1111-1111-111111111104": "GOOGLE_SHEET_OANI",
                    "11111111-1111-1111-1111-111111111105": "GOOGLE_SHEET_OSAKA",
                }
                client_id = getattr(settings, "GOOGLE_CLIENT_ID", "") or ""
                client_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", "") or ""
                refresh_token_g = getattr(settings, "GOOGLE_REFRESH_TOKEN", "") or ""

                if client_id and refresh_token_g:
                    df = date.today() - timedelta(days=3)
                    dt = date.today()
                    for branch in branches:
                        sheet_attr = GOOGLE_SHEET_MAP.get(str(branch.id))
                        if not sheet_attr:
                            continue
                        spreadsheet_id = getattr(settings, sheet_attr, "") or ""
                        if not spreadsheet_id:
                            continue
                        try:
                            data = sync_google_ads_sheet(
                                branch_id=str(branch.id),
                                branch_name=branch.name,
                                spreadsheet_id=spreadsheet_id,
                                currency=branch.currency or "VND",
                                client_id=client_id,
                                client_secret=client_secret,
                                refresh_token=refresh_token_g,
                                date_from=df,
                                date_to=dt,
                            )
                            created = updated = 0
                            for row in data["rows"]:
                                existing = (
                                    db.query(AdsPerformance)
                                    .filter_by(
                                        branch_id=branch.id,
                                        channel="Google",
                                        campaign_name=row["campaign_name"],
                                        date_from=row["date_from"],
                                    )
                                    .first()
                                )
                                if existing:
                                    for k, v in row.items():
                                        if k != "branch_id" and hasattr(existing, k):
                                            setattr(existing, k, v)
                                    updated += 1
                                else:
                                    db.add(AdsPerformance(**row))
                                    created += 1
                            db.commit()
                            logger.info("Google Ads sync OK branch=%s — %d created, %d updated", branch.name, created, updated)
                        except Exception as e:
                            db.rollback()
                            logger.warning("Google Ads sync FAIL branch=%s: %s", branch.name, e)

            except Exception:
                logger.exception("Ads sync job failed")
            finally:
                db.close()

        scheduler.add_job(
            _ads_sync_job,
            trigger=CronTrigger(hour=6, minute=0),
            id="daily_ads_sync",
            replace_existing=True,
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
        )

        scheduler.start()
        logger.info(
            "Scheduler started — "
            "Cloudbeds reservation sync at 02:00, 10:00 ICT, "
            "metrics compute (14-day lookback + next month) at 03:00 ICT, "
            "alert evaluation at 03:15 ICT, "
            "Ads sync (Meta + Google) at 06:00 ICT, "
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
