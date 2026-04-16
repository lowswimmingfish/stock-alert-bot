#!/usr/bin/env python3
"""포트폴리오 일별 스냅샷 저장 + 성과 차트 생성."""

import io
import json
import logging
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import kis_api
from config_loader import DATA_DIR, load_config

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"


# ── 스냅샷 저장/로드 ───────────────────────────────────────────────────────────

def _load_snapshots() -> dict:
    if SNAPSHOTS_FILE.exists():
        with open(SNAPSHOTS_FILE) as f:
            return json.load(f)
    return {}


def _save_snapshots(data: dict):
    with open(SNAPSHOTS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── 스냅샷 촬영 ───────────────────────────────────────────────────────────────

def take_snapshot() -> dict:
    """현재 포트폴리오 상태를 스냅샷으로 저장하고 반환."""
    today = str(date.today())
    data  = _load_snapshots()
    config = load_config()

    # 환율
    try:
        fx_rate = yf.Ticker("USDKRW=X").fast_info.last_price or 1400
    except Exception:
        fx_rate = 1400

    holdings = {}
    total_usd = 0.0
    kr_krw    = 0.0

    if kis_api.is_configured():
        # 해외
        try:
            us_raw = kis_api.get_us_balance_raw()
            for h in us_raw.get("holdings", []):
                ticker = h["ticker"]
                try:
                    price = yf.Ticker(ticker).fast_info.last_price or h["curr_price"]
                except Exception:
                    price = h["curr_price"]
                value = price * h["qty"]
                total_usd += value
                holdings[ticker] = {
                    "qty":       h["qty"],
                    "price":     round(price, 4),
                    "avg_price": h["avg_price"],
                    "value_usd": round(value, 2),
                }
        except Exception as e:
            logger.warning(f"Snapshot US error: {e}")

        # 국내
        try:
            kr_raw = kis_api.get_kr_balance_raw()
            for h in kr_raw.get("holdings", []):
                kr_krw += h.get("eval_amt", 0)
                holdings[h["ticker"]] = {
                    "qty":       h["qty"],
                    "price":     h["curr_price"],
                    "avg_price": h["avg_price"],
                    "value_krw": h.get("eval_amt", 0),
                }
        except Exception as e:
            logger.warning(f"Snapshot KR error: {e}")
    else:
        # fallback: portfolio.json + yfinance
        for s in config["portfolio"].get("us_stocks", []):
            try:
                price = yf.Ticker(s["ticker"]).fast_info.last_price or 0
                value = price * s["shares"]
                total_usd += value
                holdings[s["ticker"]] = {
                    "qty":       s["shares"],
                    "price":     round(price, 4),
                    "avg_price": s["avg_price"],
                    "value_usd": round(value, 2),
                }
            except Exception:
                pass

    total_krw = round(total_usd * fx_rate + kr_krw)

    snapshot = {
        "total_krw": total_krw,
        "total_usd": round(total_usd, 2),
        "kr_krw":    round(kr_krw),
        "fx_rate":   round(fx_rate, 2),
        "holdings":  holdings,
    }
    data[today] = snapshot
    _save_snapshots(data)
    logger.info(f"Snapshot saved: {today} | {total_krw:,.0f} KRW")
    return snapshot


# ── S&P500 비교 데이터 ────────────────────────────────────────────────────────

def _sp500_returns(start: date, end: date) -> dict[str, float]:
    """날짜별 S&P500 누적수익률 (시작일 대비, 0-based). {날짜str: float}"""
    try:
        delta   = (end - start).days
        period  = f"{max(delta + 10, 30)}d"
        hist    = yf.Ticker("^GSPC").history(period=period)
        if hist.empty:
            return {}

        result     = {}
        base_price = None
        for idx, row in hist.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            if d < start:
                base_price = row["Close"]
                continue
            if d > end:
                break
            if base_price is None:
                base_price = row["Close"]
            result[str(d)] = (row["Close"] / base_price - 1) * 100
        return result
    except Exception as e:
        logger.warning(f"S&P500 fetch error: {e}")
        return {}


# ── 차트 생성 ─────────────────────────────────────────────────────────────────

def build_performance_chart(days: int = 30) -> io.BytesIO:
    """최근 N일 포트폴리오 성과 차트 PNG를 BytesIO로 반환."""
    data = _load_snapshots()
    if not data:
        raise ValueError("스냅샷 데이터가 없어요. 매일 자동 저장되니 내일부터 확인 가능합니다.")

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(date.fromisoformat(d), v) for d, v in data.items()
         if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )

    if len(filtered) < 2:
        raise ValueError(
            f"차트를 그리려면 최소 2일치 데이터가 필요해요. (현재 {len(filtered)}일 저장됨)"
        )

    dates  = [d for d, _ in filtered]
    values = [v["total_krw"] for _, v in filtered]
    base   = values[0]
    pct_returns = [(v / base - 1) * 100 for v in values]

    # S&P500 비교
    sp = _sp500_returns(dates[0], dates[-1])
    sp_dates  = sorted([date.fromisoformat(d) for d in sp if start <= date.fromisoformat(d) <= today])
    sp_values = [sp[str(d)] for d in sp_dates if str(d) in sp]

    # ── 차트 ──
    BG = "#0d1117"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), facecolor=BG)
    fig.patch.set_facecolor(BG)

    for ax in (ax1, ax2):
        ax.set_facecolor(BG)
        ax.tick_params(colors="#aaaaaa", labelsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        for spine in ax.spines.values():
            spine.set_color("#333333")
        ax.grid(axis="y", color="#222222", linewidth=0.6)

    # 상단: 수익률
    ax1.plot(dates, pct_returns, color="#00d4aa", linewidth=2,
             label="내 포트폴리오", zorder=3)
    ax1.fill_between(dates, pct_returns, alpha=0.12, color="#00d4aa")
    if sp_dates and sp_values:
        ax1.plot(sp_dates, sp_values, color="#ffd700", linewidth=1.5,
                 linestyle="--", label="S&P500", zorder=2)
        ax1.fill_between(sp_dates, sp_values, alpha=0.06, color="#ffd700")
    ax1.axhline(0, color="#555555", linewidth=0.8)
    ax1.set_ylabel("수익률 (%)", color="#aaaaaa", fontsize=10)
    ax1.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=9,
               framealpha=0.8, loc="upper left")
    ax1.set_title(f"포트폴리오 성과 (최근 {days}일)", color="white", fontsize=13, pad=12)

    # 하단: 절대 평가금액 (백만원)
    ax2.plot(dates, [v / 1e6 for v in values], color="#00a8ff", linewidth=2)
    ax2.fill_between(dates, [v / 1e6 for v in values], alpha=0.12, color="#00a8ff")
    ax2.set_ylabel("평가금액 (백만원)", color="#aaaaaa", fontsize=10)

    # 수익 요약 텍스트
    total_pct = pct_returns[-1]
    total_gain = values[-1] - values[0]
    color_txt = "#00d4aa" if total_pct >= 0 else "#ff5555"
    sign = "+" if total_pct >= 0 else ""
    fig.text(
        0.99, 0.97,
        f"{sign}{total_pct:.2f}%  ({sign}{total_gain / 1e4:,.0f}만원)",
        color=color_txt, fontsize=13, ha="right", va="top", fontweight="bold",
    )

    plt.tight_layout(rect=[0, 0, 1, 0.96])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf


# ── 텔레그램 이미지 전송 ──────────────────────────────────────────────────────

def send_chart_telegram(bot_token: str, chat_id: str, days: int = 30):
    """차트 PNG를 텔레그램으로 전송."""
    now = datetime.now(KST)
    try:
        buf     = build_performance_chart(days)
        caption = f"📊 포트폴리오 성과 (최근 {days}일) | {now.strftime('%Y-%m-%d %H:%M')} KST"
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            files={"photo": ("chart.png", buf, "image/png")},
            data={"chat_id": chat_id, "caption": caption},
        )
    except ValueError as e:
        # 데이터 부족 등 예상 가능한 에러
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": str(e), "parse_mode": "HTML"},
        )
    except Exception as e:
        logger.error(f"Chart send error: {e}")


# ── 텍스트 성과 요약 ──────────────────────────────────────────────────────────

def get_performance_summary(days: int = 30) -> str:
    data = _load_snapshots()
    if len(data) < 2:
        return "📊 아직 데이터가 부족해요. 매일 자동 저장되니 내일 다시 확인해주세요!"

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(d, v) for d, v in data.items() if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )
    if not filtered:
        return "해당 기간 데이터가 없어요."

    first_d, first_v = filtered[0]
    last_d,  last_v  = filtered[-1]

    base = first_v["total_krw"]
    curr = last_v["total_krw"]
    pct  = (curr / base - 1) * 100
    gain = curr - base
    sign  = "+" if pct >= 0 else ""
    arrow = "📈" if pct >= 0 else "📉"

    lines = [
        f"{arrow} <b>포트폴리오 성과 (최근 {days}일)</b>",
        f"{first_d} → {last_d}",
        f"수익률: <b>{sign}{pct:.2f}%</b>",
        f"손익:   <b>{sign}{gain / 1e4:,.1f}만원</b>",
        f"현재 평가금: {curr / 1e4:,.0f}만원",
        "",
    ]

    # 보유 종목별 개별 수익률
    if kis_api.is_configured():
        try:
            us_raw = kis_api.get_us_balance_raw()
            kr_raw = kis_api.get_kr_balance_raw()
            lines.append("<b>종목별 현재 손익</b>")
            for h in us_raw.get("holdings", []):
                avg = h["avg_price"]
                pct_h = (h["curr_price"] - avg) / avg * 100 if avg else 0
                s = "+" if pct_h >= 0 else ""
                em = "📈" if pct_h >= 0 else "📉"
                lines.append(f"  {em} {h['ticker']}: {s}{pct_h:.1f}%")
            for h in kr_raw.get("holdings", []):
                pct_h = h.get("profit_pct", 0)
                s = "+" if pct_h >= 0 else ""
                em = "📈" if pct_h >= 0 else "📉"
                lines.append(f"  {em} {h['name']}: {s}{pct_h:.1f}%")
        except Exception:
            pass

    return "\n".join(lines)
