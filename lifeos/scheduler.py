from apscheduler.schedulers.background import BackgroundScheduler

from lifeos.config import settings
from lifeos.db import SessionLocal
from lifeos.agent import run_job

scheduler = BackgroundScheduler(timezone=settings.default_timezone)


def _run(job_name: str) -> None:
    db = SessionLocal()
    try:
        run_job(db, job_name)
    finally:
        db.close()


def start_scheduler() -> None:
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.add_job(lambda: _run("ingest"), "interval", minutes=5, id="ingest", replace_existing=True)
    scheduler.add_job(lambda: _run("overview_refresh"), "interval", hours=1, id="overview_refresh", replace_existing=True)
    scheduler.add_job(lambda: _run("summary_rollup"), "cron", hour=3, minute=15, id="summary_rollup", replace_existing=True)
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
