#!/usr/bin/env python3
"""포트폴리오 일별 스냅샷 저장 + 성과 차트 생성."""

import io
import json
import logging
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
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


# ── MDD + 종목별 기여도 ───────────────────────────────────────────────────────

def calc_mdd(days: int = 365) -> dict:
    """
    최대낙폭(MDD) 계산.
    반환: {mdd_pct, peak_date, trough_date, peak_val, trough_val, recovery_pct}
    """
    data = _load_snapshots()
    if len(data) < 2:
        return {}

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(date.fromisoformat(d), v["total_krw"]) for d, v in data.items()
         if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )
    if len(filtered) < 2:
        return {}

    dates  = [d for d, _ in filtered]
    values = [v for _, v in filtered]

    # 최대낙폭 계산
    peak_val   = values[0]
    peak_idx   = 0
    max_dd     = 0.0
    dd_peak_i  = 0
    dd_trough_i = 0

    for i, v in enumerate(values):
        if v > peak_val:
            peak_val  = v
            peak_idx  = i
        dd = (peak_val - v) / peak_val
        if dd > max_dd:
            max_dd      = dd
            dd_peak_i   = peak_idx
            dd_trough_i = i

    trough_val = values[dd_trough_i]
    curr_val   = values[-1]

    # 낙폭 이후 회복률 (trough 이후 현재까지)
    if trough_val > 0 and dd_trough_i < len(values) - 1:
        recovery_pct = (curr_val - trough_val) / (values[dd_peak_i] - trough_val) * 100
        recovery_pct = min(recovery_pct, 100.0)
    else:
        recovery_pct = 100.0 if curr_val >= values[dd_peak_i] else 0.0

    return {
        "mdd_pct":      round(max_dd * 100, 2),
        "peak_date":    str(dates[dd_peak_i]),
        "trough_date":  str(dates[dd_trough_i]),
        "peak_val":     round(values[dd_peak_i]),
        "trough_val":   round(trough_val),
        "recovery_pct": round(recovery_pct, 1),
        "curr_val":     round(curr_val),
        "dates":        [str(d) for d in dates],
        "values":       values,
    }


def calc_stock_contribution(days: int = 30) -> list[dict]:
    """
    종목별 포트폴리오 기여도 계산.
    반환: [{ticker, name, contrib_pct, ret_pct, weight_pct}, ...]  수익 기여 순 정렬
    """
    data = _load_snapshots()
    if len(data) < 2:
        return []

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(date.fromisoformat(d), v) for d, v in data.items()
         if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )
    if len(filtered) < 2:
        return []

    first_snap = filtered[0][1]
    last_snap  = filtered[-1][1]
    total_base = first_snap.get("total_krw", 1) or 1
    fx_last    = last_snap.get("fx_rate", 1400)
    fx_first   = first_snap.get("fx_rate", 1400)

    first_holdings = first_snap.get("holdings", {})
    last_holdings  = last_snap.get("holdings", {})

    results = []
    all_tickers = set(first_holdings) | set(last_holdings)

    for ticker in all_tickers:
        f = first_holdings.get(ticker, {})
        l = last_holdings.get(ticker, {})

        # 시작 평가금액 (KRW 환산)
        if "value_usd" in f:
            val_start = f["value_usd"] * fx_first
        elif "value_krw" in f:
            val_start = f["value_krw"]
        else:
            val_start = 0.0

        # 종료 평가금액
        if "value_usd" in l:
            val_end = l["value_usd"] * fx_last
        elif "value_krw" in l:
            val_end = l["value_krw"]
        else:
            val_end = 0.0

        if val_start == 0 and val_end == 0:
            continue

        gain = val_end - val_start
        contrib_pct = gain / total_base * 100          # 포트폴리오 전체 대비 기여도
        ret_pct     = (gain / val_start * 100) if val_start > 0 else 0.0
        weight_pct  = (val_end / (last_snap.get("total_krw", 1) or 1)) * 100

        results.append({
            "ticker":      ticker,
            "contrib_pct": round(contrib_pct, 2),
            "ret_pct":     round(ret_pct, 2),
            "weight_pct":  round(weight_pct, 1),
            "val_end_만":  round(val_end / 1e4, 1),
        })

    results.sort(key=lambda x: x["contrib_pct"], reverse=True)
    return results


# ── CAPM 분석 ────────────────────────────────────────────────────────────────

def calc_capm_metrics(days: int = 90) -> dict:
    """
    포트폴리오의 CAPM 지표를 계산합니다.
    - beta: 시장(S&P500) 대비 민감도
    - alpha: Jensen's Alpha (초과수익)
    - sharpe: 샤프 비율
    - treynor: 트레이너 비율
    - expected_return: CAPM 기대수익률 (연율화)
    - actual_return: 실제 연율화 수익률
    """
    data = _load_snapshots()
    if len(data) < 10:
        return {}

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(date.fromisoformat(d), v) for d, v in data.items()
         if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )
    if len(filtered) < 10:
        return {}

    dates  = [d for d, _ in filtered]
    values = np.array([v["total_krw"] for _, v in filtered], dtype=float)

    # 일별 포트폴리오 수익률
    port_ret = np.diff(values) / values[:-1]

    # S&P500 일별 수익률
    try:
        period = f"{days + 20}d"
        hist = yf.Ticker("^GSPC").history(period=period)
        if hist.empty:
            return {}

        sp_prices = {}
        for idx, row in hist.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            sp_prices[d] = row["Close"]

        sp_ret_list = []
        for i in range(1, len(dates)):
            d_prev, d_curr = dates[i - 1], dates[i]
            if d_prev in sp_prices and d_curr in sp_prices:
                sp_ret_list.append((sp_prices[d_curr] / sp_prices[d_prev]) - 1)
            else:
                sp_ret_list.append(None)

        # None이 너무 많으면 포기
        valid = [(p, m) for p, m in zip(port_ret, sp_ret_list) if m is not None]
        if len(valid) < 10:
            return {}

        port_r = np.array([v[0] for v in valid])
        mkt_r  = np.array([v[1] for v in valid])
    except Exception as e:
        logger.warning(f"CAPM S&P500 fetch error: {e}")
        return {}

    # 무위험 이자율 (미국 10년 국채, 일별로 환산)
    try:
        tnx = yf.Ticker("^TNX").fast_info.last_price  # 연율 %
        rf_annual = (tnx or 4.3) / 100
    except Exception:
        rf_annual = 0.043  # fallback 4.3%
    rf_daily = rf_annual / 252

    # ── 베타 계산 ──
    cov_matrix = np.cov(port_r, mkt_r)
    beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] != 0 else 1.0

    # ── 연율화 수익률 ──
    n_days = len(port_r)
    actual_annual = (1 + port_r.mean()) ** 252 - 1
    mkt_annual    = (1 + mkt_r.mean())  ** 252 - 1

    # ── CAPM 기대수익률 ──
    expected_annual = rf_annual + beta * (mkt_annual - rf_annual)

    # ── Jensen's Alpha (연율화) ──
    alpha = actual_annual - expected_annual

    # ── 샤프 비율 (연율화) ──
    excess_ret = port_r - rf_daily
    sharpe = (excess_ret.mean() / excess_ret.std() * np.sqrt(252)) if excess_ret.std() != 0 else 0.0

    # ── 트레이너 비율 (연율화) ──
    treynor = ((port_r.mean() - rf_daily) * 252) / beta if beta != 0 else 0.0

    return {
        "beta":            round(float(beta), 3),
        "alpha_pct":       round(float(alpha * 100), 2),
        "sharpe":          round(float(sharpe), 3),
        "treynor_pct":     round(float(treynor * 100), 2),
        "expected_pct":    round(float(expected_annual * 100), 2),
        "actual_pct":      round(float(actual_annual * 100), 2),
        "rf_pct":          round(float(rf_annual * 100), 2),
        "mkt_pct":         round(float(mkt_annual * 100), 2),
        "n_days":          n_days,
    }


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

    # CAPM 기대수익률 선 (일별 누적)
    capm = calc_capm_metrics(days=max(days, 30))
    capm_dates, capm_values = [], []
    if capm and capm["beta"] and sp_dates and sp_values:
        try:
            rf_annual  = capm["rf_pct"] / 100
            beta       = capm["beta"]
            # 기대수익률: rf + beta*(sp_return - rf), sp 누적 수익률로 스케일
            rf_daily   = rf_annual / 252
            capm_dates = sp_dates
            capm_values = []
            for sp_pct in sp_values:
                sp_daily_ret = sp_pct / 100  # 시작일 대비 누적
                capm_cum = rf_daily * len(capm_dates) + beta * (sp_daily_ret - rf_daily * len(capm_dates))
                capm_values.append(capm_cum * 100)
            # 더 직관적: β×S&P500_누적 + (1-β)×rf_누적
            capm_values = []
            for i, sp_pct in enumerate(sp_values):
                n = i + 1
                rf_cum = ((1 + rf_daily) ** n - 1) * 100
                capm_cum = rf_cum + beta * (sp_pct - rf_cum)
                capm_values.append(capm_cum)
        except Exception:
            capm_dates, capm_values = [], []

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

    # MDD 계산 (차트 기간 기준)
    mdd_info = calc_mdd(days=days)

    # 상단: 수익률
    ax1.plot(dates, pct_returns, color="#00d4aa", linewidth=2,
             label="내 포트폴리오", zorder=3)
    ax1.fill_between(dates, pct_returns, alpha=0.12, color="#00d4aa")
    if sp_dates and sp_values:
        ax1.plot(sp_dates, sp_values, color="#ffd700", linewidth=1.5,
                 linestyle="--", label="S&P500", zorder=2)
        ax1.fill_between(sp_dates, sp_values, alpha=0.06, color="#ffd700")
    if capm_dates and capm_values:
        ax1.plot(capm_dates, capm_values, color="#ff7f7f", linewidth=1.2,
                 linestyle=":", label=f"CAPM 기대(β={capm['beta']:.2f})", zorder=2)

    # MDD 구간 음영
    if mdd_info and mdd_info["mdd_pct"] > 0:
        try:
            peak_d   = date.fromisoformat(mdd_info["peak_date"])
            trough_d = date.fromisoformat(mdd_info["trough_date"])
            if start <= peak_d <= today and start <= trough_d <= today:
                ax1.axvspan(peak_d, trough_d, alpha=0.12, color="#ff4444", zorder=1)
                ax1.axvline(peak_d,   color="#ff6666", linewidth=0.8, linestyle="--", alpha=0.6)
                ax1.axvline(trough_d, color="#ff4444", linewidth=0.8, linestyle="--", alpha=0.6)
                # MDD 텍스트 표시
                mid_d = peak_d + (trough_d - peak_d) / 2
                y_pos = ax1.get_ylim()[0] if ax1.get_ylim()[0] != 0 else min(pct_returns) * 1.1
                ax1.text(mid_d, y_pos, f"MDD\n-{mdd_info['mdd_pct']:.1f}%",
                         color="#ff8888", fontsize=7, ha="center", va="bottom", alpha=0.85)
        except Exception:
            pass

    ax1.axhline(0, color="#555555", linewidth=0.8)
    ax1.set_ylabel("수익률 (%)", color="#aaaaaa", fontsize=10)
    ax1.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=9,
               framealpha=0.8, loc="upper left")
    ax1.set_title(f"포트폴리오 성과 (최근 {days}일)", color="white", fontsize=13, pad=12)

    # CAPM + MDD 요약 텍스트 박스
    info_lines = []
    if capm:
        alpha_sign = "+" if capm["alpha_pct"] >= 0 else ""
        info_lines.append(f"β={capm['beta']:.2f}  α={alpha_sign}{capm['alpha_pct']:.1f}%  Sharpe={capm['sharpe']:.2f}")
    if mdd_info:
        rec = mdd_info['recovery_pct']
        info_lines.append(f"MDD=-{mdd_info['mdd_pct']:.1f}%  회복={rec:.0f}%")
    if info_lines:
        ax1.text(0.99, 0.05, "\n".join(info_lines), transform=ax1.transAxes,
                 color="#cccccc", fontsize=8, ha="right", va="bottom",
                 bbox=dict(facecolor="#1a1a2e", alpha=0.7, edgecolor="#444444", boxstyle="round,pad=0.4"))

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

    # MDD
    mdd = calc_mdd(days=max(days * 2, 90))
    if mdd:
        lines.append("")
        lines.append("<b>📉 최대낙폭 (MDD)</b>")
        mdd_em = "🔴" if mdd["mdd_pct"] > 20 else ("🟡" if mdd["mdd_pct"] > 10 else "🟢")
        lines.append(f"  {mdd_em} MDD: <b>-{mdd['mdd_pct']:.1f}%</b>")
        lines.append(f"  고점: {mdd['peak_date']}  ({mdd['peak_val']/1e4:,.0f}만원)")
        lines.append(f"  저점: {mdd['trough_date']}  ({mdd['trough_val']/1e4:,.0f}만원)")
        rec = mdd["recovery_pct"]
        rec_em = "✅" if rec >= 100 else ("🔄" if rec > 0 else "⏳")
        lines.append(f"  회복률: {rec_em} {rec:.0f}%")

    # 종목별 기여도
    contribs = calc_stock_contribution(days=days)
    if contribs:
        lines.append("")
        lines.append(f"<b>🏆 종목별 기여도 (최근 {days}일)</b>")
        for c in contribs:
            sign  = "+" if c["contrib_pct"] >= 0 else ""
            em    = "📈" if c["contrib_pct"] >= 0 else "📉"
            lines.append(
                f"  {em} {c['ticker']}: {sign}{c['contrib_pct']:.2f}%p "
                f"(수익률 {c['ret_pct']:+.1f}%, 비중 {c['weight_pct']:.0f}%)"
            )

    # CAPM 분석
    capm = calc_capm_metrics(days=max(days, 30))
    if capm:
        lines.append("")
        lines.append("<b>📐 CAPM 분석</b>")
        beta_em = "🔴" if capm["beta"] > 1.2 else ("🟡" if capm["beta"] > 0.8 else "🟢")
        alpha_sign = "+" if capm["alpha_pct"] >= 0 else ""
        alpha_em = "✅" if capm["alpha_pct"] >= 0 else "⚠️"
        lines.append(f"  베타(β):    {beta_em} <b>{capm['beta']:.3f}</b>")
        lines.append(f"  알파(α):    {alpha_em} <b>{alpha_sign}{capm['alpha_pct']:.2f}%</b>")
        lines.append(f"  샤프 비율: <b>{capm['sharpe']:.3f}</b>")

    return "\n".join(lines)
