#!/usr/bin/env python3
"""공통 유틸리티 함수."""

import json
import requests
from datetime import date as _date
from pathlib import Path

_calendars: dict = {}

_FX_CACHE_PATH = Path(__file__).parent / "fx_cache.json"
FX_FALLBACK = 1400.0  # 환율 조회 불가 시 최후 기본값


def get_usdkrw() -> dict:
    """USD/KRW 환율 조회.

    yfinance 실패 시 마지막 성공값(fx_cache.json), 그것도 없으면 FX_FALLBACK.
    반환: {"rate": float, "prev_rate": float, "change_pct": float}
    """
    import yfinance as yf
    try:
        fi = yf.Ticker("USDKRW=X").fast_info
        rate, prev = fi.last_price, fi.previous_close
        if rate and rate > 0:
            prev = prev if prev and prev > 0 else rate
            result = {
                "rate": round(rate, 2),
                "prev_rate": round(prev, 2),
                "change_pct": round((rate - prev) / prev * 100, 2),
            }
            try:
                _FX_CACHE_PATH.write_text(json.dumps(result))
            except Exception:
                pass
            return result
    except Exception:
        pass
    try:
        cached = json.loads(_FX_CACHE_PATH.read_text())
        if cached.get("rate", 0) > 0:
            return {"rate": cached["rate"], "prev_rate": cached["rate"], "change_pct": 0.0}
    except Exception:
        pass
    return {"rate": FX_FALLBACK, "prev_rate": FX_FALLBACK, "change_pct": 0.0}


def _get_calendar(name: str):
    if name not in _calendars:
        import exchange_calendars as xcals
        _calendars[name] = xcals.get_calendar(name)
    return _calendars[name]


def is_us_market_holiday(d: _date | None = None) -> bool:
    """NYSE가 공휴일(주말 포함)이면 True."""
    try:
        if d is None:
            d = _date.today()
        return not _get_calendar("XNYS").is_session(str(d))
    except Exception:
        return False


def is_kr_market_holiday(d: _date | None = None) -> bool:
    """KRX가 공휴일(주말 포함)이면 True."""
    try:
        if d is None:
            d = _date.today()
        return not _get_calendar("XKRX").is_session(str(d))
    except Exception:
        return False


def send_telegram(bot_token: str, chat_id: str, text: str):
    """Telegram 메시지 전송 (4096자 초과 시 자동 분할)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    while text:
        chunk, text = text[:4000], text[4000:]
        try:
            requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass
