#!/usr/bin/env python3
"""포트폴리오 일별 스냅샷 저장 + 성과 차트 생성."""

import io
import json
import logging
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
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


# ── 스냅샷 저장/로드 (30초 인메모리 캐시 — 같은 요청 내 4~5번 중복 파일 I/O 방지) ──────

_snap_cache: dict | None = None
_snap_cache_ts: float = 0.0
_SNAP_CACHE_TTL = 30  # seconds


def _load_snapshots() -> dict:
    global _snap_cache, _snap_cache_ts
    if _snap_cache is not None and (time.time() - _snap_cache_ts) < _SNAP_CACHE_TTL:
        return _snap_cache
    import storage
    _snap_cache = storage.load_snapshots()
    _snap_cache_ts = time.time()
    return _snap_cache


def _save_snapshots(data: dict):
    global _snap_cache, _snap_cache_ts
    import storage
    storage.save_snapshots(data)
    _snap_cache = data
    _snap_cache_ts = time.time()


# ── 매입 원가 계산 헬퍼 ──────────────────────────────────────────────────────

def _cost_krw(snap: dict, ref_fx: float = None) -> float:
    """스냅샷에서 매입 원가 총액(KRW 환산)을 계산.
    US 주식: avg_price(USD) × qty × ref_fx  (ref_fx = 매입 시점 환율 근사값, 고정값)
    KR 주식: avg_price(KRW) × qty
    ref_fx를 None으로 두면 snap의 당일 환율을 사용 (환율 효과 미반영 — 권장하지 않음).
    """
    fx = ref_fx if ref_fx is not None else snap.get("fx_rate", 1400)
    cost = 0.0
    for h in snap.get("holdings", {}).values():
        qty = h.get("qty", 0)
        avg = h.get("avg_price", 0)
        if avg <= 0 or qty <= 0:
            continue
        if "value_usd" in h:   # 미국 주식
            cost += avg * qty * fx
        else:                   # 국내 주식
            cost += avg * qty
    return cost


# ── 스냅샷 촬영 ───────────────────────────────────────────────────────────────

def take_snapshot() -> dict:
    """현재 포트폴리오 상태를 스냅샷으로 저장하고 반환."""
    today = str(date.today())
    data  = _load_snapshots()
    config = load_config()

    # 환율 (실패 시 캐시/1400 fallback)
    from utils import get_usdkrw
    fx_rate = get_usdkrw()["rate"]

    holdings = {}
    total_usd = 0.0
    kr_krw    = 0.0
    cash_krw  = 0
    cash_usd  = 0.0

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

        # 예수금 (총 성과 계산에는 미포함 — 대시보드 표시용)
        try:
            cash_usd = kis_api.get_us_cash()
        except Exception as e:
            logger.warning(f"Snapshot US cash error: {e}")

        # 국내
        try:
            kr_raw = kis_api.get_kr_balance_raw()
            cash_krw = kr_raw.get("total", {}).get("cash", 0)
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
        "total_krw": total_krw,   # 주식 평가금만 (성과 계산 기준 — 현금 미포함)
        "total_usd": round(total_usd, 2),
        "kr_krw":    round(kr_krw),
        "fx_rate":   round(fx_rate, 2),
        "cash_krw":  round(cash_krw),
        "cash_usd":  round(cash_usd, 2),
        "holdings":  holdings,
    }

    # ── 이상값 방어: 직전 스냅샷 대비 40% 이상 급락이면 저장 안 함 ──
    prev_snaps = sorted(data.items(), key=lambda x: x[0])
    if prev_snaps:
        prev_total = prev_snaps[-1][1].get("total_krw", 0)
        if prev_total > 0 and total_krw < prev_total * 0.6:
            logger.warning(
                f"Snapshot anomaly: {today} total_krw={total_krw:,.0f} "
                f"vs prev {prev_total:,.0f} ({total_krw/prev_total*100:.1f}%) — skipping save"
            )
            return snapshot  # 반환은 하되 저장 안 함

    data[today] = snapshot
    _save_snapshots(data)
    logger.info(f"Snapshot saved: {today} | {total_krw:,.0f} KRW")
    return snapshot


# ── 스냅샷 소급 생성 (backfill) ───────────────────────────────────────────────

def backfill_snapshots(days: int = 90) -> int:
    """
    현재 보유 종목의 yfinance 과거 가격으로 스냅샷을 소급 생성.
    이미 데이터가 있는 날짜는 덮어쓰지 않음.
    반환: 새로 생성된 날짜 수
    """
    data = _load_snapshots()

    # 보유 종목 가져오기
    us_holdings, kr_holdings = [], []
    if kis_api.is_configured():
        try:
            us_raw = kis_api.get_us_balance_raw()
            us_holdings = us_raw.get("holdings", [])
        except Exception as e:
            logger.warning(f"Backfill KIS US error: {e}")
        try:
            kr_raw = kis_api.get_kr_balance_raw()
            kr_holdings = kr_raw.get("holdings", [])
        except Exception as e:
            logger.warning(f"Backfill KIS KR error: {e}")

    if not us_holdings and not kr_holdings:
        logger.warning("Backfill: 보유 종목 없음")
        return 0

    today = date.today()
    start = today - timedelta(days=days)

    # 환율 히스토리
    fx_hist = {}
    try:
        fx_df = yf.Ticker("USDKRW=X").history(period=f"{days + 10}d")
        for idx, row in fx_df.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            fx_hist[d] = row["Close"]
    except Exception as e:
        logger.warning(f"Backfill FX error: {e}")

    # 미국 종목 가격 히스토리
    us_price_hist: dict[str, dict] = {}
    for h in us_holdings:
        ticker = h["ticker"]
        try:
            df = yf.Ticker(ticker).history(period=f"{days + 10}d")
            us_price_hist[ticker] = {}
            for idx, row in df.iterrows():
                d = idx.date() if hasattr(idx, "date") else idx
                us_price_hist[ticker][d] = row["Close"]
        except Exception as e:
            logger.warning(f"Backfill {ticker} error: {e}")

    # 날짜별 스냅샷 생성
    added = 0
    current = start
    while current <= today:
        date_str = str(current)
        # 주말 스킵 (S&P500 거래일 기준에 맞추기 위해)
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue
        if date_str in data:
            current += timedelta(days=1)
            continue

        fx = fx_hist.get(current) or fx_hist.get(current - timedelta(days=1)) or fx_hist.get(current - timedelta(days=2)) or 1400.0

        total_usd = 0.0
        kr_krw = 0.0
        holdings = {}

        for h in us_holdings:
            ticker = h["ticker"]
            hist = us_price_hist.get(ticker, {})
            price = hist.get(current) or hist.get(current - timedelta(days=1)) or hist.get(current - timedelta(days=2))
            if price is None:
                continue
            value = price * h["qty"]
            total_usd += value
            holdings[ticker] = {
                "qty": h["qty"],
                "price": round(price, 4),
                "avg_price": h["avg_price"],
                "value_usd": round(value, 2),
            }

        for h in kr_holdings:
            val = h.get("eval_amt", 0)
            kr_krw += val
            holdings[h["ticker"]] = {
                "qty": h["qty"],
                "price": h["curr_price"],
                "avg_price": h["avg_price"],
                "value_krw": val,
            }

        total_krw = round(total_usd * fx + kr_krw)
        if total_krw == 0:
            current += timedelta(days=1)
            continue

        data[date_str] = {
            "total_krw": total_krw,
            "total_usd": round(total_usd, 2),
            "kr_krw": round(kr_krw),
            "fx_rate": round(fx, 2),
            "holdings": holdings,
            "backfilled": True,
        }
        added += 1
        current += timedelta(days=1)

    if added > 0:
        _save_snapshots(data)
        logger.info(f"Backfill 완료: {added}일 소급 생성")
    return added


# ── 이상값 스냅샷 정리 ───────────────────────────────────────────────────────

def clean_anomalous_snapshots(threshold: float = 0.6) -> int:
    """
    두 가지 이상값을 제거:
    1. 주말 날짜 스냅샷 (장 휴장일에 잘못 저장된 가격)
    2. 직전/다음 대비 모두 40% 이상 낮은 이상값 (threshold 기준)
    반환: 제거된 항목 수
    """
    data = _load_snapshots()
    if not data:
        return 0

    to_remove = []

    # ① 주말 항목 제거
    for date_str in list(data.keys()):
        try:
            d = date.fromisoformat(date_str)
            if d.weekday() >= 5:  # 토(5) / 일(6)
                to_remove.append(date_str)
                logger.info(f"Removing weekend snapshot: {date_str}")
        except ValueError:
            pass

    # ② 직전/다음 대비 threshold 이하인 이상값 제거 (3개 이상일 때)
    if len(data) >= 3:
        sorted_items = sorted(
            [(k, v) for k, v in data.items() if k not in to_remove],
            key=lambda x: x[0],
        )
        for i in range(1, len(sorted_items) - 1):
            date_str, snap = sorted_items[i]
            prev_total = sorted_items[i - 1][1].get("total_krw", 0)
            next_total = sorted_items[i + 1][1].get("total_krw", 0)
            curr_total = snap.get("total_krw", 0)
            if (prev_total > 0 and next_total > 0
                    and curr_total < prev_total * threshold
                    and curr_total < next_total * threshold):
                to_remove.append(date_str)
                logger.info(
                    f"Removing anomalous snapshot: {date_str} "
                    f"({curr_total:,.0f} KRW | prev={prev_total:,.0f} next={next_total:,.0f})"
                )

    if to_remove:
        for d in to_remove:
            data.pop(d, None)
        _save_snapshots(data)

    return len(to_remove)


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
    # 현재 보유 중인 종목만 계산 (매도 종목은 total_krw 변화에 이미 반영됨)
    all_tickers = set(last_holdings)

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

def _calc_stock_beta(yf_ticker: str, benchmark: str, period: str = "2y") -> Optional[float]:
    """
    개별 종목의 베타를 2년치 일별 수익률로 계산.
    인덱스를 date 객체로 변환 후 concat → timezone 충돌 방지.
    """
    try:
        s_hist = yf.Ticker(yf_ticker).history(period=period)["Close"]
        m_hist = yf.Ticker(benchmark).history(period=period)["Close"]
        if s_hist.empty or m_hist.empty:
            return None
        s_ret = s_hist.pct_change(fill_method=None)
        m_ret = m_hist.pct_change(fill_method=None)
        # timezone-aware DatetimeIndex → date 객체로 통일 (US/KR 시장 timezone 충돌 방지)
        s_ret.index = [i.date() if hasattr(i, "date") else i for i in s_ret.index]
        m_ret.index = [i.date() if hasattr(i, "date") else i for i in m_ret.index]
        df = pd.concat([s_ret.rename("s"), m_ret.rename("m")], axis=1).dropna()
        if len(df) < 60:
            return None
        cov = np.cov(df["s"].values, df["m"].values)
        return float(cov[0, 1] / cov[1, 1]) if cov[1, 1] != 0 else None
    except Exception:
        return None


def _portfolio_beta_from_holdings(snap: dict, fx_rate: float) -> Optional[float]:
    """
    보유 종목별 2년치 히스토리로 개별 베타를 구한 뒤 시장가치 비중으로 가중합산.
    - 미국 주식: S&P 500 (^GSPC) 기준
    - 한국 주식: KOSDAQ (^KQ11) 우선, 실패 시 KOSPI (^KS11) fallback
    """
    holdings = snap.get("holdings", {})
    if not holdings:
        return None

    items = []
    for ticker, h in holdings.items():
        qty = h.get("qty", 0)
        if qty <= 0:
            continue
        curr = h.get("price", h.get("avg_price", 0))
        is_us = "value_usd" in h
        if is_us:
            value_krw = curr * qty * fx_rate
            items.append({"ticker": ticker, "yf_ticker": ticker,
                          "benchmark": "^GSPC", "value_krw": value_krw, "is_us": True})
        else:
            value_krw = curr * qty
            items.append({"ticker": ticker, "yf_ticker": ticker + ".KQ",
                          "benchmark": "^KQ11", "value_krw": value_krw, "is_us": False})

    total_value = sum(i["value_krw"] for i in items)
    if total_value == 0:
        return None

    def _fetch(item):
        beta = _calc_stock_beta(item["yf_ticker"], item["benchmark"])
        # KR 종목: KOSDAQ 실패 시 KOSPI fallback
        if beta is None and not item["is_us"]:
            beta = _calc_stock_beta(item["ticker"] + ".KS", "^KS11")
        return item, beta

    portfolio_beta = 0.0
    covered_weight = 0.0

    with ThreadPoolExecutor(max_workers=max(len(items), 1)) as ex:
        for item, beta in ex.map(_fetch, items):
            weight = item["value_krw"] / total_value
            if beta is not None:
                portfolio_beta += weight * beta
                covered_weight += weight

    if covered_weight < 0.5:
        return None

    # 커버 안 된 비중은 베타 1.0 (시장 평균) 가정
    portfolio_beta += (1.0 - covered_weight) * 1.0
    return round(portfolio_beta, 3)


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
    if len(data) < 7:
        return {}

    today = date.today()
    start = today - timedelta(days=days)

    filtered = sorted(
        [(date.fromisoformat(d), v) for d, v in data.items()
         if start <= date.fromisoformat(d) <= today],
        key=lambda x: x[0],
    )
    if len(filtered) < 7:
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
        if len(valid) < 7:
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

    # ── 베타 계산: 종목별 2년치 히스토리 가중합 ──
    # 스냅샷 기간이 짧아도 안정적인 베타를 얻기 위해 개별 종목 장기 히스토리 사용.
    # 미국 종목 → S&P500 기준 / 한국 종목 → KOSDAQ(KOSPI) 기준으로 각각 계산 후 비중 합산.
    latest_snap = filtered[-1][1]
    snap_fx = latest_snap.get("fx_rate", 1400)
    beta = _portfolio_beta_from_holdings(latest_snap, snap_fx)
    beta_source = "history"

    if beta is None:
        # 히스토리 계산 실패 시 스냅샷 기반 fallback
        cov_matrix = np.cov(port_r, mkt_r)
        beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] != 0 else 1.0
        beta_source = "snapshot"

    n_days = len(port_r)

    # ── 기간 수익률 (기하평균 — 복리 기준, 올바른 방식) ──
    # 산술평균 연율화((1+mean)^252)는 변동성이 클수록 부풀어 실제보다 과대 표시됨.
    # 기하 총수익률을 먼저 구한 뒤 연율화해야 정확함.
    total_port = float(np.prod(1 + port_r)) - 1      # 기간 내 실제 누적수익률
    total_mkt  = float(np.prod(1 + mkt_r))  - 1

    # ── 연율화 (기하) ──
    actual_annual = (1 + total_port) ** (252 / n_days) - 1
    mkt_annual    = (1 + total_mkt)  ** (252 / n_days) - 1

    # ── CAPM 기대수익률 ──
    # 단기 실현수익률을 연율화해 쓰면 시장 급락기에 -50%+ 이상한 값이 나옴.
    # CAPM은 장기 기대수익률 모델 → ERP는 장기 평균(Damodaran ~5.5%) 사용.
    ERP_LONGRUN = 0.055
    expected_annual = rf_annual + beta * ERP_LONGRUN
    # 기간 환산 (n_days 기준, 연율과 비교 가능하도록)
    expected_period = (1 + expected_annual) ** (n_days / 252) - 1

    # ── Jensen's Alpha (연율 기준) ──
    alpha = actual_annual - expected_annual

    # ── 샤프 비율 (연율화) ──
    excess_ret = port_r - rf_daily
    sharpe = (excess_ret.mean() / excess_ret.std() * np.sqrt(252)) if excess_ret.std() != 0 else 0.0

    # ── 트레이너 비율 (연율화) ──
    treynor = ((port_r.mean() - rf_daily) * 252) / beta if beta != 0 else 0.0

    return {
        "beta":             round(float(beta), 3),
        "beta_source":      beta_source,   # "history" | "snapshot"
        "alpha_pct":        round(float(alpha * 100), 2),
        "sharpe":           round(float(sharpe), 3),
        "treynor_pct":      round(float(treynor * 100), 2),
        "actual_period_pct":   round(total_port * 100, 2),          # 기간 실제 수익률
        "expected_period_pct": round(expected_period * 100, 2),     # 기간 CAPM 기대
        "actual_pct":       round(float(actual_annual * 100), 2),   # 연율환산 (참고용)
        "expected_pct":     round(float(expected_annual * 100), 2), # 연율 CAPM 기대
        "rf_pct":           round(float(rf_annual * 100), 2),
        "mkt_pct":          round(float(mkt_annual * 100), 2),
        "erp_pct":          round(ERP_LONGRUN * 100, 1),
        "n_days":           n_days,
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

    # ── 매입가 기준 수익률 (환율 효과 포함) ──
    # US 주식 원가는 매입 시점 환율로 고정해야 환율 손익이 평가금에 반영됨.
    # 첫 스냅샷 환율을 매입 시점 환율 근사값으로 사용.
    ref_fx = filtered[0][1].get("fx_rate", 1400)
    costs = [_cost_krw(snap, ref_fx=ref_fx) for _, snap in filtered]
    pct_returns = [
        (v / c - 1) * 100 if c > 0 else 0.0
        for v, c in zip(values, costs)
    ]

    # S&P500 비교 — 포트폴리오 첫날 수익률에 맞춰 앵커
    sp = _sp500_returns(dates[0], dates[-1])
    sp_dates  = sorted([date.fromisoformat(d) for d in sp if start <= date.fromisoformat(d) <= today])
    sp_values = [sp[str(d)] for d in sp_dates if str(d) in sp]
    sp_offset = pct_returns[0] if pct_returns else 0.0
    sp_values = [v + sp_offset for v in sp_values]

    # CAPM 기대수익률 선 — 동일하게 앵커
    capm = calc_capm_metrics(days=max(days, 30))
    capm_dates, capm_values = [], []
    if capm and capm["beta"] and sp_dates:
        try:
            rf_annual = capm["rf_pct"] / 100
            beta      = capm["beta"]
            rf_daily  = rf_annual / 252
            capm_dates = sp_dates
            capm_values = []
            for i, sp_pct in enumerate(sp_values):
                n = i + 1
                rf_cum   = ((1 + rf_daily) ** n - 1) * 100
                # sp_pct는 이미 offset 포함이므로 원래 sp 수익률로 복원
                sp_raw   = sp_pct - sp_offset
                capm_cum = rf_cum + beta * (sp_raw - rf_cum)
                capm_values.append(capm_cum + sp_offset)
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
    ax1.set_ylabel("수익률 % (매입가 기준)", color="#aaaaaa", fontsize=10)
    ax1.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=9,
               framealpha=0.8, loc="upper left")
    fx_chg = (dates[-1] != dates[0]) and ref_fx > 0
    fx_label = f"FX ref {ref_fx:.0f}" if fx_chg else ""
    ax1.set_title(f"포트폴리오 성과 (최근 {days}일 | 매입가+환율 기준)", color="white", fontsize=13, pad=12)

    # CAPM + MDD 요약 텍스트 박스
    info_lines = []
    if capm:
        alpha_sign = "+" if capm["alpha_pct"] >= 0 else ""
        info_lines.append(f"β={capm['beta']:.2f}  α={alpha_sign}{capm['alpha_pct']:.1f}%  Sharpe={capm['sharpe']:.2f}")
    if mdd_info:
        rec = mdd_info['recovery_pct']
        info_lines.append(f"MDD=-{mdd_info['mdd_pct']:.1f}%  회복={rec:.0f}%")
    if fx_label:
        info_lines.append(fx_label)
    if info_lines:
        ax1.text(0.99, 0.05, "\n".join(info_lines), transform=ax1.transAxes,
                 color="#cccccc", fontsize=8, ha="right", va="bottom",
                 bbox=dict(facecolor="#1a1a2e", alpha=0.7, edgecolor="#444444", boxstyle="round,pad=0.4"))

    # 하단: 절대 평가금액 (만원), y축은 데이터 범위에 맞게 zoom
    vals_만 = [v / 1e4 for v in values]
    ax2.plot(dates, vals_만, color="#00a8ff", linewidth=2)
    _min, _max = min(vals_만), max(vals_만)
    _margin = (_max - _min) * 0.25 or _max * 0.01
    ax2.set_ylim(_min - _margin, _max + _margin)
    ax2.fill_between(dates, vals_만, _min - _margin, alpha=0.12, color="#00a8ff")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}만"))
    ax2.set_ylabel("평가금액", color="#aaaaaa", fontsize=10)

    # 수익 요약 텍스트 (매입가 기준 현재 수익률)
    total_pct  = pct_returns[-1]
    total_gain = values[-1] - costs[-1]   # 미실현 손익 (현재 평가금 - 매입 원가)
    color_txt  = "#00d4aa" if total_pct >= 0 else "#ff5555"
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

    # US 주식 원가는 첫 스냅샷 환율로 고정 (환율 효과를 평가금에 반영)
    ref_fx = first_v.get("fx_rate", 1400)

    curr = last_v["total_krw"]
    cost = _cost_krw(last_v, ref_fx=ref_fx)   # 매입 원가 (환율 고정)
    pct  = (curr / cost - 1) * 100 if cost > 0 else 0.0
    gain = curr - cost                         # 미실현 손익 (환율 손익 포함)
    sign  = "+" if pct >= 0 else ""
    arrow = "📈" if pct >= 0 else "📉"

    # 기간 내 변화 (보조 지표)
    cost_first = _cost_krw(first_v, ref_fx=ref_fx)
    base_first = first_v["total_krw"]
    pct_first  = (base_first / cost_first - 1) * 100 if cost_first > 0 else 0.0
    period_chg = pct - pct_first         # 기간 동안 수익률 변화폭
    period_sign = "+" if period_chg >= 0 else ""

    lines = [
        f"{arrow} <b>포트폴리오 성과 (매입가 기준)</b>",
        f"기간: {first_d} → {last_d}",
        f"수익률: <b>{sign}{pct:.2f}%</b>  <i>(기간 중 {period_sign}{period_chg:.2f}%p 변화)</i>",
        f"평가손익: <b>{sign}{gain / 1e4:,.1f}만원</b>",
        f"현재 평가금: {curr / 1e4:,.0f}만원",
        f"매입 원가:   {cost / 1e4:,.0f}만원",
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

        # ── 베타 ──
        b = capm["beta"]
        beta_em = "🔴" if b > 1.2 else ("🟡" if b > 0.8 else "🟢")
        if b > 1.5:
            beta_desc = f"시장 변동의 {b:.1f}배 → 매우 공격적"
        elif b > 1.2:
            beta_desc = f"시장 변동의 {b:.1f}배 → 공격형"
        elif b > 0.8:
            beta_desc = "시장과 비슷한 변동성"
        elif b > 0.5:
            beta_desc = "시장보다 덜 민감한 방어형"
        else:
            beta_desc = "매우 낮은 변동성 (고방어)"
        src_tag = "" if capm.get("beta_source") == "history" else " <i>(단기추정)</i>"
        lines.append(f"  베타(β):   {beta_em} <b>{b:.3f}</b>{src_tag}")
        lines.append(f"             └ {beta_desc}")

        # ── 알파 ──
        a = capm["alpha_pct"]
        alpha_sign = "+" if a >= 0 else ""
        alpha_em = "✅" if a >= 0 else "⚠️"
        if a >= 5:
            alpha_desc = "시장 기대치 대비 큰 초과수익 중 🎯"
        elif a >= 1:
            alpha_desc = "시장 기대치 이상 달성 (양호)"
        elif a >= -1:
            alpha_desc = "시장 기대치 수준 (보통)"
        elif a >= -5:
            alpha_desc = "시장 기대치 하회 (부진)"
        else:
            alpha_desc = "시장 기대치 크게 하회 ⚠️"
        lines.append(f"  알파(α):   {alpha_em} <b>{alpha_sign}{a:.2f}%</b>")
        lines.append(f"             └ {alpha_desc}")

        # ── 샤프 비율 ──
        s = capm["sharpe"]
        if s >= 1.5:
            sharpe_em, sharpe_desc = "🏆", "리스크 대비 수익 매우 우수"
        elif s >= 1.0:
            sharpe_em, sharpe_desc = "✅", "리스크 대비 수익 우수"
        elif s >= 0.5:
            sharpe_em, sharpe_desc = "🟡", "리스크 대비 수익 양호"
        elif s >= 0:
            sharpe_em, sharpe_desc = "🟠", "리스크 대비 수익 저조"
        else:
            sharpe_em, sharpe_desc = "🔴", "무위험 수익률에도 미달"
        lines.append(f"  샤프:      {sharpe_em} <b>{s:.3f}</b>")
        lines.append(f"             └ {sharpe_desc}")

        # ── 실제 vs 기대 수익률 ──
        lines.append("")
        ap  = capm["actual_period_pct"]
        ep  = capm["expected_period_pct"]
        aa  = capm["actual_pct"]
        ea  = capm["expected_pct"]
        ap_sign = "+" if ap >= 0 else ""
        ep_sign = "+" if ep >= 0 else ""
        lines.append(f"  실제 수익률: <b>{ap_sign}{ap:.1f}%</b>  <i>({capm['n_days']}일 / 연율 {aa:+.1f}%)</i>")
        lines.append(f"  CAPM 기대치: {ep_sign}{ep:.1f}%  <i>({capm['n_days']}일 / 연율 {ea:+.1f}%)</i>")
        lines.append(
            f"  <i>(Rf {capm['rf_pct']:.1f}% | ERP {capm['erp_pct']:.1f}% 장기평균"
            f" | S&P500 최근 {capm['mkt_pct']:+.1f}% | {capm['n_days']}일 기준)</i>"
        )

    return "\n".join(lines)
