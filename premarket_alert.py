#!/usr/bin/env python3
"""Pre-market briefing - sent before US market opens with overnight news, futures, and portfolio analysis."""

import json
import requests
import anthropic
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta
from config_loader import load_config
import kis_api


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    while text:
        chunk = text[:4000]
        text = text[4000:]
        requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"})


def get_futures():
    """Fetch major US futures and overnight data."""
    futures = {
        "S&P500 선물": "ES=F",
        "나스닥 선물": "NQ=F",
        "다우 선물": "YM=F",
        "금": "GC=F",
        "원유(WTI)": "CL=F",
        "달러인덱스": "DX-Y.NYB",
    }
    results = {}
    for name, ticker in futures.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                curr = hist["Close"].iloc[-1]
                pct = (curr - prev) / prev * 100
                results[name] = {"price": round(curr, 2), "change_pct": round(pct, 2)}
            elif len(hist) == 1:
                curr = hist["Close"].iloc[-1]
                results[name] = {"price": round(curr, 2), "change_pct": 0}
        except Exception:
            pass
    return results


def get_exchange_rate():
    try:
        t = yf.Ticker("USDKRW=X")
        hist = t.history(period="2d")
        if len(hist) >= 2:
            prev = hist["Close"].iloc[-2]
            curr = hist["Close"].iloc[-1]
            pct = (curr - prev) / prev * 100
            return {"rate": round(curr, 2), "change_pct": round(pct, 2)}
    except Exception:
        pass
    return {"rate": 0, "change_pct": 0}


def get_us_stock_premarket(tickers):
    """Get after-hours / pre-market prices for US stocks."""
    results = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            hist = t.history(period="5d")
            if len(hist) >= 1:
                last_close = hist["Close"].iloc[-1]
                results[ticker] = {
                    "last_close": round(last_close, 2),
                }
        except Exception:
            pass
    return results


def get_overnight_news(tickers):
    """Fetch overnight news headlines for portfolio tickers."""
    all_news = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            news = t.news or []
            for n in news[:3]:
                content = n.get("content", {})
                if isinstance(content, dict):
                    title = content.get("title", "")
                    pub_date = content.get("pubDate", "")
                else:
                    title = n.get("title", "")
                    pub_date = ""
                if title:
                    all_news.append(f"[{ticker}] {title}")
        except Exception:
            pass
    return all_news


def build_premarket_briefing(config):
    """Build pre-market briefing using Claude with real-time data."""
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    # KIS 실계좌에서 미국 보유 종목 가져오기
    us_holdings = []
    us_tickers = []
    if kis_api.is_configured():
        raw = kis_api.get_us_balance_raw()
        us_holdings = raw.get("holdings", [])
        us_tickers = [h["ticker"] for h in us_holdings]
    else:
        # fallback: config 수동 데이터
        us_tickers = [s["ticker"] for s in config["portfolio"].get("us_stocks", [])]

    # Gather data
    futures = get_futures()
    fx = get_exchange_rate()
    us_data = get_us_stock_premarket(us_tickers)
    news_headlines = get_overnight_news(us_tickers)

    # Build data context
    futures_text = "\n".join([
        f"- {name}: {d['price']} ({'+' if d['change_pct'] >= 0 else ''}{d['change_pct']}%)"
        for name, d in futures.items()
    ])

    if us_holdings:
        holdings_text = "\n".join([
            f"- {h['ticker']}: {h['qty']}주, 평단 ${h['avg_price']:.2f}, "
            f"현재가 ${h['curr_price']:.2f}, 손익 ${h['profit']:+.2f} ({h['profit_pct']:+.1f}%), "
            f"직전종가 ${us_data.get(h['ticker'], {}).get('last_close', 'N/A')}"
            for h in us_holdings
        ])
    else:
        holdings_text = "보유 종목 없음"

    news_text = "\n".join(news_headlines[:15]) if news_headlines else "최신 뉴스 없음"

    now = datetime.now()
    prompt = f"""지금은 {now.strftime('%Y-%m-%d %H:%M')} (한국시간)이고, 미국 증시 개장 약 1시간 전이야.
아래 데이터를 바탕으로 오늘 미장 개장 전 브리핑을 한국어로 작성해줘.

[선물 / 매크로]
{futures_text}

USD/KRW: {fx['rate']} ({'+' if fx['change_pct'] >= 0 else ''}{fx['change_pct']}%)

[내 미국 보유 종목]
{holdings_text}

[최근 뉴스 헤드라인]
{news_text}

다음 형식으로 작성해줘 (텔레그램 HTML 형식, <b>태그 사용):

1. 오늘의 전체 분위기 (한 줄)
2. 주요 선물 흐름 요약
3. 내 보유 종목별 오늘 주목할 점
4. 오늘 조심해야 할 리스크 요인
5. 한 줄 총평

간결하고 실용적으로, 투자 판단에 도움이 되는 내용으로 써줘."""

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    briefing = resp.content[0].text

    header = (
        f"<b>미장 개장 전 브리핑</b>\n"
        f"{now.strftime('%Y-%m-%d %H:%M')} | 개장까지 약 1시간\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
    )

    return header + briefing


def main():
    # Mac 절전 후 뒤늦게 실행될 경우 무시 (21:00~22:30 범위 밖이면 스킵)
    hour = datetime.now().hour
    minute = datetime.now().minute
    in_window = (hour == 21) or (hour == 22 and minute <= 30)
    if not in_window:
        print(f"Skipped: outside allowed window (current: {hour:02d}:{minute:02d})")
        return

    config = load_config()

    # 1. 일일 포트폴리오 리포트 먼저 전송
    try:
        from stock_alert import build_message
        report_msg = build_message(config)
        send_telegram(config["telegram"]["bot_token"], config["telegram"]["chat_id"], report_msg)
        print(f"Pre-market report sent at {datetime.now()}")
    except Exception as e:
        print(f"Report error: {e}")

    # 2. 미장 개장 전 브리핑 전송
    msg = build_premarket_briefing(config)
    send_telegram(config["telegram"]["bot_token"], config["telegram"]["chat_id"], msg)
    print(f"Pre-market briefing sent at {datetime.now()}")


if __name__ == "__main__":
    main()
