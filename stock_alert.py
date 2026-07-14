#!/usr/bin/env python3
"""Daily stock portfolio alert bot - sends Telegram messages with portfolio status and market overview."""

import json
import requests
import anthropic
import pytz
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config_loader import load_config, DATA_DIR
import kis_api

KST = pytz.timezone("Asia/Seoul")

_SENT_FLAG = DATA_DIR / "daily_report_sent.json"


def _kst_today() -> str:
    """KST 기준 오늘 날짜 문자열 (YYYY-MM-DD).
    리포트는 08:00 KST 실행 = 23:00 UTC(전날) → UTC date.today()와 KST 날짜가 달라짐."""
    return datetime.now(KST).strftime("%Y-%m-%d")


def _already_sent_today() -> bool:
    """오늘(KST 기준) 이미 리포트를 발송했으면 True."""
    try:
        if _SENT_FLAG.exists():
            return json.loads(_SENT_FLAG.read_text()).get("date") == _kst_today()
    except Exception:
        pass
    return False


def _mark_sent_today():
    _SENT_FLAG.write_text(json.dumps({"date": _kst_today()}))


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }, timeout=15)   # timeout 없으면 네트워크 불안정 시 수 시간 hang 가능
    return resp.json()



def _fetch_one_index(name_ticker):
    """yfinance fast_info 단건 조회 (스레드에서 호출)."""
    name, ticker = name_ticker
    try:
        fi = yf.Ticker(ticker).fast_info
        curr = fi.last_price
        prev = fi.previous_close
        if curr and prev:
            change_pct = (curr - prev) / prev * 100
            return name, {"price": round(curr, 2), "change_pct": round(change_pct, 2)}
    except Exception:
        pass
    return name, None


def get_market_indices():
    """Fetch major market indices — 9개 동시 병렬 조회."""
    tickers = {
        "S&P 500": "^GSPC",
        "NASDAQ":  "^IXIC",
        "DOW":     "^DJI",
        "KOSPI":   "^KS11",
        "KOSDAQ":  "^KQ11",
        "VIX":     "^VIX",
        "미국10년물": "^TNX",
        "금(Gold)": "GC=F",
        "WTI원유":  "CL=F",
    }
    results = {}
    with ThreadPoolExecutor(max_workers=9) as ex:
        futures = {ex.submit(_fetch_one_index, item): item[0] for item in tickers.items()}
        for fut in as_completed(futures):
            name, data = fut.result()
            if data:
                results[name] = data
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
    """Fetch USD/KRW exchange rate (실패 시 캐시/기본값 fallback — 절대 0 반환 안 함)."""
    from utils import get_usdkrw
    return get_usdkrw()


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

    # ── 1단계: 독립 API 4개 병렬 실행 ──────────────────────────────────
    # get_market_indices(9 yfinance) / get_exchange_rate(1 yfinance) /
    # get_kr_balance_raw / get_us_balance_raw  →  동시 시작
    _kr_data_holder = [None]
    _us_data_holder = [None]
    _us_cash_holder = [0.0]

    def _fetch_kr():
        if kis_api.is_configured():
            _kr_data_holder[0] = kis_api.get_kr_balance_raw()

    def _fetch_us():
        if kis_api.is_configured():
            _us_data_holder[0] = kis_api.get_us_balance_raw()

    def _fetch_us_cash():
        if kis_api.is_configured():
            _us_cash_holder[0] = kis_api.get_us_cash()

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_idx  = ex.submit(get_market_indices)
        f_fx   = ex.submit(get_exchange_rate)
        f_kr   = ex.submit(_fetch_kr)
        f_us   = ex.submit(_fetch_us)
        f_cash = ex.submit(_fetch_us_cash)
        indices = f_idx.result()
        fx      = f_fx.result()
        f_kr.result()
        f_us.result()
        f_cash.result()

    kr_data = _kr_data_holder[0] or {"holdings": [], "total": {}}
    us_data = _us_data_holder[0] or {"holdings": [], "total": {}}
    us_cash = _us_cash_holder[0]

    # ── 2단계: Claude 시황 요약 (indices 필요 → 직렬) ───────────────────
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
        # kr_data / us_data 는 1단계 병렬 패치에서 이미 받아둠

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
            if t.get("cash"):
                lines.append(f"  💵 원화 예수금: {t['cash']:,}원")
        lines.append("")

        # 해외주식 - yfinance fast_info로 실시간 가격 보정 (보유 종목 동시 조회)
        lines.append("<b>🇺🇸 US Stocks (실계좌)</b>")
        us_total_eval = 0
        us_total_profit_usd = 0
        us_total_invested = 0
        us_total_fx_effect = 0
        fx_rate = fx["rate"]
        fx_prev = fx.get("prev_rate") or fx_rate

        def _fetch_us_price(h):
            """종목 1개 yfinance 조회 → (ticker, curr, day_chg)."""
            ticker = h["ticker"]
            curr   = h["curr_price"]
            try:
                fi = yf.Ticker(ticker).fast_info
                yf_price = fi.last_price
                yf_prev  = fi.previous_close
                if yf_price and yf_price > 0:
                    day_chg = f" ({(yf_price - yf_prev) / yf_prev * 100:+.2f}%)" if yf_prev else ""
                    return ticker, yf_price, day_chg
            except Exception:
                pass
            return ticker, curr, ""

        # 보유 종목 수만큼 스레드 병렬 실행
        price_map: dict[str, tuple] = {}
        if us_data["holdings"]:
            with ThreadPoolExecutor(max_workers=len(us_data["holdings"])) as ex:
                for tkr, price, chg in ex.map(_fetch_us_price, us_data["holdings"]):
                    price_map[tkr] = (price, chg)

        for h in us_data["holdings"]:
            ticker     = h["ticker"]
            avg        = h["avg_price"]
            qty        = h["qty"]
            curr, day_chg = price_map.get(ticker, (h["curr_price"], ""))
            # KIS 계산값 그대로 사용 (yfinance 재계산 X)
            profit_usd = h["profit"]      # frcr_evlu_pfls_amt (USD)
            pct        = h["profit_pct"]  # KIS 수익률
            # 오늘 환율 효과: 현재 포지션 가치가 환율 변동으로 KRW 얼마나 변했는지
            fx_effect  = curr * qty * (fx_rate - fx_prev)
            us_total_eval       += curr * qty
            us_total_profit_usd += profit_usd
            us_total_invested   += avg * qty
            us_total_fx_effect  += fx_effect
            pe = "📈" if profit_usd >= 0 else "📉"
            lines.append(f"  {pe} <b>{ticker}</b>: ${curr:.2f}{day_chg} | {qty}주 | 평단 ${avg:.2f}")
            profit_krw = profit_usd * fx_rate
            lines.append(f"       주가손익 ${profit_usd:+.2f} ({pct:+.1f}%) ≈ {profit_krw:+,.0f}원")
            if abs(fx_effect) >= 100:
                fx_dir = "▲" if fx_effect >= 0 else "▼"
                lines.append(f"       환율효과 {fx_effect:+,.0f}원 (환율 {fx_dir}{abs(fx_rate - fx_prev):.1f}원)")
        if us_data["holdings"]:
            pct_total = round(us_total_profit_usd / us_total_invested * 100, 1) if us_total_invested else 0
            pe = "📈" if us_total_profit_usd >= 0 else "📉"
            total_us_profit_krw = us_total_profit_usd * fx_rate
            lines.append(f"  {pe} US합계: ${us_total_eval:,.2f} | 주가손익 ${us_total_profit_usd:+,.2f} ({pct_total:+.1f}%)")
            lines.append(f"       원화환산 {total_us_profit_krw:+,.0f}원 | 오늘 환율효과 {us_total_fx_effect:+,.0f}원")
        if us_cash:
            lines.append(f"  💵 달러 예수금: ${us_cash:,.2f} ≈ {us_cash * fx_rate:,.0f}원")
        lines.append("")

        # 전체 합산 (원화 환산) - yfinance 실시간 가격 기준
        kr_eval = kr_data["total"].get("eval_amt", 0) if kr_data["total"] else 0
        kr_profit = kr_data["total"].get("profit", 0) if kr_data["total"] else 0
        kr_cash = kr_data["total"].get("cash", 0) if kr_data["total"] else 0
        total_krw = kr_eval + us_total_eval * fx_rate
        total_cash_krw = kr_cash + us_cash * fx_rate
        us_stock_profit_krw = us_total_profit_usd * fx_rate
        total_profit_krw = kr_profit + us_stock_profit_krw

        pe = "📈" if total_profit_krw >= 0 else "📉"
        lines.append("<b>💰 Total (원화 환산)</b>")
        lines.append(f"  주식 평가금: {total_krw:,.0f}원")
        lines.append(f"  보유현금: {total_cash_krw:,.0f}원 (₩{kr_cash:,} + ${us_cash:,.2f})")
        lines.append(f"  총자산: <b>{total_krw + total_cash_krw:,.0f}원</b>")
        lines.append(f"  {pe} 총 주가손익: {total_profit_krw:+,.0f}원")
        if us_data["holdings"] and abs(us_total_fx_effect) >= 100:
            lines.append(f"  💱 오늘 환율효과: {us_total_fx_effect:+,.0f}원 (₩{fx_rate:,.1f}, 전일비 {fx['change_pct']:+.2f}%)")

    else:
        lines.append("⚠️ KIS API 미연결 - 포트폴리오 데이터 없음")

    return "\n".join(lines)


def _wait_for_network(timeout=60, interval=5):
    """Block until DNS resolves or timeout (seconds)."""
    import socket, time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.getaddrinfo("api.telegram.org", 443)
            return True
        except OSError:
            time.sleep(interval)
    return False


def main():
    if datetime.now(KST).weekday() >= 5:
        print("Weekend — daily report skipped.")
        return

    if _already_sent_today():
        print("Daily report already sent today — skipping.")
        return

    if not _wait_for_network():
        print("Network unavailable after 60s — aborting (will retry next launchd fire)")
        return

    config = load_config()

    # KRX 공휴일 체크
    from utils import is_kr_market_holiday
    today_kst = datetime.now(KST).date()
    if is_kr_market_holiday(today_kst):
        send_telegram(
            config["telegram"]["bot_token"],
            config["telegram"]["chat_id"],
            f"🎌 오늘({today_kst.strftime('%m/%d')}) 한국 증시 휴장입니다.",
        )
        _mark_sent_today()
        print(f"KRX holiday ({today_kst}) — holiday notice sent.")
        return

    # 포트폴리오 스냅샷 저장 (CAPM·성과차트용 일별 데이터 누적)
    try:
        from portfolio_tracker import take_snapshot
        snap = take_snapshot()
        print(f"Snapshot saved: {snap['total_krw']:,.0f} KRW")
    except Exception as e:
        print(f"Snapshot error (non-fatal): {e}")

    msg = build_message(config)

    result = send_telegram(
        config["telegram"]["bot_token"],
        config["telegram"]["chat_id"],
        msg,
    )
    if result.get("ok"):
        _mark_sent_today()
        print("Alert sent successfully!")
    else:
        print(f"Failed to send: {result}")


if __name__ == "__main__":
    main()
