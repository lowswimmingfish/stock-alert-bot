#!/usr/bin/env python3
"""
Tavily 기반 고급 알림 모듈
  1. 애널리스트 레이팅 변경 알림  (매일 09:30 KST)
  2. 실적 발표 전날 컨센서스 브리핑 (매일 19:00 KST)
  3. 주간 포트폴리오 심층 분석     (일요일 20:00 KST)
"""

import logging
import time
import anthropic
import pandas as pd
import pytz
import yfinance as yf
from datetime import datetime, date, timedelta
from tavily import TavilyClient
from config_loader import load_config
from utils import send_telegram
import kis_api

KST = pytz.timezone("Asia/Seoul")
logger = logging.getLogger(__name__)


def _get_us_holdings(config):
    """미국 보유 종목 [{ticker, name}] 반환."""
    if kis_api.is_configured():
        try:
            raw = kis_api.get_us_balance_raw()
            return [{"ticker": h["ticker"], "name": h["ticker"]}
                    for h in raw.get("holdings", [])]
        except Exception:
            pass
    return [{"ticker": s["ticker"], "name": s["ticker"]}
            for s in config["portfolio"].get("us_stocks", [])]


def _tavily_search(client, query, max_results=5):
    try:
        return client.search(query, max_results=max_results, topic="news").get("results", [])
    except Exception as e:
        logger.warning(f"Tavily search error: {e}")
        return []


# ── 1. 애널리스트 레이팅 변경 알림 ────────────────────────────────────────────

def run_analyst_alerts():
    """보유 종목 애널리스트 업그레이드/다운그레이드/목표가 변경 알림."""
    config = load_config()
    tavily_key = config.get("tavily_api_key", "")
    if not tavily_key:
        return

    bot_token = config["telegram"]["bot_token"]
    chat_id   = config["telegram"]["chat_id"]
    tavily    = TavilyClient(api_key=tavily_key)
    claude    = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    holdings  = _get_us_holdings(config)
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    for h in holdings:
        ticker = h["ticker"]
        try:
            results = _tavily_search(
                tavily,
                f"{ticker} analyst rating upgrade downgrade price target {today_str[:7]}",
                max_results=5,
            )
            if not results:
                time.sleep(0.5)
                continue

            news_text = "\n".join(
                f"- {r.get('title','')}\n  {r.get('content','')[:150]}"
                for r in results
            )

            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{"role": "user", "content": f"""{ticker} 종목 애널리스트 뉴스야. \
오늘({today_str}) 또는 어제 새로운 레이팅 변경(업그레이드/다운그레이드) 또는 목표주가 변경이 있는지 판단해줘.

{news_text}

새 레이팅/목표가 변경 있으면: ALERT: [증권사명] [변경내용] (예: ALERT: Goldman Sachs 목표가 $200→$230 상향, BUY 유지)
없으면: NONE"""}],
            )
            result = resp.content[0].text.strip()

            if result.startswith("ALERT:"):
                summary = result.replace("ALERT:", "").strip()
                send_telegram(bot_token, chat_id,
                    f"<b>📊 애널리스트 레이팅 변경</b>\n"
                    f"종목: <b>{ticker}</b>\n"
                    f"시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n\n"
                    f"{summary}"
                )
                logger.info(f"Analyst alert: {ticker} - {summary}")

            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Analyst alert error {ticker}: {e}")


# ── 2. 실적 발표 전날 컨센서스 브리핑 ─────────────────────────────────────────

def run_earnings_consensus():
    """내일 실적 발표 예정 종목 있으면 컨센서스 브리핑 발송."""
    config    = load_config()
    tavily_key = config.get("tavily_api_key", "")
    if not tavily_key:
        return

    bot_token = config["telegram"]["bot_token"]
    chat_id   = config["telegram"]["chat_id"]
    tavily    = TavilyClient(api_key=tavily_key)
    claude    = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    holdings  = _get_us_holdings(config)

    tomorrow = date.today() + timedelta(days=1)
    # 금요일이면 월요일까지 포함 (주말에 발표 없으므로)
    check_dates = {tomorrow}
    if date.today().weekday() == 4:  # 금요일
        check_dates.add(tomorrow + timedelta(days=2))

    for h in holdings:
        ticker = h["ticker"]
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            # calendar는 DataFrame 또는 dict (yfinance 버전마다 다름)
            if isinstance(cal, pd.DataFrame):
                earnings_dates = cal["Earnings Date"].dropna().tolist() if "Earnings Date" in cal.columns else []
            elif isinstance(cal, dict):
                earnings_dates = cal.get("Earnings Date", [])
                if not isinstance(earnings_dates, list):
                    earnings_dates = [earnings_dates]
            else:
                continue

            upcoming = None
            for ed in earnings_dates:
                try:
                    ed_date = ed.date() if hasattr(ed, "date") else datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
                except Exception:
                    continue
                if ed_date in check_dates:
                    upcoming = ed_date
                    break
            if not upcoming:
                continue

            logger.info(f"Upcoming earnings: {ticker} on {upcoming}")

            quarter = f"Q{(upcoming.month - 1) // 3 + 1} {upcoming.year}"
            results = _tavily_search(
                tavily,
                f"{ticker} earnings {quarter} EPS revenue estimate consensus analyst",
                max_results=6,
            )
            news_text = "\n".join(
                f"- {r.get('title','')}\n  {r.get('content','')[:200]}"
                for r in results
            )

            resp = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=700,
                messages=[{"role": "user", "content": f"""\
{ticker} {quarter} 실적 발표가 내일({upcoming})이야.
아래 정보를 바탕으로 실적 발표 전 브리핑을 한국어로 작성해줘.

{news_text}

형식 (텔레그램 HTML):
<b>컨센서스 추정치</b>
• EPS: / 매출:

<b>핵심 관전 포인트</b>
(2~3가지)

<b>최근 시장 심리</b>
(1~2줄)

<b>리스크 요인</b>
(1~2가지)"""}],
            )
            briefing = resp.content[0].text.strip()

            send_telegram(bot_token, chat_id,
                f"<b>📅 실적 발표 전날 브리핑</b>\n"
                f"종목: <b>{ticker}</b>  |  발표일: {upcoming}\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"{briefing}"
            )
            logger.info(f"Earnings consensus sent: {ticker}")
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Earnings consensus error {ticker}: {e}")


# ── 3. 주간 포트폴리오 심층 분석 ──────────────────────────────────────────────

def run_weekly_deep_analysis():
    """일요일 저녁 — 이번 주 리뷰 + 다음 주 전망 종합 브리핑."""
    config    = load_config()
    tavily_key = config.get("tavily_api_key", "")
    if not tavily_key:
        return

    bot_token = config["telegram"]["bot_token"]
    chat_id   = config["telegram"]["chat_id"]
    tavily    = TavilyClient(api_key=tavily_key)
    claude    = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    holdings  = _get_us_holdings(config)
    now_str   = datetime.now(KST).strftime("%Y년 %m월 %d일")

    # 보유 종목 심층 검색
    stock_blocks = []
    for h in holdings:
        ticker = h["ticker"]
        results = _tavily_search(tavily, f"{ticker} stock weekly review analysis outlook", max_results=4)
        if results:
            block = f"[{ticker}]\n" + "\n".join(r.get("content", "")[:200] for r in results)
            stock_blocks.append(block)
        time.sleep(0.5)

    # 매크로 심층 검색
    macro_queries = [
        "US stock market next week outlook preview",
        "Federal Reserve monetary policy week ahead",
        "S&P500 technical analysis weekly forecast",
    ]
    macro_blocks = []
    for q in macro_queries:
        results = _tavily_search(tavily, q, max_results=3)
        for r in results:
            macro_blocks.append(r.get("content", "")[:200])
        time.sleep(0.5)

    context = "\n\n".join(stock_blocks + macro_blocks)

    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        messages=[{"role": "user", "content": f"""\
{now_str} 주간 포트폴리오 심층 분석을 작성해줘.
보유 종목: {', '.join(h['ticker'] for h in holdings)}

수집된 정보:
{context[:3500]}

형식 (텔레그램 HTML, 간결하고 실용적으로):

<b>📌 이번 주 총평</b>
(시장 전반 흐름 2~3줄)

<b>📦 보유 종목별 주간 리뷰</b>
(각 종목 핵심 이슈 1~2줄)

<b>📅 다음 주 주목 일정</b>
(실적·지표·이벤트 날짜 포함)

<b>⚠️ 리스크 요인</b>
(2~3가지)

<b>💡 기회 요인</b>
(2~3가지)

<b>🎯 한 줄 전략</b>"""}],
    )
    analysis = resp.content[0].text.strip()

    send_telegram(bot_token, chat_id,
        f"<b>📈 주간 포트폴리오 심층 분석</b>\n"
        f"{now_str} 주간\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{analysis}"
    )
    logger.info("Weekly deep analysis sent")
