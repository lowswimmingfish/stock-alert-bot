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


def run_snapshot():
    """매일 장 마감 후 포트폴리오 스냅샷 저장."""
    try:
        from portfolio_tracker import take_snapshot
        snap = take_snapshot()
        logger.info(f"Snapshot OK: {snap['total_krw']:,.0f} KRW")
    except Exception as e:
        logger.error(f"Snapshot error: {e}")


def run_price_alerts():
    """가격 알림 체크 (미국장 시간 중 5분마다)."""
    try:
        now_et = time.gmtime()  # UTC
        # ET = UTC-4(EDT) or UTC-5(EST). 간단히 UTC 13:30~20:00 = ET 09:30~16:00
        import datetime as dt
        now_utc = dt.datetime.utcnow()
        h, m = now_utc.hour, now_utc.minute
        in_market = (13 * 60 + 30) <= (h * 60 + m) <= (20 * 60)
        if not in_market:
            return

        from config_loader import load_config
        import requests as req
        config    = load_config()
        bot_token = config["telegram"]["bot_token"]
        chat_id   = config["telegram"]["chat_id"]

        def send_fn(msg):
            req.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            )

        from alert_manager import check_price_alerts
        n = check_price_alerts(send_fn)
        if n:
            logger.info(f"Price alerts fired: {n}")
    except Exception as e:
        logger.error(f"Price alert check error: {e}")


def run_event_alerts():
    """실적·배당·급등락 이벤트 알림 (매일 1회)."""
    try:
        from config_loader import load_config
        import requests as req
        import kis_api
        config    = load_config()
        bot_token = config["telegram"]["bot_token"]
        chat_id   = config["telegram"]["chat_id"]

        def send_fn(msg):
            req.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            )

        # 보유 종목 목록 가져오기
        holdings = []
        if kis_api.is_configured():
            try:
                us_raw = kis_api.get_us_balance_raw()
                holdings += [
                    {"ticker": h["ticker"], "qty": h["qty"], "avg_price": h["avg_price"]}
                    for h in us_raw.get("holdings", [])
                ]
            except Exception:
                pass
        else:
            for s in config["portfolio"].get("us_stocks", []):
                holdings.append({"ticker": s["ticker"], "qty": s["shares"], "avg_price": s["avg_price"]})

        from alert_manager import (
            check_earnings_alerts, check_dividend_alerts, check_daily_change_alerts,
        )
        check_earnings_alerts(holdings, send_fn)
        check_dividend_alerts(holdings, send_fn)
        check_daily_change_alerts(holdings, send_fn)
    except Exception as e:
        logger.error(f"Event alert error: {e}")


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

    # 매일 22:00 KST (미국 장 마감 후) 포트폴리오 스냅샷
    scheduler.add_job(
        run_snapshot,
        CronTrigger(hour=22, minute=0, timezone=KST),
        id="snapshot",
        name="Portfolio Snapshot",
    )

    # 5분마다 가격 알림 체크 (미국 장중에만 실제 동작)
    scheduler.add_job(
        run_price_alerts,
        "interval",
        minutes=5,
        id="price_alerts",
        name="Price Alert Check",
    )

    # 매일 09:00 KST 이벤트 알림 (실적·배당·급등락)
    scheduler.add_job(
        run_event_alerts,
        CronTrigger(hour=9, minute=0, timezone=KST),
        id="event_alerts",
        name="Event Alert Check",
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
