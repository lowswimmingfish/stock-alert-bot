#!/usr/bin/env python3
"""공통 유틸리티 함수."""

import requests
from datetime import date as _date

_calendars: dict = {}


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
