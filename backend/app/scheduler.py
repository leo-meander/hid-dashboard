import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# All scheduled syncs now run on GitHub Actions (cron) hitting the public sync
# endpoints with `X-Sync-Token`. APScheduler is kept only for the heartbeat
# log so we can tell from logs that the FastAPI process is alive.
scheduler = AsyncIOScheduler(
    timezone="Asia/Ho_Chi_Minh",
    executors={
        "default": ThreadPoolExecutor(max_workers=8),
        "asyncio": AsyncIOExecutor(),
    },
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 600,
    },
)


def setup_scheduler(app):
    """Register scheduled jobs and attach scheduler lifecycle to FastAPI."""

    @app.on_event("startup")
    async def start_scheduler():
        from app.routers.webhooks import poll_new_reservations

        def _heartbeat():
            logger.info("Scheduler heartbeat — server alive")

        scheduler.add_job(
            _heartbeat,
            trigger=IntervalTrigger(minutes=10),
            id="scheduler_heartbeat",
            replace_existing=True,
            executor="default",
        )

        scheduler.add_job(
            poll_new_reservations,
            trigger=IntervalTrigger(minutes=10),
            id="cloudbeds_reservation_poll",
            replace_existing=True,
            executor="default",
        )

        # Webhook monitor history is a debugging aid, not a record — prune it
        # so the table can't grow without bound.
        def _purge_webhook_events():
            from app.services import webhook_log
            webhook_log.purge_old()

        scheduler.add_job(
            _purge_webhook_events,
            trigger=CronTrigger(hour=4, minute=0),
            id="webhook_events_purge",
            replace_existing=True,
            executor="default",
        )

        scheduler.start()
        logger.info("Scheduler started — polling Cloudbeds every 10 min")

    @app.on_event("shutdown")
    async def stop_scheduler():
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
