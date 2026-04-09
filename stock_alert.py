#!/usr/bin/env python3
"""Daily stock portfolio alert bot - sends Telegram messages with portfolio status and market overview."""

import requests
import anthropic
import pytz
import yfinance as yf
from datetime import datetime
from config_loader import load_config
import kis_api

KST = pytz.timezone("Asia/Seoul")


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    })
    return resp.json()



def get_market_indices():
    """Fetch major market indices (real-time via fast_info)."""
    indices = {
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW": "^DJI",
        "KOSPI": "^KS11",
        "KOSDAQ": "^KQ11",
        "VIX": "^VIX",
        "미국10년물": "^TNX",
        "금(Gold)": "GC=F",
        "WTI원유": "CL=F",
    }
    results = {}
    for name, ticker in indices.items():
        try:
            fi = yf.Ticker(ticker).fast_info
            curr = fi.last_price
            prev = fi.previous_close
            if curr and prev:
                change_pct = (curr - prev) / prev * 100
                results[name] = {
                    "price": round(curr, 2),
                    "change_pct": round(change_pct, 2),
                }
        except Exception:
            pass
    return results


def get_market_summary_ai(indices, fx, config):
    """Use Claude to write a brief market commentary."""
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        idx_text = "\n".join([
            f"{name}: {d['price']} ({d['change_pct']:+.2f}%)"
            for name, d in indices.items()
        ])
        prompt = f"""아래 시장 데이터를 바탕으로 오늘 시황을 3-4줄로 핵심만 요약해줘.
한국어로, 텔레그램 메시지에 맞게 짧고 명확하게. 불릿포인트 사용.

{idx_text}
USD/KRW: {fx['rate']} ({fx['change_pct']:+.2f}%)
날짜: {datetime.now(KST).strftime('%Y-%m-%d')}"""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return ""


def get_exchange_rate():
    """Fetch USD/KRW exchange rate (real-time via fast_info)."""
    try:
        fi = yf.Ticker("USDKRW=X").fast_info
        rate = fi.last_price
        prev = fi.previous_close
        change_pct = (rate - prev) / prev * 100 if rate and prev else 0
        return {"rate": round(rate or 0, 2), "change_pct": round(change_pct, 2)}
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
    # 항상 최신 데이터 사용 (캐시 무효화)
    kis_api.invalidate_balance_cache()

    now = datetime.now(KST)
    indices = get_market_indices()
    fx = get_exchange_rate()
    market_summary = get_market_summary_ai(indices, fx, config)

    lines = []
    lines.append("<b>📊 Daily Stock Report</b>")
    lines.append(now.strftime('%Y-%m-%d %H:%M'))
    lines.append("")

    if market_summary:
        lines.append("<b>오늘의 시황</b>")
        lines.append(market_summary)
        lines.append("")

    fx_emoji = "📈" if fx['change_pct'] > 0 else "📉"
    lines.append(f"<b>USD/KRW:</b> {format_number(fx['rate'])}원 {fx_emoji} ({format_change(fx['change_pct'])})")
    lines.append("")

    # 시장 지수
    kr_indices = ["KOSPI", "KOSDAQ"]
    us_indices = ["S&P 500", "NASDAQ", "DOW"]
    macro_indices = ["VIX", "미국10년물", "금(Gold)", "WTI원유"]

    lines.append("<b>🇰🇷 한국 시장</b>")
    for name in kr_indices:
        if name in indices:
            d = indices[name]
            e = "🔴" if d["change_pct"] < 0 else "🟢"
            lines.append(f"  {e} {name}: {format_number(d['price'])} ({format_change(d['change_pct'])})")
    lines.append("")

    lines.append("<b>🇺🇸 미국 시장</b>")
    for name in us_indices:
        if name in indices:
            d = indices[name]
            e = "🔴" if d["change_pct"] < 0 else "🟢"
            lines.append(f"  {e} {name}: {format_number(d['price'])} ({format_change(d['change_pct'])})")
    lines.append("")

    lines.append("<b>📉 매크로</b>")
    for name in macro_indices:
        if name in indices:
            d = indices[name]
            e = "🔴" if d["change_pct"] < 0 else "🟢"
            lines.append(f"  {e} {name}: {format_number(d['price'])} ({format_change(d['change_pct'])})")
    lines.append("")

    # ── KIS API 실계좌 포트폴리오 ──
    if kis_api.is_configured():
        kr_data = kis_api.get_kr_balance_raw()
        us_data = kis_api.get_us_balance_raw()

        # 국내주식
        lines.append("<b>🇰🇷 KR Stocks (실계좌)</b>")
        for h in kr_data["holdings"]:
            pe = "📈" if h["profit"] >= 0 else "📉"
            lines.append(f"  {pe} <b>{h['name']}</b>: {h['curr_price']:,}원 | {h['qty']}주 | 평단 {h['avg_price']:,}원")
            lines.append(f"       손익 {h['profit']:+,}원 ({h['profit_pct']:+.1f}%)")
        if kr_data["total"]:
            t = kr_data["total"]
            pe = "📈" if t.get("profit", 0) >= 0 else "📉"
            lines.append(f"  {pe} KR합계: {t.get('eval_amt', 0):,}원 | 손익 {t.get('profit', 0):+,}원 ({t.get('profit_pct', 0):+.1f}%)")
        lines.append("")

        # 해외주식 - yfinance fast_info로 실시간 가격 보정
        lines.append("<b>🇺🇸 US Stocks (실계좌)</b>")
        us_total_eval = 0
        us_total_profit = 0
        us_total_invested = 0
        for h in us_data["holdings"]:
            ticker = h["ticker"]
            curr = h["curr_price"]
            avg  = h["avg_price"]
            qty  = h["qty"]
            # yfinance 실시간 가격으로 덮어쓰기
            try:
                fi = yf.Ticker(ticker).fast_info
                yf_price = fi.last_price
                yf_prev  = fi.previous_close
                if yf_price and yf_price > 0:
                    curr = yf_price
                    day_chg = f" ({(curr - yf_prev) / yf_prev * 100:+.2f}%)" if yf_prev else ""
                else:
                    day_chg = ""
            except Exception:
                day_chg = ""
            profit  = (curr - avg) * qty
            pct     = profit / (avg * qty) * 100 if avg * qty else 0
            us_total_eval    += curr * qty
            us_total_profit  += profit
            us_total_invested += avg * qty
            pe = "📈" if profit >= 0 else "📉"
            lines.append(f"  {pe} <b>{ticker}</b>: ${curr:.2f}{day_chg} | {qty}주 | 평단 ${avg:.2f}")
            lines.append(f"       손익 ${profit:+.2f} ({pct:+.1f}%)")
        if us_data["holdings"]:
            pct_total = round(us_total_profit / us_total_invested * 100, 1) if us_total_invested else 0
            pe = "📈" if us_total_profit >= 0 else "📉"
            lines.append(f"  {pe} US합계: ${us_total_eval:,.2f} | 손익 ${us_total_profit:+,.2f} ({pct_total:+.1f}%)")
        lines.append("")

        # 전체 합산 (원화 환산) - yfinance 실시간 가격 기준
        kr_eval = kr_data["total"].get("eval_amt", 0) if kr_data["total"] else 0
        kr_profit = kr_data["total"].get("profit", 0) if kr_data["total"] else 0
        # us_total_eval / us_total_profit 은 위 루프에서 yfinance 가격으로 재계산된 값
        rate = fx["rate"] or 1
        total_krw = kr_eval + us_total_eval * rate
        total_profit_krw = kr_profit + us_total_profit * rate

        pe = "📈" if total_profit_krw >= 0 else "📉"
        lines.append("<b>💰 Total (원화 환산)</b>")
        lines.append(f"  총 평가금: {total_krw:,.0f}원")
        lines.append(f"  {pe} 총 손익: {total_profit_krw:+,.0f}원")

    else:
        lines.append("⚠️ KIS API 미연결 - 포트폴리오 데이터 없음")

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
