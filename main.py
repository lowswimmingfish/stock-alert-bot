#!/usr/bin/env python3
"""Railway entry point - runs bot + scheduler in a single process."""

import logging
import time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")

KST = pytz.timezone("Asia/Seoul")


def run_daily_report():
    try:
        from stock_alert import main
        main()
    except Exception as e:
        logger.error(f"Daily report error: {e}")


def run_premarket():
    try:
        from premarket_alert import main
        main()
    except Exception as e:
        logger.error(f"Premarket alert error: {e}")


def run_news_monitor():
    try:
        from news_monitor import run
        run()
    except Exception as e:
        logger.error(f"News monitor error: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=KST)

    # 매일 오전 8시 (KST)
    scheduler.add_job(
        run_daily_report,
        CronTrigger(hour=8, minute=0, timezone=KST),
        id="daily_report",
        name="Daily Portfolio Report",
    )

    # 매일 오후 9시 30분 (KST) - 미장 개장 약 1시간 전
    scheduler.add_job(
        run_premarket,
        CronTrigger(hour=21, minute=30, timezone=KST),
        id="premarket",
        name="Pre-market Briefing",
    )

    # 10분마다 뉴스 모니터링
    scheduler.add_job(
        run_news_monitor,
        "interval",
        minutes=10,
        id="news_monitor",
        name="News Monitor",
    )

    scheduler.start()
    logger.info("Scheduler started (KST timezone)")
    return scheduler


if __name__ == "__main__":
    logger.info("Starting stock alert bot...")

    # 시작하자마자 뉴스 1회 실행
    run_news_monitor()

    scheduler = start_scheduler()

    # 봇 실행 (메인 스레드 블로킹)
    from bot import poll
    try:
        poll()
    except KeyboardInterrupt:
        scheduler.shutdown()
        logger.info("Stopped.")
