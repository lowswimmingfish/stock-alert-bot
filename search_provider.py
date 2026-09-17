#!/usr/bin/env python3
"""뉴스 검색: Tavily 우선, 한도 초과·오류 시 Google News RSS로 대체.

반환 형식은 Tavily 결과와 동일한 dict 리스트:
  {"title", "url", "content", "published_date"}
"""

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape

import requests

logger = logging.getLogger(__name__)

# 한도 초과가 감지되면 이 시간 동안 Tavily 호출을 건너뛰고 바로 Google News 사용
TAVILY_COOLDOWN_SEC = 60 * 60
_QUOTA_MARKERS = ("usage limit", "exceeds your plan", "quota", "429", "432")

_lock = threading.Lock()
_tavily_blocked_until = 0.0


def _tavily_available() -> bool:
    return time.time() >= _tavily_blocked_until


def _mark_tavily_exhausted(err: Exception):
    global _tavily_blocked_until
    with _lock:
        if _tavily_available():
            logger.warning(
                f"Tavily 한도 초과 — {TAVILY_COOLDOWN_SEC // 60}분간 Google News로 대체: {err}"
            )
        _tavily_blocked_until = time.time() + TAVILY_COOLDOWN_SEC


def _search_tavily(query: str, max_results: int, tavily_key: str) -> list[dict]:
    from tavily import TavilyClient
    resp = TavilyClient(api_key=tavily_key).search(query, max_results=max_results, topic="news")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "published_date": r.get("published_date", ""),
        }
        for r in resp.get("results", [])
        if r.get("title")
    ]


def search_google_news(query: str, max_results: int = 5, recent_days: int = 3) -> list[dict]:
    """Google News RSS 검색. 한글이 포함된 검색어는 한국판, 아니면 미국판."""
    is_kr = bool(re.search(r"[가-힣]", query))
    locale = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"} if is_kr else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    q = f"{query} when:{recent_days}d" if recent_days else query
    resp = requests.get(
        "https://news.google.com/rss/search",
        params={"q": q, **locale},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        pub = it.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub).isoformat()
        except Exception:
            pass
        desc = unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        items.append({
            "title": title,
            "url": it.findtext("link") or "",
            "content": re.sub(r"\s+", " ", desc).strip(),
            "published_date": pub,
        })
        if len(items) >= max_results:
            break
    return items


def search_news(query: str, max_results: int = 5, tavily_key: str = "") -> list[dict]:
    """Tavily → (한도 초과/오류/키 없음) → Google News RSS."""
    if tavily_key and _tavily_available():
        try:
            return _search_tavily(query, max_results, tavily_key)
        except Exception as e:
            if any(m in str(e).lower() for m in _QUOTA_MARKERS):
                _mark_tavily_exhausted(e)
            else:
                logger.warning(f"Tavily 검색 오류, Google News로 대체 ({query}): {e}")
    try:
        return search_google_news(query, max_results)
    except Exception as e:
        logger.error(f"Google News 검색 실패 ({query}): {e}")
        return []
