#!/usr/bin/env python3
"""News monitor - checks portfolio holdings AND broad market topics for important events."""

import json
import hashlib
import logging
import requests
import anthropic
import yfinance as yf
from tavily import TavilyClient
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config_loader import load_config, DATA_DIR

SEEN_PATH = DATA_DIR / "seen_news.json"
LOG_PATH = Path(__file__).parent / "news_monitor.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)

# 시장 전반 모니터링 토픽 (보유 종목 외 거시경제 이슈)
MARKET_TOPICS = [
    {"name": "Fed/FOMC", "query": "Federal Reserve FOMC interest rate decision statement"},
    {"name": "미중 무역", "query": "US China trade tariffs sanctions economic policy"},
    {"name": "지정학 리스크", "query": "geopolitical risk war conflict market impact"},
    {"name": "인플레이션/CPI", "query": "US CPI inflation data consumer price index"},
    {"name": "농산물 시장", "query": "corn soybean wheat grain commodity market USDA"},
    {"name": "에너지", "query": "oil gas energy price OPEC supply"},
    {"name": "반도체", "query": "semiconductor chip shortage supply demand AI"},
]


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    # seen 항목이 너무 많아지면 최근 2000개만 유지
    seen_list = list(seen)[-2000:]
    with open(SEEN_PATH, "w") as f:
        json.dump(seen_list, f)


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def parse_yfinance_news(raw_news):
    """yfinance 뉴스 구조에서 title/link 추출."""
    items = []
    for n in raw_news:
        content = n.get("content", {})
        if isinstance(content, dict):
            title = content.get("title", "")
            canonical = content.get("canonicalUrl", {})
            link = canonical.get("url", "") if isinstance(canonical, dict) else ""
            summary = content.get("summary", "")
        else:
            title = n.get("title", "")
            link = n.get("link", "")
            summary = ""
        if title and isinstance(title, str):
            items.append({"title": title, "link": link, "summary": summary})
    return items


def fetch_stock_news(ticker):
    """yfinance로 종목 뉴스 가져오기."""
    try:
        return parse_yfinance_news(yf.Ticker(ticker).news or [])
    except Exception as e:
        logging.error(f"yfinance news error {ticker}: {e}")
        return []


def fetch_kr_news(ticker, name):
    """네이버 금융에서 한국 종목 뉴스."""
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
                        self.titles.append({"title": title, "link": href})

        p = TitleParser()
        p.feed(resp.text)
        return p.titles[:8]
    except Exception as e:
        logging.error(f"KR news error {name}: {e}")
        return []


def fetch_market_topic_news(topic, tavily_key):
    """Tavily로 시장 전반 토픽 뉴스 검색."""
    try:
        client = TavilyClient(api_key=tavily_key)
        resp = client.search(topic["query"], max_results=5, topic="news")
        items = []
        for r in resp.get("results", []):
            title = r.get("title", "")
            if title and isinstance(title, str):
                items.append({
                    "title": title,
                    "link": r.get("url", ""),
                    "summary": r.get("content", "")[:200],
                })
        return items
    except Exception as e:
        logging.error(f"Tavily error {topic['name']}: {e}")
        return []


def make_key(title):
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def is_important(news_items, name, config, is_market_topic=False):
    """Claude Haiku로 중요도 판단. 재시도 1회."""
    if not news_items:
        return None

    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    news_text = "\n".join([
        f"- {item['title']}" + (f"\n  {item.get('summary','')}" if item.get('summary') else "")
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

    prompt = f"""다음 뉴스 헤드라인을 분석해줘: [{name}]

{criteria}

뉴스:
{news_text}

중요한 이슈가 있으면:
ALERT: [한 줄 요약]
이유: [왜 중요한지 2-3줄]

없으면:
NONE"""

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            result = resp.content[0].text.strip()
            return result if result.startswith("ALERT:") else None
        except Exception as e:
            if attempt == 0:
                import time; time.sleep(2)
            else:
                logging.error(f"Claude error for {name}: {e}")
    return None


def run():
    config = load_config()
    seen = load_seen()
    bot_token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    tavily_key = config.get("tavily_api_key", "")
    alerts_sent = 0

    # 1. 보유 종목 뉴스
    stocks = (
        [{"ticker": s["ticker"], "name": s["ticker"], "is_kr": False}
         for s in config["portfolio"]["us_stocks"]] +
        [{"ticker": s["ticker"], "name": s.get("name", s["ticker"]), "is_kr": True}
         for s in config["portfolio"]["kr_stocks"]]
    )

    def check_stock(stock):
        ticker, name, is_kr = stock["ticker"], stock["name"], stock["is_kr"]
        news_items = fetch_kr_news(ticker, name) if is_kr else fetch_stock_news(ticker)
        new_items = [i for i in news_items if make_key(i["title"]) not in seen]
        for i in new_items:
            seen.add(make_key(i["title"]))
        if not new_items:
            return None
        logging.info(f"{name}: {len(new_items)} new articles")
        alert = is_important(new_items, name, config, is_market_topic=False)
        if alert:
            return (
                f"<b>종목 이슈 알림</b>\n"
                f"종목: <b>{name}</b> ({ticker})\n"
                f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"{alert}"
            )
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        for msg in ex.map(check_stock, stocks):
            if msg:
                send_telegram(bot_token, chat_id, msg)
                alerts_sent += 1

    # 2. 시장 전반 토픽 (Tavily 필요)
    if tavily_key:
        def check_topic(topic):
            news_items = fetch_market_topic_news(topic, tavily_key)
            new_items = [i for i in news_items if make_key(i["title"]) not in seen]
            for i in new_items:
                seen.add(make_key(i["title"]))
            if not new_items:
                return None
            logging.info(f"Market topic [{topic['name']}]: {len(new_items)} new articles")
            alert = is_important(new_items, topic["name"], config, is_market_topic=True)
            if alert:
                return (
                    f"<b>시장 이슈 알림</b>\n"
                    f"토픽: <b>{topic['name']}</b>\n"
                    f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"{alert}"
                )
            return None

        with ThreadPoolExecutor(max_workers=3) as ex:
            for msg in ex.map(check_topic, MARKET_TOPICS):
                if msg:
                    send_telegram(bot_token, chat_id, msg)
                    alerts_sent += 1

    save_seen(seen)
    logging.info(f"Run complete. {alerts_sent} alerts sent.")


if __name__ == "__main__":
    run()
