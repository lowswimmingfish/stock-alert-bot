#!/usr/bin/env python3
"""가격/이벤트 알림 관리자."""

import json
import logging
from datetime import date, datetime
import yfinance as yf
from config_loader import DATA_DIR

logger = logging.getLogger(__name__)

ALERTS_FILE = DATA_DIR / "alerts.json"


# ── 저장/로드 ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE) as f:
            return json.load(f)
    return {
        "price_alerts": [],
        "settings": {
            "earnings_alert": True,
            "dividend_alert": True,
            "daily_change_pct": 5.0,
        },
        "_next_id": 1,
    }


def _save(data: dict):
    with open(ALERTS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── 가격 알림 CRUD ─────────────────────────────────────────────────────────────

def add_price_alert(ticker: str, condition: str, price: float) -> str:
    """
    가격 알림 등록.
    condition: 'above' (이상) | 'below' (이하)
    """
    data = _load()
    alert_id = data.get("_next_id", 1)
    data["price_alerts"].append({
        "id":        alert_id,
        "ticker":    ticker.upper(),
        "condition": condition,
        "price":     price,
        "created":   str(date.today()),
        "triggered": False,
    })
    data["_next_id"] = alert_id + 1
    _save(data)
    cond_str = "이상 ↑" if condition == "above" else "이하 ↓"
    return f"✅ 알림 등록 [{alert_id}] {ticker.upper()} ${price:.2f} {cond_str} 도달 시 알림"


def list_alerts() -> str:
    data = _load()
    active = [a for a in data["price_alerts"] if not a["triggered"]]
    s = data.get("settings", {})

    lines = ["<b>🔔 등록된 알림</b>\n"]

    if active:
        lines.append("<b>가격 알림</b>")
        for a in active:
            cond = "↑ 이상" if a["condition"] == "above" else "↓ 이하"
            lines.append(f"  [{a['id']}] {a['ticker']} ${a['price']:.2f} {cond}  (등록 {a['created']})")
    else:
        lines.append("  (등록된 가격 알림 없음)")

    lines.append("")
    lines.append("<b>자동 이벤트 알림</b>")
    lines.append(f"  실적 발표 전날: {'✅' if s.get('earnings_alert', True) else '❌'}")
    lines.append(f"  배당락일 전:   {'✅' if s.get('dividend_alert', True) else '❌'}")
    lines.append(f"  급등락:       ±{s.get('daily_change_pct', 5.0)}% 이상")
    lines.append("\n<i>/alert 삭제 [번호]  로 가격 알림 제거</i>")
    return "\n".join(lines)


def remove_alert(alert_id: int) -> str:
    data = _load()
    before = len(data["price_alerts"])
    data["price_alerts"] = [a for a in data["price_alerts"] if a["id"] != alert_id]
    _save(data)
    if len(data["price_alerts"]) < before:
        return f"✅ 알림 [{alert_id}] 삭제됨"
    return f"❌ 알림 [{alert_id}] 없음"


# ── 스케줄러에서 호출되는 체크 함수들 ─────────────────────────────────────────

def check_price_alerts(send_fn) -> int:
    """가격 알림 체크. 발동된 알림 개수 반환."""
    data = _load()
    triggered_count = 0
    changed = False

    for alert in data["price_alerts"]:
        if alert["triggered"]:
            continue
        try:
            fi = yf.Ticker(alert["ticker"]).fast_info
            curr = fi.last_price
            if not curr:
                continue

            hit = (
                (alert["condition"] == "above" and curr >= alert["price"]) or
                (alert["condition"] == "below" and curr <= alert["price"])
            )
            if hit:
                arrow = "📈" if alert["condition"] == "above" else "📉"
                cond  = "도달 ↑" if alert["condition"] == "above" else "도달 ↓"
                msg = (
                    f"{arrow} <b>가격 알림 발동!</b>\n"
                    f"{alert['ticker']} {cond}\n"
                    f"목표가: ${alert['price']:.2f} | 현재가: ${curr:.2f}"
                )
                send_fn(msg)
                alert["triggered"] = True
                triggered_count += 1
                changed = True
                logger.info(f"Price alert triggered: {alert['ticker']} {alert['condition']} {alert['price']}")
        except Exception as e:
            logger.warning(f"Price check error {alert.get('ticker')}: {e}")

    if changed:
        _save(data)
    return triggered_count


def check_daily_change_alerts(holdings: list, send_fn) -> int:
    """보유 종목 급등락 체크 (일별 ±X% 이상). 발동 건수 반환."""
    data = _load()
    threshold = data.get("settings", {}).get("daily_change_pct", 5.0)
    triggered = 0

    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        try:
            fi = yf.Ticker(ticker).fast_info
            curr = fi.last_price
            prev = fi.previous_close
            if not curr or not prev:
                continue
            pct = (curr - prev) / prev * 100
            if abs(pct) >= threshold:
                arrow = "📈" if pct > 0 else "📉"
                qty   = h.get("qty", 0)
                avg   = h.get("avg_price", curr)
                pnl   = (curr - avg) * qty
                msg = (
                    f"{arrow} <b>급등락 알림</b>\n"
                    f"{ticker}: {pct:+.2f}% (${prev:.2f} → ${curr:.2f})\n"
                    f"보유 {qty}주 | 손익 ${pnl:+.2f}"
                )
                send_fn(msg)
                triggered += 1
                logger.info(f"Daily change alert: {ticker} {pct:+.2f}%")
        except Exception as e:
            logger.warning(f"Daily change check error {ticker}: {e}")

    return triggered


def check_earnings_alerts(holdings: list, send_fn) -> int:
    """실적 발표 하루 전 알림. 발동 건수 반환."""
    data = _load()
    if not data.get("settings", {}).get("earnings_alert", True):
        return 0

    today = date.today()
    triggered = 0

    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue

            # calendar는 DataFrame 또는 dict
            import pandas as pd
            if isinstance(cal, pd.DataFrame):
                if "Earnings Date" in cal.columns:
                    earn_dates = cal["Earnings Date"].dropna().tolist()
                else:
                    continue
            elif isinstance(cal, dict):
                earn_dates = cal.get("Earnings Date", [])
                if not isinstance(earn_dates, list):
                    earn_dates = [earn_dates]
            else:
                continue

            for earn_dt in earn_dates:
                try:
                    if hasattr(earn_dt, "date"):
                        earn_date = earn_dt.date()
                    else:
                        earn_date = datetime.strptime(str(earn_dt)[:10], "%Y-%m-%d").date()
                    if (earn_date - today).days == 1:
                        msg = (
                            f"📅 <b>실적 발표 D-1</b>\n"
                            f"{ticker} 내일({earn_date}) 실적 발표 예정\n"
                            f"보유 {h.get('qty', '?')}주"
                        )
                        send_fn(msg)
                        triggered += 1
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Earnings check error {ticker}: {e}")

    return triggered


def check_dividend_alerts(holdings: list, send_fn) -> int:
    """배당락일 1~2일 전 알림. 발동 건수 반환."""
    data = _load()
    if not data.get("settings", {}).get("dividend_alert", True):
        return 0

    today = date.today()
    triggered = 0

    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        try:
            info = yf.Ticker(ticker).info
            ex_div_ts = info.get("exDividendDate")
            if not ex_div_ts:
                continue
            ex_div = datetime.fromtimestamp(ex_div_ts).date()
            days_until = (ex_div - today).days

            if days_until in (1, 2):
                div_rate = info.get("dividendRate") or 0
                quarterly = div_rate / 4
                qty = h.get("qty", 0)
                msg = (
                    f"💰 <b>배당락일 D-{days_until}</b>\n"
                    f"{ticker} 배당락일: {ex_div}\n"
                    f"분기 배당: ${quarterly:.4f}/주 | 보유 {qty}주 → ${quarterly * qty:.2f}"
                )
                send_fn(msg)
                triggered += 1
                logger.info(f"Dividend alert: {ticker} ex-div {ex_div}")
        except Exception as e:
            logger.warning(f"Dividend check error {ticker}: {e}")

    return triggered
