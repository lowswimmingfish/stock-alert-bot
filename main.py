#!/usr/bin/env python3
"""Railway entry point - runs bot + scheduler in a single process."""

import logging
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
    """매일 장 마감 후 포트폴리오 스냅샷 저장 (주말·공휴일 스킵)."""
    try:
        import datetime as dt
        from utils import is_us_market_holiday
        now_et = dt.datetime.now(ET)
        if now_et.weekday() >= 5:  # 토(5)/일(6) — 시장 휴장
            return
        if is_us_market_holiday(now_et.date()):
            logger.info(f"Snapshot skipped: NYSE holiday ({now_et.date()})")
            return
        from portfolio_tracker import take_snapshot
        snap = take_snapshot()
        logger.info(f"Snapshot OK: {snap['total_krw']:,.0f} KRW")
    except Exception as e:
        logger.error(f"Snapshot error: {e}")


def run_price_alerts():
    """가격 알림 체크 (미국장 시간 중 5분마다)."""
    try:
        import datetime as dt
        now_et = dt.datetime.now(ET)
        if now_et.weekday() >= 5:  # 주말 스킵
            return
        t = now_et.hour * 60 + now_et.minute
        in_market = (9 * 60 + 30) <= t <= (16 * 60)
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
        import datetime as dt
        from utils import is_us_market_holiday
        if is_us_market_holiday(dt.datetime.now(ET).date()):
            logger.info("Event alerts skipped: NYSE holiday")
            return
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


def run_analyst_alerts():
    try:
        import datetime as dt
        from utils import is_us_market_holiday
        if is_us_market_holiday(dt.datetime.now(ET).date()):
            logger.info("Analyst alerts skipped: NYSE holiday")
            return
        from tavily_alerts import run_analyst_alerts as _fn
        _fn()
    except Exception as e:
        logger.error(f"Analyst alert error: {e}")


def run_earnings_consensus():
    try:
        import datetime as dt
        from utils import is_us_market_holiday
        if is_us_market_holiday(dt.datetime.now(ET).date()):
            logger.info("Earnings consensus skipped: NYSE holiday")
            return
        from tavily_alerts import run_earnings_consensus as _fn
        _fn()
    except Exception as e:
        logger.error(f"Earnings consensus error: {e}")


def run_weekly_analysis():
    try:
        from tavily_alerts import run_weekly_deep_analysis as _fn
        _fn()
    except Exception as e:
        logger.error(f"Weekly analysis error: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=KST)

    # 평일 08:00 KST — 국장(KRX 09:00) 1시간 전
    scheduler.add_job(
        run_daily_report,
        CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone=KST),
        id="daily_report",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,  # 재시작 후 60초 이내 misfire만 실행, 그 이후는 스킵
    )

    # 평일 08:30 ET — 미장(NYSE 09:30 ET) 1시간 전 (EDT/EST DST 자동 반영)
    scheduler.add_job(
        run_premarket,
        CronTrigger(hour=8, minute=30, day_of_week="mon-fri", timezone=ET),
        id="premarket",
        name="Pre-market Briefing (평일 ET 08:30)",
        max_instances=1,
        coalesce=True,
    )

    # 10분마다 뉴스 모니터링
    scheduler.add_job(
        run_news_monitor,
        "interval",
        minutes=10,
        id="news_monitor",
        name="News Monitor",
    )

    # 평일 06:05 KST — KRX(15:30 KST)·NYSE(~05:00-06:00 KST) 모두 마감 후 당일 종가 스냅샷
    scheduler.add_job(
        run_snapshot,
        CronTrigger(hour=6, minute=5, day_of_week="mon-fri", timezone=KST),
        id="snapshot",
        name="Portfolio Snapshot (평일 KST 06:05)",
    )

    # 5분마다 가격 알림 체크 (미국 장중에만 실제 동작)
    scheduler.add_job(
        run_price_alerts,
        "interval",
        minutes=5,
        id="price_alerts",
        name="Price Alert Check",
    )

    # 평일 09:00 KST 이벤트 알림 (실적·배당·급등락)
    scheduler.add_job(
        run_event_alerts,
        CronTrigger(hour=9, minute=0, day_of_week="mon-fri", timezone=KST),
        id="event_alerts",
        name="Event Alert Check (평일)",
    )

    # 평일 09:30 KST 애널리스트 레이팅 변경 알림
    scheduler.add_job(
        run_analyst_alerts,
        CronTrigger(hour=9, minute=30, day_of_week="mon-fri", timezone=KST),
        id="analyst_alerts",
        name="Analyst Rating Alerts (평일)",
    )

    # 평일 19:00 KST 실적 발표 전날 컨센서스 브리핑
    scheduler.add_job(
        run_earnings_consensus,
        CronTrigger(hour=19, minute=0, day_of_week="mon-fri", timezone=KST),
        id="earnings_consensus",
        name="Earnings Consensus Briefing (평일)",
    )

    # 일요일 20:00 KST 주간 포트폴리오 심층 분석
    scheduler.add_job(
        run_weekly_analysis,
        CronTrigger(hour=20, minute=0, day_of_week="sun", timezone=KST),
        id="weekly_analysis",
        name="Weekly Deep Analysis",
    )

    scheduler.start()
    logger.info("Scheduler started (KST timezone)")
    return scheduler


if __name__ == "__main__":
    logger.info("Starting stock alert bot...")

    # 스냅샷 데이터 소급 생성 (CAPM 등 분석을 위해 90일치 backfill)
    try:
        from portfolio_tracker import backfill_snapshots, _load_snapshots
        from datetime import date, timedelta
        data = _load_snapshots()
        # 거래일(평일) 스냅샷만 카운트
        trading_days = sum(
            1 for d in data
            if date.fromisoformat(d).weekday() < 5
        )
        if trading_days < 10:
            added = backfill_snapshots(days=90)
            logger.info(f"Backfill: {added}일 소급 생성 (기존 거래일 {trading_days}일)")
        else:
            logger.info(f"Backfill 스킵: 기존 거래일 {trading_days}일치 충분")
    except Exception as e:
        logger.error(f"Backfill error: {e}")

    # 이상값 스냅샷 정리 (주말 장 휴장 시 가격 0 저장 문제 등)
    try:
        from portfolio_tracker import clean_anomalous_snapshots
        removed = clean_anomalous_snapshots()
        if removed:
            logger.info(f"Anomalous snapshots removed: {removed}개")
    except Exception as e:
        logger.error(f"Snapshot cleanup error: {e}")

    # 뉴스는 스케줄러가 10분 후 첫 실행 — 배포 직후 즉시 실행 제거
    # (재배포 반복 시 매번 즉시 실행되어 알림 폭탄 유발)

    scheduler = start_scheduler()

    # 봇 실행 (메인 스레드 블로킹)
    from bot import poll
    try:
        poll()
    except KeyboardInterrupt:
        scheduler.shutdown()
        logger.info("Stopped.")
