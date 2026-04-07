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
ET  = pytz.timezone("America/New_York")  # DST 자동 반영 (EDT/EST)


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

    # 미국장 개장(09:30 ET) 1시간 전 = 08:30 ET (EDT/EST 자동 반영)
    scheduler.add_job(
        run_premarket,
        CronTrigger(hour=8, minute=30, timezone=ET),
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
