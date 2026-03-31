#!/usr/bin/env python3
"""Daily stock portfolio alert bot - sends Telegram messages with portfolio status and market overview."""

import json
import requests
import yfinance as yf
from pykrx import stock as pykrx_stock
from datetime import datetime, timedelta
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    })
    return resp.json()


def get_us_stock_data(tickers):
    """Fetch current US stock prices and daily changes."""
    results = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                current = hist["Close"].iloc[-1]
                change_pct = (current - prev_close) / prev_close * 100
            elif len(hist) == 1:
                current = hist["Close"].iloc[-1]
                change_pct = 0
            else:
                continue
            results[ticker] = {
                "price": round(current, 2),
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            results[ticker] = {"price": 0, "change_pct": 0, "error": str(e)}
    return results


def get_kr_stock_data(tickers):
    """Fetch current Korean stock prices and daily changes."""
    results = {}
    today = datetime.now()
    # Try last 5 business days to find valid trading days
    for ticker in tickers:
        try:
            end = today.strftime("%Y%m%d")
            start = (today - timedelta(days=10)).strftime("%Y%m%d")
            df = pykrx_stock.get_market_ohlcv(start, end, ticker)
            if len(df) >= 2:
                prev_close = df["종가"].iloc[-2]
                current = df["종가"].iloc[-1]
                change_pct = (current - prev_close) / prev_close * 100
            elif len(df) == 1:
                current = df["종가"].iloc[-1]
                change_pct = 0
            else:
                continue
            results[ticker] = {
                "price": int(current),
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            results[ticker] = {"price": 0, "change_pct": 0, "error": str(e)}
    return results


def get_market_indices():
    """Fetch major market indices."""
    indices = {
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW": "^DJI",
        "KOSPI": "^KS11",
        "KOSDAQ": "^KQ11",
    }
    results = {}
    for name, ticker in indices.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                curr = hist["Close"].iloc[-1]
                change_pct = (curr - prev) / prev * 100
                results[name] = {
                    "price": round(curr, 2),
                    "change_pct": round(change_pct, 2),
                }
        except Exception:
            pass
    return results


def get_exchange_rate():
    """Fetch USD/KRW exchange rate."""
    try:
        t = yf.Ticker("USDKRW=X")
        hist = t.history(period="2d")
        if len(hist) >= 1:
            rate = hist["Close"].iloc[-1]
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                change_pct = (rate - prev) / prev * 100
            else:
                change_pct = 0
            return {"rate": round(rate, 2), "change_pct": round(change_pct, 2)}
    except Exception:
        pass
    return {"rate": 0, "change_pct": 0}


def format_change(pct):
    if pct > 0:
        return f"+{pct}%"
    return f"{pct}%"


def format_number(n):
    if isinstance(n, float):
        return f"{n:,.2f}"
    return f"{n:,}"


def build_message(config):
    portfolio = config["portfolio"]
    now = datetime.now()

    # Gather data
    us_tickers = [s["ticker"] for s in portfolio["us_stocks"]]
    kr_tickers = [s["ticker"] for s in portfolio["kr_stocks"]]

    us_data = get_us_stock_data(us_tickers)
    kr_data = get_kr_stock_data(kr_tickers)
    indices = get_market_indices()
    fx = get_exchange_rate()

    # Build message
    lines = []
    lines.append(f"<b>Daily Stock Report</b>")
    lines.append(f"{now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # Exchange rate
    lines.append(f"<b>USD/KRW:</b> {format_number(fx['rate'])}원 ({format_change(fx['change_pct'])})")
    lines.append("")

    # Market indices
    lines.append("<b>Market Overview</b>")
    for name, data in indices.items():
        emoji = "🔴" if data["change_pct"] < 0 else "🟢"
        lines.append(f"  {emoji} {name}: {format_number(data['price'])} ({format_change(data['change_pct'])})")
    lines.append("")

    # US Portfolio
    total_us_invested = 0
    total_us_current = 0
    lines.append("<b>US Stocks</b>")
    for s in portfolio["us_stocks"]:
        ticker = s["ticker"]
        data = us_data.get(ticker, {})
        price = data.get("price", 0)
        change = data.get("change_pct", 0)
        invested = s["avg_price"] * s["shares"]
        current_val = price * s["shares"]
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested else 0
        total_us_invested += invested
        total_us_current += current_val

        emoji = "🔴" if change < 0 else "🟢"
        pnl_emoji = "📉" if pnl < 0 else "📈"
        lines.append(f"  {emoji} <b>{ticker}</b>: ${format_number(price)} ({format_change(change)})")
        lines.append(f"     {s['shares']}주 | 평단 ${format_number(s['avg_price'])}")
        lines.append(f"     {pnl_emoji} 손익: ${format_number(round(pnl, 2))} ({format_change(round(pnl_pct, 1))})")
    lines.append("")

    us_total_pnl = total_us_current - total_us_invested
    us_total_pnl_pct = (us_total_pnl / total_us_invested * 100) if total_us_invested else 0
    lines.append(f"  <b>US 합계:</b> ${format_number(round(total_us_current, 2))}")
    lines.append(f"  투자금: ${format_number(round(total_us_invested, 2))}")
    pnl_emoji = "📉" if us_total_pnl < 0 else "📈"
    lines.append(f"  {pnl_emoji} 총 손익: ${format_number(round(us_total_pnl, 2))} ({format_change(round(us_total_pnl_pct, 1))})")
    lines.append("")

    # KR Portfolio
    total_kr_invested = 0
    total_kr_current = 0
    lines.append("<b>KR Stocks</b>")
    for s in portfolio["kr_stocks"]:
        ticker = s["ticker"]
        name = s.get("name", ticker)
        data = kr_data.get(ticker, {})
        price = data.get("price", 0)
        change = data.get("change_pct", 0)
        invested = s["avg_price"] * s["shares"]
        current_val = price * s["shares"]
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested else 0
        total_kr_invested += invested
        total_kr_current += current_val

        emoji = "🔴" if change < 0 else "🟢"
        pnl_emoji = "📉" if pnl < 0 else "📈"
        lines.append(f"  {emoji} <b>{name}</b>: {format_number(price)}원 ({format_change(change)})")
        lines.append(f"     {s['shares']}주 | 평단 {format_number(s['avg_price'])}원")
        lines.append(f"     {pnl_emoji} 손익: {format_number(round(pnl))}원 ({format_change(round(pnl_pct, 1))})")
    lines.append("")

    kr_total_pnl = total_kr_current - total_kr_invested
    kr_total_pnl_pct = (kr_total_pnl / total_kr_invested * 100) if total_kr_invested else 0
    lines.append(f"  <b>KR 합계:</b> {format_number(round(total_kr_current))}원")
    lines.append(f"  투자금: {format_number(round(total_kr_invested))}원")
    pnl_emoji = "📉" if kr_total_pnl < 0 else "📈"
    lines.append(f"  {pnl_emoji} 총 손익: {format_number(round(kr_total_pnl))}원 ({format_change(round(kr_total_pnl_pct, 1))})")
    lines.append("")

    # Total portfolio in KRW
    us_in_krw = total_us_current * fx["rate"]
    us_invested_krw = total_us_invested * fx["rate"]
    total_krw = us_in_krw + total_kr_current
    total_invested_krw = us_invested_krw + total_kr_invested
    total_pnl_krw = total_krw - total_invested_krw
    total_pnl_pct = (total_pnl_krw / total_invested_krw * 100) if total_invested_krw else 0

    lines.append("<b>Total Portfolio (KRW)</b>")
    lines.append(f"  총 평가금: {format_number(round(total_krw))}원")
    lines.append(f"  총 투자금: {format_number(round(total_invested_krw))}원")
    pnl_emoji = "📉" if total_pnl_krw < 0 else "📈"
    lines.append(f"  {pnl_emoji} 총 손익: {format_number(round(total_pnl_krw))}원 ({format_change(round(total_pnl_pct, 1))})")

    return "\n".join(lines)


def main():
    config = load_config()
    msg = build_message(config)
    result = send_telegram(
        config["telegram"]["bot_token"],
        config["telegram"]["chat_id"],
        msg,
    )
    if result.get("ok"):
        print("Alert sent successfully!")
    else:
        print(f"Failed to send: {result}")


if __name__ == "__main__":
    main()
