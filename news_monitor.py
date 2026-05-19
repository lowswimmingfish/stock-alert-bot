#!/usr/bin/env python3
"""News monitor - checks portfolio holdings AND broad market topics for important events."""

import json
import hashlib
import logging
import time
import threading
import pytz
import requests
import anthropic
import yfinance as yf
from tavily import TavilyClient
from pathlib import Path
from datetime import datetime, timedelta
from config_loader import load_config, DATA_DIR
from utils import send_telegram

KST = pytz.timezone("Asia/Seoul")

SEEN_PATH = DATA_DIR / "seen_news.json"
LOG_PATH = Path(__file__).parent / "news_monitor.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)

HASH_TTL_DAYS   = 7   # 제목 해시 만료 (일)
ALERT_TTL_DAYS  = 14  # 발송 알림 기억 기간 (일) — 이 기간 내 유사 내용 차단
MAX_ALERT_MEM   = 60  # 프롬프트에 넘길 최대 알림 수
NEWS_MAX_AGE_H  = 48  # 이 시간(h)보다 오래된 뉴스는 스킵
MAX_ALERTS_PER_RUN = 5  # 1회 실행당 최대 알림 수 (배포 직후 폭탄 방지)

# 시장 전반 모니터링 토픽
MARKET_TOPICS = [
    {"name": "Fed/FOMC",    "query": "Federal Reserve FOMC interest rate decision statement"},
    {"name": "미중 무역",   "query": "US China trade tariffs sanctions economic policy"},
    {"name": "지정학 리스크","query": "geopolitical risk war conflict market impact"},
    {"name": "인플레이션/CPI","query": "US CPI inflation data consumer price index"},
    {"name": "농산물 시장", "query": "corn soybean wheat grain commodity market USDA"},
    {"name": "에너지",      "query": "oil gas energy price OPEC supply"},
    {"name": "반도체",      "query": "semiconductor chip shortage supply demand AI"},
]


# ── seen_news.json 구조 ────────────────────────────────────────────────────────
# {
#   "hashes": {"md5": unix_ts, ...},          ← 제목 해시 + 최초 발견 시각
#   "recent_alerts": [                         ← 실제 발송된 알림 메모리
#     {"topic": "NVDA", "summary": "실적 서프라이즈", "ts": unix_ts},
#     ...
#   ]
# }

def load_seen() -> dict:
    if SEEN_PATH.exists():
        try:
            raw = json.loads(SEEN_PATH.read_text())
            if isinstance(raw, list):
                # 구버전(list) → 신버전으로 마이그레이션
                return {"hashes": {h: 0 for h in raw}, "recent_alerts": []}
            raw.setdefault("hashes", {})
            raw.setdefault("recent_alerts", [])
            return raw
        except Exception:
            pass
    return {"hashes": {}, "recent_alerts": []}


def save_seen(seen: dict):
    now = time.time()
    hash_ttl  = HASH_TTL_DAYS  * 86400
    alert_ttl = ALERT_TTL_DAYS * 86400
    # 만료 항목 정리
    seen["hashes"]        = {k: v for k, v in seen["hashes"].items()        if now - v < hash_ttl}
    seen["recent_alerts"] = [a    for a    in seen["recent_alerts"]          if now - a["ts"] < alert_ttl]
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False))


def make_key(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()


def is_new_title(seen: dict, title: str) -> bool:
    return make_key(title) not in seen["hashes"]


def mark_title(seen: dict, title: str):
    seen["hashes"][make_key(title)] = time.time()


def add_alert_memory(seen: dict, topic: str, summary: str):
    seen["recent_alerts"].append({"topic": topic, "summary": summary, "ts": time.time()})
    if len(seen["recent_alerts"]) > MAX_ALERT_MEM:
        seen["recent_alerts"] = seen["recent_alerts"][-MAX_ALERT_MEM:]


def build_recent_alerts_text(seen: dict) -> str:
    """최근 알림 내역을 프롬프트용 텍스트로 변환."""
    alerts = seen.get("recent_alerts", [])
    if not alerts:
        return "없음"
    lines = []
    for a in sorted(alerts, key=lambda x: x["ts"], reverse=True)[:20]:
        ts_str = datetime.fromtimestamp(a["ts"], KST).strftime("%m/%d %H:%M")
        lines.append(f"[{ts_str}] [{a['topic']}] {a['summary']}")
    return "\n".join(lines)


def parse_yfinance_news(raw_news):
    """yfinance 뉴스에서 title/link/pub_date 추출."""
    items = []
    for n in raw_news:
        content = n.get("content", {})
        if isinstance(content, dict):
            title    = content.get("title", "")
            canonical = content.get("canonicalUrl", {})
            link     = canonical.get("url", "") if isinstance(canonical, dict) else ""
            summary  = content.get("summary", "")
            pub_date = content.get("pubDate", "")
        else:
            title    = n.get("title", "")
            link     = n.get("link", "")
            summary  = ""
            pub_date = n.get("providerPublishTime", "")  # unix ts or str
        if title and isinstance(title, str):
            items.append({"title": title, "link": link, "summary": summary, "pub_date": pub_date})
    return items


def is_too_old(pub_date) -> bool:
    """pub_date가 NEWS_MAX_AGE_H 시간보다 오래됐으면 True."""
    if not pub_date:
        return False
    try:
        if isinstance(pub_date, (int, float)):
            age_h = (time.time() - float(pub_date)) / 3600
            return age_h > NEWS_MAX_AGE_H
        # ISO 문자열 파싱 시도
        from dateutil import parser as dparser
        dt = dparser.parse(str(pub_date))
        if dt.tzinfo is None:
            import pytz as _tz
            dt = _tz.utc.localize(dt)
        age_h = (datetime.now(pytz.utc) - dt).total_seconds() / 3600
        return age_h > NEWS_MAX_AGE_H
    except Exception:
        return False


def fetch_stock_news(ticker):
    try:
        return parse_yfinance_news(yf.Ticker(ticker).news or [])
    except Exception as e:
        logging.error(f"yfinance news error {ticker}: {e}")
        return []


def fetch_stock_news_tavily(ticker, tavily_key):
    """Tavily로 US 종목 뉴스 검색 (yfinance보다 최신·정확). 실패 시 yfinance fallback."""
    try:
        client = TavilyClient(api_key=tavily_key)
        resp = client.search(f"{ticker} stock news", max_results=6, topic="news")
        items = []
        for r in resp.get("results", []):
            title = r.get("title", "")
            if title and isinstance(title, str):
                items.append({
                    "title":    title,
                    "link":     r.get("url", ""),
                    "summary":  r.get("content", "")[:200],
                    "pub_date": r.get("published_date", ""),
                })
        if items:
            return items
    except Exception as e:
        logging.warning(f"Tavily stock news error {ticker}: {e}")
    # fallback
    return fetch_stock_news(ticker)


def fetch_kr_news(ticker, name):
    try:
        from html.parser import HTMLParser
        resp = requests.get(
            f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        class TitleParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.titles = []
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    d = dict(attrs)
                    href, title = d.get("href", ""), d.get("title", "")
                    if "/news/read" in href and title:
                        self.titles.append({"title": title, "link": href, "pub_date": "", "summary": ""})
        p = TitleParser()
        p.feed(resp.text)
        return p.titles[:8]
    except Exception as e:
        logging.error(f"KR news error {name}: {e}")
        return []


def fetch_market_topic_news(topic, tavily_key):
    try:
        client = TavilyClient(api_key=tavily_key)
        resp = client.search(topic["query"], max_results=5, topic="news")
        items = []
        for r in resp.get("results", []):
            title = r.get("title", "")
            if title and isinstance(title, str):
                items.append({
                    "title":    title,
                    "link":     r.get("url", ""),
                    "summary":  r.get("content", "")[:200],
                    "pub_date": r.get("published_date", ""),
                })
        return items
    except Exception as e:
        logging.error(f"Tavily error {topic['name']}: {e}")
        return []


def is_important(news_items, name, config, recent_text, is_market_topic=False):
    """
    Claude Haiku로 중요도 판단.
    - recent_text: build_recent_alerts_text(seen) 결과 (lock 밖에서 미리 추출)
    - 반환: (alert_text | None, summary_for_memory | None)
    """
    if not news_items:
        return None, None

    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    news_text = "\n".join([
        f"- {item['title']}" + (f"\n  {item.get('summary','')}" if item.get("summary") else "")
        for item in news_items[:8]
    ])

    if is_market_topic:
        criteria = """중요한 이슈 기준 (시장 전반):
- Fed 금리 결정, FOMC 성명 또는 파월 발언
- 예상치 크게 벗어난 CPI/PPI/고용 지표
- 미중 무역 전쟁 확대 또는 새 관세 발표
- 지정학적 긴장 고조 (전쟁, 제재, 에너지 공급 차질)
- 시장 전체에 충격을 줄 수 있는 이벤트"""
    else:
        criteria = """중요한 이슈 기준 (개별 종목):
- 실적 발표 / 어닝 서프라이즈·쇼크
- 대규모 M&A, 분사, 상장
- CEO 교체 / 주요 경영진 변동
- 규제 이슈, 소송, 정부 제재
- 주가에 직접 영향을 줄 사건"""

    prompt = f"""다음 뉴스를 분석해서 중요 알림 여부를 판단해줘: [{name}]

{criteria}

━━ 최근 14일 이내 이미 보낸 알림 (참고용) ━━
{recent_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

뉴스:
{news_text}

판단 규칙:
1. 위 "이미 보낸 알림"과 같은 주제·내용이면 → DUPLICATE
2. "실적 발표 예정" 류인데 이미 보낸 알림에 "실적 발표" 결과가 있으면 → DUPLICATE
3. 예정 이벤트가 이미 지났거나 결과가 나온 것 같으면 → DUPLICATE
4. 진짜 새롭고 중요한 이슈면 → ALERT

중요한 이슈:
ALERT: [한 줄 요약 (30자 이내)]
이유: [왜 중요한지 2-3줄]

중복 또는 이미 지난 이벤트:
DUPLICATE: [이유]

해당 없음:
NONE"""

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            result = resp.content[0].text.strip()
            if result.startswith("ALERT:"):
                # 첫 줄에서 요약 추출
                summary = result.split("\n")[0].replace("ALERT:", "").strip()
                return result, summary
            return None, None
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                logging.error(f"Claude error for {name}: {e}")
    return None, None


def run():
    config    = load_config()
    seen      = load_seen()
    bot_token = config["telegram"]["bot_token"]
    chat_id   = config["telegram"]["chat_id"]
    tavily_key = config.get("tavily_api_key", "")
    alerts_sent = 0
    seen_lock = threading.Lock()

    # 1. 보유 종목 뉴스
    stocks = []
    try:
        import kis_api
        if kis_api.is_configured():
            us_raw = kis_api.get_us_balance_raw()
            kr_raw = kis_api.get_kr_balance_raw()
            stocks = (
                [{"ticker": h["ticker"], "name": h["ticker"], "is_kr": False}
                 for h in us_raw.get("holdings", [])] +
                [{"ticker": h["ticker"], "name": h["name"], "is_kr": True}
                 for h in kr_raw.get("holdings", [])]
            )
    except Exception as e:
        logging.warning(f"KIS 잔고 조회 실패, config fallback: {e}")

    if not stocks:
        stocks = (
            [{"ticker": s["ticker"], "name": s["ticker"], "is_kr": False}
             for s in config["portfolio"].get("us_stocks", [])] +
            [{"ticker": s["ticker"], "name": s.get("name", s["ticker"]), "is_kr": True}
             for s in config["portfolio"].get("kr_stocks", [])]
        )

    def check_stock(stock):
        ticker, name, is_kr = stock["ticker"], stock["name"], stock["is_kr"]
        if is_kr:
            all_items = fetch_kr_news(ticker, name)
        elif tavily_key:
            all_items = fetch_stock_news_tavily(ticker, tavily_key)
        else:
            all_items = fetch_stock_news(ticker)

        # 너무 오래된 뉴스 제거
        all_items = [i for i in all_items if not is_too_old(i.get("pub_date"))]

        with seen_lock:
            new_items = [i for i in all_items if is_new_title(seen, i["title"])]
            for i in new_items:
                mark_title(seen, i["title"])
            # 해시를 Claude 호출 전에 저장 — 재시작 시 중복 처리 방지
            save_seen(seen)

        if not new_items:
            return None
        logging.info(f"{name}: {len(new_items)} new articles")

        with seen_lock:
            recent_text = build_recent_alerts_text(seen)
        alert, summary = is_important(new_items, name, config, recent_text, is_market_topic=False)

        if alert:
            with seen_lock:
                add_alert_memory(seen, name, summary)
            return (
                f"<b>종목 이슈 알림</b>\n"
                f"종목: <b>{name}</b> ({ticker})\n"
                f"시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n\n"
                f"{alert}"
            )
        return None

    # Tavily 사용 시 순차 처리 (rate limit 방지), 아닐 땐 병렬
    if tavily_key:
        for stock in stocks:
            if alerts_sent >= MAX_ALERTS_PER_RUN:
                break
            msg = check_stock(stock)
            if msg:
                send_telegram(bot_token, chat_id, msg)
                alerts_sent += 1
            if not stock["is_kr"]:
                time.sleep(0.8)
    else:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for msg in ex.map(check_stock, stocks):
                if alerts_sent >= MAX_ALERTS_PER_RUN:
                    break
                if msg:
                    send_telegram(bot_token, chat_id, msg)
                    alerts_sent += 1

    # 2. 시장 전반 토픽
    if tavily_key:
        def check_topic(topic):
            all_items = fetch_market_topic_news(topic, tavily_key)
            all_items = [i for i in all_items if not is_too_old(i.get("pub_date"))]

            with seen_lock:
                new_items = [i for i in all_items if is_new_title(seen, i["title"])]
                for i in new_items:
                    mark_title(seen, i["title"])
                save_seen(seen)

            if not new_items:
                return None
            logging.info(f"Market topic [{topic['name']}]: {len(new_items)} new articles")

            with seen_lock:
                recent_text = build_recent_alerts_text(seen)
            alert, summary = is_important(new_items, topic["name"], config, recent_text, is_market_topic=True)

            if alert:
                with seen_lock:
                    add_alert_memory(seen, topic["name"], summary)
                return (
                    f"<b>시장 이슈 알림</b>\n"
                    f"토픽: <b>{topic['name']}</b>\n"
                    f"시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n\n"
                    f"{alert}"
                )
            return None

        # 순차 처리: Tavily 동시 요청 제한 방지 (0.5초 간격)
        for topic in MARKET_TOPICS:
            if alerts_sent >= MAX_ALERTS_PER_RUN:
                break
            msg = check_topic(topic)
            if msg:
                send_telegram(bot_token, chat_id, msg)
                alerts_sent += 1
            time.sleep(0.5)

    save_seen(seen)
    logging.info(f"Run complete. {alerts_sent} alerts sent.")


if __name__ == "__main__":
    run()
