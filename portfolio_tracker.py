#!/usr/bin/env python3
"""포트폴리오 일별 스냅샷 저장 + 성과 차트 생성."""

import bisect
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


# ── 총자산 계산 헬퍼 ─────────────────────────────────────────────────────────

def _asset_krw(snap: dict, include_cash: bool = True) -> float:
    """총자산 = 주식 평가금 + 예수금(KRW+USD 환산).
    현금 필드가 없는 과거 스냅샷은 주식 평가금만 반환 (당시 현금 미기록).
    부분매도로 주식→현금 이동 시 총자산은 변하지 않아야 차트 왜곡이 없음.
    include_cash=False면 예수금을 빼고 주식 평가금만 반환 (기준 통일용).
    """
    if not include_cash:
        return snap.get("total_krw") or 0
    fx = snap.get("fx_rate") or 1400
    return (snap.get("total_krw") or 0) \
        + (snap.get("cash_krw") or 0) \
        + (snap.get("cash_usd") or 0.0) * fx


def _has_cash_fields(snap: dict) -> bool:
    return snap.get("cash_krw") is not None or snap.get("cash_usd") is not None


def _asset_series(snaps: list) -> tuple:
    """수익률 계산용 총자산 시계열. (values, cash_included)

    예수금 기록은 2026-07-14부터 시작됨 — 기록 있는 날과 없는 날이 한 창에 섞이면
    경계일에 예수금 규모만큼 가짜 급등/급락이 생긴다(7/14 +14%, 7/20 -11%).
    창 안에서 기준이 섞이면 주식 평가금만으로 통일한다.
    """
    include = all(_has_cash_fields(s) for s in snaps)
    return [_asset_krw(s, include) for s in snaps], include


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

    # 조회 성공 여부 — 한쪽이 실패했는데도 저장하면 반쪽짜리 스냅샷이
    # 그대로 -20% 수익률로 잡힌다 (2026-06-12, 06-17 사례)
    us_ok = kr_ok = True

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
            us_ok = False
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
            kr_ok = False
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
        "total_krw": total_krw,   # 주식 평가금만 (매입가 기준 수익률 계산용)
        "total_usd": round(total_usd, 2),
        "kr_krw":    round(kr_krw),
        "fx_rate":   round(fx_rate, 2),
        "cash_krw":  round(cash_krw),
        "cash_usd":  round(cash_usd, 2),
        "holdings":  holdings,
    }
    snapshot["asset_krw"] = round(_asset_krw(snapshot))  # 총자산 (주식+현금)

    prev_snaps = sorted(data.items(), key=lambda x: x[0])
    prev_snap  = prev_snaps[-1][1] if prev_snaps else None

    # ── 방어 ①: 조회 실패로 한쪽이 통째로 빠진 반쪽 스냅샷 ──
    # 직전엔 있던 종목군이 '조회 예외' 때문에 사라졌다면 저장하지 않는다.
    # 실제 전량매도(조회는 성공, 종목 0개)는 정상 저장돼야 하므로 예외 발생
    # 여부로만 판단한다.
    if prev_snap:
        def _leg_counts(snap):
            hs = snap.get("holdings", {}) or {}
            us = sum(1 for h in hs.values() if "value_usd" in h)
            return us, len(hs) - us

        prev_us, prev_kr = _leg_counts(prev_snap)
        curr_us, curr_kr = _leg_counts(snapshot)
        missing = []
        if not us_ok and prev_us > 0 and curr_us < prev_us:
            missing.append(f"US {prev_us}→{curr_us}")
        if not kr_ok and prev_kr > 0 and curr_kr < prev_kr:
            missing.append(f"KR {prev_kr}→{curr_kr}")
        if missing:
            logger.warning(
                f"Snapshot partial fetch: {today} ({', '.join(missing)}) — skipping save"
            )
            return snapshot  # 반환은 하되 저장 안 함

    # ── 방어 ②: 종목이 줄었는데 총자산도 같이 급감한 경우 ──
    # 예외 없이 빈 목록이 오는 soft failure를 잡는다. 진짜 매도라면 대금이
    # 예수금으로 들어와 총자산은 유지되므로, 종목 감소 + 총자산 15% 이상
    # 급감은 조회 누락으로 본다. 양쪽 다 예수금이 기록된 경우에만 적용.
    if prev_snap and _has_cash_fields(prev_snap) and _has_cash_fields(snapshot):
        prev_n = len(prev_snap.get("holdings", {}) or {})
        curr_n = len(snapshot.get("holdings", {}) or {})
        prev_asset = _asset_krw(prev_snap)
        curr_asset = snapshot["asset_krw"]
        if (curr_n < prev_n and prev_asset > 0
                and curr_asset < prev_asset * 0.85):
            logger.warning(
                f"Snapshot holdings shrank without cash offset: {today} "
                f"종목 {prev_n}→{curr_n}, asset {prev_asset:,.0f}→{curr_asset:,.0f} "
                f"({curr_asset/prev_asset*100:.1f}%) — skipping save"
            )
            return snapshot  # 반환은 하되 저장 안 함

    # ── 방어 ③: 직전 스냅샷 대비 총자산 40% 이상 급락이면 저장 안 함 ──
    # (주식만 비교하면 부분매도→현금 이동이 급락으로 오탐됨)
    if prev_snap:
        prev_asset = _asset_krw(prev_snap)
        curr_asset = snapshot["asset_krw"]
        if prev_asset > 0 and curr_asset < prev_asset * 0.6:
            logger.warning(
                f"Snapshot anomaly: {today} asset_krw={curr_asset:,.0f} "
                f"vs prev {prev_asset:,.0f} ({curr_asset/prev_asset*100:.1f}%) — skipping save"
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

    # 총자산 기준 — 부분매도(주식→현금)가 낙폭으로 잡히지 않도록
    filtered = sorted(
        [(date.fromisoformat(d), _asset_krw(v)) for d, v in data.items()
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


def _portfolio_beta_from_holdings(snap: dict, fx_rate: float) -> Optional[dict]:
    """
    보유 종목별 2년치 히스토리로 개별 베타를 구한 뒤 시장가치 비중으로 가중합산.
    - 미국 주식: S&P 500 (^GSPC) 기준
    - 한국 주식: KOSDAQ (^KQ11) 우선, 실패 시 KOSPI (^KS11) fallback

    반환: {"beta": float, "us_weight": float} — us_weight는 알파 계산 시
    벤치마크를 동일한 비중으로 블렌딩하기 위해 함께 돌려준다.
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
    us_weight = sum(i["value_krw"] for i in items if i["is_us"]) / total_value
    return {"beta": round(portfolio_beta, 3), "us_weight": us_weight}


# 외부 현금흐름/데이터 결손 판정 임계치
# 하루 절대변동이 크면서 '시장으로 설명 안 되는' 잔차까지 큰 날만 끊는다.
# (실제 시장 급락일은 잔차가 작으므로 그대로 유지됨)
_FLOW_ABS_THRESHOLD   = 0.08   # 일수익률 절대값 8%
_FLOW_RESID_THRESHOLD = 0.05   # 시장 대비 잔차 5%
# 연율 알파를 표시해도 되는 최소 관측 기간 (일)
_ANNUAL_ALPHA_MIN_DAYS = 120


def _fetch_index_returns(symbol: str, dates: list, period_days: int) -> Optional[list]:
    """dates의 인접 쌍마다 지수 수익률을 반환. 종가가 없는 날은 직전 거래일 종가 사용.

    스냅샷은 일~목에 저장되는데 일요일엔 지수 종가가 없다. 날짜를 정확히
    매칭하면 일요일과 (일→월) 쌍이 통째로 버려져 주당 2일씩 날아간다.
    '해당일 이하의 마지막 종가'로 찾으면 체인이 끊기지 않는다.
    """
    try:
        hist = yf.Ticker(symbol).history(period=f"{period_days}d")
        if hist.empty:
            return None
        px = {}
        for idx, row in hist.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            px[d] = row["Close"]
        idx_dates = sorted(px)

        def close_on_or_before(d):
            i = bisect.bisect_right(idx_dates, d) - 1
            return px[idx_dates[i]] if i >= 0 else None

        out = []
        for i in range(1, len(dates)):
            pa = close_on_or_before(dates[i - 1])
            pb = close_on_or_before(dates[i])
            out.append((pb / pa) - 1 if (pa and pb) else None)
        return out
    except Exception as e:
        logger.warning(f"Index fetch error {symbol}: {e}")
        return None


def _fx_series(snaps: list) -> list:
    """스냅샷의 환율 시계열. 조회 실패 fallback(1400)이나 비정상 점프는 직전값 유지."""
    out, prev = [], None
    for s in snaps:
        f = s.get("fx_rate") or 0
        if not f or (prev and abs(f / prev - 1) > 0.03):
            f = prev or 1400
        out.append(f)
        prev = f
    return out


def calc_capm_metrics(days: int = 90) -> dict:
    """
    포트폴리오의 CAPM 지표를 계산합니다.
    - beta: 시장 대비 민감도 (종목별 2년 히스토리 가중합)
    - alpha: Jensen's Alpha — 기간 기준이 기본값. 연율 알파는 관측기간이
      180일 이상일 때만 산출한다 (짧은 창을 연율화하면 알파가 수백 %로 튐).
    - sharpe / treynor
    벤치마크는 포트폴리오와 동일한 원화 기준으로 맞춘다:
      w_us × (S&P500 원화환산) + w_kr × KOSDAQ
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

    dates = [d for d, _ in filtered]
    snaps = [v for _, v in filtered]

    # 총자산 기준 — 부분매도일이 가짜 급락 수익률로 잡히지 않도록.
    # 창 안에 예수금 기록 유무가 섞이면 주식 평가금 기준으로 통일한다.
    values, cash_included = _asset_series(snaps)
    values = np.array(values, dtype=float)
    if (values[:-1] <= 0).any():
        return {}
    port_ret = np.diff(values) / values[:-1]

    # ── 베타 계산: 종목별 2년치 히스토리 가중합 ──
    # 스냅샷 기간이 짧아도 안정적인 베타를 얻기 위해 개별 종목 장기 히스토리 사용.
    # 미국 종목 → S&P500 기준 / 한국 종목 → KOSDAQ(KOSPI) 기준으로 각각 계산 후 비중 합산.
    latest_snap = snaps[-1]
    snap_fx = latest_snap.get("fx_rate", 1400)
    beta_info   = _portfolio_beta_from_holdings(latest_snap, snap_fx)
    beta        = beta_info["beta"] if beta_info else None
    us_weight   = beta_info["us_weight"] if beta_info else 1.0
    beta_source = "history" if beta_info else "snapshot"

    # ── 벤치마크: 포트폴리오와 같은 원화 기준으로 맞춤 ──
    # 포트폴리오 수익률은 원화 표시(환율 변동 포함)인데 S&P500만 쓰면
    # 원/달러 변동분이 통째로 알파에 섞인다. 달러 지수는 원화 환산하고,
    # 한국 비중은 KOSDAQ으로 블렌딩한다 (베타를 뽑은 기준과 동일).
    sp_ret = _fetch_index_returns("^GSPC", dates, days + 20)
    if sp_ret is None:
        return {}
    kq_ret = _fetch_index_returns("^KQ11", dates, days + 20)
    if kq_ret is None:
        kq_ret = _fetch_index_returns("^KS11", dates, days + 20)

    fx = _fx_series(snaps)
    mkt_list, mkt_basis = [], "blended"
    if kq_ret is None:
        mkt_basis = "sp500_krw"   # KOSDAQ 조회 실패 → S&P500(원화) 단독
    for i, sp in enumerate(sp_ret):
        if sp is None:
            mkt_list.append(None)
            continue
        fx_ret = fx[i + 1] / fx[i] - 1
        sp_krw = (1 + sp) * (1 + fx_ret) - 1
        kq = kq_ret[i] if kq_ret else None
        if kq is None:
            mkt_list.append(sp_krw)
        else:
            mkt_list.append(us_weight * sp_krw + (1 - us_weight) * kq)

    # ── 외부 현금흐름·데이터 결손 제거 (TWR 링크 끊기) ──
    # 입출금이나 보유종목 조회 누락은 수익률이 아니다. 변동이 크면서
    # 시장으로 설명되지 않는 구간은 체인에서 제외하고 나머지만 연결한다.
    kept, excluded = [], []
    for i, (p, m) in enumerate(zip(port_ret, mkt_list)):
        if m is None:
            continue
        if abs(p) > _FLOW_ABS_THRESHOLD and abs(p - m) > _FLOW_RESID_THRESHOLD:
            excluded.append({"date": dates[i + 1].isoformat(), "ret_pct": round(p * 100, 2)})
            continue
        kept.append((p, m, dates[i], dates[i + 1]))

    if len(kept) < 7:
        return {}

    port_r = np.array([k[0] for k in kept])
    mkt_r  = np.array([k[1] for k in kept])
    n_obs  = len(kept)
    # 실제 경과일수 — 주말·휴일 갭과 제외 구간을 반영해야 연율화가 부풀지 않는다.
    elapsed_days = sum((k[3] - k[2]).days for k in kept)
    if elapsed_days <= 0:
        return {}

    # 무위험 이자율 (미국 10년 국채)
    try:
        tnx = yf.Ticker("^TNX").fast_info.last_price  # 연율 %
        rf_annual = (tnx or 4.3) / 100
    except Exception:
        rf_annual = 0.043  # fallback 4.3%

    # ── 기간 수익률 (기하 — 복리 기준) ──
    total_port = float(np.prod(1 + port_r)) - 1
    total_mkt  = float(np.prod(1 + mkt_r))  - 1

    # ── 연율화: 거래일 수가 아니라 실제 경과일수 기준 ──
    ann_exp       = 365 / elapsed_days
    actual_annual = (1 + total_port) ** ann_exp - 1
    mkt_annual    = (1 + total_mkt)  ** ann_exp - 1

    # ── 베타 fallback (히스토리 실패 시 스냅샷 회귀) ──
    if beta is None:
        cov_matrix = np.cov(port_r, mkt_r)
        beta = float(cov_matrix[0, 1] / cov_matrix[1, 1]) if cov_matrix[1, 1] != 0 else 1.0

    # ── CAPM 기대수익률 ──
    # 두 가지를 구분해서 낸다:
    #  (1) 장기 기대수익 — ERP는 장기 평균(Damodaran ~5.5%). "이 베타면 장기적으로
    #      이 정도" 라는 모델값이며, 실적 평가 기준으로 쓰면 안 된다.
    #  (2) Jensen's Alpha — 같은 기간의 '실현' 시장수익률로 벤치마킹한 교과서 정의.
    #      (1)로 알파를 내면 시장이 빠진 구간에선 실력과 무관하게 항상 음수가 된다.
    ERP_LONGRUN = 0.055
    expected_annual = rf_annual + beta * ERP_LONGRUN
    expected_period = (1 + expected_annual) ** (elapsed_days / 365) - 1

    # ── Jensen's Alpha (기간 기준) ──
    # 짧은 창의 실현수익률을 연율화해서 빼면 21거래일 +10%가 +212%로 증폭돼
    # 알파가 노이즈가 된다. 기본은 기간 기준, 연율 알파는 관측기간이 충분할 때만.
    long_enough  = elapsed_days >= _ANNUAL_ALPHA_MIN_DAYS
    rf_period    = (1 + rf_annual) ** (elapsed_days / 365) - 1
    capm_period  = rf_period + beta * (total_mkt - rf_period)   # 실현 기준 기대수익
    alpha_period = total_port - capm_period
    capm_annual  = (1 + capm_period) ** ann_exp - 1
    alpha_annual = actual_annual - capm_annual if long_enough else None

    # ── 샤프 비율 (관측 빈도로 연율화) ──
    periods_per_year = n_obs * 365 / elapsed_days
    rf_per_obs = rf_annual / periods_per_year
    excess_ret = port_r - rf_per_obs
    sharpe = (excess_ret.mean() / excess_ret.std() * np.sqrt(periods_per_year)) \
        if excess_ret.std() != 0 else 0.0

    # ── 트레이너 비율 (연율, 기하 기준) ──
    treynor = (actual_annual - rf_annual) / beta if beta != 0 else 0.0

    return {
        "beta":             round(float(beta), 3),
        "beta_source":      beta_source,   # "history" | "snapshot"
        "alpha_pct":        round(float(alpha_period * 100), 2),   # 기간 기준 (기본)
        "alpha_period_pct": round(float(alpha_period * 100), 2),
        "alpha_annual_pct": (round(float(alpha_annual * 100), 2)
                             if alpha_annual is not None else None),
        "sharpe":           round(float(sharpe), 3),
        "treynor_pct":      round(float(treynor * 100), 2),
        "actual_period_pct":   round(total_port * 100, 2),          # 기간 실제 수익률
        "capm_period_pct":     round(capm_period * 100, 2),         # 기간 CAPM 기대 (실현 시장 기준)
        "expected_period_pct": round(expected_period * 100, 2),     # 기간 장기모델 기대 (ERP 5.5%)
        "mkt_period_pct":      round(total_mkt * 100, 2),           # 기간 벤치마크
        # 연율환산은 관측기간이 충분할 때만 — 짧은 창을 연율화하면 알파와 같은 이유로 폭주
        "actual_pct":       (round(float(actual_annual * 100), 2) if long_enough else None),
        "mkt_pct":          (round(float(mkt_annual * 100), 2)    if long_enough else None),
        "expected_pct":     round(float(expected_annual * 100), 2), # 연율 CAPM 기대 (모델값)
        "rf_pct":           round(float(rf_annual * 100), 2),
        "erp_pct":          round(ERP_LONGRUN * 100, 1),
        "n_days":           n_obs,
        "elapsed_days":     elapsed_days,
        "cash_basis":       "total" if cash_included else "stock_only",
        "mkt_basis":        mkt_basis,
        "us_weight_pct":    round(us_weight * 100, 1),
        "excluded":         excluded,
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
        info_lines.append(
            f"β={capm['beta']:.2f}  α={alpha_sign}{capm['alpha_pct']:.1f}%"
            f"({capm['elapsed_days']}d)  Sharpe={capm['sharpe']:.2f}"
        )
    if mdd_info:
        rec = mdd_info['recovery_pct']
        info_lines.append(f"MDD=-{mdd_info['mdd_pct']:.1f}%  회복={rec:.0f}%")
    if fx_label:
        info_lines.append(fx_label)
    if info_lines:
        ax1.text(0.99, 0.05, "\n".join(info_lines), transform=ax1.transAxes,
                 color="#cccccc", fontsize=8, ha="right", va="bottom",
                 bbox=dict(facecolor="#1a1a2e", alpha=0.7, edgecolor="#444444", boxstyle="round,pad=0.4"))

    # 하단: 총자산 (주식+현금, 만원) — 부분매도해도 현금이 포함돼 왜곡 없음
    assets = [_asset_krw(snap) for _, snap in filtered]
    assets_만 = [a / 1e4 for a in assets]
    vals_만   = [v / 1e4 for v in values]
    has_cash  = any(a != v for a, v in zip(assets, values))

    ax2.plot(dates, assets_만, color="#00a8ff", linewidth=2, label="총자산", zorder=3)
    _min, _max = min(assets_만), max(assets_만)
    if has_cash:
        ax2.plot(dates, vals_만, color="#888888", linewidth=1.2,
                 linestyle="--", label="주식 평가금", zorder=2)
        _min, _max = min(_min, min(vals_만)), max(_max, max(vals_만))
        ax2.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=8,
                   framealpha=0.8, loc="upper left")
    _margin = (_max - _min) * 0.25 or _max * 0.01
    ax2.set_ylim(_min - _margin, _max + _margin)
    ax2.fill_between(dates, assets_만, _min - _margin, alpha=0.12, color="#00a8ff")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}만"))
    ax2.set_ylabel("총자산 (주식+현금)", color="#aaaaaa", fontsize=10)

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
    ]
    asset = _asset_krw(last_v)
    if asset != curr:  # 현금이 기록된 스냅샷이면 총자산도 표시
        lines.append(f"총자산(현금 포함): {asset / 1e4:,.0f}만원")
    lines.append("")

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
        lines.append(f"  알파(α):   {alpha_em} <b>{alpha_sign}{a:.2f}%</b> "
                     f"<i>({capm['elapsed_days']}일 초과수익)</i>")
        lines.append(f"             └ {alpha_desc}")
        if capm.get("excluded"):
            lines.append(f"             └ <i>입출금/결손 추정 {len(capm['excluded'])}일 제외</i>")

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
        ea  = capm["expected_pct"]
        nd  = capm["elapsed_days"]
        ap_sign = "+" if ap >= 0 else ""
        ep_sign = "+" if ep >= 0 else ""
        # 연율환산은 관측기간이 짧으면 폭주하므로 값이 있을 때만 표기
        aa_txt = f" / 연율 {capm['actual_pct']:+.1f}%" if capm["actual_pct"] is not None else ""
        mkt_lbl = "벤치마크(원화)" if capm.get("mkt_basis") == "blended" else "S&P500(원화)"
        cp = capm["capm_period_pct"]
        cp_sign = "+" if cp >= 0 else ""
        lines.append(f"  실제 수익률: <b>{ap_sign}{ap:.1f}%</b>  <i>({nd}일{aa_txt})</i>")
        lines.append(f"  CAPM 기대치: {cp_sign}{cp:.1f}%  <i>({nd}일 / β로 설명되는 몫)</i>")
        lines.append(
            f"  <i>(Rf {capm['rf_pct']:.1f}% | {mkt_lbl} {capm['mkt_period_pct']:+.1f}%"
            f" | 장기 기대 {ep_sign}{ep:.1f}% (연율 {ea:+.1f}%, ERP {capm['erp_pct']:.1f}%)"
            f" | {nd}일 기준)</i>"
        )

    return "\n".join(lines)
