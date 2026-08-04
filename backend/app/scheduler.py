import logging

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(
    timezone="Asia/Ho_Chi_Minh",
    executors={
        "default": ThreadPoolExecutor(max_workers=8),
        "asyncio": AsyncIOExecutor(),
    },
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
)


def start_scheduler() -> None:
    """Register jobs and start them from FastAPI's lifespan context.

    FastAPI bypasses ``@app.on_event`` startup handlers when a lifespan handler
    is configured. Keeping scheduler lifecycle explicit prevents a deployment
    from silently coming up without the reservation poller.
    """
    if scheduler.running:
        return

    from app.routers.webhooks import poll_new_reservations

    def heartbeat() -> None:
        logger.info("Scheduler heartbeat - server alive")

    def purge_webhook_events() -> None:
        from app.services import webhook_log
        webhook_log.purge_old()

    scheduler.add_job(
        heartbeat,
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
    scheduler.add_job(
        purge_webhook_events,
        trigger=CronTrigger(hour=4, minute=0),
        id="webhook_events_purge",
        replace_existing=True,
        executor="default",
    )
    scheduler.start()
    logger.info("Scheduler started - polling Cloudbeds every 10 min")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
